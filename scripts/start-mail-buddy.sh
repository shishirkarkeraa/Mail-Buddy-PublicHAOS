#!/bin/sh
# Start the complete Mail-Buddy Compose stack without exposing secrets.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_dir"

fail() {
  echo "Mail-Buddy start failed: $*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || \
  fail "Docker is not installed. Install and start Docker Desktop first."
docker info >/dev/null 2>&1 || \
  fail "Docker is installed but its engine is unavailable. Start Docker Desktop and retry."
[ -f compose.yaml ] || fail "compose.yaml is missing from $project_dir"
[ -f .env ] || fail ".env is missing; copy .env.example and configure it first."

secrets_dir=$(awk -F= '
  /^[[:space:]]*#/ { next }
  /^[[:space:]]*MAIL_BUDDY_SECRETS_DIR[[:space:]]*=/ {
    value=$2
    sub(/^[[:space:]]+/, "", value)
    sub(/[[:space:]]+$/, "", value)
    gsub(/^"|"$/, "", value)
    gsub(/^'"'"'|'"'"'$/, "", value)
    print value
    exit
  }
' .env)
secrets_dir=${secrets_dir:-./secrets}

for secret_name in encryption_key session_secret password_hash google_client_secret.json; do
  [ -f "$secrets_dir/$secret_name" ] && [ -s "$secrets_dir/$secret_name" ] || \
    fail "required secret is missing or empty: $secrets_dir/$secret_name"
done

docker compose config --quiet || fail "Compose configuration is invalid."

echo "Starting Mail-Buddy services..."
docker compose up -d --build

echo
docker compose ps
echo
echo "The first run downloads and verifies the local model; this can take several minutes."
echo "Follow startup progress with: docker compose logs -f model-init ollama app caddy"
if [ -n "$(docker compose ps -q app 2>/dev/null)" ]; then
  echo
  echo "Recovering any stale training lock and reporting local training readiness..."
  if docker compose exec -T app mail-buddy recover-stale-training --older-than-hours 12; then
    docker compose exec -T app mail-buddy training-status || \
      echo "Training status will be available once the application is ready."
  else
    echo "Run './scripts/start-mail-buddy.sh' again after the application is ready to recover stale training locks."
  fi
else
  echo "Training status will be available after the application finishes starting."
fi
echo "Companion-model training runs inside the app on its configured schedule."
echo "Laptop LoRA training stays opt-in and remote; use the restricted trainer flow only when ready."
echo "The dashboard is available at: https://$(awk -F= '/^[[:space:]]*MAIL_BUDDY_HOSTNAME[[:space:]]*=/ {print $2; exit}' .env | tr -d '[:space:]')"
