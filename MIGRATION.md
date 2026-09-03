# Switch the existing HAOS Mail-Buddy app to this public source

This preserves the existing Home Assistant app ID (`local_mail_buddy`) and its
persistent `/data`: Gmail OAuth credentials, encryption/session secrets,
SQLite database, backups, and Ollama model.

## 1. Create a rollback backup

In Home Assistant, open **Settings -> System -> Backups**, choose **Backup
now**, and create a manual backup that includes **Mail-Buddy**. Download it and
keep the emergency kit/password separately.

From Terminal & SSH, the equivalent command is:

```sh
ha backups new --name "mail-buddy-before-public-source" --addons local_mail_buddy
ha backups list
```

## 2. Clone the public source

On the Mac:

```sh
git clone https://github.com/shishirkarkeraa/Mail-Buddy-PublicHAOS.git ~/Desktop/mail-buddy-haos-public
```

## 3. Replace source files but keep the local app and its data

In the HAOS Samba `addons` share:

1. Stop Mail-Buddy.
2. Rename `mail-buddy` to `mail-buddy-source-backup`.
3. Copy `~/Desktop/mail-buddy-haos-public/mail-buddy` to the `addons` share.
4. Name the copied folder exactly `mail-buddy`.
5. Start Mail-Buddy and use **Update** when Home Assistant offers it.

Do **not** uninstall Mail-Buddy. Its `/data` belongs to the local app ID and
survives a source-folder replacement.

## 4. Validate before cleanup

Confirm all of these before deleting `mail-buddy-source-backup`:

- Dashboard opens through Home Assistant Ingress.
- The existing Gmail account appears connected.
- The local Ollama model is ready without a new multi-GB download.
- Existing activity/history remains visible.

If the source update fails, stop Mail-Buddy, restore
`mail-buddy-source-backup` to `addons/mail-buddy`, restart Home Assistant, and
start Mail-Buddy. The original data remains untouched.

## Direct public App Store install

You may add this public repository to the Home Assistant App Store, but install
it only after the retained-data local deployment is working. A direct Store
install receives a new app ID and intentionally starts with empty app data; it
is not the data-preserving switch procedure.
