#!/usr/bin/env bash
# Download the local voice models (all CPU, all cached under data/models/).
# Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=.venv/bin/python
MODELS=data/models
mkdir -p "$MODELS/piper"

echo "== Silero VAD (ONNX, ~2MB, MIT license) =="
VAD="$MODELS/silero_vad.onnx"
if [ ! -f "$VAD" ]; then
  curl -fsSL -o "$VAD" \
    https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx
fi
ls -la "$VAD"

echo "== Piper voice (en_US-lessac-medium) =="
PIPER_ONNX="$MODELS/piper/en_US-lessac-medium.onnx"
PIPER_JSON="$PIPER_ONNX.json"
# Guard on BOTH files: a prior run that fetched the .onnx but died before the
# .onnx.json would otherwise look "done" and leave Piper permanently broken.
# Fallback curls download to .part then atomically move, so a failed download
# never leaves a half file at the real path (set -e aborts before the mv).
if [ ! -f "$PIPER_ONNX" ] || [ ! -f "$PIPER_JSON" ]; then
  $VENV -m piper.download_voices en_US-lessac-medium --data-dir "$MODELS/piper" 2>/dev/null \
    || { # older piper-tts releases use a different downloader
      BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
      curl -fsSL -o "$PIPER_ONNX.part" "$BASE/en_US-lessac-medium.onnx"
      mv "$PIPER_ONNX.part" "$PIPER_ONNX"
      curl -fsSL -o "$PIPER_JSON.part" "$BASE/en_US-lessac-medium.onnx.json"
      mv "$PIPER_JSON.part" "$PIPER_JSON"
    }
fi
ls -la "$MODELS/piper/"

echo "== openWakeWord (0.4.0 bundles pretrained models — just verifying) =="
$VENV - <<'EOF'
from pathlib import Path
from openwakeword import get_pretrained_model_paths
names = [Path(p).stem for p in get_pretrained_model_paths()]
assert any(n.startswith("hey_jarvis") for n in names), names
print("hey_jarvis model present:", names)
EOF

echo "== faster-whisper small.en (warms the HF cache) =="
$VENV - <<'EOF'
from faster_whisper import WhisperModel
WhisperModel("small.en", device="cpu", compute_type="int8")
print("whisper model cached")
EOF

echo "All voice models ready."
