# --- Stage 1: build libgourou's command-line tools (acsmdownloader, adept_activate, adept_remove)
FROM debian:bookworm-slim AS libgourou

# A tag or commit from https://forge.soutade.fr/soutade/libgourou. Pin it once a build works for you.
ARG LIBGOUROU_REF=master

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
        libpugixml-dev libzip-dev libcurl4-openssl-dev libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://forge.soutade.fr/soutade/libgourou.git /src \
    && git -C /src checkout "$LIBGOUROU_REF"
WORKDIR /src
# The first `make` also fetches updfparser via scripts/setup.sh.
RUN make BUILD_UTILS=1 BUILD_STATIC=1 BUILD_SHARED=0 \
    && mkdir /out \
    && cp utils/acsmdownloader utils/adept_activate utils/adept_remove /out/

# --- Stage 2: the web app
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpugixml1v5 libzip4 libcurl4 libssl3 zlib1g ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=libgourou /out/ /usr/local/bin/
# Fail the build now, not on the first book, if a shared library is missing.
RUN ! ldd /usr/local/bin/acsmdownloader /usr/local/bin/adept_remove | grep "not found"

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/

ENV ADEPT_DIR=/config/adept \
    WORK_DIR=/tmp/libtokindle \
    PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
