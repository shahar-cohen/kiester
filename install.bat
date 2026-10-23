@echo off
rem Kiester installer for Windows - double-click. Installs Python with winget if needed, then opens the installer window.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Kiester installer

set "PYEXE="
rem The "py" launcher avoids the Microsoft Store "python" alias, which only opens the Store.
for %%V in (3.13 3.12 3.11 3.10 3.9 3) do (
  if not defined PYEXE (
    for /f "delims=" %%i in ('py -%%V -c "import sys;assert sys.version_info>=(3,9);print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
  )
)
if not defined PYEXE (
  for /f "delims=" %%i in ('python -c "import sys;assert sys.version_info>=(3,9);print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
)
if not defined PYEXE (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
)

if not defined PYEXE (
  echo Python was not found. Installing Python 3.13 for your user ^(no admin needed^)...
  where winget >nul 2>nul
  if errorlevel 1 goto :nopython
  winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PYEXE=%%D\python.exe"
)
if not defined PYEXE goto :nopython

echo Using Python: %PYEXE%
set "PYW=%PYEXE:python.exe=pythonw.exe%"
"%PYEXE%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo ^(no window toolkit in this Python - using text mode^)
  "%PYEXE%" deploy\installer.py --console %*
  pause
  exit /b
)
if exist "%PYW%" (
  start "" "%PYW%" deploy\installer.py %*
) else (
  "%PYEXE%" deploy\installer.py %*
)
exit /b

:nopython
echo.
echo Could not install Python automatically.
echo The download page will open: install Python 3 ^(tick "Add python.exe to PATH"^), then run install.bat again.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1
