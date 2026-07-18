import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

// Live task/agent activity: seeded from /tasks, updated by /events SSE.
export default function AgentMonitor({ lastEvent }) {
  const [rows, setRows] = useState([]);
  const [expanded, setExpanded] = useState(() => new Set());
  const [steps, setSteps] = useState({}); // task_id -> flat step list

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
      if (lastEvent.event === "step") {
        // live timeline tick: keep the row's status, remember the last step
        next.unshift({
          ...prev, ...lastEvent,
          status: prev.status ?? "running",
          last_step: { step_type: lastEvent.step_type, summary: lastEvent.summary },
        });
        return next.slice(0, 15);
      }
      const status = { queued: "queued", started: "running", refused: "retrying" }[lastEvent.event] ?? lastEvent.event;
      next.unshift({ ...prev, ...lastEvent, status, last_step: null });
      return next.slice(0, 15);
    });
  }, [lastEvent]);

  function toggle(id, kind) {
    setExpanded((open) => {
      const next = new Set(open);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
    if (kind === "agentic" && !steps[id]) {
      getJSON(`/task/${id}/steps`)
        .then((byRun) => setSteps((m) => ({ ...m, [id]: Object.values(byRun).flat() })))
        .catch(() => {});
    }
  }

  if (!rows.length) return <p className="empty">Nothing yet.</p>;
  return (
    <ul className="monitor">
      {rows.map((r) => {
        const open = expanded.has(r.task_id);
        return (
          <li key={r.task_id} className={open ? "open" : ""}>
            <button className="row" onClick={() => toggle(r.task_id, r.kind)} aria-expanded={open}>
              <span className={`chip chip-${r.status}`}>{r.status}</span>
              <span className={`chip chip-kind`}>{r.kind}</span>
              <span className="text">{r.text}</span>
              {r.cost_usd != null && <span className="cost">${r.cost_usd.toFixed(2)}</span>}
              <span className="caret">{open ? "▾" : "▸"}</span>
            </button>
            {!open && r.status === "running" && r.last_step && (
              <div className="live-step">
                {r.last_step.step_type}: {r.last_step.summary}
              </div>
            )}
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
                  {r.ttft_ms != null && (
                    <>
                      <dt>ttft</dt>
                      <dd>{(r.ttft_ms / 1000).toFixed(1)}s</dd>
                    </>
                  )}
                  {r.tokens_per_s != null && (
                    <>
                      <dt>tok/s</dt>
                      <dd>{r.tokens_per_s}</dd>
                    </>
                  )}
                  {r.error && (
                    <>
                      <dt>error</dt>
                      <dd className="err">{r.error}</dd>
                    </>
                  )}
                </dl>
                {(steps[r.task_id] ?? []).length > 0 && (
                  <ol className="steps">
                    {steps[r.task_id].map((s, i) => (
                      <li key={i}>
                        <span className="t">{((s.elapsed_ms ?? 0) / 1000).toFixed(1)}s</span>
                        <span className="stype">{s.step_type}</span>
                        <span className="ssum">{s.summary}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
