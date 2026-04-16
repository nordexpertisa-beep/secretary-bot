#!/bin/bash
set -e
SSH="ssh -i ~/.ssh/anonymizer_deploy ann@193.187.94.87"
SCP="scp -i ~/.ssh/anonymizer_deploy"

echo "==> Deploying secretary-bot to server..."

# Copy files
$SSH "mkdir -p /home/ann/secretary-bot"
$SCP bot.py requirements.txt ann@193.187.94.87:/home/ann/secretary-bot/

# Setup venv + deps
$SSH "cd /home/ann/secretary-bot && python3 -m venv venv && venv/bin/pip install -q -r requirements.txt"

# Install systemd service
$SCP secretary.service ann@193.187.94.87:/tmp/
$SSH "sudo mv /tmp/secretary.service /etc/systemd/system/anonymizer-secretary.service && sudo systemctl daemon-reload"

echo "==> Done. Set /home/ann/secretary-bot/.env and run: sudo systemctl enable --now anonymizer-secretary"
