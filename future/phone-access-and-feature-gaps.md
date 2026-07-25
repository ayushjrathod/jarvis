# Phone access + feature-gap research

_Written 2026-07-23 (session 16). Planning only — no code written, nothing built._

Two questions were researched this session:

1. What does the 2026 personal-agent field consider must-have, and what are we missing?
2. How should the system be reachable from a phone?

Calendar awareness was researched and then **explicitly dropped by the user** — the
notes are kept at the foot of this file in case it comes back.

---

## 1. Field scan — what counts as table stakes in 2026

Across current roundups of personal/local AI assistants, four things separate a
"chatbot" from an "assistant":

- **Proactive / ambient operation** — works your inbox and calendar without being asked.
- **Reach from your phone** — messaging bridges or a remote-accessible UI are the
  default front-end; localhost-only is treated as a dev setup, not a product.
- **Real-world actions with human-in-the-loop checkpoints** — the credible systems
  define exact moments a person reviews/stops the agent, rather than either
  full autonomy or full manual.
- **Local-first privacy plus model-routing fallback** — degrade, don't die.

Memory, skills, MCP tools and self-improvement loops are now *assumed baseline* —
which is precisely the part this repo already has.

### Gap analysis vs. Mission Control

| Field expectation | Our state | Verdict |
|---|---|---|
| Persistent memory, semantic + graph | Phases F/J, live | ahead of most |
| Self-improving skills | Phase G reflection + curator | ahead (Hermes-class) |
| Observability | Phase H | ahead |
| Voice in/out, barge-in | Phase B | done |
| Ingest/RAG over own files | Phase I | done |
| Scheduled automations + notify gate | Phase I | done |
| Email/calendar awareness | Gmail MCP unstarted (OAuth pending), no calendar | **gap** (user dropped for now) |
| **Reach from phone** | 127.0.0.1 only | **gap — this doc's subject** |
| Event-driven proactivity | time-triggered only | gap |
| Approval queue for outbound actions | read-only scopes, no HITL surface | gap |
| Model fallback when the plan cap hits | dead until reset (hit twice, sessions 7/10) | gap |
| Browser / computer use | none | optional |
| Planner / complexity tiers | H4, deliberately unbuilt | optional |

**Explicitly not worth building** for a single-maintainer local system, despite being
field trends: browser automation, multi-channel fan-out (Discord/Slack/WhatsApp),
agent marketplaces, cloud routines, multi-agent fan-out.

---

## 2. Phone access — the security fact that drives everything

`dispatcher/main.py:123` implements an **origin guard, not authentication.** It
rejects cross-origin *browser* requests; it does nothing against a direct `curl`.
That is safe today only because `config.yaml:5` binds `127.0.0.1`, so the sole
reachable client is the local machine.

Consequence: **any option that puts the dispatcher on the public internet requires
building token auth first.** Any option that keeps it on a private network requires
no auth work at all. Without auth, whoever has the URL can:

- `POST /task` — spend the Claude budget, run agentic tools
- `GET /memory/search` — read the entire vault
- `GET /screenshots/*` — pull captured screen images

Second, subtler issue for any public-URL option: the guard also *breaks* the SPA.
Browsers send `Origin` on same-origin POSTs, and `_origin_is_local` trusts only
loopback plus `cfg.host` — so a dashboard loaded from a tunnel domain would have
its own POSTs rejected until a `public_host` entry is added to the trusted set.

### Options compared

| Option | Exposure | Auth work needed | Off home wifi | Cost |
|---|---|---|---|---|
| **Tailscale** | private mesh, device-to-device | **none** | yes | free (3 users / 100 devices) |
| **Cloudflare Tunnel + Access** | public URL, Google login at the edge | none (edge does it) | yes | free (needs a CF domain) |
| **ngrok** | public URL, open | **required** — free tier has no OAuth/IP allowlist | yes | free: 1 static domain, 20k req/mo |
| **LAN only** (`host: 0.0.0.0`) | home network | none, if the wifi is trusted | **no** | free |
| **Telegram bot** | no inbound port at all | none (allowlist chat ID) | yes | free |
| **WireGuard on a VPS** | private, self-run | none | yes | ~$5/mo + setup |

### Recommendation: Tailscale

Install on the Arch box and the phone; both join the tailnet. The dispatcher either
binds its tailnet IP or stays on loopback with Tailscale Serve in front. Dashboard,
`/ask`, and the mic button all work as-is over `https://<machine>.<tailnet>.ts.net`.

- No token middleware, no origin-guard change, no public surface.
- The "anyone who finds the URL spends my budget" failure mode never exists.
- Effort: roughly one session including a systemd unit; the phone app does the rest.
- Trade-off: your-devices-only, no shareable link — for a personal life OS that is
  a feature, not a limitation.

**Cloudflare Tunnel + Access** is the alternative if a bookmarkable URL matters.
`cloudflared` dials outbound so no ports open, and Access puts a free Google login
in front, so again no code changes. Costs: a domain on Cloudflare, and traffic
transits their edge — a larger trust delegation than Tailscale's encrypted mesh.

**ngrok** (the user's initial instinct) is the worst fit of the three tunnels. It is
built for temporary dev sharing, and its free tier withholds exactly the auth
features that would make a permanently-exposed personal assistant safe. Choosing it
means committing to bearer-token middleware **plus** the `public_host` origin-guard
fix, and accepting that a leaked URL is a full compromise until rotated. It is the
only option where a mistake is expensive.

**LAN-only** deserves naming because it is ~10 minutes: flip `config.yaml:5` to
`0.0.0.0`, confirm the firewall. If "phone access" mostly means *from the couch*,
this is sufficient and the work stops there.

**Telegram** is a different shape — not remote access to the dashboard but a second
dispatcher client, alongside voice and the SPA. Nothing inbound at all, works on bad
networks, and inherits push notifications, which would finally give the Phase I
notify gate a real delivery channel. Loses the dashboard UI; message text transits
Telegram's servers.

If both dashboard and push are wanted, **Tailscale now + a small Telegram bridge
later** is the least-exposure combination.

### If ngrok is chosen anyway — the ordered, non-skippable steps

1. **Token auth.** `security.auth_token` in config; middleware requires
   `Authorization: Bearer <token>` on every route where the request did *not*
   arrive on loopback. Loopback stays open so voice, dictation, ask-screen and the
   timers keep working untouched. The SPA takes the token from `?k=` on first load
   and stores it in `localStorage`.
2. **Origin guard fix.** Add `security.public_host` (the ngrok domain) to the
   trusted set in `_origin_is_local`.
3. **Tunnel.** `systemd/mission-tunnel.service` running
   `ngrok http 8765 --domain=<static>.ngrok-free.dev`; optionally stack ngrok
   Traffic Policy basic auth as a second layer.

Steps 1 and 2 must land and be tested **before** the tunnel unit is enabled.

### Free addendum, whichever option wins

The dashboard is already a responsive SPA and `/ask` already has a mic button backed
by `POST /stt`. Adding a web-app manifest makes it an installable PWA — a ~10-minute
addendum, not a phase.

---

## Dropped: calendar awareness

Researched, then dropped by the user mid-session. Preserved for revival:

- **Secret ICS URL** (was the recommendation): Google Calendar's "secret address in
  iCal format", polled on the existing automations scheduler, parsed to today/tomorrow
  events, written to `vault/inbox/calendar-<date>.md` so the existing
  ingest → FTS → vector path indexes it for free. No OAuth, no browser flow, VEVENT
  parsing simple enough to stay dependency-free. Downside: read-only forever, and the
  secret URL is a bearer token sitting in config.
- **MCP** (`claude-per mcp add`): richer, supports later write/scheduling, but needs
  OAuth and only the *agentic* path sees it — the quick voice path would have no
  calendar context.

---

## Open decisions

1. ~~Which phone-access option~~ — **Tailscale chosen (session 18, 2026-07-24)**;
   the origin-guard + serve-unit + setup-script + PWA are implemented. User-side:
   `sudo pacman -S tailscale` then `scripts/setup_tailscale.sh` (installs the
   Tailscale app on the phone, joins the tailnet, adds the printed
   `public_hosts` line to config.yaml). See CLAUDE.md "phone access (Tailscale)".
2. ~~Whether the PWA manifest rides along~~ — **yes, shipped** (session 18):
   manifest + service worker + icons in `ui/public/`, dashboard installs to the
   phone home screen.
3. Whether any of the other gaps above (event-driven proactivity, approval queue,
   local-model fallback) get promoted into a v3 phase plan. **Still open.**

## Sources

- https://www.vellum.ai/blog/best-open-source-personal-ai-assistants
- https://www.vellum.ai/blog/best-local-ai-assistants
- https://www.sitepoint.com/the-rise-of-open-source-personal-ai-agents-a-new-os-paradigm/
- https://lifestack.ai/blog/proactive-ai-assistant
- https://www.usecarly.com/blog/ai-agent-triages-your-inbox/
- https://getclawdbot.com/blog/self-hosted-ai-agent-complete-guide-2026/
- https://needtoknowit.com.au/blog/tailscale-vs-cloudflare-tunnels-for-remote-access/
- https://toolradar.com/compare/tailscale-vs-cloudflare-tunnel
- https://contabo.com/blog/pangolin-vs-cloudflare-tunnels-vs-tailscale/
- https://ngrok.com/blog/free-static-domains-ngrok-users
- https://ngrok.com/blog/authentication-with-ngrok
