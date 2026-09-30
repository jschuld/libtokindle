# libtokindle — Plan

Goal: borrow an ebook from Auckland Libraries on your phone, hand it to a small
self-hosted service, and have it arrive on your Kindle a minute later.

```
 Phone (Libby)                     Home server / NAS / Pi (Docker)                    Amazon
┌──────────────┐  .acsm file   ┌──────────────────────────────────────────┐   email   ┌─────────┐
│ Borrow book  │ ────────────▶ │ 1. fulfil  .acsm → encrypted .epub       │ ────────▶ │ Send to │
│ Download as  │  (share sheet │ 2. strip Adobe ADEPT DRM → plain .epub   │  SMTP     │ Kindle  │
│ EPUB (.acsm) │   / email-in) │ 3. (optional) clean up with Calibre      │           │ → device│
└──────────────┘               │ 4. email .epub to you@kindle.com         │           └─────────┘
                               └──────────────────────────────────────────┘
```

## 1. How the pieces fit

### What Auckland Libraries gives you
- **Libby / OverDrive**: most titles are offered as an "EPUB" loan, which you
  download as a small `.acsm` file (an Adobe Content Server ticket, not the book
  itself). In Libby this is under the loan's **Read With… → (other options) →
  EPUB** menu, when the publisher allows it. Libby's own "Read with Kindle"
  option is US-only, so it isn't available in NZ. That's the gap this project fills.
- **BorrowBox**: books are locked inside the BorrowBox app. You can't get an
  `.acsm` file, so **BorrowBox is out of scope**.
- Kindle can't open Adobe-DRM EPUBs, but **Send to Kindle accepts plain EPUB**
  and converts it on Amazon's side.

### The conversion pipeline
| Step | Tool | Notes |
|---|---|---|
| Fulfil `.acsm` → encrypted EPUB | [libgourou](https://forge.soutade.fr/soutade/libgourou) `acsmdownloader` | Open-source Adobe ADEPT client. Needs a one-time device activation (`adept_activate`, anonymous or with an Adobe ID). The activation files (`device.xml`, `activation.xml`, `devicesalt`) persist in a volume. |
| Remove DRM | libgourou `adept_remove` | Uses the same activation keys. |
| Optional cleanup | Calibre `ebook-convert` / `ebook-polish` | Only if Amazon rejects a file (bad EPUB structure is the usual reason). |
| Deliver | SMTP → `yourname@kindle.com` | The sender address must be on Amazon's *Approved Personal Document E-mail List*. Limit is 50 MB per email. |

Existing Docker images wrap libgourou (e.g. `bcliang/docker-libgourou`). We'll
use one as a base image, or build libgourou ourselves in a multi-stage
Dockerfile, which also gives us arm64 for a Raspberry Pi. **Alternative
considered**: `linuxserver/calibre` with the DeACSM and DeDRM plugins. It works,
but it needs a desktop GUI and is hard to automate. libgourou is headless and
scriptable, so we use it.

## 2. Getting the file from the phone to the service

Offer two ways in. Both feed the same pipeline.

1. **Web upload + share sheet (primary)**
   - A tiny web app (FastAPI, one page) with a file picker and a "Send to Kindle" button.
   - **Android**: make it an installable PWA with a Web Share Target, so the
     `.acsm` can be shared straight from the downloads / Libby "open with" dialog.
   - **iOS**: an Apple Shortcut ("Share → Send to Kindle") that POSTs the file to
     `/api/upload` with a bearer token. Share sheets make this one tap.
2. **Email-in (fallback, zero client setup)**
   - A dedicated mailbox (e.g. a Gmail alias). The service polls it over IMAP,
     takes any `.acsm` attachment, runs the pipeline and replies with the result.
   - Useful from any device, including a desktop browser.

**Network exposure**: don't open the service to the internet. Run it on the home
network and reach it from the phone via **Tailscale**, or a Cloudflare Tunnel
with Access in front. It still needs an upload token either way.

## 3. Service design

```
libtokindle/
├── docker-compose.yml        # one service, volumes for /config and /data
├── Dockerfile                # multi-stage: build libgourou → python:slim runtime (+ optional calibre)
├── app/
│   ├── main.py               # FastAPI: GET / (upload page), POST /api/upload, GET /api/jobs/{id}
│   ├── pipeline.py           # fulfil → dedrm → (cleanup) → send; each step a subprocess with timeout
│   ├── mailer.py             # SMTP send with attachment
│   ├── imap_watcher.py       # optional email-in poller (enabled via env)
│   ├── jobs.py               # SQLite job log: status, title, error, timestamps
│   └── static/               # upload page, PWA manifest + share_target, service worker
├── scripts/activate.sh       # one-time adept_activate into /config/adept
└── tests/                    # pipeline tests with mocked subprocess + SMTP
```

**Config (env / `.env`)**: `KINDLE_EMAIL`, `SMTP_HOST/PORT/USER/PASSWORD`,
`SMTP_FROM`, `UPLOAD_TOKEN`, optional `IMAP_*`, `KEEP_FILES=false`.

**Job flow**
1. Upload → validate the file is XML with an `<fulfillmentToken>` root and under 64 KB → create job → run in a background task.
2. `acsmdownloader -f book.acsm -o /tmp/job/enc.epub` (the ACSM ticket expires, so do this right away).
3. `adept_remove -f /tmp/job/enc.epub -o /tmp/job/book.epub`.
4. Rename to `Title - Author.epub` from the EPUB's OPF metadata. Send to Kindle's subject line is left blank, so Amazon keeps the metadata.
5. Email it and mark the job done. The page polls `/api/jobs/{id}` and shows ✅ or the error.
6. Delete the working files (unless `KEEP_FILES`). Nothing is stored long term.

**Error cases to handle clearly**: ticket already fulfilled or expired
(re-download the `.acsm` from Libby), device not activated, Adobe's servers
unreachable, a PDF loan instead of EPUB (send the PDF as is), SMTP auth failure,
and a file over 50 MB.

## 4. Milestones

1. **Spike (manual, ~1 evening)**: run a libgourou container by hand, activate
   it, fulfil one real Auckland Libraries `.acsm`, remove the DRM, email it to
   Kindle. This proves the whole chain before writing any code.
2. **Pipeline container**: Dockerfile plus `pipeline.py` as a CLI
   (`libtokindle send book.acsm`), with activation persisted in a volume.
3. **Web service**: FastAPI upload page, job status, token auth, docker-compose.
4. **Phone integration**: PWA share target (Android), iOS Shortcut, and a README with screenshots.
5. **Email-in** (optional): IMAP watcher.
6. **Hardening**: tests, arm64 build via GitHub Actions, a healthcheck, and a cron job that clears old job rows.

## 5. Things to be aware of

- **Adobe activation**: each activation uses one of your Adobe ID's device slots
  (about 6). Activate once and keep `/config/adept` backed up, rather than
  re-activating on every container rebuild.
- **Legal / licensing**: stripping DRM is a grey area. NZ's Copyright Act has
  anti-circumvention provisions, and library ebooks are licensed rather than
  owned. Keep this strictly personal: one reader, your own loans, and delete the
  book from the Kindle (Manage Content) when the loan ends. The service keeps no
  copies by default, so it doesn't turn into a library.
- **Fragility**: Adobe or OverDrive can change things. Libby could hide the EPUB
  option for some titles, or Adobe could change the ADEPT protocol. libgourou
  is maintained, so pin a known-good version and update deliberately.

## 6. Decisions (answered)

- **Hosting**: Docker at home, reached from the iPhone over Tailscale. A Google
  Cloud free-tier `e2-micro` VM is the documented alternative.
- **Client**: an iPhone using a plain web page in Safari with just an upload
  button, plus a watched Google Drive folder. No Shortcut, share target or email-in.
- **Email**: generic SMTP settings. Gmail with an app password is the documented default.
- **libgourou**: use the prebuilt `bcliang/docker-libgourou` image as the base image (the home PC is an Intel i7, so amd64) instead of compiling from source.
- **Libby**: own small Python client for Libby's (unofficial) API rather than libby-archiver (too new, Node, rebuilds EPUBs from the web reader) or odmpy (unmaintained since 2023, GPL, no holds). Fetch the loan's Adobe EPUB .acsm (or a DRM-free EPUB when offered) and reuse the libgourou pipeline. Auto-borrow on, skip existing loans at connect, check every 30 minutes (decided with the user).
- **Jobs**: kept in memory (last 50), because there's one user and no history needs to survive a restart.

## 7. Status

- [x] Pipeline (`app/pipeline.py`), mailer, and job store
- [x] Web app: upload page, token auth, job status (`app/main.py`, `app/static/index.html`)
- [x] Dockerfile built on `bcliang/docker-libgourou:0.8.9-ubuntu` (amd64), docker-compose, `activate` command
- [x] Tests with fake libgourou tools
- [x] Google Drive folder watcher (`app/drive.py`, service account with read-only access, polls every 60 s)
- [x] Pass/fail email to the Gmail address after every job (`NOTIFY_EMAIL`, defaults to `SMTP_USER`)
- [x] Settings page (`/settings`): all settings, Google key upload, Adobe activation and a test email, saved to `/config/settings.json`, with no file editing needed
- [x] Persistent log files (`/config/logs`, daily) and book history (`/config/history.json`), deleted after 1–30 days (default 30); Logs page (`/logs`)
- [x] Libby integration (`app/libby.py`): signs in once with the library card number + PIN (PIN not stored; the setup-code flow was dropped because current Libby makes the new device show the code), checks every 30 min, auto-borrows ready ebook holds, sends new ebook loans through the existing .acsm pipeline; loans present at connect are skipped (Send to Kindle button for those)
- [ ] First real Libby run on the home server (the Libby API can't be reached from the dev environment)
- [x] First real run: build the image, activate, and send one Auckland Libraries loan (milestone 1)
