# Computer use — what else we can add (research, 2026-07-28)

_Research only. Nothing here is approved or built. Deps named below are NOT
installed except where the probe says otherwise._

Follows `future/computer-use.md` (session 16 design, session 20 spike, session
23 fix). That doc's reference-repo audit (§4, OpenClaw + Hermes) is still
current and is **not** repeated here — `references/` has no libei/AT-SPI/portal
material at all (checked), so everything new in this round came from probing
this machine and from the web.

Everything below marked **measured** was run on this box today.

---

## 1. The headline: the session-20 spike was too pessimistic

`future/computer-use.md` deferred window management as "not reachable zero-dep,
needs a GNOME shell extension". That conclusion came from testing exactly two
D-Bus surfaces — `Shell.Eval` (locked) and `Shell.Introspect` (AccessDenied) —
and stopping there.

**AT-SPI2 was never tried, and it works.** Measured:

```
$ python3 -c "…Atspi.get_desktop(0)…"
applications on the a11y bus: 9
  'org.gnome.Nautilus'  frame: 'Telegram Desktop'      ifaces=[Accessible, Action, Component]
  'vlc'                 frame: 'Search … - VLC media'  ifaces=[Accessible, Action, Component]
  'gnome-shell'         window: 'Main stage'           ifaces=[Accessible, Collection, Component]
```

That is the deferred capability, live: **window enumeration with titles**, plus
two interfaces the design doc never accounted for —

- **`Component`** → geometry and `grabFocus()` (focus a window by name).
- **`Action`** → `doAction()` on widgets: **click a button by its accessible
  name, with no coordinates and no pixels**. This is Hermes' `click(element=7)`
  model, available locally.

Caveats found in the same probe, both real:

- **`toolkit-accessibility` is `false`** here, and the tree is still readable
  for GTK/Qt. Electron/Chromium apps generally are *not* in the tree until the
  `org.a11y.Status` flags are set — which is exactly what cua's driver does on
  startup ([cua blog](https://cua.ai/blog/inside-linux-computer-use)). Coverage
  is per-toolkit, not universal; that part of the doc's T2 warning holds.
- The tree is **messy**: `org.gnome.Nautilus` reports a frame titled "Telegram
  Desktop", and `mutter-x11-frames` reports VLC's title. App identity and window
  identity do not line up reliably — match on the window, not the app name.

**How the dispatcher would reach it** (three options, in order of preference):

| Path | Dep cost | Notes |
|---|---|---|
| jeepney → `org.a11y.Bus.GetAddress` → `org.a11y.atspi.*` | **none** — jeepney already a dep | Measured: returns `unix:path=/run/user/1000/at-spi/bus`. Most code, but same pattern as `spotify.py`/`ask_screen.py` |
| `pygobject` in the venv | **one dep** (`python-gobject` 3.56.3 already on the system; pip build needs gobject-introspection + cairo) | Cleanest API by far — `Atspi.get_desktop(0)` |
| subprocess to `/usr/bin/python3` (has `gi`) | none | Ugly but consistent with how every other verb shells out |

---

## 2. What this box can actually do (measured today)

| Surface | State | Implication |
|---|---|---|
| GNOME Shell | **50.2**, Wayland | Newer than the doc assumes ("GNOME 4x") |
| `at-spi2-core` | **2.60.4 installed**, bus reachable | T2 is reachable — see above |
| `python-gobject` | **3.56.3 installed** (system python only) | Not in the venv |
| `libei` / `libeis` | **1.6.0 installed** | Sanctioned Wayland input exists on the box |
| portal `RemoteDesktop` | **version 2** | v2 is the one with persist/`restore_token` |
| portal `GlobalShortcuts` | version 1 | Could replace the ask-screen gsettings hack |
| portal `ScreenCast` / `Screenshot` | present | Screenshot already used by `ask_screen.py` |
| `ydotool` | installed, user-verified | Works regardless of Wayland/X11 |
| `node` | **22.22.2, built-in `WebSocket`** | CDP with zero deps — already proven twice in-repo |
| `chromium` | installed | ditto |
| `wtype`, `dotool`, `xdotool`, `playwright` | absent | — |

---

## 3. The seam question is now settled — and it forces the answer

`future/computer-use.md` §7 Q1 left open whether computer use should target
Anthropic's native computer tool (a **Messages API** tool — current version
`computer_20251124`, beta header `computer-use-2025-11-24`) or a local MCP
server driven by the CLI.

**Session 22 answered it by removing the API key path.** The native tool is a
Messages API feature; the subscription `claude -p` path cannot reach it at all.
So the only tool channel is a **local stdio MCP server**, granted per task via
`--allowedTools mcp__desktop__click …`. That is a good outcome, not a
consolation: it reuses the existing least-privilege grant machinery, the
`sanitize_untrusted_metadata` trust boundary, and the `run_steps` audit trail —
and it is testable without an LLM.

Mark Q1 answered in the doc.

---

## 4. Ranked options

### A. Browser control (T3) — biggest unlock, zero new deps

Most "computer use" tasks are web tasks, and this repo has already driven
chromium over CDP twice (sessions 6 and 20) using **Node's built-in WebSocket**
— no dependency, on a box where `node --version` is 22. A dedicated profile on
a debug port plus a small Node helper the dispatcher shells out to is the
cheapest real capability on this list, and the DOM is structured, so the model
stays cheap and reliable.

**New alternative worth surfacing:** Google now ships an official
[`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp)
(~29 tools over Puppeteer/CDP — navigation, input, network inspection,
performance traces, Lighthouse). Because the seam is now MCP anyway (§3), this
plugs straight into `--allowedTools` **with no code written here**. Cost: an npm
package (Chrome / Chrome-for-Testing only, not chromium-generic), and a
dependency to approve. Trade: write ~200 lines ourselves and own it, or approve
one npm dep and get 29 tools plus performance tooling for free.

### B. Window management + click-by-name (T2 via AT-SPI) — un-defers a deferred item

Per §1. Delivers the three verbs the doc gave up on (list / focus / close-ish)
**plus** semantic clicking, which the doc ranked as the sweet spot after the
Hermes audit. Dep decision needed (jeepney-raw vs pygobject). Coverage is
per-toolkit and the tree is messy — this is app-by-app, not universal, and
should be built behind the same never-raises/`detect()` shape as
`desktop.py`.

### C. A screenshot the agent can take by itself

`ask_screen.py` uses the **interactive area-select** Screenshot portal — fine
for a human-triggered question, useless for an agent loop. Two candidates, both
needing a spike: the Screenshot portal's non-interactive mode, and ScreenCast
with a persisted restore token. This is the precondition for any T4 pixel work,
and also for "what's on my screen right now" without a dialog.

### D. Sanctioned Wayland input (RemoteDesktop portal) — correct, but ydotool already works

Portal **v2 is present**, which is the version with `persist_mode` +
`restore_token`: one consent dialog, then a token that (on GNOME) survives
reboots — the token is single-use and rotates on each restore
([portal docs](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html),
[GNOME discussion](https://discourse.gnome.org/t/persistent-remote-desktop-access-api/19415)).
Reachable with jeepney; no new deps. It is the *right* channel (no `/dev/uinput`
group quirk, compositor-sanctioned), but ydotool already works and is
user-verified, so this is a correctness upgrade, not a capability one. Low
priority — worth documenting so the option isn't rediscovered later.

### E. `cua-driver` as a dependency — **still not viable here**

The doc flagged it as gated on a Wayland spike. The web answer is now explicit:
Linux support is **X11/XTEST primary, native Wayland in preview behind
`CUA_DRIVER_RS_ENABLE_WAYLAND=1`**, with "screen capture and full AT-SPI parity
still landing", and native-Wayland-only apps potentially invisible to the driver
([cua blog](https://cua.ai/blog/inside-linux-computer-use),
[trycua/cua](https://github.com/trycua/cua)). This box is GNOME/Wayland with
most apps native. **Recommend dropping it** — its two best ideas (AT-SPI as the
semantic layer; focus-free writes so the user keeps typing) are things we can
implement directly, and §1 shows the AT-SPI half is already reachable.

Two of its findings are worth lifting regardless: flip the `org.a11y.Status`
flags at startup so Chromium-family apps build trees, and keep the agent's
cursor separate from the user's physical pointer.

### F. Small polish: `GlobalShortcuts` portal (v1 present)

Would replace the ask-screen gsettings keybinding installed by
`setup_ask_screen.sh` with the sanctioned API. Cosmetic; mention only.

---

## 5. What has not changed

- The three OpenClaw mechanisms remain non-negotiable for anything
  coordinate-based: **frame/element staleness token**, **reference-frame
  normalization**, **held-input release on abort** (our PTT barge-in makes the
  third mandatory, not optional).
- Hermes' scoping rule still holds and now maps onto A/B above: browser tools
  for web, file tools for edits, desktop control reserved for native apps.
- The safety plane already exists and is reusable as-is — `computer.policy`
  allow/confirm/deny, SSE `confirm`, the dashboard `ConfirmBar`, voice
  "yeah"/"nope". Any new tier plugs into it rather than inventing one.
- **Session 23's lesson applies to all of this**: a verb that reports success is
  not a verb that was verified. Live-test through the running dispatcher.

---

## 6. Corrections owed to `future/computer-use.md`

1. §3 T1 "window list/focus/close via GNOME Shell D-Bus" — the *mechanism* is
   wrong but the *capability* is available via AT-SPI. Un-defer it.
2. §4 "evaluate cua-driver as a dependency" — evaluated; recommend dropping
   (§E above).
3. §7 Q1 (API key vs MCP) — answered by session 22's API removal (§3 above).
4. Environment check (2026-07-23) is stale: it lists neither `at-spi2-core`,
   `libei`, nor the portal versions, all of which change the tier ordering.

---

## 7. Open questions for the user

1. **Which direction first** — browser (A) or window/semantic control (B)?
   A is more useful per hour of work; B closes a gap the docs currently
   (wrongly) call impossible.
2. **`chrome-devtools-mcp` (one npm dep, ~29 tools, official) or hand-rolled CDP
   over the Node WebSocket (zero deps, ~200 lines, ours)?**
3. **AT-SPI access path**: approve `pygobject` in the venv, or take the
   zero-dep jeepney route and write more code?
4. Still open from the original doc: does any of this ever run **unattended**
   (from a timer/automation), or attended only? That decides whether the
   focus-free/no-focus-steal contract is a requirement or a nicety.
