"""Backend API tests for StreamClip AI (Twitch highlight bot)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://twitch-highlight-bot.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    return requests.Session()


@pytest.fixture(scope="module")
def seeded(s):
    r = s.post(f"{API}/demo/seed", timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True
    assert isinstance(data.get("channels"), list) and len(data["channels"]) == 3
    return data


# ---------- Seed & basic reads ----------

def test_seed_demo(seeded):
    logins = {c["login"] for c in seeded["channels"]}
    assert {"xqc", "kaicenat", "pokimane"}.issubset(logins)


def test_list_channels(s, seeded):
    r = s.get(f"{API}/channels", timeout=30)
    assert r.status_code == 200
    chans = r.json()
    assert len([c for c in chans if c.get("is_demo")]) >= 3
    # ensure no _id leaks
    for c in chans:
        assert "_id" not in c


def test_list_clips_all(s, seeded):
    r = s.get(f"{API}/clips", timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert len(clips) >= 18  # 3 channels * 6
    for c in clips:
        assert "_id" not in c


def test_list_clips_filtered(s, seeded):
    ch = seeded["channels"][0]
    r = s.get(f"{API}/clips", params={"channel_id": ch["id"]}, timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert len(clips) == 6
    assert all(c["channel_id"] == ch["id"] for c in clips)


# ---------- Settings ----------

def test_settings(s):
    r = s.get(f"{API}/settings", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["twitch_configured"] is False
    assert isinstance(d.get("redirect_uri"), str) and d["redirect_uri"].startswith("http")


# ---------- AI regeneration (real GPT 5.4) ----------

def test_regenerate_clip_ai(s, seeded):
    ch = seeded["channels"][0]
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    clip = clips[0]
    original_title = clip["ai_title"]
    r = s.post(f"{API}/clips/{clip['id']}/generate", timeout=90)
    assert r.status_code == 200, r.text
    d = r.json()
    assert isinstance(d.get("ai_title"), str) and len(d["ai_title"]) > 0
    assert isinstance(d.get("ai_hashtags"), list) and len(d["ai_hashtags"]) >= 1
    assert all(str(h).startswith("#") for h in d["ai_hashtags"])
    assert isinstance(d.get("ai_caption"), str) and len(d["ai_caption"]) > 0
    # Persistence check
    got = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    updated = next(c for c in got if c["id"] == clip["id"])
    assert updated["ai_title"] == d["ai_title"]
    # Not asserting title changed since AI may return same content, but should be non-empty
    print(f"Original: {original_title} -> New: {d['ai_title']}")


# ---------- PATCH clip ----------

def test_patch_clip(s, seeded):
    ch = seeded["channels"][1]
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    clip = clips[0]
    payload = {"caption_overlay": {"position": "top", "highlight": "#00FF00", "font_size": 40},
               "ai_caption": "TEST OVERLAY CAPTION"}
    r = s.patch(f"{API}/clips/{clip['id']}", json=payload, timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["ai_caption"] == "TEST OVERLAY CAPTION"
    assert d["caption_overlay"]["position"] == "top"
    assert d["caption_overlay"]["font_size"] == 40


# ---------- PATCH channel ----------

def test_patch_channel_clips_per_day(s, seeded):
    ch = seeded["channels"][2]
    r = s.patch(f"{API}/channels/{ch['id']}", json={"clips_per_day": 12}, timeout=15)
    assert r.status_code == 200
    assert r.json()["clips_per_day"] == 12


# ---------- Live status for demo ----------

def test_channel_live_demo(s, seeded):
    ch = seeded["channels"][0]
    r = s.get(f"{API}/channels/{ch['id']}/live", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["is_live"] is True
    assert d["viewer_count"] > 0


def test_sync_demo_returns_400(s, seeded):
    ch = seeded["channels"][0]
    r = s.post(f"{API}/channels/{ch['id']}/sync", json={"period_days": 1}, timeout=15)
    assert r.status_code == 400


# ---------- Add channel while Twitch not configured ----------

def test_add_channel_invalid(s):
    r = s.post(f"{API}/channels", json={"url": "a"}, timeout=15)
    assert r.status_code == 400


def test_add_channel_no_twitch_configured(s):
    # Valid username format but Twitch not connected
    r = s.post(f"{API}/channels", json={"url": "https://twitch.tv/some_random_unique_user_12345"}, timeout=15)
    # Without Twitch creds, code path creates record with user_id=None
    assert r.status_code in (200, 201), f"expected create, got {r.status_code}: {r.text}"
    doc = r.json()
    assert doc["login"] == "some_random_unique_user_12345"
    # cleanup
    s.delete(f"{API}/channels/{doc['id']}")


# ---------- Download job flow / 9:16 render (async) ----------

def test_download_demo_clip_job_returns_400(s, seeded):
    ch = seeded["channels"][0]
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    clip = clips[0]
    r = s.post(f"{API}/clips/{clip['id']}/download-jobs", timeout=30)
    assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text[:200]}"
    body = r.json()
    detail = (body.get("detail") or "").lower()
    assert "sample" in detail or "your own channel" in detail or "get clips" in detail, detail


def test_download_job_unknown_returns_404(s):
    r = s.get(f"{API}/download-jobs/does-not-exist-xyz", timeout=15)
    assert r.status_code == 404


def test_download_e2e_real_job_flow(s, tmp_path):
    import time, subprocess, shutil as _sh
    # 1) Kick off job
    r = s.post(f"{API}/clips/e2e-real/download-jobs", timeout=30)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
    job_id = r.json().get("job_id")
    assert isinstance(job_id, str) and len(job_id) > 0

    # 2) Poll status (each request short — no 502 possible)
    status = "processing"
    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        pr = s.get(f"{API}/download-jobs/{job_id}", timeout=15)
        assert pr.status_code == 200, f"poll status {pr.status_code}: {pr.text[:200]}"
        last = pr.json()
        status = last.get("status")
        if status in ("done", "error"):
            break
        time.sleep(2)
    assert status == "done", f"job did not complete cleanly: {last}"

    # 3) Fetch file
    fr = s.get(f"{API}/download-jobs/{job_id}/file", timeout=60)
    assert fr.status_code == 200, f"file endpoint {fr.status_code}: {fr.text[:200]}"
    ctype = fr.headers.get("content-type", "")
    assert ctype.startswith("video/mp4"), ctype
    assert len(fr.content) > 10_000, f"video too small: {len(fr.content)} bytes"
    # Cloudflare fix: Content-Length header MUST equal actual body length
    clen = fr.headers.get("content-length")
    assert clen is not None, "missing content-length header"
    assert int(clen) == len(fr.content), f"content-length {clen} != body {len(fr.content)}"
    out = tmp_path / "vertical.mp4"
    out.write_bytes(fr.content)
    if _sh.which("ffprobe"):
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(out)],
            capture_output=True, text=True, timeout=30,
        )
        dims = (p.stdout or "").strip()
        print("ffprobe dims:", dims)
        assert dims == "1080x1920", f"expected 1080x1920 got {dims}"


# ---------- Delete flows (run last) ----------

def test_zz_delete_clip_and_channel(s):
    # Refresh channels
    chans = s.get(f"{API}/channels").json()
    demo_chans = [c for c in chans if c.get("is_demo")]
    assert demo_chans
    ch = demo_chans[-1]
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    if clips:
        cid = clips[0]["id"]
        r = s.delete(f"{API}/clips/{cid}", timeout=15)
        assert r.status_code == 200
        remaining = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
        assert all(c["id"] != cid for c in remaining)
    r = s.delete(f"{API}/channels/{ch['id']}", timeout=15)
    assert r.status_code == 200
    # verify cascade
    remaining_clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()
    assert remaining_clips == []
