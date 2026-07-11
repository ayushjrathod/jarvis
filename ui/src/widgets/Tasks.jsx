import React, { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "../api.js";

const today = () => new Date().toISOString().slice(0, 10);

export default function Tasks({ lastEvent }) {
  const [tasks, setTasks] = useState([]);

  const refresh = useCallback(() => {
    getJSON("/vault/tasks").then(setTasks).catch(() => setTasks([]));
  }, []);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    // voice/agent runs may have created or completed task files
    if (lastEvent?.event === "done" && lastEvent.kind === "agentic") refresh();
  }, [lastEvent, refresh]);

  async function toggle(file) {
    await postJSON(`/vault/tasks/${encodeURIComponent(file)}/toggle`);
    refresh();
  }

  if (!tasks.length) return <p className="empty">No tasks — say "add a task: …"</p>;
  return (
    <ul className="tasks">
      {tasks.map((t) => (
        <li key={t.file} className={t.status === "done" ? "done" : ""}>
          <label>
            <input
              type="checkbox"
              checked={t.status === "done"}
              onChange={() => toggle(t.file)}
            />
            <span className="title">{t.title}</span>
            {t.due && (
              <span className={`due ${t.status === "open" && t.due < today() ? "overdue" : ""}`}>
                {t.due}
              </span>
            )}
          </label>
        </li>
      ))}
    </ul>
  );
}
