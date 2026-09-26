import React, { useCallback, useEffect, useState } from "react";
import { postJSON } from "../api.js";

// Open browser tabs (T3), read-only like the Windows card: POSTs the list
// phrasing and renders the executor's sentence. Switching and reading stay
// in the confirm plane. Refreshes on epoch and desktop events.
export default function Tabs({ lastEvent, epoch }) {
  const [line, setLine] = useState(null);

  const refresh = useCallback(() => {
    postJSON("/desktop", { command: "what tabs are open", source: "ui" })
      .then((out) => setLine(out.speech || out.status))
      .catch((e) => setLine(`Couldn't list tabs: ${e.message}`));
  }, [epoch]);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    if (lastEvent?.kind === "desktop") refresh();
  }, [lastEvent, refresh]);

  if (!line) return <p className="empty">loading…</p>;
  return <p className="meta">{line}</p>;
}
