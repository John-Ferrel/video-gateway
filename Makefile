.PHONY: install dev test lint run local docker

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	cp -n .env.example .env || true

dev:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt
	cp -n .env.example .env || true

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check gateway worker tests scripts skills
	node --check gateway/static/app.js

run:
	.venv/bin/python -m gateway.main

local:
	VIDEO_BACKEND=local .venv/bin/python -m gateway.main

docker:
	docker compose up -d --build
