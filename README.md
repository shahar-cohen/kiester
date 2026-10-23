# kie studio

Local web UI + CLI for [kie.ai](https://kie.ai) video and image generation, for Windows, macOS and Linux.
One Python repo, no npm, no Electron: Flask serves a plain HTML/CSS/JS front end (no build step) that opens in a
Chrome/Edge app window; SQLite holds history; ffmpeg does posters/frames/stitching.

```
install.bat / install.command / install.sh   double-click installers (Windows / macOS / Linux)
kie_studio/
  server.py      Flask app, job runner, poller, asset normalisation, migration from the v1 history.json
  client.py      kie.ai API client (stdlib only)
  models.json    model registry: params, modes, image slots, prices   ← edit this to add models
  __main__.py    CLI (serve / open / gen / jobs / models / credits / task)
  static/        index.html, style.css, app.js
deploy/installer.py   the installer window (Tk) + text mode; stdlib only
tests/           test_e2e.py (fake kie API, no credits) + browser_smoke.py (headless Chrome)
```

## Install

Download the repo (or `git clone`), then double-click the installer for your system:

| | |
|---|---|
| **Windows** | `install.bat` — installs Python 3.13 with winget if it's missing (no admin needed) |
| **macOS** | `install.command` — if macOS blocks it: right-click → Open → Open. Needs Python from python.org (the installer opens the download page if it's missing) |
| **Linux** | `./install.sh` — installs `python3-venv` / `python3-tk` with your package manager if missing (asks for your password) |

The installer window lets you choose an app-menu / Start-menu entry, a desktop shortcut, starting at login, and
optionally paste the API key. Everything installs for your user only: Python packages go into `.venv/` inside this folder.
**Uninstall** is a button in the same window (`--uninstall --console` in text mode); it keeps your media, history and key.
Without a desktop (or with `--console`) it asks the same questions in the terminal.

The **kie studio** shortcut starts the server in the background if it isn't running and opens it in its own
app window (Chrome / Edge / Brave / Chromium `--app` mode; no tabs or address bar), falling back to the default browser.
Edge ships with Windows, so Windows always gets the app window. The UI is always at http://127.0.0.1:8770.

On first start the app asks for your kie.ai API key (from https://kie.ai/api-key), checks it with kie.ai and saves it
to `~/.config/kie/token` (user-only permissions). Change it later with the 🔑 button.

| | default |
|---|---|
| data | `~/Videos/kie/` (macOS: `~/Movies/kie/`) — `studio.db`, `assets/`, `images/`, `posters/`, videos, `kie-studio.log` |
| ffmpeg | the system one if installed, otherwise the copy bundled by the `imageio-ffmpeg` package |
| autostart | Linux: systemd `--user` unit (`journalctl --user -u kie-studio`), macOS: LaunchAgent, Windows: Startup folder |

Environment overrides: `KIE_STUDIO_PORT` (8770), `KIE_STUDIO_MEDIA`, `KIE_API_KEY` (overrides the saved key),
`KIE_KEY_FILE`, `KIE_STUDIO_HOST` (non-local addresses require `KIE_STUDIO_TOKEN`; open `/?token=…` once).

For development: `python3 -m kie_studio serve` with flask, pillow and (optionally) imageio-ffmpeg installed.

## CLI

```zsh
python3 -m kie_studio models
python3 -m kie_studio gen wan3 i2v "she turns and smiles" --image first_frame_url=a.jpg --set duration=5 --set resolution=720P --wait
python3 -m kie_studio jobs -n 5
python3 -m kie_studio credits
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
- API key never reaches the browser (the key screen sends it once; it is never echoed back). Host-header check (DNS rebinding) + required `X-Studio` header (CSRF from other sites).
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
