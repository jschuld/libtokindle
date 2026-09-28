import os
import tempfile

# Keep the app's files (settings, history, logs, Adobe keys) out of the real /config.
# This runs before any test module imports app.main.
_config = tempfile.mkdtemp(prefix="libtokindle-test-")
for name, default in {
    "SETTINGS_FILE": "settings.json",
    "HISTORY_FILE": "history.json",
    "LOG_DIR": "logs",
    "ADEPT_DIR": "adept",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "google-service-account.json",
    "DRIVE_STATE_FILE": "drive-state.json",
}.items():
    os.environ.setdefault(name, os.path.join(_config, default))
