#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
agent_dir=$HOME/Library/LaunchAgents
log_dir=$HOME/Library/Logs/Mail-Buddy
target=$agent_dir/com.mail-buddy.trainer.plist
config=$HOME/.config/mail-buddy/trainer.env
mlx_bin=$script_dir/../.mlx-venv/bin

[ -f "$config" ] || {
  echo "Create $config from scripts/mail-buddy-trainer.env.example first." >&2
  exit 2
}
[ "$(stat -f '%Lp' "$config")" = "600" ] || {
  echo "$config must have mode 0600." >&2
  exit 2
}
[ -x "$mlx_bin/mlx_lm.lora" ] && [ -x "$mlx_bin/mlx_lm.fuse" ] || {
  echo "Create .mlx-venv and install mlx-lm[train] before installing launchd." >&2
  exit 2
}
mkdir -p "$agent_dir" "$log_dir"
sed \
  -e "s|__TRAINER_SCRIPT__|$script_dir/macos-trainer.sh|g" \
  -e "s|__LOG_DIR__|$log_dir|g" \
  -e "s|__MLX_BIN__|$mlx_bin|g" \
  "$script_dir/com.mail-buddy.trainer.plist.template" > "$target"
plutil -lint "$target"
launchctl bootout "gui/$(id -u)" "$target" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$target"
echo "Installed hourly trainer: $target"
