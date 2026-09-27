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
      exec adept_activate --random-serial --username "$ADOBE_ID" --password "$ADOBE_PASSWORD" --output-dir "$ADEPT_DIR"
    else
      exec adept_activate --random-serial --anonymous --output-dir "$ADEPT_DIR"
    fi
    ;;
  *)
    exec "$@"
    ;;
esac
