#!/bin/bash
# Kiester installer for macOS — double-click in Finder.
# If macOS says it "cannot be opened": right-click (or Control-click) the file → Open → Open.
cd "$(dirname "$0")"
{ echo "# Kiester install log — safe to delete"; date; } > install.log
exec > >(tee -a install.log) 2>&1

ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; }
PY=""
# python.org and Homebrew builds first; /usr/bin/python3 is only a stub unless the Xcode command line tools are installed
for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
  [ -x "$c" ] || continue
  [ "$c" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1 && continue
  if ok "$c"; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  osascript -e 'display dialog "Kiester needs Python 3 (free).\n\nThe download page will open now. Install Python, then double-click install.command again." buttons {"OK"} default button 1 with title "Kiester"' >/dev/null
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

if "$PY" -c 'import tkinter; tkinter.Tk().destroy()' 2>/dev/null; then
  "$PY" deploy/installer.py "$@"
else
  echo "(Python at $PY has no window toolkit — using text mode. Tip: the python.org installer includes it.)"
  "$PY" deploy/installer.py --console "$@"
fi
