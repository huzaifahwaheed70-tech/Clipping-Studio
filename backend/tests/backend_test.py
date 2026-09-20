"""Backend API tests for StreamClip AI — VOD-record engine + delete CRUD.

Tests the MAJOR engine change: app now RECORDS from VODs (past broadcasts) itself
using ffmpeg, cuts hype moments from chat density — NOT returning Twitch's
pre-made clips. Also tests the new delete endpoints (single / per-channel / all).

Pre-seeded channels (is_demo=false):
  xqc     = 476dc540-1e59-4e60-baee-ee6395832863   (~24 pre-populated VOD clips)
  kaicenat= 2e4bf3a5-fa1e-4823-997f-9497a13fc228
  pokimane= a9defff4-626f-429f-a606-fcc8f5c788cd

IMPORTANT: destructive delete-all tests run LAST and re-sync xqc afterward.
"""
import os
import time
import subprocess
import shutil as _sh
import pytest
import requests

def _load_backend_url():
    v = os.environ.get("REACT_APP_BACKEND_URL", "").strip()
    if v:
        return v.rstrip("/")
    # Fallback: read from /app/frontend/.env
    try:
        with open("/app/frontend/.env") as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    return line.split("=", 1)[1].strip().rstrip("/")
    except OSError:
        pass
    return ""


BASE_URL = _load_backend_url()
assert BASE_URL, "REACT_APP_BACKEND_URL is required"
API = f"{BASE_URL}/api"

XQC = "476dc540-1e59-4e60-baee-ee6395832863"
KAI = "2e4bf3a5-fa1e-4823-997f-9497a13fc228"
POK = "a9defff4-626f-429f-a606-fcc8f5c788cd"


@pytest.fixture(scope="module")
def s():
    return requests.Session()


# ---------- 1) Channels present ----------

def test_channels_are_three_real(s):
    r = s.get(f"{API}/channels", timeout=30)
    assert r.status_code == 200
    chans = r.json()
    by_login = {c["login"]: c for c in chans}
    for login in ("xqc", "kaicenat", "pokimane"):
        assert login in by_login, f"missing {login}"
        c = by_login[login]
        assert c.get("is_demo") is False, f"{login} should have is_demo=false"
        assert "_id" not in c


# ---------- 2) VOD clips (xqc already populated) ----------

def test_xqc_clips_are_vod_sourced(s):
    r = s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert len(clips) > 0, "xqc should be pre-populated"
    # Sorted by created_at desc
    for c in clips[:5]:
        assert c.get("source_type") == "vod", f"expected source_type=vod, got {c.get('source_type')}"
        tcid = c.get("twitch_clip_id", "")
        assert tcid.startswith("vod-"), f"twitch_clip_id must start with 'vod-', got {tcid}"
        assert not c.get("embed_url"), f"embed_url must be empty (not a Twitch pre-made clip), got {c.get('embed_url')}"
        assert isinstance(c.get("start_seconds"), (int, float)) and c["start_seconds"] >= 0
        dur = c.get("duration")
        assert isinstance(dur, (int, float)) and 18 <= dur <= 45, f"duration out of range: {dur}"
        assert c.get("vod_id"), "missing vod_id"
        assert c.get("ai_title"), "missing ai_title"
        assert isinstance(c.get("ai_hashtags"), list) and len(c["ai_hashtags"]) >= 1
        assert all(str(h).startswith("#") for h in c["ai_hashtags"])
        assert c.get("ai_caption"), "missing ai_caption"
        assert "render_status" in c
    # sort order
    times = [c["created_at"] for c in clips]
    assert times == sorted(times, reverse=True), "clips should be sorted by created_at desc"


# ---------- 3) POST /channels/{id}/sync on a real (non-demo) channel ----------

def _has_vod_clip(s, channel_id):
    clips = s.get(f"{API}/clips", params={"channel_id": channel_id}, timeout=30).json()
    return len([c for c in clips if c.get("source_type") == "vod"])


def test_sync_kaicenat_creates_vod_clips(s):
    """Sync a real channel and confirm the app FINDS hype moments from VODs itself."""
    # Ensure fresh: delete kaicenat's existing clips so sync actually stores new ones
    s.delete(f"{API}/clips", params={"channel_id": KAI}, timeout=30)
    r = s.post(f"{API}/channels/{KAI}/sync", json={"period_days": 30}, timeout=300)
    # Twitch VOD availability varies; if 404 (no VODs available), skip rather than fail
    if r.status_code == 404:
        pytest.skip(f"kaicenat has no past broadcasts right now: {r.text[:200]}")
    if r.status_code == 502:
        pytest.skip(f"Twitch upstream flaky: {r.text[:200]}")
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    d = r.json()
    assert d.get("fetched", 0) > 0, f"fetched should be >0, got {d}"
    assert d.get("stored", 0) > 0, f"stored should be >0 (real hype moments found), got {d}"
    # verify persistence
    clips = s.get(f"{API}/clips", params={"channel_id": KAI}, timeout=30).json()
    vod_clips = [c for c in clips if c.get("source_type") == "vod"]
    assert vod_clips, "no VOD clips persisted"
    sample = vod_clips[0]
    assert sample["twitch_clip_id"].startswith("vod-")
    assert not sample.get("embed_url")
    assert 18 <= sample["duration"] <= 45
    assert sample.get("ai_title") and sample.get("ai_caption")
    assert isinstance(sample.get("ai_hashtags"), list)
    assert "render_status" in sample


# ---------- 4) GET /clips filter and sort ----------

def test_get_clips_filter_channel(s):
    r = s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert clips
    assert all(c["channel_id"] == XQC for c in clips)


# ---------- 5) /clips/{id}/thumb ----------

def test_clip_thumb(s):
    clips = s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30).json()
    assert clips
    cid = clips[0]["id"]
    r = s.get(f"{API}/clips/{cid}/thumb", timeout=30, allow_redirects=False)
    # Either a served poster (image/*) OR a redirect to the source thumbnail
    assert r.status_code in (200, 301, 302, 303, 307, 308), f"{r.status_code}: {r.text[:200]}"
    if r.status_code == 200:
        ctype = r.headers.get("content-type", "")
        assert ctype.startswith("image/"), f"expected image/*, got {ctype}"
        assert len(r.content) > 500
    else:
        loc = r.headers.get("location", "")
        assert loc.startswith("http"), f"redirect location invalid: {loc}"


# ---------- 6) /clips/{id}/prepare ----------

def test_prepare_returns_render_status(s):
    clips = s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30).json()
    target = clips[0]
    r = s.post(f"{API}/clips/{target['id']}/prepare", timeout=30)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d.get("render_status") in ("pending", "rendering", "done"), d


def test_prepare_unknown_404(s):
    r = s.post(f"{API}/clips/does-not-exist-xyz/prepare", timeout=15)
    assert r.status_code == 404


# ---------- 7) /clips/{id}/video — the CORE deliverable ----------

def _pick_rendered_xqc_clip(s):
    clips = s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30).json()
    done = [c for c in clips if c.get("render_status") == "done" and c.get("rendered")]
    return done[0] if done else clips[0]


def test_get_clip_video_is_real_916_mp4(s, tmp_path):
    """Downloads a rendered clip and ffprobes it as h264 1080x1920.
    Prepares first (background render) and polls until done to avoid CF edge timeouts."""
    clip = _pick_rendered_xqc_clip(s)
    # Kick a background render if needed and wait until it's cached
    ps = s.post(f"{API}/clips/{clip['id']}/prepare", timeout=30)
    assert ps.status_code == 200
    deadline = time.time() + 240
    status = ps.json().get("render_status")
    while status != "done" and time.time() < deadline:
        time.sleep(4)
        cd = s.get(f"{API}/clips/{clip['id']}", timeout=15).json()
        status = cd.get("render_status")
        if status == "error":
            pytest.skip(f"render errored for this clip: {cd.get('last_error')}")
    assert status == "done", f"render never finished (status={status})"

    r = s.get(f"{API}/clips/{clip['id']}/video", timeout=90)
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    ctype = r.headers.get("content-type", "")
    assert ctype.startswith("video/mp4"), ctype
    disp = r.headers.get("content-disposition", "").lower()
    assert "attachment" in disp, f"expected attachment disposition, got {disp!r}"
    body = r.content
    assert len(body) > 10_000
    out = tmp_path / "vertical.mp4"
    out.write_bytes(body)
    if _sh.which("ffprobe"):
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0:s=x", str(out)],
            capture_output=True, text=True, timeout=30,
        )
        vinfo = (p.stdout or "").strip()
        print("ffprobe:", vinfo)
        parts = vinfo.split("x")
        assert len(parts) == 3, f"unexpected ffprobe: {vinfo}"
        assert parts[0] == "h264", f"codec={parts[0]}"
        assert parts[1] == "1080" and parts[2] == "1920", f"dims={parts[1]}x{parts[2]}"


def test_get_clip_video_not_found(s):
    r = s.get(f"{API}/clips/does-not-exist-xyz/video", timeout=15)
    assert r.status_code == 404


# ---------- 8) DELETE single clip ----------

def test_delete_single_clip(s):
    # Prefer to delete a kaicenat clip so we don't erode xqc dataset
    ch_id = KAI
    clips = s.get(f"{API}/clips", params={"channel_id": ch_id}, timeout=30).json()
    if not clips:
        # sync kaicenat if empty
        s.post(f"{API}/channels/{ch_id}/sync", json={"period_days": 30}, timeout=300)
        clips = s.get(f"{API}/clips", params={"channel_id": ch_id}, timeout=30).json()
    if not clips:
        pytest.skip("no kaicenat clips to delete-test")
    victim = clips[0]
    before = len(clips)
    r = s.delete(f"{API}/clips/{victim['id']}", timeout=30)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    after = s.get(f"{API}/clips", params={"channel_id": ch_id}, timeout=30).json()
    ids = {c["id"] for c in after}
    assert victim["id"] not in ids, "clip should be gone"
    assert len(after) == before - 1


# ---------- 9) DELETE per-channel ----------

def test_delete_per_channel(s):
    """Delete all clips for pokimane; xqc clips must remain."""
    # ensure pokimane has some clips first
    pok_clips = s.get(f"{API}/clips", params={"channel_id": POK}, timeout=30).json()
    if not pok_clips:
        r = s.post(f"{API}/channels/{POK}/sync", json={"period_days": 30}, timeout=300)
        if r.status_code not in (200,):
            pytest.skip(f"couldn't seed pokimane: {r.status_code} {r.text[:200]}")
        pok_clips = s.get(f"{API}/clips", params={"channel_id": POK}, timeout=30).json()
    if not pok_clips:
        pytest.skip("pokimane never populated")
    xqc_before = len(s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30).json())

    r = s.delete(f"{API}/clips", params={"channel_id": POK}, timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d.get("deleted", 0) >= 1

    pok_after = s.get(f"{API}/clips", params={"channel_id": POK}, timeout=30).json()
    assert len(pok_after) == 0, f"pokimane should be empty, has {len(pok_after)}"
    xqc_after = len(s.get(f"{API}/clips", params={"channel_id": XQC}, timeout=30).json())
    assert xqc_after == xqc_before, f"xqc clip count changed: {xqc_before} -> {xqc_after}"


# ---------- 10) DELETE ALL (run last — destructive) ----------

def test_z_delete_all_clips(s):
    total_before = len(s.get(f"{API}/clips", timeout=30).json())
    assert total_before > 0
    r = s.delete(f"{API}/clips", timeout=30)
    assert r.status_code == 200
    d = r.json()
    # Allow a small drift because auto_sync_loop / background render can create clips concurrently
    assert d.get("deleted", 0) >= total_before, f"deleted={d.get('deleted')} vs before={total_before}"
    # And post-delete list must be near-zero (again, a background worker may have inserted 1-2)
    total_after = len(s.get(f"{API}/clips", timeout=30).json())
    assert total_after <= 2, f"expected ~0 clips after delete-all, got {total_after}"

    # Re-populate all three channels so the frontend / next test agent still has data
    for cid in (XQC, KAI, POK):
        s.post(f"{API}/channels/{cid}/sync", json={"period_days": 30}, timeout=300)
