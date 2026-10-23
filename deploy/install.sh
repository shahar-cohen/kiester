#!/usr/bin/env bash
# Install/refresh the kie-studio systemd --user service. Idempotent. Log: deploy/install.log (safe to delete).
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=deploy/install.log
{ echo "# kie-studio install log — safe to delete"; date; } > "$LOG"
exec > >(tee -a "$LOG") 2>&1

python3 -c "import flask, PIL" || { echo "missing deps: sudo apt install python3-flask python3-pil"; exit 1; }
command -v ffmpeg >/dev/null || { echo "missing ffmpeg: sudo apt install ffmpeg"; exit 1; }
[ -s ~/.config/kie/token ] || { echo "missing API key in ~/.config/kie/token"; exit 1; }

# the old single-file UI held port 8770; stop it so the service can bind
pkill -f 'scripts/kie-video-ui.py' && echo "stopped old kie-video-ui.py" || true

mkdir -p ~/.config/systemd/user
ln -sf "$PWD/deploy/kie-studio.service" ~/.config/systemd/user/kie-studio.service
systemctl --user daemon-reload
systemctl --user enable kie-studio.service
systemctl --user restart kie-studio.service
sleep 2
systemctl --user --no-pager status kie-studio.service | head -5
curl -sf http://127.0.0.1:8770/api/config >/dev/null && echo "OK: http://127.0.0.1:8770"
