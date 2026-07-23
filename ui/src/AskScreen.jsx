import React, { useEffect, useRef, useState } from "react";
import { postTask } from "./api.js";

// Ask-about-my-screen popup (chromium --app window at /ask?shot=<id>):
// screenshot preview + typed or mic-recorded question -> streamed answer.
// Follow-ups reuse source "screen:<shot>" so the dispatcher resumes the CLI
// session (image stays in context, resumed turns are ~6x cheaper).
export default function AskScreen() {
  const shot = (() => {
    const s = new URLSearchParams(location.search).get("shot") ?? "";
    return /^[\w-]+$/.test(s) ? s : "";
  })();

  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [exchanges, setExchanges] = useState([]); // [{q, a, err}]
  const [recState, setRecState] = useState("idle"); // idle | recording | transcribing
  const inputRef = useRef(null);
  const recRef = useRef(null);

  useEffect(() => inputRef.current?.focus(), []);

  async function submit() {
    const q = text.trim();
    if (!q || busy || !shot) return;
    setBusy(true);
    setText("");
    setExchanges((xs) => [...xs, { q, a: "", err: null }]);
    const patchLast = (fn) =>
      setExchanges((xs) => xs.map((x, i) => (i === xs.length - 1 ? fn(x) : x)));
    try {
      const res = await postTask(q, {
        source: `screen:${shot}`,
        mode: "quick",
        metadata: { screenshot: `${shot}.png` },
        onDelta: (d) => patchLast((x) => ({ ...x, a: x.a + d })),
      });
      if (res.status && res.status !== "done") {
        patchLast((x) => ({ ...x, err: res.error ?? "failed" }));
      }
    } catch (err) {
      patchLast((x) => ({ ...x, err: err.message }));
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  async function toggleMic() {
    if (recRef.current) {
      recRef.current.stop(); // onstop below does the rest
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        recRef.current = null;
        setRecState("transcribing");
        try {
          const blob = new Blob(chunks, { type: rec.mimeType || "audio/webm" });
          const resp = await fetch("/stt", {
            method: "POST",
            headers: { "content-type": blob.type },
            body: blob,
          });
          if (!resp.ok) throw new Error(`stt ${resp.status}`);
          const { text: heard } = await resp.json();
          if (heard) setText((t) => (t ? `${t} ${heard}` : heard));
        } catch (err) {
          setExchanges((xs) => [...xs, { q: "(mic)", a: "", err: err.message }]);
        } finally {
          setRecState("idle");
          inputRef.current?.focus();
        }
      };
      rec.start();
      recRef.current = rec;
      setRecState("recording");
    } catch (err) {
      setExchanges((xs) => [...xs, { q: "(mic)", a: "", err: err.message }]);
    }
  }

  if (!shot) return <p className="empty">Missing ?shot= parameter.</p>;
  return (
    <div className="ask-page">
      <img className="ask-shot" src={`/screenshots/${shot}.png`} alt="screenshot" />
      <div className="ask-exchanges">
        {exchanges.map((x, i) => (
          <div key={i} className="exchange">
            <p className="q">{x.q}</p>
            {x.a && <p className="a">{x.a}</p>}
            {x.err && <p className="err">{x.err}</p>}
          </div>
        ))}
      </div>
      <div className="ask-input">
        <textarea
          ref={inputRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about the screenshot… (Enter to send, F9 to dictate)"
          rows={2}
          disabled={busy}
        />
        <button
          type="button"
          className={`mic ${recState}`}
          onClick={toggleMic}
          disabled={busy || recState === "transcribing"}
          title={recState === "recording" ? "Stop recording" : "Record a question"}
        >
          {recState === "recording" ? "◼" : recState === "transcribing" ? "…" : "🎤"}
        </button>
        <button type="button" onClick={submit} disabled={busy || !text.trim()}>
          {busy ? "…" : "Ask"}
        </button>
      </div>
    </div>
  );
}
