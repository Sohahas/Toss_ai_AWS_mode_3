#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CONFIG_DIR="${HOME}/.config/ai-stock-assistant"
ENV_FILE="${CONFIG_DIR}/worker.env"
VENV_DIR="${HOME}/.venvs/ai-stock-assistant"
SERVICE_NAME="ai-stock-worker"

echo "AI Stock Assistant - safe AWS layout setup"
echo "Source: $REPO_DIR"
echo "Settings: $ENV_FILE"
echo "Python: $VENV_DIR"

mkdir -p "$CONFIG_DIR" "$(dirname "$VENV_DIR")"
chmod 700 "$CONFIG_DIR"

if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$REPO_DIR/.env" ]; then
    install -m 600 "$REPO_DIR/.env" "$ENV_FILE"
    echo "Existing .env was copied to the protected settings directory."
  else
    install -m 600 "$REPO_DIR/.env.aws.example" "$ENV_FILE"
    echo "A settings template was created at: $ENV_FILE"
    echo "Fill in the real values, then run this script again:"
    echo "nano $ENV_FILE"
    exit 2
  fi
fi
chmod 600 "$ENV_FILE"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"

SERVICE_FILE="$(mktemp)"
trap 'rm -f "$SERVICE_FILE"' EXIT
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=AI Stock Assistant AWS Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=${ENV_FILE}
Environment=AI_STOCK_ENV_FILE=${ENV_FILE}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python -m app.worker
Restart=always
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

sudo install -m 644 "$SERVICE_FILE" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager -l

echo
echo "Safe layout is active. Git updates no longer depend on repository .env or .venv."
echo "Future updates: ./update_aws_worker.sh"
