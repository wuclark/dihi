# Variables
VENV := venv
PYTHON := python3
PIP := $(VENV)/bin/pip

.PHONY: setup install run clean

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

# Run your app using the venv's python
run: $(VENV)/bin/activate
	$(VENV)/bin/python main.py

# Clean up the virtual environment
clean:
	rm -rf $(VENV)
