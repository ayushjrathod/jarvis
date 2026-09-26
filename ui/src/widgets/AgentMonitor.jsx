import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

const TERMINAL = new Set(["done", "failed", "cancelled"]);

// Server-internal plumbing: after a nightly cycle the window fills with
// these and the user's own tasks scroll away. Hidden by default, one click
// to audit. Matches the sources the dispatch path actually emits (see
// memory.should_capture's exclusion list for the same family).
const INTERNAL_SOURCES = new Set([
  "automation-parse", "notify-gate", "media-parse",
  "graph-extract", "graph-reconcile", "reflection", "automation",
]);

// Live task/agent activity: seeded from /tasks, updated by /events SSE.
export default function AgentMonitor({ lastEvent, epoch }) {
  const [rows, setRows] = useState([]);
  const [showSystem, setShowSystem] = useState(false);
  const [expanded, setExpanded] = useState(() => new Set());
  const [steps, setSteps] = useState({}); // task_id -> flat step list

  useEffect(() => {
    seed();
  }, [epoch]);

  // Toggling the filter re-seeds at the matching width.
  useEffect(() => {
    seed();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showSystem]);

  // Nothing reconciles a healthy stream: both loss paths above leave SSE up,
  // so no reconnect fires and a row whose terminal event was dropped reads
  // non-terminal until reload. While any visible row is non-terminal, poll
  // /tasks lightly; the moment everything settles, the interval clears
  // itself — no background churn on a quiet board.
  useEffect(() => {
    if (!rows.some((r) => !TERMINAL.has(r.status))) return;
    const t = setInterval(seed, 5000);
    return () => clearInterval(t);
  });

  function seed() {
    // Fetch wide when hiding plumbing so the visible window stays full.
    getJSON(showSystem ? "/tasks?limit=15" : "/tasks?limit=40")
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
  }

  useEffect(() => {
    if (!lastEvent?.task_id) return;
    // automation/notify events reference a task but aren't status changes
    if (["notify", "notify_skipped", "automation", "automation_created"].includes(lastEvent.event)) return;
    // keep an expanded row's timeline live: append the step instead of waiting
    // for a re-expand to refetch
    if (lastEvent.event === "step" && expanded.has(lastEvent.task_id)) {
      setSteps((m) => ({
        ...m,
        [lastEvent.task_id]: [
          ...(m[lastEvent.task_id] ?? []),
          { step_type: lastEvent.step_type, summary: lastEvent.summary,
            elapsed_ms: lastEvent.elapsed_ms },
        ],
      }));
    }
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

  function toggle(id, kind, status) {
    const willOpen = !expanded.has(id);
    setExpanded((open) => {
      const next = new Set(open);
      willOpen ? next.add(id) : next.delete(id);
      return next;
    });
    // fetch on expand; also refetch a non-terminal row whose cached steps may
    // be incomplete (it was expanded mid-run)
    if (willOpen && kind === "agentic" && (!steps[id] || !TERMINAL.has(status))) {
      getJSON(`/task/${id}/steps`)
        .then((byRun) => setSteps((m) => ({ ...m, [id]: Object.values(byRun).flat() })))
        .catch(() => {});
    }
  }

  if (!rows.length) return <p className="empty">Nothing yet.</p>;
  const visible = (showSystem ? rows : rows.filter((r) => !INTERNAL_SOURCES.has(r.source))).slice(0, 15);
  return (
    <>
    <label className="sys-toggle">
      <input type="checkbox" checked={showSystem}
             onChange={(e) => setShowSystem(e.target.checked)} />
      system tasks
    </label>
    <ul className="monitor">
      {visible.map((r) => {
        const open = expanded.has(r.task_id);
        return (
          <li key={r.task_id} className={open ? "open" : ""}>
            <button className="row" onClick={() => toggle(r.task_id, r.kind, r.status)} aria-expanded={open}>
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
    </>
  );
}
