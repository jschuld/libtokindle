import json
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from app import main, settings
from app.config import Config
from tests.test_pipeline import fake_tool


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ["UPLOAD_TOKEN", "KINDLE_EMAIL", "SMTP_USER", "SMTP_PASSWORD", "DRIVE_FOLDER_ID", "NOTIFY_EMAIL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SETTINGS_FILE", str(tmp_path / "config" / "settings.json"))
    monkeypatch.setenv("ADEPT_DIR", str(tmp_path / "config" / "adept"))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "config" / "google-service-account.json"))
    monkeypatch.setenv("DRIVE_STATE_FILE", str(tmp_path / "config" / "drive-state.json"))
    # Never talk to Google from tests.
    monkeypatch.setattr(main.DriveWatcher, "start", lambda self: threading.Event())
    main.apply_config(Config.load())
    yield TestClient(main.app)
    main.apply_config(Config.from_env({}))


def auth(token="a-long-token-123"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def set_up(client):
    assert client.post("/api/setup", json={"token": "a-long-token-123"}).status_code == 200
    return client


def test_first_run_setup(client):
    assert client.get("/api/setup").json() == {"needs_setup": True}
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/setup", json={"token": "short"}).status_code == 400

    assert client.post("/api/setup", json={"token": "a-long-token-123"}).status_code == 200
    assert client.get("/api/setup").json() == {"needs_setup": False}
    assert client.get("/api/settings", headers=auth()).status_code == 200
    # Can't be used again to take over once a token exists.
    assert client.post("/api/setup", json={"token": "someone-elses-token"}).status_code == 409


def test_save_settings(set_up, tmp_path):
    res = set_up.put("/api/settings", headers=auth(), json={
        "KINDLE_EMAIL": "me@kindle.com",
        "SMTP_USER": "me@gmail.com",
        "SMTP_PASSWORD": "abcd efgh ijkl mnop",
        "DRIVE_FOLDER_ID": "https://drive.google.com/drive/folders/1AbC_d-9?usp=sharing",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["values"]["KINDLE_EMAIL"] == "me@kindle.com"
    assert body["values"]["DRIVE_FOLDER_ID"] == "1AbC_d-9"
    assert "SMTP_PASSWORD" not in body["values"]
    assert body["secrets_set"] == {"SMTP_PASSWORD": True, "UPLOAD_TOKEN": True}
    assert main.cfg.smtp_password == "abcdefghijklmnop"
    assert main.cfg.notify_email == "me@gmail.com"

    path = tmp_path / "config" / "settings.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # An empty secret keeps the saved one.
    set_up.put("/api/settings", headers=auth(), json={"SMTP_PASSWORD": "", "KINDLE_EMAIL": "other@kindle.com"})
    assert main.cfg.smtp_password == "abcdefghijklmnop"
    assert main.cfg.kindle_email == "other@kindle.com"


def test_round_trip_keeps_defaults(set_up):
    """Saving the page exactly as loaded must not change anything, e.g. turn result emails off."""
    values = set_up.get("/api/settings", headers=auth()).json()["values"]
    assert values["NOTIFY_EMAIL"] == ""
    set_up.put("/api/settings", headers=auth(), json={**values, "SMTP_USER": "me@gmail.com"})
    assert main.cfg.notify_email == "me@gmail.com"
    assert main.cfg.smtp_from == "me@gmail.com"


def test_saved_settings_override_env(set_up, monkeypatch):
    monkeypatch.setenv("KINDLE_EMAIL", "from-env@kindle.com")
    set_up.put("/api/settings", headers=auth(), json={"KINDLE_EMAIL": "saved@kindle.com"})
    assert Config.load().kindle_email == "saved@kindle.com"


def test_invalid_settings_rejected(set_up):
    res = set_up.put("/api/settings", headers=auth(), json={"KINDLE_EMAIL": "nope", "DRIVE_POLL_SECONDS": "5"})
    assert res.status_code == 400
    assert "isn't an email address" in res.json()["detail"]
    assert "at least 15" in res.json()["detail"]
    assert set_up.put("/api/settings", headers=auth(), json={"ADEPT_DIR": "/etc"}).status_code == 400


def test_change_token(set_up):
    set_up.put("/api/settings", headers=auth(), json={"UPLOAD_TOKEN": "a-brand-new-token"})
    assert set_up.get("/api/settings", headers=auth()).status_code == 401
    assert set_up.get("/api/settings", headers=auth("a-brand-new-token")).status_code == 200


def test_google_key_upload(set_up, tmp_path):
    bad = set_up.post("/api/settings/google-key", headers=auth(), files={"file": ("key.json", b'{"type": "user"}')})
    assert bad.status_code == 400

    key = {"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com", "private_key": "-----BEGIN..."}
    res = set_up.post("/api/settings/google-key", headers=auth(), files={"file": ("key.json", json.dumps(key).encode())})
    assert res.status_code == 200
    assert res.json()["google_key"] == {"present": True, "client_email": "bot@proj.iam.gserviceaccount.com"}
    saved = tmp_path / "config" / "google-service-account.json"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_drive_watcher_follows_settings(set_up):
    assert main.watcher is None
    set_up.put("/api/settings", headers=auth(), json={"DRIVE_FOLDER_ID": "folder1"})
    assert main.watcher is not None and main.watcher.cfg.drive_folder_id == "folder1"
    status = set_up.get("/api/status", headers=auth()).json()["drive"]
    assert "Google key" in status["last_error"]
    set_up.put("/api/settings", headers=auth(), json={"DRIVE_FOLDER_ID": ""})
    assert main.watcher is None


def test_activate(set_up, tmp_path, monkeypatch):
    tool = fake_tool(tmp_path / "adept_activate", """
        out = sys.argv[sys.argv.index("--output-dir") + 1]
        assert "--anonymous" in sys.argv
        os.makedirs(out, exist_ok=True)
        open(os.path.join(out, "activation.xml"), "w").write("<activation/>")
    """)
    monkeypatch.setenv("ADEPT_ACTIVATE", tool)
    res = set_up.post("/api/settings/activate", headers=auth(), json={})
    assert res.status_code == 200, res.text
    assert res.json()["adobe_activated"] is True
    assert set_up.post("/api/settings/activate", headers=auth(), json={}).status_code == 400


def test_activate_failure_reported(set_up, tmp_path, monkeypatch):
    tool = fake_tool(tmp_path / "adept_activate", 'print("Error : E_AUTH_FAILED", file=sys.stderr); sys.exit(1)\n')
    monkeypatch.setenv("ADEPT_ACTIVATE", tool)
    res = set_up.post("/api/settings/activate", headers=auth(), json={"adobe_id": "me@example.com", "password": "x"})
    assert res.status_code == 400
    assert "E_AUTH_FAILED" in res.json()["detail"]


def test_test_email(set_up, monkeypatch):
    sent = []
    monkeypatch.setattr(main.mailer, "send_notification", lambda cfg, subject, body: sent.append(cfg.notify_email))
    set_up.put("/api/settings", headers=auth(), json={"SMTP_USER": "me@gmail.com"})
    assert set_up.post("/api/settings/test-email", headers=auth()).json() == {"sent_to": "me@gmail.com"}
    assert sent == ["me@gmail.com"]


def test_pages_served(client):
    assert client.get("/settings").status_code == 200
    assert client.get("/static/common.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_drive_folder_id_parsing():
    assert settings.drive_folder_id("abc123") == "abc123"
    assert settings.drive_folder_id("https://drive.google.com/drive/u/0/folders/XyZ_1-2") == "XyZ_1-2"
    assert settings.drive_folder_id("https://drive.google.com/open?id=Q9") == "Q9"


def test_retention_setting(set_up):
    res = set_up.put("/api/settings", headers=auth(), json={"LOG_RETENTION_DAYS": "31"})
    assert res.status_code == 400
    assert "1 to 30 days" in res.json()["detail"]
    res = set_up.put("/api/settings", headers=auth(), json={"LOG_RETENTION_DAYS": "7"})
    assert res.json()["values"]["LOG_RETENTION_DAYS"] == "7"
    assert main.cfg.retention_days == 7
    assert main.store.retention_days == 7
