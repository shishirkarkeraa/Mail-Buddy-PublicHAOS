# Mail-Buddy Google OAuth verification demo

Use this script to record the private, **unlisted** video submitted to Google
with the `gmail.modify` scope verification request. Target length: 4–6 minutes.
Record only a dedicated Gmail test account containing synthetic messages. Do
not show passwords, OAuth client secrets, access tokens, real email content,
or an actual recipient address.

## Before recording

1. Publish the public site and confirm the home page, privacy policy, and
   terms links load over HTTPS.
2. Start Mail-Buddy with the destination Gmail test account connected. Use
   test messages with harmless invented data, including one forwarded message.
3. Ensure the test account has at least:
   - one finance message staged under `Bank Transactions`;
   - one uncertain message in `Needs Review` and removed from Inbox;
   - one previously applied category batch available to undo.
4. Open Google Auth Platform's **Data Access** page in a separate tab. It must
   show only the scopes actually requested by this build, including
   `https://www.googleapis.com/auth/gmail.modify`.
5. Use a fresh browser profile or revoke the test account's existing access,
   so the OAuth consent flow can be shown from start to finish.
6. Start the recorder at 1080p. Keep browser zoom at 100%, hide bookmarks and
   notifications, and use text narration or voice narration in English.

## Recording script

### 0:00–0:20 — Identify the app and scope

**Screen:** Google Auth Platform → Data Access. Show the Mail-Buddy project
name and the `gmail.modify` scope. Do not show secrets.

**Say:**

> This is Mail-Buddy, a self-hosted Gmail organization application. This
> project requests Gmail modify only. It reads message content to classify
> mail, then creates and applies Mail-Buddy labels, keeps uncertain messages
> in review, archives only approved messages by removing Inbox, and can undo
> those label and Inbox changes. It does not send mail or permanently delete
> mail.

### 0:20–1:10 — OAuth authorization

**Screen:** Start the Mail-Buddy OAuth command in a terminal. Show only the
command and the browser authorization URL; redact any values that should not
be visible. Open the URL and show the Google account chooser, consent screen,
and the authorized scope.

**Say:**

> I am connecting only the destination Gmail account. Source accounts only
> forward mail to this account and are never authorized. Google displays the
> requested permission during this consent flow. I select the dedicated test
> account and allow access.

**Action:** Complete consent, return to the terminal only long enough to show
successful completion, then open the Mail-Buddy dashboard.

### 1:10–1:40 — Connected local dashboard and data handling

**Screen:** Mail-Buddy **Overview**. Show the connected status and the Privacy
posture panel.

**Say:**

> The dashboard now shows that the destination account is connected. Mail-Buddy
> runs its classifier locally. It does not send email bodies or attachment text
> to a hosted AI service, and it keeps only encrypted credentials plus local
> decisions and redacted operational metadata needed to run and recover the
> service.

### 1:40–2:35 — Read mail and apply a category label

**Screen:** **Backfill**. Expand the Finance category. Use **Reveal Gmail
preview** on a harmless synthetic sample, then approve the category. Switch to
Gmail and show that the test message received `Bank Transactions` and is no
longer in Inbox.

**Say:**

> Mail-Buddy reads the message metadata and content needed to classify it. The
> historical workflow stages messages first; Gmail is unchanged until I approve
> a category. This synthetic finance message is then labeled
> Bank Transactions. Because I approved it, Mail-Buddy archives it by removing
> the Inbox label. This is why a labels-only scope is insufficient: the app must
> read content to classify and modify message labels to apply the result.

### 2:35–3:20 — Forwarded-mail behavior and safe review

**Screen:** In Gmail, open the synthetic forwarded message; then return to
Mail-Buddy **Needs review**. Reveal its limited preview and leave it in the
queue, or correct it using a category and a “future similar” rule.

**Say:**

> Mail-Buddy supports a consolidated inbox. It reads the original content in a
> forwarded wrapper as classification evidence, but treats it as untrusted.
> Messages that are uncertain, conflicting, or suspicious are labeled
> Needs Review and are removed from Inbox. A person decides before any
> archive action. A correction can also create a local deterministic rule for
> future similar messages.

### 3:20–4:05 — Undo and the limits of the permission

**Screen:** **Activity**. Click **Undo batch** for the earlier Finance batch;
confirm it. Switch back to Gmail and show the restored Inbox state and removal
of the Mail-Buddy category label.

**Say:**

> Each applied batch can be undone for 90 days. Undo restores the prior
> Mail-Buddy labels and Inbox state. Mail-Buddy does not permanently delete
> messages, does not send email, and does not change unrelated Gmail labels.

### 4:05–4:25 — Close

**Screen:** Mail-Buddy Settings, then the public Privacy Policy page.

**Say:**

> The public privacy policy describes this handling and the user can disconnect
> Gmail from Settings. Disconnect revokes local authorization and removes local
> account metadata; existing Gmail messages and labels are left unchanged.

## What to submit

- Upload the recording to YouTube as **Unlisted** (not public and not private)
  or provide another Google-accessible unlisted link.
- Use the link in the verification form's demo-video field.
- Make sure the project name, consent screen, requested scopes, and displayed
  app behavior match the submitted branding and scope justification exactly.
- If the UI or requested scopes change after recording, record a new video
  before submitting.

## Do not do this

- Do not use real customer/personal messages in the video.
- Do not edit together mock Gmail actions and present them as real app output.
- Do not expose `google_client_secret.json`, token files, passwords, API keys,
  or a non-redacted OAuth authorization code.
- Do not claim that `gmail.modify` is labels-only. The recording should clearly
  show why Mail-Buddy reads content and changes labels/Inbox state.
