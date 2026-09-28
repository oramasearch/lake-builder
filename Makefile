PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff

.PHONY: proof format

proof:
	$(RUFF) check src tests
	$(RUFF) format --check src tests
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

format:
	$(RUFF) format src tests
