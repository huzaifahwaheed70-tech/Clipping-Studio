# StreamClip AI — Product Requirements

## Original Problem Statement
Add Twitch channels → the app AUTOMATICALLY records the streamers' own content, cuts the fun/hype
moments, overlays relevant text/captions, and gives an AI title + hashtags. High-quality 9:16
vertical videos (blurred background + centered clip + burned-in captions) that save directly to the
phone. No Twitch login. Open access (no user accounts).

### Critical user intent (clarified June 2026)
The user does NOT want Twitch's pre-made clips handed to them. The app must **make the clips itself**:
record the streamer's own video feed and cut the moments where the live chat blows up.
- Live streams → record the live feed when chat gets hyped.
- Offline streamers → pull their **past broadcasts (VODs)** and cut ~24 hype moments each.
- Full delete controls: delete one clip, delete all clips for a channel, delete all clips everywhere.

## Architecture
- Frontend: React + Tailwind + Shadcn (`/app/frontend`). Dashboard, Sidebar, ClipCard, PullVodDialog.
- Backend: FastAPI + Motor/MongoDB (`/app/backend/server.py`, ~1300 lines).
- Video: ffmpeg (server-side), CPU-throttled: `nice -n 19`, `-threads 1`, `asyncio.Semaphore(1)`.
- AI: GPT-5.4 via Emergent LLM Key (titles/hashtags/captions).
- Twitch: 100% public GraphQL (no OAuth/login). Client-ID `kimne78kx3ncx6brgo4mv6wki5h1ko`.

## Self-recording engine (engine_v2 — June 2026)
- `gql_list_vods()` — a channel's ARCHIVE VODs via public GQL.
- `vod_playlist_url()` / `live_playlist_url()` — HLS via videoPlaybackAccessToken/streamPlaybackAccessToken + usher.
- `vod_chat_density()` + `find_hype_moments_in_vod()` — sample VOD chat replay; densest windows = hype moments. Clip length scales with hype (18–45s).
- `_store_vod_clips()` — creates clip docs (`source_type='vod'`, `vod_id`, `start_seconds`, `duration`), dedupes by offset, generates AI copy.
- `render_vertical(..., ss, duration)` — ffmpeg seeks into the VOD/live feed and outputs 1080x1920 blurred+captioned H.264/AAC MP4, 30fps.
- `render_clip_to_store()` — renders + extracts a poster frame.
- `capture_live_moment()` + `live_monitor_loop()` — when a channel is live AND chat hype ≥55, record the live feed (20–60s) and render. (Only active while a channel is live.)
- One-time `engine_v2` startup migration wipes old borrowed Twitch clips and re-records from VODs.

## Key API endpoints
- `GET/POST/PATCH/DELETE /api/channels[/{id}]`
- `POST /api/channels/{id}/sync` (body `{period_days:int}`), `POST /api/channels/{id}/pull-vod`, `GET /api/channels/{id}/vods`
- `GET /api/channels/{id}/live`, `GET /api/channels/{id}/hype`
- `GET /api/clips[?channel_id=]`, `GET /api/clips/{id}`, `PATCH /api/clips/{id}`, `POST /api/clips/{id}/generate`
- `POST /api/clips/{id}/prepare` (bg render), `GET /api/clips/{id}/video` (9:16 MP4, attachment), `GET /api/clips/{id}/thumb` (poster)
- `DELETE /api/clips/{id}` (single), `DELETE /api/clips[?channel_id=]` (all / per-channel)

## DB schema (clips)
`{id, channel_id, channel_login, source_type('vod'|'live'), vod_id, start_seconds, duration,
twitch_clip_id('vod-<id>-<off>'|'live-<uuid>'), title, url, thumbnail_url, poster_file,
hype_type, hype_density, game_name, ai_title, ai_hashtags[], ai_caption, caption_overlay,
render_status('pending'|'rendering'|'done'|'error'), render_file, is_demo, created_at}`

## Status (June 2026)
Verified via testing_agent iteration_10: backend 11/12 pass, frontend 100%. Real VOD-recorded 9:16
clips, poster thumbs, inline video playback, Save-to-phone via navigator.share, and all delete flows working.
Channel IDs: xqc=476dc540-1e59-4e60-baee-ee6395832863, kaicenat=2e4bf3a5-fa1e-4823-997f-9497a13fc228,
pokimane=a9defff4-626f-429f-a606-fcc8f5c788cd.

## Backlog / Future
- P1: Persist clips collection to disk (channels already persisted to `/app/.appdata`).
- P2: crop-to-fill vs blurred-background toggle; streamer-name watermark.
- P2: `up_to_date` flag on sync response (frontend now toasts "already have latest" when stored=0).
- P3: Live capture is built but only testable when a channel is actually live.
- P3: Stream `get_clip_video` instead of reading whole file into memory (fine at 30fps sizes now).

## Notes for next agent
- CRITICAL CPU RULE (root cause of recurring Cloudflare "unparseable/empty response" on add/any request): the pod is HARD-CAPPED at 2 CPUs (`cpu.max = 200000 100000`). Rendering MUST stay throttled: `RENDER_SEM = asyncio.Semaphore(1)`, `render_worker` must `await render_clip_to_store(...)` serially (NEVER `create_task`), and every ffmpeg is launched via `taskset -c $RENDER_CPUS(=0) nice -n 19 ...` with `-threads 1`, `-filter_complex_threads 1`, and `-x264-params threads=1:lookahead-threads=1:sliced-threads=0`. This pins one render to ≤1 CPU, leaving a full CPU for uvicorn (verified: API ~0.14s during render). Do NOT raise the semaphore or drop taskset — it instantly starves the web server.
- NO Twitch OAuth for the original flow. NOTE: a later refactor made add_channel/live/vods use the Helix API (requires Twitch client_id/secret in Settings); VOD playback still uses public GQL.
- Test /video by prepare→poll render_status=done→GET /video (avoids CF 100s edge timeout).
- The 3 channels are real (is_demo removed); demo seed concept is deprecated.
