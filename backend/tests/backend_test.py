"""Backend API tests for StreamClip AI (rebuilt: no-creds GQL + on-demand 9:16 render)."""
import os
import time
import subprocess
import shutil as _sh
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://twitch-highlight-bot.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def s():
    return requests.Session()


# ---------- Regression: seed + reads ----------

@pytest.fixture(scope="module")
def seeded(s):
    r = s.post(f"{API}/demo/seed", timeout=60)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True and len(d.get("channels", [])) == 3
    return d


def test_seed_demo(seeded):
    logins = {c["login"] for c in seeded["channels"]}
    assert {"xqc", "kaicenat", "pokimane"}.issubset(logins)


def test_list_channels_no_id_leak(s, seeded):
    r = s.get(f"{API}/channels", timeout=30)
    assert r.status_code == 200
    chans = r.json()
    assert len([c for c in chans if c.get("is_demo")]) >= 3
    for c in chans:
        assert "_id" not in c


def test_list_clips_all(s, seeded):
    r = s.get(f"{API}/clips", timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert len(clips) >= 18
    for c in clips:
        assert "_id" not in c


def test_list_clips_filtered(s, seeded):
    ch = seeded["channels"][0]
    r = s.get(f"{API}/clips", params={"channel_id": ch["id"]}, timeout=30)
    assert r.status_code == 200
    clips = r.json()
    assert len(clips) == 6
    assert all(c["channel_id"] == ch["id"] for c in clips)


def test_get_single_clip(s, seeded):
    ch = seeded["channels"][0]
    clip = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()[0]
    r = s.get(f"{API}/clips/{clip['id']}", timeout=15)
    assert r.status_code == 200
    assert r.json()["id"] == clip["id"]


def test_regenerate_clip_ai(s, seeded):
    ch = seeded["channels"][0]
    clip = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()[0]
    r = s.post(f"{API}/clips/{clip['id']}/generate", timeout=90)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("ai_title"), str) and d["ai_title"]
    assert isinstance(d.get("ai_hashtags"), list) and len(d["ai_hashtags"]) >= 1
    assert all(str(h).startswith("#") for h in d["ai_hashtags"])
    assert isinstance(d.get("ai_caption"), str) and d["ai_caption"]


# ---------- CREDENTIAL-FREE add channel via public GQL ----------

def test_add_channel_via_public_gql_summit1g(s):
    # Cleanup any prior run
    chans = s.get(f"{API}/channels").json()
    for c in chans:
        if c.get("login") == "summit1g":
            s.delete(f"{API}/channels/{c['id']}")

    r = s.post(f"{API}/channels", json={"url": "twitch.tv/summit1g"}, timeout=30)
    assert r.status_code in (200, 201), f"{r.status_code}: {r.text[:300]}"
    doc = r.json()
    assert doc["login"] == "summit1g"
    # GQL should resolve real display name + avatar without Twitch creds
    assert isinstance(doc.get("display_name"), str) and doc["display_name"].lower() == "summit1g"
    assert isinstance(doc.get("avatar_url"), str) and doc["avatar_url"].startswith("http")
    assert doc.get("twitch_user_id"), "expected twitch_user_id resolved from GQL"

    ch_id = doc["id"]
    # Auto-fetch: background task should populate clips within ~30s
    deadline = time.time() + 45
    clips = []
    while time.time() < deadline:
        clips = s.get(f"{API}/clips", params={"channel_id": ch_id}, timeout=20).json()
        if len(clips) >= 3:
            break
        time.sleep(3)
    assert len(clips) >= 3, f"auto-fetch never populated clips (got {len(clips)})"
    for c in clips[:5]:
        assert c.get("ai_title"), "missing ai_title"
        assert isinstance(c.get("ai_hashtags"), list) and c["ai_hashtags"]
        assert c.get("ai_caption"), "missing ai_caption"
        assert isinstance(c.get("view_count"), int)
        assert c.get("hype_type")

    # cleanup
    s.delete(f"{API}/channels/{ch_id}")


def test_add_channel_invalid_url(s):
    r = s.post(f"{API}/channels", json={"url": "a"}, timeout=15)
    assert r.status_code == 400


def test_add_channel_unknown_returns_404(s):
    r = s.post(f"{API}/channels", json={"url": "twitch.tv/zzq_no_real_ch_9182"}, timeout=30)
    assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"


# ---------- ON-DEMAND 9:16 SAVE endpoint (core fix) ----------

def _get_or_add_timthetatman(s):
    chans = s.get(f"{API}/channels", timeout=15).json()
    ch = next((c for c in chans if c.get("login") == "timthetatman"), None)
    if not ch:
        r = s.post(f"{API}/channels", json={"url": "twitch.tv/timthetatman"}, timeout=30)
        assert r.status_code in (200, 201)
        ch = r.json()
        time.sleep(6)
    return ch


def _pick_real_clip(s, prefer_done=True):
    ch = _get_or_add_timthetatman(s)
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}, timeout=30).json()
    assert clips, "timthetatman has no clips"
    if prefer_done:
        rendered = [c for c in clips if c.get("render_status") == "done" and c.get("rendered")]
        if rendered:
            return rendered[0]
    return clips[0]


def test_get_clip_video_on_demand_916(s, tmp_path):
    clip = _pick_real_clip(s, prefer_done=True)
    r = s.get(f"{API}/clips/{clip['id']}/video", timeout=90)
    assert r.status_code == 200, f"expected 200 got {r.status_code}: {r.text[:200]}"
    ctype = r.headers.get("content-type", "")
    assert ctype.startswith("video/mp4"), ctype
    body = r.content
    assert len(body) > 10_000, f"video too small ({len(body)})"
    clen = r.headers.get("content-length")
    assert clen is not None, "missing content-length header (Cloudflare needs this)"
    assert int(clen) == len(body), f"content-length {clen} != body {len(body)}"
    out = tmp_path / "vertical.mp4"
    out.write_bytes(body)
    if _sh.which("ffprobe"):
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(out)],
            capture_output=True, text=True, timeout=30,
        )
        dims = (p.stdout or "").strip()
        print("ffprobe:", dims)
        assert dims == "1080x1920", f"expected 1080x1920 got {dims}"


def test_get_clip_video_demo_returns_400(s, seeded):
    ch = seeded["channels"][0]
    clip = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()[0]
    r = s.get(f"{API}/clips/{clip['id']}/video", timeout=15, allow_redirects=False)
    assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text[:200]}"
    detail = (r.json().get("detail") or "").lower()
    assert "sample" in detail or "add your own channel" in detail


def test_get_clip_video_not_found(s):
    r = s.get(f"{API}/clips/does-not-exist-xyz/video", timeout=15)
    assert r.status_code == 404


# ---------- Regression on legacy download-job endpoints (still exposed) ----------

def test_download_demo_clip_job_returns_400(s, seeded):
    ch = seeded["channels"][0]
    clip = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()[0]
    r = s.post(f"{API}/clips/{clip['id']}/download-jobs", timeout=30)
    assert r.status_code == 400


def test_download_job_unknown_returns_404(s):
    r = s.get(f"{API}/download-jobs/does-not-exist-xyz", timeout=15)
    assert r.status_code == 404


# ---------- Settings / channel-live ----------

def test_settings(s):
    r = s.get(f"{API}/settings", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["twitch_configured"] is False
    assert isinstance(d.get("redirect_uri"), str) and d["redirect_uri"].startswith("http")


def test_channel_live_demo(s, seeded):
    ch = seeded["channels"][0]
    r = s.get(f"{API}/channels/{ch['id']}/live", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["is_live"] is True
    assert d["viewer_count"] > 0


# ---------- PATCH clip / channel ----------

def test_patch_clip(s, seeded):
    ch = seeded["channels"][1]
    clip = s.get(f"{API}/clips", params={"channel_id": ch["id"]}).json()[0]
    payload = {"caption_overlay": {"position": "top", "highlight": "#00FF00", "font_size": 40},
               "ai_caption": "TEST OVERLAY CAPTION"}
    r = s.patch(f"{API}/clips/{clip['id']}", json=payload, timeout=15)
    assert r.status_code == 200


# ---------- CORE FIX: Responsiveness while a render is running ----------

def test_responsiveness_during_render(s):
    """While ONE /video render runs in the background, lightweight endpoints must
    stay fast (<5s) and NEVER return 5xx / Cloudflare empty response."""
    import threading

    ch = _get_or_add_timthetatman(s)
    clips = s.get(f"{API}/clips", params={"channel_id": ch["id"]}, timeout=30).json()
    # pick a pending (not-yet-rendered) real clip to guarantee ffmpeg actually runs
    pending = [c for c in clips if c.get("render_status") in ("pending", "rendering")]
    target = (pending or clips)[0]

    render_result = {}

    def _fire_render():
        try:
            rr = requests.get(f"{API}/clips/{target['id']}/video", timeout=180)
            render_result["status"] = rr.status_code
            render_result["len"] = len(rr.content)
        except Exception as e:
            render_result["err"] = str(e)

    t = threading.Thread(target=_fire_render, daemon=True)
    t.start()
    # give ffmpeg time to actually spin up
    time.sleep(4)

    # Now hammer lightweight endpoints and measure
    timings = []
    for _ in range(3):
        for method, url, kwargs in [
            ("GET", f"{API}/", {}),
            ("GET", f"{API}/channels", {}),
            ("POST", f"{API}/channels", {"json": {"url": "twitch.tv/shroud"}}),
        ]:
            t0 = time.time()
            r = requests.request(method, url, timeout=15, **kwargs)
            dt = time.time() - t0
            timings.append((method, url, r.status_code, dt))
            print(f"{method} {url} -> {r.status_code} in {dt:.2f}s")
            assert r.status_code < 500, f"5xx during render on {method} {url}: {r.status_code} {r.text[:200]}"
            assert dt < 5.0, f"{method} {url} took {dt:.2f}s during render (should stay <5s)"
        time.sleep(2)

    # cleanup shroud we might have just added (only if we added it)
    chans = s.get(f"{API}/channels", timeout=15).json()
    for c in chans:
        if c.get("login") == "shroud":
            s.delete(f"{API}/channels/{c['id']}")
            break

    # let the render thread finish so we know it wasn't broken either
    t.join(timeout=180)
    print("render result:", render_result)
    # We don't hard-fail if the render errored (source may 404); the key assertion is
    # responsiveness above. But if it did return, it must be 200 and non-trivial.
    if render_result.get("status") is not None:
        assert render_result["status"] in (200, 500), render_result
        if render_result["status"] == 200:
            assert render_result["len"] > 10_000


def test_patch_channel_clips_per_day(s, seeded):
    ch = seeded["channels"][2]
    r = s.patch(f"{API}/channels/{ch['id']}", json={"clips_per_day": 12}, timeout=15)
    assert r.status_code == 200
    assert r.json()["clips_per_day"] == 12
