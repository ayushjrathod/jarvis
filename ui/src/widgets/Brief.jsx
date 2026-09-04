import React, { useCallback, useEffect, useState } from "react";
import { getJSON } from "../api.js";

export default function Brief({ lastEvent, epoch }) {
  const [brief, setBrief] = useState(null);
  const [missing, setMissing] = useState(false);

  const refresh = useCallback(() => {
    getJSON("/vault/brief")
      .then((b) => {
        setBrief(b);
        setMissing(false);
      })
      .catch(() => setMissing(true));
  }, [epoch]);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    if (lastEvent?.event === "done" && lastEvent.kind === "agentic") refresh();
  }, [lastEvent, refresh]);

  // The quiet path writes the file with no task and no event — the normal
  // day here — so event-driven refresh alone leaves yesterday up until a
  // reconnect. Poll lightly until today's brief exists, then stop: a daily
  // file needs no background churn once it's here.
  useEffect(() => {
    if (brief?.is_today) return undefined;
    const t = setInterval(refresh, 5 * 60 * 1000);
    return () => clearInterval(t);
  }, [brief?.is_today, refresh]);

  if (missing) return <p className="empty">No briefs yet — the morning timer writes the first one.</p>;
  if (!brief) return <p className="empty">loading…</p>;
  return (
    <div>
      <p className="meta">
        {brief.file} {brief.is_today ? "" : "(latest — none for today yet)"}
      </p>
      <pre className="brief">{brief.content}</pre>
    </div>
  );
}
