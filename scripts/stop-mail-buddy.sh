#!/bin/sh
# Stop Mail-Buddy containers without deleting databases, backups, models, or secrets.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_dir"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is not installed." >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "Docker's engine is unavailable; no containers were stopped." >&2
  exit 1
}
[ -f compose.yaml ] || {
  echo "compose.yaml is missing from $project_dir" >&2
  exit 1
}

if [ -n "$(docker compose ps -q app 2>/dev/null)" ]; then
  echo "Training status before shutdown (no email content is displayed):"
  docker compose exec -T app mail-buddy training-status || \
    echo "Training status is unavailable; stale runs are recovered on the next start."
fi

echo "Stopping Mail-Buddy services gracefully; persistent volumes, models, and secrets will be kept."
# Give an in-flight local classification or companion training operation time to
# finish cleanly. Remote LoRA training is not started or terminated by Compose.
docker compose stop --timeout 45
echo
docker compose ps
echo "Mail-Buddy is stopped. Restart it with ./scripts/start-mail-buddy.sh"
