# Ask-about-my-screen: shortcut → area screenshot → popup query → streamed answer

## Context

The user wants: press a shortcut → take a screenshot → a popup opens where they can
**type or voice-record** a question about it → answer comes back. This becomes a new
front-end to the existing dispatcher (locked decision: single dispatch path — the
popup is just another client).

**User-approved UX decisions:**
- Trigger: **GNOME custom shortcut `<Super><Alt>a`** (no evdev, no new systemd service — a tap doesn't need keyup tracking).
- Capture: **area-select** via xdg-desktop-portal `Screenshot(interactive=true)` (GNOME 50 Wayland; grim doesn't work here).
- Popup: **chromium `--app` window** served by the dispatcher — screenshot preview, textarea, mic button, streamed answer.
- Voice: **both** — mic button (MediaRecorder → new `/stt` endpoint) AND existing hold-F9 dictation (types into the focused textarea for free).
- **New dep approved by user: `jeepney`** (pure-Python D-Bus) for the portal call.

**Verified seams that make this cheap (no image-content-block plumbing needed):**
- `dispatcher/service.py` `stream_quick` (line ~145) already passes `tools=`/`context=` into `quick.stream`; `quick.py` forces the CLI backend when tools are set, and `--allowedTools Read` lets the model read the PNG. Prompt-wrap: "Use the Read tool to view the screenshot at `<path>`, then answer: …".
- Quick-session continuity is keyed by `source` → use `source: "screen:<shot_id>"` so each popup gets isolated context and follow-ups resume the CLI session (image stays in context, ~6× cheaper).
- `TaskIn.metadata` (free-form JSON, already flows to the `tasks` table) carries `{"screenshot": "<shot_id>.png"}` — no schema change.
- `av` 18 is already in the venv → faster-whisper decodes browser webm/opus blobs directly (`WhisperModel.transcribe(io.BytesIO(data))`).
- `python-multipart` NOT in venv → `/stt` takes a raw body (`await request.body()`), which is simpler anyway.
- `quick_max_tokens` is messages_api-only; screenshot tasks always ride the CLI backend (governed by `quick_max_cost_usd` + timeout) — no bump needed.
- UI: clone the streaming pattern from `ui/src/widgets/CommandBox.jsx`; `postTask` in `ui/src/api.js` currently hardcodes `{text, source:"ui"}`.

## Implementation steps (each independently testable)

### 1. Config keys + loaders
`config.yaml`:
```yaml
dispatcher:
  screenshots_dir: data/screenshots
  stt: { model: small.en, compute: int8, language: en }
jarvis:
  ask_screen:
    shortcut: "<Super><Alt>a"
    window_size: [520, 720]
    keep_last: 20
    chromium_bin: chromium
```
Add matching fields + `d.get(...)` defaults in `dispatcher/config.py` and `jarvis/config.py`
(both loaders only pass declared keys; follow the existing `stt:` merge pattern in jarvis/config.py).

### 2. `dispatcher/stt.py` + `POST /stt`
- New ~30-line module: `SpeechToText(model, compute, language)` — lazy `WhisperModel` load
  behind a `threading.Lock` (serializes concurrent transcribes; CPU-bound anyway);
  `transcribe_bytes(data) -> str` via `io.BytesIO` (av decodes webm/wav). Module-level
  `get_stt(cfg)` singleton (patchable in tests). Do NOT reuse `FasterWhisperSTT` — it takes
  int16 numpy; file-like decode is cleaner for uploads.
- `dispatcher/main.py`: `POST /stt` — raw body, `<100` bytes → 400; decode failure → 422;
  transcribe via `asyncio.to_thread`; returns `{"text": ...}`.

### 3. `GET /screenshots/{name}` + `GET /ask`
- `resolve_screenshot(cfg, name) -> Path|None` in `dispatcher/service.py` (shared with step 4):
  require `.png` suffix, resolved parent == `screenshots_dir.resolve()` (traversal guard), `is_file()`.
- `/screenshots/{name}` → `FileResponse` or 404; `/ask` → `FileResponse(ui/dist/index.html)`
  (registered before the `/` StaticFiles mount, so routes win).

### 4. Screenshot-aware quick path (`dispatcher/service.py`, `stream_quick`)
Client sends `{text, source: "screen:<shot_id>", mode: "quick", metadata: {"screenshot": "<shot_id>.png"}}` every turn.
After the area lookup (~line 130):
- Parse `task["metadata"]`; if `screenshot` resolves: add `"Read"` to `q_tools`, append a short
  `SCREEN_CONTEXT` blurb to `q_context` ("user is asking about a screenshot; concise on-screen prose").
- Inside the attempt loop: wrap the prompt with the Read-the-screenshot instruction **only when
  `resume_id is None`** (first turn, or vanished-session fresh retry which must re-read the image);
  resumed follow-ups send the raw question. Pass the wrapped text to `quick.stream`, keep the raw
  question in the DB `text` column.
- Missing/pruned file → log warning, proceed unwrapped (keep_last=20 makes this unrealistic in-window).

### 5. UI (`ui/src`)
- `api.js`: `postTask(text, { onDelta, source = "ui", mode, metadata } = {})` — spread optional
  `mode`/`metadata` into the body. `CommandBox.jsx` untouched.
- New `AskScreen.jsx` page (not a widget): `shot` from `URLSearchParams` (validate `/^[\w-]+$/`);
  `<img src="/screenshots/<shot>.png">` preview (max-height ~40vh); autofocused textarea
  (Enter submits, Shift+Enter newline — focused field also makes F9 dictation work);
  mic toggle via `getUserMedia` + `MediaRecorder` → on stop POST blob to `/stt` → append transcript
  to textarea; submit → `postTask(q, {source: "screen:"+shot, mode: "quick", metadata, onDelta})`;
  exchanges stack as `[{q, a, err}]` so follow-ups stay in one window; refocus input after done.
- `main.jsx`: `location.pathname === "/ask" ? <AskScreen/> : <App/>`.
- `vite.config.js`: add `"/stt", "/screenshots"` to the proxy list (NOT `/ask` — Vite's SPA
  fallback must serve the dev index.html there).
- `styles.css`: `.ask-page` column layout + recording state on the mic button.

### 6. Helper `jarvis/ask_screen.py` (`python -m jarvis.ask_screen`)
Flow: `JarvisConfig.load()` → `GET /health` (3s timeout; failure → `zenity --error`, stderr fallback, exit 1) → portal screenshot → store + prune → launch chromium → exit.

Jeepney portal flow (no SetAlias — it's handle_token + AddMatch + Response signal):
subscribe a `MatchRule` on `org.freedesktop.portal.Request` / member `Response` at the predicted
handle path `/org/freedesktop/portal/desktop/request/<sender-with-underscores>/<token>` **before**
calling `Screenshot("", {handle_token, interactive: true, modal: true})` (no race); if the reply's
request path differs (pre-0.9 portal), re-subscribe on the returned path; `recv_until_filtered`
with a 300s timeout. Response code 1 = user cancelled → silent exit 0; code ≠ 0 → error; else
`results["uri"]`.
- `store(uri)`: assert `file://` scheme, `shutil.move` to `data/screenshots/<YYYYmmdd-HHMMSS>.png`
  (mkdir parents; `-2` suffix on collision; move keeps ~/Pictures clean).
- `prune()`: keep newest `keep_last` PNGs by name.
- Launch: `Popen([chromium_bin, "--app=<dispatcher_url>/ask?shot=<id>", "--window-size=W,H"], start_new_session=True, stdout/stderr DEVNULL)`.

### 7. `scripts/setup_ask_screen.sh` (idempotent)
1. `.venv/bin/pip install --quiet jeepney`; `mkdir -p data/screenshots`.
2. Binding from `$1` or `JarvisConfig.load().ask_screen["shortcut"]`.
3. gsettings custom keybinding at keypath `.../custom-keybindings/mission-ask-screen/`:
   append keypath to the `custom-keybindings` list (special-case the `@as []` empty literal),
   then set `name` / `binding` / `command`. Command must be self-contained (GNOME spawns with no
   repo cwd): `sh -c 'cd <abs repo> && exec .venv/bin/python -m jarvis.ask_screen'` built from `$PWD`.

No systemd changes. Deploy = `systemctl --user restart mission-dispatcher` + `cd ui && npm run build`.

### 8. Tests + verification
- `tests/test_ask_screen.py` (unittest style; `TestClient(app)` without `with` skips lifespan/queue-watcher):
  `/screenshots` ok + 404 + traversal (`%2e%2e%2f`); `/stt` with patched `get_stt` + 400 empty body;
  `resolve_screenshot` guard; step-4 service tests with a recording fake `quick.stream`
  (turn 1 wrapped + Read tool; resumed turn 2 unwrapped; missing file unwrapped; DB text stays raw).
- `scripts/smoke_ask_screen.sh` (dispatcher running): `/ask` is HTML; PNG round-trip;
  `curl --path-as-is /screenshots/../config.yaml` → 404; **real-audio STT** via the smoke_phase_b
  trick (Piper synthesizes a phrase → WAV bytes → `curl --data-binary` → grep transcript);
  optionally one real screenshot quick task asserting `event: done`. Leave smoke_phase_a.sh frozen.
- Manual E2E: run setup script → `<Super><Alt>a` → select region → popup preview → typed query
  streams an answer → follow-up resumes (check `runs.session_id` / cheaper cost) → mic button
  round-trip → F9 dictation into the textarea → portal-cancel exits silently.

### 9. Bookkeeping (session protocol)
`.gitignore` += `data/screenshots/`; CLAUDE.md operational note (endpoints, helper, setup script,
jeepney, shortcut); update `context/STATE.md`; append `context/sessions/2026-07-18-<n>.md`.

## Risks (accepted / mitigations)
1. Portal may show an extra permission confirm for a bare python process (no app-id) — user is in a selection UI anyway; verify on first manual run.
2. Chromium `--app` window-size is ignored when an instance is already running; mutter may override. Follow-up if annoying: dedicated `--user-data-dir` profile (config already has `chromium_bin`).
3. Mic permission: `http://127.0.0.1` is a secure context — chromium prompts once per profile.
4. Second Whisper instance in the dispatcher (~200-300MB, lazy-loaded, first `/stt` ~3s) — accepted for v1.
5. Vanished-session retry re-reads the image (fresh session must) — correct, rare, slightly costlier.
