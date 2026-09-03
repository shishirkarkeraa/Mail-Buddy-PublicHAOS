#!/bin/sh
# Restricted SSH command for the macOS fine-tuning agent.
set -eu

project_dir=${MAIL_BUDDY_PROJECT_DIR:-/opt/mail-buddy}
models_dir=$project_dir/models
original=${SSH_ORIGINAL_COMMAND:-${*:-}}

fail() {
  echo "Mail-Buddy training command rejected: $*" >&2
  exit 2
}

valid_name() {
  printf '%s\n' "$1" | grep -Eq '^mail-buddy-llama:[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}$'
}

valid_file() {
  printf '%s\n' "$1" | grep -Eq '^mail-buddy-llama-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}\.(gguf|modelfile|json)$'
}

cd "$project_dir"
set -f
set -- $original
command_name=${1:-}

case "$command_name" in
  status)
    [ "$#" -eq 1 ] || fail "status takes no arguments"
    exec docker compose exec -T app mail-buddy training-status
    ;;
  export-due)
    [ "$#" -eq 1 ] || fail "export-due takes no arguments"
    exec docker compose exec -T app mail-buddy training-export --if-due --output -
    ;;
  export-now)
    [ "$#" -eq 1 ] || fail "export-now takes no arguments"
    exec docker compose exec -T app mail-buddy training-export --output -
    ;;
  register)
    [ "$#" -eq 2 ] || fail "register requires one metadata filename"
    valid_file "$2" || fail "invalid metadata filename"
    exec docker compose exec -T app mail-buddy register-main-model --metadata "/models/$2"
    ;;
  mark-laptop)
    [ "$#" -eq 3 ] || fail "mark-laptop requires name and checksum"
    valid_name "$2" || fail "invalid model name"
    printf '%s\n' "$3" | grep -Eq '^[a-f0-9]{64}$' || fail "invalid checksum"
    exec docker compose exec -T app mail-buddy mark-main-model-installed \
      --name "$2" --host laptop --sha256 "$3"
    ;;
  install-pi)
    [ "$#" -eq 5 ] || fail "install-pi requires name, GGUF, Modelfile, and checksum"
    valid_name "$2" || fail "invalid model name"
    valid_file "$3" || fail "invalid GGUF filename"
    valid_file "$4" || fail "invalid Modelfile filename"
    printf '%s\n' "$5" | grep -Eq '^[a-f0-9]{64}$' || fail "invalid checksum"
    [ -f "$models_dir/$3" ] && [ -f "$models_dir/$4" ] || fail "candidate files missing"
    actual=$(sha256sum "$models_dir/$3" | awk '{print $1}')
    [ "$actual" = "$5" ] || fail "candidate checksum mismatch"
    docker compose exec -T ollama ollama create "$2" -f "/models/$4"
    docker compose exec -T ollama ollama show "$2" >/dev/null
    docker compose exec -T app mail-buddy canary-main-model --name "$2"
    exec docker compose exec -T app mail-buddy mark-main-model-installed \
      --name "$2" --host pi --artifact "/models/$3"
    ;;
  promote)
    [ "$#" -eq 2 ] || fail "promote requires one model name"
    valid_name "$2" || fail "invalid model name"
    exec docker compose exec -T app mail-buddy promote-main-model --name "$2"
    ;;
  fail)
    [ "$#" -eq 2 ] || fail "fail requires a safe reason code"
    printf '%s\n' "$2" | grep -Eq '^[a-z0-9_]{1,64}$' || fail "invalid reason"
    exec docker compose exec -T app mail-buddy fail-training --reason "$2"
    ;;
  rollback)
    [ "$#" -eq 1 ] || fail "rollback takes no arguments"
    exec docker compose exec -T app mail-buddy rollback-main-model
    ;;
  scp)
    [ "$original" = "scp -t /opt/mail-buddy/models/" ] || \
      [ "$original" = "scp -t /opt/mail-buddy/models" ] || \
      fail "scp is restricted to the model inbox"
    exec scp -t "$models_dir"
    ;;
  *)
    fail "unknown command"
    ;;
esac
