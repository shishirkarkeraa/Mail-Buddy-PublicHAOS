#!/bin/sh
set -eu

backup_name="${1:-}"
project_dir="${MAIL_BUDDY_PROJECT_DIR:-$(pwd)}"
lock_dir="${TMPDIR:-/tmp}/mail-buddy-restore.lock"
umask 077

usage() {
  echo "Usage: $0 mail_buddy-YYYYMMDDTHHMMSSZ.sqlite3" >&2
  exit 2
}

[ -n "$backup_name" ] || usage
if ! printf '%s\n' "$backup_name" \
  | grep -Eq '^mail_buddy-[0-9]{8}T[0-9]{6}Z\.sqlite3$'; then
  echo "Refusing an invalid backup filename: $backup_name" >&2
  usage
fi
[ -d "$project_dir" ] || {
  echo "Project directory does not exist: $project_dir" >&2
  exit 1
}
cd "$project_dir"
[ -f compose.yaml ] || {
  echo "compose.yaml is missing from $project_dir" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "docker is required." >&2
  exit 1
}
docker compose config --quiet

if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "Another Mail-Buddy restore appears to be running: $lock_dir" >&2
  exit 1
fi

app_was_running=0
app_stopped=0
restore_committed=0

cleanup() {
  status=$?
  trap - 0 INT TERM
  rmdir "$lock_dir" 2>/dev/null || true
  if [ "$status" -ne 0 ] \
    && [ "$app_was_running" -eq 1 ] \
    && [ "$app_stopped" -eq 1 ] \
    && [ "$restore_committed" -eq 0 ]; then
    echo "Restore failed before commit; restarting the unchanged app." >&2
    docker compose start app >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup 0
trap 'exit 130' INT
trap 'exit 143' TERM

# Copy the selected backup to the data volume before creating the safety backup.
# The normal seven-file retention pass therefore cannot prune the restore input.
docker compose run --rm --no-deps -T \
  -e "MAIL_BUDDY_RESTORE_FILE=$backup_name" \
  --entrypoint /bin/sh app -c '
    set -eu
    source_path="/backups/$MAIL_BUDDY_RESTORE_FILE"
    staged_path="/data/.mail_buddy.restore.sqlite3"
    [ -f "$source_path" ] || {
      echo "Requested backup does not exist: $source_path" >&2
      exit 1
    }
    cp "$source_path" "$staged_path"
    chmod 0600 "$staged_path"
    python -c "
import sqlite3
import sys

connection = sqlite3.connect(f\"file:{sys.argv[1]}?mode=ro\", uri=True)
try:
    result = [row[0] for row in connection.execute(\"PRAGMA quick_check\")]
finally:
    connection.close()
if result != [\"ok\"]:
    raise SystemExit(\"Backup failed SQLite integrity validation\")
" "$staged_path"
    rm -f \
      "$staged_path-wal" \
      "$staged_path-shm" \
      "$staged_path-journal"
    sync "$staged_path"
  '

if [ -n "$(docker compose ps -q app 2>/dev/null)" ]; then
  app_was_running=1
fi
docker compose stop app
app_stopped=1

echo "Creating a safety backup of the current database before restore..."
docker compose run --rm --no-deps -T app mail-buddy backup

# The app is stopped and the replacement is already durable in the same Docker
# volume. Remove SQLite sidecars from the old database before an atomic rename,
# so an old WAL can never be replayed into the restored database.
docker compose run --rm --no-deps -T \
  --entrypoint /bin/sh app -c '
    set -eu
    staged_path="/data/.mail_buddy.restore.sqlite3"
    destination="/data/mail_buddy.sqlite3"
    [ -f "$staged_path" ] || {
      echo "Staged restore database disappeared." >&2
      exit 1
    }
    rm -f \
      "$destination-wal" \
      "$destination-shm" \
      "$destination-journal" \
      "$staged_path-wal" \
      "$staged_path-shm" \
      "$staged_path-journal"
    mv -f "$staged_path" "$destination"
    chmod 0600 "$destination"
    sync "$destination"
  '
restore_committed=1

docker compose up -d app
app_stopped=0
echo "Restored $backup_name and restarted Mail-Buddy."
