#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
SERVICE="circles-archiver"
USER_NAME="${SUDO_USER:-$(id -un)}"
if [[ ! -x "$PYTHON" ]]; then echo "Сначала запусти ./setup.sh" >&2; exit 2; fi
if [[ ! -f "$ROOT/config.json" ]]; then echo "Сначала создай config.json: .venv/bin/python combined_multi_account.py setup" >&2; exit 2; fi
if [[ $EUID -ne 0 ]]; then exec sudo -E bash "$0" "$@"; fi
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=CIRCLES CONTROL Telegram Manager
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${ROOT}
Environment=PYTHONUTF8=1
Environment=PYTHONIOENCODING=utf-8
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
ExecStart=${PYTHON} ${ROOT}/combined_multi_account.py run
Restart=always
RestartSec=10
RestartPreventExitStatus=2 3 4
TimeoutStopSec=45
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now "$SERVICE"
systemctl --no-pager --full status "$SERVICE"
echo "Установка systemd завершена. Логи: journalctl -u $SERVICE -f"