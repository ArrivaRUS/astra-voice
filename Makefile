# Astra Voice — цели разработки и сборки.
# Dev-окружение: python3 -m venv --system-site-packages $(VENV); pip install pytest ruff mypy
VENV ?= $(HOME)/.cache/astra-voice-dev/venv-a
PY   := $(VENV)/bin/python
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest

.PHONY: lint test test-xvfb test-engine deb wheels theme user-bundle

lint:
	$(RUFF) check .
	$(RUFF) format --check .
	$(MYPY)

test:
	$(PYTEST) -m unit

test-xvfb:
	xvfb-run -a $(PYTEST) -m xvfb

test-engine:
	$(PYTEST) -m engine

deb:
	packaging/build-deb.sh

wheels:
	packaging/build-deb.sh --download-wheels

theme:
	$(PY) scripts/gen_theme.py

user-bundle:
	@echo "user-bundle: запланировано на v1.1 (G4)"; exit 0
