# StreamClip AI — PRD

## Original Problem Statement
Website where the user adds Twitch channel links; it auto-detects when those people are live and starts clipping them; then adds relevant text over the clips and generates a title with hashtags for posting. High quality videos, clipped at fun/good moments.

## User Choices
- Good-moment detection: chat activity spikes + fun/creepy moments
- Clip creation: Twitch official Clip API; clips as long as possible
- AI: GPT 5.4 (Emergent Universal LLM key)
- Separate each channel; choose clips/day (default 24)
- Also pull clips from past broadcasts on demand
- User is obtaining Twitch dev credentials (Client ID + Secret)

## Architecture
- Frontend: React (CRA/craco) + Tailwind + shadcn/ui + framer-motion. Dark neon Twitch dashboard (sidebar + bento canvas). Files: `src/pages/Dashboard.jsx`, `src/components/{Sidebar,ClipCard,SettingsDialog,PullVodDialog}.jsx`, `src/lib/api.js`.
- Backend: FastAPI + MongoDB (motor). All routes `/api`. `backend/server.py`.
- Twitch Helix via app access token (client credentials) for live status, users, clips, VODs. User OAuth (clips:edit) for live Create Clip. Anonymous IRC websocket for chat-hype sampling.
- AI: GPT 5.4 via emergentintegrations LlmChat for title/hashtags/on-clip caption.

## Personas
- Clip editor / content creator turning streamers' live moments into short-form posts (TikTok/Reels/Shorts).

## Core Requirements (static)
- Add/remove Twitch channels; per-channel isolation and settings.
- Live detection + viewer count. Chat-hype meter.
- Fetch best clips (ranked by views = good moments); pull from past broadcasts.
- AI title + hashtags + on-video caption; editable caption overlay (position/color/size).
- Copy title+hashtags for posting. Per-channel clips/day limit (default 24).

## Implemented (2026-06-19)
- Demo mode: `POST /api/demo/seed` (3 channels, 6 clips each) for instant AHA without Twitch creds.
- Channels CRUD, per-channel clips/day, auto_clip flag, caption overlay defaults.
- Live status (`/live`), chat-hype sampling via anonymous IRC (`/hype`).
- Clip sync (`/sync`, ranked by views), past-broadcast pull (`/pull-vod`), VOD list (`/vods`).
- Real GPT 5.4 AI generation (`/clips/{id}/generate`) + inline auto-gen on sync; caption/overlay PATCH.
- Twitch settings via UI (`/settings`, `/settings/twitch`), OAuth flow (`/auth/twitch/start|callback`), live Create Clip (`/clip-now`).
- Background auto-sync loop (every 15 min) pulls new clips for live auto-clip channels up to daily limit.
- Responsive UI incl. mobile drawer. All tests 100% pass (iteration_1).

## Backlog / Remaining
- P1: Real video download WITH burned-in captions (server-side ffmpeg render) — currently overlay is a live preview only.
- P1: Persistent per-channel live chat monitoring worker (continuous hype-spike auto-clip triggering) — currently on-demand sampling.
- P2: Direct posting/scheduling to TikTok/Reels/Shorts.
- P2: Multi-user auth + saved token refresh/encryption.
- P2: Custom clip length beyond Twitch's ~30-60s limit (self-recording pipeline).

## Notes / Limitations
- Twitch Create Clip requires user OAuth (clips:edit) + broadcaster live & clips enabled.
- On-video caption is a styled preview overlay, not yet burned into a downloadable file.
