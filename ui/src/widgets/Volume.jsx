import React, { useState } from "react";
import { postJSON } from "../api.js";

// System volume control (computer-use T1). The desktop tier was previously
// reachable only by typing or speaking a command; this is its first UI
// surface: step, mute and unmute over POST /desktop, with the executor's own
// sentence as the readout — no state is parsed out of prose, so the widget
// can never disagree with what the tier actually did.
const COMMANDS = [
  { label: "−", command: "system volume down", title: "Volume down 10%" },
  { label: "+", command: "system volume up", title: "Volume up 10%" },
  { label: "mute", command: "mute the system volume", title: "Mute" },
  { label: "unmute", command: "unmute the system volume", title: "Unmute" },
];

export default function Volume() {
  const [line, setLine] = useState(null);
  const [busy, setBusy] = useState(false);

  async function run(command) {
    setBusy(true);
    try {
      const out = await postJSON("/desktop", { command, source: "ui" });
      setLine(out.speech || out.status);
    } catch (e) {
      setLine(`Couldn't do that: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="volume">
      <div className="volume-buttons">
        {COMMANDS.map((c) => (
          <button key={c.label} title={c.title} disabled={busy}
                  onClick={() => run(c.command)}>
            {c.label}
          </button>
        ))}
      </div>
      {line && <p className="meta">{line}</p>}
    </div>
  );
}
