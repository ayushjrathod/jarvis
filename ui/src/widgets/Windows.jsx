import React, { useCallback, useEffect, useState } from "react";
import { postJSON } from "../api.js";

// Open windows (T2), read-only. POSTs the list phrasing to /desktop and
// renders the executor's own sentence — like Volume, no state is parsed,
// so the card can never disagree with the tier. Focus stays in the
// confirm plane (dashboard banner or a bare yeah), not in one-click
// buttons. Refreshes with the app epoch and whenever a desktop event
// lands, so a focus you trigger by voice shows up here too.
export default function Windows({ lastEvent, epoch }) {
  const [line, setLine] = useState(null);

  const refresh = useCallback(() => {
    postJSON("/desktop", { command: "what windows are open", source: "ui" })
      .then((out) => setLine(out.speech || out.status))
      .catch((e) => setLine(`Couldn't list windows: ${e.message}`));
  }, [epoch]);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    if (lastEvent?.kind === "desktop") refresh();
  }, [lastEvent, refresh]);

  if (!line) return <p className="empty">loading…</p>;
  return <p className="meta">{line}</p>;
}
