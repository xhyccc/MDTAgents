.PHONY: install install-dev install-ui test test-unit test-int lint run ui help

PYTHON   ?= python3
PYTEST   ?= $(PYTHON) -m pytest
CASE     ?= cases/demo_case

# ── Default target ────────────────────────────────────────────────────────
help:
	@echo "MDT-Orchestrator — available make targets:"
	@echo ""
	@echo "  make install       Install core Python dependencies"
	@echo "  make install-dev   Install core + dev (pytest) dependencies"
	@echo "  make install-ui    Install core + UI (Streamlit) dependencies"
	@echo "  make setup         Run full setup script (installs opencode if absent)"
	@echo "  make setup-ui      Run full setup script with UI support"
	@echo ""
	@echo "  make test          Run all tests (unit + integration)"
	@echo "  make test-unit     Run unit tests only"
	@echo "  make test-int      Run integration tests only"
	@echo "  make lint          Syntax-check all source files"
	@echo ""
	@echo "  make run           Run MDT on CASE (default: $(CASE))"
	@echo "  make ui            Launch Streamlit web UI"
	@echo ""
	@echo "Override case folder:  make run CASE=cases/my_case"

# ── Installation ──────────────────────────────────────────────────────────
install:
	$(PYTHON) -m pip install --quiet -r requirements.txt

install-dev: install
	$(PYTHON) -m pip install --quiet -r requirements-dev.txt

install-ui: install
	$(PYTHON) -m pip install --quiet -r requirements-ui.txt

setup:
	bash scripts/setup.sh

setup-ui:
	bash scripts/setup.sh --with-ui

# ── Testing ───────────────────────────────────────────────────────────────
test: install-dev
	$(PYTEST) tests/ -v

test-unit: install-dev
	$(PYTEST) tests/unit/ -v

test-int: install-dev
	$(PYTEST) tests/integration/ -v

# ── Lint (syntax only — no external linter required) ─────────────────────
lint:
	@echo "Checking Python syntax …"
	@$(PYTHON) -m py_compile src/scanner.py       && echo "  ✓ scanner.py"
	@$(PYTHON) -m py_compile src/file_bus.py      && echo "  ✓ file_bus.py"
	@$(PYTHON) -m py_compile src/cli_client.py    && echo "  ✓ cli_client.py"
	@$(PYTHON) -m py_compile src/coordinator.py   && echo "  ✓ coordinator.py"
	@$(PYTHON) -m py_compile src/specialist_pool.py && echo "  ✓ specialist_pool.py"
	@$(PYTHON) -m py_compile src/main.py          && echo "  ✓ main.py"
	@echo "All files OK."

# ── Run ───────────────────────────────────────────────────────────────────
run:
	$(PYTHON) -m src.main $(CASE)

# ── UI ────────────────────────────────────────────────────────────────────
ui:
	streamlit run app.py
