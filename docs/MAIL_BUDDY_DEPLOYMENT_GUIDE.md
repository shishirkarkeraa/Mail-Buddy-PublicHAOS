---
title: "Mail-Buddy Deployment and Destination Gmail Integration Guide"
subtitle: "Private, local-first sorting for a consolidated inbox"
date: "1 September 2026"
---

# Purpose

Deploy Mail-Buddy once and connect it to one destination Gmail account. Configure every other mailbox to forward or redirect to that account. Mail-Buddy watches the destination inbox, classifies received mail, and applies its category labels there.

Mail-Buddy uses Gmail API access with the gmail.modify scope, a local SQLite database, and a local Ollama model. It does not need IMAP, SMTP, a Gmail password, an app password, a hosted database, or a hosted AI service.

It also includes a Pi-local personalized classifier. Owner-confirmed Needs Review corrections and dashboard accuracy answers are stored as bounded redacted, encrypted training examples. The latest promoted personal model advises the main Llama model; confident disagreement is sent back to Needs Review rather than silently applied.

> Safety boundary: start with a dedicated Gmail test account. Inspect staged samples, test undo, and only then connect the real destination account. Do not expose the dashboard or OAuth callback to the public internet.

# Mail handling outcome

| Condition | Destination Gmail result |
|---|---|
| Approved, confidently classified new mail | One direct category label is added and the message is archived from Inbox. |
| General or shopping promotion | The message is moved to Gmail Trash, not permanently deleted; batch undo restores it. |
| Uncertain, conflicting, or suspicious mail | Needs Review is added and the message is removed from Inbox. |
| Historical mail during the first scan | The message is staged locally; Gmail stays unchanged until the category is approved. |
| Existing labels and unread state | Preserved. Mail-Buddy changes only labels mapped to its categories and, for approved results, Inbox membership. |
| Incorrect batch approval | Activity -> Undo batch restores the prior category labels and Inbox state. |

Authorization creates these direct top-level labels in the destination account:

~~~
Security OTP
Password Reset
Account Alerts
Bank Transactions
General Promotions
College Important
College Internship Opportunities
College Placements
College Notices
Subscriptions
Job Related
Internship
Shopping Promotions
Order Updates
Social
Personal
Other
Needs Review
~~~

When upgrading an existing mailbox, Mail-Buddy renames its known legacy
`Mail-Buddy/...` labels in place. Gmail preserves the messages attached to a
renamed label.

# Before you begin

## Deployment host

Choose one supported host:

- macOS with Docker Desktop; the dashboard is local at https://localhost.
- Raspberry Pi 5 with 64-bit Raspberry Pi OS and Docker Engine; the dashboard is on the private home LAN only.

Reserve at least 10 GB storage. The first run downloads about 2 GB of local model layers. The macOS Docker VM should have at least 6 GB memory and 4 CPUs; 7-8 GB is preferable on a 16 GB-or-larger Mac. A Pi should have 8 GB RAM and active cooling.

## Items to prepare

1. A destination Gmail account. This is the only account Mail-Buddy authorizes.
2. A Google Cloud project you own.
3. A Desktop app OAuth client JSON downloaded from that project.
4. A dashboard password of at least 12 characters.
5. For a Pi: its fixed private IPv4 address, LAN subnet, and network interface.
6. Trusted college-sender domains, if college mail needs special handling.

Never put OAuth JSON, passwords, tokens, or encryption keys in .env.

# 1. Install Docker and confirm the host

## macOS

Install and start Docker Desktop from Docker's official macOS instructions. Allocate the resources above, then run:

~~~
docker version
docker compose version
uname -m
df -h .
~~~

Port 443 serves local HTTPS and port 8765 is used only while authorizing Gmail. Before authorization, confirm 8765 is free:

~~~
lsof -nP -iTCP:8765 -sTCP:LISTEN
~~~

## Raspberry Pi 5

Install Docker Engine and Compose through Docker's current Raspberry Pi OS instructions, then run:

~~~
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin avahi-daemon
sudo systemctl enable --now docker avahi-daemon
sudo usermod -aG docker "$USER"
~~~

Sign out and back in, then confirm Docker:

~~~
docker compose version
docker run --rm hello-world
~~~

Place the project in the location expected by the supplied systemd service:

~~~
sudo mkdir -p /opt/mail-buddy
sudo chown "$USER":"$USER" /opt/mail-buddy
cd /opt/mail-buddy
# Copy or clone the Mail-Buddy project here.
test -f compose.yaml && echo "Project root is correct"
~~~

# 2. Create the Google/Gmail OAuth client

In Google Cloud Console:

1. Create or select a project you own.
2. Enable Gmail API.
3. Open Google Auth Platform and complete Branding.
4. Use an External audience for a personal Gmail account.
5. Under Data Access, request only:

   ~~~
   https://www.googleapis.com/auth/gmail.modify
   ~~~

6. Move the project to In production. An External project left in Testing can issue refresh tokens that expire after seven days.
7. Create a client with application type Desktop app.
8. Download the OAuth client JSON and keep it private.

Do not create a Web application client. Desktop app OAuth is required for the loopback callback. A managed Workspace administrator can block gmail.modify; Mail-Buddy cannot bypass that policy.

# 3. Configure Mail-Buddy

From the project root:

~~~
cp .env.example .env
~~~

For a local Mac deployment:

~~~
MAIL_BUDDY_HOSTNAME=localhost
MAIL_BUDDY_BIND_ADDRESS=127.0.0.1
MAIL_BUDDY_SECRETS_DIR=./secrets
MAIL_BUDDY_OLLAMA_MODEL=llama3.2:3b-instruct-q4_K_M
MAIL_BUDDY_COLLEGE_DOMAINS=
TZ=Asia/Kolkata
~~~

For a Raspberry Pi, replace the address, hostname, domains, and timezone:

~~~
MAIL_BUDDY_HOSTNAME=mail-buddy.local
MAIL_BUDDY_BIND_ADDRESS=192.168.1.50
MAIL_BUDDY_SECRETS_DIR=./secrets
MAIL_BUDDY_OLLAMA_MODEL=llama3.2:3b-instruct-q4_K_M
MAIL_BUDDY_COLLEGE_DOMAINS=college.example.edu,placements.college.example.edu
TZ=Asia/Kolkata
~~~

On a Pi, the bind address must be assigned to the chosen LAN interface. Do not use 0.0.0.0 or loopback. Set a DHCP reservation in the router for the Pi address.

# 4. Create and install private secrets

Create the local encryption key, session secret, and dashboard password verifier:

~~~
./scripts/create-secrets.sh
~~~

This creates:

~~~
secrets/encryption_key
secrets/session_secret
secrets/password_hash
~~~

Install the downloaded OAuth JSON under the exact required name:

~~~
install -m 0600 /absolute/path/to/downloaded-oauth-client.json secrets/google_client_secret.json
~~~

On a Pi, install the JSON as root:root mode 0600. Do not print secret contents. Check only names, modes, and nonzero sizes:

~~~
# macOS
stat -f '%Sp %Su:%Sg %z %N' secrets/encryption_key secrets/session_secret secrets/password_hash secrets/google_client_secret.json

# Raspberry Pi
sudo stat -c '%A %U:%G %s %n' secrets/encryption_key secrets/session_secret secrets/password_hash secrets/google_client_secret.json
~~~

Docker copies the four source files into a network-disabled runtime volume. The application reads only that private, read-only runtime copy.

# 5. Authorize the destination Gmail account

Run these commands from the project root:

~~~
docker compose config --quiet
docker compose --profile auth run --rm --service-ports auth
~~~

Open the printed Google authorization URL. Choose the destination Gmail account, inspect the gmail.modify permission, and complete consent. The only callback is:

~~~
http://127.0.0.1:8765/
~~~

Success ends with:

~~~
Gmail account connected. Mail-Buddy labels are ready.
~~~

Mail-Buddy creates the destination label tree, encrypts the refresh token in local SQLite, and never stores the Gmail password.

For Pi authorization, do not expose 8765. On the computer with the browser, keep this SSH tunnel open:

~~~
ssh -L 8765:127.0.0.1:8765 pi@192.168.1.50
~~~

Then run the same authorization command on the Pi and open its printed URL in the browser on the tunneled computer.

# 6. Redirect all source mailboxes to the destination

Perform forwarding or redirection in every source mailbox. Each provider has different screens, but every source must use the destination Gmail address authorized in step 5.

Recommended sequence:

1. Enable forwarding to the destination at one source account.
2. Complete the provider's forwarding-verification email, if prompted.
3. Send a harmless test message to that source address.
4. Confirm it arrives in destination Gmail.
5. Repeat for every remaining source account.

Do not authorize every source account in Mail-Buddy. The app monitors one destination Gmail account and applies labels there.

Forwarding formats differ. Native redirect usually preserves the original sender. Some providers instead send an outer Fwd wrapper. Mail-Buddy recognizes common forwarded-message separators and compact From, Sent, To, and Subject blocks, then uses bounded original-message content as classification evidence. Suspicious or ambiguous mail still reaches Needs Review instead of being hidden.

# 7. Secure a Raspberry Pi deployment

Skip this section on macOS.

Find the trusted subnet and Pi interface:

~~~
ip route
~~~

Apply the Docker-aware firewall rule, replacing the examples:

~~~
sudo ./scripts/configure-firewall.sh 192.168.1.0/24 eth0
~~~

Persist the values and run the preflight:

~~~
sudo install -d -m 0755 /etc/mail-buddy
printf '%s\n' \
  'MAIL_BUDDY_LAN_SUBNET=192.168.1.0/24' \
  'MAIL_BUDDY_LAN_INTERFACE=eth0' \
  | sudo tee /etc/mail-buddy/firewall.env >/dev/null
sudo chmod 0600 /etc/mail-buddy/firewall.env
sudo /opt/mail-buddy/scripts/deployment-preflight.sh /opt/mail-buddy
~~~

Never configure router port forwarding. OAuth port 8765 stays loopback-only and temporary.

# 8. Start and verify

Start Mail-Buddy:

~~~
docker compose config --quiet
docker compose up --build --detach
docker compose ps
docker compose ps -a secret-init model-init
docker compose logs --tail=100 secret-init app ollama model-init caddy
~~~

On the first run, the local model is downloaded and its pinned manifest is checked. Continue only when:

- secret-init exited with code 0.
- model-init exited with code 0.
- app, ollama, and caddy are healthy.

Verify readiness:

~~~
docker compose exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"
~~~

Expected output:

~~~
ready
~~~

# 9. Trust local HTTPS and open the dashboard

Export Caddy's local root certificate:

~~~
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./mail-buddy-root.crt
~~~

On a Mac, trust it:

~~~
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ./mail-buddy-root.crt
~~~

Open https://localhost on macOS or https://mail-buddy.local on the Pi. Do not click through a certificate warning. Correct the hostname, name resolution, or root-certificate installation instead.

# 10. First safe sorting run

Use this acceptance checklist with the test destination account:

1. Sign in using the dashboard password created in step 4.
2. Confirm Gmail connected, local classifier ready, and a healthy queue.
3. Add real college sender domains in Settings if appropriate.
4. Open Backfill and start the historical scan.
5. Confirm Gmail stays unchanged while messages are staged.
6. Inspect representative samples for each category.
7. Approve only categories whose samples are correct.
8. Review Needs Review messages and save correction rules where useful.
9. Send tests through each source mailbox and confirm the destination gets the expected Mail-Buddy label.
10. Use Activity -> Undo batch once and confirm original Inbox and label state is restored.

After this test passes, repeat the authorization for the real destination account and enable forwarding on the source mailboxes.

# Operations, backup, and recovery

~~~
# Status and redacted logs
docker compose ps
docker compose logs --tail=200 app ollama caddy

# Restart only the application
docker compose restart app

# Rebuild after an update
docker compose up --build --detach

# Stop without deleting volumes
docker compose down

# Create an atomic SQLite backup
docker compose exec -T app mail-buddy backup
~~~

Never run docker compose down -v; the -v option deletes local data volumes.

Restore a deliberately selected backup:

~~~
./scripts/restore-backup.sh mail_buddy-YYYYMMDDTHHMMSSZ.sqlite3
~~~

Verify SQLite:

~~~
docker compose exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/mail_buddy.sqlite3'); print(c.execute('PRAGMA integrity_check').fetchone()[0])"
~~~

Expected output is ok.

Disconnect through the dashboard by typing DISCONNECT, or run:

~~~
docker compose exec -T app mail-buddy disconnect --yes
~~~

Disconnect removes local account metadata and attempts OAuth revocation; it leaves existing Gmail labels and messages unchanged.

# Personalized learning and accuracy

Open **Personal learning** after the first safe backfill. Correct Needs Review
items and answer the sampled “What is the correct label?” questions. Unanswered
or unresolved predictions are not training truth. Configure automatic training
there with an enabled/disabled switch, a 1, 3, 7, 14, or 30 day interval, and
the start hour in the configured local timezone. Timestamps are stored in UTC.

Training and five-fold held-out evaluation run locally on the Pi. A candidate
becomes active only after meeting the minimum example, evaluation, and accuracy
thresholds and only when it is not worse than the active model. The active
personalized classifier runs alongside Llama for ambiguous mail. It supplies an
advisory category/confidence result to either the laptop Ollama primary or Pi
fallback; confident disagreement files the message under Needs Review and removes it from Inbox.

Back up the SQLite volume because it contains encrypted examples and trained
artifacts. The encryption key is required to use either. Disconnecting Gmail
deletes both. If your public privacy policy currently says no message-derived
data is persisted, update it to disclose encrypted redacted training excerpts.

At 200 suitable owner-confirmed examples, the optional Apple Silicon trainer
can create a Llama 3.2 3B QLoRA release. It evaluates candidate and production
through Ollama, transfers only the redacted bundle, verifies the identical GGUF
checksum on both hosts, and activates atomically. Follow
`docs/PERSONAL_LEARNING_AND_MLX_DEPLOYMENT.md` for setup and recovery.

# Troubleshooting

| Symptom | Check | Corrective action |
|---|---|---|
| OAuth callback fails | Check whether port 8765 is in use | Use the exact auth command; on Pi keep the SSH tunnel open. Verify the OAuth client is a Desktop app client. |
| Gmail stops after seven days | Google Auth Platform publishing status | Move the External project to In production, then authorize again. |
| Secret import fails | docker compose logs --tail=100 secret-init | Verify all four secret files exist, are private, and are nonempty. |
| Local model unavailable | docker compose logs --tail=200 ollama model-init | Check outbound internet and do not bypass pinned-manifest verification. |
| TLS warning | Hostname, local DNS/mDNS, root certificate | Correct the hostname/address pairing and trust Caddy's local root. |
| A category is wrong | Needs Review and correction controls | Correct the message and save a sender or similar-message rule. |
| Destination mail is not sorting | Dashboard health, forwarding test, category approval | Confirm the message reached the authorized Gmail, inspect queue/logs, and make sure its category is approved. |

# Final go-live checklist

- [ ] Docker and Docker Compose are available on the deployment host.
- [ ] Gmail API is enabled and the OAuth client is Desktop app type.
- [ ] The OAuth project is In production.
- [ ] All four local secret files are private and nonempty.
- [ ] Only the destination Gmail account is authorized.
- [ ] The dashboard is local-only or restricted to the trusted home LAN.
- [ ] Router port forwarding is disabled.
- [ ] Every source account successfully forwards or redirects to destination Gmail.
- [ ] Test forwarded mail receives the expected destination Gmail label.
- [ ] Needs Review messages are removed from Inbox.
- [ ] An undo test restored original labels and Inbox state.
- [ ] A verified SQLite backup exists.
