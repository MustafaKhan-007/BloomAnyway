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
# Linting isn't pinned in requirements.txt, but the codebase is checked with it.
pip install ruff

# Local dev uses SQLite (no DATABASE_URL) and the built-in DevConfig defaults,
# so no secrets are needed to boot. Prepare the schema and seed content.
export FLASK_APP=app:create_app
if ! flask db upgrade; then
  # A leftover dev database whose schema was built outside migrations can't be
  # upgraded onto, and would block every install from here on. It is a
  # disposable, gitignored SQLite file, so move it aside — don't delete it —
  # and build a clean one. Stamping it instead would claim a version it hasn't
  # got and fail later, on a missing column, further from the cause.
  # A relative sqlite path is resolved against the instance folder, not the
  # working directory, so ask the app where the file really is.
  stale=$(python -c "
import os
from app import create_app
from app.config import DevConfig
app = create_app(DevConfig)
uri = app.config['SQLALCHEMY_DATABASE_URI']
path = uri[10:] if uri.startswith('sqlite:///') else ''
print(path if os.path.isabs(path) or not path
      else os.path.join(app.instance_path, path))" 2>/dev/null || true)
  if [ -n "$stale" ] && [ -f "$stale" ]; then
    mv "$stale" "$stale.unmigratable.$(date +%s)"
    echo "Set aside a dev database that could not be migrated: $stale.unmigratable.*"
    flask db upgrade
  else
    exit 1
  fi
fi
python seed.py
