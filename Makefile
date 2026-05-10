# Variables
VENV := venv
PYTHON := python3
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
DIHI := $(VENV)/bin/dihi
YTDLP := $(VENV)/bin/yt-dlp

# WSL2: detect Windows username and locate browser profiles on the host C: drive
_WIN_USER     := $(shell cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n')
_CHROME_PROF  := /mnt/c/Users/$(_WIN_USER)/AppData/Local/Google/Chrome/User Data
_EDGE_PROF    := /mnt/c/Users/$(_WIN_USER)/AppData/Local/Microsoft/Edge/User Data

.PHONY: setup install dev-install run clean test data cookies cookies-browser install-chrome

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

# Initialise host data files required by docker-compose bind mounts.
# Docker creates missing mount targets as directories; running this first
# ensures they are plain files so yt-dlp can read/write them correctly.
data:
	mkdir -p data/merged
	touch data/archive.txt data/cookies.txt

# Export YouTube cookies from your Windows browser into data/cookies.txt (WSL2 only).
# Tries Chrome → Edge → Firefox in order; stops at the first one found.
# Run this before `docker compose up` so the bind-mount target is a real file.
#   make cookies
cookies: data $(VENV)/bin/activate
	@rm -f data/cookies.txt; \
	if [ -d "$(_CHROME_PROF)" ]; then \
		echo "Found Chrome ($(_WIN_USER)) — exporting cookies…"; \
		$(YTDLP) --cookies-from-browser "chrome:$(_CHROME_PROF)" \
		          --cookies data/cookies.txt --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	elif [ -d "$(_EDGE_PROF)" ]; then \
		echo "Found Edge ($(_WIN_USER)) — exporting cookies…"; \
		$(YTDLP) --cookies-from-browser "edge:$(_EDGE_PROF)" \
		          --cookies data/cookies.txt --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	else \
		echo "Chrome/Edge not found — trying Firefox…"; \
		$(YTDLP) --cookies-from-browser firefox \
		          --cookies data/cookies.txt --skip-download \
		          "https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	fi
	@echo "Done — cookies written to data/cookies.txt"

# Linux / WSLg: launch a fresh browser, let you log into YouTube, then extract
# the session cookies once you close the window.
# Requires a display: WSLg on Windows 11, or an X server (VcXsrv/Xming) on W10.
# If no browser is installed, run `make install-chrome` first.
cookies-browser: data $(VENV)/bin/activate
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
		rm -f data/cookies.txt; \
		$(YTDLP) --cookies-from-browser "chrome:$$TMPPROF" \
			--cookies data/cookies.txt --skip-download \
			"https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	else \
		FFPROF="$$TMPPROF/ffprofile"; mkdir -p "$$FFPROF"; \
		firefox --profile "$$FFPROF" --no-remote \
			"https://accounts.google.com/ServiceLogin?service=youtube" 2>/dev/null; \
		echo "Browser closed — extracting cookies from Firefox profile…"; \
		rm -f data/cookies.txt; \
		$(YTDLP) --cookies-from-browser "firefox:$$FFPROF" \
			--cookies data/cookies.txt --skip-download \
			"https://www.youtube.com/watch?v=dQw4w9WgXcQ"; \
	fi; \
	rm -rf "$$TMPPROF"; \
	echo "Done — cookies written to data/cookies.txt"

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
# Requires: make dev-install (run once after setup)
.DEFAULT:
	@test -x "$(DIHI)" || { echo "Run 'make dev-install' first to install the dihi CLI."; exit 1; }
	@$(DIHI) download "$@"
