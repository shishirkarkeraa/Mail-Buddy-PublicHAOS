# Mail-Buddy for Home Assistant OS

This repository contains only the self-contained Home Assistant OS app for
Mail-Buddy. The installable app is in [`mail-buddy`](mail-buddy/).

It contains no Gmail OAuth JSON, passwords, database, model, or application
data. Enter the OAuth JSON and dashboard password in the Home Assistant app
configuration screen.

## Automatic releases

The main Mail-Buddy repository tests and publishes this app after eligible
pushes to `main`. Each successful release gets a higher app version.

In Home Assistant, add this repository through **Settings -> Apps -> App store
-> Repositories**. Enable automatic updates if you want the Supervisor to
install released app updates automatically.

## Existing local installation

Installing this app from the public app store creates a new Home Assistant app
ID and a new `/data` directory. If your current local app already contains your
Gmail connection and model, follow [`MIGRATION.md`](MIGRATION.md) and do not
uninstall it until the replacement is working.
