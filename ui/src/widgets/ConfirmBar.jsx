import React, { useEffect, useState } from "react";
import { postJSON } from "../api.js";

// Desktop verbs whose policy is "confirm" (clipboard_get, open) park in the
// dispatcher and fire an SSE `confirm` event instead of running. This banner is
// the answer channel. It sits ABOVE the grid rather than inside a card on
// purpose: it's a blocking question with a timeout (config confirm_timeout_s),
// not a status readout, so it must not scroll away among the widgets.
//
// The server parks the intent for `timeout_s` and then drops it. Until
// 2026-08-11 this component never expired anything, so a banner kept offering
// Yes on an id the dispatcher had already forgotten — click it and you got
// "that confirmation expired", with nothing beforehand to say the window was
// closing. A dispatcher restart drops every pending intent too, and the banner
// outlived that as well. Now each row carries its own deadline and removes
// itself, which is also the honest direction to fail: the parked verbs are
// clipboard_get and open, so a silently-stale Yes button is the one thing this
// UI must not offer.
const DEFAULT_TIMEOUT_S = 120;

export default function ConfirmBar({ lastEvent }) {
  const [pending, setPending] = useState([]);
  const [resolved, setResolved] = useState(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (lastEvent?.event !== "confirm" || !lastEvent.confirm_id) return;
    const ttl = (lastEvent.timeout_s ?? DEFAULT_TIMEOUT_S) * 1000;
    setPending((list) =>
      list.some((p) => p.confirm_id === lastEvent.confirm_id)
        ? list
        : [...list, { ...lastEvent, expiresAt: Date.now() + ttl }]
    );
  }, [lastEvent]);

  // Answered anywhere else (a bare "yeah" by voice spends the id server-side
  // and fires confirm_resolved): drop the row instead of offering Yes on a
  // spent confirmation for the rest of its TTL.
  useEffect(() => {
    if (lastEvent?.event !== "confirm_resolved" || !lastEvent.confirm_id) return;
    setPending((list) => list.filter((p) => p.confirm_id !== lastEvent.confirm_id));
  }, [lastEvent]);

  // One timer for the whole bar rather than one per row: it only drives a
  // seconds countdown, and it stops entirely when nothing is pending.
  useEffect(() => {
    if (!pending.length) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [pending.length]);

  useEffect(() => {
    setPending((list) => {
      const live = list.filter((p) => p.expiresAt > now);
      return live.length === list.length ? list : live;
    });
  }, [now]);

  async function answer(confirm_id, approve) {
    setPending((list) => list.filter((p) => p.confirm_id !== confirm_id));
    try {
      const out = await postJSON("/desktop/confirm", { confirm_id, approve });
      setResolved(out.speech || null);
    } catch (e) {
      setResolved(`Couldn't answer that: ${e.message}`);
    }
  }

  if (!pending.length && !resolved) return null;

  return (
    <div className="confirmbar" role="alert">
      {pending.map((p) => (
        <div key={p.confirm_id} className="confirm-row">
          <span className="confirm-q">{p.speech || `Shall I ${p.description}?`}</span>
          <span className="confirm-actions">
            <span className="confirm-ttl">
              {Math.max(0, Math.ceil((p.expiresAt - now) / 1000))}s
            </span>
            <button className="yes" onClick={() => answer(p.confirm_id, true)}>
              Yes
            </button>
            <button onClick={() => answer(p.confirm_id, false)}>No</button>
          </span>
        </div>
      ))}
      {resolved && (
        <div className="confirm-row resolved">
          <span>{resolved}</span>
          <button onClick={() => setResolved(null)}>dismiss</button>
        </div>
      )}
    </div>
  );
}
