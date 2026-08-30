// Dispatcher API helpers. POST /task answers either as SSE (quick) or JSON
// 202 (agentic) — postTask() normalizes both.

export const getJSON = (url) =>
  fetch(url).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${url}`))));

export const postJSON = (url, body) =>
  fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${url}`))));

function parseSSEBlock(block) {
  let event = null;
  const data = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!event) return null;
  try {
    return { event, data: data.length ? JSON.parse(data.join("\n")) : {} };
  } catch {
    return { event, data: {} };
  }
}

export async function postTask(text, { onDelta, source = "ui", mode, metadata } = {}) {
  const body = { text, source };
  if (mode) body.mode = mode;
  if (metadata) body.metadata = metadata;
  const resp = await fetch("/task", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    // surface a real error instead of parsing an error page as JSON/SSE
    let detail = "";
    try {
      detail = (await resp.json())?.detail ?? "";
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`POST /task ${resp.status}${detail ? `: ${detail}` : ""}`);
  }
  const ctype = resp.headers.get("content-type") || "";
  if (ctype.startsWith("application/json")) {
    return { kind: "agentic", ...(await resp.json()) };
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  const result = { kind: "quick", text: "" };
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const ev = parseSSEBlock(buf.slice(0, idx));
      buf = buf.slice(idx + 2);
      if (!ev) continue;
      if (ev.event === "delta") {
        result.text += ev.data.text ?? "";
        onDelta?.(ev.data.text ?? "");
      } else if (ev.event === "done") {
        Object.assign(result, ev.data, { kind: "quick" });
      } else if (ev.event === "task") {
        result.task_id = ev.data.task_id;
      }
    }
  }
  return result;
}

const EVENT_NAMES = ["queued", "started", "done", "failed", "refused", "cancelled",
                     "requeued", "step", "notify", "notify_skipped",
                     "automation", "automation_created", "confirm",
                     "confirm_resolved"];

// Backoff for re-opening a stream EventSource gave up on. Capped, because the
// dispatcher restarting is the common case and should recover in a second or
// two, while a box that is genuinely off shouldn't be hammered.
const RECONNECT_MS = [1000, 2000, 5000, 10000, 30000];

// onStatus("online" | "offline") is optional and fires on every transition.
export function subscribeEvents(onEvent, onStatus) {
  let es = null;
  let attempt = 0;
  let timer = null;
  let stopped = false;

  function connect() {
    es = new EventSource("/events");

    es.onopen = () => {
      attempt = 0;
      onStatus?.("online");
    };

    for (const name of EVENT_NAMES) {
      es.addEventListener(name, (e) => {
        try {
          onEvent({ ...JSON.parse(e.data), event: name });
        } catch {
          onEvent({ event: name });
        }
      });
    }

    // EventSource retries a *dropped* connection on its own, but a non-2xx on
    // reconnect is terminal: readyState goes CLOSED and nothing ever tries
    // again. That is exactly what `systemctl --user restart mission-dispatcher`
    // looks like through Tailscale Serve (502 while it's down), so before this
    // (2026-08-08) a routine restart left the dashboard — most painfully the
    // phone, where there is no console to notice it in — looking perfectly
    // healthy and permanently frozen.
    es.onerror = () => {
      if (stopped) return;
      onStatus?.("offline");
      if (es.readyState === EventSource.CLOSED) {
        es.close();
        const wait = RECONNECT_MS[Math.min(attempt, RECONNECT_MS.length - 1)];
        attempt += 1;
        clearTimeout(timer);
        timer = setTimeout(connect, wait);
      }
    };
  }

  connect();
  return () => {
    stopped = true;
    clearTimeout(timer);
    es?.close();
  };
}
