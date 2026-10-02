@echo off
chcp 65001 >nul
rem Установка Tollgate: зависимости, ярлык в автозагрузке, запуск
cd /d "%~dp0"
set "TG=%~dp0"

where python >nul 2>nul || (
    echo Не найден Python 3.10+. Установите с python.org с галкой "Add python.exe to PATH".
    pause
    exit /b 1
)

python -m pip install --user -r requirements.txt || (pause & exit /b 1)

rem статус-строка Claude Code - свежие лимиты после каждого ответа (свою статус-строку не перетирает)
python statusline.py --install

rem pythonw из той же установки Python, куда только что поставили зависимости
for /f "delims=" %%P in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%P"

powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Tollgate.lnk'); $s.TargetPath=$env:PYW; $s.Arguments=[char]34+$env:TG+'tollgate.py'+[char]34; $s.WorkingDirectory=$env:TG; $s.Save()"

start "" "%PYW%" "%TG%tollgate.py"
echo Готово: Tollgate запущен (иконка у часов) и добавлен в автозагрузку.
pause
