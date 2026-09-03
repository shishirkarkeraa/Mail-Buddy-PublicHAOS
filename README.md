# Mail-Buddy

Mail-Buddy is a self-hosted, single-user Gmail organizer for macOS, Raspberry
Pi 5, and Home Assistant OS. It classifies English email with
`llama3.2:3b-instruct-q4_K_M` running in local Ollama, stages historical
decisions for review, and applies Gmail labels only when the relevant category
is approved.

No OAuth client JSON, dashboard password, Gmail tokens, database, learned
models, or runtime data are committed here. Copy [`.env.example`](.env.example)
to `.env` for Docker deployments; Home Assistant OS settings are entered in the
app configuration screen.

There is no hosted database, Redis, hosted model, analytics service, CDN, or
telemetry. SQLite lives in a Docker volume on the machine running Mail-Buddy.
Full email bodies and extracted attachment text are processed transiently and
are not stored. When personal learning is enabled, bounded redacted
sender-domain, subject, and snippet features from owner-labeled examples are
encrypted in SQLite.

> Mail-Buddy can reorganize a real mailbox. First use a dedicated Gmail test
> account, inspect category samples, test undo, and only then authorize your
> main account.

## What v1 does

- Polls Gmail history every two minutes and gives new mail priority over backfill.
- Excludes Sent, Drafts, Spam, and Trash.
- Applies exactly one direct category label per processed message.
- Removes `INBOX` whenever it applies a Mail-Buddy label, including Needs Review.
- Moves general and shopping promotions to Gmail Trash; each move remains reversible
  through the existing batch undo window.
- Stages the historical mailbox without changing Gmail and shows up to 25
  representative messages per category.
- Shows the signed-in owner each message's sender, subject, and full readable
  content directly from Gmail; message content is not written to SQLite.
- Uses deterministic corrections before model classification.
- Trains a lightweight mailbox-specific model from Needs Review corrections
  and dashboard accuracy answers, then uses it alongside Llama.
- Runs held-out evaluation for every personalized candidate and promotes only
  a candidate that meets the accuracy gate and is not worse than the active model.
- Extracts bounded text from PDF, TXT, CSV, and Markdown attachments without OCR.
- Preserves unread state, unrelated Gmail labels, message contents, and existing
  archived/inbox state during exact batch undo.

## Consolidating multiple mailboxes

Configure forwarding or redirection at each source mailbox, but connect
Mail-Buddy only to the one destination Gmail account. New redirected messages
arrive there like any other received mail and are sorted into the destination
account's direct category labels. When a provider wraps a forwarded message,
Mail-Buddy uses the bounded original-message content as classification evidence
instead of relying only on the forwarding wrapper. Mail that is uncertain,
suspicious, or conflicts with the taxonomy is filed under Needs Review.

The taxonomy covers security (OTP, password reset, account alert), banking
transactions, general and shopping promotions, college mail (important,
internships, placements, notices), subscriptions, career/job mail, internships,
shopping order updates, social mail, direct personal mail, and Other. Uncertain,
conflicting, or suspicious messages use `Needs Review`.

## Architecture

```text
Home-LAN browser
      │ HTTPS (Caddy local CA, password + CSRF)
      ▼
   Caddy ─── FastAPI dashboard + Gmail worker ─── Gmail API
                    │                 │
                    │                 └── transient email/attachment content
                    ▼
             Local SQLite         Local Ollama
             decisions +          Llama 3.2 3B
             encrypted learner         ▲
                    │                  │ advisory hint + independent decision
                    └── personal model ┘
```

The Compose project contains:

- `app`: Python 3.12, FastAPI, server-rendered UI, worker queue, and SQLite WAL.
- `ollama`: local Ollama with one loaded model and one parallel request.
- `model-init`: downloads the model and rejects it unless its registry manifest
  matches [model-manifest.lock.json](model-manifest.lock.json).
- `caddy`: the only LAN-published service, serving internal-CA HTTPS on port 443.
- `auth`: an opt-in profile for the one-time loopback Gmail OAuth callback.
- `secret-init`: copies private source secrets into a network-disabled,
  read-only runtime volume with portable container ownership.

The app and Ollama have no published host ports. Caddy cannot reach Ollama.

## Personalized learning ensemble

Open **Personal learning** in the authenticated dashboard. Correcting an item
in **Needs Review** records the correct direct label as encrypted training
ground truth. The dashboard also samples existing classified messages and asks
you to confirm or correct their labels; each answer both fixes Gmail and becomes
an accuracy/training example. Unanswered Needs Review predictions are never
treated as truth.

The learner is a dependency-free multinomial text classifier designed to train
quickly on Raspberry Pi. For ambiguous mail, it runs together with the main
Llama model: its confidence-scored result is included as an advisory hint in
Llama's trusted context. If confident models agree, the decision records the
ensemble agreement. If they disagree, the message is filed under
`Needs Review`. Deterministic security, authentication, and prompt-injection
guards still take precedence.

In **Personal learning**, choose whether automatic training is enabled, a
1, 3, 7, 14, or 30 day interval, and the start hour in the configured local
timezone, or run it manually. Every companion candidate
is scored with deterministic five-fold held-out testing. By default it needs at
least 10 owner-labeled examples, 5 evaluated predictions, 65% accuracy, and an
accuracy no lower than the active model before promotion. Model artifacts and
examples are encrypted by the existing Mail-Buddy encryption key and are
removed when Gmail is disconnected.

The optional second-stage Apple Silicon trainer becomes eligible at 200
owner-confirmed examples. It performs checksum-pinned MLX QLoRA training and
dual-host Ollama installation without replacing the active model on failure.
See [Personal Learning and MLX Deployment](docs/PERSONAL_LEARNING_AND_MLX_DEPLOYMENT.md).

## Start on macOS, Raspberry Pi OS, or Home Assistant OS

Use [START_HERE.txt](START_HERE.txt) as the canonical setup guide. It contains
the exact macOS and Raspberry Pi commands, every `.env` setting, Google Cloud
and Gmail OAuth setup, local HTTPS trust, verification, backups, and
troubleshooting.

The same Compose project runs on:

- Apple Silicon or Intel macOS through Docker Desktop. The dashboard is
  loopback-only by default at `https://localhost`; Ollama inference inside
  Docker Desktop is CPU-based and does not promise Apple Metal acceleration.
- A 64-bit Raspberry Pi OS installation on Raspberry Pi 5. The dashboard can
  be made available on the trusted home LAN after the included firewall setup.

The pinned Python, Ollama, and Caddy image indexes contain both Linux ARM64 and
AMD64 variants. Compose selects the host architecture automatically; do not add
an emulated `platform` override.

For Home Assistant OS, install the included Supervisor-managed custom add-on
instead of this Compose stack. It runs the dashboard through Home Assistant
Ingress and stores Mail-Buddy state in the add-on data volume. Follow
[Home Assistant OS deployment](docs/HOME_ASSISTANT_OS_ON_PI5.md).

## Raspberry Pi installation

### 1. Prepare the Pi

Use a Raspberry Pi 5 with 8 GB RAM, active cooling, 64-bit Raspberry Pi OS,
and at least 10 GB free storage.

```bash
uname -m
getconf LONG_BIT
df -h /
```

Expected architecture is `aarch64` or `arm64`, and the bit width is `64`.

Install Docker Engine and the Compose plugin from Docker's official Raspberry
Pi/Debian repository. The current authoritative instructions are:

- <https://docs.docker.com/engine/install/raspberry-pi-os/>
- <https://docs.docker.com/compose/install/linux/>

After configuring that repository:

```bash
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin avahi-daemon
sudo systemctl enable --now docker avahi-daemon
sudo usermod -aG docker "$USER"
newgrp docker
docker compose version
docker run --rm hello-world
```

Set a stable hostname and reserve a stable IP for the Pi in your router:

```bash
sudo hostnamectl set-hostname mail-buddy
hostname -I
```

`mail-buddy.local` should resolve through mDNS on the home LAN. If the router
provides local DNS, a router-managed name is more reliable; set that name as
`MAIL_BUDDY_HOSTNAME`.

Place the project at `/opt/mail-buddy`:

```bash
sudo mkdir -p /opt/mail-buddy
sudo chown "$USER":"$USER" /opt/mail-buddy
cd /opt/mail-buddy
# Copy or clone this repository here.
cp .env.example .env
```

Edit `.env`. At minimum, set the hostname and the Pi's fixed LAN address:

```dotenv
MAIL_BUDDY_HOSTNAME=mail-buddy.local
MAIL_BUDDY_BIND_ADDRESS=192.168.1.50
MAIL_BUDDY_SECRETS_DIR=./secrets
MAIL_BUDDY_COLLEGE_DOMAINS=college.example.edu,placements.college.example.edu
```

Do not place passwords, OAuth credentials, or encryption keys in `.env`.

### 2. Create root-owned source secrets

Generate the source secrets with the same pinned application image used at
runtime:

```bash
cd /opt/mail-buddy
sudo ./scripts/create-secrets.sh
```

The command prompts for a dashboard password of at least 12 characters and
creates:

- `encryption_key`: encrypts OAuth tokens and correction-rule values.
- `session_secret`: signs dashboard cookies.
- `password_hash`: an Argon2id password verifier; the password is not stored.

Keep offline recovery copies. Rotating the encryption key without decrypting
and re-encrypting existing data makes stored OAuth/rule ciphertext unreadable.
The source directory remains root-owned mode `0700` with mode `0600` files.
Before app startup, the network-disabled `secret-init` service creates
container-owned mode `0400` runtime copies in a private named volume. The app
mounts that runtime volume read-only.

### 3. Configure Google OAuth

In a Google Cloud project:

1. Enable the Gmail API.
2. Open **Google Auth platform** and configure Branding.
3. For a personal Gmail account, use an External audience, add your own account,
   and move publishing status to **In production**. A project left in Testing
   can issue refresh tokens that expire after seven days.
4. Add only
   `https://www.googleapis.com/auth/gmail.modify` under Data Access.
5. Create an OAuth client with application type **Desktop app**.
6. Download its JSON file.

Google's current desktop-client walkthrough is at
<https://developers.google.com/workspace/gmail/api/quickstart/python>. The
`gmail.modify` scope is documented at
<https://developers.google.com/workspace/gmail/api/auth/scopes>.

Copy the downloaded file to the Pi without printing it:

```bash
scp ~/Downloads/client_secret_*.json pi@192.168.1.50:/tmp/google-client.json
ssh pi@192.168.1.50
sudo install -o root -g root -m 0600 /tmp/google-client.json /opt/mail-buddy/secrets/google_client_secret.json
rm /tmp/google-client.json
cd /opt/mail-buddy
docker compose build app
```

Run OAuth through an SSH loopback tunnel. From a computer with a browser:

```bash
ssh -L 8765:127.0.0.1:8765 pi@192.168.1.50
```

In that SSH session:

```bash
cd /opt/mail-buddy
docker compose --profile auth run --rm --service-ports auth
```

Open the printed Google URL on the computer running SSH. Google's callback to
`http://localhost:8765` travels through the encrypted tunnel to the Pi. Accept
the unverified-app warning only for the Cloud project you created. Mail-Buddy
stores the resulting token encrypted in local SQLite and creates its Gmail
labels.

Do not expose port 8765 on the router or add it to Compose.

### 4. Limit HTTPS to the home LAN

Install the firewall before the first Compose start. Docker-published ports can
bypass simple UFW rules. The included script installs an idempotent rule in
Docker's `DOCKER-USER` chain. Replace the subnet and interface with values from
`ip route`:

```bash
ip route
cd /opt/mail-buddy
sudo ./scripts/configure-firewall.sh 192.168.1.0/24 eth0
```

Use `wlan0` for Wi-Fi. This rule restricts Docker containers published on
TCP/443 on that interface, so review it first if this Pi hosts other HTTPS
containers. Never configure router port forwarding for Mail-Buddy.

To reapply the rule after boot:

```bash
sudo install -d -m 0755 /etc/mail-buddy
printf '%s\n' 'MAIL_BUDDY_LAN_SUBNET=192.168.1.0/24' 'MAIL_BUDDY_LAN_INTERFACE=eth0' \
  | sudo tee /etc/mail-buddy/firewall.env >/dev/null
sudo chmod 0600 /etc/mail-buddy/firewall.env
sudo install -m 0644 deploy/mail-buddy-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-buddy-firewall.service
```

Verify from one LAN device and one non-LAN network. The LAN device should reach
TCP/443; the non-LAN device must not.

### 5. Start Mail-Buddy

Confirm `.env` binds TCP/443 to the Pi's fixed LAN IPv4 address, not
`0.0.0.0`. The Compose fallback is loopback-only and fails closed if this value
is forgotten.

```bash
cd /opt/mail-buddy
sudo ./scripts/deployment-preflight.sh /opt/mail-buddy
docker compose up --build -d
docker compose ps
docker compose logs --tail=100 app ollama caddy
```

For routine starts and stops, use the project scripts instead. They check
Docker, `.env`, and the four private secret files before starting, and never
delete persistent volumes when stopping:

```bash
./scripts/start-mail-buddy.sh
./scripts/stop-mail-buddy.sh
```

The first run downloads the Llama weights and verifies the locked manifest. It
can take several minutes. Healthy services should show `app`, `ollama`, and
`caddy` running; `model-init` exits successfully.

### Optional laptop-GPU primary with Pi fallback

Mail-Buddy can prefer a trusted laptop Ollama endpoint and automatically retry
the Pi's local Ollama when the laptop is unavailable. Set a literal private or
Tailscale IP in `.env`:

```dotenv
MAIL_BUDDY_OLLAMA_PRIMARY_URL=http://192.168.1.25:11434
```

Public IPs, hostnames, URL credentials, and arbitrary paths are rejected. Both
hosts must run `llama3.2:3b-instruct-q4_K_M`. See
[the laptop GPU and Pi fallback guide](docs/LAPTOP_GPU_PI_FAILOVER.md) for
firewall, privacy, setup, and failover-test instructions.

### 6. Trust Caddy's local CA

Caddy uses an internal CA because `.local` certificates cannot be publicly
issued. Copy its public root certificate:

```bash
cd /opt/mail-buddy
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./mail-buddy-root.crt
```

Transfer only `mail-buddy-root.crt` to each trusted LAN device. This is a public
certificate, not a secret. On macOS:

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain mail-buddy-root.crt
```

On Debian/Ubuntu:

```bash
sudo install -m 0644 mail-buddy-root.crt /usr/local/share/ca-certificates/mail-buddy.crt
sudo update-ca-certificates
```

Windows and mobile devices can import the certificate into their trusted root
store. Firefox may use its own certificate store. Caddy explains this behavior
at <https://caddyserver.com/docs/caddyfile/directives/tls#internal>.

Then visit:

```text
https://mail-buddy.local/
```

Do not click through a certificate warning. Fix hostname resolution or install
the CA correctly.

## First-run workflow

1. Sign in to the LAN dashboard.
2. Confirm Gmail and the local model are healthy.
3. Start Historical scan under **Backfill**.
4. Gmail remains unchanged while decisions are staged.
5. Expand a category to view its clearly formatted subject and body. Email
   content comes directly from Gmail and is not persisted.
6. Approve only after the samples look correct. The approval is tied to taxonomy
   version 1 and the pinned model.
7. Correct uncertain messages in **Needs review**. Choose message only, future
   similar messages, or all mail from that sender.
8. Use **Activity → Undo batch** to verify exact restoration with the test
   account.

Historical messages that were already archived receive a category label but are
not returned to the inbox.

## Automatic startup and daily backups

On macOS, enable Docker Desktop's start-at-login option; the long-running
containers use `restart: unless-stopped`. Run manual backups with the command
below or schedule it with a local tool of your choice.

On Raspberry Pi OS, install the included systemd units:

```bash
cd /opt/mail-buddy
sudo install -m 0644 deploy/mail-buddy.service /etc/systemd/system/
sudo install -m 0644 deploy/mail-buddy-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/mail-buddy-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mail-buddy.service mail-buddy-backup.timer
systemctl list-timers mail-buddy-backup.timer
```

The timer runs SQLite's online backup API daily and retains seven local backup
files in the `mail_buddy_backups` Docker volume. A backup on the same storage
device does not protect against device failure; periodically copy an encrypted
backup to offline media.

Create a manual backup:

```bash
docker compose exec -T app mail-buddy backup
docker run --rm -v mail-buddy_mail_buddy_backups:/backups:ro \
  alpine:3.22.0@sha256:8a1f59ffb675680d47db6337b49d22281a139e9d709335b492be023728e11715 \
  ls -lh /backups
```

The restore helper validates the selected backup, stages it in the local data
volume, stops the app, creates a safety backup, removes stale SQLite sidecars,
atomically installs the selected database, and restarts the app. Replace the
example filename deliberately:

```bash
./scripts/restore-backup.sh mail_buddy-YYYYMMDDTHHMMSSZ.sqlite3
```

## Day-to-day operations

```bash
# Status
docker compose ps

# Redacted service logs (Caddy and Uvicorn access logs are disabled)
docker compose logs --tail=200 app ollama caddy

# Restart
docker compose restart app

# Verify health locally on the Pi
docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"

# Pause without deleting containers or volumes
docker compose stop

# Resume
docker compose start
```

Disconnect from the dashboard by typing `DISCONNECT`, or:

```bash
docker compose exec -T app mail-buddy disconnect --yes
```

Disconnect revokes the token where possible and purges local account metadata,
rules, decisions, events, and audit records. It leaves Gmail messages and labels
unchanged.

## Updates

Dependency versions are locked in `requirements.lock`; Python direct
dependencies and container images are pinned to exact versions and
multi-architecture image digests. The model tag is additionally locked to an
exact registry-manifest SHA-256; a changed upstream tag makes `model-init` fail
closed. Review release notes, licenses, image manifests, and every layer in
`model-manifest.lock.json` before changing any pin.

```bash
cd /opt/mail-buddy
docker compose build --pull
docker compose up -d
docker compose ps
```

Back up before upgrades. Do not use floating `latest` image tags in production.

## Development and tests

Python 3.12 is required:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -r requirements-dev.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Run a worker-disabled local dashboard only with explicit demo secrets/settings:

```bash
export MAIL_BUDDY_DATA_DIR=/tmp/mail-buddy-dev/data
export MAIL_BUDDY_BACKUP_DIR=/tmp/mail-buddy-dev/backups
export MAIL_BUDDY_DEMO_MODE=true
export MAIL_BUDDY_DISABLE_WORKER=true
export MAIL_BUDDY_SECURE_COOKIES=false
mkdir -p /tmp/mail-buddy-dev/secrets
.venv/bin/python -m mail_buddy generate-secrets \
  --output-dir /tmp/mail-buddy-dev/secrets
export MAIL_BUDDY_ENCRYPTION_KEY_FILE=/tmp/mail-buddy-dev/secrets/encryption_key
export MAIL_BUDDY_SESSION_SECRET_FILE=/tmp/mail-buddy-dev/secrets/session_secret
export MAIL_BUDDY_PASSWORD_HASH_FILE=/tmp/mail-buddy-dev/secrets/password_hash
.venv/bin/python -m mail_buddy serve --host 127.0.0.1 --port 8000
```

The secret generator prompts without putting the password in shell history.

## SBOM and licenses

The repository includes a generated runtime
[SPDX SBOM](sbom/python-runtime.spdx.json) and
[third-party license report](THIRD_PARTY_REPORT.md). Refresh the dependency
license report after installing the pinned development tools:

```bash
make licenses
```

The SBOM command always refreshes the Python runtime SPDX inventory. Build the
container and install the free Syft CLI (or Docker's SBOM plugin) to add source
and container inventories:

```bash
docker compose build app
make sbom
```

Artifacts are written under `sbom/`. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The project source is MIT licensed, but the Llama model is **not** covered by
the MIT license; it uses the Meta Llama Community License.

## Troubleshooting

**Dashboard says Gmail disconnected**

Run the OAuth profile again through the SSH tunnel. Verify the downloaded client
is a Desktop client and the JSON file exists at
`secrets/google_client_secret.json`.

**OAuth callback cannot connect**

Keep the SSH session with `-L 8765:127.0.0.1:8765` open, run the auth container
in that session, and open the printed URL on the same computer that owns the
local end of the tunnel. Check that no other program uses local port 8765.

**Model unavailable**

```bash
docker compose ps
docker compose logs --tail=200 ollama model-init
docker compose exec -T ollama ollama list
```

The required tag is `llama3.2:3b-instruct-q4_K_M`.

**Browser shows a TLS warning**

Use the exact configured hostname, verify it resolves to the Pi, and import
Caddy's root certificate. Never work around the warning.

**Pi is swapping or unresponsive**

Confirm active cooling, close other workloads, and inspect:

```bash
free -h
docker stats --no-stream
vcgencmd measure_temp
```

The Compose memory limits keep the combined services below the Pi's 8 GB, but
another host workload can still cause pressure.

**Database check**

SQLite is only in the local `mail_buddy_data` volume:

```bash
docker volume inspect mail-buddy_mail_buddy_data
docker compose exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/mail_buddy.sqlite3'); print(c.execute('PRAGMA integrity_check').fetchone()[0])"
```

Expected output is `ok`. Do not copy a live SQLite file directly; use
`mail-buddy backup`.

## Security boundaries

Mail-Buddy is for one trusted user on a private home LAN. It is not designed for
public internet exposure, multi-user isolation, enterprise Google Workspace
administration, sending mail, OCR, or clinical/financial decision-making.
Read [SECURITY.md](SECURITY.md) before connecting a real mailbox.
