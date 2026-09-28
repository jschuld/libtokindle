# libtokindle

Self-hosted service that sends Auckland Libraries (Libby/OverDrive) ebook loans to a
Kindle. Upload a loan's `.acsm` file, drop it (or an EPUB/PDF) in a watched Google
Drive folder, or let it watch the Libby account (auto-borrows ready holds and sends new
ebook loans). The service downloads the book with libgourou, strips the Adobe ADEPT DRM,
emails the EPUB to the Kindle's Send to Kindle address, and emails a ✅/❌ result to the
user's Gmail. `README.md` is the user guide; `PLAN.md` holds the original design and the
decision log.

## The user's setup and preferences

- Runs it with Docker on a home **Intel i7 PC (amd64)**. A Raspberry Pi isn't supported,
  because the base image is amd64-only. The Google Cloud free tier is documented but unused.
- Uses an **iPhone with Safari**. They want a plain web page with **just an upload
  button**: no Apple Shortcut, share target or email-in. Also a watched **Google Drive
  folder** (Libby → Share → Save to Files → Drive).
- Email goes through Gmail with an app password. The Kindle address is on Amazon's
  approved sender list.
- Wants no file editing on the server: everything is configured on the `/settings` page.
- Logs and history must be kept **at most 30 days** (a hard cap in code).
- Libby: **auto-borrow ready holds: yes**; **skip loans that exist when connecting**;
  **check every 30 minutes**.
- Updates on the server with `git pull && docker compose up -d --build`.
- Work on the branch the session names (so far `claude/drm-removal-kindle-workflow-fxqaly`),
  commit, and push. No PRs unless asked.

## Layout

```
app/
  main.py       FastAPI app: pages (/, /settings, /logs), JSON API, token auth,
                apply_config() hot-reloads settings and restarts the Drive watcher,
                hourly housekeeping thread (prunes logs and history)
  config.py     Config dataclass; Config.load() = env vars overlaid with /config/settings.json;
                parse_drive_folder_id() accepts a folder link or an ID
  settings.py   what the Settings page edits (EDITABLE, SECRETS), validation, saving
                (mode 600), Google key upload, Adobe activation (adept_activate)
  pipeline.py   process(): .acsm → acsmdownloader → adept_remove → rename from OPF
                metadata → email. Other Kindle formats are sent as they are. notify() sends
                the result email and never raises
  drive.py      DriveWatcher: polls one folder with a service account (drive.readonly)
                through the Drive v3 REST API and google-auth AuthorizedSession
  libby.py      LibbyClient (unofficial Libby API: link by setup code, sync, borrow,
                open + fulfill a loan) and LibbyWatcher (polls, auto-borrows ready ebook
                holds, sends new ebook loans through pipeline.process, rate-limit backoff)
  jobs.py       JobStore: book history, persisted to /config/history.json, trimmed by retention
  logs.py       daily TimedRotatingFileHandler in /config/logs, age-based prune(), read()
  mailer.py     SMTP (STARTTLS on 587, SSL on 465): send_to_kindle(), send_notification()
  static/       index.html (upload), settings.html, logs.html, common.js (api(), token in
                localStorage), style.css (light and dark)
tests/          pytest; conftest.py points every /config path at a temp dir
Dockerfile      FROM bcliang/docker-libgourou:0.8.9-ubuntu, plus a Python venv
docker-entrypoint.sh  `serve` (uvicorn) | `activate` (command-line Adobe activation)
docker-compose.yml    port 8080→8000, ./config:/config, .env optional (needs Compose ≥ 2.24)
```

## Runtime files (in `./config` on the server; back it up)

| File | What it holds |
|---|---|
| `adept/` (`activation.xml`, `device.xml`, `devicesalt`) | Adobe device keys. Re-activating uses up one of the Adobe ID's roughly 6 device slots |
| `settings.json` | Values saved on the Settings page, keyed by env var name; they override `.env` |
| `google-service-account.json` | Drive service account key |
| `drive-state.json` | `{"folder", "since", "seen": [ids]}`. Files created before `since` are skipped |
| `history.json` | Book history (list of Job dicts; `source` is upload, drive or libby) |
| `libby.json` | Libby identity token (mode 600). Its presence means "connected" |
| `libby-state.json` | `{"seen": [loan keys], "hold_failures": [hold keys]}`. Key = `cardId:titleId:checkoutDate` (or `placedDate` for holds) |
| `logs/libtokindle.log[.YYYY-MM-DD]` | Daily logs |

## libgourou facts (learned the hard way)

- Version 0.8.9 in the image. The tools are in `/usr/local/bin`: `acsmdownloader`,
  `adept_activate`, `adept_remove` and `adept_loan_mgt`.
- Use the **long options, with the input file as a positional argument**:
  - `acsmdownloader --adept-directory D --output-dir OUT file.acsm` (it names the output
    file itself; the code picks up the `*.epub`/`*.pdf` it writes).
  - `adept_remove --adept-directory D --output-file /full/path.epub in.epub`.
    **Don't pass `--output-dir` and `--output-file` together**: it fails with
    "you cannot use both -o and -O".
  - `adept_activate --random-serial --anonymous --output-dir D` (or `--username/--password`).
    It won't overwrite a half-made output dir, so settings.py deletes one without
    `activation.xml` first.
- An `.acsm` can only be fulfilled once, and it expires. The fix is always "download it
  again from Libby".
- An EPUB is DRM-protected if it contains `META-INF/rights.xml`.

## Libby facts

- There's no official API. Base URL `https://sentry-read.svc.overdrive.com`, JSON, with the
  header `Authorization: Bearer <identity>`.
  - Link: `POST chip?client=dewey` (anonymous) → `POST chip/clone/code {"code": "12345678"}`
    with that token → `POST chip?client=dewey` again with the token (so the identity
    carries the cards) → save it. A 401 later is handled by re-POSTing `chip` with the
    old token.
  - `GET chip/sync` returns `cards`, `loans` and `holds`. A ready hold has `isAvailable: true`.
    Type is in `type.id` (`ebook`, `audiobook`, `magazine`), formats in `formats[].id`.
  - Borrow: `POST card/{cardId}/loan/{titleId}` with
    `{"period": N, "units": "days", "lucky_day": null, "title_format": "ebook"}`. N comes
    from `card.lendingPeriods[type or "book"].preference[0]`, else the last `options`
    entry, else 21.
  - Before fulfilling: `GET open/{type}/card/{cardId}/title/{titleId}` (failure only
    logged). Fulfill: `GET card/{cardId}/loan/{titleId}/fulfill/{format}` →
    `ebook-epub-adobe` returns the .acsm body, and `ebook-epub-open` returns a 302 to a
    DRM-free file. Preference order: epub-open, epub-adobe, pdf-open, pdf-adobe.
  - Rate limiting: `403 {"result": "whoa"}` or 429 → `LibbyRateLimited` → the wait
    doubles, up to 4 hours.
- References: odmpy (`ping/odmpy`, GPL, unmaintained) and ping's Libby calibre plugin.
  Don't copy their code. libby-archiver (`JavaGT/libby-archiver`, Node, Sept 2026) was
  evaluated and rejected as the core: too new, and it rebuilds EPUBs from the web reader.
- Untested against real Libby: the dev environment's proxy blocks
  sentry-read.svc.overdrive.com. Whether fulfilling `ebook-epub-adobe` locks the loan's
  format in the Libby app is unverified.

## Google Drive facts

- The folder must be shared with the service account's `client_email` as Viewer, and the
  Drive API must be enabled in its Cloud project. `_check()` turns 404 and 403 responses
  into plain-English messages that name that address.
- A user once put the full folder URL in `DRIVE_FOLDER_ID` (through `.env`), which caused
  404s. `parse_drive_folder_id` is applied in `Config.from_env`, so a link works from any
  source.
- A failing check is logged once (and again on recovery), not on every poll.

## Develop and test

```sh
pip install -r requirements-dev.txt
python -m pytest -q            # currently 87 tests
UPLOAD_TOKEN=dev uvicorn app.main:app --reload
```

- `tests/test_libby.py` has a `FakeLibby` HTTP fake (chip, clone, sync, open, fulfill
  with redirect, borrow, 401 expiry, `whoa` rate limit). Extend it when the Libby client
  changes.
- The tests use **fake libgourou tools** (`fake_tool()` in tests/test_pipeline.py writes
  small Python scripts) and fake Drive and SMTP. Make the fakes reject what the real
  tools reject: the `-o`/`-O` bug slipped through because the fake accepted it.
- Never let tests write to the real `/config`. `tests/conftest.py` sets `SETTINGS_FILE`,
  `HISTORY_FILE`, `LOG_DIR`, `ADEPT_DIR`, `GOOGLE_SERVICE_ACCOUNT_FILE` and
  `DRIVE_STATE_FILE` to a temp dir before `app.main` is imported.
- `ADEPT_ACTIVATE`, `ACSMDOWNLOADER` and `ADEPT_REMOVE` env vars override the tool paths,
  which the tests use.
- For UI checks, drive the pages with Playwright (Node) from the scratchpad against
  Chromium in `/opt/pw-browsers`, at the `iPhone 13` device size. This caught real
  bugs: a `[hidden]` element shown anyway, and saving the page unchanged turning
  result emails off.
- Round-trip rule for Settings: saving the page exactly as it loaded must change nothing.
  `settings.current()` shows the *saved* value of defaultable fields such as
  `NOTIFY_EMAIL`, not the resolved one.

## Limits of the cloud dev environment

- `sentry-read.svc.overdrive.com` (Libby) is blocked by the proxy too.
- There's **no Docker daemon**, `forge.soutade.fr` (libgourou source) is blocked, and
  Docker Hub is rate-limited, so the image can't be built or inspected here. The user
  builds and tests it on their PC. Say so plainly rather than claiming it's verified.
- Removing `/config` in the container is blocked by a safety check. Keep tests out of it.
- The system `cryptography` package here is broken for google-auth; run
  `pip install --ignore-installed cryptography cffi` locally if imports fail. This doesn't
  affect Docker, which uses its own venv.
- If pushing returns 403, the Claude GitHub App needs access to `jschuld/libtokindle`.

## Conventions

- Error messages shown to the user (web page, result email) say what to do next.
  `PipelineError`, `SettingsError` and `DriveError` carry those messages.
- Secrets (`SMTP_PASSWORD`, `UPLOAD_TOKEN`) are never returned by the API or logged.
  Settings changes log only the key names.
- After each change: run the tests, update README.md (user-facing) and PLAN.md
  (status/decisions) when behaviour changes, commit with a descriptive message, and push.
- Legal note: this is for the user's own loans. It keeps no copies of books (working
  files are deleted unless `KEEP_FILES=true`).
