// Docs content as data — the page renders it, nothing here is markdown (no
// renderer dependency). Block shapes: {p}, {note}, {code}, {list}, {table}.
// Keep this in sync with README.md and the CLAUDE.md operational notes.

export const guide = [
  {
    id: "quickstart",
    title: "Quick start",
    blocks: [
      {
        table: {
          head: ["Where", "URL"],
          rows: [
            ["Dashboard", "http://127.0.0.1:8765/"],
            ["These docs", "http://127.0.0.1:8765/system-docs"],
            ["Swagger / OpenAPI explorer", "http://127.0.0.1:8765/docs"],
            ["Live event feed (SSE)", "http://127.0.0.1:8765/events"],
            ["Ask-screen popup", "http://127.0.0.1:8765/ask?shot=<id>"],
          ],
        },
      },
      {
        table: {
          head: ["Shortcut", "Does"],
          rows: [
            ["“hey jarvis”", "wake the assistant — pause, then speak; it answers aloud"],
            ["hold Right Ctrl", "push-to-talk: same assistant, but the key release ends your turn"],
            ["hold F9", "dictation: speech is typed at your cursor (mission-dictate)"],
            ["<Super><Alt>a", "ask about my screen: drag a region → popup → streamed answer"],
            ["talk over Jarvis", "barge-in — TTS stops and the call aborts within ~200ms"],
            ["“play …” / “pause” / “skip”", "drives the local Spotify client directly — no model, under a second"],
            ["“lock the screen” / “open firefox”", "desktop control; sensitive verbs ask before acting"],
          ],
        },
      },
      { p: "Commands worth memorising:" },
      {
        code: "systemctl --user restart mission-dispatcher     # after any config.yaml edit\njournalctl --user -u mission-dispatcher -f      # live logs\n.venv/bin/python -m unittest discover tests     # tests\ncd ui && npm run build                          # rebuild dashboard + docs",
      },
      { p: "Talk to it without voice — same phrases work in the dashboard command box or over HTTP:" },
      {
        code: "curl -sN -X POST localhost:8765/task -H 'content-type: application/json' \\\n  -d '{\"text\": \"add a task: renew the domain by friday\"}'",
      },
      { note: "New here? Read Overview next. Something broken? Jump to Troubleshooting." },
    ],
  },
  {
    id: "overview",
    title: "Overview",
    blocks: [
      { p: "Mission Control is a local-first personal life OS. A React dashboard and Jarvis (a fully local voice assistant) are two front-ends to one brain: the dispatcher, a FastAPI service that owns every Claude invocation. Models run on CPU, scheduling is systemd user timers, nothing leaves this machine except the Claude calls themselves." },
      {
        list: [
          "Single dispatch path — voice, dashboard, timers and the queue directory are all clients of the dispatcher. Nothing else calls Claude.",
          "Quick vs agentic — a short question streams back token by token; anything that needs tools runs headless (claude -p) in the background with a tool allowlist, a spend cap and a wall-clock timeout.",
          "Refusal fallback — a headless run that stops with a refusal is retried automatically on the fallback model, and both attempts are logged.",
          "Memory is a markdown vault plus SQLite: files stay readable and editable by hand.",
          "Life areas are plugins — one directory under areas/ with a SKILL.md; adding one needs zero core changes.",
          "Deterministic where it can be — music, desktop control and schedule phrasing are parsed by plain code, so they cost nothing, answer in well under a second, and never hand the model a privileged action. A model is only consulted when the wording is genuinely ambiguous, and even then it just picks words: the action itself stays on the deterministic path.",
          "Degrades instead of dying — no embeddings means keyword-only search, a missing optional dependency means that format is counted rather than indexed, and hitting the rate limit means an extractive answer quoted from local memory rather than silence.",
        ],
      },
      { p: "Reachable from your phone: Tailscale Serve fronts the dispatcher as tailnet HTTPS, so the service itself stays bound to 127.0.0.1, no port is opened and no password exists — only your own devices can reach it. The dashboard installs to the home screen as a PWA." },
      {
        table: {
          head: ["Directory", "What lives there"],
          rows: [
            ["dispatcher/", "FastAPI service, task router, claude -p runner, memory, graph, automations"],
            ["jarvis/", "voice pipeline — plugins/ (ABCs), engines/, main loop, dictation, ask-screen"],
            ["areas/", "life-area skills (SKILL.md, agents/) — tasks, memory, learn"],
            ["vault/", "markdown memory: notes/, tasks/, briefs/, memory/, inbox/"],
            ["data/", "mission.db (SQLite), backups/, voice + embedding models, screenshots"],
            ["queue/", "drop a markdown file here and it becomes a task"],
            ["ui/", "this React SPA (dashboard, ask-screen popup, these docs)"],
            ["context/", "STATE.md + session logs — project continuity, read first"],
            ["config.yaml", "the single config file for everything"],
          ],
        },
      },
    ],
  },

  {
    id: "running",
    title: "Setup & running",
    blocks: [
      { p: "Everything runs as systemd user services — no terminal needs to stay open, and each unit restarts itself on crash. One-time install:" },
      { code: "./systemd/install.sh              # dispatcher + all timers\nloginctl enable-linger $USER      # optional: start at boot without logging in" },
      { p: "Two setup scripts cover the optional front-ends; both are idempotent:" },
      { code: "./scripts/setup_voice.sh          # ~90MB of wake/STT/TTS models\n./scripts/setup_ask_screen.sh     # jeepney + the <Super><Alt>a binding" },
      { p: "Voice also needs raw input access for the F9 hotkey — sudo usermod -aG input $USER, then log out and back in." },
      { p: "Day-to-day service control (same verbs work for every unit):" },
      {
        code: "systemctl --user start   mission-dispatcher\nsystemctl --user stop    mission-dispatcher   # also frees port 8765\nsystemctl --user restart mission-dispatcher   # apply a config.yaml change\nsystemctl --user status  mission-dispatcher\njournalctl --user -u mission-dispatcher -f    # live logs",
      },
      { note: "Restart the dispatcher after any config.yaml edit — the file is read once at startup." },
      { p: "Running by hand during development (stop the service first to free the port):" },
      {
        code: ".venv/bin/python -m dispatcher.main           # serves 127.0.0.1:8765\n.venv/bin/python -m jarvis.main --mode wake   # voice assistant\n.venv/bin/python -m jarvis.dictate            # hold-F9 dictation\ncd ui && npm run dev                          # UI with hot reload, API proxied",
      },
      { p: "No API key is involved: the dispatcher drives the claude CLI with your subscription login (CLAUDE_CONFIG_DIR=~/.claude-per), so costs shown in logs and on the dashboard are notional rather than billed. Do not export ANTHROPIC_API_KEY — the CLI prefers a key over the claude.ai login, so a stray one replaces your subscription instead of adding to it. The dispatcher strips it from every claude subprocess for that reason." },
      { p: "Tests and acceptance smokes:" },
      {
        code: ".venv/bin/python -m unittest discover tests   # unit tests, no network\n./scripts/smoke_phase_a.sh                    # dispatcher (server must be up)\n.venv/bin/python scripts/smoke_phase_b.py     # voice, no mic needed\n./scripts/smoke_ask_screen.sh                 # ask-about-my-screen",
      },
    ],
  },

  {
    id: "frontends",
    title: "Front-ends",
    blocks: [
      { p: "The dashboard at http://127.0.0.1:8765/ is one page of widgets, all fed by a single SSE subscription to /events:" },
      {
        table: {
          head: ["Widget", "What it does"],
          rows: [
            ["Command", "type anything you would say to Jarvis — quick answers stream in place"],
            ["Today's brief", "renders the current vault/briefs/ entry"],
            ["Tasks", "vault/tasks/ files; the checkboxes write back to the markdown"],
            ["Agent activity", "live run list — current step under running rows, full timeline on expand"],
            ["Automations", "standing schedules: pause, resume, delete"],
            ["Observability", "tasks by status, cost per source, success rate, quick latency, what Jarvis learned"],
          ],
        },
      },
      { p: "A confirmation banner appears above the widgets when something asks permission — currently the desktop verbs whose policy is “confirm” (reading the clipboard, opening a URL). Answer with the Yes/No buttons, or just say “yes”/“no” to Jarvis. Requests expire after two minutes, and each can only be answered once." },
      { p: "Desktop verbs work from either front-end and reach no model at all: lock the screen, set the system volume, mute, read or set the clipboard, open a URL, and launch any installed app by name — “open firefox”, “launch text editor”, “fire up obsidian”. Names are matched against your .desktop entries, exact match first. A launched app is checked for afterwards rather than assumed, so you are told “<app> didn't start” instead of a cheerful lie, and it runs in its own systemd scope, so restarting the dispatcher never closes it." },
      { p: "The Command box also has a mic button — the browser-side twin of “hey jarvis”: click to record, click to stop, and the reply is spoken back with the browser's own speech synthesis (“mute reply” silences it). It works from the phone too, since the Tailscale URL is HTTPS and the mic needs a secure context." },
      { p: "Jarvis (voice) runs in both modes as installed. Wake word: say “hey jarvis”, pause, then speak. Push-to-talk: hold Right Ctrl, speak, release — a beep marks each edge and the key release ends the turn, so there is no waiting for silence. Quick questions are spoken sentence by sentence as they stream; a long agentic ask answers “On it — …” immediately and speaks “Done: …” when it finishes." },
      {
        list: [
          "Modes: .venv/bin/python -m jarvis.main --mode wake | ptt | both (the installed unit uses both).",
          "F9 is dictation, NOT the assistant: mission-dictate is a separate unit that types transcribed speech at the cursor via ydotool. The two keys must differ — both services read the raw evdev stream, so sharing one would trigger both at once (config: jarvis.trigger_key vs jarvis.ptt_key).",
          "Barge-in: talk over Jarvis, or press Right Ctrl, and it stops within ~200ms and aborts the in-flight call.",
          "Echo caveat: without echo cancellation the mic hears Jarvis through the speakers. Use headphones, or pactl load-module module-echo-cancel.",
          "Not every finished job is announced: routine timer and automation successes are filtered by a notify-or-not check, so only results worth interrupting you for are spoken.",
          "If Claude's plan-wide cap is hit, Jarvis says so and then answers from its own indexed memory where it can — quoting the vault verbatim with the source named, rather than going quiet until the reset.",
        ],
      },
      { p: "Ask about my screen: press <Super><Alt>a, drag a region, and a small popup opens with the shot. Type or dictate a question and the answer streams back; follow-ups resume the same session, so the image stays in context and later turns are much cheaper." },
    ],
  },

  {
    id: "areas",
    title: "Areas, vault & queue",
    blocks: [
      { p: "A life area is a directory under areas/ containing a SKILL.md (with trigger phrases in its frontmatter), optionally a CLAUDE.md and agent prompts under agents/. Incoming text is matched against those triggers to pick an area; the area's skill then shapes the prompt and the tool grants. Adding an area requires no core change." },
      {
        table: {
          head: ["Path", "Contents"],
          rows: [
            ["vault/notes/", "free-form notes — indexed and searchable"],
            ["vault/tasks/", "one markdown file per task; the dashboard checkboxes edit these"],
            ["vault/briefs/", "daily briefs and weekly reviews"],
            ["vault/memory/", "USER.md and MEMORY.md core blocks, plus projects/"],
            ["vault/inbox/", "drop exports here (txt, html, pdf, images) to get them indexed"],
            ["queue/", "drop a .md file → it becomes a task within ~5s, then moves to .processed/"],
          ],
        },
      },
      { p: "A queue file is plain markdown with optional frontmatter:" },
      { code: "---\nmode: agentic\n---\nSummarize this week's notes." },
      { p: "The tasks area responds to the same phrases by voice or in the command box: “add a task: renew the domain by friday”, “list my open tasks”, “mark the domain task as done”, “morning brief”. Trigger matching is loose, so “can you add a new task for me” works too." },
    ],
  },

  {
    id: "memory",
    title: "Memory & learning",
    blocks: [
      { p: "Every settled task appends a raw episode row to SQLite — no LLM involved in capture. Nightly consolidation turns those episodes into durable memory, and the core blocks vault/memory/USER.md and MEMORY.md are injected into every prompt, quick or agentic. Edit them by hand whenever you like; character budgets live in config.yaml." },
      { p: "Search is hybrid: FTS5 keyword ranking fused with vector nearest-neighbours (local bge-small embeddings, int8, CPU) using reciprocal rank fusion. Deterministic filters (file, after, before) force the keyword-only path. If sqlite-vec is missing or embeddings are disabled, everything degrades silently to keyword search." },
      { p: "Facts extracted from episodes form a bi-temporal knowledge graph: facts are sentences linked to entities, and contradictions are invalidated with a timestamp rather than deleted, so history stays inspectable. Query it with scope=graph on /memory/search to get matching facts plus their one-hop neighbours." },
      { p: "The learning loop closes the circle: a complex agentic run (12+ turns) forks a reflection that resumes its own session and patches the relevant skill under areas/. “Nothing to save.” is a perfectly normal outcome. Skill usage counters drive a deterministic curator that ages unused areas active → stale → archived (moved to areas/.archive/, never deleted); pinned, protected and timer-backed areas are exempt." },
      {
        table: {
          head: ["Timer", "When", "What"],
          rows: [
            ["mission-daily-brief", "07:30 daily", "writes today's brief into vault/briefs/"],
            ["mission-weekly-review", "Sun 18:00", "weekly review note"],
            ["mission-memory-consolidate", "02:30 daily", "episodes → memory blocks + graph extraction"],
            ["mission-memory-reconcile", "Sun 04:30", "invalidate duplicate/contradictory facts"],
            ["mission-skill-curator", "1st of month, 04:00", "skill lifecycle (stale → archived)"],
            ["mission-backup", "03:30 daily", "SQLite backup, kept 14 days in data/backups/"],
          ],
        },
      },
    ],
  },

  {
    id: "automations",
    title: "Automations & notifications",
    blocks: [
      { p: "Anything phrased as a schedule becomes a standing automation instead of a one-shot task: say or type “every morning at 8, summarize my open tasks”. One LLM parse turns it into a schedule spec, which is then validated mechanically and stored. A scheduler inside the dispatcher checks every 30 seconds and submits due rows as normal tasks, so a machine that was asleep catches up on wake." },
      {
        list: [
          "Schedule kinds: daily, weekly, interval and once (a once row spends itself after firing).",
          "Re-enabling a paused automation recomputes its next run, so a stale past-due row can't instantly fire.",
          "Questions are never diverted into automations, and an explicit quick/agentic mode bypasses the divert entirely.",
          "Manage them from the Automations widget or the /automations endpoints.",
        ],
      },
      { p: "Finished automation and timer runs pass a notify-or-not gate: a small, cheap model is shown the request and the result and decides whether it is worth surfacing. A NOTIFY verdict fires a desktop notification and an SSE event (spoken aloud if Jarvis is running); SKIP is logged only. The gate fails open — if it breaks, you still get a generic notification. Internal machinery (reflection, consolidation, graph work) is never announced." },
      { p: "Not everything proactive is on a clock. Drop a file into vault/inbox/ and it is indexed automatically within about a minute, with a notification when it lands — the watcher waits for the file to stop changing first, so a large download isn't indexed half-written. Deleting a file removes its entries just as quietly." },
    ],
  },

  {
    id: "config",
    title: "Configuration",
    blocks: [
      { p: "config.yaml is the single source of truth. Restart the dispatcher after editing it." },
      {
        table: {
          head: ["Block", "Controls"],
          rows: [
            ["dispatcher.models", "quick / agentic / fallback / classifier model ids and effort, plus meta — the cheap model used for internal classification work (notify gate, schedule and music parsing)"],
            ["dispatcher.budgets", "per-task spend caps, wall-clock timeout, quick token cap"],
            ["dispatcher.task_types", "per-task-type tool allowlists (read-only baseline in task_defaults)"],
            ["dispatcher.security", "privileged_areas — the only areas allowed to declare Bash or unscoped writes"],
            ["dispatcher.quick_session_idle_minutes", "how long a follow-up still resumes the previous conversation"],
            ["dispatcher.memory", "capture on/off, core-block budgets, indexed directories, graph on/off"],
            ["dispatcher.embeddings", "local embedding model, enable flag, refresh interval"],
            ["dispatcher.learning", "reflection threshold and budget, curator ageing windows, protected areas"],
            ["dispatcher.automations", "scheduler interval, NL detection, notify gate and destinations"],
            ["dispatcher.inbox", "vault/inbox watcher: poll interval, notify on arrival, optional per-file summary (off by default — it costs a model call)"],
            ["dispatcher.media", "Spotify control: enable, launch command, credentials path, search market"],
            ["dispatcher.computer", "desktop verbs: enable, and the per-verb allow / confirm / deny policy"],
            ["dispatcher.security.public_hosts", "hostnames the CSRF origin guard trusts — your Tailscale name goes here for phone access"],
            ["jarvis", "trigger keys (ptt_key for the assistant, trigger_key for dictation — they must differ), wake word and threshold, STT/TTS engines, VAD and barge-in tuning"],
            ["jarvis.ask_screen", "shortcut, popup size, screenshots kept, browser binary"],
          ],
        },
      },
      { note: "Engines are swappable in one line — tts: { engine: kokoro } picks a different voice backend behind the same ABC." },
    ],
  },

  {
    id: "troubleshooting",
    title: "Troubleshooting",
    blocks: [
      {
        table: {
          head: ["Symptom", "Fix"],
          rows: [
            ["no readable keyboard devices", "not in the input group, or you didn't log out and back in"],
            ["ydotoold dead / dictation types nothing", "the /dev/uinput quirk: sudo chmod g+rw /dev/uinput, then modprobe -r uinput && modprobe uinput"],
            ["wake word never fires", "check the default mic (wpctl status) and lower wake_threshold; the journal logs near-miss scores"],
            ["Jarvis interrupts itself while speaking", "echo — use headphones or load module-echo-cancel"],
            ["task failed with error_max_budget_usd", "raise budgets.max_cost_per_task_usd; a bare claude -p already costs ~$0.15–0.25 notional"],
            ["quick answers feel slow", "~3s to the first word is the floor here: every cold claude -p run re-sends the Claude Code system prompt. Jarvis speaks sentence by sentence as the answer streams, so it feels quicker than it measures"],
            ["every quick answer suddenly fails", "check for a stray ANTHROPIC_API_KEY in the environment: the claude CLI prefers a key over your claude.ai login, so one replaces your subscription. The dispatcher strips it from its own subprocesses, but a hand-run dispatcher inherits your shell"],
            ["ui not built (404 on /ask or /system-docs)", "cd ui && npm run build"],
            ["running tasks stuck after a restart", "they're marked failed as orphans on the next startup; resubmit"],
            ["a desktop command refuses", "its policy is deny in the computer.policy block; GET /desktop/verbs lists every verb and its current setting"],
            ["“<app> didn't start” or “I can't reach your desktop session”", "the dispatcher was started before your graphical session and has no display to launch into. It borrows one from the systemd user manager; scripts/doctor.sh reports whether the manager has one, and a restart from inside the desktop session fixes it"],
            ["a file in vault/inbox/ isn't searchable", "give it a minute (the watcher waits for it to stop changing), then check the journal for “inbox”"],
          ],
        },
      },
    ],
  },
];

export const endpoints = [
  {
    group: "Tasks & events",
    id: "api-tasks",
    items: [
      {
        method: "POST", path: "/task",
        summary: "Submit work. Quick tasks stream back as SSE; agentic tasks ack with 202 and run in the background. Schedule-phrased text may divert into an automation.",
        params: "text, source, mode (auto|quick|agentic), area, metadata",
        returns: "SSE stream (task/delta/done) or 202 {task_id, status}",
      },
      { method: "GET", path: "/task/{id}", summary: "One task with its runs.", params: "—", returns: "task + runs" },
      { method: "POST", path: "/task/{id}/cancel", summary: "Cancel a queued or running task, killing the subprocess.", params: "—", returns: "{task_id, status}" },
      { method: "GET", path: "/task/{id}/steps", summary: "Full step timeline of an agentic run (init, text, tool_use, tool_result, result).", params: "—", returns: "step rows" },
      { method: "GET", path: "/tasks", summary: "Recent task history with costs.", params: "status, kind, limit (default 50)", returns: "task rows" },
      { method: "GET", path: "/events", summary: "Live server-sent event feed powering the dashboard.", params: "—", returns: "SSE: queued, started, done, failed, refused, cancelled, requeued, step, notify, automation, confirm" },
      { method: "GET", path: "/stats", summary: "Tasks by status, cost per source, success rate, quick-path latency, recent reflections.", params: "days (default 7)", returns: "stats object" },
      { method: "GET", path: "/health", summary: "Liveness plus a snapshot of the running configuration.", params: "—", returns: "{status, …}" },
    ],
  },
  {
    group: "Vault",
    id: "api-vault",
    items: [
      { method: "GET", path: "/vault/tasks", summary: "Parsed task files from vault/tasks/.", params: "—", returns: "task list" },
      { method: "POST", path: "/vault/tasks/{filename}/toggle", summary: "Flip a task between open and done, rewriting the markdown file.", params: "—", returns: "updated task" },
      { method: "GET", path: "/vault/brief", summary: "Today's brief from vault/briefs/.", params: "—", returns: "{date, text}" },
    ],
  },
  {
    group: "Memory & graph",
    id: "api-memory",
    items: [
      {
        method: "GET", path: "/memory/search",
        summary: "Hybrid keyword + vector search over the vault index, episodes and the fact graph.",
        params: "q, limit (10), scope (all|entries|episodes|graph), file, after, before",
        returns: "ranked hits",
      },
      { method: "POST", path: "/memory/reindex", summary: "Re-walk the indexed directories, then backfill embeddings.", params: "—", returns: "ingest stats" },
      { method: "GET", path: "/memory/blocks", summary: "The core blocks injected into every prompt.", params: "—", returns: "{USER.md, MEMORY.md}" },
      { method: "POST", path: "/memory/consolidate", summary: "Export unconsolidated episodes and queue the consolidation agent plus graph extraction.", params: "—", returns: "{status, …}" },
      { method: "POST", path: "/memory/reconcile", summary: "Invalidate duplicate or contradictory facts in the graph.", params: "—", returns: "{status, …}" },
    ],
  },
  {
    group: "Automations",
    id: "api-automations",
    items: [
      { method: "POST", path: "/automations", summary: "Create a standing automation from natural language.", params: "request, source", returns: "201 automation + speech" },
      { method: "GET", path: "/automations", summary: "All automations with human-readable schedules.", params: "—", returns: "automation rows" },
      { method: "POST", path: "/automations/{id}/toggle", summary: "Pause or resume; resuming recomputes the next run time.", params: "—", returns: "updated automation" },
      { method: "DELETE", path: "/automations/{id}", summary: "Delete an automation.", params: "—", returns: "{automation_id, status}" },
    ],
  },
  {
    group: "Media & desktop",
    id: "api-desktop",
    items: [
      { method: "POST", path: "/media", summary: "Deterministic music command — play, pause, skip, what's playing, volume. Drives the local Spotify client over MPRIS; no model involved.", params: "command, source", returns: "{speech}" },
      { method: "GET", path: "/media/state", summary: "What the local player is doing right now.", params: "—", returns: "player state" },
      { method: "POST", path: "/desktop", summary: "Deterministic desktop verb — lock, launch an app, open a URL, system volume/mute, read or set the clipboard. A verb whose policy is 'confirm' parks instead of running and returns a confirm_id.", params: "command, source", returns: "{status, speech, confirm_id?}" },
      { method: "POST", path: "/desktop/confirm", summary: "Answer a parked confirmation. Ids are single-use and expire after confirm_timeout_s.", params: "confirm_id, approve", returns: "{status, speech}" },
      { method: "GET", path: "/desktop/verbs", summary: "Every desktop verb and its current allow / confirm / deny policy.", params: "—", returns: "{verbs, pending}" },
    ],
  },
  {
    group: "Learning & skills",
    id: "api-learn",
    items: [
      { method: "POST", path: "/learn", summary: "Author an area skill from a described workflow; an empty request distils the source's recent conversation instead.", params: "request, source", returns: "202 {task_id, resumed}" },
      { method: "GET", path: "/skills", summary: "Areas with their triggers and usage counters.", params: "—", returns: "skill rows" },
      { method: "POST", path: "/skills/curate", summary: "Run the deterministic skill lifecycle (stale → archived).", params: "—", returns: "curation report" },
    ],
  },
  {
    group: "Ask about my screen",
    id: "api-ask",
    items: [
      { method: "POST", path: "/stt", summary: "Raw audio body (webm or wav) transcribed server-side by Whisper.", params: "raw body, ≤ a few MB", returns: "{text}" },
      { method: "GET", path: "/screenshots/{name}", summary: "A captured screenshot (path-traversal guarded).", params: "—", returns: "image/png" },
      { method: "GET", path: "/ask", summary: "The ask-about-my-screen popup page.", params: "?shot=<id>", returns: "text/html" },
      { method: "GET", path: "/system-docs", summary: "This page.", params: "—", returns: "text/html" },
    ],
  },
];
