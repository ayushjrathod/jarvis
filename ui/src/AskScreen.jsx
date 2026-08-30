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
  const [exchanges, setExchanges] = useState([]); // [{id, q, a, err}]
  const [recState, setRecState] = useState("idle"); // idle | recording | transcribing
  // A question is seconds, not minutes: cap a recording at two minutes so a
  // forgotten-open mic can't hold a 25MB transcribe lock against every other
  // /stt call (or the user's quota of patience). Auto-stop uploads whatever
  // was captured through the normal path.
  const MIC_MAX_MS = 120000;
  const recTimerRef = useRef(null);
  const inputRef = useRef(null);
  const recRef = useRef(null);
  const startingRef = useRef(false); // set before the getUserMedia await
  const nextId = useRef(0); // stable exchange ids so streamed deltas patch the
                            // right row even if another exchange is appended

  useEffect(() => inputRef.current?.focus(), []);

  async function submit() {
    const q = text.trim();
    if (!q || busy || !shot) return;
    setBusy(true);
    setText("");
    const id = nextId.current++;
    setExchanges((xs) => [...xs, { id, q, a: "", err: null }]);
    const patch = (fn) =>
      setExchanges((xs) => xs.map((x) => (x.id === id ? fn(x) : x)));
    try {
      const res = await postTask(q, {
        source: `screen:${shot}`,
        mode: "quick",
        metadata: { screenshot: `${shot}.png` },
        onDelta: (d) => patch((x) => ({ ...x, a: x.a + d })),
      });
      if (res.degraded) {
        // rate-limited: the streamed text is a real extractive answer from the
        // local index, not a failure — label it instead of replacing it with
        // the raw CLI limit string (2026-08-08)
        patch((x) => ({ ...x, note: "from local memory — Claude unavailable" }));
      } else if (res.status && res.status !== "done") {
        patch((x) => ({ ...x, err: res.speech || res.error || "failed" }));
      }
    } catch (err) {
      patch((x) => ({ ...x, err: err.message }));
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
      clearTimeout(recTimerRef.current);
      recRef.current.stop(); // onstop below does the rest
      return;
    }
    // Synchronous guard: recRef is only set after the await, so a double-tap
    // started two recorders and orphaned the first with its mic tracks still
    // live (2026-08-08). Same bug, same shape, as CommandBox.
    if (startingRef.current) return;
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const chunks = [];
      rec.ondataavailable = (e) => chunks.push(e.data);
      rec.onstop = async () => {
        clearTimeout(recTimerRef.current);
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
          setExchanges((xs) => [...xs, { id: nextId.current++, q: "(mic)", a: "", err: err.message }]);
        } finally {
          setRecState("idle");
          inputRef.current?.focus();
        }
      };
      rec.start();
      recRef.current = rec;
      recTimerRef.current = setTimeout(() => {
        if (recRef.current) recRef.current.stop();
      }, MIC_MAX_MS);
      setRecState("recording");
    } catch (err) {
      setExchanges((xs) => [...xs, { id: nextId.current++, q: "(mic)", a: "", err: err.message }]);
    } finally {
      startingRef.current = false;
    }
  }

  if (!shot) return <p className="empty">Missing ?shot= parameter.</p>;
  return (
    <div className="ask-page">
      <img className="ask-shot" src={`/screenshots/${shot}.png`} alt="screenshot" />
      <div className="ask-exchanges">
        {exchanges.map((x) => (
          <div key={x.id} className="exchange">
            <p className="q">{x.q}</p>
            {x.a && <p className="a">{x.a}</p>}
            {x.note && <p className="note">{x.note}</p>}
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
