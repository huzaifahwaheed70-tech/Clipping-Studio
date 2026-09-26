import os
import re
import json
import math
import uuid
import shutil
import asyncio
import logging
import tempfile
import hashlib
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

import httpx
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse, StreamingResponse, FileResponse, Response
from starlette.background import BackgroundTask
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


class BufferChannelConfig(BaseModel):
    platform: str          # "tiktok", "instagram", "youtube", etc.
    channel_id: str        # Buffer's channel ID for that connected account
    max_posts_per_day: int = 12   # stay safely under TikTok's ~15/day cap


class ChannelUpdate(BaseModel):
    clips_per_day: int | None = None
    auto_clip: bool | None = None
    caption_overlay: dict | None = None
    buffer_channels: list[BufferChannelConfig] | None = None


class ClipUpdate(BaseModel):
    caption_overlay: dict | None = None
    ai_caption: str | None = None


class SyncRequest(BaseModel):
    period_days: int = 1


class BufferSettingsIn(BaseModel):
    api_key: str


# ---------------- Helpers ----------------

_token_cache = {"token": None, "exp": datetime.now(timezone.utc)}

# Durable, disk-backed persistence so the Twitch connection + channels survive
# database resets (there is no login, so this keeps everything shared + persistent).
APPDATA = ROOT_DIR / ".appdata"
APPDATA.mkdir(exist_ok=True)
SETTINGS_FILE = APPDATA / "settings.json"
CHANNELS_FILE = APPDATA / "channels.json"


def _write_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, default=str))
    except Exception as e:
        logger.warning(f"persist write {path.name} failed: {e}")


def _read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        logger.warning(f"persist read {path.name} failed: {e}")
    return None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def snapshot_settings():
    docs = await db.settings.find({}).to_list(50)
    _write_json(SETTINGS_FILE, docs)


async def snapshot_channels():
    docs = await db.channels.find({}, {"_id": 0}).to_list(500)
    _write_json(CHANNELS_FILE, docs)


async def restore_from_disk():
    """On startup, if the DB was reset, restore the Twitch connection + channels from disk."""
    if await db.settings.count_documents({}) == 0:
        data = _read_json(SETTINGS_FILE)
        if data:
            for d in data:
                _id = d.get("_id")
                if _id:
                    d.pop("_id", None)
                    await db.settings.update_one({"_id": _id}, {"$set": d}, upsert=True)
            logger.info("restored Twitch settings from disk backup")
    if await db.channels.count_documents({}) == 0:
        data = _read_json(CHANNELS_FILE)
        if data:
            await db.channels.insert_many([{k: v for k, v in c.items() if k != "_id"} for c in data])
            logger.info(f"restored {len(data)} channels from disk backup")


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


async def helix_user(login: str):
    data = await helix("/users", {"login": login.lower()})
    rows = data.get("data") or []
    return rows[0] if rows else None


async def helix_stream(user_id: str):
    data = await helix("/streams", {"user_id": user_id, "first": 1})
    rows = data.get("data") or []
    return rows[0] if rows else None


def parse_twitch_duration(value: str) -> int:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def helix_video_to_vod(v: dict) -> dict:
    return {
        "id": v.get("id"),
        "title": v.get("title") or "",
        "length": parse_twitch_duration(v.get("duration", "")),
        "created_at": v.get("created_at") or "",
        "thumbnail": (v.get("thumbnail_url") or "").replace("%{width}", "480").replace("%{height}", "272"),
        "game": "",
        "url": v.get("url") or f"https://www.twitch.tv/videos/{v.get('id')}",
    }


async def helix_latest_vod(user_id: str):
    data = await helix("/videos", {"user_id": user_id, "type": "archive", "first": 1})
    rows = data.get("data") or []
    return helix_video_to_vod(rows[0]) if rows else None


async def helix_latest_vods(user_id: str, first: int = 10):
    data = await helix("/videos", {"user_id": user_id, "type": "archive", "first": min(first, 100)})
    return [helix_video_to_vod(v) for v in data.get("data") or []]


# ---------------- Buffer auto-posting ----------------

BUFFER_GQL_URL = "https://api.buffer.com"

CREATE_POST_MUTATION = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post { id }
    }
    ... on MutationError {
      message
    }
  }
}
"""


async def buffer_graphql(api_key: str, query: str, variables: dict):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            BUFFER_GQL_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
    r.raise_for_status()
    return r.json()


async def posts_today_count(channel_id: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return await db.buffer_posts.count_documents({
        "channel_id": channel_id,
        "created_at": {"$gte": cutoff},
    })


def clip_video_public_url(clip_id: str) -> str:
    return f"{APP_PUBLIC_URL}/api/clips/{clip_id}/video"


async def queue_clip_to_buffer(clip: dict, channel: dict, api_key: str):
    text = (clip.get("ai_title") or clip.get("title") or "").strip()
    hashtags = " ".join(clip.get("ai_hashtags") or [])
    if hashtags:
        text = f"{text}\n\n{hashtags}"

    video_url = clip_video_public_url(clip["id"])
    variables = {
        "input": {
            "text": text,
            "channelId": channel["channel_id"],
            "schedulingType": "automatic",
            "mode": "addToQueue",
            "assets": [{"video": {"url": video_url}}],
        }
    }
    try:
        resp = await buffer_graphql(api_key, CREATE_POST_MUTATION, variables)
        result = (resp.get("data") or {}).get("createPost") or {}
        if result.get("message"):
            raise RuntimeError(result["message"])
        post_id = ((result.get("post") or {}).get("id"))
        await db.buffer_posts.insert_one({
            "id": str(uuid.uuid4()),
            "clip_id": clip["id"],
            "platform": channel["platform"],
            "channel_id": channel["channel_id"],
            "buffer_post_id": post_id,
            "created_at": now_iso(),
        })
        await db.clips.update_one(
            {"id": clip["id"]},
            {"$addToSet": {"buffer_posted_channels": channel["channel_id"]}},
        )
        logger.info(f"queued clip {clip['id']} to Buffer ({channel['platform']})")
    except Exception as e:
        logger.warning(f"buffer post failed for clip {clip['id']} on {channel['platform']}: {e}")
        await db.clips.update_one(
            {"id": clip["id"]},
            {"$set": {f"buffer_error.{channel['channel_id']}": str(e)}},
        )


async def buffer_sync_loop():
    """Every few minutes, for each Twitch channel that has its own configured
    posting accounts, queue that channel's oldest not-yet-posted rendered clip
    to each of its destinations, respecting each destination's daily cap."""
    await asyncio.sleep(60)
    while True:
        try:
            settings = await db.settings.find_one({"_id": "buffer"})
            api_key = settings.get("api_key") if settings else None
            if api_key:
                channels = await db.channels.find({
                    "buffer_channels": {"$exists": True, "$ne": []},
                }).to_list(200)
                for ch in channels:
                    for dest in ch.get("buffer_channels", []):
                        count = await posts_today_count(dest["channel_id"])
                        if count >= dest.get("max_posts_per_day", 12):
                            continue
                        clip = await db.clips.find_one(
                            {
                                "channel_id": ch["id"],
                                "render_status": "done",
                                "buffer_posted_channels": {"$ne": dest["channel_id"]},
                            },
                            sort=[("created_at", 1)],
                        )
                        if clip:
                            await queue_clip_to_buffer(clip, dest, api_key)
                            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"buffer_sync_loop: {e}")
        await asyncio.sleep(300)  # check every 5 minutes


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


TITLE_PATTERNS = [
    "{who} JUST BROKE THE CHAT",
    "CHAT DID NOT EXPECT THIS",
    "THAT PLAY WAS ACTUALLY INSANE",
    "THE WHOLE CHAT LOST IT",
    "THIS MOMENT GOT WILD FAST",
    "NOBODY SAW THAT COMING",
    "THAT REACTION WAS PERFECT",
    "THIS WAS PURE CHAOS",
    "THE CLUTCH OF THE STREAM",
    "THAT WAS WAY TOO CLEAN",
    "BRO REALLY PULLED THAT OFF",
    "THE STREAM JUST ERUPTED",
    "THAT MOMENT WAS UNREAL",
    "EVERYONE IN CHAT FREAKED OUT",
    "THIS TURNED INTO A MOVIE",
    "THE TIMING WAS PERFECT",
    "THAT WAS ABSOLUTELY NUTS",
    "THE CHAT COULD NOT HANDLE THIS",
    "ONE OF THE CRAZIEST MOMENTS",
    "THAT PLAY CHANGED EVERYTHING",
    "THE REACTION MAKES THIS 10X BETTER",
    "THIS ESCALATED SO FAST",
    "THE PERFECT LAST-SECOND PLAY",
    "THAT WAS A MASSIVE THROW",
    "THE MOST RANDOM MOMENT EVER",
    "CHAT KNEW SOMETHING WAS COMING",
    "THAT MOMENT DESERVED A CLIP",
    "THIS IS WHY WE WATCH LIVE",
    "THE STREAM PEAKED RIGHT HERE",
    "ABSOLUTE CHAOS IN THE CHAT",
]

def _fallback_ai(clip):
    t = clip.get("title") or "Insane Twitch Moment"
    game = clip.get("game_name") or "Live"
    who = clip.get("channel_login", "streamer")
    return {
        "ai_title": t[:70],
        "ai_hashtags": [f"#{who}", "#twitch", "#twitchclips", f"#{re.sub(r'[^a-z0-9]','',game.lower()) or 'gaming'}", "#fyp", "#viral"],
        "ai_caption": (t[:40]).upper(),
    }


async def make_unique_clip_title(clip: dict) -> str:
    """Create a unique title without consuming an LLM credit."""
    who = (clip.get("channel_login") or "streamer").replace("_", " ").strip().title()
    hype = clip.get("hype_type") or "Hype Spike"
    source = str(clip.get("twitch_clip_id") or clip.get("id") or uuid.uuid4().hex)
    digest = hashlib.sha1(source.encode()).hexdigest()[:6].upper()
    base = TITLE_PATTERNS[int(digest, 16) % len(TITLE_PATTERNS)].format(who=who)
    candidate = f"{base} • {hype} #{digest}"[:90]
    if not await db.clips.find_one({"ai_title": candidate}):
        return candidate
    return f"{base} • {hype} #{uuid.uuid4().hex[:8].upper()}"[:90]


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


# ---------------- Twitch GQL (no credentials needed) ----------------

CLIPS_QUERY = """query($login:String!,$limit:Int!,$period:ClipsPeriod!){
  user(login:$login){
    id displayName profileImageURL(width:150) description
    clips(first:$limit, criteria:{period:$period, sort:VIEWS_DESC}){
      edges{ node{ slug title viewCount durationSeconds createdAt thumbnailURL game{ name } } }
    }
  }
}"""

INFO_QUERY = """query($login:String!){
  user(login:$login){ id displayName profileImageURL(width:150) description
    stream{ id viewersCount type game{ name } } lastBroadcast{ title } }
}"""


def _period_for(days: int) -> str:
    if days <= 1:
        return "LAST_DAY"
    if days <= 7:
        return "LAST_WEEK"
    if days <= 31:
        return "LAST_MONTH"
    return "ALL_TIME"


async def gql_query(query: str, variables: dict):
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post("https://gql.twitch.tv/gql",
                             json={"query": query, "variables": variables},
                             headers={"Client-ID": GQL_CLIENT_ID})
        if r.status_code != 200:
            logger.warning(f"gql http {r.status_code}")
            return None
        d = r.json()
        if isinstance(d, dict):
            if d.get("errors"):
                logger.warning(f"gql errors: {str(d['errors'])[:200]}")
            return d.get("data")
        return None
    except Exception as e:
        logger.warning(f"gql failed: {e}")
        return None


def _node_to_raw(node: dict) -> dict:
    slug = node["slug"]
    return {
        "id": slug,
        "title": node.get("title", ""),
        "url": f"https://clips.twitch.tv/{slug}",
        "embed_url": f"https://clips.twitch.tv/embed?clip={slug}",
        "thumbnail_url": node.get("thumbnailURL", ""),
        "duration": node.get("durationSeconds", 0),
        "view_count": node.get("viewCount", 0),
        "creator_name": "",
        "game_name": (node.get("game") or {}).get("name", ""),
        "created_at": node.get("createdAt", ""),
    }


async def gql_channel_info(login: str):
    data = await gql_query(INFO_QUERY, {"login": login})
    return (data or {}).get("user")


async def gql_list_clips(login: str, period: str, limit: int = 100):
    data = await gql_query(CLIPS_QUERY, {"login": login, "limit": limit, "period": period})
    user = (data or {}).get("user")
    if not user:
        return None, []
    edges = (user.get("clips") or {}).get("edges") or []
    return user, [_node_to_raw(e["node"]) for e in edges if e.get("node")]


# ---------------- VOD + Live recording engine (credential-free) ----------------
# We record the streamer's own video feed (their past broadcasts / live stream) with
# ffmpeg and cut the moments where the live chat blew up. Nothing is a Twitch pre-made clip.

GQL_VOD_CHAT_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

VODS_QUERY = """query($login:String!,$limit:Int!){
  user(login:$login){ id displayName
    videos(first:$limit, type:ARCHIVE, sort:TIME){
      edges{ node{ id title lengthSeconds createdAt previewThumbnailURL(width:480,height:272) game{ name } } }
    }
  }
}"""


async def gql_list_vods(login: str, limit: int = 5):
    data = await gql_query(VODS_QUERY, {"login": login, "limit": limit})
    user = (data or {}).get("user")
    if not user:
        return None, []
    edges = (user.get("videos") or {}).get("edges") or []
    vods = []
    for e in edges:
        n = e.get("node") or {}
        if not n.get("id"):
            continue
        thumb = (n.get("previewThumbnailURL") or "").replace("%{width}", "480").replace("%{height}", "272")
        vods.append({
            "id": n["id"], "title": n.get("title", ""),
            "length": int(n.get("lengthSeconds") or 0),
            "created_at": n.get("createdAt", ""),
            "thumbnail": thumb,
            "game": (n.get("game") or {}).get("name", ""),
        })
    return user, vods


async def _access_token(kind: str, ident: str):
    if kind == "vod":
        q = ('query{videoPlaybackAccessToken(id:"%s",params:{platform:"web",'
             'playerBackend:"mediaplayer",playerType:"site"}){signature value}}' % ident)
        field = "videoPlaybackAccessToken"
    else:
        q = ('query{streamPlaybackAccessToken(channelName:"%s",params:{platform:"web",'
             'playerBackend:"mediaplayer",playerType:"site"}){signature value}}' % ident)
        field = "streamPlaybackAccessToken"
    data = await gql_query(q, {})
    tok = (data or {}).get(field)
    if not tok or not tok.get("signature"):
        return None
    return tok


async def _best_variant(usher_url: str):
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(usher_url)
        if r.status_code != 200:
            logger.warning(f"usher {r.status_code}")
            return None
        urls = [ln.strip() for ln in r.text.splitlines() if ln.strip().startswith("http")]
        return urls[0] if urls else None  # first entry = highest quality (source/chunked)
    except Exception as e:
        logger.warning(f"usher fetch failed: {e}")
        return None


async def vod_playlist_url(vod_id: str):
    tok = await _access_token("vod", vod_id)
    if not tok:
        return None
    ush = (f"https://usher.ttvnw.net/vod/{vod_id}.m3u8?sig={tok['signature']}"
           f"&token={quote(tok['value'])}&allow_source=true&allow_audio_only=false&player=twitchweb")
    return await _best_variant(ush)


async def live_playlist_url(login: str):
    tok = await _access_token("live", login)
    if not tok:
        return None
    ush = (f"https://usher.ttvnw.net/api/channel/hls/{login}.m3u8?sig={tok['signature']}"
           f"&token={quote(tok['value'])}&allow_source=true&allow_audio_only=false"
           f"&player=twitchweb&fast_bread=true")
    return await _best_variant(ush)


async def vod_chat_density(vod_id: str, offset: int) -> float:
    """Messages-per-second in the chat replay around `offset` = how hyped the moment was."""
    body = [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": vod_id, "contentOffsetSeconds": int(offset)},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_VOD_CHAT_HASH}},
    }]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://gql.twitch.tv/gql", json=body, headers={"Client-ID": GQL_CLIENT_ID})
        edges = r.json()[0]["data"]["video"]["comments"]["edges"]
        offs = [e["node"]["contentOffsetSeconds"] for e in edges if e.get("node")]
        if len(offs) < 2:
            return 0.0
        span = max(offs) - min(offs)
        return float(len(offs)) if span <= 0 else len(offs) / span
    except Exception:
        return 0.0


def _moment_duration(density: float) -> int:
    """Clip length follows how long/intense the hype is (bounded for the 2-core server)."""
    return int(max(18, min(45, 18 + density * 6)))


async def find_hype_moments_in_vod(vod_id: str, vod_length: int, num_moments: int):
    """Find non-overlapping chat spikes. Only strong relative spikes are returned."""
    start = 60
    end = max(start + 60, vod_length - 60)
    if end <= start:
        start, end = 0, max(60, vod_length)

    # A 20-second grid gives enough resolution without creating hundreds of
    # requests for a long VOD. This is not a clip-count limit.
    step = 20
    offsets = list(range(start, end, step))
    sem = asyncio.Semaphore(8)

    async def sample(offset):
        async with sem:
            return offset, await vod_chat_density(vod_id, offset)

    results = await asyncio.gather(*(sample(o) for o in offsets), return_exceptions=True)
    valid = [r for r in results if isinstance(r, tuple) and r[1] > 0]
    if not valid:
        return []

    values = sorted(r[1] for r in valid)
    median = values[len(values) // 2]
    threshold = max(0.15, median * 1.35)
    hot = sorted([r for r in valid if r[1] >= threshold], key=lambda x: x[1], reverse=True)

    picked = []
    for offset, density in hot:
        duration = _moment_duration(density)
        if all(abs(offset - p["peak"]) >= max(25, p["duration"] * 1.25) for p in picked):
            picked.append({
                "peak": offset,
                "offset": max(0, int(offset - duration * 0.35)),
                "duration": duration,
                "density": round(density, 2),
            })

    return picked[:num_moments]


CLIP_LEN_DEFAULT = 30
RENDER_DIR = "/tmp/renders"
os.makedirs(RENDER_DIR, exist_ok=True)


async def _make_poster(video_path: str, out: str):
    try:
        args = ["nice", "-n", "19", "ffmpeg", "-y", "-i", video_path,
                "-ss", "1", "-frames:v", "1", "-q:v", "4", "-threads", "1", out]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=30)
        return os.path.exists(out)
    except Exception:
        return False


async def _store_vod_clips(ch: dict, vods: list, need: int = 0):
    """Create clips from the single most recent completed VOD.

    There is no daily clip quota. The number of moments is determined by the
    amount of non-overlapping chat activity found in the latest VOD. AI is not
    called while discovering clips, which keeps discovery cheap and reliable.
    """
    if not vods:
        return []

    # Twitch returns newest broadcasts first. Only use the latest completed VOD
    # for the Get Clips button, exactly as the UI promises.
    vod = max(vods, key=lambda v: v.get("created_at", ""))
    length = int(vod.get("length", 0) or 0)
    if length < 30:
        return []

    # One scan point roughly every 30 seconds. This is a discovery resolution,
    # not a clip-count limit. Longer broadcasts therefore naturally produce more
    # candidate moments.
    scan_count = max(30, min(360, length // 20))
    moments = await find_hype_moments_in_vod(vod["id"], length, scan_count)

    out = []
    for m in moments:
        off = int(m["offset"])
        dup = await db.clips.find_one({
            "channel_id": ch["id"],
            "vod_id": vod["id"],
            "start_seconds": {"$gte": off - 12, "$lte": off + 12},
        })
        if dup:
            continue

        cid = str(uuid.uuid4())
        clip = {
            "id": cid,
            "channel_id": ch["id"],
            "channel_login": ch["login"],
            "source_type": "vod",
            "vod_id": vod["id"],
            "start_seconds": off,
            "duration": m["duration"],
            "twitch_clip_id": f"vod-{vod['id']}-{off}",
            "title": vod.get("title", ""),
            "url": f"https://www.twitch.tv/videos/{vod['id']}?t={off}s",
            "embed_url": "",
            "thumbnail_url": vod.get("thumbnail", ""),
            "poster_file": None,
            "view_count": 0,
            "hype_density": m["density"],
            "creator_name": "",
            "game_name": vod.get("game", ""),
            "created_at_twitch": vod.get("created_at", ""),
            "hype_type": hype_for(cid),
            "caption_overlay": dict(ch.get("caption_overlay", DEFAULT_OVERLAY)),
                "rendered": False,
            "render_status": "pending",
            "render_file": None,
            "created_at": now_iso(),
            **_fallback_ai({
                "title": vod.get("title", ""),
                "game_name": vod.get("game", ""),
                "channel_login": ch["login"],
            }),
            "generated": False,
        }
        clip["ai_title"] = await make_unique_clip_title(clip)
        clip["ai_caption"] = clip["ai_title"][:40].upper()
        await db.clips.insert_one(dict(clip))
        out.append(clean(dict(clip)))

    return out


async def create_live_twitch_clip(ch: dict, hype: dict, user: dict):
    """Create a real Twitch clip at a live hype spike.

    Requires the Twitch account connected in Settings to have clips:edit.
    Twitch captures the live broadcast itself, so this does not depend on a
    fragile local HLS buffer.
    """
    oauth = await db.settings.find_one({"_id": "oauth"})
    access_token = (oauth or {}).get("access_token")
    if not access_token:
        logger.warning("live auto-clipping skipped: Twitch OAuth is not connected")
        return None

    cid, _ = await get_creds()
    if not cid or not ch.get("twitch_user_id"):
        return None

    try:
        temp_clip = {
            "id": str(uuid.uuid4()),
            "twitch_clip_id": f"live-{ch['id']}-{int(asyncio.get_event_loop().time())}",
            "channel_login": ch["login"],
            "hype_type": hype_for(ch["id"]),
            "start_seconds": 0,
        }
        unique_title = await make_unique_clip_title(temp_clip)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.twitch.tv/helix/clips",
                params={
                    "broadcaster_id": ch["twitch_user_id"],
                    "has_delay": "false",
                    "duration": "30",
                    "title": unique_title,
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": cid,
                },
            )
        if r.status_code not in (200, 202):
            logger.warning("Twitch create clip failed: %s %s", r.status_code, r.text[:300])
            return None

        item = (r.json().get("data") or [None])[0]
        if not item:
            return None

        clip_id = item.get("id")
        edit_url = item.get("edit_url", "")
        if not clip_id:
            return None

        # Give Twitch a moment to publish the clip, then resolve its public URL.
        await asyncio.sleep(2)
        raw = await gql_list_clips(ch["login"], "LAST_DAY", 100)
        _, recent = raw
        found = next((x for x in recent if x.get("id") == clip_id), None)

        clip = {
            "id": str(uuid.uuid4()),
            "channel_id": ch["id"],
            "channel_login": ch["login"],
            "source_type": "live",
            "vod_id": None,
            "start_seconds": 0,
            "duration": 30,
            "twitch_clip_id": clip_id,
            "title": user.get("title", ""),
            "url": (found or {}).get("url") or f"https://clips.twitch.tv/{clip_id}",
            "embed_url": (found or {}).get("embed_url", ""),
            "thumbnail_url": (found or {}).get("thumbnail_url", ""),
            "poster_file": None,
            "view_count": (found or {}).get("view_count", 0),
            "hype_density": round(hype.get("messages_per_minute", 0) / 60, 2),
            "creator_name": "",
            "game_name": user.get("game_name", ""),
            "created_at_twitch": now_iso(),
            "hype_type": hype_for(clip_id),
            "caption_overlay": dict(ch.get("caption_overlay", DEFAULT_OVERLAY)),
                "rendered": False,
            "render_status": "pending",
            "render_file": None,
            "created_at": now_iso(),
            **_fallback_ai({
                "title": user.get("title", "Live Twitch Moment"),
                "game_name": user.get("game_name", ""),
                "channel_login": ch["login"],
            }),
            "generated": False,
            "twitch_edit_url": edit_url,
        }
        clip["ai_title"] = unique_title
        clip["ai_caption"] = clip["ai_title"][:40].upper()
        await db.clips.insert_one(dict(clip))
        return clean(dict(clip))
    except Exception as e:
        logger.exception("create_live_twitch_clip failed for %s: %s", ch.get("login"), e)
        return None


# ---------------- Routes ----------------

@api.get("/")
async def root():
    return {"message": "StreamClip AI"}


@api.get("/settings")
async def get_settings():
    doc = await db.settings.find_one({"_id": "twitch"})
    cid, csec = await get_creds()
    oauth = await db.settings.find_one({"_id": "oauth"})
    buffer_doc = await db.settings.find_one({"_id": "buffer"})
    return {
        "twitch_configured": bool(cid and csec),
        "client_id_preview": (cid[:6] + "..." if cid else ""),
        "oauth_connected": bool(oauth and oauth.get("access_token")),
        "redirect_uri": REDIRECT_URI,
        "buffer_configured": bool(buffer_doc and buffer_doc.get("api_key")),
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
    await snapshot_settings()
    return {"ok": True}


@api.post("/settings/buffer")
async def save_buffer_settings(body: BufferSettingsIn):
    await db.settings.update_one(
        {"_id": "buffer"},
        {"$set": {"api_key": body.api_key.strip(), "updated_at": now_iso()}},
        upsert=True,
    )
    await snapshot_settings()
    return {"ok": True}


@api.get("/settings/buffer/channels")
async def list_buffer_channels():
    """Fetch the user's connected Buffer channels (TikTok/Instagram/YouTube/etc.)
    so the frontend can show a picker instead of asking for raw channel IDs."""
    settings = await db.settings.find_one({"_id": "buffer"})
    if not settings or not settings.get("api_key"):
        raise HTTPException(400, "Connect Buffer first — add your API key above.")
    api_key = settings["api_key"]

    org_query = "query { account { organizations { id name } } }"
    org_resp = await buffer_graphql(api_key, org_query, {})
    orgs = (((org_resp.get("data") or {}).get("account") or {}).get("organizations")) or []
    if not orgs:
        raise HTTPException(502, "Buffer returned no organizations for this account.")

    channels_query = """
    query GetChannels($organizationId: OrganizationId!) {
      channels(input: { organizationId: $organizationId }) {
        id
        name
        service
      }
    }
    """
    all_channels = []
    for org in orgs:
        resp = await buffer_graphql(api_key, channels_query, {"organizationId": org["id"]})
        chans = (resp.get("data") or {}).get("channels") or []
        for c in chans:
            all_channels.append({
                "channel_id": c["id"],
                "name": c.get("name", ""),
                "platform": c.get("service", ""),
                "organization": org.get("name", ""),
            })
    return all_channels


@api.post("/clips/{clip_id}/post-to-buffer")
async def post_clip_now(clip_id: str):
    """Manually queue one clip to all Buffer destinations configured on its channel, right now."""
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    if clip.get("render_status") != "done":
        raise HTTPException(400, "Clip isn't rendered yet.")
    settings = await db.settings.find_one({"_id": "buffer"})
    if not settings or not settings.get("api_key"):
        raise HTTPException(400, "Connect Buffer first (Settings).")
    ch = await db.channels.find_one({"id": clip["channel_id"]})
    dests = (ch or {}).get("buffer_channels") or []
    if not dests:
        raise HTTPException(400, "This channel has no Buffer destinations configured yet.")
    for dest in dests:
        await queue_clip_to_buffer(clip, dest, settings["api_key"])
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

    user = await helix_user(login)
    if not user:
        raise HTTPException(404, f"No Twitch channel found for '{login}'.")

    doc = {
        "id": str(uuid.uuid4()),
        "login": login,
        "display_name": user.get("display_name") or login,
        "twitch_user_id": user.get("id"),
        "avatar_url": user.get("profile_image_url") or "",
        "description": user.get("description") or "",
        "auto_clip": True,
        "caption_overlay": dict(DEFAULT_OVERLAY),
        "created_at": now_iso(),
    }
    await db.channels.insert_one(dict(doc))
    await snapshot_channels()
    return clean(doc)


@api.patch("/channels/{channel_id}")
async def update_channel(channel_id: str, body: ChannelUpdate):
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    if upd:
        await db.channels.update_one({"id": channel_id}, {"$set": upd})
    doc = await db.channels.find_one({"id": channel_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "Channel not found")
    await snapshot_channels()
    return doc


@api.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    await db.channels.delete_one({"id": channel_id})
    await db.clips.delete_many({"channel_id": channel_id})
    await snapshot_channels()
    return {"ok": True}


@api.get("/channels/{channel_id}/live")
async def channel_live(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    stream = await helix_stream(ch["twitch_user_id"])
    if not stream:
        return {"is_live": False, "viewer_count": 0, "title": "", "game_name": ""}
    return {
        "is_live": True,
        "viewer_count": stream.get("viewer_count", 0),
        "title": stream.get("title", ""),
        "game_name": stream.get("game_name", ""),
        "started_at": stream.get("started_at", ""),
    }


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
                "rendered": False,
            "render_status": "pending",
            "render_file": None,
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


async def auto_pull_channel(ch: dict, period: str = None):
    """Analyze the newest completed Twitch livestream for this channel."""
    try:
        vod = await helix_latest_vod(ch["twitch_user_id"])
        if not vod:
            return []
        return await _store_vod_clips(ch, [vod], 0)
    except Exception as e:
        logger.warning("auto_pull_channel %s: %s", ch.get("login"), e)
        return []


@api.post("/channels/{channel_id}/sync")
async def sync_clips(channel_id: str, body: SyncRequest):
    """Get every distinct hype moment we can identify in the latest completed livestream."""
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    vod = await helix_latest_vod(ch["twitch_user_id"])
    if not vod:
        raise HTTPException(404, f"{ch.get('display_name') or ch['login']} has no completed livestream available to clip.")
    stored = await _store_vod_clips(ch, [vod], 0)
    return {"fetched": 1, "stored": len(stored), "clips": stored, "vod_id": vod["id"]}


@api.get("/channels/{channel_id}/vods")
async def list_vods(channel_id: str):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    return await helix_latest_vods(ch["twitch_user_id"], 8)


@api.post("/channels/{channel_id}/pull-vod")
async def pull_vod(channel_id: str, days: int = Query(30)):
    ch = await db.channels.find_one({"id": channel_id})
    if not ch:
        raise HTTPException(404, "Channel not found")
    vod = await helix_latest_vod(ch["twitch_user_id"])
    if not vod:
        raise HTTPException(404, f"{ch.get('display_name') or ch['login']} has no completed livestream available to clip from.")
    stored = await _store_vod_clips(ch, [vod], 0)
    return {"fetched": 1, "stored": len(stored), "clips": stored}


@api.get("/clips")
async def get_clips(channel_id: str | None = None):
    q = {"channel_id": channel_id} if channel_id else {}
    clips = await db.clips.find(q, {"_id": 0}).sort("created_at", -1).to_list(2000)
    return clips


@api.get("/clips/{clip_id}")
async def get_clip(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    return clip


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


def _remove_clip_files(doc: dict):
    for k in ("render_file", "poster_file"):
        f = doc.get(k)
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass


@api.delete("/clips")
async def delete_all_clips(channel_id: str | None = None):
    """Delete every clip (optionally scoped to one channel) and clean up rendered files."""
    q = {"channel_id": channel_id} if channel_id else {}
    docs = await db.clips.find(q, {"render_file": 1, "poster_file": 1, "_id": 0}).to_list(5000)
    for d in docs:
        _remove_clip_files(d)
    res = await db.clips.delete_many(q)
    return {"deleted": res.deleted_count}


@api.delete("/clips/{clip_id}")
async def delete_clip(clip_id: str):
    doc = await db.clips.find_one({"id": clip_id}, {"render_file": 1, "poster_file": 1, "_id": 0})
    if doc:
        _remove_clip_files(doc)
    await db.clips.delete_one({"id": clip_id})
    return {"ok": True}


def clip_mp4_url(thumbnail_url: str) -> str | None:
    """Fallback: derive the MP4 from an older-format Twitch clip thumbnail URL."""
    if not thumbnail_url or "-preview" not in thumbnail_url:
        return None
    return thumbnail_url.split("-preview")[0] + ".mp4"


GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_CLIP_HASH = "36b89d2507fce29e5ca551df756d27c1cfe079e2609642b4390aa4c35796eb11"


async def resolve_clip_source(slug: str) -> str | None:
    """Resolve a Twitch clip's highest-quality downloadable MP4 URL via the public GQL API."""
    if not slug:
        return None
    body = [{
        "operationName": "VideoAccessToken_Clip",
        "variables": {"slug": slug},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_CLIP_HASH}},
    }]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://gql.twitch.tv/gql", json=body, headers={"Client-ID": GQL_CLIENT_ID})
        if r.status_code != 200:
            return None
        clip = r.json()[0]["data"]["clip"]
        if not clip or not clip.get("videoQualities"):
            return None
        qualities = sorted(clip["videoQualities"], key=lambda q: int(q.get("quality", "0")), reverse=True)
        token = clip["playbackAccessToken"]
        src = qualities[0]["sourceURL"]
        return f"{src}?sig={token['signature']}&token={quote(token['value'])}"
    except Exception as e:
        logger.warning(f"GQL clip resolve failed for {slug}: {e}")
        return None


FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _drawtext_y(position: str) -> str:
    if position == "top":
        return "150"
    if position == "center":
        return "(H-text_h)/2"
    return "H-text_h-200"  # bottom


async def render_vertical(src: str, out: str, caption: str, overlay: dict, workdir: str, ss=None, duration=None):
    """Convert a 16:9 clip to a 1080x1920 (9:16) video with a blurred fill and burned-in caption.
    `src` may be a local path OR a remote URL (ffmpeg reads it directly, overlapping fetch + encode).
    `ss`/`duration` let us seek into a long VOD/live stream and grab just the hype window."""
    # cheap blurred background: blur a tiny frame then upscale (visually identical, far faster)
    fc = (
        "[0:v]scale=384:216,boxblur=18:3,scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1[bg];"
        "[0:v]scale=1080:-2:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v1]"
    )
    out_label = "[v1]"
    cap = (caption or "").strip()
    if cap and os.path.exists(FONT_PATH):
        cap_file = os.path.join(workdir, "caption.txt")
        with open(cap_file, "w") as f:
            f.write(cap.upper())
        size = int(overlay.get("font_size", 28) or 28)
        fontsize = max(48, min(120, int(size * 2.6)))
        y = _drawtext_y(overlay.get("position", "bottom"))
        highlight = overlay.get("highlight", "#9146FF").lstrip("#")
        if not re.match(r"^[0-9a-fA-F]{6}$", highlight):
            highlight = "9146FF"
        fc += (
            f";[v1]drawtext=textfile={cap_file}:expansion=none:fontfile={FONT_PATH}:"
            f"fontcolor=white:fontsize={fontsize}:line_spacing=10:"
            f"box=1:boxcolor=black@0.55:boxborderw=26:"
            f"bordercolor=0x{highlight}@0.9:borderw=4:"
            f"x=(w-text_w)/2:y={y}[vout]"
        )
        out_label = "[vout]"

    args = [
        "nice", "-n", "19",
        "ffmpeg", "-y",
        "-user_agent", "Mozilla/5.0",
    ]
    if ss is not None:
        args += ["-ss", str(ss)]
    args += ["-i", src]
    if duration is not None:
        args += ["-t", str(duration)]
    args += [
        "-filter_complex", fc,
        "-map", out_label, "-map", "0:a?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k",
        "-r", "30",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-threads", "1",
        out,
    ]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=280)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("render timed out")
    if proc.returncode != 0:
        logger.warning(f"ffmpeg failed: {stderr.decode()[-500:]}")
        raise RuntimeError("ffmpeg failed")


RENDER_DIR = "/tmp/renders"
os.makedirs(RENDER_DIR, exist_ok=True)
RENDER_SEM = asyncio.Semaphore(3)


async def render_clip_to_store(clip_id: str):
    """Render a clip to a stored 9:16 file so it's ready to save instantly."""
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        return
    if clip.get("render_file") and os.path.exists(clip["render_file"]):
        await db.clips.update_one({"id": clip_id}, {"$set": {"rendered": True, "render_status": "done"}})
        return
    await db.clips.update_one({"id": clip_id}, {"$set": {"render_status": "rendering"}})
    workdir = tempfile.mkdtemp(prefix="clip_")
    try:
        out = os.path.join(RENDER_DIR, f"clip_{clip_id}.mp4")
        async with RENDER_SEM:
            st = clip.get("source_type")
            if st == "vod":
                pl = await vod_playlist_url(clip.get("vod_id", ""))
                if not pl:
                    raise RuntimeError("could not open the past broadcast video")
                await render_vertical(pl, out, clip.get("ai_caption", ""),
                                      clip.get("caption_overlay", DEFAULT_OVERLAY), workdir,
                                      ss=clip.get("start_seconds", 0),
                                      duration=clip.get("duration", CLIP_LEN_DEFAULT))
            else:
                # legacy fallback (old borrowed-clip records)
                mp4 = await resolve_clip_source(clip.get("twitch_clip_id", "")) or clip_mp4_url(clip.get("thumbnail_url", ""))
                if not mp4:
                    raise RuntimeError("no downloadable source")
                await render_vertical(mp4, out, clip.get("ai_caption", ""),
                                      clip.get("caption_overlay", DEFAULT_OVERLAY), workdir)
        poster = os.path.join(RENDER_DIR, f"clip_{clip_id}.jpg")
        has_poster = await _make_poster(out, poster)
        await db.clips.update_one({"id": clip_id}, {"$set": {
            "rendered": True, "render_status": "done", "render_file": out,
            "poster_file": poster if has_poster else None}})
    except Exception as e:
        logger.warning(f"render_clip_to_store {clip_id}: {e}")
        await db.clips.update_one({"id": clip_id}, {"$set": {"render_status": "error"}})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@api.post("/clips/{clip_id}/prepare")
async def prepare_clip(clip_id: str):
    """Kick off (or report) the background 9:16 render so Save becomes instant."""
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    status = clip.get("render_status")
    if status == "done" and clip.get("render_file") and os.path.exists(clip["render_file"]):
        return {"render_status": "done"}

    # A process restart can leave a clip stuck in rendering. Treat stale/error
    # states as retryable instead of leaving the UI on Preparing forever.
    await db.clips.update_one(
        {"id": clip_id},
        {"$set": {"render_status": "pending", "rendered": False}}
    )
    asyncio.create_task(render_clip_to_store(clip_id))
    return {"render_status": "rendering"}


@api.get("/clips/{clip_id}/video")
async def get_clip_video(clip_id: str):
    """Serve the ready-to-save 9:16 MP4 (renders on-demand if not cached yet)."""
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not (clip.get("render_file") and os.path.exists(clip["render_file"])):
        await render_clip_to_store(clip_id)
        clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    f = (clip or {}).get("render_file")
    if not f or not os.path.exists(f):
        raise HTTPException(500, "Could not render this clip. Please try another.")
    with open(f, "rb") as fh:
        data = fh.read()
    name = re.sub(r"[^a-zA-Z0-9]+", "_", (clip.get("ai_title") or clip.get("title") or "clip")).strip("_")[:50] or "clip"
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'inline; filename="{name}_9x16.mp4"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "Accept-Ranges": "none",
        },
    )


@api.get("/clips/{clip_id}/thumb")
async def get_clip_thumb(clip_id: str):
    """Serve the poster frame from the rendered 9:16 video (falls back to the source thumbnail)."""
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    poster = clip.get("poster_file")
    if poster and os.path.exists(poster):
        with open(poster, "rb") as fh:
            data = fh.read()
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    if clip.get("thumbnail_url"):
        return RedirectResponse(clip["thumbnail_url"])
    raise HTTPException(404, "No thumbnail yet")


async def _prune_old_renders():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    old = await db.render_jobs.find({"created_at": {"$lt": cutoff.isoformat()}}).to_list(500)
    for j in old:
        if j.get("file"):
            try:
                os.remove(j["file"])
            except OSError:
                pass
    await db.render_jobs.delete_many({"created_at": {"$lt": cutoff.isoformat()}})


async def _run_render_job(job_id: str, clip: dict):
    workdir = tempfile.mkdtemp(prefix="clip_")
    try:
        mp4 = await resolve_clip_source(clip.get("twitch_clip_id", "")) or clip_mp4_url(clip.get("thumbnail_url", ""))
        if not mp4:
            await db.render_jobs.update_one({"id": job_id}, {"$set": {
                "status": "error", "error": "Couldn't find a downloadable video file for this clip."}})
            return
        out = os.path.join(RENDER_DIR, f"{job_id}.mp4")
        await render_vertical(mp4, out, clip.get("ai_caption", ""), clip.get("caption_overlay", DEFAULT_OVERLAY), workdir)
        await db.render_jobs.update_one({"id": job_id}, {"$set": {"status": "done", "file": out}})
    except Exception as e:
        logger.warning(f"render job {job_id} failed: {e}")
        await db.render_jobs.update_one({"id": job_id}, {"$set": {
            "status": "error", "error": "Could not render this clip. Please try another."}})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@api.post("/clips/{clip_id}/download-jobs")
async def create_download_job(clip_id: str):
    clip = await db.clips.find_one({"id": clip_id}, {"_id": 0})
    if not clip:
        raise HTTPException(404, "Clip not found")
    await _prune_old_renders()
    job_id = uuid.uuid4().hex
    await db.render_jobs.insert_one({
        "id": job_id, "clip_id": clip_id, "status": "processing", "created_at": now_iso()})
    asyncio.create_task(_run_render_job(job_id, clip))
    return {"job_id": job_id}


@api.get("/download-jobs/{job_id}")
async def get_download_job(job_id: str):
    j = await db.render_jobs.find_one({"id": job_id}, {"_id": 0})
    if not j:
        raise HTTPException(404, "Job not found")
    return {"status": j["status"], "error": j.get("error")}


@api.get("/download-jobs/{job_id}/file")
async def get_download_job_file(job_id: str):
    j = await db.render_jobs.find_one({"id": job_id}, {"_id": 0})
    if not j or j.get("status") != "done" or not j.get("file") or not os.path.exists(j["file"]):
        raise HTTPException(404, "File not ready")
    clip = await db.clips.find_one({"id": j["clip_id"]}, {"_id": 0}) or {}
    name = re.sub(r"[^a-zA-Z0-9]+", "_", (clip.get("ai_title") or clip.get("title") or "clip")).strip("_")[:50] or "clip"
    with open(j["file"], "rb") as f:
        data = f.read()
    return Response(
        content=data,
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{name}_9x16.mp4"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "Accept-Ranges": "none",
        },
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
                   "scope": "clips:edit channel:manage:clips", "state": state})
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
    await snapshot_settings()
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


# -------- Background monitoring --------

async def auto_sync_loop():
    await asyncio.sleep(20)
    while True:
        try:
            chans = await db.channels.find({"auto_clip": True}).to_list(500)
            for ch in chans:
                await auto_pull_channel(ch)
        except Exception as e:
            logger.warning("auto_sync_loop: %s", e)
        await asyncio.sleep(int(os.environ.get("AUTO_VOD_CHECK_SECONDS", "300")))


async def render_worker():
    await asyncio.sleep(15)
    while True:
        try:
            clip = await db.clips.find_one({"render_status": "pending"}, {"_id": 0})
            if clip:
                asyncio.create_task(render_clip_to_store(clip["id"]))
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(8)
        except Exception as e:
            logger.warning("render_worker: %s", e)
            await asyncio.sleep(8)


LIVE_CAPTURE = {}


async def live_monitor_loop():
    """Keep watching configured channels and create real Twitch clips at strong chat spikes."""
    await asyncio.sleep(10)
    while True:
        try:
            chans = await db.channels.find({"auto_clip": True}).to_list(500)
            for ch in chans:
                try:
                    stream = await helix_stream(ch["twitch_user_id"])
                    if not stream:
                        continue
                    hype = await sample_hype(ch["login"], int(os.environ.get("LIVE_CHAT_SAMPLE_SECONDS", "8")))
                    if hype.get("hype_level", 0) < int(os.environ.get("LIVE_HYPE_THRESHOLD", "45")):
                        continue
                    now = asyncio.get_event_loop().time()
                    last = LIVE_CAPTURE.get(ch["id"], 0)
                    if now - last < int(os.environ.get("LIVE_CLIP_COOLDOWN", "75")):
                        continue
                    LIVE_CAPTURE[ch["id"]] = now
                    await create_live_twitch_clip(ch, hype, stream)
                except Exception as e:
                    logger.warning("live_monitor %s: %s", ch.get("login"), e)
        except Exception as e:
            logger.warning("live_monitor_loop: %s", e)
        await asyncio.sleep(int(os.environ.get("LIVE_CHECK_SECONDS", "20")))


@app.on_event("startup")
async def on_startup():
    await restore_from_disk()
    try:
        # Real mode only. Delete legacy demo data and never seed it.
        await db.channels.delete_many({"is_demo": True})
        await db.clips.delete_many({"is_demo": True})
        await db.channels.update_many({}, {"$unset": {"is_demo": ""}})
        await db.clips.update_many({}, {"$unset": {"is_demo": ""}})
        # Make the unique login index idempotent: drop any stale index, then remove
        # duplicate login docs (keep the oldest per login) BEFORE creating the unique index.
        try:
            existing = await db.channels.index_information()
            if "login_1" in existing:
                await db.channels.drop_index("login_1")
        except Exception as e:
            logger.warning(f"drop login_1 index skipped: {e}")
        seen = set()
        async for ch in db.channels.find({}, {"login": 1, "created_at": 1}).sort("created_at", 1):
            lg = ch.get("login")
            if lg in seen:
                await db.channels.delete_one({"_id": ch["_id"]})
            else:
                seen.add(lg)
        await db.channels.create_index("login", unique=True)
        await db.clips.create_index("twitch_clip_id")
        await db.clips.create_index([("channel_id", 1), ("vod_id", 1), ("start_seconds", 1)])
        await db.clips.update_many({"render_status": "rendering"}, {"$set": {"render_status": "pending"}})
        done = await db.clips.find({"render_status": "done"}, {"id": 1, "render_file": 1}).to_list(3000)
        for c in done:
            if not c.get("render_file") or not os.path.exists(c["render_file"]):
                await db.clips.update_one({"id": c["id"]}, {"$set": {"render_status": "pending", "rendered": False, "render_file": None}})
    except Exception as e:
        logger.error(f"startup index/seed block failed (continuing): {e}")
    if os.environ.get("ENABLE_AUTO_VOD", "true").lower() == "true":
        asyncio.create_task(auto_sync_loop())
    if os.environ.get("ENABLE_RENDER_WORKER", "true").lower() == "true":
        asyncio.create_task(render_worker())
    if os.environ.get("ENABLE_LIVE_MONITOR", "true").lower() == "true":
        asyncio.create_task(live_monitor_loop())
    if os.environ.get("ENABLE_BUFFER_WORKER", "true").lower() == "true":
        asyncio.create_task(buffer_sync_loop())


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