#!/bin/sh
# Lint and tests, the same way the pipeline runs them. Needs Python 3.11+.
set -eu
pip install --quiet --disable-pip-version-check --root-user-action=ignore -r requirements-dev.txt
ruff check .
python -m pytest -q tests
