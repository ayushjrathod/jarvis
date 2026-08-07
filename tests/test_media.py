"""Media control tests: intent parsing, LLM-fallback validation, search-result
formatting, MPRIS call shapes. stdlib unittest — no network, no D-Bus, no LLM."""

import unittest
from types import SimpleNamespace
from unittest import mock

from dispatcher import automations, spotify
from dispatcher.spotify import MAYBE, Intent


def _cfg(**over):
    media = {"enabled": True, "nl_detect": True, "llm_fallback": True,
             "market": "IN", "search_limit": 5, "credentials": "data/spotify.json"}
    media.update(over)
    return SimpleNamespace(root=__import__("pathlib").Path("/nonexistent"),
                           media=media)


class TestDetectPlay(unittest.TestCase):
    def test_plain_song(self):
        i = spotify.detect("play bohemian rhapsody")
        self.assertEqual((i.action, i.query, i.type), ("play", "bohemian rhapsody", "track"))

    def test_strips_on_spotify_suffix(self):
        self.assertEqual(spotify.detect("play thunderstruck on spotify").query,
                         "thunderstruck")

    def test_strips_some(self):
        self.assertEqual(spotify.detect("play some pink floyd").query, "pink floyd")

    def test_put_on(self):
        self.assertEqual(spotify.detect("put on daft punk").query, "daft punk")

    def test_song_by_artist_kept_whole(self):
        # Spotify's search handles "title artist" fine; splitting loses signal
        self.assertEqual(spotify.detect("play numb by linkin park").query,
                         "numb by linkin park")

    def test_album_type(self):
        i = spotify.detect("play the album abbey road")
        self.assertEqual((i.type, i.query), ("album", "abbey road"))

    def test_artist_type(self):
        i = spotify.detect("play the artist radiohead")
        self.assertEqual((i.type, i.query), ("artist", "radiohead"))

    def test_playlist_type(self):
        i = spotify.detect("play the playlist deep focus")
        self.assertEqual((i.type, i.query), ("playlist", "deep focus"))

    def test_podcast_type(self):
        i = spotify.detect("play the podcast darknet diaries")
        self.assertEqual((i.type, i.query), ("show", "darknet diaries"))

    def test_wake_prefix_and_punctuation(self):
        self.assertEqual(spotify.detect("Hey Jarvis, play Clair de Lune.").query,
                         "clair de lune")

    def test_bare_play_is_resume(self):
        self.assertEqual(spotify.detect("play").action, "resume")


class TestDetectTransport(unittest.TestCase):
    def test_pause_variants(self):
        for t in ("pause", "pause the music", "stop the music", "stop spotify"):
            self.assertEqual(spotify.detect(t).action, "pause", t)

    def test_resume_variants(self):
        for t in ("resume", "unpause", "continue the music", "keep playing"):
            self.assertEqual(spotify.detect(t).action, "resume", t)

    def test_next_variants(self):
        for t in ("next", "skip", "skip this song", "next track", "skip it"):
            self.assertEqual(spotify.detect(t).action, "next", t)

    def test_previous_variants(self):
        for t in ("previous", "go back", "previous song", "play the last track"):
            self.assertEqual(spotify.detect(t).action, "previous", t)

    def test_now_playing_variants(self):
        for t in ("what's playing", "what song is this", "who sings this",
                  "what am i listening to"):
            self.assertEqual(spotify.detect(t).action, "now_playing", t)

    def test_now_playing_survives_question_mark(self):
        # the question-mark veto must not swallow the one question we answer
        self.assertEqual(spotify.detect("what's playing?").action, "now_playing")


class TestDetectVolume(unittest.TestCase):
    def test_absolute(self):
        i = spotify.detect("volume 40")
        self.assertEqual(i.action, "volume")
        self.assertAlmostEqual(i.arg, 0.4)

    def test_percent_and_set_prefix(self):
        self.assertAlmostEqual(spotify.detect("set the volume to 75 percent").arg, 0.75)

    def test_clamped_above_100(self):
        self.assertAlmostEqual(spotify.detect("volume 300").arg, 1.0)

    def test_relative(self):
        up, down = spotify.detect("turn it up"), spotify.detect("turn it down")
        self.assertGreater(up.delta, 0)
        self.assertLess(down.delta, 0)
        self.assertIsNone(up.arg)

    def test_mute(self):
        self.assertAlmostEqual(spotify.detect("mute the music").arg, 0.0)


class TestDetectNegatives(unittest.TestCase):
    """The divert must never steal non-music speech — that's how it stays safe
    to run ahead of Claude on every mode=auto task."""

    def test_idioms_vetoed(self):
        for t in ("play devil's advocate for a second",
                  "let me play devils advocate", "play it safe here",
                  "play a video of the talk", "play chess with me",
                  "play it by ear on the demo", "put on the kettle"):
            self.assertIsNone(spotify.detect(t), t)

    def test_questions_go_to_claude(self):
        for t in ("what should I play this weekend?",
                  "how do I play a chord on guitar?",
                  "who is the artist behind this album?"):
            self.assertIsNone(spotify.detect(t), t)

    def test_ordinary_requests_untouched(self):
        for t in ("add task renew the domain", "what's on my calendar",
                  "summarize my notes", "remember that I use neovim", ""):
            self.assertIsNone(spotify.detect(t), t)


class TestDetectMaybe(unittest.TestCase):
    def test_vague_requests_arm_the_fallback(self):
        for t in ("play something chill", "put on something to focus to",
                  "play that song from the batman movie",
                  "play something that sounds like radiohead"):
            self.assertIs(spotify.detect(t), MAYBE, t)

    def test_music_shaped_but_unparsed(self):
        self.assertIs(spotify.detect("i want to listen to some music now"), MAYBE)


# one natural sentence per VETO entry; the covers-everything test below keeps
# this honest when someone adds an idiom to the tuple
VETO_SENTENCES = {
    "devil's advocate": "let me play devil's advocate about the rollout",
    "devils advocate": "let me play devils advocate about the rollout",
    "play along": "i'll play along with the bit",
    "play it safe": "play it safe on the deploy",
    "play a role": "did the cache play a role in the outage",
    "playing field": "that levels the playing field",
    "play out": "let the argument play out",
    "play down": "play down the risk in the report",
    "play games": "don't play games with the schedule",
    "foul play": "there was no foul play here",
    "play on youtube": "play on youtube instead",
    "play the video": "play the video again",
    "play a video": "play a video of the talk",
    "play on netflix": "play on netflix tonight",
    "play tennis": "play tennis on sunday",
    "play football": "play football with the kids",
    "play cricket": "play cricket this weekend",
    "play chess": "play chess with me",
    "play a game": "play a game with me",
    "by ear": "play it by ear on the demo",
    "kettle": "put on the kettle",
}


class TestVetoWordBoundaries(unittest.TestCase):
    """VETO beats every pattern, so it is the one rule that must not overreach.
    It matched as a bare substring until 2026-08-08, and "play out" is inside
    "play Outkast" while "play down" is inside "play downtempo jazz" — two
    perfectly ordinary commands that returned None and went to Claude as text.
    """

    def test_band_names_containing_a_vetoed_phrase_now_play(self):
        for text, query in (("play Outkast", "outkast"),
                            ("play downtempo jazz", "downtempo jazz"),
                            ("play Outlandish", "outlandish"),
                            ("play Downstait", "downstait")):
            with self.subTest(text=text):
                i = spotify.detect(text)
                self.assertIsNotNone(i, text)
                self.assertEqual((i.action, i.query), ("play", query))

    def test_the_sample_map_covers_every_veto_entry(self):
        self.assertEqual(set(VETO_SENTENCES), set(spotify.VETO))

    def test_every_veto_entry_still_vetoes_its_idiom(self):
        # narrowing the match is only safe if the list still does its job
        for entry, sentence in VETO_SENTENCES.items():
            with self.subTest(entry=entry):
                self.assertTrue(spotify._VETO_RE.search(sentence))
                self.assertIsNone(spotify.detect(sentence), sentence)


class TestLooksMusicalWordBoundaries(unittest.TestCase):
    """A false MAYBE is not a rounding error. MAYBE *diverts*, so the request
    is spent on a media-parse call and answered "I couldn't work out what to
    play." — it never reaches Claude at all. Until 2026-08-08 the music-word
    test also accepted bare substrings: "band" ⊂ "abandoned", "tune" ⊂
    "fortune", "song" ⊂ "songwriter".
    """

    def test_substrings_of_ordinary_words_no_longer_divert(self):
        for t in ("listen to my abandoned draft",
                  "let's hear your take on the fortune 500 list",
                  "hear me out on the songwriter's contract"):
            self.assertIsNone(spotify.detect(t), t)

    def test_helper_is_word_level_not_substring_level(self):
        self.assertFalse(spotify._looks_musical("an abandoned fortune songwriter"))
        # …but still tolerant of punctuation, which a bare token split is not
        self.assertTrue(spotify._looks_musical("some music, please"))

    def test_genuine_fuzzy_requests_still_arm_the_fallback(self):
        for t in ("i want to listen to some music now",
                  "i'd love to hear that new taylor swift album",
                  "put on something chill", "play something chill"):
            self.assertIs(spotify.detect(t), MAYBE, t)


class TestAutomationPrecedence(unittest.TestCase):
    """"every morning play jazz" is a standing automation, not a play command —
    the automation divert runs first in POST /task, so it must claim it."""

    def test_scheduled_music_is_an_automation(self):
        self.assertTrue(automations.detect("every morning at 8 play jazz"))

    def test_unscheduled_music_is_not(self):
        self.assertFalse(automations.detect("play jazz"))


class TestParseFallback(unittest.TestCase):
    def test_valid(self):
        intent = spotify.validate_parsed(spotify.parse_response(
            '{"action": "play", "search": "lo-fi beats", "type": "playlist"}'))
        self.assertEqual((intent.action, intent.query, intent.type),
                         ("play", "lo-fi beats", "playlist"))

    def test_fenced_json(self):
        reply = 'Sure!\n```json\n{"action":"play","search":"miles davis"}\n```'
        self.assertEqual(spotify.validate_parsed(
            spotify.parse_response(reply)).query, "miles davis")

    def test_action_none_returns_none(self):
        self.assertIsNone(spotify.validate_parsed(
            spotify.parse_response('{"action": "none"}')))

    def test_unknown_type_falls_back_to_track(self):
        self.assertEqual(spotify.validate_parsed(
            {"action": "play", "search": "x", "type": "banana"}).type, "track")

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            spotify.parse_response("no json here")
        with self.assertRaises(ValueError):
            spotify.parse_response("{not valid json}")
        with self.assertRaises(ValueError):
            spotify.validate_parsed({"action": "play", "search": "  "})


SEARCH_JSON = {
    "tracks": {"items": [
        {"uri": "spotify:track:abc", "name": "Bohemian Rhapsody",
         "artists": [{"name": "Queen"}]},
        {"uri": "spotify:track:def", "name": "Cover version",
         "artists": [{"name": "Someone"}]},
    ]}
}


class TestSearch(unittest.TestCase):
    def _patched(self, payload, status=200):
        resp = mock.Mock(status_code=status, json=mock.Mock(return_value=payload))
        return mock.patch.object(spotify.httpx, "get", return_value=resp)

    def setUp(self):
        spotify._token_cache = ("tok", 9e12)   # skip the token round trip

    def tearDown(self):
        spotify._token_cache = None

    def test_takes_top_hit(self):
        with self._patched(SEARCH_JSON):
            hit = spotify.search(_cfg(), "bohemian rhapsody", "track")
        self.assertEqual(hit["uri"], "spotify:track:abc")
        self.assertEqual(hit["artist"], "Queen")

    def test_market_and_limit_passed(self):
        with self._patched(SEARCH_JSON) as get:
            spotify.search(_cfg(market="US", search_limit=3), "x", "track")
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["market"], params["limit"]), ("US", 3))

    def test_no_results(self):
        with self._patched({"tracks": {"items": []}}):
            self.assertIsNone(spotify.search(_cfg(), "asdkjhasd", "track"))

    def test_null_padded_playlist_items_skipped(self):
        payload = {"playlists": {"items": [None, {"uri": "spotify:playlist:p",
                                                  "name": "Deep Focus"}]}}
        with self._patched(payload):
            hit = spotify.search(_cfg(), "deep focus", "playlist")
        self.assertEqual(hit["uri"], "spotify:playlist:p")

    def test_http_error_is_media_error(self):
        with self._patched({}, status=429):
            with self.assertRaises(spotify.MediaError):
                spotify.search(_cfg(), "x", "track")

    def test_missing_credentials(self):
        spotify._token_cache = None
        with mock.patch.object(spotify, "credentials", return_value=None):
            with self.assertRaises(spotify.MediaError) as ctx:
                spotify.search(_cfg(), "x", "track")
        self.assertEqual(str(ctx.exception), "no-credentials")


class TestSpokenSentences(unittest.TestCase):
    def test_track(self):
        self.assertEqual(
            spotify._spoken_track({"name": "Numb", "artist": "Linkin Park",
                                   "type": "track"}),
            "Playing Numb by Linkin Park.")

    def test_album(self):
        self.assertEqual(
            spotify._spoken_track({"name": "Abbey Road", "artist": "The Beatles",
                                   "type": "album"}),
            "Playing the album Abbey Road by The Beatles.")

    def test_artist(self):
        self.assertEqual(
            spotify._spoken_track({"name": "Radiohead", "artist": "",
                                   "type": "artist"}),
            "Playing Radiohead.")

    def test_playlist_has_no_by_clause(self):
        self.assertEqual(
            spotify._spoken_track({"name": "Deep Focus", "artist": "Spotify",
                                   "type": "playlist"}),
            "Playing the playlist Deep Focus.")


class _FakeConn:
    """Records MPRIS traffic instead of touching the session bus."""

    def __init__(self, metadata=None, status="Playing", volume=0.5):
        self.calls = []
        self.metadata = metadata if metadata is not None else {
            "xesam:title": ("s", "Numb"),
            "xesam:artist": ("as", ["Linkin Park"]),
            "xesam:album": ("s", "Meteora"),
        }
        self.status, self.volume = status, volume

    def close(self):
        self.calls.append(("close",))


class TestRunIntent(unittest.TestCase):
    """run_intent's contract: always a spoken sentence, never an exception."""

    def setUp(self):
        self.conn = _FakeConn()
        patches = [
            mock.patch.object(spotify, "_open_connection", return_value=self.conn),
            mock.patch.object(spotify, "is_running", return_value=True),
            mock.patch.object(spotify, "ensure_running"),
            mock.patch.object(spotify, "SETTLE_S", 0),
            mock.patch.object(spotify, "_player_call",
                              side_effect=lambda c, m, *a: c.calls.append((m, a))),
            mock.patch.object(spotify, "_prop_get",
                              side_effect=lambda c, n: {"Metadata": c.metadata,
                                                        "PlaybackStatus": c.status,
                                                        "Volume": c.volume}[n]),
            mock.patch.object(spotify, "_prop_set",
                              side_effect=lambda c, n, s, v: c.calls.append(("set", n, v))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _methods(self):
        return [c[0] for c in self.conn.calls]

    def test_pause(self):
        self.assertEqual(spotify.run_intent(_cfg(), Intent("pause")), "Paused.")
        self.assertIn("Pause", self._methods())

    def test_resume(self):
        self.assertEqual(spotify.run_intent(_cfg(), Intent("resume")), "Playing.")
        self.assertIn("Play", self._methods())

    def test_next_reports_the_new_track(self):
        self.assertEqual(spotify.run_intent(_cfg(), Intent("next")),
                         "Skipped to Numb by Linkin Park.")
        self.assertIn("Next", self._methods())

    def test_now_playing(self):
        self.assertEqual(spotify.run_intent(_cfg(), Intent("now_playing")),
                         "Numb by Linkin Park.")

    def test_now_playing_when_paused(self):
        self.conn.status = "Paused"
        self.assertEqual(spotify.run_intent(_cfg(), Intent("now_playing")),
                         "Paused on Numb by Linkin Park.")

    def test_now_playing_empty(self):
        self.conn.metadata = {}
        self.assertEqual(spotify.run_intent(_cfg(), Intent("now_playing")),
                         "Nothing is playing.")

    def test_volume_absolute(self):
        self.assertEqual(spotify.run_intent(_cfg(), Intent("volume", arg=0.4)),
                         "Volume 40 percent.")
        self.assertIn(("set", "Volume", 0.4), self.conn.calls)

    def test_volume_relative_clamps(self):
        self.conn.volume = 0.95
        self.assertEqual(
            spotify.run_intent(_cfg(), Intent("volume", delta=0.15)),
            "Volume 100 percent.")

    def test_play_opens_the_uri(self):
        with mock.patch.object(spotify, "search", return_value={
                "uri": "spotify:track:abc", "name": "Numb",
                "artist": "Linkin Park", "type": "track"}):
            speech = spotify.run_intent(_cfg(), Intent("play", query="numb"))
        self.assertEqual(speech, "Playing Numb by Linkin Park.")
        # _player_call(conn, "OpenUri", "s", (uri,)) — signature then body
        self.assertEqual(self.conn.calls[0],
                         ("OpenUri", ("s", ("spotify:track:abc",))))

    def test_play_no_match(self):
        with mock.patch.object(spotify, "search", return_value=None):
            self.assertEqual(
                spotify.run_intent(_cfg(), Intent("play", query="asdkjh")),
                "I couldn't find asdkjh on Spotify.")

    def test_missing_credentials_is_actionable(self):
        with mock.patch.object(spotify, "search",
                               side_effect=spotify.MediaError("no-credentials")):
            speech = spotify.run_intent(_cfg(), Intent("play", query="numb"))
        self.assertIn("setup_spotify.sh", speech)

    def test_transport_without_a_running_player(self):
        with mock.patch.object(spotify, "is_running", return_value=False):
            self.assertEqual(spotify.run_intent(_cfg(), Intent("pause")),
                             "Spotify isn't running.")

    def test_unexpected_crash_still_speaks(self):
        with mock.patch.object(spotify, "_player_call",
                               side_effect=RuntimeError("boom")):
            speech = spotify.run_intent(_cfg(), Intent("pause"))
        self.assertIn("failed", speech)


if __name__ == "__main__":
    unittest.main()
