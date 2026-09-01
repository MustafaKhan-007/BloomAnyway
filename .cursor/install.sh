#!/usr/bin/env bash
# Idempotent repository bootstrap for the Bloom Anyway Flask app.
# Creates a virtualenv, installs dependencies, then prepares the local
# SQLite dev database (migrations + idempotent content seed). Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

# python3-venv is required to create virtualenvs on Debian/Ubuntu images.
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y python3-venv >/dev/null
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Local dev uses SQLite (no DATABASE_URL) and the built-in DevConfig defaults,
# so no secrets are needed to boot. Prepare the schema and seed content.
export FLASK_APP=app:create_app
flask db upgrade
python seed.py
