#!/bin/sh
set -eu

expected="${MAIL_BUDDY_MODEL_MANIFEST_SHA256:-}"
manifest="${MAIL_BUDDY_MODEL_MANIFEST_PATH:-/root/.ollama/models/manifests/registry.ollama.ai/library/llama3.2/3b-instruct-q4_K_M}"

case "$expected" in
  sha256:[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
    ;;
  *)
    echo "The pinned Ollama manifest SHA-256 is invalid." >&2
    exit 2
    ;;
esac

if [ ! -f "$manifest" ]; then
  echo "The pulled Ollama manifest was not found." >&2
  exit 1
fi

actual="sha256:$(sha256sum "$manifest" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
  echo "Ollama model manifest verification failed." >&2
  echo "Expected: $expected" >&2
  echo "Actual:   $actual" >&2
  echo "Review model-manifest.lock.json before accepting a changed model." >&2
  exit 1
fi

echo "Verified pinned Ollama manifest: $actual"
