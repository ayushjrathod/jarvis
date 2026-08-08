import React, { useEffect, useRef, useState } from "react";
import { subscribeEvents } from "./api.js";
import { widgets } from "./widgets/index.js";
import ConfirmBar from "./widgets/ConfirmBar.jsx";
import Notices from "./widgets/Notices.jsx";

export default function App() {
  // one SSE subscription for the whole app; widgets react to lastEvent
  const [lastEvent, setLastEvent] = useState(null);
  const [conn, setConn] = useState("online");
  // Bumped on every RE-connect. Widgets seed themselves from a REST endpoint on
  // mount and then only patch that state from events, so everything that
  // happened while the stream was down is invisible to them — a task that
  // started and finished during a dispatcher restart stays pinned as "running"
  // forever. They depend on this instead of [] so a reconnect re-seeds.
  const [epoch, setEpoch] = useState(0);
  const everConnected = useRef(false);

  useEffect(
    () =>
      subscribeEvents(
        (ev) => setLastEvent(ev),
        (status) => {
          setConn(status);
          // not on the first connect — the widgets' mount fetch is that seed
          if (status === "online") {
            if (everConnected.current) setEpoch((n) => n + 1);
            everConnected.current = true;
          }
        }
      ),
    []
  );

  return (
    <div className="app">
      <header>
        <h1>Mission Control</h1>
        <span className="sub">local · dispatcher :8765</span>
        <a className="docs-back" href="/system-docs">
          docs →
        </a>
      </header>
      {conn === "offline" && (
        <div className="conn-banner">
          Disconnected from the dispatcher — retrying. Values below may be stale.
        </div>
      )}
      <ConfirmBar lastEvent={lastEvent} />
      <Notices lastEvent={lastEvent} />
      <main className="grid">
        {widgets.map(({ id, title, Component }) => (
          <section key={id} className={`card card-${id}`}>
            <h2>{title}</h2>
            <Component lastEvent={lastEvent} epoch={epoch} />
          </section>
        ))}
      </main>
    </div>
  );
}
