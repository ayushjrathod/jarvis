import React, { useRef, useState } from "react";
import { postTask } from "../api.js";

const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;

// The typed — or spoken — equivalent of talking to Jarvis: same endpoint, same
// routing. The mic button is "same mode as hey jarvis": click to start
// listening, click again to stop, then it transcribes (POST /stt), asks Jarvis
// (mode auto, like the voice path), streams the answer, and speaks it aloud
// (Web Speech API — no server TTS). Voice tasks use source "ui-voice" so
// follow-ups resume the same CLI session, kept apart from the on-device "voice".
export default function CommandBox() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [answer, setAnswer] = useState("");
  const [note, setNote] = useState("");
  const [question, setQuestion] = useState(""); // last spoken question, shown back
  const [recState, setRecState] = useState("idle"); // idle | recording | transcribing
  const [muted, setMuted] = useState(false);
  const recRef = useRef(null);
  // set SYNCHRONOUSLY, before the getUserMedia await — see toggleMic
  const startingRef = useRef(false);
  // `say` is captured by rec.onstop, which closes over the render that STARTED
  // the recording. Reading `muted` from that closure meant ticking "mute reply"
  // while recording was ignored and the answer was spoken anyway (2026-08-08).
  const mutedRef = useRef(false);
  mutedRef.current = muted;

  // Core dispatch, shared by typed submit and voice. `speak` voices the reply.
  async function runTask(q, { speak = false, source = "ui" } = {}) {
    if (!q || busy) return;
    setBusy(true);
    setAnswer("");
    setNote("");
    if (canSpeak) window.speechSynthesis.cancel(); // stop any prior utterance
    try {
      const res = await postTask(q, {
        source,
        onDelta: (d) => setAnswer((a) => a + d),
      });
      if (res.kind === "agentic") {
        const ack = `${res.ack} (task ${res.task_id})`;
        setNote(ack);
        if (speak) say(ack);
      } else if (res.degraded) {
        // The plan cap tripped and the dispatcher answered extractively from
        // the local index. The run really did fail, so status is "failed" — but
        // the text streamed above is a real answer, and until 2026-08-08 this
        // branch threw it away and rendered (and SPOKE) the raw CLI limit
        // string sitting in res.error instead, with the answer visible right
        // underneath. `degraded` is documented as existing so a client can
        // label where the text came from; nothing in ui/src read it.
        setNote("answered from local memory — Claude is unavailable right now");
        if (speak && res.text) say(res.text);
      } else if (res.status && res.status !== "done") {
        // res.speech is the sentence the server built for a human; res.error is
        // the machine string. Prefer the former when there is one.
        const err = res.speech || `failed: ${res.error ?? "unknown error"}`;
        setNote(err);
        if (speak) say(err);
      } else if (speak && res.text) {
        say(res.text);
      }
      setText("");
    } catch (err) {
      const msg = `error: ${err.message}`;
      setNote(msg);
      if (speak) say(msg);
    } finally {
      setBusy(false);
    }
  }

  function say(t) {
    if (!canSpeak || mutedRef.current || !t) return;
    try {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(t));
    } catch {
      /* best-effort; iOS Safari can be picky about async speech */
    }
  }

  function submit(e) {
    e.preventDefault();
    if (!text.trim()) return;
    setQuestion("");
    runTask(text.trim());
  }

  // Click to start listening, click again to stop; onstop transcribes + asks.
  async function toggleMic() {
    if (recRef.current) {
      recRef.current.stop(); // onstop below does the rest
      return;
    }
    // Guard BEFORE the await (2026-08-08). recRef is only assigned after
    // getUserMedia resolves, and the button stays enabled through that window
    // (recState is still "idle") — trivially double-tappable on a phone, where
    // there is no feedback at all until the device opens. Two recorders then
    // started; the second overwrote recRef, so stopping stopped only that one
    // and the FIRST kept recording with its tracks never stopped — the browser
    // recording indicator stays lit and the mic stays open until reload.
    if (startingRef.current) return;
    startingRef.current = true;
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
          setRecState("idle");
          if (heard) {
            setQuestion(heard);
            runTask(heard, { speak: true, source: "ui-voice" });
          } else {
            setNote("Didn't catch that.");
          }
        } catch (err) {
          setRecState("idle");
          setNote(`mic error: ${err.message}`);
        }
      };
      rec.start();
      recRef.current = rec;
      setRecState("recording");
    } catch (err) {
      setNote(`mic error: ${err.message}`);
    } finally {
      startingRef.current = false;
    }
  }

  const micLabel =
    recState === "recording" ? "◼ listening…" : recState === "transcribing" ? "…" : "🎤";

  return (
    <div>
      <form onSubmit={submit} className="command-form">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder='ask anything, or "add a task: …"'
          disabled={busy}
        />
        <button
          type="button"
          className={`mic ${recState}`}
          onClick={toggleMic}
          disabled={busy || recState === "transcribing"}
          title={recState === "recording" ? "Stop listening" : "Talk to Jarvis"}
        >
          {micLabel}
        </button>
        <button disabled={busy || !text.trim()}>{busy ? "…" : "Send"}</button>
      </form>
      <div className="command-tools">
        {canSpeak && (
          <label className="mute-toggle">
            <input
              type="checkbox"
              checked={muted}
              onChange={(e) => setMuted(e.target.checked)}
            />
            mute reply
          </label>
        )}
      </div>
      {question && <p className="asked">you: {question}</p>}
      {note && <p className="note">{note}</p>}
      {answer && <p className="answer">{answer}</p>}
    </div>
  );
}
