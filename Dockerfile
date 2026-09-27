# Built on the prebuilt libgourou image (amd64), which already has acsmdownloader,
# adept_activate and adept_remove in /usr/local/bin. Pinned to a libgourou release;
# bump the tag deliberately. Source: https://hub.docker.com/r/bcliang/docker-libgourou
FROM bcliang/docker-libgourou:0.8.9-ubuntu

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fail the build now, not on the first book, if the tools aren't where we expect.
RUN command -v acsmdownloader && command -v adept_remove && command -v adept_activate

WORKDIR /srv
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/

ENV PATH=/opt/venv/bin:$PATH \
    ADEPT_DIR=/config/adept \
    WORK_DIR=/tmp/libtokindle \
    PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=5s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# Replace the base image's own entrypoint (its one-shot dedrm script).
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
