# Variables
ifneq ($(wildcard .env),)
include .env
endif

VENV := venv
PYTHON := python3
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
DIHI := $(VENV)/bin/dihi
YTDLP := $(VENV)/bin/yt-dlp
DATA_DIR ?= $(if $(DIHI_DATA_DIR),$(DIHI_DATA_DIR),./data)
USB_MOUNT ?= /mnt/usbdocker1
USB_DRIVE ?= D:
STATIC_CATALOG_PORT ?= 8765
.DEFAULT_GOAL := help

# WSL2: detect Windows username and locate browser profiles on the host C: drive
_WIN_USER     := $(shell command -v cmd.exe >/dev/null 2>&1 && timeout 2s cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n' || true)
_CHROME_PROF  := /mnt/c/Users/$(_WIN_USER)/AppData/Local/Google/Chrome/User Data
_EDGE_PROF    := /mnt/c/Users/$(_WIN_USER)/AppData/Local/Microsoft/Edge/User Data

.PHONY: help startup setup install dev-install pot-provider usb-mount storage-check docker-up docker-down docker-logs test-docker run clean test data data-size data-migrate catalog-static catalog-static-serve cookies cookies-browser install-chrome git-add git-commit-push

help:
	@echo 'Available targets:'
	@echo '  help             Show this help'
	@echo '  setup            Create venv and install dependencies, including the PO Token plugin'
	@echo '  install          Alias for setup'
	@echo '  dev-install      Install the dihi CLI in editable mode'
	@echo '  pot-provider     Start the Docker PO Token service for host downloads'
	@echo '  usb-mount        Mount the configured Windows USB drive (USB_DRIVE=D:)'
	@echo '  storage-check    Verify the configured data root is readable/writable'
	@echo '  docker-up        Build and start the dihi server and PO Token provider'
	@echo '  docker-down      Stop the Docker Compose services'
	@echo '  docker-logs      Follow Docker Compose service logs'
	@echo '  test-docker      Start Docker and run end-to-end smoke tests'
	@echo '  startup          Show the venv activation command'
	@echo '  run              Run the app with the venv Python'
	@echo '  test             Run unit tests with coverage'
	@echo '  data             Create Docker bind-mount data files'
	@echo '  data-size        Show runtime data disk usage'
	@echo '  data-migrate     Copy runtime data to DEST_DATA_DIR without deleting source'
	@echo '  catalog-static   Generate an offline catalog browser under the data root'
	@echo '  catalog-static-serve  Generate and serve it at http://localhost:8765'
	@echo '  cookies          Export YouTube cookies from a WSL2 browser'
	@echo '  cookies-browser  Export cookies from a fresh Linux browser session'
	@echo '  install-chrome   Install Chrome on Debian/Ubuntu'
	@echo '  git-add          Stage project files, excluding runtime data'
	@echo '  git-commit-push  Commit and push staged changes (requires MSG=...)'
	@echo '  clean            Remove the venv'
	@echo '  <video ID or URL> Download a video or playlist'

# Mark setup complete only after every install succeeds. The activate script
# exists before pip runs, so using it as the target would hide failed installs.
$(VENV)/.setup-complete: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $@

startup:
	@echo 'To activate the venv in your shell, run: source $(VENV)/bin/activate'

# Create/refresh venv + deps
setup: $(VENV)/.setup-complete

# Install dependencies (alias)
install: $(VENV)/.setup-complete

# Install the dihi package in editable mode; re-runs when pyproject.toml changes
$(DIHI): pyproject.toml $(VENV)/.setup-complete
	$(PIP) install -q -e .

# Alias for the editable install
dev-install: $(DIHI)

# Start the per-video PO Token provider on host loopback. Docker Compose also
# starts this service automatically when the full server stack is launched.
pot-provider:
	@command -v docker >/dev/null || { echo 'Docker is required for the PO Token provider.'; exit 1; }
	@docker compose up -d bgutil-provider

# Mount the Windows USB drive at a stable WSL path. This is intentionally
# explicit because sudo may prompt and the Windows drive letter can change.
usb-mount:
	@sudo mkdir -p "$(USB_MOUNT)"
	@if mountpoint -q "$(USB_MOUNT)"; then \
		if ! df -P "$(USB_MOUNT)" >/dev/null 2>&1; then \
			echo "ERROR: $(USB_MOUNT) is mounted but unavailable; reconnect the drive or restart WSL, then retry."; exit 1; \
		fi; \
		echo "USB mount already available: $(USB_MOUNT)"; \
	else \
		sudo mount -t drvfs "$(USB_DRIVE)" "$(USB_MOUNT)"; \
	fi
	@test -d "$(USB_MOUNT)" && df -P "$(USB_MOUNT)" >/dev/null 2>&1 || { echo "ERROR: USB mount is not available at $(USB_MOUNT)."; exit 1; }

# Fail before `make data` or Docker can create anything under a missing USB
# mount. Local ./data workflows remain unaffected.
storage-check:
	@case "$(DATA_DIR)" in \
		$(USB_MOUNT)/*) \
			probe="$(DATA_DIR)/.dihi-storage-check-$$$$"; \
			if ! test -d "$(DATA_DIR)" || ! df -P "$(DATA_DIR)" >/dev/null 2>&1 || ! test -r "$(DATA_DIR)" || ! ( : > "$$probe" && rm -f "$$probe" ); then \
				echo "ERROR: configured data root is unavailable: $(DATA_DIR)"; \
				echo "Run: make usb-mount USB_DRIVE=$(USB_DRIVE) USB_MOUNT=$(USB_MOUNT)"; \
				exit 1; \
			fi; \
			;; \
		esac

# Build and start the complete Docker deployment, including the PO Token provider.
docker-up: storage-check data
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

test-docker: docker-up
	DIHI_DOCKER_TESTS=1 $(PYTEST) tests/test_docker_smoke.py -v --tb=short

# Run your app using the venv's python
run: $(VENV)/.setup-complete
	DIHI_DATA_DIR="$(DATA_DIR)" $(VENV)/bin/python src/dihi/app3.py

# Run unit tests (pure — no network, no ffmpeg, no HTTP)
test: $(VENV)/.setup-complete
	$(PIP) install -q -r requirements-dev.txt
	$(PYTEST) tests/ -v --tb=short --cov=src/dihi --cov-report=term-missing

# Stage project files while leaving local runtime data out of commits.
git-add:
	git add -u
	@git restore --staged -- data 2>/dev/null || true
	@git ls-files --others --exclude-standard | grep -Ev '^(data/)' | xargs -r git add --
	git status --short

# Stage all changes, commit them, and push the current branch.
# Usage:
#   make git-commit-push MSG="Describe the change"
git-commit-push: git-add
	@test -n "$(MSG)" || { echo 'ERROR: provide a commit message: make git-commit-push MSG="Describe the change"'; exit 1; }
	git commit -m "$(MSG)"
	git push

# Initialise host data files required by docker-compose bind mounts.
# Docker creates missing mount targets as directories; running this first
# ensures they are plain files so yt-dlp can read/write them correctly.
data: storage-check
	mkdir -p $(DATA_DIR)/media-strict $(DATA_DIR)/media-legacy $(DATA_DIR)/media-fallback $(DATA_DIR)/playlists $(DATA_DIR)/audio
	touch $(DATA_DIR)/archive.txt $(DATA_DIR)/cookies.txt $(DATA_DIR)/media-fallback/archive.txt $(DATA_DIR)/media-catalog.db

data-size:
	@echo "Data root: $(DATA_DIR)"
	@du -sh "$(DATA_DIR)" 2>/dev/null || { echo "Data directory does not exist: $(DATA_DIR)"; exit 1; }
	@du -sh "$(DATA_DIR)"/* 2>/dev/null | sort -h || true

catalog-static:
	PYTHONPATH=src/dihi $(PYTHON) src/dihi/static_catalog.py --data-root "$(DATA_DIR)"

catalog-static-serve: catalog-static
	@echo "Serving $(DATA_DIR)/static-catalog at http://localhost:$(STATIC_CATALOG_PORT)"
	$(PYTHON) -m http.server "$(STATIC_CATALOG_PORT)" --directory "$(DATA_DIR)/static-catalog"

data-migrate:
	@test -n "$(DEST_DATA_DIR)" || { echo 'Usage: make data-migrate DEST_DATA_DIR=/mnt/e/dihi-data'; exit 1; }
	@test "$(DEST_DATA_DIR)" != "$(DATA_DIR)" || { echo 'DEST_DATA_DIR must differ from DATA_DIR'; exit 1; }
	@test -d "$(DATA_DIR)" || { echo "Source data directory does not exist: $(DATA_DIR)"; exit 1; }
	mkdir -p "$(DEST_DATA_DIR)"
	@if command -v rsync >/dev/null 2>&1; then \
		rsync -r --inplace --no-perms --no-owner --no-group "$(DATA_DIR)/" "$(DEST_DATA_DIR)/"; \
	else cp -r --no-preserve=mode,ownership,timestamps "$(DATA_DIR)/." "$(DEST_DATA_DIR)/"; fi
	@echo "Copied $(DATA_DIR) to $(DEST_DATA_DIR); source was not removed."
	@du -sh "$(DEST_DATA_DIR)"

# Export YouTube cookies from your Windows browser into the configured data root (WSL2 only).
# Tries Chrome → Edge → Firefox in order; stops at the first one found.
# Run this before `docker compose up` so the bind-mount target is a real file.
#   make cookies
cookies: data $(VENV)/.setup-complete
	@if [ -d "$(_CHROME_PROF)" ]; then \
		echo "Found Chrome ($(_WIN_USER)) — exporting cookies…"; \
		$(YTDLP) --cookies-from-browser "chrome:$(_CHROME_PROF)" \
		          --cookies "$(DATA_DIR)/cookies.txt" --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	elif [ -d "$(_EDGE_PROF)" ]; then \
		echo "Found Edge ($(_WIN_USER)) — exporting cookies…"; \
		$(YTDLP) --cookies-from-browser "edge:$(_EDGE_PROF)" \
		          --cookies "$(DATA_DIR)/cookies.txt" --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	else \
		echo "Chrome/Edge not found — trying Firefox…"; \
		$(YTDLP) --cookies-from-browser firefox \
		          --cookies "$(DATA_DIR)/cookies.txt" --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	fi
	@echo "Done — cookies written to $(DATA_DIR)/cookies.txt"

# Linux / WSLg: launch a fresh browser, let you log into YouTube, then extract
# the session cookies once you close the window.
# Requires a display: WSLg on Windows 11, or an X server (VcXsrv/Xming) on W10.
# If no browser is installed, run `make install-chrome` first.
cookies-browser: data $(VENV)/.setup-complete
	@set -e; \
	[ -n "$${DISPLAY:-}$${WAYLAND_DISPLAY:-}" ] || { \
		echo "ERROR: no display detected."; \
		echo "  WSL2/Windows 11: WSLg should set DISPLAY automatically (check Windows Update)."; \
		echo "  WSL2/Windows 10: install VcXsrv, launch it, then: export DISPLAY=:0"; \
		exit 1; }; \
	TMPPROF=$$(mktemp -d /tmp/yt-login-XXXXXX); \
	BROWSER=''; TYPE=''; \
	for b in google-chrome-stable google-chrome chromium-browser chromium; do \
		if command -v $$b >/dev/null 2>&1; then BROWSER=$$b; TYPE=chrome; break; fi; \
	done; \
	if [ -z "$$BROWSER" ] && command -v firefox >/dev/null 2>&1; then \
		BROWSER=firefox; TYPE=firefox; \
	fi; \
	if [ -z "$$BROWSER" ]; then \
		rm -rf "$$TMPPROF"; \
		echo "ERROR: no browser found. Run: make install-chrome"; exit 1; \
	fi; \
	echo "Launching $$BROWSER (fresh profile — your normal browser is untouched)."; \
	echo ">>> Sign into YouTube, then CLOSE THE WINDOW to continue. <<<"; \
	if [ "$$TYPE" = chrome ]; then \
		$$BROWSER --user-data-dir="$$TMPPROF" \
			--no-first-run --no-default-browser-check --no-sandbox \
			"https://accounts.google.com/ServiceLogin?service=youtube" 2>/dev/null; \
		echo "Browser closed — extracting cookies from Chrome profile…"; \
		$(YTDLP) --cookies-from-browser "chrome:$$TMPPROF" \
			--cookies "$(DATA_DIR)/cookies.txt" --skip-download \
			"https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	else \
		FFPROF="$$TMPPROF/ffprofile"; mkdir -p "$$FFPROF"; \
		firefox --profile "$$FFPROF" --no-remote \
			"https://accounts.google.com/ServiceLogin?service=youtube" 2>/dev/null; \
		echo "Browser closed — extracting cookies from Firefox profile…"; \
		$(YTDLP) --cookies-from-browser "firefox:$$FFPROF" \
			--cookies "$(DATA_DIR)/cookies.txt" --skip-download \
			"https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	fi; \
	rm -rf "$$TMPPROF"; \
	echo "Done — cookies written to $(DATA_DIR)/cookies.txt"

# Install Google Chrome on Debian/Ubuntu (WSL2 or native Linux).
install-chrome:
	@command -v google-chrome-stable google-chrome 2>/dev/null | head -1 | grep -q chrome && \
		{ echo "Chrome already installed: $$(google-chrome --version 2>/dev/null || google-chrome-stable --version)"; exit 0; } || true
	@echo "Installing Google Chrome…"
	@curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
		| sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
	@echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] \
		https://dl.google.com/linux/chrome/deb/ stable main" \
		| sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
	sudo apt-get update -qq
	sudo apt-get install -y google-chrome-stable
	@echo "Chrome installed: $$(google-chrome-stable --version)"

# Clean up the virtual environment
clean:
	rm -rf $(VENV)

# Download a YouTube video or playlist by ID or URL:
#   make dQw4w9WgXcQ
#   make PLxxxxxxxxxxxxxx
#   make "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# Requires: make dev-install (run once after setup). The provider is started
# automatically unless DIHI_PO_TOKEN_PROVIDER_URL names an external endpoint.
.DEFAULT:
	@test -x "$(DIHI)" || { echo "Run 'make dev-install' first to install the dihi CLI."; exit 1; }
	@if [ -z "$${DIHI_PO_TOKEN_PROVIDER_URL:-}" ]; then $(MAKE) --no-print-directory pot-provider; fi
	@$(DIHI) download "$@" --archive "$(DATA_DIR)/archive.txt" --merged-dir "$(DATA_DIR)/media-strict"
