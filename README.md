# live-clipper

Turns **@wazeoficial's TikTok lives** into **8 different clips per day** (4 accounts × 2), ready to review
and post. Each video is the live clip on top, a photo you choose underneath, and a hook title. The caption
is already written: you copy it, download the video and post.

```
live ends ─► recorder saves it ─► transcribe (Whisper) ─► LLM picks moments ─► clip pool
                                                                                   │
review app (phone) ◄── caption + render per post ◄── 2 clips/account/day planned ◄─┘
   │  swap photo · crop · hook · copy caption · download · mark posted
   ▼
TikTok Studio (schedule)
```

## What it does
- **Records lives automatically.** Uses [tiktok-live-recorder](https://github.com/Michele0303/tiktok-live-recorder) and checks every 5 min.
  You can also **upload** a recording in the app, or drop a file into `data/inbox/`.
- **Transcribes** with Whisper large-v3, through Groq's API (free tier).
- **Finds 15–75 s moments** with an LLM. It also gets a loudness signal, so laughs and shouting stand out.
  It writes a hook in the speaker's dialect (pt-PT or pt-BR).
- **Pool, not angles.** Every clip goes into one shared pool. Each clip is used **once**, on one account only.
  Overlapping cuts of the same moment are merged.
- **Plans 3 days ahead.** It fills the nearest day first. **Reject** a clip and the next one from the pool replaces it.
  Clips planned for a past day that were never posted go back into the pool.
- **Captions** are written in pt-PT or pt-BR to match the clip, with mixed PT/BR hashtags and a credit line.
- **Renders** a 1080×1920 H.264 file. The crop has presets for split-screen guest lives (guest left/right, zoom).

## Run it (VPS)
Any small Linux VPS with Docker works. 2 vCPU and 4 GB RAM is plenty.

```bash
git clone https://github.com/martimpfig10/live-clipper && cd live-clipper
cp .env.example .env     # set GROQ_API_KEY and APP_PASSWORD
docker compose up -d --build
```
Open `http://<server-ip>:8000` and log in with `APP_USER` / `APP_PASSWORD`.

> **HTTPS (recommended):** put Caddy in front, or use Tailscale. The "Copy caption" button also works on plain
> http (it falls back to a different copy method), but https is cleaner.

### Keys and cost
| What | Default | Cost |
|---|---|---|
| Transcription | Groq `whisper-large-v3` | free tier, then ~$0.11 per hour of audio |
| LLM | Groq `openai/gpt-oss-120b` | free tier (rate-limited; the app retries automatically) |
| Server | your VPS | a few € per month |

You can switch the LLM to Claude, OpenRouter, Ollama or any other OpenAI-compatible endpoint by changing
3 lines in `.env`. Note that a Claude **Pro** subscription doesn't include API access; Claude needs an API key from console.anthropic.com.

## Daily routine
1. Open the app on your phone. **Today** shows each account with its 2 posts.
2. For each post, tap **Edit** if you want to change the photo, crop or hook. The video re-renders in a few seconds.
3. Tap **Copy caption**, then **Download**.
4. In TikTok Studio, upload the video, paste the caption, set the time shown on the card and schedule it.
5. Tap **Mark posted**.

Tip: schedule up to 10 days ahead in TikTok Studio. Two sessions a week covers everything.

## Config (`config.yaml`)
- `accounts`: account names and their daily time slots. Add or remove accounts here.
- `clips`: min/max length, minimum score, dedupe thresholds.
- `render`: how much of the height the live clip takes, hook font size and position (`seam` or `top`).
- `crop_presets`: the crop buttons shown in the review screen.
- `captions.base_hashtags`, `creator.credit_line`.

## Recorder notes
- Recordings land in `data/recordings/`. The worker only processes a file once it has stopped growing for 2 minutes.
- If TikTok starts blocking anonymous requests, add `cookies.json` following the recorder's
  [guide](https://github.com/Michele0303/tiktok-live-recorder/blob/main/docs/GUIDE.md#how-to-set-cookies) and
  uncomment the volume line in `docker-compose.yml`.
- Get the creator's OK in writing for recording and clipping his lives.

## Dev
```bash
pip install -r requirements-dev.txt
pytest                         # unit + full pipeline test (APIs faked, real ffmpeg renders)
DATA_DIR=./data APP_PASSWORD=dev uvicorn app.web:app --reload    # app
DATA_DIR=./data python -m app.worker                              # pipeline
```

Layout: `app/worker.py` runs the pipeline loop · `transcribe.py` · `highlights.py` · `pool.py` handles
dedupe and planning · `captions.py` · `render.py` · `web.py` + `templates/` are the review app.
