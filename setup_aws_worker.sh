#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "AI Stock Assistant - AWS worker setup"
python3 --version

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Setup complete."
echo "Next:"
echo "1) cp .env.aws.example .env"
echo "2) nano .env"
echo "3) ./run_aws_worker.sh"
