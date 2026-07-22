#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="${HOME}/.config/ai-stock-assistant/worker.env"
VENV_DIR="${HOME}/.venvs/ai-stock-assistant"
SERVICE_NAME="ai-stock-worker"

cd "$REPO_DIR"

if [ ! -f "$ENV_FILE" ] || [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "The safe AWS layout is not installed. Run ./harden_aws_worker.sh first."
  exit 1
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Tracked source files were changed on AWS. Update stopped without overwriting them."
  echo "Run: git status --short"
  exit 1
fi

git fetch origin main
if ! git merge --ff-only origin/main; then
  echo "The AWS branch and GitHub branch have diverged. No files were overwritten."
  echo "Do not use stash or reset. Check: git log --oneline --left-right HEAD...origin/main"
  exit 1
fi

"$VENV_DIR/bin/python" -m pip install -r requirements.txt
AI_STOCK_ENV_FILE="$ENV_FILE" PYTHONDONTWRITEBYTECODE=1 "$VENV_DIR/bin/python" -c "import app.worker"

sudo systemctl restart "$SERVICE_NAME"
sleep 2
if ! sudo systemctl is-active --quiet "$SERVICE_NAME"; then
  sudo systemctl status "$SERVICE_NAME" --no-pager -l || true
  sudo journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
  exit 1
fi

sudo systemctl status "$SERVICE_NAME" --no-pager -l
