#!/usr/bin/env bash
# One-time (idempotent) bootstrap for the /demo pipeline.
# Installs native tools (ffmpeg, whisper.cpp), a dedicated Python venv with kokoro-onnx,
# and downloads the Kokoro + Whisper model files. Safe to re-run.
set -euo pipefail

CACHE="$HOME/.cache/demo-skill"
VENV="$CACHE/venv"
MODELS="$CACHE/models"
# kokoro-onnx needs a Python with onnxruntime wheels. Override with DEMO_PYTHON.
PY="${DEMO_PYTHON:-/opt/homebrew/bin/python3.14}"

echo "==> demo-skill setup"
mkdir -p "$MODELS"

# 1. Native binaries via Homebrew.
if ! command -v brew >/dev/null 2>&1; then
  echo "error: Homebrew required (https://brew.sh)"; exit 1
fi
for tool in ffmpeg whisper-cpp; do
  if ! brew list "$tool" >/dev/null 2>&1; then
    echo "==> brew install $tool"
    brew install "$tool"
  fi
done

# 2. Dedicated Python venv (isolated from any project venv) + TTS deps.
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating venv at $VENV ($PY)"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
echo "==> installing kokoro-onnx + soundfile + pillow"
"$VENV/bin/pip" install --quiet kokoro-onnx soundfile pillow

# 3. Model files (skip if already present).
download() {  # url dest
  if [ -f "$2" ]; then echo "==> have $(basename "$2")"; return; fi
  echo "==> downloading $(basename "$2")"
  curl -sSL -o "$2" "$1"
}
KOKORO_REL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
download "$KOKORO_REL/kokoro-v1.0.onnx" "$MODELS/kokoro-v1.0.onnx"
download "$KOKORO_REL/voices-v1.0.bin" "$MODELS/voices-v1.0.bin"
download "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin" \
  "$MODELS/ggml-base.en.bin"

echo "==> setup complete. venv: $VENV"
echo "    run the pipeline with: $VENV/bin/python <skill>/pipeline/demo.py ..."
