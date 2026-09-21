#!/usr/bin/env bash
# One-shot server setup for the GCP e2-micro (Debian 12) deploy.
# Run as root from the cloned repo:  sudo deploy/setup.sh
# Re-running after a git pull re-runs the whole script; the venv step is
# skipped if it already exists, and the service is restarted at the end.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE=/opt/mcp-memory

echo "== mcp-memory-bridge: server setup =="
echo "   repo: $REPO_DIR"

# --- user & directories -------------------------------------------------
id -u memory &>/dev/null || useradd --system --home "$BASE" --shell /usr/sbin/nologin memory
mkdir -p "$BASE/data" "$BASE/venv"
# The service reads its env file; the data dir must be writable by it.
chown -R memory:memory "$BASE/data"
touch "$BASE/env"
chmod 600 "$BASE/env"
chown memory:memory "$BASE/env"

# --- lean Python env (no ML stack on a 1GB-RAM VM) ----------------------
if [ ! -x "$BASE/venv/bin/python" ]; then
  echo "== creating venv (first run only) =="
  python3 -m venv "$BASE/venv"
  "$BASE/venv/bin/pip" install --quiet --upgrade pip
  # Lean mode, matching the Dockerfile: only the two runtime deps.
  "$BASE/venv/bin/pip" install --quiet "mcp==2.2.0" "numpy==2.2.6"
fi

# --- systemd service ----------------------------------------------------
cp "$REPO_DIR/deploy/memory-server.service" /etc/systemd/system/memory-server.service
systemctl daemon-reload
systemctl enable memory-server.service

# --- cloudflared (tunnel) ----------------------------------------------
if [ ! -x /usr/local/bin/cloudflared ]; then
  echo "== installing cloudflared =="
  curl -fsSL -o /tmp/cloudflared.deb \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  dpkg -i /tmp/cloudflared.deb
fi
mkdir -p /etc/cloudflared
# Owns the tunnel; config written by docs/DEPLOY.md step 4.
chown -R memory:memory /etc/cloudflared

# --- start --------------------------------------------------------------
echo "== restarting memory-server =="
systemctl restart memory-server.service
systemctl --no-pager status memory-server.service | head -12 || true

echo
echo "Next (as the tunnel user): docs/DEPLOY.md, step 4 — cloudflared tunnel login."
