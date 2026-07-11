import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

// Live task/agent activity: seeded from /tasks, updated by /events SSE.
export default function AgentMonitor({ lastEvent }) {
  const [rows, setRows] = useState([]);

  useEffect(() => {
    getJSON("/tasks?limit=15")
      .then((tasks) =>
        setRows(
          tasks.map((t) => ({
            task_id: t.id,
            kind: t.kind,
            status: t.status,
            text: t.text,
            source: t.source,
          }))
        )
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!lastEvent?.task_id) return;
    setRows((rows) => {
      const next = rows.filter((r) => r.task_id !== lastEvent.task_id);
      const prev = rows.find((r) => r.task_id === lastEvent.task_id) ?? {};
      const status = { queued: "queued", started: "running", refused: "retrying" }[lastEvent.event] ?? lastEvent.event;
      next.unshift({ ...prev, ...lastEvent, status });
      return next.slice(0, 15);
    });
  }, [lastEvent]);

  if (!rows.length) return <p className="empty">Nothing yet.</p>;
  return (
    <ul className="monitor">
      {rows.map((r) => (
        <li key={r.task_id}>
          <span className={`chip chip-${r.status}`}>{r.status}</span>
          <span className={`chip chip-kind`}>{r.kind}</span>
          <span className="text" title={r.text}>{r.text}</span>
          {r.cost_usd != null && <span className="cost">${r.cost_usd.toFixed(2)}</span>}
        </li>
      ))}
    </ul>
  );
}
