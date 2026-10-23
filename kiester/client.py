"""Thin, thread-safe kie.ai API client (stdlib only).

Pitfalls handled here:
- HTTP errors come back as exceptions with a readable message (never sys.exit in a worker thread).
- kie returns errors with HTTP 200 + a "code" field; both shapes are normalised into KieError.
- Transient network failures / 5xx / 429 are retried with backoff; 4xx are not.
- The API key is read server-side only and never logged or sent to the browser.
"""
import json, mimetypes, os, re, time, uuid, urllib.request, urllib.error

API = os.environ.get("KIE_API_BASE", "https://api.kie.ai/api/v1")          # overridable for tests
UPLOAD = os.environ.get("KIE_UPLOAD_URL", "https://kieai.redpandaai.co/api/file-stream-upload")
KEY_FILE = os.path.expanduser(os.environ.get("KIE_KEY_FILE", "~/.config/kie/token"))

FRIENDLY = {
    401: "Invalid or missing API key (set it in Kiester).",
    402: "Not enough kie.ai credits. Top up at kie.ai/billing.",
    404: "Endpoint or task not found.",
    422: "kie rejected the request parameters.",
    429: "Rate limited by kie.ai. Wait a minute and retry.",
    455: "kie.ai is under maintenance.",
    500: "kie/vendor internal error (often the model endpoint is down; retry later or pick another model).",
    501: "Generation failed on the vendor side.",
    505: "Feature disabled by kie.ai.",
    524: "Vendor timed out (model endpoint overloaded or down; not billed).",
}


class KieError(Exception):
    def __init__(self, code, msg, transient=False):
        self.code, self.msg, self.transient = code, msg, transient
        hint = FRIENDLY.get(code if isinstance(code, int) else _int(code))
        if re.search(r"range|must be|unsupported|invalid|not allowed|required|too (long|short|large|small)", str(msg), re.I):
            hint = "a parameter value is outside what this model accepts (kie reports it as a generic error code; not billed)"
        super().__init__(f"{code}: {msg}" + (f" — {hint}" if hint and hint.lower() not in str(msg).lower() else ""))


def _int(x):
    try: return int(x)
    except Exception: return None


def api_key():
    k = os.environ.get("KIE_API_KEY", "").strip()
    if not k and os.path.exists(KEY_FILE):
        k = open(KEY_FILE, encoding="utf-8-sig").read().strip()   # -sig: Notepad may have added a BOM
    if not k:
        raise KieError(401, "no API key saved yet")
    return k


def key_source():
    if os.environ.get("KIE_API_KEY", "").strip(): return "env"
    return "file" if os.path.exists(KEY_FILE) and open(KEY_FILE, encoding="utf-8-sig").read().strip() else None


def save_api_key(key):
    """Write the key readable by this user only (0600 on Linux/macOS; on Windows the profile folder is already private)."""
    os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
    tmp = KEY_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(key.strip() + "\n")
    os.replace(tmp, KEY_FILE)
    try: os.chmod(KEY_FILE, 0o600)
    except OSError: pass


class Kie:
    def __init__(self, key=None):
        self.key = key or api_key()

    def _req(self, method, url, body=None, headers=None, timeout=90, retries=3):
        h = {"Authorization": f"Bearer {self.key}", "User-Agent": "kiester/2.0 (+https://github.com/shahar-cohen/kiester)"}
        h.update(headers or {})
        delay = 2
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=body, method=method, headers=h)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                try: data = json.loads(raw)
                except ValueError: raise KieError(502, f"non-JSON response: {raw[:200]!r}", True)
            except urllib.error.HTTPError as e:
                raw = e.read()[:400]
                try: msg = json.loads(raw).get("msg") or raw.decode(errors="replace")
                except Exception: msg = raw.decode(errors="replace")
                err = KieError(e.code, msg, transient=e.code >= 500 or e.code == 429)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                err = KieError(0, f"network error: {e}", transient=True)
            except KieError as e:
                err = e
            else:
                code = data.get("code", 200)
                if code == 200:
                    return data
                err = KieError(code, data.get("msg") or "unknown error", transient=code in (429, 455, 500, 502, 503))
            if not err.transient or attempt == retries:
                raise err
            time.sleep(delay); delay *= 2

    def credits(self):
        return float(self._req("GET", f"{API}/chat/credit")["data"])

    def create_task(self, model, inp):
        # never retry createTask blindly on a 5xx: it may have created (and billed) a task
        d = self._req("POST", f"{API}/jobs/createTask", json.dumps({"model": model, "input": inp}).encode(),
                      {"Content-Type": "application/json"}, retries=0)
        return d["data"]["taskId"]

    def task(self, task_id):
        return self._req("GET", f"{API}/jobs/recordInfo?taskId={task_id}")["data"] or {}

    def upload(self, path, name=None):
        name = name or os.path.basename(path)
        boundary = uuid.uuid4().hex
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts = []
        for k, v in (("uploadPath", "images/kiester"), ("fileName", name)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode())
        with open(path, "rb") as f: parts.append(f.read())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        d = self._req("POST", UPLOAD, b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=180)
        if not d.get("success", True) or not d.get("data"):
            raise KieError(d.get("code", 500), f"upload failed: {d.get('msg')}")
        return d["data"]["downloadUrl"]

    @staticmethod
    def download(url, dest, retries=4):
        tmp = dest + ".part"; delay = 3
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kiester/2.0 (+https://github.com/shahar-cohen/kiester)"}), timeout=300) as r, open(tmp, "wb") as f:
                    while chunk := r.read(1 << 16): f.write(chunk)
                if os.path.getsize(tmp) == 0: raise OSError("empty download")
                os.replace(tmp, dest); return dest
            except Exception as e:
                if attempt == retries:
                    try: os.remove(tmp)
                    except OSError: pass
                    raise KieError(0, f"download failed: {e}", True)
                time.sleep(delay); delay *= 2
