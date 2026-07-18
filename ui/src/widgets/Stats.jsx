import React, { useEffect, useState } from "react";
import { getJSON } from "../api.js";

// Observability tiles over /stats (Phase H) + the self-improvement strip:
// what the reflection passes recently saved ("Jarvis learned").
export default function Stats({ lastEvent }) {
  const [stats, setStats] = useState(null);

  const refresh = () => getJSON("/stats").then(setStats).catch(() => {});
  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {
    if (["done", "failed", "cancelled"].includes(lastEvent?.event)) refresh();
  }, [lastEvent]);

  if (!stats) return <p className="empty">No stats yet.</p>;
  const s = stats;
  const total = Object.values(s.tasks_by_status ?? {}).reduce((a, b) => a + b, 0);
  const tiles = [
    { label: `tasks ${s.days}d`, value: total },
    {
      label: "success",
      value: s.success_rate != null ? `${Math.round(s.success_rate * 100)}%` : "—",
    },
    { label: `cost ${s.days}d`, value: `$${(s.total_cost_usd ?? 0).toFixed(2)}` },
    {
      label: "quick TTFT",
      value:
        s.quick_latency?.avg_ttft_ms != null
          ? `${(s.quick_latency.avg_ttft_ms / 1000).toFixed(1)}s`
          : "—",
    },
  ];
  const learned = (s.recent_reflections ?? []).filter(
    (r) => r.output_text && !/^nothing to save\.?$/i.test(r.output_text.trim())
  );
  return (
    <div className="stats">
      <div className="tiles">
        {tiles.map((t) => (
          <div className="tile" key={t.label}>
            <div className="value">{t.value}</div>
            <div className="label">{t.label}</div>
          </div>
        ))}
      </div>
      <div className="reflections">
        <h3>Jarvis learned</h3>
        {learned.length ? (
          <ul>
            {learned.map((r, i) => (
              <li key={i}>
                <span className="when">{(r.finished_at ?? "").slice(0, 10)}</span>{" "}
                {r.output_text}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty">Nothing new from recent reflections.</p>
        )}
      </div>
    </div>
  );
}
