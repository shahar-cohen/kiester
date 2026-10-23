"""Headless-Chrome smoke test driven over CDP: runs UI steps and reports JS exceptions/console errors.

  python3 tests/browser_smoke.py http://127.0.0.1:8771/ [WxH]     (needs google-chrome + the websockets package; spends no credits)
"""
import asyncio, json, base64, subprocess, sys, time, urllib.request, websockets
import os, tempfile
URL = sys.argv[1]; W, H = map(int, (sys.argv[2] if len(sys.argv) > 2 else "1500x1000").split("x"))
SP = tempfile.mkdtemp(prefix="kie-smoke-")
STEPS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_steps.json")))
for s in STEPS:
    if s.get("nav") == "URL": s["nav"] = URL
chrome = subprocess.Popen(["google-chrome", "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={SP}/chrome2",
    "--remote-debugging-port=9333", f"--window-size={W},{H}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    try: tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9333/json")); break
    except Exception: time.sleep(0.2)
ws_url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]

def handle(r):
    m = r.get("method")
    if m == "Runtime.exceptionThrown":
        d = r["params"]["exceptionDetails"]; print("EXCEPTION:", d.get("exception", {}).get("description", d["text"])[:400])
    if m == "Runtime.consoleAPICalled" and r["params"]["type"] in ("error", "warning"):
        print("CONSOLE:", [a.get("value", a.get("description")) for a in r["params"]["args"]])
    if m == "Log.entryAdded" and r["params"]["entry"]["level"] == "error":
        print("LOG:", r["params"]["entry"]["text"][:300], r["params"]["entry"].get("url", ""))
    if m == "Page.javascriptDialogOpening":
        print("DIALOG:", r["params"]["message"][:200]); asyncio.get_event_loop().create_task(WS.send(json.dumps({"id": 99999, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}})))

async def main():
    global WS
    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        WS = ws; n = 0
        async def call(m, **p):
            nonlocal n; n += 1; i = n; await ws.send(json.dumps({"id": i, "method": m, "params": p}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == i: return r.get("result", r.get("error"))
                handle(r)
        await call("Runtime.enable"); await call("Log.enable"); await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=W < 600)
        for st in STEPS:
            if "nav" in st: await call("Page.navigate", url=st["nav"]); await asyncio.sleep(2.5)
            if "js" in st:
                r = await call("Runtime.evaluate", expression=st["js"], awaitPromise=True, returnByValue=True)
                v = r.get("result", {}).get("value")
                if r.get("exceptionDetails"): print("STEP ERR:", st["js"][:80], r["exceptionDetails"].get("exception", {}).get("description", "")[:300])
                elif v is not None: print("=>", json.dumps(v)[:800])
                await asyncio.sleep(st.get("wait", 0.6))
            if "shot" in st:
                r = await call("Page.captureScreenshot", format="png"); open(f"{SP}/{st['shot']}.png", "wb").write(base64.b64decode(r["data"]))
        try:
            while True: handle(json.loads(await asyncio.wait_for(ws.recv(), 1)))
        except Exception: pass
asyncio.run(main()); chrome.terminate()
