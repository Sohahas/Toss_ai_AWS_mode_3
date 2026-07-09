#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
  echo ".env file was not found."
  echo "Copy .env.aws.example to .env and fill your real values first."
  exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
  echo "Python virtual environment was not found."
  echo "Run ./setup_aws_worker.sh first."
  exit 1
fi

echo "AI Stock Assistant - AWS worker starting"
echo "Keep this process running. It handles Toss orders, account refresh, AI analysis, and Telegram alerts."
echo

source .venv/bin/activate
python -m app.worker
