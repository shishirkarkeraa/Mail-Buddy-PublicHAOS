# Third-party notices

Mail-Buddy application source is licensed under the repository's MIT
[LICENSE](LICENSE). That license does not relicense third-party software, images,
container layers, or model weights.

## Meta Llama 3.2

`llama3.2:3b-instruct-q4_K_M` model weights are governed by the **Meta Llama 3.2
Community License Agreement** and the **Acceptable Use Policy**, not by
Mail-Buddy's MIT license.

- License: <https://www.llama.com/llama3_2/license/>
- Acceptable Use Policy: <https://www.llama.com/llama3_2/use-policy/>

The model is downloaded from Ollama during deployment and is not distributed in
this source repository. Operators are responsible for reviewing and complying
with Meta's terms before downloading or using it.

## Runtime components

The deployment also uses independently licensed components, including:

- Ollama — MIT-licensed application; bundled dependencies/model content may use
  other licenses: <https://github.com/ollama/ollama>
- Caddy — Apache License 2.0:
  <https://github.com/caddyserver/caddy>
- Python — Python Software Foundation License:
  <https://www.python.org/psf/license/>
- FastAPI, Starlette, Uvicorn, Jinja2, Argon2-cffi, ItsDangerous, Pydantic,
  HTTPX, Beautiful Soup, Cryptography, PyPDF, Google API Client, Google Auth,
  and their transitive dependencies — each remains under its upstream license.

Container base images also contain operating-system packages under their
respective licenses. Image tags are pins for reproducibility, not statements
that every layer uses the project's MIT license.

## Generate the exact installed inventory

The dependency set can change when pins are deliberately updated. Generate the
inventory for the exact checkout and built image:

```bash
make licenses
docker compose build app
make sbom
```

`make licenses` writes `THIRD_PARTY_REPORT.md`. `make sbom` uses the free Syft
CLI or Docker SBOM plugin and writes SPDX JSON plus Python license metadata under
`sbom/`. Review these artifacts before redistributing a container or appliance.
