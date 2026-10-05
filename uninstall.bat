@echo off
chcp 65001 >nul
rem Удаление Tollgate: остановить виджет, убрать автозапуск и статус-строку Claude Code
cd /d "%~dp0"

rem остановить работающий виджет
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -like '*tollgate.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }"

rem ярлык автозапуска
powershell -NoProfile -Command "Remove-Item -LiteralPath ([Environment]::GetFolderPath('Startup')+'\Tollgate.lnk') -ErrorAction SilentlyContinue"

rem статус-строка Claude Code: убирается, только если это статус-строка Tollgate (с бэкапом settings.json)
where python >nul 2>nul && python statusline.py --uninstall

echo Готово: Tollgate остановлен и убран из автозагрузки.
echo Данные виджета остались в %USERPROFILE%\.tollgate - эту папку и папку проекта можно удалить вручную.
pause
