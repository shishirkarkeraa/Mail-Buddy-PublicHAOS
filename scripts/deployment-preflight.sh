#!/bin/sh
set -eu

project_dir="${1:-$(pwd)}"
firewall_env="${MAIL_BUDDY_FIREWALL_ENV_FILE:-/etc/mail-buddy/firewall.env}"

fail() {
  echo "Mail-Buddy deployment preflight failed: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 \
    || fail "required command '$1' is not installed"
}

read_env_value() {
  env_file="$1"
  env_key="$2"
  value="$(
    awk -v key="$env_key" '
      /^[[:space:]]*#/ { next }
      {
        line = $0
        sub(/\r$/, "", line)
        separator = index(line, "=")
        if (separator == 0) {
          next
        }
        name = substr(line, 1, separator - 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
        if (name == key) {
          result = substr(line, separator + 1)
        }
      }
      END {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", result)
        print result
      }
    ' "$env_file"
  )"
  case "$value" in
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
  esac
  printf '%s' "$value"
}

check_secret() {
  secret_path="$1"
  secret_name="$2"

  [ -f "$secret_path" ] || fail "missing $secret_name at $secret_path"
  [ ! -L "$secret_path" ] || fail "$secret_name must not be a symbolic link"
  [ -s "$secret_path" ] || fail "$secret_name is empty"

  owner="$(stat -c '%u' "$secret_path")"
  group="$(stat -c '%g' "$secret_path")"
  mode="$(stat -c '%a' "$secret_path")"
  [ "$owner" = "0" ] \
    || fail "$secret_name must be owned by root (found UID $owner)"
  [ "$group" = "0" ] \
    || fail "$secret_name must be owned by group root (found GID $group)"
  [ "$mode" = "600" ] \
    || fail "$secret_name must have mode 0600 (found $mode)"
}

[ -d "$project_dir" ] || fail "project directory does not exist: $project_dir"
cd "$project_dir"
[ -f compose.yaml ] || fail "compose.yaml is missing from $project_dir"
[ -f .env ] || fail "copy .env.example to .env and configure the Pi first"
[ -r "$firewall_env" ] \
  || fail "firewall configuration is missing or unreadable: $firewall_env"

require_command awk
require_command docker
require_command ip
require_command python3
require_command stat

docker compose version >/dev/null 2>&1 \
  || fail "the Docker Compose plugin is unavailable"

bind_address="$(read_env_value .env MAIL_BUDDY_BIND_ADDRESS)"
[ -n "$bind_address" ] || fail "MAIL_BUDDY_BIND_ADDRESS is not set in .env"
case "$bind_address" in
  0.0.0.0 | 127.* | localhost | :: | ::1)
    fail "MAIL_BUDDY_BIND_ADDRESS must be the Pi's fixed LAN IPv4 address"
    ;;
esac

lan_subnet="$(read_env_value "$firewall_env" MAIL_BUDDY_LAN_SUBNET)"
lan_interface="$(read_env_value "$firewall_env" MAIL_BUDDY_LAN_INTERFACE)"
[ -n "$lan_subnet" ] \
  || fail "MAIL_BUDDY_LAN_SUBNET is not set in $firewall_env"
[ -n "$lan_interface" ] \
  || fail "MAIL_BUDDY_LAN_INTERFACE is not set in $firewall_env"

if ! python3 -c '
import ipaddress
import sys

address = ipaddress.IPv4Address(sys.argv[1])
network = ipaddress.IPv4Network(sys.argv[2], strict=True)
if address not in network:
    raise SystemExit(1)
' "$bind_address" "$lan_subnet" >/dev/null 2>&1; then
  fail "$bind_address is not a valid host address inside $lan_subnet"
fi

if ! ip link show dev "$lan_interface" >/dev/null 2>&1; then
  fail "configured LAN interface '$lan_interface' does not exist"
fi
if ! ip -4 -o addr show dev "$lan_interface" | awk \
  -v expected="$bind_address" '
    {
      address = $4
      sub(/\/.*/, "", address)
      if (address == expected) {
        found = 1
      }
    }
    END { exit found ? 0 : 1 }
  '; then
  fail "$bind_address is not assigned to $lan_interface"
fi

secrets_dir="$(read_env_value .env MAIL_BUDDY_SECRETS_DIR)"
[ -n "$secrets_dir" ] || secrets_dir="./secrets"
case "$secrets_dir" in
  /*) ;;
  *) secrets_dir="$project_dir/${secrets_dir#./}" ;;
esac
[ -d "$secrets_dir" ] || fail "secrets directory does not exist: $secrets_dir"

secret_dir_owner="$(stat -c '%u' "$secrets_dir")"
secret_dir_mode="$(stat -c '%a' "$secrets_dir")"
[ "$secret_dir_owner" = "0" ] \
  || fail "the secrets directory must be owned by root"
[ "$secret_dir_mode" = "700" ] \
  || fail "the secrets directory must have mode 0700 (found $secret_dir_mode)"

check_secret "$secrets_dir/encryption_key" "encryption_key"
check_secret "$secrets_dir/session_secret" "session_secret"
check_secret "$secrets_dir/password_hash" "password_hash"
check_secret \
  "$secrets_dir/google_client_secret.json" \
  "google_client_secret.json"

if ! python3 -c '
import base64
import json
import sys
from pathlib import Path

secret_dir = Path(sys.argv[1])
encryption_key = (secret_dir / "encryption_key").read_text().strip()
decoded_key = base64.b64decode(encryption_key, altchars=b"-_", validate=True)
if len(decoded_key) != 32:
    raise ValueError("invalid encryption key")
if len((secret_dir / "session_secret").read_text().strip()) < 48:
    raise ValueError("invalid session secret")
if not (secret_dir / "password_hash").read_text().strip().startswith("$argon2id$"):
    raise ValueError("invalid password hash")
oauth = json.loads((secret_dir / "google_client_secret.json").read_text())
installed = oauth.get("installed")
if not isinstance(installed, dict):
    raise ValueError("OAuth client is not a Desktop app")
for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
    if not isinstance(installed.get(key), str) or not installed[key]:
        raise ValueError("incomplete OAuth client")
' "$secrets_dir" >/dev/null 2>&1; then
  fail "secret contents are invalid; recreate them from the documented sources"
fi

docker compose config --quiet \
  || fail "docker compose config validation failed"

available_kib="$(df -Pk "$project_dir" | awk 'NR == 2 { print $4 }')"
if [ -n "$available_kib" ] && [ "$available_kib" -lt 10485760 ]; then
  echo "Warning: less than 10 GiB is free on the project filesystem." >&2
fi

echo "Mail-Buddy deployment preflight passed."
echo "HTTPS bind: $bind_address on $lan_interface; trusted LAN: $lan_subnet"
