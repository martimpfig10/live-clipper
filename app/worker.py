"""Background worker: ingest -> transcribe -> highlights -> pool -> plan -> captions/renders."""
from __future__ import annotations

import json
import logging
import time
import traceback
from pathlib import Path

import numpy as np

from . import pool
from .captions import write_caption
from .config import Settings, get_settings
from .db import DB, now
from .highlights import find_highlights
from .media import extract_frame, loudness_per_second, probe
from .render import Crop, render_post
from .transcribe import transcribe_source

log = logging.getLogger("worker")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".flv", ".m4v", ".ts"}
STABLE_SECONDS = 120          # a file must stop growing for this long before we touch it
PARTIAL_GRACE = 30 * 60       # recorder's "_flv.mp4" temp files: only if abandoned this long


def scan(db: DB, s: Settings) -> list[int]:
    """Register new, finished video files from the inbox and recordings folders."""
    new_ids = []
    t = time.time()
    for folder, kind in ((s.inbox, "upload"), (s.recordings, "recording")):
        for p in sorted(folder.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXT or p.name.startswith("."):
                continue
            age = t - p.stat().st_mtime
            if age < STABLE_SECONDS:
                continue
            if p.name.endswith("_flv.mp4"):  # tiktok-live-recorder still converting / or crashed
                final = p.with_name(p.name.replace("_flv.mp4", ".mp4"))
                if final.exists() or age < PARTIAL_GRACE:
                    continue
            if db.one("SELECT id FROM sources WHERE path=?", (str(p),)):
                continue
            sid = db.exec(
                "INSERT INTO sources(path, filename, kind, status, created_at) VALUES (?,?,?, 'new', ?)",
                (str(p), p.name, kind, now()),
            )
            db.enqueue("process_source", source_id=sid)
            new_ids.append(sid)
            log.info("new source #%d %s", sid, p.name)
    return new_ids


def _stage(db: DB, sid: int, stage: str) -> None:
    db.exec("UPDATE sources SET stage=? WHERE id=?", (stage, sid))


def process_source(db: DB, s: Settings, source_id: int) -> int:
    src = db.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not src or src["status"] == "done":
        return 0
    db.exec("UPDATE sources SET status='processing', error='' WHERE id=?", (source_id,))
    path = Path(src["path"])
    work = s.work / f"source_{source_id}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        _stage(db, source_id, "probing")
        info = probe(path)
        db.exec("UPDATE sources SET duration=?, width=?, height=? WHERE id=?",
                (info["duration"], info["width"], info["height"], source_id))
        if not info["has_audio"]:
            raise RuntimeError("video has no audio track")

        _stage(db, source_id, "transcribing")
        transcript = transcribe_source(s, path, info["duration"], work)

        _stage(db, source_id, "measuring loudness")
        loud_cache = work / "loudness.npy"
        if loud_cache.exists():
            loud = np.load(loud_cache)
        else:
            loud = loudness_per_second(path)
            np.save(loud_cache, loud)

        _stage(db, source_id, "finding highlights")
        clips = find_highlights(s, transcript, loud, info["duration"])
        (work / "highlights.json").write_text(json.dumps(clips, ensure_ascii=False, indent=1))
        added = pool.insert_clips(db, s, source_id, clips)

        db.exec("UPDATE sources SET status='done', stage=? WHERE id=?",
                (f"{added} clips added ({len(clips)} found)", source_id))
        log.info("source #%d: %d clips added", source_id, added)
        db.enqueue("plan")
        return added
    except Exception as e:
        db.exec("UPDATE sources SET status='error', error=? WHERE id=?", (str(e)[:2000], source_id))
        raise


def _post_bundle(db: DB, post_id: int):
    return db.one(
        """SELECT p.*, c.start, c.end, c.text, c.dialect, c.source_id,
                  s.path AS src_path, s.width AS src_w, s.height AS src_h,
                  ph.path AS photo_path
           FROM posts p JOIN clips c ON c.id=p.clip_id JOIN sources s ON s.id=c.source_id
           LEFT JOIN photos ph ON ph.id=p.photo_id WHERE p.id=?""",
        (post_id,),
    )


def default_photo(db: DB, s: Settings, post_id: int) -> None:
    """Suggest a still from the clip as the starting photo (you swap it in review)."""
    b = _post_bundle(db, post_id)
    if not b or b["photo_id"]:
        return
    out = s.photos / f"frame_clip{b['clip_id']}.jpg"
    if not out.exists():
        extract_frame(b["src_path"], (b["start"] + b["end"]) / 2, out)
    row = db.one("SELECT id FROM photos WHERE path=?", (str(out),))
    pid = row["id"] if row else db.exec(
        "INSERT INTO photos(path, kind, clip_id, created_at) VALUES (?, 'frame', ?, ?)",
        (str(out), b["clip_id"], now()),
    )
    db.exec("UPDATE posts SET photo_id=? WHERE id=?", (pid, post_id))


def do_caption(db: DB, s: Settings, post_id: int) -> None:
    b = _post_bundle(db, post_id)
    if not b:
        return
    cap = write_caption(s, b["hook"], b["text"], b["dialect"])
    db.exec("UPDATE posts SET caption=?, updated_at=? WHERE id=?", (cap, now(), post_id))


def do_render(db: DB, s: Settings, post_id: int) -> None:
    b = _post_bundle(db, post_id)
    if not b:
        return
    db.exec("UPDATE posts SET render_status='rendering', render_error='' WHERE id=?", (post_id,))
    try:
        crop = Crop.from_dict(json.loads(b["crop"] or "{}"))
        out = s.renders / f"post_{post_id}.mp4"
        render_post(s, Path(b["src_path"]), b["src_w"], b["src_h"], b["start"], b["end"],
                    b["hook"], Path(b["photo_path"]) if b["photo_path"] else None, crop, out)
        db.exec(
            """UPDATE posts SET render_path=?, render_status='done',
                      status=CASE WHEN status='draft' THEN 'ready' ELSE status END, updated_at=?
               WHERE id=?""",
            (str(out), now(), post_id),
        )
    except Exception as e:
        db.exec("UPDATE posts SET render_status='error', render_error=? WHERE id=?", (str(e)[:2000], post_id))
        raise


def on_new_post(db: DB, s: Settings):
    def _cb(post_id: int):
        try:
            default_photo(db, s, post_id)
        except Exception:
            log.exception("default photo failed for post %d", post_id)
        db.enqueue("caption", post_id=post_id)
        db.exec("UPDATE posts SET render_status='queued' WHERE id=?", (post_id,))
        db.enqueue("render", post_id=post_id)
    return _cb


def run_job(db: DB, s: Settings, job) -> None:
    payload = json.loads(job["payload"] or "{}")
    kind = job["kind"]
    if kind == "process_source":
        process_source(db, s, payload["source_id"])
    elif kind == "plan":
        n = pool.plan(db, s, on_new_post=on_new_post(db, s))
        if n:
            log.info("planned %d new posts", n)
    elif kind == "caption":
        do_caption(db, s, payload["post_id"])
    elif kind == "render":
        do_render(db, s, payload["post_id"])
    else:
        raise ValueError(f"unknown job kind {kind}")


def run_once(db: DB, s: Settings) -> bool:
    job = db.next_job()
    if not job:
        return False
    try:
        run_job(db, s, job)
        db.finish_job(job["id"])
    except Exception as e:
        log.error("job %s #%d failed: %s", job["kind"], job["id"], e)
        log.debug(traceback.format_exc())
        db.finish_job(job["id"], error=f"{e}")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    db = DB(s.db_path)
    db.reset_running_jobs()
    log.info("worker started; watching %s and %s", s.inbox, s.recordings)
    last_scan = 0.0
    while True:
        if time.time() - last_scan > 30:
            last_scan = time.time()
            try:
                scan(db, s)
                db.enqueue("plan")
            except Exception:
                log.exception("scan failed")
        if not run_once(db, s):
            time.sleep(2)


if __name__ == "__main__":
    main()
