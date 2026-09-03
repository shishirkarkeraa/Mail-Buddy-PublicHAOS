# Mail-Buddy Laptop GPU + Raspberry Pi Fallback Guide

## Purpose

Mail-Buddy can keep Gmail synchronization and storage on the Raspberry Pi while
preferring an Ollama instance on a more powerful laptop for classification.
If the laptop is asleep, disconnected, or its Ollama request fails, Mail-Buddy
automatically retries the same message against the Pi's local Ollama service.

```text
Destination Gmail
       |
       v
Mail-Buddy on Raspberry Pi
       |-- primary  --> Laptop Ollama (GPU)
       `-- fallback --> Pi Ollama (CPU)
```

Both machines must have the exact pinned model:
`llama3.2:3b-instruct-q4_K_M`. The laptop endpoint must be a literal private
IPv4, private IPv6, or Tailscale CGNAT address. Public IPs, public hostnames,
credentials in URLs, HTTPS URLs, and arbitrary URL paths are rejected.

## Direct Gmail labels

Mail-Buddy creates direct top-level labels such as `Security OTP`,
`Bank Transactions`, `College Notices`, `Order Updates`, and `Needs Review`.
It does not create a `Mail-Buddy/...` hierarchy. When an existing deployment
starts, known legacy labels are renamed in place so Gmail retains their message
membership.

## Security and privacy boundary

With this option enabled, a redacted model prompt derived from an email travels
from the Pi to the laptop. It remains on user-controlled devices, but it no
longer remains on the Pi alone. Use only a trusted home LAN or Tailscale, do not
forward TCP 11434 on the router, and restrict the laptop firewall to the Pi.
Ollama's local HTTP API has no application-level authentication by default.

The prompt removes common email addresses, OTPs, and secret-bearing URL query
values, but it can still contain message text. Never use a public Ollama server.

## 1. Prepare the laptop

### Apple Silicon Mac

Install the native Ollama application from <https://ollama.com/download>. The
native app uses Metal acceleration. Do not use Ollama inside Docker Desktop for
Mac GPU inference.

### Windows or Linux laptop

Install Ollama using the official platform instructions. Supported NVIDIA and
AMD configurations can use GPU acceleration. Verify drivers before continuing.

### Pull and verify the model

On the laptop:

```bash
ollama pull llama3.2:3b-instruct-q4_K_M
ollama run llama3.2:3b-instruct-q4_K_M "Reply with READY only"
ollama ps
```

`ollama ps` should show GPU use where supported.

## 2. Give the laptop a stable private address

Prefer one of these:

1. Reserve the laptop's LAN IPv4 address in the router, for example
   `192.168.1.25`.
2. Install Tailscale on both machines and use the laptop's stable
   `100.64.0.0/10` address.

The Mail-Buddy URL validator intentionally requires a literal address. Do not
use `.local` or public DNS names.

## 3. Expose Ollama only to the trusted network

Ollama normally listens only on `127.0.0.1`. Configure it to listen on the
laptop network interface.

On macOS, quit Ollama, run:

```bash
launchctl setenv OLLAMA_HOST "0.0.0.0:11434"
```

Then reopen Ollama. On Linux, add this systemd override:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

On Windows, set user environment variable `OLLAMA_HOST` to
`0.0.0.0:11434`, quit Ollama completely, and start it again.

Configure the laptop firewall to allow inbound TCP 11434 only from the Pi's
fixed LAN or Tailscale IP. Do not create a router port-forwarding rule.

From the Pi, verify connectivity without sending mail content:

```bash
curl --fail --max-time 5 http://192.168.1.25:11434/api/tags
```

The returned list must include `llama3.2:3b-instruct-q4_K_M`.

## 4. Configure Mail-Buddy on the Pi

Edit `/opt/mail-buddy/.env` and set the laptop's actual private address:

```dotenv
MAIL_BUDDY_OLLAMA_PRIMARY_URL=http://192.168.1.25:11434
MAIL_BUDDY_OLLAMA_CONNECT_TIMEOUT_SECONDS=3
MAIL_BUDDY_OLLAMA_TIMEOUT_SECONDS=120
```

The existing internal setting remains the fallback and should not be replaced:

```text
MAIL_BUDDY_OLLAMA_URL=http://ollama:11434
```

Compose supplies that internal value automatically. Start the stack:

```bash
cd /opt/mail-buddy
./scripts/start-mail-buddy.sh
docker compose logs --tail=100 app ollama model-init
```

The Pi still downloads the pinned model because it must remain ready when the
laptop is unavailable.

## 5. Personalized model used with Llama

The personalized learner runs on the Pi and remains available regardless of
which Ollama host is selected. It is a second model, not a replacement:

1. Deterministic safety and explicit correction rules run first.
2. For unresolved mail, the latest promoted personalized model produces a
   category, confidence, and margin from encrypted owner-reviewed examples.
3. That opinion is included as an advisory hint in the request sent to the
   laptop Ollama primary or Pi Ollama fallback.
4. Llama independently classifies the message. Confident agreement is recorded;
   confident disagreement is filed under `Needs Review` and removed from Inbox.

Open **Personal learning** in the dashboard to answer accuracy questions,
enable or disable automatic training, set the interval and local-time hour, and see
candidate accuracy/promotion history. Needs Review data is used only after you
choose its correct label; uncertain model guesses are never self-trained.

The database stores only bounded redacted sender-domain, subject, and snippet
training features, encrypted at rest. Full bodies and attachments remain
transient. Update the public privacy policy to disclose both optional laptop
inference and encrypted local personalization data.

At 200 suitable confirmed examples, the optional macOS MLX agent can train,
evaluate, and install a versioned Q4_K_M fine-tune on both hosts. Follow
`docs/PERSONAL_LEARNING_AND_MLX_DEPLOYMENT.md`; no unreviewed prediction is
included as ground truth.

## 6. Prove primary and fallback behavior

Use a dedicated Gmail test account and harmless synthetic messages.

### Test the laptop primary

1. Keep laptop Ollama running.
2. Send a synthetic message that needs semantic classification.
3. On the laptop, run `ollama ps` and inspect Ollama logs.
4. Confirm Mail-Buddy labels the message as expected.

### Test the Pi fallback

1. Quit Ollama on the laptop or disconnect the laptop from the network.
2. Send a different synthetic message.
3. Mail-Buddy waits only for the configured connection timeout when the laptop
   cannot be reached, then retries against Pi Ollama.
4. Confirm the message is classified and that Pi logs show the model request.

```bash
docker compose logs --since=5m app ollama
```

### Test total model failure

If both Ollama instances are unavailable, Mail-Buddy fails safely. A message
that cannot be handled deterministically is kept in Inbox and assigned
`Needs Review`; it is not silently archived.

## 7. Operations

- Start Pi services: `./scripts/start-mail-buddy.sh`
- Stop Pi services safely: `./scripts/stop-mail-buddy.sh`
- Check containers: `docker compose ps`
- Check laptop model: `ollama ps`
- Disable laptop primary: leave `MAIL_BUDDY_OLLAMA_PRIMARY_URL=` blank and
  restart Mail-Buddy.
- Change laptop address: update the one `.env` value and restart the stack.

## Troubleshooting

| Symptom | Check | Resolution |
|---|---|---|
| Laptop never receives requests | `curl` from Pi to `/api/tags` | Check `OLLAMA_HOST`, laptop firewall, IP, and model tag |
| Pi always waits before fallback | Laptop is asleep but its IP still blackholes traffic | Keep connect timeout at 3 seconds; prefer Tailscale or a stable LAN |
| Configuration rejects URL | URL uses a name, public IP, HTTPS, path, or credentials | Use `http://PRIVATE-IP:11434` only |
| Laptop uses CPU | `ollama ps` processor column | Run native Ollama and verify GPU support/drivers |
| Both hosts fail | App and Ollama logs | Restore either Ollama host; review messages remain safe in Inbox |
| Classification differs between hosts | Model list and tag | Pull the exact pinned model on both machines |

## Go-live checklist

- [ ] Laptop and Pi both list the exact pinned model.
- [ ] Laptop has a stable private or Tailscale IP.
- [ ] TCP 11434 is allowed only from the Pi.
- [ ] No router port forwarding exposes Ollama.
- [ ] `.env` contains the laptop private-IP URL.
- [ ] Laptop-primary test succeeded with synthetic mail.
- [ ] Pi-fallback test succeeded while the laptop was unavailable.
- [ ] Both-hosts-down test kept unresolved mail in Needs Review.
- [ ] Needs Review correction appears as an owner-labeled training example.
- [ ] Accuracy questions were answered with harmless test mail.
- [ ] A candidate below the gate remained rejected; an eligible candidate was promoted.
- [ ] Confident personal/Llama disagreement stayed in Needs Review.
- [ ] Privacy policy says model prompts may travel to a trusted user-controlled laptop.

## Official references

- Ollama FAQ and network configuration: <https://docs.ollama.com/faq>
- Ollama hardware and GPU support: <https://docs.ollama.com/gpu>
- Ollama macOS support: <https://docs.ollama.com/macos>
- Ollama API: <https://docs.ollama.com/api/introduction>
