# Mail-Buddy security guide

## Supported deployment

Mail-Buddy v0.1 is supported only as a single-user service on a maintained
64-bit Raspberry Pi OS host or macOS host running a supported Docker Desktop
release. Use it on the same machine or a trusted private LAN behind the included
Caddy configuration. It must not be exposed through router port forwarding, a
public reverse proxy, or a public tunnel.

Security fixes apply to the latest commit/release only.

## Protected data

- OAuth refresh/access tokens and correction-rule patterns are encrypted with
  Fernet before local SQLite persistence.
- The encryption key, session secret, dashboard Argon2id hash, and Google OAuth
  client JSON are file-backed deployment secrets. A network-disabled init
  container copies them into a private named volume as mode-0400 files owned by
  the non-root application uid; the application mounts that volume read-only.
- Email bodies, addresses, subjects, snippets, OTPs, URLs, account/order IDs,
  and extracted attachment text must not appear in SQLite or logs.
- An authenticated preview can fetch sender, subject, and a bounded snippet
  directly from Gmail. The HTTP response is `no-store` and is not persisted.
- Caddy and Uvicorn access logs are disabled because message IDs can appear in
  authenticated paths.

The host administrator, root, Docker daemon or Docker Desktop backend, and
anyone holding the dashboard password or local CA trust anchor are trusted.
Disk encryption is recommended because application encryption does not protect
SQLite metadata or a running host compromised as an administrator.

## Network controls

- In the continuously running stack, only Caddy publishes TCP/443. The
  short-lived OAuth helper additionally publishes TCP/8765 on loopback only.
- FastAPI and Ollama remain on Docker networks with no host port.
- `tls internal` provides LAN encryption; install the Caddy root only on trusted
  devices.
- On a Pi LAN deployment, the `DOCKER-USER` firewall rule limits published
  HTTPS to the configured LAN CIDR. A local-only Mac deployment remains bound
  to loopback; use the macOS firewall before opting into LAN access.
- OAuth is published on host loopback only. Use it directly on a Mac, or reach
  the Pi loopback listener through an SSH tunnel.

Do not trust a certificate-warning bypass, publish OAuth on a non-loopback
address, or configure a WAN port-forward. Review Docker and host-firewall
behavior after every major Docker or operating-system upgrade.

## Account controls

- The only requested Google scope is `gmail.modify`.
- Mail-Buddy never calls permanent-delete APIs.
- Five failed sign-ins from one client address within five minutes are blocked.
- Cookies are signed, Secure, HttpOnly, and SameSite=Strict in production.
- Every state-changing web form requires a session-bound CSRF token.
- Disconnect revokes credentials when possible and removes local account data;
  Gmail messages and labels are deliberately retained.

Use a strong, unique dashboard password. If it is exposed, replace
`password_hash` and restart. If the session secret is exposed, rotate it and
restart, invalidating all sessions. If the encryption key is exposed, disconnect
Gmail, rotate the key, reauthorize, and recreate correction rules.

## Host maintenance

Apply Raspberry Pi OS or macOS, Docker, Caddy, Ollama, and Python dependency
security updates after reviewing compatibility. Back up before changing pinned
versions. On a Pi, keep active cooling enabled. On either platform, monitor free
storage; full disks can interrupt SQLite writes.

Run periodically:

```bash
docker compose ps
docker stats --no-stream
docker compose exec -T app python -c \
  "import sqlite3; c=sqlite3.connect('/data/mail_buddy.sqlite3'); print(c.execute('PRAGMA integrity_check').fetchone()[0])"
make licenses
make sbom
```

## Reporting a vulnerability

Do not attach real email, OAuth JSON, tokens, secret files, database files, or
unredacted logs to a public issue. Report the smallest reproducible case to the
project maintainer through a private channel. Include:

- the Mail-Buddy version/commit;
- host operating system and architecture, Docker, and browser versions;
- a synthetic reproduction;
- only redacted operational error codes.

If private contact information is not published for your checkout, withhold the
sensitive reproduction until the maintainer provides a secure channel.
