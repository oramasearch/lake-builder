# SPDX-License-Identifier: MIT

PYTHON ?= .venv/bin/python
RUFF ?= .venv/bin/ruff

IMAGE ?= lake-builder:check

.PHONY: proof format image-check

proof:
	$(RUFF) check src tests
	$(RUFF) format --check src tests
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

format:
	$(RUFF) format src tests

image-check:
	docker buildx build --load -t $(IMAGE) .
	python3 tools/image-files.py $(IMAGE)
