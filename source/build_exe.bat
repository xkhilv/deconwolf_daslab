@echo off
REM Builds dist\DasLabDeconwolf.exe
REM Needs 64-bit Python 3.13 (3.10 to 3.12 also work). Python 3.15 does NOT work.
REM Only needed on the PC that builds the app. The .exe runs without Python.
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
REM Use Python 3.13 if it is installed, even if a newer one is too
py -3.13 --version >nul 2>nul && set "PY=py -3.13"
%PY% --version
if errorlevel 1 (
  echo.
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)

if not exist .venv (
  %PY% -m venv .venv || goto :err
)
call .venv\Scripts\activate.bat || goto :err
python -m pip install --upgrade pip || goto :err
python -m pip install -r requirements.txt pyinstaller || goto :err

pyinstaller --noconfirm --clean --onefile --windowed --name DasLabDeconwolf ^
  --collect-all nd2 --collect-submodules ome_types DasLabDeconwolf.py || goto :err

echo.
echo Self-test of the built program:
start /wait "" dist\DasLabDeconwolf.exe --selftest
type selftest.txt
echo.
echo Done: %cd%\dist\DasLabDeconwolf.exe
pause
exit /b 0

:err
echo.
echo Build FAILED. Copy the messages above and send them to whoever maintains this tool.
pause
exit /b 1
