"""Kiester command line (Kiester — a kie.ai API interface; unofficial).

  python3 -m kiester serve [--log FILE]          run the web UI (http://127.0.0.1:8770)
  python3 -m kiester open                        start it in the background if needed, then open it in an app window
  python3 -m kiester models                      list models / modes / params
  python3 -m kiester credits                     show the kie.ai balance
  python3 -m kiester gen MODEL MODE "prompt" [--image SLOT=FILE ...] [--set KEY=VALUE ...] [--wait]
  python3 -m kiester jobs [-n 10]                recent jobs
  python3 -m kiester task TASK_ID                raw recordInfo for a kie task

`gen` and `jobs` talk to the running server, so CLI jobs show up in the UI history and get the same
upload caching, pre-flight balance check, restart recovery and download retries.
"""
import argparse, json, os, subprocess, sys, time, urllib.request, urllib.error, uuid, webbrowser
from .paths import media_dir

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get("KIESTER_PORT", "8770")
BASE = f"http://127.0.0.1:{PORT}"
UI_URL = f"http://127.0.0.1:{PORT}/"


def find_chromium():
    """Chrome / Edge / Brave / Chromium, whichever exists. Edge ships with Windows, so Windows always has one."""
    import shutil
    if os.name == "nt":
        roots = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rels = [r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe"]
        cands = [os.path.join(r, x) for x in rels for r in roots if r]
    elif sys.platform == "darwin":
        apps = ["Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "Brave Browser.app/Contents/MacOS/Brave Browser", "Chromium.app/Contents/MacOS/Chromium"]
        cands = [os.path.join(d, a) for a in apps for d in ("/Applications", os.path.expanduser("~/Applications"))]
    else:
        cands = [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                                            "microsoft-edge", "microsoft-edge-stable", "brave-browser")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def open_window(url=UI_URL):
    """Open the UI as its own app window (no tabs / address bar). Falls back to the default browser."""
    exe = find_chromium()
    if exe:
        kw = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920", "--new-window"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
            return
        except OSError: pass
    webbrowser.open(url)


def is_up():
    try:
        with urllib.request.urlopen(BASE + "/api/config", timeout=2) as r: return r.status == 200
    except Exception: return False


def open_ui():
    """What the desktop shortcut runs: reuse a running server, or start one detached (it outlives this process)."""
    if not is_up():
        py = sys.executable
        if os.name == "nt":   # pythonw: no console window
            w = os.path.join(os.path.dirname(py), "pythonw.exe"); py = w if os.path.exists(w) else py
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            kw = {"creationflags": flags}
        else: kw = {"start_new_session": True}
        subprocess.Popen([py, "-m", "kiester", "serve", "--log", ""], cwd=os.path.dirname(HERE), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **kw)
        for _ in range(150):   # first start may migrate data / build posters
            if is_up(): break
            time.sleep(0.2)
        else:
            media = media_dir()
            msg = f"Kiester did not start. See the log: {os.path.join(media, 'kiester.log')}"
            _alert(msg); sys.exit(msg)
    open_window()


def _alert(msg):
    """Show an error even when started from a shortcut with no console."""
    try:
        import tkinter, tkinter.messagebox
        r = tkinter.Tk(); r.withdraw(); tkinter.messagebox.showerror("Kiester", msg); r.destroy()
    except Exception: pass


def make_icons(out):
    """Draw the app icon (PNG for Linux, ICO for Windows, ICNS for macOS) with Pillow."""
    from PIL import Image, ImageDraw
    os.makedirs(out, exist_ok=True)
    s = 1024; im = Image.new("RGBA", (s, s), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s, s), radius=256, fill=(217, 72, 31, 255))
    k = [(9.5, 7.5), (13.1, 7.5), (13.1, 14.4), (19.3, 7.5), (23.7, 7.5), (17.1, 14.6), (24.1, 24.5), (19.7, 24.5),
         (14.7, 17.3), (13.1, 19.0), (13.1, 24.5), (9.5, 24.5)]          # same "K" as the favicon (32-unit grid)
    d.polygon([(x * 32, y * 32) for x, y in k], fill=(255, 255, 255, 255))
    im.resize((256, 256), Image.LANCZOS).save(os.path.join(out, "kiester.png"))
    im.save(os.path.join(out, "kiester.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    try: im.save(os.path.join(out, "kiester.icns"))
    except Exception: pass


def call(method, path, body=None, files=None):
    h = {"X-Kiester": "1"}
    if files:
        b = uuid.uuid4().hex; parts = []
        for f in files:
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{os.path.basename(f)}"\r\n'
                         f"Content-Type: application/octet-stream\r\n\r\n".encode() + open(f, "rb").read() + b"\r\n")
        data = b"".join(parts) + f"--{b}--\r\n".encode(); h["Content-Type"] = f"multipart/form-data; boundary={b}"
    else:
        data = json.dumps(body).encode() if body is not None else None
        if data: h["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, method=method, headers=h), timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read()).get("error")
        except Exception: msg = e.reason
        sys.exit(f"error {e.code}: {msg}")
    except urllib.error.URLError:
        sys.exit(f"kiester server not reachable at {BASE}. Start it: python3 -m kiester serve")


def coerce(v):
    if v.lower() in ("true", "false"): return v.lower() == "true"
    try: return int(v)
    except ValueError: return v


def main():
    ap = argparse.ArgumentParser(prog="kiester", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve"); sv.add_argument("--log", nargs="?", const="", default=None, help="log to FILE (empty: default log in the data folder)")
    sub.add_parser("open"); ic = sub.add_parser("icons"); ic.add_argument("out")
    sub.add_parser("models"); sub.add_parser("credits")
    g = sub.add_parser("gen")
    g.add_argument("model"); g.add_argument("mode"); g.add_argument("prompt")
    g.add_argument("--image", action="append", default=[], metavar="SLOT=FILE", help="repeat; same slot twice = multiple images")
    g.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    g.add_argument("--wait", action="store_true", help="block until finished and print the output path")
    g.add_argument("--force", action="store_true", help="skip the balance pre-check")
    j = sub.add_parser("jobs"); j.add_argument("-n", type=int, default=10)
    t = sub.add_parser("task"); t.add_argument("task_id")
    a = ap.parse_args()

    if a.cmd == "serve":
        from .server import main as serve; serve(log=a.log); return
    if a.cmd == "open": open_ui(); return
    if a.cmd == "icons": make_icons(a.out); return
    if a.cmd == "models":
        reg = json.load(open(os.path.join(HERE, "models.json")))
        for kind in ("video", "image"):
            print(f"== {kind}")
            for k, m in reg[kind].items():
                print(f"  {k:14} {m['label']}  modes: {', '.join(m['modes'])}")
                for p in m["params"]:
                    print(f"      {p['key']}={p['default']!r}" + (f"  {p['opts']}" if p.get("opts") else ""))
                for mk, mo in m["modes"].items():
                    for s in mo["slots"]:
                        print(f"      [{mk}] slot {s['key']}{' (required)' if s.get('required') else ''}{' ×' + str(s.get('max')) if s.get('array') else ''}")
        return
    if a.cmd in ("credits", "task"):
        from .client import Kie, KieError
        try:
            if a.cmd == "credits":
                c = Kie().credits(); print(f"{c:g} credits (${c * 0.005:.2f})")
            else: print(json.dumps(Kie().task(a.task_id), indent=2))
        except KieError as e: sys.exit(str(e))
        return
    if a.cmd == "jobs":
        for x in call("GET", f"/api/jobs?limit={a.n}"):
            print(f"{x['id']}  {time.strftime('%m-%d %H:%M', time.localtime(x['created']))}  {x['status']:11} "
                  f"{x['model_label']}/{x['mode_key']}  {x.get('credits') or '-'} cr  {x['prompt'][:60]!r}")
        return

    reg = call("GET", "/api/config")["registry"]
    kind = next((k for k in ("video", "image") if a.model in reg[k]), None)
    if not kind: sys.exit(f"unknown model {a.model!r}; see: python3 -m kiester models")
    slots = {}
    for spec in a.image:
        slot, _, f = spec.partition("=")
        if not f or not os.path.exists(f): sys.exit(f"--image needs SLOT=existing-file, got {spec!r}")
        r = call("POST", "/api/assets", files=[f])
        if r["errors"]: sys.exit("; ".join(r["errors"]))
        slots.setdefault(slot, []).append(r["assets"][0]["id"])
    params = {k: coerce(v) for k, _, v in (s.partition("=") for s in a.set)}
    r = call("POST", "/api/jobs", {"kind": kind, "model_key": a.model, "mode_key": a.mode, "prompt": a.prompt,
                                    "params": params, "slots": slots, "force": a.force})
    jid = r["id"]; print(f"job {jid} queued")
    if not a.wait: return
    last = None
    while True:
        x = call("GET", f"/api/jobs/{jid}")
        if x["status"] != last: print(f"  {x['status']}", flush=True); last = x["status"]
        if x["status"] == "success":
            media = media_dir()
            for o in x["outputs"]: print(os.path.join(media, o))
            if x.get("error"): print("warning:", x["error"])
            return
        if x["status"] == "fail": sys.exit(f"failed: {x['error']}")
        time.sleep(4)


if __name__ == "__main__":
    main()
