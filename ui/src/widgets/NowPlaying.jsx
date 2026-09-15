import React, { useCallback, useEffect, useState } from "react";
import { getJSON } from "../api.js";

// Now playing (media tier): player snapshot over GET /media/state. The
// player changes outside our events (pause in the Spotify app itself), so
// this polls lightly instead of pretending the stream covers it — and says
// plainly when media control is off or the player is down, rather than
// rendering an empty card.
export default function NowPlaying({ epoch }) {
  const [state, setState] = useState(null);
  const [off, setOff] = useState(false);

  const refresh = useCallback(() => {
    getJSON("/media/state")
      .then((s) => {
        setState(s);
        setOff(false);
      })
      .catch((e) => {
        if (String(e.message).startsWith("503")) setOff(true);
      });
  }, [epoch]);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  if (off) return <p className="empty">Media control is disabled in config.</p>;
  if (!state) return <p className="empty">loading…</p>;
  if (!state.running) return <p className="empty">Spotify isn't running.</p>;
  const bits = [state.title, state.artist].filter(Boolean).join(" — ");
  return (
    <div className="now-playing">
      <p className="np-title">{bits || "(nothing loaded)"}</p>
      <p className="meta">
        {[state.status, state.album].filter(Boolean).join(" · ")}
      </p>
    </div>
  );
}
