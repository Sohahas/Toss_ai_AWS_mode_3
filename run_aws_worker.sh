#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="${AI_STOCK_ENV_FILE:-${HOME}/.config/ai-stock-assistant/worker.env}"
VENV_DIR="${HOME}/.venvs/ai-stock-assistant"
cd "$REPO_DIR"

if [ ! -f "$ENV_FILE" ]; then
  echo "AWS worker environment file was not found: $ENV_FILE"
  echo "Run ./harden_aws_worker.sh first."
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "AWS worker virtual environment was not found: $VENV_DIR"
  echo "Run ./harden_aws_worker.sh first."
  exit 1
fi

echo "AI Stock Assistant - AWS worker starting"
echo "Keep this process running. It handles Toss orders, account refresh, AI analysis, and Telegram alerts."
echo

export AI_STOCK_ENV_FILE="$ENV_FILE"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUNBUFFERED=1
exec "$VENV_DIR/bin/python" -m app.worker
