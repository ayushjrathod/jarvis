import React, { useEffect, useState } from "react";
import { subscribeEvents } from "./api.js";
import { widgets } from "./widgets/index.js";

export default function App() {
  // one SSE subscription for the whole app; widgets react to lastEvent
  const [lastEvent, setLastEvent] = useState(null);
  const [eventLog, setEventLog] = useState([]);

  useEffect(
    () =>
      subscribeEvents((ev) => {
        setLastEvent(ev);
        setEventLog((log) => [ev, ...log].slice(0, 100));
      }),
    []
  );

  return (
    <div className="app">
      <header>
        <h1>Mission Control</h1>
        <span className="sub">local · dispatcher :8765</span>
      </header>
      <main className="grid">
        {widgets.map(({ id, title, Component }) => (
          <section key={id} className={`card card-${id}`}>
            <h2>{title}</h2>
            <Component lastEvent={lastEvent} eventLog={eventLog} />
          </section>
        ))}
      </main>
    </div>
  );
}
