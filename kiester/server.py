#!/usr/bin/env python3
"""Kiester — a kie.ai API interface: local web UI for video + image generation.
Independent project; not affiliated with or endorsed by kie.ai.

Run:  python3 -m kiester serve   (installed launchers run `open`, which starts it if needed)  → http://127.0.0.1:8770
Data: ~/Videos/Kiester/ (~/Movies/Kiester on macOS; see paths.py). Key: ~/.config/kie/token (set from the UI; never sent to the browser).
Env:  KIESTER_HOST (default 127.0.0.1), KIESTER_PORT (8770), KIESTER_TOKEN (required when not on localhost).
"""
import hashlib, io, json, mimetypes, os, re, secrets, shutil, sqlite3, subprocess, sys, threading, time, uuid, urllib.request
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_from_directory, abort, make_response

from .paths import media_dir
from .client import Kie, KieError, api_key, save_api_key, key_source

try:
    from PIL import Image, ImageOps
except ImportError:  # still works, just without image normalisation
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = media_dir()
ASSETS = os.path.join(MEDIA, "assets")
IMAGES_OUT = os.path.join(MEDIA, "images")
DB_PATH = os.path.join(MEDIA, "studio.db")
HOST = os.environ.get("KIESTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("KIESTER_PORT", "8770"))
TOKEN = os.environ.get("KIESTER_TOKEN", "")
UPLOAD_TTL = 20 * 3600        # kie deletes uploaded files after 24 h; re-upload after 20 h
POLL_EVERY = float(os.environ.get("KIESTER_POLL", "6"))
JOB_HARD_TIMEOUT = 3 * 3600
for d in (MEDIA, ASSETS, IMAGES_OUT): os.makedirs(d, exist_ok=True)

if HOST not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
    raise SystemExit("Refusing to listen on a non-local address without KIESTER_TOKEN set.")

REG = json.load(open(os.path.join(HERE, "models.json")))
CREDIT_USD = REG["credit_usd"]

# ---------------------------------------------------------------- database
db_lock = threading.RLock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, client_id TEXT UNIQUE, created REAL, updated REAL,
  kind TEXT, model_key TEXT, mode_key TEXT, model_id TEXT, prompt TEXT,
  params TEXT, slots TEXT, status TEXT, task_id TEXT,
  error TEXT, credits REAL, est_credits REAL, cost_time REAL,
  result_urls TEXT, outputs TEXT, poster TEXT, favorite INTEGER DEFAULT 0, notes TEXT DEFAULT '',
  submitted REAL);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
CREATE TABLE IF NOT EXISTS events(job_id TEXT, ts REAL, msg TEXT);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
CREATE TABLE IF NOT EXISTS assets(
  id TEXT PRIMARY KEY, file TEXT, mime TEXT, w INTEGER, h INTEGER, bytes INTEGER,
  created REAL, source TEXT, label TEXT, remote_url TEXT, remote_at REAL, hidden INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")

def q(sql, args=(), one=False):
    with db_lock:
        cur = db.execute(sql, args); rows = cur.fetchall(); db.commit()
    return (rows[0] if rows else None) if one else rows

JSON_COLS = ("params", "slots", "result_urls", "outputs")
def job_dict(r):
    d = dict(r)
    for c in JSON_COLS: d[c] = json.loads(d[c]) if d.get(c) else ([] if c in ("result_urls", "outputs") else {})
    m = REG[d["kind"]].get(d["model_key"], {}) if d["kind"] in ("video", "image") else {}
    d["model_label"] = m.get("label", d["model_key"])
    d["mode_label"] = m.get("modes", {}).get(d["mode_key"], {}).get("label", d["mode_key"])
    if d["model_key"] == "_stitch": d["model_label"], d["mode_label"] = "Stitched", f"{len(d['params'].get('sources', []))} clips"
    return d

def job_update(jid, **kv):
    kv["updated"] = time.time()
    for c in JSON_COLS:
        if c in kv and not isinstance(kv[c], str): kv[c] = json.dumps(kv[c])
    q(f"UPDATE jobs SET {', '.join(k + '=?' for k in kv)} WHERE id=?", (*kv.values(), jid))

def ev(jid, msg):
    q("INSERT INTO events VALUES(?,?,?)", (jid, time.time(), str(msg)[:2000]))

def asset_dict(r):
    d = dict(r); d["url"] = f"/media/assets/{d['file']}"; return d

# ---------------------------------------------------------------- assets
IMG_OK = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
MAX_BYTES, MAX_SIDE, MIN_SIDE = 19 * 1024 * 1024, 4096, 240

def ingest_image(raw, source="upload", label=""):
    """Normalise an image so every kie model accepts it; dedupe by content hash.
    Fixes: EXIF rotation from phones, HEIC/BMP/GIF/TIFF → JPEG/PNG, >20 MB, sides >4096 or <240 px, CMYK/palette modes."""
    notes = []
    if Image is None:
        data, ext, mime, w, h = raw, ".jpg", "image/jpeg", 0, 0
    else:
        try:
            im = Image.open(io.BytesIO(raw)); fmt = im.format
            im.load()
        except Exception:
            head = raw[:16]
            if b"ftypheic" in head or b"ftypheix" in head or b"ftypmif1" in head:
                raise ValueError("HEIC photos are not supported. On the phone, share/export as JPEG (or set camera to 'Most compatible').")
            raise ValueError("Not a readable image file.")
        if getattr(im, "n_frames", 1) > 1: notes.append("animated image: using first frame")
        changed = fmt not in IMG_OK
        t = ImageOps.exif_transpose(im)
        if t is not im: changed = True; notes.append("applied EXIF rotation")
        im = t
        w, h = im.size
        if max(w, h) / max(1, min(w, h)) > 8: raise ValueError(f"Aspect ratio {w}x{h} exceeds 8:1, which kie rejects.")
        if max(w, h) > MAX_SIDE:
            s = MAX_SIDE / max(w, h); im = im.resize((round(w * s), round(h * s)), Image.LANCZOS); changed = True
            notes.append(f"downscaled from {w}x{h}")
        if min(im.size) < MIN_SIDE:
            s = MIN_SIDE / min(im.size); im = im.resize((round(im.size[0] * s), round(im.size[1] * s)), Image.LANCZOS); changed = True
            notes.append(f"upscaled to meet {MIN_SIDE}px minimum")
        alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        if im.mode not in ("RGB", "RGBA"): im = im.convert("RGBA" if alpha else "RGB"); changed = True
        if changed or len(raw) > MAX_BYTES:
            buf = io.BytesIO()
            if alpha: im.save(buf, "PNG", optimize=True); ext, mime = ".png", "image/png"
            else: im.convert("RGB").save(buf, "JPEG", quality=92); ext, mime = ".jpg", "image/jpeg"
            data = buf.getvalue()
            if len(data) > MAX_BYTES:
                buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=82); data = buf.getvalue(); ext, mime = ".jpg", "image/jpeg"
        else:
            data, mime = raw, IMG_OK[fmt]; ext = mimetypes.guess_extension(mime) or ".jpg"
        w, h = im.size
    aid = hashlib.sha256(data).hexdigest()[:20]
    old = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if old:
        if old["hidden"]: q("UPDATE assets SET hidden=0 WHERE id=?", (aid,))
        d = asset_dict(old); d["notes"] = notes; return d
    fn = aid + ext
    with open(os.path.join(ASSETS, fn), "wb") as f: f.write(data)
    q("INSERT INTO assets(id,file,mime,w,h,bytes,created,source,label) VALUES(?,?,?,?,?,?,?,?,?)",
      (aid, fn, mime, w, h, len(data), time.time(), source, label[:200]))
    d = asset_dict(q("SELECT * FROM assets WHERE id=?", (aid,), one=True)); d["notes"] = notes; return d

def remote_url(kie, aid, jid):
    a = q("SELECT * FROM assets WHERE id=?", (aid,), one=True)
    if not a: raise KieError(404, f"asset {aid} no longer exists")
    if a["remote_url"] and time.time() - (a["remote_at"] or 0) < UPLOAD_TTL:
        ev(jid, f"reusing upload of {aid} ({int((time.time()-a['remote_at'])/60)} min old)")
        return a["remote_url"]
    url = kie.upload(os.path.join(ASSETS, a["file"]), a["file"])
    q("UPDATE assets SET remote_url=?, remote_at=? WHERE id=?", (url, time.time(), aid))
    ev(jid, f"uploaded {a['file']}")
    return url

# ---------------------------------------------------------------- params / pricing
def spec(kind, mkey, mokey):
    m = REG.get(kind, {}).get(mkey)
    if not m or mokey not in m["modes"]: raise ValueError("unknown model/mode")
    mo = m["modes"][mokey]
    return m, mo, m["params"] + mo.get("extra", [])

def clean_params(pspec, raw):
    out = {}
    for p in pspec:
        v = raw.get(p["key"], p["default"])
        if p["type"] == "bool": v = bool(v)
        elif p["type"] == "int":
            try: v = int(v)
            except (TypeError, ValueError): v = p["default"]
            v = max(p.get("min", v), min(p.get("max", v), v))
        elif p["type"] == "select":
            if v not in p["opts"]:
                v = next((o for o in p["opts"] if str(o) == str(v)), p["default"])
        out[p["key"]] = v
    return out

def estimate(kind, m, params):
    price = m.get("price"); pk = m.get("price_key")
    rate = (price or {}).get(str(params.get(pk))) if price else None
    if rate is None:
        # fall back to what this model actually cost before
        r = q("SELECT credits, params FROM jobs WHERE kind=? AND model_key=? AND status='success' AND credits>0 ORDER BY created DESC LIMIT 20", (kind, m["_key"]))
        same = [x for x in r if json.loads(x["params"]).get(pk) == params.get(pk)]
        if not same: return None, "unknown"
        if kind == "video":
            per_s = [x["credits"] / max(1, int(json.loads(x["params"]).get("duration", 5) or 5)) for x in same]
            rate = sum(per_s) / len(per_s)
        else: rate = sum(x["credits"] for x in same) / len(same)
        src = "history"
    else: src = "list"
    if kind == "video":
        dur = int(params.get("duration", 5) or 5)
        if dur <= 0: dur = 15
        return round(rate * dur, 2), src
    return round(rate, 2), src

for kind in ("video", "image"):
    for k, m in REG[kind].items(): m["_key"] = k

# ---------------------------------------------------------------- job runner
kie_lock = threading.Lock(); _kie = None
def kie():
    global _kie
    with kie_lock:
        if _kie is None: _kie = Kie(api_key())
        return _kie

def reset_kie():
    global _kie
    with kie_lock: _kie = None
    _credits.update(t=0, v=None, err=None)

pool = ThreadPoolExecutor(max_workers=3)

def submit(jid):
    j = job_dict(q("SELECT * FROM jobs WHERE id=?", (jid,), one=True))
    m, mo, _ = spec(j["kind"], j["model_key"], j["mode_key"])
    try:
        k = kie()
        job_update(jid, status="uploading")
        inp = {"prompt": j["prompt"], **j["params"]}
        for slot in mo["slots"]:
            ids = j["slots"].get(slot["key"]) or []
            if not ids: continue
            urls = [remote_url(k, a, jid) for a in ids]
            inp[slot["key"]] = urls if slot.get("array") else urls[0]
        job_update(jid, status="submitting")
        shown = {k2: (v if not isinstance(v, str) or len(v) < 80 else v[:77] + "…") for k2, v in inp.items() if k2 != "prompt"}
        ev(jid, f"createTask {mo['model']} {json.dumps(shown)}")
        tid = k.create_task(mo["model"], inp)
        job_update(jid, status="running", task_id=tid, submitted=time.time())
        ev(jid, f"task {tid} accepted")
    except KieError as e:
        job_update(jid, status="fail", error=str(e)); ev(jid, f"ERROR {e}")
    except Exception as e:
        job_update(jid, status="fail", error=f"local error: {e}"); ev(jid, f"ERROR local: {e}")

def finish(j, d):
    jid = j["id"]
    try: urls = json.loads(d.get("resultJson") or "{}").get("resultUrls") or []
    except ValueError: urls = []
    job_update(jid, credits=d.get("creditsConsumed"), cost_time=d.get("costTime"), result_urls=urls)
    if not urls:
        job_update(jid, status="fail", error="task succeeded but returned no result URL"); return
    fetch_outputs(jid, urls, j["kind"])

def fetch_outputs(jid, urls, kind):
    outs = []; stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        for i, u in enumerate(urls):
            ext = os.path.splitext(u.split("?")[0])[1].lower() or (".mp4" if kind == "video" else ".png")
            rel = (f"{stamp}-{jid}{'-' + str(i) if i else ''}{ext}" if kind == "video"
                   else f"images/{stamp}-{jid}-{i}{ext}")
            Kie.download(u, os.path.join(MEDIA, rel)); outs.append(rel)
        poster = make_poster(outs[0]) if kind == "video" else outs[0]
        job_update(jid, status="success", outputs=outs, poster=poster, error=None)
        ev(jid, f"saved {', '.join(outs)}")
    except KieError as e:
        job_update(jid, status="success", outputs=outs, error=f"result not saved locally ({e}); kie keeps it 14 days — use Retry download")
        ev(jid, f"download failed: {e}")

_ffmpeg = None
def ffmpeg_exe():
    """System ffmpeg if present, else the static build shipped by the imageio-ffmpeg package (Windows/macOS installs)."""
    global _ffmpeg
    if _ffmpeg is None:
        _ffmpeg = shutil.which("ffmpeg")
        if not _ffmpeg:
            try:
                import imageio_ffmpeg; _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception: _ffmpeg = ""
    return _ffmpeg

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flashing on Windows

def ffmpeg(*args, timeout=600):
    exe = ffmpeg_exe()
    if not exe: raise ValueError("ffmpeg not found — rerun the installer (it installs imageio-ffmpeg) or install ffmpeg.")
    return subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args], capture_output=True, text=True,
                          timeout=timeout, creationflags=NO_WINDOW)

def probe(f):
    """(width, height, has_audio, duration) parsed from `ffmpeg -i` — avoids needing ffprobe, which imageio-ffmpeg lacks."""
    exe = ffmpeg_exe()
    r = subprocess.run([exe, "-hide_banner", "-i", f], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    err = r.stderr
    m = re.search(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})", err)
    dm = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(dm[1]) * 3600 + int(dm[2]) * 60 + float(dm[3]) if dm else 5.0
    return (int(m[1]), int(m[2])) if m else (1280, 720), bool(re.search(r"Stream #.*?Audio:", err)), dur

def make_poster(rel):
    src = os.path.join(MEDIA, rel); pr = "posters/" + os.path.splitext(os.path.basename(rel))[0] + ".jpg"   # DB paths are always posix
    os.makedirs(os.path.join(MEDIA, "posters"), exist_ok=True)
    r = ffmpeg("-ss", "0.3", "-i", src, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", os.path.join(MEDIA, pr))
    return pr if r.returncode == 0 else None

def poller():
    while True:
        try:
            for r in q("SELECT * FROM jobs WHERE status='running'"):
                j = job_dict(r)
                if time.time() - (j["submitted"] or j["created"]) > JOB_HARD_TIMEOUT:
                    job_update(j["id"], status="fail", error="gave up after 3 h (task may still finish on kie.ai; check its dashboard)"); continue
                try: d = kie().task(j["task_id"])
                except KieError as e:
                    ev(j["id"], f"poll error (will retry): {e}"); continue
                st = d.get("state")
                if st == "success":
                    ev(j["id"], f"success after {d.get('costTime')} s, {d.get('creditsConsumed')} credits")
                    job_update(j["id"], status="downloading"); pool.submit(finish, j, d)
                elif st == "fail":
                    code, msg = d.get("failCode") or d.get("errorCode"), d.get("failMsg") or d.get("errorMessage")
                    err = KieError(int(code) if str(code).isdigit() else code, msg or "failed")
                    if re.search(r"sensitive|nsfw|safety|moderat|policy", msg or "", re.I):
                        err = KieError("filtered", (msg or "") + " — blocked by the vendor's content filter (not a bug; rephrase the prompt or change the images)")
                    job_update(j["id"], status="fail", error=str(err), credits=d.get("creditsConsumed"))
                    ev(j["id"], f"FAILED {err} (credits charged: {d.get('creditsConsumed')})")
                elif st:
                    last = q("SELECT msg FROM events WHERE job_id=? ORDER BY ts DESC LIMIT 1", (j["id"],), one=True)
                    if not last or not last["msg"].startswith("state: " + st): ev(j["id"], f"state: {st}")
        except Exception as e:
            print("poller error:", e, flush=True)
        time.sleep(POLL_EVERY)

def recover():
    """Server restarts must never lose or double-bill jobs."""
    for r in q("SELECT id, status FROM jobs WHERE status IN ('queued','uploading')"):
        job_update(r["id"], status="fail", error="interrupted by server restart before submission (not billed) — press Rerun")
    for r in q("SELECT id FROM jobs WHERE status='submitting'"):
        job_update(r["id"], status="fail", error="interrupted by restart during createTask; it may exist on kie.ai — check the dashboard before rerunning")
    for r in q("SELECT * FROM jobs WHERE status='downloading'"):
        j = job_dict(r); pool.submit(fetch_outputs, j["id"], j["result_urls"], j["kind"])

# ---------------------------------------------------------------- migration from the v1 history.json
def migrate():
    if q("SELECT v FROM meta WHERE k='migrated_v1'", one=True): return
    hp = os.path.join(MEDIA, "history.json"); up = os.path.join(MEDIA, "uploads"); amap = {}
    if os.path.isdir(up):
        for fn in sorted(os.listdir(up)):
            try: amap[fn] = ingest_image(open(os.path.join(up, fn), "rb").read(), "upload", fn)["id"]
            except Exception as e: print("skip", fn, e)
    if os.path.exists(hp):
        for e in reversed(json.load(open(hp))):
            if "model_key" not in e: continue
            st = {"done": "success", "error": "fail"}.get(e["status"], "fail")
            if e["status"] == "running" and e.get("task"): st = "running"
            slots = {k: [amap[n] for n in v if n in amap] for k, v in (e.get("images") or {}).items()}
            created = time.mktime(time.strptime(e["time"], "%Y-%m-%d %H:%M")) if e.get("time") else time.time()
            mo = REG["video"].get(e["model_key"], {}).get("modes", {}).get(e["mode_key"], {})
            outs = [e["video"]] if e.get("video") else []
            q("INSERT OR IGNORE INTO jobs(id,created,updated,kind,model_key,mode_key,model_id,prompt,params,slots,status,task_id,error,outputs,poster) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (e["id"], created, created, "video", e["model_key"], e["mode_key"], mo.get("model"), e.get("prompt", ""),
               json.dumps(e.get("params") or {}), json.dumps(slots), st, e.get("task"), e.get("error"), json.dumps(outs),
               make_poster(outs[0]) if outs else None))
    q("INSERT OR REPLACE INTO meta VALUES('migrated_v1', ?)", (str(time.time()),))
    print("migrated v1 history", flush=True)

def backfill_credits():
    """Fill real credit costs for migrated jobs (lets history-based estimates work)."""
    rows = q("SELECT id, task_id FROM jobs WHERE task_id IS NOT NULL AND credits IS NULL AND status IN ('success','fail')")
    for r in rows:
        try:
            d = kie().task(r["task_id"]); job_update(r["id"], credits=d.get("creditsConsumed"), cost_time=d.get("costTime"))
        except Exception: pass

# ---------------------------------------------------------------- web app
app = Flask(__name__, static_folder=None)
app.json.sort_keys = False   # keep models.json order (curated: best/default model first)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}

@app.before_request
def guard():
    # DNS-rebinding protection: only answer to expected Host headers (any host allowed once a token is set)
    if not TOKEN and request.host not in ALLOWED_HOSTS: abort(403)
    if TOKEN:
        t = request.args.get("token")
        if t and secrets.compare_digest(t, TOKEN): return  # cookie set in after_request
        if not secrets.compare_digest(request.cookies.get("kst", ""), TOKEN): abort(401)
    # CSRF: any other website could POST to localhost and spend credits. Custom header forces a CORS preflight we never approve.
    # (X-Studio: pages still open from before the rename)
    if request.method not in ("GET", "HEAD") and "1" not in (request.headers.get("X-Kiester"), request.headers.get("X-Studio")): abort(403)

@app.after_request
def headers(resp):
    if TOKEN and request.args.get("token") == TOKEN:
        resp.set_cookie("kst", TOKEN, httponly=True, samesite="Strict", max_age=90 * 86400)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp

@app.errorhandler(ValueError)
def bad(e): return jsonify(error=str(e)), 400

@app.get("/")
def index(): return send_from_directory(os.path.join(HERE, "static"), "index.html")

@app.get("/static/<path:p>")
def static_files(p): return send_from_directory(os.path.join(HERE, "static"), p)

@app.get("/media/<path:p>")
def media(p):
    # only generated media + assets; never the database, partial downloads or dotfiles
    if any(part.startswith(".") for part in p.split("/")) or p.endswith((".part", ".db", "-wal", "-shm", ".json")): abort(404)
    return send_from_directory(MEDIA, p, conditional=True)

@app.get("/api/config")
def config():
    reg = {k: {mk: {kk: vv for kk, vv in mv.items() if kk != "_key"} for mk, mv in REG[k].items()} for k in ("video", "image")}
    try: api_key(); has_key = True
    except KieError: has_key = False
    return jsonify(registry=reg, credit_usd=CREDIT_USD, has_key=has_key, key_source=key_source(), ffmpeg=bool(ffmpeg_exe()))

@app.post("/api/key")
def set_key():
    """First-run / change-key screen. The key is checked against kie.ai before it is saved; it is never echoed back."""
    k = str((request.get_json(force=True) or {}).get("key") or "").strip()
    if k.lower().startswith("bearer "): k = k[7:].strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-.]{16,200}", k): raise ValueError("That doesn't look like a kie.ai API key (copy it from kie.ai → API Key).")
    if key_source() == "env": raise ValueError("The key is set by the KIE_API_KEY environment variable, which overrides a saved key. Change it there.")
    try: bal = Kie(k).credits()
    except KieError as e:
        if e.code == 401: raise ValueError("kie.ai rejected this key. Check that you copied all of it.")
        raise ValueError(f"Couldn't reach kie.ai to check the key ({e}). Check your internet connection and try again.")
    save_api_key(k); reset_kie(); _credits.update(v=bal, t=time.time())
    return jsonify(ok=True, credits=bal)

_credits = {"t": 0, "v": None, "err": None}
@app.get("/api/credits")
def credits():
    if time.time() - _credits["t"] > 20 or request.args.get("fresh"):
        try: _credits.update(v=kie().credits(), err=None)
        except KieError as e: _credits.update(err=str(e))
        _credits["t"] = time.time()
    spent = q("SELECT COALESCE(SUM(credits),0) s, COUNT(*) n FROM jobs WHERE created > ?", (time.time() - 86400,), one=True)
    return jsonify(credits=_credits["v"], usd=None if _credits["v"] is None else round(_credits["v"] * CREDIT_USD, 2),
                   error=_credits["err"], spent_24h=spent["s"], jobs_24h=spent["n"])

@app.post("/api/estimate")
def api_estimate():
    b = request.get_json(force=True)
    m, mo, ps = spec(b["kind"], b["model_key"], b["mode_key"])
    p = clean_params(ps, b.get("params") or {})
    c, src = estimate(b["kind"], m, p)
    return jsonify(credits=c, usd=None if c is None else round(c * CREDIT_USD, 3), source=src)

@app.post("/api/jobs")
def create_job():
    b = request.get_json(force=True)
    cid = str(b.get("client_id") or uuid.uuid4())
    dup = q("SELECT id FROM jobs WHERE client_id=?", (cid,), one=True)
    if dup: return jsonify(id=dup["id"], duplicate=True)          # double-click / retry protection
    kind = b.get("kind")
    m, mo, ps = spec(kind, b.get("model_key"), b.get("mode_key"))
    prompt = (b.get("prompt") or "").strip()
    if not prompt: raise ValueError("Prompt is empty.")
    if len(prompt) > 20000: raise ValueError("Prompt exceeds 20,000 characters.")
    params = clean_params(ps, b.get("params") or {})
    slots = {}
    for s in mo["slots"]:
        ids = [a for a in (b.get("slots") or {}).get(s["key"], []) if q("SELECT 1 FROM assets WHERE id=?", (a,), one=True)]
        ids = ids[: s.get("max", 1)]
        if s.get("required") and not ids: raise ValueError(f"{s['label']} is required.")
        if ids: slots[s["key"]] = ids
    est, _ = estimate(kind, m, params)
    # pre-flight: kie accepts the task and fails it later with 402; catch it before uploading anything
    bal = _credits["v"] if time.time() - _credits["t"] < 120 else None
    if bal is None:
        try: bal = kie().credits(); _credits.update(v=bal, t=time.time(), err=None)
        except KieError: bal = None
    if bal is not None and est and est > bal and not b.get("force"):
        raise ValueError(f"Estimated {est:g} credits but the balance is {bal:g}. Top up at kie.ai/billing.")
    jid = uuid.uuid4().hex[:10]; now = time.time()
    q("INSERT INTO jobs(id,client_id,created,updated,kind,model_key,mode_key,model_id,prompt,params,slots,status,est_credits) "
      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (jid, cid, now, now, kind, m["_key"], b["mode_key"], mo["model"], prompt, json.dumps(params), json.dumps(slots), "queued", est))
    ev(jid, "queued"); pool.submit(submit, jid)
    return jsonify(id=jid)

@app.get("/api/jobs")
def list_jobs():
    a = request.args; where, args = [], []
    if a.get("kind"): where.append("kind=?"); args.append(a["kind"])
    if a.get("model"): where.append("model_key=?"); args.append(a["model"])
    if a.get("status") == "active": where.append("status IN ('queued','uploading','submitting','running','downloading')")
    elif a.get("status"): where.append("status=?"); args.append(a["status"])
    if a.get("fav"): where.append("favorite=1")
    if a.get("q"): where.append("(prompt LIKE ? OR notes LIKE ?)"); args += [f"%{a['q']}%"] * 2
    if a.get("before"): where.append("created<?"); args.append(float(a["before"]))
    if a.get("since"): where.append("updated>?"); args.append(float(a["since"]))  # cheap incremental refresh
    lim = min(int(a.get("limit", 40)), 200)
    rows = q(f"SELECT * FROM jobs {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY created DESC LIMIT ?", (*args, lim))
    return jsonify([job_dict(r) for r in rows])

def get_job(jid):
    r = q("SELECT * FROM jobs WHERE id=?", (jid,), one=True)
    if not r: abort(404)
    return job_dict(r)

@app.get("/api/jobs/<jid>")
def job(jid):
    j = get_job(jid)
    j["events"] = [dict(e) for e in q("SELECT ts, msg FROM events WHERE job_id=? ORDER BY ts", (jid,))]
    j["assets"] = {k: [asset_dict(x) for x in [q("SELECT * FROM assets WHERE id=?", (a,), one=True) for a in v] if x]
                   for k, v in j["slots"].items()}
    return jsonify(j)

@app.patch("/api/jobs/<jid>")
def patch_job(jid):
    get_job(jid); b = request.get_json(force=True); kv = {}
    if "favorite" in b: kv["favorite"] = 1 if b["favorite"] else 0
    if "notes" in b: kv["notes"] = str(b["notes"])[:2000]
    if kv: job_update(jid, **kv)
    return jsonify(get_job(jid))

@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    j = get_job(jid)
    if j["status"] in ("uploading", "submitting", "running", "downloading"):
        raise ValueError("Job is still active; dismiss it first.")
    if request.args.get("files") == "1":
        for rel in j["outputs"] + ([j["poster"]] if j["poster"] and j["kind"] == "video" else []):
            p = os.path.realpath(os.path.join(MEDIA, rel))
            if p.startswith(os.path.realpath(MEDIA) + os.sep) and os.path.exists(p): os.remove(p)
    q("DELETE FROM jobs WHERE id=?", (jid,)); q("DELETE FROM events WHERE job_id=?", (jid,))
    return jsonify(ok=True)

@app.post("/api/jobs/<jid>/dismiss")
def dismiss(jid):
    j = get_job(jid)
    if j["status"] == "running":
        job_update(jid, status="fail", error="dismissed locally (kie has no cancel; the task may still run and bill)")
        ev(jid, "dismissed by user")
    return jsonify(get_job(jid))

@app.post("/api/jobs/<jid>/redownload")
def redownload(jid):
    j = get_job(jid)
    if not j["result_urls"]: raise ValueError("No remote result to download.")
    job_update(jid, status="downloading"); pool.submit(fetch_outputs, jid, j["result_urls"], j["kind"])
    return jsonify(ok=True)

@app.post("/api/jobs/<jid>/frame")
def frame(jid):
    j = get_job(jid); which = (request.get_json(silent=True) or {}).get("which", "last")
    if j["kind"] != "video" or not j["outputs"]: raise ValueError("No local video for this job.")
    src = os.path.join(MEDIA, j["outputs"][0]); tmp = os.path.join(MEDIA, f".frame-{jid}.png")
    args = (["-sseof", "-0.5", "-i", src, "-update", "1"] if which == "last" else ["-i", src, "-frames:v", "1"])
    r = ffmpeg(*args, tmp)
    if r.returncode or not os.path.exists(tmp): raise ValueError(f"ffmpeg failed: {r.stderr[-300:]}")
    raw = open(tmp, "rb").read(); os.remove(tmp)
    return jsonify(ingest_image(raw, "frame", f"{which} frame of {jid}"))

@app.post("/api/jobs/<jid>/as-asset")
def output_as_asset(jid):
    j = get_job(jid); i = int((request.get_json(silent=True) or {}).get("index", 0))
    if j["kind"] != "image" or len(j["outputs"]) <= i: raise ValueError("No local image output.")
    return jsonify(ingest_image(open(os.path.join(MEDIA, j["outputs"][i]), "rb").read(), "generated", f"output of {jid}"))

@app.post("/api/stitch")
def stitch():
    ids = (request.get_json(force=True) or {}).get("ids") or []
    jobs_ = [get_job(i) for i in ids]
    files = [os.path.join(MEDIA, j["outputs"][0]) for j in jobs_ if j["kind"] == "video" and j["outputs"]]
    if len(files) < 2: raise ValueError("Select at least two finished videos.")
    info = [(*wh, a, dur) for wh, a, dur in map(probe, files)]; W, H = info[0][0] // 2 * 2, info[0][1] // 2 * 2
    args, filt, n = [], [], len(files)
    for f in files: args += ["-i", f]
    for i, (w, h, has_a, dur) in enumerate(info):
        if not has_a: args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    extra = n
    for i, (w, h, has_a, dur) in enumerate(info):
        filt.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{i}]")
        if has_a: filt.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else: filt.append(f"[{extra}:a]aformat=channel_layouts=stereo[a{i}]"); extra += 1
    filt.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
    jid = uuid.uuid4().hex[:10]; rel = f"{time.strftime('%Y%m%d-%H%M%S')}-{jid}-stitch.mp4"
    r = ffmpeg(*args, "-filter_complex", ";".join(filt), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", os.path.join(MEDIA, rel))
    if r.returncode: raise ValueError(f"ffmpeg failed: {r.stderr[-400:]}")
    now = time.time()
    q("INSERT INTO jobs(id,created,updated,kind,model_key,mode_key,model_id,prompt,params,slots,status,outputs,poster,credits) "
      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
      (jid, now, now, "video", "_stitch", "stitch", "ffmpeg", "Stitched: " + " + ".join(ids), json.dumps({"sources": ids}), "{}",
       "success", json.dumps([rel]), make_poster(rel), 0))
    return jsonify(get_job(jid))

@app.post("/api/assets")
def upload_assets():
    out, errs = [], []
    for f in request.files.getlist("files"):
        try: out.append(ingest_image(f.read(), "upload", f.filename or ""))
        except ValueError as e: errs.append(f"{f.filename}: {e}")
    return jsonify(assets=out, errors=errs)

@app.get("/api/assets")
def list_assets():
    rows = q("SELECT * FROM assets WHERE hidden=0 ORDER BY created DESC LIMIT ?", (min(int(request.args.get("limit", 120)), 500),))
    return jsonify([asset_dict(r) for r in rows])

@app.delete("/api/assets/<aid>")
def hide_asset(aid):
    # soft delete: history entries keep working and can still re-run with it
    q("UPDATE assets SET hidden=1 WHERE id=?", (aid,)); return jsonify(ok=True)

@app.get("/api/health")
def health():
    """Recent outcome per model+mode, so a down endpoint is visible before spending a click."""
    out = {}
    for r in q("SELECT kind, model_key, mode_key, status, error, updated FROM jobs WHERE status IN ('success','fail') AND model_key!='_stitch' ORDER BY updated DESC LIMIT 400"):
        key = f"{r['kind']}/{r['model_key']}/{r['mode_key']}"
        lst = out.setdefault(key, [])
        if len(lst) < 5: lst.append({"ok": r["status"] == "success", "error": (r["error"] or "")[:120], "t": r["updated"]})
    return jsonify(out)

@app.get("/api/stats")
def stats():
    tot = q("SELECT COALESCE(SUM(credits),0) c, COUNT(*) n, SUM(status='success') ok, SUM(status='fail') bad FROM jobs", one=True)
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(MEDIA) for f in fs)
    return jsonify(credits_total=tot["c"], usd_total=round(tot["c"] * CREDIT_USD, 2), jobs=tot["n"], ok=tot["ok"], failed=tot["bad"],
                   disk_mb=round(size / 1e6, 1), free_gb=round(shutil.disk_usage(MEDIA).free / 1e9, 1))


def running(port=PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2) as r: return r.status == 200
    except Exception: return False

def main(log=None):
    if log is not None or sys.stdout is None:   # pythonw / launchd / autostart: no console, so log to a file
        log = log or os.path.join(MEDIA, "kiester.log")
        if os.path.exists(log) and os.path.getsize(log) > 5e6: os.replace(log, log + ".old")
        f = open(log, "a", buffering=1, encoding="utf-8"); sys.stdout = sys.stderr = f
        print(f"# kiester log — safe to delete\n{time.strftime('%Y-%m-%d %H:%M:%S')} starting")
    # Windows lets a second process bind the same port, so check for a live instance explicitly
    if running(): raise SystemExit(f"kiester is already running on port {PORT}.")
    from werkzeug.serving import make_server
    try: srv = make_server(HOST, PORT, app, threaded=True)
    except OSError as e:
        raise SystemExit(f"Port {PORT} is busy ({e}). Is another kie UI running? Set KIESTER_PORT or stop it.")
    migrate(); recover()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=backfill_credits, daemon=True).start()
    print(f"kiester on http://127.0.0.1:{PORT}", flush=True)
    if not ffmpeg_exe(): print("warning: ffmpeg not found — posters, frames and stitching are disabled", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
