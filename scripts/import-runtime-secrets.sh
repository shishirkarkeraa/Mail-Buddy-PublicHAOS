#!/bin/sh
set -eu

source_dir="${MAIL_BUDDY_SECRET_SOURCE_DIR:-/run/secrets}"
target_dir="${MAIL_BUDDY_SECRET_TARGET_DIR:-/runtime-secrets}"
runtime_uid="${MAIL_BUDDY_RUNTIME_UID:-10001}"
runtime_gid="${MAIL_BUDDY_RUNTIME_GID:-10001}"

case "$runtime_uid" in
  "" | *[!0-9]*)
    echo "Runtime secret uid must be numeric." >&2
    exit 2
    ;;
esac
case "$runtime_gid" in
  "" | *[!0-9]*)
    echo "Runtime secret gid must be numeric." >&2
    exit 2
    ;;
esac

umask 077
install -d -o "$runtime_uid" -g "$runtime_gid" -m 0700 "$target_dir"

for filename in encryption_key session_secret password_hash google_client_secret; do
  source_file="$source_dir/$filename"
  if [ ! -s "$source_file" ]; then
    echo "Required deployment secret is missing or empty: $source_file" >&2
    exit 1
  fi
done

for filename in encryption_key session_secret password_hash google_client_secret; do
  source_file="$source_dir/$filename"
  target_file="$target_dir/$filename"
  temporary_file="$target_dir/.$filename.$$"
  trap 'rm -f "$temporary_file"' EXIT HUP INT TERM
  install -o "$runtime_uid" -g "$runtime_gid" -m 0400 \
    "$source_file" "$temporary_file"
  mv -f "$temporary_file" "$target_file"
  trap - EXIT HUP INT TERM
done

echo "Imported four deployment secrets into the private runtime volume."
