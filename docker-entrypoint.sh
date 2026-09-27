#!/bin/sh
set -e

case "$1" in
  serve)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
    ;;
  activate)
    # One-time Adobe device activation. The keys it writes to $ADEPT_DIR are what
    # let this service open your loans, so keep /config backed up.
    if [ -f "$ADEPT_DIR/activation.xml" ]; then
      echo "Already activated ($ADEPT_DIR/activation.xml exists). Delete it first to re-activate." >&2
      exit 1
    fi
    if [ -n "$ADOBE_ID" ]; then
      exec adept_activate -u "$ADOBE_ID" -p "$ADOBE_PASSWORD" -O "$ADEPT_DIR"
    else
      exec adept_activate --anonymous -O "$ADEPT_DIR"
    fi
    ;;
  *)
    exec "$@"
    ;;
esac
