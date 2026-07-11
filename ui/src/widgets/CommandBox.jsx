import React, { useState } from "react";
import { postTask } from "../api.js";

// The typed equivalent of talking to Jarvis: same endpoint, same routing.
export default function CommandBox() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const [note, setNote] = useState("");

  async function submit(e) {
    e.preventDefault();
    if (!text.trim() || busy) return;
    setBusy(true);
    setAnswer("");
    setNote("");
    try {
      const res = await postTask(text.trim(), {
        onDelta: (d) => setAnswer((a) => a + d),
      });
      if (res.kind === "agentic") {
        setNote(`${res.ack} (task ${res.task_id})`);
      } else if (res.status && res.status !== "done") {
        setNote(`failed: ${res.error ?? "unknown error"}`);
      }
      setText("");
    } catch (err) {
      setNote(`error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <form onSubmit={submit} className="command-form">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='ask anything, or "add a task: …"'
          disabled={busy}
        />
        <button disabled={busy || !text.trim()}>{busy ? "…" : "Send"}</button>
      </form>
      {note && <p className="note">{note}</p>}
      {answer && <p className="answer">{answer}</p>}
    </div>
  );
}
