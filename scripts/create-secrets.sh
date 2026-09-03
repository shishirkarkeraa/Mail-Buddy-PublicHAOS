#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
secret_dir="${1:-$project_dir/secrets}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required. Install Docker Desktop on macOS or Docker Engine on Raspberry Pi." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed, but its daemon is unavailable or this user cannot access it." >&2
  exit 2
fi

mkdir -p "$secret_dir"
secret_dir=$(CDPATH= cd -- "$secret_dir" && pwd -P)
if [ "$(id -u)" -eq 0 ]; then
  chown 0:0 "$secret_dir"
fi
chmod 0700 "$secret_dir"

for filename in encryption_key session_secret password_hash; do
  if [ -e "$secret_dir/$filename" ]; then
    echo "$secret_dir/$filename already exists; refusing to overwrite deployment secrets." >&2
    exit 1
  fi
done

echo "Building the pinned Mail-Buddy application image..."
docker build --tag mail-buddy:0.1.0 "$project_dir"

echo "Create a dashboard password when prompted."
docker run --rm -it \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 64 \
  --memory 512m \
  --cpus 1 \
  --tmpfs /tmp:size=32m,mode=1777,noexec,nosuid,nodev \
  --user "$(id -u):$(id -g)" \
  --volume "$secret_dir:/output" \
  mail-buddy:0.1.0 \
  mail-buddy generate-secrets --output-dir /output

for filename in encryption_key session_secret password_hash; do
  secret_path="$secret_dir/$filename"
  if [ ! -f "$secret_path" ] || [ -L "$secret_path" ] || [ ! -s "$secret_path" ]; then
    echo "Secret generation did not create a non-empty regular file: $secret_path" >&2
    exit 1
  fi
  chmod 0600 "$secret_path"
done

echo "Deployment secrets are ready in $secret_dir."
echo "Keep this directory private, back it up offline, and never commit it."
