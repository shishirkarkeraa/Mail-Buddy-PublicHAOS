#!/bin/sh
# Run with sudo on a normal Linux Raspberry Pi host (not the HA OS host shell).
set -eu

[ "$(id -u)" -eq 0 ] || {
  echo "Run this installer with sudo." >&2
  exit 2
}
[ "$#" -eq 1 ] || {
  echo "Usage: $0 /path/to/mail_buddy_trainer_ed25519.pub" >&2
  exit 2
}
public_key_file=$1
[ -r "$public_key_file" ] || {
  echo "Public key is not readable: $public_key_file" >&2
  exit 2
}
public_key=$(sed -n '1p' "$public_key_file")
printf '%s\n' "$public_key" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/]+={0,3}( .*)?$' || {
  echo "Only one OpenSSH Ed25519 public key is accepted." >&2
  exit 2
}

trainer_user=mailbuddy-trainer
trainer_home=/var/lib/mailbuddy-trainer
authorized_keys=$trainer_home/.ssh/authorized_keys
forced_command=/opt/mail-buddy/scripts/training-remote.sh
[ -x "$forced_command" ] || {
  echo "Restricted command is not executable: $forced_command" >&2
  exit 2
}
if ! id "$trainer_user" >/dev/null 2>&1; then
  useradd --system --home-dir "$trainer_home" --create-home --shell /bin/sh "$trainer_user"
fi
install -d -m 0700 -o "$trainer_user" -g "$trainer_user" "$trainer_home/.ssh"
install -d -m 0750 -o "$trainer_user" -g "$trainer_user" /opt/mail-buddy/models
line="restrict,command=\"$forced_command\" $public_key"
if [ ! -f "$authorized_keys" ] || ! grep -Fqx "$line" "$authorized_keys"; then
  printf '%s\n' "$line" >> "$authorized_keys"
fi
chown "$trainer_user:$trainer_user" "$authorized_keys"
chmod 0600 "$authorized_keys"

# Docker access is required only for the fixed forced-command wrapper.
if getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$trainer_user"
else
  echo "Docker group is missing; install Docker before configuring the trainer." >&2
  exit 2
fi
echo "Installed a restrict+forced-command trainer key for $trainer_user."
