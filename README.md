# libtokindle

Send Auckland Libraries (Libby) ebook loans to your Kindle from your phone's browser.

Upload the loan's `.acsm` file on a small web page. The service downloads the book,
removes the Adobe DRM with [libgourou](https://forge.soutade.fr/soutade/libgourou)
(through the `bcliang/docker-libgourou` image), and emails the EPUB to your Kindle's
Send to Kindle address. See [PLAN.md](PLAN.md)
for the design.

> Use this only for your own loans, and delete the book from your Kindle when the loan ends.

## Using it

1. In **Libby**, borrow the book, then open it under **Loans → Read With… → EPUB**
   (the wording can differ). Safari downloads a small `.acsm` file.
2. Open the libtokindle page in Safari, tap **Upload .acsm file**, and choose the file
   from **Downloads**.
3. Within a few minutes the book appears on your Kindle.

**Or through Google Drive:** in Libby's download, tap **Share → Save to Files**, choose
your watched Google Drive folder, and save. Within about a minute the service picks it up
and sends it. EPUB and PDF files you put in that folder are sent as they are.

**Or automatically from Libby:** connect your Libby account on the Settings page. When a
hold becomes ready, the service borrows it and sends it to your Kindle, and any new ebook
loan is sent too. See [Connect Libby](#optional-connect-libby).

Every way, you get an email at your Gmail address saying whether it worked (✅) or
failed (❌), with the reason. The ✅ email has the converted book attached (unless it's
over about 18 MB, which Gmail won't take), and every sent book is also kept on the
**Downloads** page (see below).

Each `.acsm` file can be used only once, and it expires after a while. If the upload
fails with a download error, download the `.acsm` again from Libby.

Books in BorrowBox can't be sent, because BorrowBox doesn't offer an `.acsm` file.

## Setup (Docker at home)

You need an always-on computer at home that runs Docker on a normal Intel/AMD (amd64) processor.
The image is built on [`bcliang/docker-libgourou`](https://hub.docker.com/r/bcliang/docker-libgourou),
which provides the libgourou tools and is published for amd64 only (not Raspberry Pi).

### 1. Before you start
- **Amazon**: find your Send to Kindle address (amazon.com → *Manage Your Content and
  Devices* → *Preferences* → *Personal Document Settings*). On the same page, add your
  Gmail address to the **Approved Personal Document E-mail List**.
- **Gmail**: create an app password at <https://myaccount.google.com/apppasswords>
  (this needs 2-step verification turned on).

### 2. Build and start
```sh
git clone https://github.com/jschuld/libtokindle && cd libtokindle
docker compose up -d --build
```

### 3. Set it up in the browser
Open `http://<server-ip>:8080`.
1. Choose an **access token**: a password of at least 12 characters for these pages.
2. You land on **Settings**. Fill in your Kindle address, Gmail address and app
   password, then tap **Save and send a test email**.
3. Under **Adobe device**, tap **Activate**. This is a one-time step.

Everything is saved in `./config` on the server, and changes apply as soon as you save.
**Back up `./config`**: it holds the Adobe keys, and re-activating uses up another of
your Adobe account's limited device slots.

`/healthz` returns `{"ok": true}` once everything is set up.

Settings can also come from a `.env` file (see `.env.example`). Anything saved on the
Settings page takes priority over it. If the server is reachable by people you don't
trust, set `UPLOAD_TOKEN` in `.env` before the first start, because otherwise whoever
opens the page first chooses the token.

### 4. Reach it from your iPhone
Don't open the service to the internet. Instead:
- **At home only**: open `http://<server-ip>:8080` on your home Wi-Fi.
- **Anywhere (recommended)**: install [Tailscale](https://tailscale.com) (free) on the
  server and your iPhone, then open `http://<server-name>:8080`.

In Safari, **Share → Add to Home Screen** makes it behave like an app. Enter your
access token the first time; Safari remembers it.

## Logs and history

The **Logs** page (link at the top of the main page) has two tabs:
- **Books**: every file sent from the web page or Google Drive, with ✅/❌ and the reason
  for any failure. It survives restarts and updates.
- **Log**: the service's log for each day, with a **Problems only** filter.

Both are stored in `./config` on the server (`logs/` and `history.json`) and deleted
automatically after 30 days. You can shorten that under **Settings → Logs and history**,
anywhere from 1 to 30 days. `docker compose logs -f` shows the same log live, plus a
line for every web request.

## Downloads

Every book sent to your Kindle is also kept on the server, in `./config/books`, and listed
on the **Downloads** page (link at the top of the main page). Tap **Download** to save the
EPUB to your phone, or **Delete** to remove it from the server (it stays on your Kindle).
If the email to your Kindle fails, the converted book is still kept here.

Books are deleted automatically after the same period as the logs: 30 days, or less if you
change **Settings → Logs, history and downloads**. Download links on the page are signed
and expire after 10 minutes, so the files aren't reachable without your access token.

## Optional: connect Libby

The service can watch your Libby account. Every 30 minutes it checks for:
- **Holds that are ready**: it borrows them straight away (you can turn this off), using
  your library's usual loan length.
- **New ebook loans**, including ones it just borrowed: it gets the book from Libby,
  removes the DRM and sends it to your Kindle.

Loans you already have when you connect are skipped. **Your Libby loans** on the main
page shows what happened to each loan (sent, failed, or skipped because you already had
it), with a **Send to Kindle** button to send one now. Audiobooks and magazines are
ignored. A few titles can only be read in the Libby app or a browser; you get a ❌ email
for those.

To connect, go to the **Settings** page, find **Libby**, and enter:
- **Library**: already filled in as `aucklandlibraries`. It's the word after
  `libbyapp.com/library/` when you open your library in a browser. If connecting says
  Libby doesn't know the library, check that word there.
- Your **library card number** and **PIN**, the same ones you use on the library's website.

Then tap **Connect Libby**. The server signs in once and keeps only Libby's session in
`./config/libby.json`. The PIN is used for that sign-in only; it is never saved or
logged. Loans and holds belong to your card, so the server sees the same ones as the Libby
app on your phone. Your reading position and tags stay on the phone. **Disconnect**
removes the session.

(Libby's "Copy To Another Device" setup code isn't used: the current Libby app expects
the *new* device to show a code, which a server can't do.)

On some networks OverDrive's server shows a certificate for its edge network
(`*.odrsre.overdrive.com`) instead of its own name. The service recognises that case and
still checks the certificate fully; you'll see one warning about it in the log. Any other
certificate problem is refused.

Libby has no official API. This uses the same one the Libby app uses, so it could stop
working if OverDrive changes it, and Libby limits how often it can be asked (checks are
at most every 15 minutes). If Libby says it's getting too many requests, the service waits
30 minutes, then an hour, then at most 2 hours between tries, and the main page shows when
the next check is due. If the Libby check ever stops or hangs, it's restarted
automatically within 5 minutes; **Check now** also restarts it.

## Optional: watch a Google Drive folder

The service checks one Drive folder every minute and sends any **new** file in it:
`.acsm` loans go through the DRM step, and EPUB, PDF, DOCX, TXT and similar files are
sent as they are. Files already in the folder when you turn this on are skipped. Files
are never changed or deleted, and each file is sent only once. Delete old files from the
folder whenever you like.

Drive access uses a Google Cloud *service account*: a robot account that can only
see the one folder you share with it. It's free.

1. **Create the service account** in the [Google Cloud console](https://console.cloud.google.com):
   1. Create a project, or pick an existing one.
   2. **APIs & Services → Library**: search for **Google Drive API** and click **Enable**.
   3. **IAM & Admin → Service accounts → Create service account**. Any name works,
      e.g. `libtokindle`. Skip the optional roles and access steps.
   4. Open the new account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
2. On the **Settings** page, under **Google Drive folder**, tap **Upload key (.json)**
   and choose that file. The page then shows the service account's email address,
   which looks like `libtokindle@<project>.iam.gserviceaccount.com`.
3. **Share the folder**: in Google Drive, create a folder (e.g. `Kindle`), click
   **Share**, and add that address as a **Viewer**.
4. Paste the folder's link (or just its ID) into **Folder link or ID** and tap **Save**.
   The main page then shows "Also watching your Google Drive folder" with the last
   check time, or the error if something is wrong.

On the iPhone, the Google Drive app adds Drive to **Files**, so Libby downloads can
be saved straight into the folder.

The key file gives read access to everything shared with the service account, so keep
it private and only share the one folder with it.

## Alternative: Google Cloud free tier

The same container runs on an `e2-micro` VM (free tier in `us-west1`, `us-central1`
or `us-east1`):
1. Create a Debian VM, install Docker, and follow steps 2–3 above.
2. Either install Tailscale on the VM (easiest, nothing is exposed), or put
   [Caddy](https://caddyserver.com) in front for HTTPS on a domain, opening only
   port 443. Never serve it over plain HTTP on the internet, because the access
   token would be sent unencrypted.

Home is the simpler choice. Your Adobe keys and Gmail password stay on your own
machine, and there's no public server to maintain.

## Development
```sh
pip install -r requirements-dev.txt
python -m pytest
UPLOAD_TOKEN=dev uvicorn app.main:app --reload     # http://localhost:8000
```
The tests use fake `acsmdownloader`/`adept_remove` scripts, so libgourou isn't needed to run them.
