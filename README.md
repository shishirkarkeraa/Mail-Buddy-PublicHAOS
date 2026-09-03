# Mail-Buddy for Home Assistant OS

This is the public, self-contained Home Assistant app repository for
Mail-Buddy. It contains no Gmail OAuth JSON, passwords, database, model, or
application data. The app is in [`mail-buddy`](mail-buddy/).

## Retain existing HAOS data while switching source

The existing locally installed app has the ID `local_mail_buddy`. Installing
from this public app-store repository would create a different app ID and a new
`/data` directory. To retain the connected Gmail account, encrypted SQLite
database, generated secrets, and downloaded Ollama model, keep the existing
**local** app ID and replace only its source files from this public repository.

Follow [`MIGRATION.md`](MIGRATION.md) exactly. Do not uninstall the existing
local Mail-Buddy app until the public-source version is running and its
dashboard confirms that Gmail is connected.
