# Mail-Buddy on Home Assistant OS

Mail-Buddy can run on Home Assistant OS as a custom Home Assistant add-on. It
is not installed with Docker Compose and does not modify the HAOS host. The
Supervisor builds and owns one add-on container that contains Mail-Buddy and
its local Ollama process. The dashboard is opened through Home Assistant
Ingress, so it has no LAN port and needs no Caddy container.

The add-on is supported on 64-bit `aarch64` and `amd64` Home Assistant OS.
Allow at least 8 GB RAM, active cooling, and 12 GB free disk space on the
device. The initial Llama model download is several GB and is CPU-only.

## Install

1. If the GitHub repository is public, in Home Assistant open **Settings ->
   Apps -> App store**. Open the menu, select **Repositories**, and add:

   ```text
   https://github.com/shishirkarkeraa/Mail-Buddy
   ```

2. Refresh the app store, select **Mail-Buddy**, then install it. Building the
   image and downloading the local model can take several minutes on the first
   launch.
3. In the add-on configuration, paste the Google **Desktop application** OAuth
   JSON into **Google client secret JSON** and set a strong **Dashboard
   password**. These values are stored in the add-on's protected persistent
   configuration and are included in Home Assistant backups; keep those backups
   private.
4. Keep **OAuth authorize** disabled, save, start the add-on, and open it from
   the sidebar. HA Ingress authenticates the Home Assistant session; the
   Mail-Buddy password remains a second, independent dashboard lock.

Mail-Buddy stores its SQLite database, generated encryption/session secrets,
Gmail OAuth token, backups, and Ollama models under the add-on's persistent
`/data` directory. Home Assistant backups include this directory. Do not delete
the add-on's data unless you intend to remove its local Mail-Buddy state.

### Private GitHub repository: local add-on route

The Home Assistant store cannot supply GitHub credentials when cloning a custom
repository. Keep the repository private and use a local add-on instead:

1. On the computer that has this Mail-Buddy checkout, create a self-contained
   package. Choose a new, empty output path:

   ```bash
   /bin/sh scripts/package-haos-addon.sh ~/Desktop/mail-buddy-haos
   ```

2. Using the Home Assistant Samba or SSH app, copy that generated folder's
   **contents** to `/addons/mail-buddy` on HAOS. The resulting directory must
   contain `config.yaml`, `Dockerfile`, `run.sh`, `src/`, and
   `requirements.lock` directly; do not add another parent folder.
3. Reload the App store. **Mail-Buddy** appears in the local apps repository;
   install and configure it using steps 3 and 4 above.

This route does not clone Mail-Buddy from GitHub while Home Assistant builds
the image. It packages the precise local source revision instead.

## One-time Gmail OAuth

Google's desktop OAuth flow requires a browser-local loopback callback. The
add-on keeps this callback off by default. To connect Gmail safely:

1. Enable the add-on's `8765/tcp` port in its **Network** settings temporarily.
2. Set **OAuth authorize** to true and restart the add-on. Its log prints the
   Google URL and waits for the callback instead of starting the dashboard.
3. From the computer running the browser, open an SSH tunnel to the HA device:

   ```bash
   ssh -N -L 8765:HOME_ASSISTANT_IP:8765 your-ssh-user@HOME_ASSISTANT_IP
   ```

   The SSH service can be the official Terminal & SSH add-on. Do not disable
   add-on protection or enable host Docker access for this workflow.
4. Open the URL from the Mail-Buddy log in that computer's browser and finish
   Google consent. The browser callback reaches its local port 8765, traverses
   the SSH tunnel, and is handled by Mail-Buddy.
5. Set **OAuth authorize** back to false, remove the temporary `8765/tcp` port
   mapping, and restart Mail-Buddy normally.

Only grant Gmail's `gmail.modify` scope. It permits direct labels, removing
Inbox, moving promotional mail to Gmail Trash, and undoing those actions. It
does not permit permanent deletion; Gmail Trash retention applies.

## Operational boundary

- Do not install the Compose stack, Caddy, systemd units, firewall scripts, or
  Ollama directly on HAOS.
- Do not use host networking, Docker socket mounts, privileged mode, or disable
  the add-on protection for Mail-Buddy.
- Do not port-forward Home Assistant, its Ingress endpoint, or the temporary
  OAuth port.
- HAOS training is the lightweight encrypted companion model built into
  Mail-Buddy. Apple Silicon MLX LoRA training remains an optional remote-Mac
  workflow; it is not run inside HAOS.

## Validate before using a real mailbox

- The add-on starts and the Ingress dashboard reports the local model ready.
- Gmail is connected only to the single destination account.
- A labelled test message disappears from Gmail Inbox.
- A shopping/general promotion is in Gmail Trash, and **Undo** restores it.
- The dashboard displays the test email's complete subject and readable body.
- A Home Assistant backup completes, and its retention/privacy implications are
  acceptable to you.

## References

- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Home Assistant Ingress presentation](https://developers.home-assistant.io/docs/apps/presentation/)
- [Testing local custom apps](https://developers.home-assistant.io/docs/apps/testing/)
