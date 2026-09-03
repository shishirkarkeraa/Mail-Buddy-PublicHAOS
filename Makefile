PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: install test lint serve secrets up down logs auth backup compose-check licenses sbom

install:
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

serve:
	MAIL_BUDDY_DEMO_MODE=true MAIL_BUDDY_SECURE_COOKIES=false $(PYTHON) -m mail_buddy serve --host 127.0.0.1 --port 8000

secrets:
	./scripts/create-secrets.sh

compose-check:
	docker compose config --quiet

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs --tail=200 -f app ollama caddy

auth:
	docker compose --profile auth run --rm --service-ports auth

backup:
	docker compose exec -T app mail-buddy backup

licenses:
	$(PYTHON) -m piplicenses --format=markdown --with-urls --output-file=THIRD_PARTY_REPORT.md

sbom:
	PYTHON="$(PYTHON)" ./scripts/generate-sbom.sh
