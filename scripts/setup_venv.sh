#!/usr/bin/env bash
# Quick setup: create venv and install all dependencies
set -e
cd "$(dirname "$0")/.."

python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q

cp -n .env.example .env 2>/dev/null && echo "Created .env from example — fill in your keys" || true
echo "Setup complete. Edit .env then run: venv/bin/python main.py"
