#!/usr/bin/env bash
# Установка на Raspberry Pi 4 (Raspberry Pi OS / Debian 12).
# Запускать из папки проекта:
#     bash install.sh

set -euo pipefail

if ! command -v python3 >/dev/null; then
  echo "Установи Python 3.10+ сначала: sudo apt install -y python3 python3-venv python3-pip"
  exit 1
fi

# 1) права на USB-Serial (нужно перелогиниться после первого запуска)
if ! id -nG "$USER" | grep -qw dialout; then
  echo "Добавляю $USER в группу dialout (для доступа к /dev/ttyUSB*)"
  sudo usermod -aG dialout "$USER"
  echo ">>> Выйди и зайди заново (или перезагрузи Pi), чтобы изменения применились."
fi

# 2) виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Готово. Запусти руками:    source .venv/bin/activate && python app.py"
echo "Или установи как сервис:"
echo "  sudo cp weather-mesh-bridge.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now weather-mesh-bridge"
echo
echo "UI откроется на http://$(hostname -I | awk '{print $1}'):5000"
