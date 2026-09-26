import React, { useEffect, useState } from "react";

const DISMISS_MS = 20000;
const MAX = 4;

// Proactive announcements: the notify-or-not gate's verdict on an automation or
// timer result, the inbox watcher's "Indexed report.pdf — searchable now", and
// (since 2026-08-08) a deterministic divert's own sentence.
//
// Until 2026-08-08 these events were subscribed in api.js and rendered NOWHERE:
// AgentMonitor explicitly skips them and no other widget read them. On the box
// that was masked by notify-send, but on the phone — which is the entire reason
// the PWA and the Tailscale work exist — the whole proactive-notification
// feature was invisible. `task_id` is null for inbox notices, so anything
// keying on it drops them; key on the event name instead.
export default function Notices({ lastEvent }) {
  const [items, setItems] = useState([]); // [{key, text, at}]

  useEffect(() => {
    if (lastEvent?.event !== "notify") return;
    const text = lastEvent.speech || lastEvent.summary;
    if (!text) return;
    // a monotonic key, because task_id is null for inbox/divert notices and two
    // notices can otherwise collide on the same React key
    setItems((list) => [
      ...list.slice(-(MAX - 1)),
      { key: `${Date.now()}-${list.length}`, text, at: Date.now() },
    ]);
  }, [lastEvent]);

  useEffect(() => {
    if (!items.length) return;
    const t = setTimeout(
      () => setItems((list) => list.filter((i) => Date.now() - i.at < DISMISS_MS)),
      DISMISS_MS
    );
    return () => clearTimeout(t);
  }, [items]);

  if (!items.length) return null;
  return (
    <div className="notices" role="status">
      {items.map((i) => (
        <div key={i.key} className="notice-row">
          <span className="notice-text">{i.text}</span>
          <button
            onClick={() => setItems((list) => list.filter((x) => x.key !== i.key))}
          >
            dismiss
          </button>
        </div>
      ))}
    </div>
  );
}
