# kie studio

Local web UI + CLI for [kie.ai](https://kie.ai) video and image generation. One Python repo, no npm:
Flask serves a plain HTML/CSS/JS front end (no build step), SQLite holds history, ffmpeg does posters/frames/stitching.

```
kie_studio/
  server.py      Flask app, job runner, poller, asset normalisation, migration from the v1 history.json
  client.py      kie.ai API client (stdlib only)
  models.json    model registry: params, modes, image slots, prices   ← edit this to add models
  __main__.py    CLI (serve / gen / jobs / models / credits / task)
  static/        index.html, style.css, app.js
deploy/          systemd --user unit + install.sh
tests/           test_e2e.py (fake kie API, no credits) + browser_smoke.py (headless Chrome)
```

## Run

```zsh
./deploy/install.sh                 # systemd --user service on http://127.0.0.1:8770 (stops the old kie-video-ui.py)
python3 -m kie_studio serve         # or run it in the foreground
journalctl --user -u kie-studio -f  # logs
```

Requires `python3-flask python3-pil ffmpeg` and the API key in `~/.config/kie/token`.
Data lives in `~/Videos/kie/`: `studio.db`, `assets/` (input images, deduplicated), `images/`, `posters/`, videos at top level.
The v1 `history.json` + `uploads/` are imported once on first start and left untouched.

| env | default | |
|---|---|---|
| `KIE_STUDIO_PORT` | 8770 | |
| `KIE_STUDIO_HOST` | 127.0.0.1 | anything non-local requires `KIE_STUDIO_TOKEN`; open `/?token=…` once to set the cookie |
| `KIE_STUDIO_MEDIA` | ~/Videos/kie | |
| `KIE_API_KEY` | — | overrides the token file |

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
- API key never reaches the browser. Host-header check (DNS rebinding) + required `X-Studio` header (CSRF from other sites).
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
