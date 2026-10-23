# Kiester — a kie.ai API interface

A desktop app for generating videos and images with the AI models available through [kie.ai](https://kie.ai)
(Wan, Seedance, Grok Imagine, FLUX.2, Qwen Image, Seedream…). Windows, macOS and Linux.

> **Unofficial.** Kiester is an independent open-source project. It is not affiliated with, endorsed by or supported
> by kie.ai. You use your own kie.ai account and API key, and kie.ai bills generations to that account.

## Install (3 steps)

1. **Download** — click the green **Code** button above → **Download ZIP**, and unzip it somewhere permanent
   (e.g. your Documents folder). Or: `git clone https://github.com/shahar-cohen/kiester`
2. **Run the installer** for your system:

   | | |
   |---|---|
   | **Windows** | double-click **`install.bat`**. If Windows shows a security warning, choose **Run** (or **More info → Run anyway**). Python is installed automatically if needed (no admin rights). |
   | **macOS** | double-click **`install.command`**. If macOS says it can't be opened: **right-click → Open → Open**. If Python is missing, the download page opens — install it, then run `install.command` again. |
   | **Linux** | run **`./install.sh`** in a terminal (it may ask for your password to install `python3-venv` / `python3-tk`). |

3. In the installer window choose your shortcuts, click **Install**, then **Open Kiester**.

Kiester opens in its own window. The first time, it asks for your kie.ai API key — see below.

### Getting a kie.ai API key

An **API key** is a long secret code that lets an app use your kie.ai account — like a password, but only for apps.
Anyone who has it can spend your credits, so don't share or post it. You can reset it on kie.ai at any time
(the old key then stops working).

1. **Sign in to kie.ai** or create an account: https://kie.ai/login
2. **Add credits** — every generation costs credits (a few cents to a few dollars each).
   Prices: https://kie.ai/pricing · top up: https://kie.ai/billing
3. **Copy your key** from https://kie.ai/api-key (create one there if the page has none yet).
4. **Paste it into Kiester** when it asks (or into the installer). Kiester checks it with kie.ai before saving.

The key is stored only on your computer, in `~/.config/kie/token` (readable by your user only), and is sent only to
kie.ai. Change it later with the 🔑 button in the app.

### Uninstall
Run the installer again and click **Uninstall**. It removes the shortcuts, autostart and Kiester's private Python
environment; your videos, images, history and API key are kept. Then delete the folder.

## How it works

One Python repo, no npm, no Electron: a small Flask server on `http://127.0.0.1:8770` (this computer only) serves a
plain HTML/CSS/JS front end that opens in a Chrome / Edge / Brave / Chromium app window (`--app` mode: no tabs or
address bar), falling back to the default browser. Edge ships with Windows, so Windows always gets the app window.
SQLite holds history; ffmpeg makes thumbnails, grabs frames and stitches clips.

```
install.bat / install.command / install.sh   double-click installers (Windows / macOS / Linux)
deploy/installer.py   installer window (Tk) + text mode; stdlib only
kiester/
  server.py      Flask app, job runner, poller, asset normalisation, migration from the v1 history.json
  client.py      kie.ai API client (stdlib only)
  paths.py       data folder location
  models.json    model registry: params, modes, image slots, prices   ← edit this to add models
  __main__.py    CLI (serve / open / gen / jobs / models / credits / task)
  static/        index.html, style.css, app.js
tests/           test_e2e.py (fake kie API, no credits) + browser_smoke.py (headless Chrome)
```

The installer puts Python packages in `.venv/` inside the Kiester folder, for your user only. Without a desktop
(or with `--console`) it asks the same questions in the terminal; `--uninstall --console` uninstalls.

| | default |
|---|---|
| data | `~/Videos/Kiester/` (macOS: `~/Movies/Kiester/`) — `studio.db`, `assets/`, `images/`, `posters/`, videos, `kiester.log`. Installs from before the rename keep using `~/Videos/kie/`. |
| ffmpeg | the system one if installed, otherwise the copy bundled by the `imageio-ffmpeg` package |
| autostart (optional) | Linux: systemd `--user` unit (`journalctl --user -u kiester`), macOS: LaunchAgent, Windows: Startup folder |

Environment overrides: `KIESTER_PORT` (8770), `KIESTER_MEDIA`, `KIE_API_KEY` (overrides the saved key),
`KIE_KEY_FILE`, `KIESTER_HOST` (non-local addresses require `KIESTER_TOKEN`; open `/?token=…` once).

For development: `python3 -m kiester serve` with flask, pillow and (optionally) imageio-ffmpeg installed.

## CLI

```zsh
python3 -m kiester models
python3 -m kiester gen wan3 i2v "she turns and smiles" --image first_frame_url=a.jpg --set duration=5 --set resolution=720P --wait
python3 -m kiester jobs -n 5
python3 -m kiester credits
```
`gen` goes through the running server, so CLI jobs appear in the UI history.

## Pitfalls handled

**Money**
- Double-click / network-retry can't double-bill: every submit carries a `client_id`; duplicates return the existing job.
- `createTask` is never retried automatically (a 5xx may still have created a billed task).
- Pre-flight balance check before uploading anything (kie otherwise accepts and fails later); confirm dialog above 200 credits.
- Cost estimate per model/resolution/duration; models without a published price fall back to the average of your own past runs.
- kie has no cancel: "Dismiss" only stops tracking and says so.

**Reliability**
- Jobs live in SQLite; a server restart re-polls tasks kie already accepted and resumes interrupted downloads.
  Jobs interrupted *during* `createTask` are flagged "check the kie dashboard before rerunning".
- Downloads are retried with backoff, written to `.part` then renamed; if they still fail, "Retry download" works for 14 days.
- kie's HTTP-200-with-error-code responses, 429/5xx retries, and vendor content-filter failures get plain-language messages.
- Per-mode health dots show the last 5 outcomes, so a model whose endpoint is down is visible before you spend.

**Inputs**
- Images are normalised on upload: EXIF rotation, HEIC rejected with instructions, GIF/BMP/TIFF/CMYK converted,
  >4096 px downscaled, <240 px upscaled, >8:1 aspect rejected, >19 MB recompressed.
- Content-hash dedupe; kie-side uploads are reused for 20 h (kie deletes them after ~24 h, then they're re-uploaded).
- Param types are coerced to what each endpoint wants (some want `"5"`, others `5`); values are clamped server-side.
- `@ImageN` references beyond the attached images are flagged; switching models keeps prompt/images/valid params
  but never carries `seed` (0 = random on Wan, fixed on Seedance).

**Security**
- API key never reaches the browser (the key screen sends it once; it is never echoed back). Host-header check (DNS rebinding) + required `X-Kiester` header (CSRF from other sites).
- `/media` never serves the database, JSON, dotfiles or partial downloads.

## Workflow features
Reuse / Rerun any job · continue a video from its last frame · use a generated image as input · stitch clips with ffmpeg
(mismatched sizes/missing audio handled) · favourites, notes, search, filters · drag-drop / paste images ·
desktop notifications · drafts survive reloads · light/dark theme · phone layout.

## Tests
```zsh
python3 -m unittest discover -s tests -v                 # full flow against a fake kie API, ~10 s
python3 tests/browser_smoke.py http://127.0.0.1:8770/    # UI smoke test, reports JS errors (spends nothing)
```
