"""Persistence tests: verify disk-backed durable persistence for channels & Twitch settings."""
import json
import os
import time
import subprocess
from pathlib import Path

import pytest
import requests
from pymongo import MongoClient
from dotenv import load_dotenv

BACKEND_DIR = Path("/app/backend")
load_dotenv(BACKEND_DIR / ".env")

BASE_URL = os.environ.get("APP_PUBLIC_URL", "https://twitch-highlight-bot.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

APPDATA = BACKEND_DIR / ".appdata"
CHANNELS_FILE = APPDATA / "channels.json"
SETTINGS_FILE = APPDATA / "settings.json"


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _restart_backend():
    subprocess.run(["sudo", "supervisorctl", "restart", "backend"], check=True, capture_output=True)
    # Wait for service to accept requests
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            r = requests.get(f"{API}/", timeout=3)
            if r.status_code == 200:
                time.sleep(1)  # let startup restore complete
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("backend did not come back")


def test_1_channel_create_writes_to_disk(mongo):
    # cleanup pre-existing
    mongo.channels.delete_many({"login": "persist_qa_1"})
    r = requests.post(f"{API}/channels", json={"url": "twitch.tv/persist_qa_1"}, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["login"] == "persist_qa_1"
    assert data["twitch_user_id"] is None  # twitch not configured
    assert CHANNELS_FILE.exists(), "channels.json snapshot missing"
    snap = json.loads(CHANNELS_FILE.read_text())
    logins = [c["login"] for c in snap]
    assert "persist_qa_1" in logins


def test_2_channels_restored_after_db_reset(mongo):
    assert CHANNELS_FILE.exists()
    # Drop channels collection
    mongo.channels.drop()
    assert mongo.channels.count_documents({}) == 0
    _restart_backend()
    r = requests.get(f"{API}/channels", timeout=15)
    assert r.status_code == 200
    logins = [c["login"] for c in r.json()]
    assert "persist_qa_1" in logins, f"channel not restored, got: {logins}"


def test_3_settings_restored_after_db_reset(mongo):
    # Write fake settings snapshot to disk
    SETTINGS_FILE.write_text(json.dumps([
        {"_id": "twitch", "client_id": "QA_CID", "client_secret": "QA_SECRET"}
    ]))
    mongo.settings.drop()
    assert mongo.settings.count_documents({}) == 0
    _restart_backend()
    r = requests.get(f"{API}/settings", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body.get("twitch_configured") is True, body
    assert "redirect_uri" in body


def test_4_regression_demo_seed(mongo):
    r = requests.post(f"{API}/demo/seed", timeout=30)
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert len(data["channels"]) == 3

    r = requests.get(f"{API}/channels", timeout=15)
    assert r.status_code == 200
    chans = r.json()
    demo_logins = {c["login"] for c in chans if c.get("is_demo")}
    assert demo_logins == {"xqc", "kaicenat", "pokimane"}

    r = requests.get(f"{API}/clips", timeout=15)
    assert r.status_code == 200
    clips = r.json()
    demo_clips = [c for c in clips if c.get("is_demo")]
    assert len(demo_clips) == 18  # 3 x 6

    r = requests.get(f"{API}/settings", timeout=15)
    assert r.status_code == 200
    s = r.json()
    assert "redirect_uri" in s
    assert "twitch_configured" in s
    assert "oauth_connected" in s
    assert s["redirect_uri"].endswith("/api/auth/twitch/callback")


def test_5_cleanup(mongo):
    # Delete persist_qa_1 via API
    r = requests.get(f"{API}/channels", timeout=15)
    assert r.status_code == 200
    for c in r.json():
        if c["login"] == "persist_qa_1":
            d = requests.delete(f"{API}/channels/{c['id']}", timeout=15)
            assert d.status_code == 200

    # Drop settings collection & remove disk snapshots
    mongo.settings.drop()
    if SETTINGS_FILE.exists():
        SETTINGS_FILE.unlink()
    if CHANNELS_FILE.exists():
        CHANNELS_FILE.unlink()

    # Re-seed demo
    r = requests.post(f"{API}/demo/seed", timeout=30)
    assert r.status_code == 200

    # Verify final clean state
    r = requests.get(f"{API}/settings", timeout=15)
    assert r.status_code == 200
    assert r.json()["twitch_configured"] is False

    r = requests.get(f"{API}/channels", timeout=15)
    assert r.status_code == 200
    logins = {c["login"] for c in r.json()}
    assert "persist_qa_1" not in logins
    assert logins == {"xqc", "kaicenat", "pokimane"}
