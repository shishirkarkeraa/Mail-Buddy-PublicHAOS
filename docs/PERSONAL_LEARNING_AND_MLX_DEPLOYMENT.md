# Personal Learning and MLX Deployment

## Outcome and trust boundary

Mail-Buddy learns only from labels explicitly confirmed by the destination
mailbox owner. It never treats an unanswered `Needs Review` prediction as
truth. The Pi-local companion remains active beside whichever Llama instance
is serving inference; after the dataset is large enough, a Mac may also build a
versioned Llama 3.2 3B QLoRA release.

The application persists an encrypted, redacted, bounded representation:
sender fingerprint, sender domain, subject, Gmail snippet, old prediction,
confirmed direct label, feedback source, split assignment, dataset version,
and UTC timestamps. It does not persist complete bodies or attachment text.
Disconnecting Gmail purges examples, jobs, model registries, and encrypted
companion artifacts from the application database. Quantized GGUF files live
outside that database; remove `/opt/mail-buddy/models/mail-buddy-llama-*` and
the matching Ollama tags separately when decommissioning the host.

## Readiness and promotion gates

| Stage | Ready when | Promotion gate |
|---|---|---|
| Pi companion | 10 confirmed examples, two categories | At least 5 sender-grouped held-out predictions, accuracy at least 65%, no macro-F1 regression |
| Mac QLoRA | 200 confirmed examples; included categories have at least 10 examples; sender-isolated train/validation/test splits exist | Accuracy at least 70% and no worse than production; macro-F1 and sensitive recall do not regress; runtime safety tests pass; identical checksum/tag installed on both hosts |

The deterministic split seed and MLX seed are 42. QLoRA uses batch size 1,
gradient accumulation 8, 8 trainable layers, rank 8, scale 20, 1024-token
maximum sequences, prompt masking, gradient checkpointing, and about five
dataset passes.

## Dashboard workflow

Open **Personal learning** in the authenticated dashboard.

1. Select automatic training and a frequency of 1, 3, 7, 14, or 30 days.
2. Select the local start hour. `TZ=Asia/Kolkata` is the default; stored
   timestamps remain UTC.
3. Correct `Needs Review` mail or answer an Accuracy Question. The answer
   replaces only Mail-Buddy's application label, preserves unrelated Gmail
   labels and the applicable Inbox rule, and stores the old prediction.
4. Review category counts, both readiness states, last/next run, phase,
   accuracy, macro-F1, failures, active tags, and rejected versions.
5. Use **Train and evaluate now** for the companion. QLoRA is started by the
   hourly Mac agent once the Pi says it is ready and due.
6. Use **Rollback** to atomically restore the previous fine-tuned tag. The
   registry retains the prior two promoted versions.

## Prepare the Mac trainer

Requirements: Apple Silicon macOS, at least 20 GiB free disk space, AC power,
native Ollama, Python 3, a project virtual environment, MLX-LM with training
extras, and a locally built `llama.cpp` checkout.

```bash
cd /path/to/Mail-Buddy
python3 -m venv .mlx-venv
.mlx-venv/bin/pip install "mlx-lm[train]"
ollama pull llama3.2:3b-instruct-q4_K_M
```

Build `llama.cpp` according to its upstream instructions. Record immutable
identifiers rather than a branch name:

```bash
git -C /path/to/llama.cpp rev-parse HEAD
shasum -a 256 /path/to/llama.cpp/build/bin/llama-quantize
```

The trainer refuses to run when either value differs. The MLX base defaults to
`mlx-community/Llama-3.2-3B-Instruct-4bit`; pin a reviewed local snapshot if
you require fully reproducible/offline downloads.

## Create the restricted Pi SSH identity

On the Mac:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mail_buddy_trainer_ed25519 \
  -C mail-buddy-trainer
scp ~/.ssh/mail_buddy_trainer_ed25519.pub pi@PI_PRIVATE_IP:/tmp/
```

On a normal Raspberry Pi OS Mail-Buddy host:

```bash
cd /opt/mail-buddy
sudo ./scripts/install-pi-trainer-key.sh \
  /tmp/mail_buddy_trainer_ed25519.pub
```

The resulting authorized-key entry uses OpenSSH `restrict` plus a forced
command. It exposes only status, due/locked export, candidate registration,
checksum-bound host installation, promotion, rollback, failure reporting, and
SCP into `/opt/mail-buddy/models`. Do not reuse this key for an interactive
shell. The forced-command account belongs to the Docker group, which is
privileged; the forced command and private network are therefore mandatory
security controls.

Copy and edit the Mac configuration:

```bash
mkdir -p ~/.config/mail-buddy
cp scripts/mail-buddy-trainer.env.example \
  ~/.config/mail-buddy/trainer.env
chmod 600 ~/.config/mail-buddy/trainer.env
```

Use only the Pi's private LAN/Tailscale address. The private-key path must not
contain spaces because the POSIX trainer builds fixed SSH options.

## Install and test launchd

The launchd installer adds this checkout's `.mlx-venv/bin` to the agent's
restricted PATH. Also run the normal development setup so `.venv/bin/pytest`
exists for the runtime safety gate. Then:

```bash
./scripts/install-macos-trainer.sh
launchctl print gui/$(id -u)/com.mail-buddy.trainer
tail -f "$HOME/Library/Logs/Mail-Buddy/trainer.log"
```

The agent checks hourly and runs at login, so a sleeping Mac catches up after
waking. The Pi—not the laptop—decides whether the local-time schedule,
automatic-training setting, readiness threshold, and database lock allow an
export. A mode-0700 temporary directory contains plaintext JSONL only during
the run; cleanup overwrites the first block where possible and deletes it.
APFS/SSD wear levelling prevents a guarantee that overwriting destroys every
physical copy, so keep FileVault enabled.

## Candidate lifecycle

The automated path performs these fail-closed steps:

1. Fetch Pi status and acquire a unique LoRA job lock through `export-due`.
2. Export only encrypted-at-rest fields after decryption/redaction on the Pi.
3. Write mode-0600 MLX JSONL files with the production prompt and schema.
4. Train QLoRA, fuse/dequantize, export FP16 GGUF, and quantize once to Q4_K_M.
5. Evaluate candidate and production tags end-to-end through Ollama.
6. Run classification, split, and operational safety tests.
7. Create a tag `mail-buddy-llama:UTCSTAMP-CHECKSUMPREFIX`.
8. Install and verify it locally, transfer checksum-bound files, install on the
   Pi, and record both installation flags.
9. Atomically promote only after every gate passes. Any error records a safe
   reason and leaves the active production tag unchanged.

Useful Pi-side diagnostics:

```bash
docker compose exec -T app mail-buddy training-status
docker compose exec -T app mail-buddy recover-stale-training \
  --older-than-hours 12
docker compose exec -T app mail-buddy rollback-main-model
docker compose logs --since=1h app ollama
```

Manual redacted export (this acquires the same lock):

```bash
docker compose exec -T app mail-buddy training-export --output - \
  > private-training-bundle.json
chmod 600 private-training-bundle.json
```

Call `mail-buddy fail-training --reason manual_export_complete` or recover the
stale lock after examining a manual bundle. Delete plaintext exports promptly.

## Failure and rollback checks

- Laptop asleep/offline: no export occurs; the hourly agent catches up later.
- Training/evaluation failure: current model stays active; failure is visible.
- Transfer interruption: the incomplete candidate is never promoted.
- Pi import failure: laptop copy may exist but registry activation is blocked.
- Different checksum: installation marking fails.
- Only one host installed: promotion fails.
- Both inference hosts unavailable: ambiguous mail stays in Inbox with
  `Needs Review`.
- Bad promoted behavior: type `ROLLBACK` in the dashboard; no retraining is
  required.

## Privacy-policy disclosure text

Adapt and publish this text before enabling learning for other users:

> When personal learning is enabled, Mail-Buddy stores encrypted, redacted,
> bounded excerpts derived from sender domain, subject, and Gmail snippet,
> together with labels explicitly confirmed by the mailbox owner. Complete
> message bodies and attachment text are not retained for training. An optional
> user-controlled Apple Silicon laptop may receive only the redacted training
> bundle to fine-tune and evaluate a local model. Disconnecting Gmail deletes
> these examples, job records, and application model metadata. Locally exported
> model files must be removed separately when decommissioning a device.

## Home Assistant OS boundary

Do not run this Compose stack, the restricted SSH account, or the MLX trainer
inside Home Assistant OS. Mail-Buddy itself can run there only through the
Supervisor-managed custom add-on; its lightweight encrypted companion learner
is available, but Apple Silicon MLX training remains a remote-Mac workflow.
See `docs/HOME_ASSISTANT_OS_ON_PI5.md` for the add-on deployment and security
boundary.

## Upstream references

- [MLX-LM LoRA and QLoRA](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md)
- [Ollama model import](https://github.com/ollama/ollama/blob/main/docs/import.mdx)
