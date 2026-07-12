import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

// Live task/agent activity: seeded from /tasks, updated by /events SSE.
export default function AgentMonitor({ lastEvent }) {
  const [rows, setRows] = useState([]);
  const [expanded, setExpanded] = useState(() => new Set());

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
            area: t.area,
            created_at: t.created_at,
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

  function toggle(id) {
    setExpanded((open) => {
      const next = new Set(open);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  if (!rows.length) return <p className="empty">Nothing yet.</p>;
  return (
    <ul className="monitor">
      {rows.map((r) => {
        const open = expanded.has(r.task_id);
        return (
          <li key={r.task_id} className={open ? "open" : ""}>
            <button className="row" onClick={() => toggle(r.task_id)} aria-expanded={open}>
              <span className={`chip chip-${r.status}`}>{r.status}</span>
              <span className={`chip chip-kind`}>{r.kind}</span>
              <span className="text">{r.text}</span>
              {r.cost_usd != null && <span className="cost">${r.cost_usd.toFixed(2)}</span>}
              <span className="caret">{open ? "▾" : "▸"}</span>
            </button>
            {open && (
              <div className="detail">
                <p className="full-text">{r.text}</p>
                <dl>
                  <dt>task</dt>
                  <dd>{r.task_id}</dd>
                  <dt>source</dt>
                  <dd>{r.source ?? "—"}</dd>
                  {r.area && (
                    <>
                      <dt>area</dt>
                      <dd>{r.area}</dd>
                    </>
                  )}
                  {r.created_at && (
                    <>
                      <dt>created</dt>
                      <dd>{new Date(r.created_at).toLocaleString()}</dd>
                    </>
                  )}
                  {r.model && (
                    <>
                      <dt>model</dt>
                      <dd>{r.model}</dd>
                    </>
                  )}
                  {r.cost_usd != null && (
                    <>
                      <dt>cost</dt>
                      <dd>${r.cost_usd.toFixed(4)}</dd>
                    </>
                  )}
                  {r.error && (
                    <>
                      <dt>error</dt>
                      <dd className="err">{r.error}</dd>
                    </>
                  )}
                </dl>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
