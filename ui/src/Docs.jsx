import React, { useEffect, useState } from "react";
import { getJSON } from "./api.js";
import { endpoints, guide } from "./docs/content.js";

// Documentation page served at /system-docs (NOT /docs — that is FastAPI's
// Swagger UI). Content is data in docs/content.js; this file only renders it.

function Block({ block }) {
  if (block.p) return <p>{block.p}</p>;
  if (block.note) return <p className="docs-note">{block.note}</p>;
  if (block.code) return <pre className="docs-code">{block.code}</pre>;
  if (block.list)
    return (
      <ul className="docs-list">
        {block.list.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    );
  if (block.table) {
    const { head, rows } = block.table;
    return (
      <div className="docs-scroll">
        <table className="docs-table">
          <thead>
            <tr>
              {head.map((h) => (
                <th key={h}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td key={j}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return null;
}

function EndpointGroup({ group }) {
  return (
    <div className="docs-endpoints" id={group.id}>
      <h3>{group.group}</h3>
      <div className="docs-scroll">
        <table className="docs-table">
          <thead>
            <tr>
              <th>Endpoint</th>
              <th>What it does</th>
              <th>Params</th>
              <th>Returns</th>
            </tr>
          </thead>
          <tbody>
            {group.items.map((e) => (
              <tr key={`${e.method} ${e.path}`}>
                <td className="ep">
                  <span className={`chip method-${e.method.toLowerCase()}`}>{e.method}</span>
                  <code>{e.path}</code>
                </td>
                <td>{e.summary}</td>
                <td className="dim">{e.params}</td>
                <td className="dim">{e.returns}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function HealthDot() {
  const [state, setState] = useState("checking"); // checking | up | down
  useEffect(() => {
    let live = true;
    getJSON("/health")
      .then(() => live && setState("up"))
      .catch(() => live && setState("down"));
    return () => {
      live = false;
    };
  }, []);
  const label = { checking: "checking…", up: "dispatcher up", down: "dispatcher unreachable" }[state];
  return <span className={`docs-health ${state}`}>{label}</span>;
}

export default function Docs() {
  // the browser resolves #hash before React has rendered the sections, so a
  // cold deep-link (/system-docs#api) lands at the top — scroll it ourselves
  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id) document.getElementById(id)?.scrollIntoView();
  }, []);

  return (
    <div className="app docs-page">
      <header>
        <h1>Mission Control</h1>
        <span className="sub">documentation · dispatcher :8765</span>
        <HealthDot />
        <a className="docs-back" href="/">
          ← dashboard
        </a>
      </header>

      <div className="docs-body">
        <nav className="docs-nav">
          {guide.map((s) => (
            <a key={s.id} href={`#${s.id}`}>
              {s.title}
            </a>
          ))}
          <a href="#api">HTTP API</a>
          {endpoints.map((g) => (
            <a key={g.id} href={`#${g.id}`} className="sub-link">
              {g.group}
            </a>
          ))}
        </nav>

        <main className="docs-main">
          {guide.map((section) => (
            <section key={section.id} id={section.id} className="docs-section">
              <h2>{section.title}</h2>
              {section.blocks.map((block, i) => (
                <Block key={i} block={block} />
              ))}
            </section>
          ))}

          <section id="api" className="docs-section">
            <h2>HTTP API</h2>
            <p>
              Every front-end talks to the dispatcher over these routes. The interactive
              OpenAPI explorer generated from the code lives at <a href="/docs">/docs</a>.
            </p>
            {endpoints.map((group) => (
              <EndpointGroup key={group.id} group={group} />
            ))}
          </section>
        </main>
      </div>
    </div>
  );
}
