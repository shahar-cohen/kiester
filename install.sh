#!/usr/bin/env bash
# Kiester installer for Linux: makes sure Python 3.9+, venv and Tk exist, then opens the installer window.
#   ./install.sh               install / update (window, or text mode without a desktop)
#   ./install.sh --console     text mode
#   ./install.sh --uninstall --console
# Log: install.log next to this file (safe to delete).
set -uo pipefail
cd "$(dirname "$0")"
{ echo "# Kiester install log — safe to delete"; date; } > install.log
exec > >(tee -a install.log) 2>&1

need=()
PY=$(command -v python3 || true)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then need+=(python); fi
if [ -n "$PY" ] && ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then need+=(venv); fi
gui=1; [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && gui=0
if [ $gui = 1 ] && [ -n "$PY" ] && ! "$PY" -c 'import tkinter' 2>/dev/null; then need+=(tk); fi

# double-clicked from a file manager there is no terminal for sudo's password prompt: use the graphical one
if [ -t 0 ]; then SUDO=sudo; elif command -v pkexec >/dev/null; then SUDO=pkexec; else SUDO=sudo; fi

if [ ${#need[@]} -gt 0 ]; then
  echo "Installing system packages for: ${need[*]} (asks for your password)"
  if command -v apt-get >/dev/null; then
    pk=(); for n in "${need[@]}"; do case $n in python) pk+=(python3);; venv) pk+=(python3-venv);; tk) pk+=(python3-tk);; esac; done
    $SUDO apt-get install -y "${pk[@]}"
  elif command -v dnf >/dev/null; then
    pk=(); for n in "${need[@]}"; do case $n in python) pk+=(python3);; tk) pk+=(python3-tkinter);; esac; done
    [ ${#pk[@]} -gt 0 ] && $SUDO dnf install -y "${pk[@]}"
  elif command -v pacman >/dev/null; then
    pk=(); for n in "${need[@]}"; do case $n in python) pk+=(python);; tk) pk+=(tk);; esac; done
    [ ${#pk[@]} -gt 0 ] && $SUDO pacman -S --needed --noconfirm "${pk[@]}"
  elif command -v zypper >/dev/null; then
    pk=(); for n in "${need[@]}"; do case $n in python) pk+=(python3);; tk) pk+=(python3-tk);; esac; done
    [ ${#pk[@]} -gt 0 ] && $SUDO zypper install -y "${pk[@]}"
  else
    echo "Please install Python 3.9+ (with venv and tkinter) using your package manager, then run this again."; exit 1
  fi
  [ $? -ne 0 ] && echo "(could not install system packages; continuing — without tkinter the installer runs in text mode)"
  PY=$(command -v python3)
  [ -z "$PY" ] && { echo "Python 3 is still missing. Install it with your package manager and run this again."; exit 1; }
fi

"$PY" -c 'import tkinter' 2>/dev/null || gui=0
if [ $gui = 1 ] && [[ " $* " != *" --console "* ]]; then
  exec "$PY" deploy/installer.py "$@"
else
  exec "$PY" deploy/installer.py --console "$@"
fi
