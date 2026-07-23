#!/usr/bin/env bash
# Ask-screen acceptance (needs the dispatcher running). Real-audio STT check
# rides the smoke_phase_b trick: Piper synthesizes a phrase, /stt must hear it.
set -uo pipefail
cd "$(dirname "$0")/.."
BASE="${1:-http://127.0.0.1:8765}"
pass=0; fail=0
ok()  { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

# 1. /ask serves the SPA
curl -sf "$BASE/ask" | grep -qi "<!doctype html" \
  && ok "/ask serves HTML" || bad "/ask serves HTML"

# 2. screenshot round-trip
mkdir -p data/screenshots
.venv/bin/python - <<'EOF'
import struct, zlib
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
idat = zlib.compress(b"\x00\xff\x00\x00")
png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
       + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
open("data/screenshots/smoke-ask.png", "wb").write(png)
EOF
curl -sf "$BASE/screenshots/smoke-ask.png" -o /tmp/smoke-ask-back.png \
  && cmp -s data/screenshots/smoke-ask.png /tmp/smoke-ask-back.png \
  && ok "PNG round-trip" || bad "PNG round-trip"

# 3. traversal guard
code=$(curl --path-as-is -s -o /dev/null -w "%{http_code}" "$BASE/screenshots/../config.yaml")
[ "$code" = "404" ] && ok "traversal -> 404" || bad "traversal -> got $code"

# 4. /stt transcribes real (synthesized) speech
.venv/bin/python - <<'EOF'
import wave
from jarvis import engines
from jarvis.config import JarvisConfig
import numpy as np
cfg = JarvisConfig.load()
tts = engines.create("tts", "piper", voice=cfg.tts.get("voice"),
                     models_dir=cfg.models_dir / "piper")
tts.load()
audio = np.concatenate(list(tts.synthesize("the quick brown fox jumps over the lazy dog")))
with wave.open("/tmp/smoke-ask-stt.wav", "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(tts.sample_rate)
    w.writeframes(audio.tobytes())
EOF
curl -sf -X POST --data-binary @/tmp/smoke-ask-stt.wav \
     -H "content-type: audio/wav" "$BASE/stt" | grep -qi "quick brown fox" \
  && ok "/stt hears synthesized speech" || bad "/stt hears synthesized speech"

# 5. real screenshot quick task (one LLM call; asserts the SSE completes)
resp=$(curl -sN -X POST "$BASE/task" -H 'content-type: application/json' \
  --max-time 120 -d '{"text": "What single solid color fills this image? One word.",
    "source": "screen:smoke-ask", "mode": "quick",
    "metadata": {"screenshot": "smoke-ask.png"}}')
echo "$resp" | grep -q 'event: done' \
  && ok "screenshot quick task completed" || bad "screenshot quick task completed"
echo "$resp" | grep -qi "red" \
  && ok "answer saw the red pixel" || echo "note: answer did not name 'red' (model wording varies)"

rm -f data/screenshots/smoke-ask.png /tmp/smoke-ask-back.png /tmp/smoke-ask-stt.wav
echo "----------------------------------------"
echo "ask-screen smoke: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
