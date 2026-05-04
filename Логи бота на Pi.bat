@echo off
:: Подключается к Pi и сразу показывает живые логи бота.
:: Закрытие — Ctrl+C, потом любая клавиша.

set "PI_USER=pi"
set "PI_HOST=192.168.0.225"

chcp 65001 >nul
title Логи бота — %PI_HOST%

echo Подключаюсь к %PI_HOST% и читаю логи...
echo Выход: Ctrl+C
echo.

ssh -t %PI_USER%@%PI_HOST% "sudo journalctl -u weather-mesh-bridge -f"

echo.
pause >nul
