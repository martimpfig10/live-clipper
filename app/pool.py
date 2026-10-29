"""Clip pool: dedupe on insert, hand out to accounts, keep N days planned."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from .config import Settings
from .db import DB, now

log = logging.getLogger(__name__)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def overlap_ratio(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    shorter = max(0.01, min(a1 - a0, b1 - b0))
    return inter / shorter


def insert_clips(db: DB, s: Settings, source_id: int, clips: list[dict]) -> int:
    cfg = s.section("clips")
    thr_overlap = float(cfg.get("dedupe_overlap", 0.4))
    thr_text = float(cfg.get("dedupe_text_jaccard", 0.6))
    same = [dict(r) for r in db.q("SELECT start, end FROM clips WHERE source_id=?", (source_id,))]
    others = [dict(r) for r in db.q(
        "SELECT text FROM clips WHERE source_id<>? ORDER BY id DESC LIMIT 1000", (source_id,)
    )]
    added = 0
    for c in sorted(clips, key=lambda c: -c.get("score", 0)):
        if any(overlap_ratio(c["start"], c["end"], o["start"], o["end"]) > thr_overlap for o in same):
            continue
        if c.get("text") and any(jaccard(c["text"], o["text"]) >= thr_text for o in others):
            continue
        db.exec(
            """INSERT INTO clips(source_id, start, end, hook, reason, category, dialect, main_speaker,
                                 score, text, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?, 'pool', ?)""",
            (source_id, c["start"], c["end"], c["hook"], c.get("reason", ""), c.get("category", ""),
             c.get("dialect", "pt-PT"), c.get("main_speaker", "host"), c.get("score", 0),
             c.get("text", ""), now()),
        )
        same.append({"start": c["start"], "end": c["end"]})
        added += 1
    return added


def today(s: Settings) -> date:
    return datetime.now(s.tz).date()


def _pick(pool: list[dict], last: dict | None) -> dict | None:
    """Best clip, nudged away from repeating the account's previous source/category."""
    if not pool:
        return None

    def score(c):
        sc = c["score"]
        if last:
            if c["source_id"] == last.get("source_id"):
                sc -= 0.75
            if c["category"] and c["category"] == last.get("category"):
                sc -= 1.25
        return sc

    return max(pool, key=score)


def release_expired(db: DB, s: Settings) -> int:
    """Posts from past days that were never posted give their clip back to the pool."""
    t = today(s).isoformat()
    rows = db.q("SELECT id, clip_id FROM posts WHERE day < ? AND status IN ('draft','ready')", (t,))
    for r in rows:
        db.exec("UPDATE posts SET status='expired', updated_at=? WHERE id=?", (now(), r["id"]))
        db.exec("UPDATE clips SET status='pool' WHERE id=? AND status='assigned'", (r["clip_id"],))
    return len(rows)


def plan(db: DB, s: Settings, on_new_post=None) -> int:
    """Fill every empty (day, slot, account) for the next `days_ahead` days.

    Fills nearest day first, slot by slot, round-robin across accounts — so if the pool is
    short, tomorrow is complete before the day after starts.
    """
    release_expired(db, s)
    days = int(s.section("planning").get("days_ahead", 3))
    crop_default = s.raw.get("crop_presets", {}).get(s.raw.get("default_crop", "center"), {})
    pool = [dict(r) for r in db.q("SELECT * FROM clips WHERE status='pool'")]
    created = 0
    max_slots = max((len(a.slots) for a in s.accounts), default=0)
    start_day = today(s)
    for d in range(days):
        day = (start_day + timedelta(days=d)).isoformat()
        for slot_i in range(max_slots):
            for acc in s.accounts:
                if slot_i >= len(acc.slots):
                    continue
                slot = acc.slots[slot_i]
                taken = db.one(
                    "SELECT id FROM posts WHERE account=? AND day=? AND slot=? AND status<>'rejected'",
                    (acc.name, day, slot),
                )
                if taken:
                    continue
                last_row = db.one(
                    """SELECT c.source_id, c.category FROM posts p JOIN clips c ON c.id=p.clip_id
                       WHERE p.account=? AND p.status<>'rejected' ORDER BY p.day DESC, p.slot DESC LIMIT 1""",
                    (acc.name,),
                )
                clip = _pick(pool, dict(last_row) if last_row else None)
                if clip is None:
                    return created
                pool.remove(clip)
                t = now()
                with db.tx() as c:
                    c.execute("UPDATE clips SET status='assigned' WHERE id=?", (clip["id"],))
                    cur = c.execute(
                        """INSERT INTO posts(clip_id, account, day, slot, status, hook, crop,
                                             created_at, updated_at)
                           VALUES (?,?,?,?, 'draft', ?, ?, ?, ?)""",
                        (clip["id"], acc.name, day, slot, clip["hook"], json.dumps(crop_default), t, t),
                    )
                    post_id = cur.lastrowid
                created += 1
                if on_new_post:
                    on_new_post(post_id)
    return created


def reject(db: DB, post_id: int) -> None:
    p = db.one("SELECT clip_id FROM posts WHERE id=?", (post_id,))
    if not p:
        return
    db.exec("UPDATE posts SET status='rejected', updated_at=? WHERE id=?", (now(), post_id))
    db.exec("UPDATE clips SET status='rejected' WHERE id=?", (p["clip_id"],))


def mark_posted(db: DB, post_id: int, posted: bool = True) -> None:
    p = db.one("SELECT clip_id FROM posts WHERE id=?", (post_id,))
    if not p:
        return
    db.exec("UPDATE posts SET status=?, updated_at=? WHERE id=?",
            ("posted" if posted else "ready", now(), post_id))
    db.exec("UPDATE clips SET status=? WHERE id=?", ("posted" if posted else "assigned", p["clip_id"]))


def runway(db: DB, s: Settings) -> dict:
    per_day = sum(len(a.slots) for a in s.accounts) or 1
    n = db.one("SELECT COUNT(*) n FROM clips WHERE status='pool'")["n"]
    return {"pool": n, "per_day": per_day, "days": round(n / per_day, 1)}
