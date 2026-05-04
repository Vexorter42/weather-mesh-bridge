@echo off
:: Перезапускает службу weather-mesh-bridge на Pi.

set "PI_USER=pi"
set "PI_HOST=192.168.0.225"

chcp 65001 >nul
title Перезапуск бота — %PI_HOST%

echo Перезапускаю weather-mesh-bridge на %PI_HOST%...
echo.

ssh -t %PI_USER%@%PI_HOST% "sudo systemctl restart weather-mesh-bridge && sudo systemctl status weather-mesh-bridge --no-pager -l"

echo.
echo Готово. Закрой окно или нажми любую клавишу.
pause >nul
