import os
import re
import json
import uuid
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
REDIRECT_URI = f"{APP_PUBLIC_URL}/api/auth/twitch/callback"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("streamclip")

app = FastAPI()
api = APIRouter(prefix="/api")

HYPE_TAGS = ["Hype Spike", "Laughter", "Creepy Moment", "Insane Play", "Clutch", "Fail", "Wholesome"]
DEMO_IMAGES = [
    "https://images.unsplash.com/photo-1542751371-adc38448a05e?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzd8MHwxfHNlYXJjaHwxfHxlc3BvcnRzJTIwc3RyZWFtZXIlMjBnYW1pbmclMjBzZXR1cCUyMGxpdmUlMjBzdHJlYW18ZW58MHx8fHwxNzg5ODU3NTc0fDA&ixlib=rb-4.1.0&q=85",
    "https://images.unsplash.com/photo-1696710257827-75e2e5954059?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzd8MHwxfHNlYXJjaHw0fHxlc3BvcnRzJTIwc3RyZWFtZXIlMjBnYW1pbmclMjBzZXR1cCUyMGxpdmUlMjBzdHJlYW18ZW58MHx8fHwxNzg5ODU3NTc0fDA&ixlib=rb-4.1.0&q=85",
    "https://images.unsplash.com/photo-1626218174358-7769486c4b79?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Nzd8MHwxfHNlYXJjaHwyfHxlc3BvcnRzJTIwc3RyZWFtZXIlMjBnYW1pbmclMjBzZXR1cCUyMGxpdmUlMjBzdHJlYW18ZW58MHx8fHwxNzg5ODU3NTc0fDA&ixlib=rb-4.1.0&q=85",
    "https://images.pexels.com/photos/12832570/pexels-photo-12832570.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
    "https://images.pexels.com/photos/7862594/pexels-photo-7862594.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
]

DEFAULT_OVERLAY = {
    "position": "bottom",
    "highlight": "#9146FF",
    "font_size": 28,
    "shadow": True,
    "bg": "rgba(8,8,12,0.55)",
}

# ---------------- Models ----------------

class TwitchCreds(BaseModel):
    client_id: str
    client_secret: str


class ChannelCreate(BaseModel):
    url: str


class ChannelUpdate(BaseModel):
    clips_per_day: int | None = None
    auto_clip: bool | None = None
    caption_overlay: dict | None = None


class ClipUpdate(BaseModel):
    caption_overlay: dict | None = None
    ai_caption: str | None = None


class SyncRequest(BaseModel):
    period_days: int = 1


# ---------------- Helpers ----------------

_token_cache = {"token": None, "exp": datetime.now(timezone.utc)}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def get_creds():
    doc = await db.settings.find_one({"_id": "twitch"})
    if doc and doc.get("client_id") and doc.get("client_secret"):
        return doc["client_id"], doc["client_secret"]
    cid = os.environ.get("TWITCH_CLIENT_ID", "")
    csec = os.environ.get("TWITCH_CLIENT_SECRET", "")
    if cid and csec:
        return cid, csec
    return None, None


async def app_token():
    cid, csec = await get_creds()
    if not cid or not csec:
        raise HTTPException(400, "Twitch is not connected yet. Add your Client ID and Secret in Settings.")
    if _token_cache["token"] and _token_cache["exp"] > datetime.now(timezone.utc) + timedelta(seconds=60):
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": cid, "client_secret": csec, "grant_type": "client_credentials"})
    if r.status_code != 200:
        raise HTTPException(400, "Twitch rejected your credentials. Double-check Client ID and Secret.")
    d = r.json()
    _token_cache["token"] = d["access_token"]
    _token_cache["exp"] = datetime.now(timezone.utc) + timedelta(seconds=d.get("expires_in", 3600))
    return _token_cache["token"]


async def helix(path, params=None):
    cid, _ = await get_creds()
    token = await app_token()
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get("https://api.twitch.tv/helix" + path, params=params,
                        headers={"Authorization": f"Bearer {token}", "Client-Id": cid})
    if r.status_code == 401:
        _token_cache["token"] = None
        raise HTTPException(502, "Twitch token expired, please retry.")
    if r.status_code != 200:
        raise HTTPException(502, f"Twitch API error: {r.text[:200]}")
    return r.json()


def parse_login(url: str) -> str:
    s = url.strip().lower()
    s = s.replace("https://", "").replace("http://", "").replace("www.", "")
    if s.startswith("twitch.tv/"):
        s = s.split("twitch.tv/", 1)[1]
    s = s.lstrip("@").split("/")[0].split("?")[0]
    return s


def hype_for(clip_id: str) -> str:
    return HYPE_TAGS[abs(hash(clip_id)) % len(HYPE_TAGS)]


def clean(doc: dict) -> dict:
    doc.pop("_id", None)
    return doc


# ---------------- AI ----------------

AI_SYSTEM = (
    "You are a viral short-form clip editor for TikTok, YouTube Shorts and Reels. "
    "You write punchy, high-CTR titles and captions for Twitch gaming clips. "
    "Always respond with ONLY valid minified JSON, no markdown, no commentary."
)


def _fallback_ai(clip):
    t = clip.get("title") or "Insane Twitch Moment"
    game = clip.get("game_name") or "Live"
    who = clip.get("channel_login", "streamer")
    return {
        "ai_title": t[:70],
        "ai_hashtags": [f"#{who}", "#twitch", "#twitchclips", f"#{re.sub(r'[^a-z0-9]','',game.lower()) or 'gaming'}", "#fyp", "#viral"],
        "ai_caption": (t[:40]).upper(),
    }


async def generate_ai_content(clip: dict) -> dict:
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        return _fallback_ai(clip)
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(api_key=key, session_id=f"clip-{clip['id']}", system_message=AI_SYSTEM).with_model("openai", "gpt-5.4")
        prompt = (
            "Create viral social copy for this Twitch clip.\n"
            f"Streamer: {clip.get('channel_login')}\n"
            f"Original clip title: {clip.get('title')}\n"
            f"Game/Category: {clip.get('game_name')}\n"
            f"Views: {clip.get('view_count')}\n"
            f"Vibe/moment: {clip.get('hype_type')}\n\n"
            "Return JSON with exactly these keys: "
            '{"ai_title": "a catchy <70 char title with emojis", '
            '"ai_hashtags": ["6-8 relevant hashtags each starting with #"], '
            '"ai_caption": "a SHORT punchy 3-6 word ALL-CAPS on-video caption"}'
        )
        resp = await chat.send_message(UserMessage(text=prompt))
        text = resp if isinstance(resp, str) else str(resp)
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0) if m else text)
        return {
            "ai_title": str(data.get("ai_title", ""))[:100] or _fallback_ai(clip)["ai_title"],
            "ai_hashtags": [str(h) for h in data.get("ai_hashtags", [])][:8] or _fallback_ai(clip)["ai_hashtags"],
            "ai_caption": str(data.get("ai_caption", ""))[:60] or _fallback_ai(clip)["ai_caption"],
        }
    except Exception as e:
        logger.warning(f"AI generation failed: {e}")
        return _fallback_ai(clip)


# ---------------- Chat hype sampling (anonymous IRC) ----------------

async def sample_hype(login: str, seconds: int = 6) -> dict:
    login = login.lower()
    count = 0
    try:
        async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=None) as ws:
            await ws.send("PASS SCHMOOPIIE\r\n")
            await ws.send(f"NICK justinfan{uuid.uuid4().int % 900000 + 100000}\r\n")
            await ws.send(f"JOIN #{login}\r\n")
            end = asyncio.get_event_loop().time() + seconds
            while asyncio.get_event_loop().time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=end - asyncio.get_event_loop().time())
                except (asyncio.TimeoutError, Exception):
                    break
                for line in raw.split("\r\n"):
                    if line.startswith("PING"):
                        await ws.send("PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG #" in line:
                        count += 1
    except Exception as e:
        logger.warning(f"hype sample failed for {login}: {e}")
        return {"messages_per_minute": 0, "hype_level": 0, "sampled": False}
    per_min = int(count * (60 / max(seconds, 1)))
    level = min(100, int((per_min / 250) * 100))  # ~250 msgs/min = 100% hype
    return {"messages_per_minute": per_min, "hype_level": level, "sampled": True}


# ---------------- Routes ----------------

@api.get("/")
async def root():
    return {"message": "StreamClip AI"}


@api.get("/settings")
async def get_settings():
    doc = await db.settings.find_one({"_id": "twitch"})
    cid, csec = await get_creds()
    oauth = await db.settings.find_one({"_id": "oauth"})
    return {
        "twitch_configured": bool(cid and csec),
        "client_id_preview": (cid[:6] + "..." if cid else ""),
        "oauth_connected": bool(oauth and oauth.get("access_token")),
        "redirect_uri": REDIRECT_URI,
    }


@api.post("/settings/twitch")
async def save_twitch(creds: TwitchCreds):
    await db.settings.update_one(
        {"_id": "twitch"},
        {"$set": {"client_id": creds.client_id.strip(), "client_secret": creds.client_secret.strip(), "updated_at": now_iso()}},
        upsert=True,
    )
    _token_cache["token"] = None
    await app_token()  # validate immediately
    return {"ok": True}


@api.get("/channels")
async def list_channels():
    chans = await db.channels.find({}, {"_id": 0}).sort("created_at", 1).to_list(500)
    return chans


@api.post("/channels")
async def add_channel(body: ChannelCreate):
    login = parse_login(body.url)
    if not login or not re.match(r"^[a-z0-9_]{2,30}$", login):
        raise HTTPException(400, "That doesn't look like a valid Twitch channel link or username.")
    existing = await db.channels.find_one({"login": login})
    if existing:
        raise HTTPException(400, f"{login} is already added.")

    display_name, user_id, avatar, description = login, None, "", ""
    cid, csec = await get_creds()
    if cid and csec:
        data = await helix("/users", {"login": login})
        if not data.get("data"):
            raise HTTPException(404, f"No Twitch channel found for '{login}'.")
        u = data["data"][0]
        display_name = u["display_name"]
        user_id = u["id"]
        avatar = u["profile_image_url"]
        description = u.get("description", "")

    doc = {
        "id": str(uuid.uuid4()),
        "login": login,
        "display_name": display_name,
        "twitch_user_id": user_id,
        "avatar_url": avatar,
        "description": description,
        "clips_per_day": 24,
        "auto_clip": True,
        "caption_overlay": dict(DEFAULT_OVERLAY),
        "is_demo": False,
        "created_at": now_iso(),
    }
    await db.channels.insert_one(dict(doc))
    return clean(doc)


@api.patch("/channels/{channel_id}")
async def update_channel(channel_id: str, body: ChannelUpdate):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd:
        await db.channels.update_one({"id": channel_id}, {"$set": upd})
    doc = await db.channels.find_one({"id": channel_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Channel not found")
    return doc


@api.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    await db.channels.delete_one({"id": channel_id})
    await db.clips.delete_many({"channel_id": channel_id})
    return {"ok": True}


@api.get("/channels/{channel_id}/live")
async def channel_live(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    if ch.get("is_demo"):
        return {"is_live": True, "viewer_count": 42137, "title": "DEMO: cranking clips live!",
                "game_name": "Just Chatting", "started_at": now_iso()}
    if not ch.get("twitch_user_id"):
        return {"is_live": False, "viewer_count": 0, "title": "", "game_name": ""}
    data = await helix("/streams", {"user_id": ch["twitch_user_id"]})
    if not data.get("data"):
        return {"is_live": False, "viewer_count": 0, "title": "", "game_name": ""}
    s = data["data"][0]
    return {"is_live": True, "viewer_count": s["viewer_count"], "title": s["title"],
            "game_name": s.get("game_name", ""), "started_at": s.get("started_at")}


@api.get("/channels/{channel_id}/hype")
async def channel_hype(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    res = await sample_hype(ch["login"])
    return res


async def _store_clips(ch, raw_clips, limit, generate_ai=True):
    raw_clips.sort(key=lambda x: x.get("view_count", 0), reverse=True)
    raw_clips = raw_clips[:limit]
    sem = asyncio.Semaphore(5)
    out = []

    async def process(rc):
        existing = await db.clips.find_one({"twitch_clip_id": rc["id"]}, {"_id": 0})
        if existing:
            out.append(existing)
            return
        clip = {
            "id": str(uuid.uuid4()),
            "channel_id": ch["id"],
            "channel_login": ch["login"],
            "twitch_clip_id": rc["id"],
            "title": rc.get("title", ""),
            "url": rc.get("url", ""),
            "embed_url": rc.get("embed_url", ""),
            "thumbnail_url": rc.get("thumbnail_url", ""),
            "duration": rc.get("duration", 0),
            "view_count": rc.get("view_count", 0),
            "creator_name": rc.get("creator_name", ""),
            "game_name": rc.get("game_name", ""),
            "created_at_twitch": rc.get("created_at", ""),
            "hype_type": hype_for(rc["id"]),
            "caption_overlay": dict(ch.get("caption_overlay", DEFAULT_OVERLAY)),
            "is_demo": False,
            "created_at": now_iso(),
        }
        if generate_ai:
            async with sem:
                ai = await generate_ai_content(clip)
            clip.update(ai)
            clip["generated"] = True
        else:
            clip.update(_fallback_ai(clip))
            clip["generated"] = False
        await db.clips.insert_one(dict(clip))
        out.append(clean(clip))

    await asyncio.gather(*[process(rc) for rc in raw_clips])
    return out


@api.post("/channels/{channel_id}/sync")
async def sync_clips(channel_id: str, body: SyncRequest):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    if ch.get("is_demo"):
        raise HTTPException(400, "This is a demo channel with sample clips already loaded.")
    if not ch.get("twitch_user_id"):
        raise HTTPException(400, "Connect Twitch in Settings so I can look up this channel.")
    started = (datetime.now(timezone.utc) - timedelta(days=max(1, body.period_days))).isoformat()
    data = await helix("/clips", {"broadcaster_id": ch["twitch_user_id"], "started_at": started, "first": 100})
    raw = data.get("data", [])
    limit = ch.get("clips_per_day", 24)
    stored = await _store_clips(ch, raw, limit)
    return {"fetched": len(raw), "stored": len(stored), "clips": stored}


@api.get("/channels/{channel_id}/vods")
async def list_vods(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    if not ch.get("twitch_user_id"):
        raise HTTPException(400, "Connect Twitch in Settings first.")
    data = await helix("/videos", {"user_id": ch["twitch_user_id"], "type": "archive", "first": 20})
    return [{"id": v["id"], "title": v["title"], "created_at": v["created_at"],
             "duration": v["duration"], "url": v["url"], "thumbnail_url": v.get("thumbnail_url", "")}
            for v in data.get("data", [])]


@api.post("/channels/{channel_id}/pull-vod")
async def pull_vod(channel_id: str, days: int = Query(30)):
    """Pull the best clips from a channel's past broadcasts window."""
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    if not ch.get("twitch_user_id"):
        raise HTTPException(400, "Connect Twitch in Settings first.")
    started = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    data = await helix("/clips", {"broadcaster_id": ch["twitch_user_id"], "started_at": started, "first": 100})
    raw = data.get("data", [])
    limit = ch.get("clips_per_day", 24)
    stored = await _store_clips(ch, raw, limit)
    return {"fetched": len(raw), "stored": len(stored), "clips": stored}


@api.get("/clips")
async def get_clips(channel_id: str | None = None):
    q = {"channel_id": channel_id} if channel_id else {}
    clips = await db.clips.find(q, {"_id": 0}).sort("view_count", -1).to_list(2000)
    return clips


@api.post("/clips/{clip_id}/generate")
async def regenerate_clip(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    ai = await generate_ai_content(clip)
    ai["generated"] = True
    await db.clips.update_one({"id": clip_id}, {"$set": ai})
    clip.update(ai)
    return clip


@api.patch("/clips/{clip_id}")
async def update_clip(clip_id: str, body: ClipUpdate):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd:
        await db.clips.update_one({"id": clip_id}, {"$set": upd})
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


@api.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str):
    await db.clips.delete_one({"id": clip_id})
    return {"ok": True}


def clip_mp4_url(thumbnail_url: str) -> str | None:
    """Derive the direct high-quality MP4 from a Twitch clip thumbnail URL."""
    if not thumbnail_url or "-preview" not in thumbnail_url:
        return None
    return thumbnail_url.split("-preview")[0] + ".mp4"


@api.get("/clips/{clip_id}/download")
async def download_clip(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.get("is_demo"):
        raise HTTPException(400, "This is a sample clip. Connect Twitch to pull real, downloadable videos.")
    mp4 = clip_mp4_url(clip.get("thumbnail_url", ""))
    if not mp4:
        raise HTTPException(400, "No downloadable video is available for this clip.")

    local = httpx.AsyncClient(timeout=None, follow_redirects=True)
    resp = await local.send(local.build_request("GET", mp4), stream=True)
    if resp.status_code != 200:
        await resp.aclose()
        await local.aclose()
        raise HTTPException(404, "The video file could not be fetched from Twitch.")

    name = re.sub(r"[^a-zA-Z0-9]+", "_", (clip.get("ai_title") or clip.get("title") or "clip")).strip("_")[:50] or "clip"

    async def gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await local.aclose()

    return StreamingResponse(
        gen(),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{name}.mp4"'},
    )


# -------- OAuth (create-clip capability) --------

@api.get("/auth/twitch/start")
async def oauth_start():
    cid, _ = await get_creds()
    if not cid:
        raise HTTPException(400, "Add Twitch credentials first.")
    from urllib.parse import urlencode
    state = uuid.uuid4().hex
    await db.settings.update_one({"_id": "oauth_state"}, {"$set": {"state": state}}, upsert=True)
    q = urlencode({"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT_URI,
                   "scope": "clips:edit", "state": state})
    return RedirectResponse("https://id.twitch.tv/oauth2/authorize?" + q)


@api.get("/auth/twitch/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"{APP_PUBLIC_URL}/?twitch=error")
    saved = await db.settings.find_one({"_id": "oauth_state"})
    if not state or not saved or saved.get("state") != state:
        return RedirectResponse(f"{APP_PUBLIC_URL}/?twitch=error")
    cid, csec = await get_creds()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": cid, "client_secret": csec, "code": code,
            "grant_type": "authorization_code", "redirect_uri": REDIRECT_URI})
    if r.status_code != 200:
        return RedirectResponse(f"{APP_PUBLIC_URL}/?twitch=error")
    t = r.json()
    await db.settings.update_one({"_id": "oauth"}, {"$set": {
        "access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
        "updated_at": now_iso()}}, upsert=True)
    return RedirectResponse(f"{APP_PUBLIC_URL}/?twitch=connected")


@api.post("/channels/{channel_id}/clip-now")
async def clip_now(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    oauth = await db.settings.find_one({"_id": "oauth"})
    if not oauth or not oauth.get("access_token"):
        raise HTTPException(401, "Connect your Twitch account (Settings) to create live clips.")
    cid, _ = await get_creds()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.twitch.tv/helix/clips",
                         params={"broadcaster_id": ch["twitch_user_id"]},
                         headers={"Authorization": f"Bearer {oauth['access_token']}", "Client-Id": cid})
    if r.status_code not in (200, 202):
        raise HTTPException(r.status_code, f"Could not create clip: {r.text[:200]}")
    return {"ok": True, "pending": r.json().get("data", [])}


# -------- Demo --------

DEMO_CHANNELS = [
    {"login": "xqc", "display_name": "xQc", "game": "Grand Theft Auto V"},
    {"login": "kaicenat", "display_name": "KaiCenat", "game": "Just Chatting"},
    {"login": "pokimane", "display_name": "Pokimane", "game": "Valorant"},
]

DEMO_CLIP_TITLES = [
    "He did NOT expect that to happen", "1 HP clutch of the century", "Chat went absolutely feral",
    "This jumpscare took 5 years off my life", "The funniest fail you'll see today",
    "Insane 200 IQ outplay", "Wholesome moment with the community", "This bit had everyone crying laughing",
]


@api.post("/demo/seed")
async def seed_demo():
    await db.channels.delete_many({"is_demo": True})
    await db.clips.delete_many({"is_demo": True})
    created = []
    for i, dc in enumerate(DEMO_CHANNELS):
        ch = {
            "id": str(uuid.uuid4()),
            "login": dc["login"],
            "display_name": dc["display_name"],
            "twitch_user_id": None,
            "avatar_url": DEMO_IMAGES[i % len(DEMO_IMAGES)],
            "description": "Demo channel with sample clips.",
            "clips_per_day": 24,
            "auto_clip": True,
            "caption_overlay": dict(DEFAULT_OVERLAY),
            "is_demo": True,
            "created_at": now_iso(),
        }
        await db.channels.insert_one(dict(ch))
        created.append(clean(dict(ch)))
        for j in range(6):
            title = DEMO_CLIP_TITLES[(i * 6 + j) % len(DEMO_CLIP_TITLES)]
            cid_local = str(uuid.uuid4())
            hype = HYPE_TAGS[(i * 6 + j) % len(HYPE_TAGS)]
            clip = {
                "id": cid_local,
                "channel_id": ch["id"],
                "channel_login": dc["login"],
                "twitch_clip_id": f"demo-{cid_local}",
                "title": title,
                "url": f"https://twitch.tv/{dc['login']}",
                "embed_url": "",
                "thumbnail_url": DEMO_IMAGES[(i + j) % len(DEMO_IMAGES)],
                "duration": 30 + (j * 4),
                "view_count": 250000 - (j * 21000) - (i * 5000),
                "creator_name": "ClipBot",
                "game_name": dc["game"],
                "created_at_twitch": now_iso(),
                "hype_type": hype,
                "caption_overlay": dict(DEFAULT_OVERLAY),
                "is_demo": True,
                "generated": True,
                "ai_title": f"{title} 😱🔥",
                "ai_hashtags": [f"#{dc['login']}", "#twitch", "#twitchclips",
                                f"#{re.sub(r'[^a-z0-9]','', dc['game'].lower())}", "#fyp", "#viral", "#gaming"],
                "ai_caption": title.upper()[:38],
                "created_at": now_iso(),
            }
            await db.clips.insert_one(dict(clip))
    return {"ok": True, "channels": created}


# -------- Background auto-sync --------

async def auto_sync_loop():
    await asyncio.sleep(30)
    while True:
        try:
            cid, csec = await get_creds()
            if cid and csec:
                chans = await db.channels.find({"auto_clip": True, "is_demo": {"$ne": True}}).to_list(200)
                for ch in chans:
                    if not ch.get("twitch_user_id"):
                        continue
                    try:
                        live = await helix("/streams", {"user_id": ch["twitch_user_id"]})
                        if not live.get("data"):
                            continue
                        started = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                        data = await helix("/clips", {"broadcaster_id": ch["twitch_user_id"], "started_at": started, "first": 100})
                        current = await db.clips.count_documents({
                            "channel_id": ch["id"],
                            "created_at": {"$gte": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}})
                        remaining = max(0, ch.get("clips_per_day", 24) - current)
                        if remaining:
                            await _store_clips(ch, data.get("data", []), remaining)
                    except Exception as e:
                        logger.warning(f"auto-sync {ch.get('login')}: {e}")
        except Exception as e:
            logger.warning(f"auto_sync_loop: {e}")
        await asyncio.sleep(900)  # every 15 minutes


@app.on_event("startup")
async def on_startup():
    await db.channels.create_index("login")
    await db.clips.create_index("twitch_clip_id")
    asyncio.create_task(auto_sync_loop())


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
