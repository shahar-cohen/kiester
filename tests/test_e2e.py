"""End-to-end test against a fake kie.ai API (stdlib only, spends no credits).

  python3 -m unittest discover -s tests -v
"""
import io, json, os, shutil, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.request, urllib.error, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FakeKie(BaseHTTPRequestHandler):
    """createTask → waiting → success after 2 polls. Prompts containing 'NSFW' fail like the vendor filter does."""
    tasks, created, balance = {}, [], 1000.0
    video = image = b""

    def log_message(self, *a): pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/chat/credit"):
            if self.headers.get("Authorization") == "Bearer rejected-key-0000000000":
                return self.send({"code": 401, "msg": "You do not have access permissions"})
            return self.send({"code": 200, "data": FakeKie.balance})
        if self.path.startswith("/api/v1/jobs/recordInfo"):
            tid = self.path.split("taskId=")[1]; t = FakeKie.tasks[tid]; t["polls"] += 1
            base = f"http://127.0.0.1:{self.server.server_port}"
            if t["polls"] < 2: return self.send({"code": 200, "data": {"taskId": tid, "state": "generating"}})
            if "NSFW" in t["input"]["prompt"]:
                return self.send({"code": 200, "data": {"taskId": tid, "state": "fail", "failCode": "500", "creditsConsumed": 0,
                                  "failMsg": "The request failed because the output video may contain sensitive information."}})
            url = f"{base}/files/out.mp4" if t["model"].startswith("wan") else f"{base}/files/out.png"
            return self.send({"code": 200, "data": {"taskId": tid, "state": "success", "costTime": 12, "creditsConsumed": 80,
                                                    "resultJson": json.dumps({"resultUrls": [url]})}})
        if self.path == "/files/out.mp4": return self.send(FakeKie.video, ctype="video/mp4")
        if self.path == "/files/out.png": return self.send(FakeKie.image, ctype="image/png")
        if self.path.startswith("/files/up-"): return self.send(b"x", ctype="image/jpeg")
        self.send({"code": 404, "msg": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        if self.path == "/upload":
            return self.send({"success": True, "code": 200, "data": {"downloadUrl": f"http://127.0.0.1:{self.server.server_port}/files/up-{uuid.uuid4().hex}.jpg"}})
        if self.path == "/api/v1/jobs/createTask":
            b = json.loads(raw); tid = uuid.uuid4().hex
            FakeKie.tasks[tid] = {"polls": 0, **b}; FakeKie.created.append(b)
            return self.send({"code": 200, "data": {"taskId": tid}})
        self.send({"code": 404, "msg": "nope"}, 404)


class StudioE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kiester-test-")
        vid = os.path.join(cls.tmp, "src.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=1",
                        "-pix_fmt", "yuv420p", vid], check=True)
        FakeKie.video = open(vid, "rb").read(); os.remove(vid)
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (640, 360), (200, 50, 50)).save(buf, "PNG"); FakeKie.image = buf.getvalue()
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeKie)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fk = f"http://127.0.0.1:{cls.fake.server_port}"
        cls.port = free_port(); cls.base = f"http://127.0.0.1:{cls.port}"
        media = os.path.join(cls.tmp, "media"); os.makedirs(media)
        cls.env = {**os.environ, "KIE_KEY_FILE": os.path.join(cls.tmp, "token"), "KIESTER_MEDIA": media, "KIESTER_PORT": str(cls.port), "KIE_API_KEY": "test",
                   "KIE_API_BASE": fk + "/api/v1", "KIE_UPLOAD_URL": fk + "/upload", "KIESTER_POLL": "0.3", "PYTHONDONTWRITEBYTECODE": "1"}
        cls.media = media
        cls.start()

    @classmethod
    def start(cls):
        cls.proc = subprocess.Popen([sys.executable, "-m", "kiester", "serve"], cwd=ROOT, env=cls.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try: urllib.request.urlopen(cls.base + "/api/config", timeout=1); return
            except Exception: time.sleep(0.1)
        cls.proc.kill(); raise RuntimeError("server did not start: " + cls.proc.stdout.read())

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(5); cls.fake.shutdown(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, raw=None, ctype=None):
        h = {"X-Kiester": "1", **(headers or {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None: h["Content-Type"] = ctype or "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, method=method, headers=h), timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read())
            except Exception: return e.code, None

    def upload(self, img_bytes, name="a.png"):
        b = uuid.uuid4().hex
        data = (f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode()
                + img_bytes + f"\r\n--{b}--\r\n".encode())
        return self.req("POST", "/api/assets", raw=data, ctype=f"multipart/form-data; boundary={b}")

    def wait(self, jid, until=("success", "fail"), t=20):
        end = time.time() + t
        while time.time() < end:
            _, j = self.req("GET", f"/api/jobs/{jid}")
            if j["status"] in until: return j
            time.sleep(0.2)
        self.fail(f"job {jid} stuck in {j['status']}")

    def video_job(self, prompt="a red ball rolls", **kw):
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (100, 3000), (0, 0, 255)).save(buf, "PNG")  # too thin → must be normalised/rejected
        st, r = self.upload(FakeKie.image)
        self.assertEqual(st, 200); aid = r["assets"][0]["id"]
        body = {"client_id": uuid.uuid4().hex, "kind": "video", "model_key": "wan3", "mode_key": "i2v", "prompt": prompt,
                "params": {"duration": "3", "resolution": "480P"}, "slots": {"first_frame_url": [aid]}, **kw}
        return self.req("POST", "/api/jobs", body)

    # ---------------------------------------------------------------- tests
    def test_index_and_static(self):
        for p in ("/", "/static/app.js", "/static/style.css"):
            with urllib.request.urlopen(self.base + p) as r: self.assertEqual(r.status, 200)

    def test_csrf_and_host_guard(self):
        req = urllib.request.Request(self.base + "/api/jobs", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as c: urllib.request.urlopen(req)
        self.assertEqual(c.exception.code, 403)
        req = urllib.request.Request(self.base + "/api/config", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as c: urllib.request.urlopen(req)
        self.assertEqual(c.exception.code, 403)

    def test_database_not_served(self):
        for p in ("/media/studio.db", "/media/history.json", "/media/../kiester/server.py"):
            st, _ = self.req("GET", p); self.assertIn(st, (403, 404), p)

    def test_full_video_flow(self):
        st, r = self.video_job(); self.assertEqual(st, 200, r)
        j = self.wait(r["id"])
        self.assertEqual(j["status"], "success", j)
        self.assertTrue(os.path.exists(os.path.join(self.media, j["outputs"][0])))
        self.assertTrue(j["poster"] and os.path.exists(os.path.join(self.media, j["poster"])))
        self.assertEqual(j["credits"], 80)
        sent = FakeKie.created[-1]
        self.assertEqual(sent["model"], "wan/3-0-video")
        self.assertIsInstance(sent["input"]["duration"], int)            # wan3 wants an int, UI may send "3"
        self.assertTrue(sent["input"]["first_frame_url"].startswith("http"))
        # last frame → new asset for chaining
        st, a = self.req("POST", f"/api/jobs/{j['id']}/frame", {"which": "last"}); self.assertEqual(st, 200, a)
        self.assertGreaterEqual(min(a["w"], a["h"]), 240)

    def test_idempotent_client_id(self):
        st, r = self.upload(FakeKie.image); aid = r["assets"][0]["id"]
        body = {"client_id": "dup-1", "kind": "video", "model_key": "wan3", "mode_key": "i2v", "prompt": "x",
                "params": {}, "slots": {"first_frame_url": [aid]}}
        n = len(FakeKie.created)
        _, a = self.req("POST", "/api/jobs", body); _, b = self.req("POST", "/api/jobs", body)
        self.assertEqual(a["id"], b["id"]); self.assertTrue(b.get("duplicate"))
        self.wait(a["id"]); self.assertEqual(len(FakeKie.created), n + 1)

    def test_upload_reused_within_ttl(self):
        _, r1 = self.video_job(); self.wait(r1["id"])
        _, r2 = self.video_job(); self.wait(r2["id"])
        _, j = self.req("GET", f"/api/jobs/{r2['id']}")
        self.assertTrue(any("reusing upload" in e["msg"] for e in j["events"]))

    def test_content_filter_message(self):
        _, r = self.video_job("NSFW thing"); j = self.wait(r["id"])
        self.assertEqual(j["status"], "fail"); self.assertIn("content filter", j["error"])

    def test_validation(self):
        st, r = self.req("POST", "/api/jobs", {"kind": "video", "model_key": "wan3", "mode_key": "i2v", "prompt": "x", "slots": {}})
        self.assertEqual(st, 400); self.assertIn("required", r["error"])
        st, r = self.req("POST", "/api/jobs", {"kind": "video", "model_key": "wan3", "mode_key": "t2v", "prompt": "  "})
        self.assertEqual(st, 400)
        st, r = self.req("POST", "/api/jobs", {"kind": "video", "model_key": "nope", "mode_key": "t2v", "prompt": "x"})
        self.assertEqual(st, 400)

    def test_balance_preflight(self):
        old = FakeKie.balance; FakeKie.balance = 5
        try:
            self.req("GET", "/api/credits?fresh=1")
            st, r = self.req("POST", "/api/jobs", {"kind": "video", "model_key": "wan3", "mode_key": "t2v", "prompt": "x",
                                                   "params": {"resolution": "1080P", "duration": 10}})
            self.assertEqual(st, 400); self.assertIn("balance", r["error"])
        finally:
            FakeKie.balance = old; self.req("GET", "/api/credits?fresh=1")

    def test_bad_images(self):
        st, r = self.upload(b"not an image", "x.png"); self.assertTrue(r["errors"])
        st, r = self.upload(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100, "x.heic"); self.assertIn("HEIC", r["errors"][0])
        from PIL import Image
        buf = io.BytesIO(); Image.new("RGB", (5000, 1000)).save(buf, "PNG")
        _, r = self.upload(buf.getvalue()); a = r["assets"][0]
        self.assertLessEqual(max(a["w"], a["h"]), 4096)

    def test_image_flow_and_stitch(self):
        _, r = self.req("POST", "/api/jobs", {"kind": "image", "model_key": "flux2pro", "mode_key": "t2i", "prompt": "a cat", "params": {}})
        j = self.wait(r["id"]); self.assertEqual(j["status"], "success", j)
        st, a = self.req("POST", f"/api/jobs/{j['id']}/as-asset", {"index": 0}); self.assertEqual(st, 200)
        _, v1 = self.video_job(); _, v2 = self.video_job()
        self.wait(v1["id"]); self.wait(v2["id"])
        st, s = self.req("POST", "/api/stitch", {"ids": [v1["id"], v2["id"]]}); self.assertEqual(st, 200, s)
        self.assertTrue(os.path.exists(os.path.join(self.media, s["outputs"][0])))

    def test_since_filter(self):
        t = time.time()
        _, r = self.video_job(); self.wait(r["id"])
        _, rows = self.req("GET", f"/api/jobs?since={t}")
        self.assertIn(r["id"], [x["id"] for x in rows])

    def test_first_run_without_key(self):
        """No key anywhere → UI is told so; a rejected key is not saved; a good key is saved 0600 and used."""
        port = free_port(); keyfile = os.path.join(self.tmp, "fresh-token")
        env = {**self.env, "KIESTER_PORT": str(port), "KIE_KEY_FILE": keyfile,
               "KIESTER_MEDIA": os.path.join(self.tmp, "media2")}
        env.pop("KIE_API_KEY")
        p = subprocess.Popen([sys.executable, "-m", "kiester", "serve"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        base, self.base = self.base, f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                try: urllib.request.urlopen(self.base + "/api/config", timeout=1); break
                except Exception: time.sleep(0.1)
            _, c = self.req("GET", "/api/config"); self.assertFalse(c["has_key"]); self.assertIsNone(c["key_source"])
            _, cr = self.req("GET", "/api/credits"); self.assertIsNone(cr["credits"]); self.assertTrue(cr["error"].startswith("401"))
            st, r = self.req("POST", "/api/key", {"key": "short"}); self.assertEqual(st, 400)
            st, r = self.req("POST", "/api/key", {"key": "rejected-key-0000000000"}); self.assertEqual(st, 400); self.assertIn("rejected", r["error"])
            self.assertFalse(os.path.exists(keyfile))
            st, r = self.req("POST", "/api/key", {"key": "  Bearer good-key-1234567890abcdef \n"}); self.assertEqual(st, 200, r)
            self.assertEqual(open(keyfile).read().strip(), "good-key-1234567890abcdef")
            if os.name != "nt": self.assertEqual(os.stat(keyfile).st_mode & 0o777, 0o600)
            _, c = self.req("GET", "/api/config"); self.assertTrue(c["has_key"]); self.assertNotIn("good-key", json.dumps(c))
            _, cr = self.req("GET", "/api/credits?fresh=1"); self.assertEqual(cr["credits"], FakeKie.balance)
        finally:
            self.base = base; p.terminate(); p.wait(5)

    def test_zz_restart_recovery(self):
        # a task accepted by kie must survive a server restart and still complete
        FakeKie.slow = True
        _, r = self.video_job()
        self.wait(r["id"], until=("running", "downloading", "success"))
        self.proc.terminate(); self.proc.wait(5); type(self).start()
        j = self.wait(r["id"]); self.assertEqual(j["status"], "success")


if __name__ == "__main__":
    unittest.main()
