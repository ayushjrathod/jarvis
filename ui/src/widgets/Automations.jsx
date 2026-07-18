import React, { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "../api.js";

// Standing automations (Phase I): list, pause/resume, delete. Creation goes
// through the command box ("every morning, tell me …") or POST /automations.
export default function Automations({ lastEvent }) {
  const [rows, setRows] = useState([]);

  const refresh = useCallback(() => {
    getJSON("/automations").then(setRows).catch(() => setRows([]));
  }, []);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    if (["automation", "automation_created"].includes(lastEvent?.event)) refresh();
  }, [lastEvent, refresh]);

  async function toggle(id) {
    await postJSON(`/automations/${id}/toggle`);
    refresh();
  }

  async function remove(id) {
    await fetch(`/automations/${id}`, { method: "DELETE" });
    refresh();
  }

  if (!rows.length) return <p className="empty">None yet — say "every morning, tell me …"</p>;
  return (
    <ul className="automations">
      {rows.map((a) => (
        <li key={a.id} className={a.enabled ? "" : "paused"}>
          <div className="auto-main">
            <span className="title">{a.task_text}</span>
            <span className="sched">{a.describe}</span>
            {a.enabled && a.next_run_at && (
              <span className="next">next {new Date(a.next_run_at).toLocaleString()}</span>
            )}
            {!a.enabled && <span className="next">paused</span>}
          </div>
          <div className="auto-actions">
            <button onClick={() => toggle(a.id)}>{a.enabled ? "pause" : "resume"}</button>
            <button className="danger" onClick={() => remove(a.id)}>delete</button>
          </div>
        </li>
      ))}
    </ul>
  );
}
