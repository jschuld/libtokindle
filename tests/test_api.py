import pytest
from fastapi.testclient import TestClient

from app import main
from tests.test_pipeline import ACSM


@pytest.fixture
def client(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(main, "cfg", replace(main.cfg, upload_token="secret"))
    ran = []
    monkeypatch.setattr(main.pipeline, "process", lambda cfg, store, job, data: ran.append(job.id))
    c = TestClient(main.app)
    c.ran = ran
    return c


AUTH = {"Authorization": "Bearer secret"}


def test_index_is_public(client):
    assert client.get("/").status_code == 200


def test_api_requires_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/status", headers=AUTH).status_code == 200


def test_upload_starts_job(client):
    res = client.post("/api/upload", headers=AUTH, files={"file": ("loan.acsm", ACSM)})
    assert res.status_code == 200
    job = res.json()
    assert client.ran == [job["id"]]
    assert client.get(f"/api/jobs/{job['id']}", headers=AUTH).json()["filename"] == "loan.acsm"


def test_upload_rejects_non_acsm(client):
    res = client.post("/api/upload", headers=AUTH, files={"file": ("book.epub", b"PK\x03\x04")})
    assert res.status_code == 400
    assert "acsm" in res.json()["detail"]
    assert client.ran == []
