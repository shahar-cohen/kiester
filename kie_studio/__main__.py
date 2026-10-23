"""kie-studio command line.

  python3 -m kie_studio serve                       run the web UI (http://127.0.0.1:8770)
  python3 -m kie_studio models                      list models / modes / params
  python3 -m kie_studio credits                     show the kie.ai balance
  python3 -m kie_studio gen MODEL MODE "prompt" [--image SLOT=FILE ...] [--set KEY=VALUE ...] [--wait]
  python3 -m kie_studio jobs [-n 10]                recent jobs
  python3 -m kie_studio task TASK_ID                raw recordInfo for a kie task

`gen` and `jobs` talk to the running server, so CLI jobs show up in the UI history and get the same
upload caching, pre-flight balance check, restart recovery and download retries.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = f"http://127.0.0.1:{os.environ.get('KIE_STUDIO_PORT', '8770')}"


def call(method, path, body=None, files=None):
    h = {"X-Studio": "1"}
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
        sys.exit(f"kie-studio server not reachable at {BASE}. Start it: python3 -m kie_studio serve")


def coerce(v):
    if v.lower() in ("true", "false"): return v.lower() == "true"
    try: return int(v)
    except ValueError: return v


def main():
    ap = argparse.ArgumentParser(prog="kie_studio", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve"); sub.add_parser("models"); sub.add_parser("credits")
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
        from .server import main as serve; serve(); return
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
    if not kind: sys.exit(f"unknown model {a.model!r}; see: python3 -m kie_studio models")
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
            media = os.path.expanduser(os.environ.get("KIE_STUDIO_MEDIA", "~/Videos/kie"))
            for o in x["outputs"]: print(os.path.join(media, o))
            if x.get("error"): print("warning:", x["error"])
            return
        if x["status"] == "fail": sys.exit(f"failed: {x['error']}")
        time.sleep(4)


if __name__ == "__main__":
    main()
