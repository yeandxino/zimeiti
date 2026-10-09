@echo off
chcp 65001 >nul 2>&1
title Xinmeiti Workflow
cd /d "%~dp0"

echo.
echo ============================================================
echo   Starting... please wait  /  正在启动，请稍等
echo ============================================================
echo.

rem ---------- 1. locate Python ----------
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  echo   [X] Python not found.
  echo.
  echo   Please install Python first:  https://www.python.org/downloads/
  echo   IMPORTANT: check "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

rem ---------- 2. check deps, install if missing ----------
%PY% -c "import imageio_ffmpeg, PIL, pyJianYingDraft" >nul 2>&1
if errorlevel 1 (
  echo   First run: preparing components, about 1-3 minutes...
  echo.
  %PY% -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>&1
  %PY% -m pip install imageio-ffmpeg pillow pyJianYingDraft --quiet --disable-pip-version-check
  echo.
  %PY% -c "import imageio_ffmpeg, PIL, pyJianYingDraft" >nul 2>&1
  if errorlevel 1 (
    echo   [!] Some components failed to install.
    echo       Auto video-editing may be unavailable.
    echo       Text generation still works.
    echo.
  ) else (
    echo   [OK] Components ready.
    echo.
  )
)

rem ---------- 3. run server ----------
%PY% "%~dp0workflow_server.py"

echo.
echo   Server stopped. If this was not expected, screenshot the error above.
pause
