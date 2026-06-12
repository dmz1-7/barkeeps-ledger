#!/usr/bin/env bash
# Convenience launcher: create venv, install deps, run.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
  # Bootstrap pip if the system Python ships without ensurepip (some distros).
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python /tmp/get-pip.py
  fi
fi

.venv/bin/pip install -q -r requirements.txt
# Local dev convenience: allow running without a passcode on localhost. For a
# public/remote deploy set APP_PASSWORD instead (see run_remote.sh / README).
export ALLOW_OPEN="${ALLOW_OPEN:-1}"
exec .venv/bin/python app.py
