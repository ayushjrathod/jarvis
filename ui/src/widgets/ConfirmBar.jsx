import React, { useEffect, useState } from "react";
import { postJSON } from "../api.js";

// Desktop verbs whose policy is "confirm" (clipboard_get, open) park in the
// dispatcher and fire an SSE `confirm` event instead of running. This banner is
// the answer channel. It sits ABOVE the grid rather than inside a card on
// purpose: it's a blocking question with a timeout (config confirm_timeout_s),
// not a status readout, so it must not scroll away among the widgets.
export default function ConfirmBar({ lastEvent }) {
  const [pending, setPending] = useState([]);
  const [resolved, setResolved] = useState(null);

  useEffect(() => {
    if (lastEvent?.event !== "confirm" || !lastEvent.confirm_id) return;
    setPending((list) =>
      list.some((p) => p.confirm_id === lastEvent.confirm_id)
        ? list
        : [...list, lastEvent]
    );
  }, [lastEvent]);

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
    <div className="confirmbar">
      {pending.map((p) => (
        <div key={p.confirm_id} className="confirm-row">
          <span className="confirm-q">{p.speech || `Shall I ${p.description}?`}</span>
          <span className="confirm-actions">
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
