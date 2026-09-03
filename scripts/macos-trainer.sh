#!/bin/sh
# Hourly, fail-closed MLX QLoRA trainer for a 16 GB Apple Silicon laptop.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
config_file=${MAIL_BUDDY_TRAINER_CONFIG:-$HOME/.config/mail-buddy/trainer.env}
[ -r "$config_file" ] || {
  echo "Missing trainer config: $config_file" >&2
  exit 2
}
# The operator-owned file contains simple KEY=value entries and must be mode 0600.
# shellcheck disable=SC1090
. "$config_file"

: "${MAIL_BUDDY_PI_SSH_HOST:?Set MAIL_BUDDY_PI_SSH_HOST}"
: "${MAIL_BUDDY_PI_SSH_KEY:?Set MAIL_BUDDY_PI_SSH_KEY}"
: "${MAIL_BUDDY_LLAMA_CPP_DIR:?Set MAIL_BUDDY_LLAMA_CPP_DIR}"
: "${MAIL_BUDDY_LLAMA_CPP_COMMIT:?Set MAIL_BUDDY_LLAMA_CPP_COMMIT}"
: "${MAIL_BUDDY_LLAMA_QUANTIZE_SHA256:?Set MAIL_BUDDY_LLAMA_QUANTIZE_SHA256}"

mlx_model=${MAIL_BUDDY_MLX_MODEL:-mlx-community/Llama-3.2-3B-Instruct-4bit}
ollama_url=${MAIL_BUDDY_LAPTOP_OLLAMA_URL:-http://127.0.0.1:11434}
state_dir=${MAIL_BUDDY_TRAINER_STATE_DIR:-$HOME/Library/Application Support/Mail-Buddy Trainer}
minimum_kib=${MAIL_BUDDY_TRAINER_MIN_FREE_KIB:-20971520}
quantize_bin=$MAIL_BUDDY_LLAMA_CPP_DIR/build/bin/llama-quantize
ssh_args="-i $MAIL_BUDDY_PI_SSH_KEY -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10"

umask 077
mkdir -p "$state_dir"
chmod 700 "$state_dir"
lock_dir=$state_dir/run.lock
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "A Mail-Buddy training job is already running."
  exit 0
fi

work_dir=
run_started=0
completed=0
cleanup() {
  result=$?
  if [ "$run_started" -eq 1 ] && [ "$completed" -ne 1 ]; then
    # The forced-command key accepts only this fixed safe reason code.
    # shellcheck disable=SC2086
    ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" fail trainer_failed >/dev/null 2>&1 || true
  fi
  if [ -n "$work_dir" ] && [ -d "$work_dir" ]; then
    find "$work_dir" -type f -exec sh -c 'for file do dd if=/dev/zero of="$file" bs=4096 count=1 conv=notrunc 2>/dev/null || true; done' sh {} +
    find "$work_dir" -depth -delete
  fi
  rmdir "$lock_dir" 2>/dev/null || true
  exit "$result"
}
trap cleanup EXIT HUP INT TERM

for command_name in python3 ssh scp git shasum ollama mlx_lm.lora mlx_lm.fuse; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is unavailable: $command_name" >&2
    exit 2
  }
done
[ -x "$quantize_bin" ] || {
  echo "Pinned llama.cpp quantizer is missing: $quantize_bin" >&2
  exit 2
}
[ "$(git -C "$MAIL_BUDDY_LLAMA_CPP_DIR" rev-parse HEAD)" = "$MAIL_BUDDY_LLAMA_CPP_COMMIT" ] || {
  echo "llama.cpp checkout does not match the configured commit" >&2
  exit 2
}
[ "$(shasum -a 256 "$quantize_bin" | awk '{print $1}')" = "$MAIL_BUDDY_LLAMA_QUANTIZE_SHA256" ] || {
  echo "llama-quantize checksum mismatch" >&2
  exit 2
}
pmset -g batt | grep -q "AC Power" || {
  echo "Training skipped: laptop is not connected to AC power."
  exit 0
}
available_kib=$(df -Pk "$state_dir" | awk 'NR == 2 {print $4}')
[ "$available_kib" -ge "$minimum_kib" ] || {
  echo "Training skipped: less than the configured free disk threshold."
  exit 0
}

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/mail-buddy-training.XXXXXX")
chmod 700 "$work_dir"
status_file=$work_dir/status.json
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" status > "$status_file"
set +e
python3 - "$status_file" <<'PY'
import json, sys
status = json.load(open(sys.argv[1], encoding="utf-8"))
if not status.get("enabled") or not status.get("ready") or not status.get("lora_due"):
    raise SystemExit(3)
PY
status_result=$?
set -e
if [ "$status_result" -eq 3 ]; then
  echo "Training skipped: the Pi reports that LoRA is not ready or not due."
  exit 0
fi
[ "$status_result" -eq 0 ] || exit "$status_result"

bundle_file=$work_dir/bundle.json
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" export-due > "$bundle_file"
run_started=1
dataset_dir=$work_dir/dataset
adapter_dir=$work_dir/adapters
python3 "$script_dir/training-bundle.py" prepare \
  --bundle "$bundle_file" --output "$dataset_dir" \
  --model "$mlx_model" --adapter-path "$adapter_dir"

mlx_lm.lora --config "$dataset_dir/lora-config.yaml"
fused_dir=$work_dir/fused
mlx_lm.fuse --model "$mlx_model" --adapter-path "$adapter_dir" \
  --save-path "$fused_dir" --dequantize --export-gguf \
  --gguf-path mail-buddy-f16.gguf
f16_gguf=$fused_dir/mail-buddy-f16.gguf
quantized_gguf=$work_dir/candidate.gguf
"$quantize_bin" "$f16_gguf" "$quantized_gguf" Q4_K_M
artifact_sha=$(shasum -a 256 "$quantized_gguf" | awk '{print $1}')
stamp=$(date -u +%Y%m%dT%H%M%SZ)
model_name=mail-buddy-llama:$stamp-$(printf '%s' "$artifact_sha" | cut -c1-6)
file_stem=mail-buddy-llama-$stamp-$(printf '%s' "$artifact_sha" | cut -c1-6)

local_modelfile=$work_dir/LocalModelfile
printf 'FROM %s\nPARAMETER temperature 0\nPARAMETER num_ctx 4096\n' "$quantized_gguf" > "$local_modelfile"
ollama create "$model_name" -f "$local_modelfile"
ollama show "$model_name" >/dev/null

production_model=$(python3 - "$status_file" <<'PY'
import json, sys
status = json.load(open(sys.argv[1], encoding="utf-8"))
active = status.get("active_main") or {}
print(active.get("name") or "llama3.2:3b-instruct-q4_K_M")
PY
)
candidate_metrics=$work_dir/candidate-metrics.json
production_metrics=$work_dir/production-metrics.json
python3 "$script_dir/evaluate-ollama-model.py" --url "$ollama_url" \
  --model "$model_name" --dataset "$dataset_dir/test.jsonl" \
  --manifest "$dataset_dir/manifest.json" --output "$candidate_metrics"
python3 "$script_dir/evaluate-ollama-model.py" --url "$ollama_url" \
  --model "$production_model" --dataset "$dataset_dir/test.jsonl" \
  --manifest "$dataset_dir/manifest.json" --output "$production_metrics"

"$project_dir/.venv/bin/pytest" -q \
  "$project_dir/tests/test_classification.py" \
  "$project_dir/tests/test_fine_tuning.py" \
  "$project_dir/tests/test_operational_scripts.py"

transfer_gguf=$work_dir/$file_stem.gguf
transfer_modelfile=$work_dir/$file_stem.modelfile
transfer_metadata=$work_dir/$file_stem.json
cp "$quantized_gguf" "$transfer_gguf"
printf 'FROM /models/%s.gguf\nPARAMETER temperature 0\nPARAMETER num_ctx 4096\n' "$file_stem" > "$transfer_modelfile"
python3 "$script_dir/training-bundle.py" metadata \
  --manifest "$dataset_dir/manifest.json" \
  --candidate-metrics "$candidate_metrics" \
  --production-metrics "$production_metrics" \
  --name "$model_name" --sha256 "$artifact_sha" \
  --output "$transfer_metadata" --application-tests-passed

# scp triggers the Pi account's restricted scp-only forced command.
# shellcheck disable=SC2086
scp $ssh_args "$transfer_gguf" "$transfer_modelfile" "$transfer_metadata" \
  "$MAIL_BUDDY_PI_SSH_HOST:/opt/mail-buddy/models/"
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" register "$file_stem.json"
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" mark-laptop "$model_name" "$artifact_sha"
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" install-pi "$model_name" \
  "$file_stem.gguf" "$file_stem.modelfile" "$artifact_sha"
# shellcheck disable=SC2086
ssh $ssh_args "$MAIL_BUDDY_PI_SSH_HOST" promote "$model_name"
completed=1
echo "Promoted $model_name on the laptop and Pi."
