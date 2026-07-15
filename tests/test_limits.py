"""Usage-limit classification tests. The two positive fixtures are verbatim
failures from data/mission.db (2026-07-09/10) — the real strings this
classifier exists for."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from dispatcher.limits import classify_limit, limit_speech

SESSION_MSG = "You've hit your session limit · resets 2:30pm (Asia/Kolkata)"
MODEL_MSG = ("You've reached your Fable 5 limit. Run /usage-credits to "
             "continue or switch models with /model.")


class TestClassifyLimit(unittest.TestCase):
    def test_session_limit_with_reset_time(self):
        now = datetime(2026, 7, 10, 10, 43, tzinfo=ZoneInfo("Asia/Kolkata"))
        info = classify_limit(SESSION_MSG, now=now)
        self.assertIsNotNone(info)
        self.assertEqual(info.scope, "session")
        self.assertEqual(info.reset_phrase, "2:30pm")
        local = info.resets_at.astimezone(ZoneInfo("Asia/Kolkata"))
        self.assertEqual((local.hour, local.minute), (14, 30))
        self.assertEqual(local.date(), now.date())  # 2:30pm still ahead

    def test_reset_already_past_rolls_to_tomorrow(self):
        now = datetime(2026, 7, 10, 18, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        info = classify_limit(SESSION_MSG, now=now)
        local = info.resets_at.astimezone(ZoneInfo("Asia/Kolkata"))
        self.assertEqual(local.day, 11)
        self.assertEqual((local.hour, local.minute), (14, 30))

    def test_model_scoped_limit(self):
        info = classify_limit(MODEL_MSG)
        self.assertEqual(info.scope, "model")
        self.assertIsNone(info.resets_at)

    def test_budget_errors_never_classify(self):
        # --max-budget-usd trips are deliberate; a fallback retry would burn money
        self.assertIsNone(classify_limit("Budget limit exceeded your max of $0.50"))
        self.assertIsNone(classify_limit("error_max_budget_usd"))

    def test_ordinary_failures_never_classify(self):
        self.assertIsNone(classify_limit(None))
        self.assertIsNone(classify_limit(""))
        self.assertIsNone(classify_limit("unparseable output (exit 1): boom"))
        self.assertIsNone(classify_limit("killed after 600s"))
        self.assertIsNone(classify_limit("spawn error: [Errno 2] No such file"))
        # mentions a limit but nothing was reached/hit
        self.assertIsNone(classify_limit("the rate limit applies to new accounts"))

    def test_unknown_timezone_still_gives_phrase(self):
        info = classify_limit("You've hit your session limit · resets 7pm (Middle/Earth)")
        self.assertEqual(info.scope, "session")
        self.assertEqual(info.reset_phrase, "7pm")
        self.assertIsNotNone(info.resets_at)  # falls back to local tz

    def test_speech_lines(self):
        now = datetime(2026, 7, 10, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        s = limit_speech(classify_limit(SESSION_MSG, now=now))
        self.assertEqual(s, "Claude's session limit is hit right now. It resets at 2:30pm.")
        m = limit_speech(classify_limit(MODEL_MSG))
        self.assertEqual(m, "Claude's usage limit is hit right now.")


if __name__ == "__main__":
    unittest.main()
