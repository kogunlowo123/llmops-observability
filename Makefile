# Thin wrapper around tasks.py so `make` and `python tasks.py` never diverge.
# tasks.py is the source of truth and is the supported entry point on Windows,
# where make is usually not installed at all.

PY ?= python

.DEFAULT_GOAL := help
.PHONY: help setup fmt lint typecheck test test-unit test-integration \
        test-security test-e2e test-meta gate check-incidents cost doctor \
        corpus corpus-check examples site security docker-build smoke all

help:
	@$(PY) tasks.py --list

setup fmt lint typecheck test test-unit test-integration test-security \
test-e2e test-meta gate check-incidents cost doctor corpus corpus-check \
examples site security docker-build smoke all:
	@$(PY) tasks.py $@
