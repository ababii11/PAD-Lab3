#!/usr/bin/env bash
# One-time setup on Ubuntu 24.04 (WSL2 on Windows, or plain Linux):
#   HAProxy, MongoDB 8.0, and a Python virtual environment with the dependencies.
#
#   bash scripts/install-wsl.sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

sudo apt-get update
sudo apt-get install -y haproxy python3-venv python3-pip curl gnupg

if ! command -v mongod >/dev/null; then
  # Official MongoDB repository (MongoDB is not in Ubuntu's own repositories).
  curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/mongodb-server-8.0.gpg
  echo "deb [ arch=amd64,arm64 signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.0 multiverse" \
    | sudo tee /etc/apt/sources.list.d/mongodb-org-8.0.list
  sudo apt-get update
  sudo apt-get install -y mongodb-org
fi

# The packages install system services; our scripts start their own instances
# (one mongod per node, our own haproxy.cfg), so keep the system ones off.
sudo systemctl disable --now mongod 2>/dev/null || true
sudo systemctl disable --now haproxy 2>/dev/null || true

# Virtual environment on the Linux file system (fast even if the project is on C:).
python3 -m venv "$HOME/.venvs/pad3"
"$HOME/.venvs/pad3/bin/pip" install --upgrade pip
"$HOME/.venvs/pad3/bin/pip" install -r "$ROOT/requirements.txt"

echo
haproxy -v | head -1
mongod --version | head -1
echo "Setup done. Start everything with:  bash scripts/start-all.sh"
