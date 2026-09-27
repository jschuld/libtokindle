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

Each `.acsm` file can be used only once, and it expires after a while. If the upload
fails with a download error, download the `.acsm` again from Libby.

Books in BorrowBox can't be sent, because BorrowBox doesn't offer an `.acsm` file.

## Setup (Docker at home)

You need an always-on computer at home that runs Docker on a normal Intel/AMD (amd64) processor.
The image is built on [`bcliang/docker-libgourou`](https://hub.docker.com/r/bcliang/docker-libgourou),
which provides the libgourou tools and is published for amd64 only (not Raspberry Pi).

### 1. Amazon
- Find your Send to Kindle address: amazon.com → *Manage Your Content and Devices* →
  *Preferences* → *Personal Document Settings*.
- On the same page, add the email address you'll send from to the
  **Approved Personal Document E-mail List**.

### 2. Gmail app password
Create an app password at <https://myaccount.google.com/apppasswords>. This needs
2-step verification turned on. Use it as `SMTP_PASSWORD`.

### 3. Configure and build
```sh
git clone https://github.com/jschuld/libtokindle && cd libtokindle
cp .env.example .env        # then fill it in
docker compose build
```

### 4. Activate an Adobe device (once)
```sh
docker compose run --rm libtokindle activate
```
This writes Adobe keys to `./config/adept`. Back that folder up. Re-activating uses
up another of your Adobe account's limited device slots, and books already sent
don't depend on it. Put `ADOBE_ID`/`ADOBE_PASSWORD` in `.env` to activate with an
Adobe account instead of anonymously. A free account works.

### 5. Start it
```sh
docker compose up -d
curl localhost:8080/healthz      # {"ok": true, ...} once everything is set up
```

### 6. Reach it from your iPhone
Don't open the service to the internet. Instead:
- **At home only**: open `http://<server-ip>:8080` on your home Wi-Fi.
- **Anywhere (recommended)**: install [Tailscale](https://tailscale.com) (free) on the
  server and your iPhone, then open `http://<server-name>:8080`.

In Safari, **Share → Add to Home Screen** makes it behave like an app. Enter your
`UPLOAD_TOKEN` the first time; Safari remembers it.

## Alternative: Google Cloud free tier

The same container runs on an `e2-micro` VM (free tier in `us-west1`, `us-central1`
or `us-east1`):
1. Create a Debian VM, install Docker, and follow steps 3–5 above.
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
