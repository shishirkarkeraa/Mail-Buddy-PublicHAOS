#!/bin/sh
set -eu

options_file=/data/options.json
secrets_dir=/data/secrets
model_dir=/data/ollama/models
backup_dir=/data/backups

option() {
    jq -er --arg name "$1" '.[$name] // empty' "$options_file"
}

mkdir -p "$secrets_dir" "$model_dir" "$backup_dir"
chmod 0700 "$secrets_dir" "$model_dir" "$backup_dir"

password_hash="$secrets_dir/password_hash"
if [ ! -s "$secrets_dir/encryption_key" ] || [ ! -s "$secrets_dir/session_secret" ] || [ ! -s "$password_hash" ]; then
    dashboard_password="$(option dashboard_password || true)"
    if [ -z "$dashboard_password" ]; then
        echo "Set Dashboard password before starting Mail-Buddy for the first time." >&2
        exit 1
    fi
    printf '%s\n' "$dashboard_password" | mail-buddy generate-secrets --password-stdin --output-dir "$secrets_dir"
    unset dashboard_password
fi

if [ "$(option reset_dashboard_password || true)" = "true" ]; then
    dashboard_password="$(option dashboard_password || true)"
    if [ -z "$dashboard_password" ]; then
        echo "Dashboard password is required when reset dashboard password is enabled." >&2
        exit 1
    fi
    printf '%s\n' "$dashboard_password" | mail-buddy hash-password --password-stdin > "$password_hash"
    chmod 0600 "$password_hash"
    unset dashboard_password
fi

client_secret="$(option google_client_secret_json || true)"
if [ -n "$client_secret" ]; then
    if ! printf '%s' "$client_secret" | jq -e '.installed.client_id and .installed.client_secret' >/dev/null; then
        echo "Google client secret must be valid Desktop OAuth JSON with installed.client_id and installed.client_secret." >&2
        exit 1
    fi
    printf '%s\n' "$client_secret" > "$secrets_dir/google_client_secret.json"
    chmod 0600 "$secrets_dir/google_client_secret.json"
fi
unset client_secret

if [ ! -s "$secrets_dir/google_client_secret.json" ]; then
    echo "Set Google client secret JSON before starting Mail-Buddy." >&2
    exit 1
fi

export MAIL_BUDDY_DATA_DIR=/data
export MAIL_BUDDY_BACKUP_DIR="$backup_dir"
export MAIL_BUDDY_OLLAMA_URL=http://127.0.0.1:11434
export MAIL_BUDDY_ENCRYPTION_KEY_FILE="$secrets_dir/encryption_key"
export MAIL_BUDDY_SESSION_SECRET_FILE="$secrets_dir/session_secret"
export MAIL_BUDDY_PASSWORD_HASH_FILE="$password_hash"
export MAIL_BUDDY_GOOGLE_CLIENT_SECRET_PATH="$secrets_dir/google_client_secret.json"
export MAIL_BUDDY_OLLAMA_MODEL="$(option ollama_model)"
export MAIL_BUDDY_POLL_INTERVAL_SECONDS="$(option poll_interval_seconds)"
export MAIL_BUDDY_TRAINING_INTERVAL_DAYS="$(option training_interval_days)"
export MAIL_BUDDY_TRAINING_HOUR_LOCAL="$(option training_hour_local)"
export MAIL_BUDDY_COLLEGE_DOMAINS="$(option college_domains || true)"
# Home Assistant Ingress can be opened on a LAN or Tailscale HTTP address.
# A Secure cookie is discarded by browsers on those addresses, which makes a
# successful login redirect straight back to the blank login form.
export MAIL_BUDDY_SECURE_COOKIES=false
export MAIL_BUDDY_ENVIRONMENT=production
export TZ="$(option timezone)"

ollama serve >/proc/1/fd/1 2>/proc/1/fd/2 &
ollama_pid=$!
cleanup() {
    kill "$ollama_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

attempt=0
until curl --fail --silent --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "Timed out waiting for local Ollama." >&2
        exit 1
    fi
    sleep 1
done

echo "Ensuring local model $MAIL_BUDDY_OLLAMA_MODEL is available; first startup can take several minutes."
ollama pull "$MAIL_BUDDY_OLLAMA_MODEL"
if [ "$MAIL_BUDDY_OLLAMA_MODEL" = "llama3.2:3b-instruct-q4_K_M" ]; then
    export MAIL_BUDDY_MODEL_MANIFEST_SHA256=sha256:a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72
    export MAIL_BUDDY_MODEL_MANIFEST_PATH="$model_dir/manifests/registry.ollama.ai/library/llama3.2/3b-instruct-q4_K_M"
    verify-model-manifest
fi

mail-buddy recover-stale-training --older-than-hours 12 || true
if [ "$(option oauth_authorize || true)" = "true" ]; then
    echo "OAuth mode is active. Open the URL below through an SSH tunnel, then set OAuth authorize to false and restart this add-on."
    mail-buddy auth --bind 0.0.0.0 --redirect-host 127.0.0.1 --port 8765
    exit 0
fi

exec mail-buddy serve --host 0.0.0.0 --port 8099 --forwarded-allow-ips 172.30.32.2
