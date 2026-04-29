# Variables
VENV := venv
PYTHON := python3
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
DIHI := $(VENV)/bin/dihi

.PHONY: setup install dev-install run clean test

# Build the venv (and install requirements) when requirements.txt changes
$(VENV)/bin/activate: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	source $(VENV)/bin/activate

startup: source $(VENV)/bin/activate

# Create/refresh venv + deps
setup: $(VENV)/bin/activate

# Install dependencies (alias)
install: $(VENV)/bin/activate

# Install the dihi package in editable mode; re-runs when pyproject.toml changes
$(DIHI): pyproject.toml $(VENV)/bin/activate
	$(PIP) install -q -e .

# Alias for the editable install
dev-install: $(DIHI)

# Run your app using the venv's python
run: $(VENV)/bin/activate
	$(VENV)/bin/python main.py

# Run unit tests (pure — no network, no ffmpeg, no HTTP)
test: $(VENV)/bin/activate
	$(PIP) install -q -r requirements-dev.txt
	$(PYTEST) tests/ -v --tb=short --cov=src/dihi --cov-report=term-missing

# Clean up the virtual environment
clean:
	rm -rf $(VENV)

# Download a YouTube video or playlist by ID or URL:
#   make dQw4w9WgXcQ
#   make PLxxxxxxxxxxxxxx
#   make "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# Requires: make dev-install (run once after setup)
.DEFAULT:
	@test -x "$(DIHI)" || { echo "Run 'make dev-install' first to install the dihi CLI."; exit 1; }
	@$(DIHI) download "$@"
