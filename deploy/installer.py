#!/usr/bin/env python3
"""kie studio installer — Windows, macOS and Linux. Standard library only.

Started by install.bat / install.command / install.sh (which first make sure Python 3.9+ exists).
Shows a window (Tk); falls back to questions in the terminal when Tk is unavailable or with --console.

What it does, all inside the user's account (no admin rights):
  1. creates a private Python environment in <repo>/.venv and installs flask, pillow, imageio-ffmpeg
  2. adds "kie studio" to the app menu / Start menu and optionally the desktop
     (the shortcut starts the server if needed and opens it in a Chrome/Edge app window)
  3. optionally starts the server automatically at login (systemd --user / LaunchAgent / Startup folder)
  4. optionally saves the kie.ai API key (it can also be entered later in the app)
Uninstall removes 2, 3 and .venv; generated media, history and the API key are kept.
"""
import os, queue, shutil, subprocess, sys, threading, time, urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(REPO, ".venv")
WIN, MAC = os.name == "nt", sys.platform == "darwin"
VPY = os.path.join(VENV, "Scripts", "python.exe") if WIN else os.path.join(VENV, "bin", "python")
VPYW = os.path.join(VENV, "Scripts", "pythonw.exe") if WIN else VPY
ICONS = os.path.join(VENV, "kie-icons")
PORT = os.environ.get("KIE_STUDIO_PORT", "8770")
NAME = "kie studio"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
sys.path.insert(0, REPO)   # kie_studio.client is stdlib-only, so the key can be checked before deps exist


class Step(Exception): pass


def run(cmd, log, check=True, **kw):
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                         creationflags=NO_WINDOW, **kw)
    out = []
    for line in p.stdout:
        line = line.rstrip(); out.append(line)
        if line: log("  " + line)
    p.wait()
    if check and p.returncode: raise Step(f"command failed ({p.returncode}): {cmd[0]} … — see the log above")
    return p.returncode, "\n".join(out)


def server_up():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/config", timeout=2) as r: return r.status == 200
    except Exception: return False


def stop_server(log):
    """Stop a server we may have started earlier so the new environment takes over (best effort)."""
    if not WIN and not MAC and shutil.which("systemctl"):
        run(["systemctl", "--user", "stop", "kie-studio.service"], log, check=False)
    if MAC:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.kie.studio"], log, check=False)
    if server_up():
        try:   # detached `open`-started servers: find the PID listening on our port
            if WIN:
                _, out = run(["netstat", "-ano", "-p", "TCP"], lambda *_: None, check=False)
                pids = {l.split()[-1] for l in out.splitlines() if f"127.0.0.1:{PORT} " in l and "LISTENING" in l}
                for pid in pids: run(["taskkill", "/PID", pid, "/F"], log, check=False)
            elif shutil.which("lsof"):
                _, out = run(["lsof", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"], lambda *_: None, check=False)
                for pid in out.split(): run(["kill", pid], log, check=False)
        except Exception as e: log(f"  (could not stop running server: {e})")
        for _ in range(20):
            if not server_up(): break
            time.sleep(0.25)


# ------------------------------------------------------------------ install steps
def make_venv(log):
    if sys.version_info < (3, 9): raise Step(f"Python 3.9 or newer is needed (this is {sys.version.split()[0]}).")
    if os.path.exists(VPY):
        log("Python environment already exists — updating it.")
    else:
        log(f"Creating a private Python environment in {VENV}")
        exe = sys.executable
        if WIN and exe.lower().endswith("pythonw.exe"): exe = exe[:-5] + ".exe"
        code, out = run([exe, "-m", "venv", VENV], log, check=False)
        if code:
            shutil.rmtree(VENV, ignore_errors=True)
            if "ensurepip" in out:
                raise Step("Python's venv module is missing. On Debian/Ubuntu run:  sudo apt install python3-venv  — then run the installer again.")
            raise Step("Could not create the Python environment (see the log).")
    run([VPY, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"], log, check=False)
    run([VPY, "-m", "pip", "install", "--disable-pip-version-check", "-r", os.path.join(REPO, "requirements.txt")], log)
    run([VPY, "-c", "import flask, PIL, shutil, imageio_ffmpeg; "
                    "f = shutil.which('ffmpeg') or imageio_ffmpeg.get_ffmpeg_exe(); print('ffmpeg:', f)"], log, cwd=REPO)
    run([VPY, "-m", "kie_studio", "icons", ICONS], log, cwd=REPO)


def desktop_dir():
    if WIN:
        _, out = run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"], lambda *_: None, check=False)
        return out.strip().splitlines()[-1] if out.strip() else os.path.expanduser("~/Desktop")
    if not MAC and shutil.which("xdg-user-dir"):
        _, out = run(["xdg-user-dir", "DESKTOP"], lambda *_: None, check=False)
        if out.strip(): return out.strip()
    return os.path.expanduser("~/Desktop")


def win_folder(name):
    _, out = run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{name}')"], lambda *_: None, check=False)
    return out.strip().splitlines()[-1]


def win_shortcut(path, target, args, log, icon=None):
    sq = lambda v: "'" + str(v).replace("'", "''") + "'"   # PowerShell single-quoted literal
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({sq(path)});$s.TargetPath={sq(target)};"
          f"$s.Arguments={sq(args)};$s.WorkingDirectory={sq(REPO)};"
          + (f"$s.IconLocation={sq(icon + ',0')};" if icon else "") + "$s.Save()")
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], log)


def linux_desktop_entry(exec_args, name=NAME, comment="Generate videos and images with kie.ai"):
    cmd = " ".join(f'"{a}"' for a in exec_args)
    return (f"[Desktop Entry]\nType=Application\nName={name}\nComment={comment}\nExec={cmd}\n"
            f"Path={REPO}\nIcon={os.path.join(ICONS, 'kie-studio.png')}\nTerminal=false\nCategories=Graphics;Video;AudioVideo;\n"
            f"StartupNotify=false\n")


def mac_app():
    app = os.path.expanduser(f"~/Applications/{NAME}.app")
    os.makedirs(os.path.join(app, "Contents", "MacOS"), exist_ok=True)
    os.makedirs(os.path.join(app, "Contents", "Resources"), exist_ok=True)
    exe = os.path.join(app, "Contents", "MacOS", "kie-studio")
    with open(exe, "w") as f: f.write(f'#!/bin/sh\ncd "{REPO}" && exec "{VPY}" -m kie_studio open\n')
    os.chmod(exe, 0o755)
    if os.path.exists(os.path.join(ICONS, "kie-studio.icns")):
        shutil.copy(os.path.join(ICONS, "kie-studio.icns"), os.path.join(app, "Contents", "Resources", "icon.icns"))
    with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>{NAME}</string><key>CFBundleDisplayName</key><string>{NAME}</string>
<key>CFBundleIdentifier</key><string>ai.kie.studio.launcher</string><key>CFBundleExecutable</key><string>kie-studio</string>
<key>CFBundleIconFile</key><string>icon</string><key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>2.0</string><key>LSUIElement</key><true/>
</dict></plist>
""")
    return app


def shortcuts(menu, desktop, log):
    if WIN:
        icon = os.path.join(ICONS, "kie-studio.ico")
        targets = ([os.path.join(win_folder("Programs"), f"{NAME}.lnk")] if menu else []) + \
                  ([os.path.join(desktop_dir(), f"{NAME}.lnk")] if desktop else [])
        for t in targets:
            win_shortcut(t, VPYW, "-m kie_studio open", log, icon); log(f"shortcut: {t}")
    elif MAC:
        app = mac_app(); log(f"app: {app}  (find it with Spotlight or in Launchpad)")
        if desktop:
            link = os.path.join(desktop_dir(), f"{NAME}.app")
            if os.path.islink(link): os.remove(link)
            if not os.path.exists(link): os.symlink(app, link); log(f"desktop alias: {link}")
    else:
        entry = linux_desktop_entry([VPY, "-m", "kie_studio", "open"])
        apps = os.path.expanduser("~/.local/share/applications")
        if menu:
            os.makedirs(apps, exist_ok=True)
            p = os.path.join(apps, "kie-studio.desktop"); open(p, "w").write(entry); log(f"app menu entry: {p}")
            if shutil.which("update-desktop-database"): run(["update-desktop-database", apps], log, check=False)
        if desktop:
            d = desktop_dir(); os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "kie-studio.desktop"); open(p, "w").write(entry); os.chmod(p, 0o755)
            if shutil.which("gio"): run(["gio", "set", p, "metadata::trusted", "true"], log, check=False)   # GNOME "Allow launching"
            log(f"desktop shortcut: {p}")


def autostart(enable, log):
    if WIN:
        p = os.path.join(win_folder("Startup"), f"{NAME} server.lnk")
        if enable: win_shortcut(p, VPYW, "-m kie_studio serve --log", log); log(f"starts at login: {p}")
        elif os.path.exists(p): os.remove(p)
    elif MAC:
        p = os.path.expanduser("~/Library/LaunchAgents/ai.kie.studio.plist")
        run(["launchctl", "bootout", f"gui/{os.getuid()}/ai.kie.studio"], log, check=False)
        if enable:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>ai.kie.studio</string>
<key>ProgramArguments</key><array><string>{VPY}</string><string>-m</string><string>kie_studio</string><string>serve</string><string>--log</string></array>
<key>WorkingDirectory</key><string>{REPO}</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
</dict></plist>
""")
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", p], log, check=False); log(f"starts at login: {p}")
        elif os.path.exists(p): os.remove(p)
    else:
        unit = os.path.expanduser("~/.config/systemd/user/kie-studio.service")
        xdg = os.path.expanduser("~/.config/autostart/kie-studio-server.desktop")
        systemd = shutil.which("systemctl") and run(["systemctl", "--user", "show-environment"], lambda *_: None, check=False)[0] == 0
        if systemd:
            run(["systemctl", "--user", "disable", "--now", "kie-studio.service"], log, check=False)
            if os.path.islink(unit) or os.path.exists(unit): os.remove(unit)   # older installs symlinked a unit from the repo
        if os.path.exists(xdg): os.remove(xdg)
        if not enable:
            if systemd: run(["systemctl", "--user", "daemon-reload"], log, check=False)
            return
        if systemd:
            os.makedirs(os.path.dirname(unit), exist_ok=True)
            with open(unit, "w") as f:
                f.write(f"""# written by kie studio's installer (deploy/installer.py)
[Unit]
Description=kie studio (local kie.ai web UI)
After=network-online.target

[Service]
WorkingDirectory={REPO}
ExecStart={VPY} -m kie_studio serve
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=10

[Install]
WantedBy=default.target
""")
            run(["systemctl", "--user", "daemon-reload"], log)
            run(["systemctl", "--user", "enable", "--now", "kie-studio.service"], log)
            log(f"starts at login: {unit}  (logs: journalctl --user -u kie-studio)")
        else:
            os.makedirs(os.path.dirname(xdg), exist_ok=True)
            open(xdg, "w").write(linux_desktop_entry([VPY, "-m", "kie_studio", "serve", "--log"], f"{NAME} server", "Background server"))
            log(f"starts at login: {xdg}")


def save_key(key, log):
    from kie_studio.client import Kie, KieError, save_api_key
    key = key.strip()
    if not key: return
    try: bal = Kie(key).credits()
    except KieError as e:
        if e.code == 401: raise Step("kie.ai rejected that API key. Fix it, or leave it empty and enter it in the app later.")
        log(f"Couldn't check the key with kie.ai ({e}); saving it anyway."); bal = None
    save_api_key(key); log("API key saved" + (f" — balance {bal:g} credits" if bal is not None else ""))


def install(opts, log):
    stop_server(log)
    log("\n== 1/4  Python packages"); make_venv(log)
    log("\n== 2/4  Shortcuts"); shortcuts(opts["menu"], opts["desktop"], log)
    log("\n== 3/4  Start at login"); autostart(opts["autostart"], log)
    if not opts["autostart"]: log("off — the shortcut starts kie studio when you open it")
    log("\n== 4/4  API key")
    if opts.get("key"): save_key(opts["key"], log)
    else: log("already saved — kept" if has_key() else "skipped — the app will ask for it")
    log("\nDone.")


def uninstall(log):
    stop_server(log)
    autostart(False, log)
    if WIN:
        for p in (os.path.join(win_folder("Programs"), f"{NAME}.lnk"), os.path.join(desktop_dir(), f"{NAME}.lnk")):
            if os.path.exists(p): os.remove(p); log(f"removed {p}")
    elif MAC:
        for p in (os.path.join(desktop_dir(), f"{NAME}.app"),):
            if os.path.islink(p): os.remove(p); log(f"removed {p}")
        app = os.path.expanduser(f"~/Applications/{NAME}.app")
        if os.path.exists(app): shutil.rmtree(app); log(f"removed {app}")
    else:
        for p in (os.path.expanduser("~/.local/share/applications/kie-studio.desktop"), os.path.join(desktop_dir(), "kie-studio.desktop")):
            if os.path.exists(p): os.remove(p); log(f"removed {p}")
    if os.path.exists(VENV): shutil.rmtree(VENV, ignore_errors=True); log(f"removed {VENV}")
    log("\nUninstalled. Your videos, images, history and API key were kept.")


def launch():
    subprocess.Popen([VPY, "-m", "kie_studio", "open"], cwd=REPO, creationflags=NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def has_key():
    try:
        from kie_studio.client import key_source; return bool(key_source())
    except Exception: return False


# ------------------------------------------------------------------ console mode
def console(uninstall_only=False):
    log = lambda m: print(m, flush=True)
    yn = lambda q, d=True: (input(f"{q} [{'Y/n' if d else 'y/N'}] ").strip().lower() or ("y" if d else "n")).startswith("y")
    print(f"\n{NAME} installer\n  app folder: {REPO}\n")
    try:
        if uninstall_only or (os.path.exists(VPY) and not yn("Install / update? (answer n to uninstall)")):
            if yn("Uninstall kie studio (keeps your media and history)?", False): uninstall(log)
            return
        opts = {"menu": yn("Add to the app menu / Start menu?"), "desktop": yn("Add a desktop shortcut?"),
                "autostart": yn("Start the server automatically when you log in?", False), "key": ""}
        if not has_key():
            import getpass
            opts["key"] = getpass.getpass("kie.ai API key (from kie.ai/api-key; Enter to skip, input is hidden): ")
        install(opts, log)
        if yn("Open kie studio now?"): launch()
    except Step as e:
        print(f"\nERROR: {e}"); sys.exit(1)


# ------------------------------------------------------------------ window
def gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    import webbrowser
    import tkinter.font as tkfont
    root = tk.Tk(); root.title(f"Install {NAME}"); root.minsize(600, 540)
    if not WIN and not MAC: ttk.Style().theme_use("clam")   # Windows/macOS already get native themes
    title_font = tkfont.nametofont("TkDefaultFont").copy(); title_font.configure(size=18, weight="bold")
    try: root.iconphoto(True, tk.PhotoImage(file=os.path.join(ICONS, "kie-studio.png")))
    except Exception: pass
    q = queue.Queue()
    pad = {"padx": 16}
    frm = ttk.Frame(root, padding=(0, 14)); frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=NAME, font=title_font).pack(anchor="w", **pad)
    ttk.Label(frm, text="Generate videos and images with kie.ai models from a simple app window.\n"
                        f"Installs into {REPO} for your user only — no admin rights needed.",
              justify="left", wraplength=570).pack(anchor="w", pady=(2, 10), **pad)
    v_menu = tk.BooleanVar(value=True); v_desk = tk.BooleanVar(value=True); v_auto = tk.BooleanVar(value=False)
    menu_label = "Add to the Start menu" if WIN else "Add to Applications" if MAC else "Add to the app menu"
    for var, text in ((v_menu, menu_label), (v_desk, "Put a shortcut on the desktop"),
                      (v_auto, "Start automatically when I log in (otherwise it starts when you open it)")):
        ttk.Checkbutton(frm, text=text, variable=var).pack(anchor="w", **pad)
    kf = ttk.Frame(frm); kf.pack(fill="x", pady=(12, 0), **pad)
    have = has_key()
    ttk.Label(kf, text="kie.ai API key " + ("(already saved — leave empty to keep it)" if have else "(optional — the app asks if empty)")).pack(anchor="w")
    v_key = tk.StringVar(); ent = ttk.Entry(kf, textvariable=v_key, show="•"); ent.pack(fill="x", pady=2)
    link = ttk.Label(kf, text="Get your key at kie.ai/api-key", foreground="#6a4cff", cursor="hand2"); link.pack(anchor="w")
    link.bind("<Button-1>", lambda e: webbrowser.open("https://kie.ai/api-key"))
    bar = ttk.Progressbar(frm, mode="indeterminate"); bar.pack(fill="x", pady=(14, 4), **pad)
    out = scrolledtext.ScrolledText(frm, height=12, font=("TkFixedFont", 9), state="disabled", wrap="word")
    out.pack(fill="both", expand=True, **pad)
    btns = ttk.Frame(frm); btns.pack(fill="x", pady=(10, 0), **pad)
    b_inst = ttk.Button(btns, text="Update" if os.path.exists(VPY) else "Install")
    b_open = ttk.Button(btns, text=f"Open {NAME}", command=lambda: (launch(), root.after(1500, root.destroy)))
    b_un = ttk.Button(btns, text="Uninstall")
    b_inst.pack(side="right"); b_un.pack(side="left")
    if os.path.exists(VPY): b_open.pack(side="right", padx=8)

    def log(m): q.put(("log", m))
    def write(m):
        out.configure(state="normal"); out.insert("end", m + "\n"); out.see("end"); out.configure(state="disabled")
    def work(fn):
        for b in (b_inst, b_un, b_open): b.state(["disabled"])
        bar.start(12)
        def t():
            try: fn(); q.put(("done", None))
            except Step as e: q.put(("err", str(e)))
            except Exception as e: q.put(("err", f"unexpected error: {e!r}"))
        threading.Thread(target=t, daemon=True).start()
    def pump():
        try:
            while True:
                kind, m = q.get_nowait()
                if kind == "log": write(m)
                else:
                    bar.stop()
                    for b in (b_inst, b_un, b_open): b.state(["!disabled"])
                    if kind == "err":
                        write("\nERROR: " + m); tk.messagebox.showerror(NAME, m)
                    elif os.path.exists(VPY):
                        b_inst.configure(text="Update"); b_open.pack(side="right", padx=8)
                        if tk.messagebox.askyesno(NAME, f"{NAME} is installed. Open it now?"): launch(); root.after(1500, root.destroy)
                    else: b_open.pack_forget(); b_inst.configure(text="Install")
        except queue.Empty: pass
        root.after(80, pump)
    import tkinter.messagebox
    b_inst.configure(command=lambda: work(lambda: install({"menu": v_menu.get(), "desktop": v_desk.get(),
                                                            "autostart": v_auto.get(), "key": v_key.get()}, log)))
    b_un.configure(command=lambda: tk.messagebox.askyesno(NAME, "Remove shortcuts, autostart and the app's Python environment?\n"
                                                                "Your videos, images, history and API key are kept.")
                   and work(lambda: uninstall(log)))
    write(f"App folder: {REPO}\nPython: {sys.version.split()[0]} ({sys.executable})")
    root.after(80, pump); root.mainloop()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--uninstall" in args and "--console" in args: console(uninstall_only=True)
    elif "--console" in args: console()
    else:
        try: import tkinter; gui()
        except Exception as e:
            print(f"(no installer window available: {e}; using text mode)"); console()
