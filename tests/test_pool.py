from datetime import timedelta

from app import pool
from app.db import now


def add_source(db, n=1):
    return db.exec("INSERT INTO sources(path, filename, status, created_at) VALUES (?,?, 'done', ?)",
                   (f"/x/{n}.mp4", f"{n}.mp4", now()))


def fake_clips(k, offset=0, cat="engraçado"):
    return [{"start": offset + i * 100, "end": offset + i * 100 + 30, "hook": f"hook {i}", "score": 8 - (i % 3),
             "category": cat if i % 2 else "história", "text": f"texto unico numero {offset + i} palavra{offset + i}"}
            for i in range(k)]


def test_insert_dedupes_overlaps(db, settings):
    sid = add_source(db)
    clips = fake_clips(3) + [{"start": 5, "end": 33, "hook": "dup", "score": 5, "text": "outra coisa"}]
    assert pool.insert_clips(db, settings, sid, clips) == 3


def test_insert_dedupes_text_across_sources(db, settings):
    s1, s2 = add_source(db, 1), add_source(db, 2)
    pool.insert_clips(db, settings, s1, [{"start": 0, "end": 30, "hook": "a", "score": 8,
                                          "text": "ele contou a historia do carro avariado na autoestrada"}])
    added = pool.insert_clips(db, settings, s2, [{"start": 0, "end": 30, "hook": "b", "score": 8,
                                                  "text": "ele contou a historia do carro avariado na autoestrada ontem"}])
    assert added == 0


def test_plan_fills_all_slots_one_clip_one_account(db, settings):
    sid = add_source(db)
    pool.insert_clips(db, settings, sid, fake_clips(30))
    n = pool.plan(db, settings)
    assert n == 4 * 2 * 3  # 4 accounts x 2 posts x 3 days
    rows = db.q("SELECT clip_id, account, day FROM posts")
    assert len({r["clip_id"] for r in rows}) == len(rows)  # never the same clip twice
    for acc in settings.accounts:
        for d in {r["day"] for r in rows}:
            assert sum(1 for r in rows if r["account"] == acc.name and r["day"] == d) == 2
    assert pool.plan(db, settings) == 0  # idempotent


def test_short_pool_fills_nearest_day_first(db, settings):
    sid = add_source(db)
    pool.insert_clips(db, settings, sid, fake_clips(10))
    pool.plan(db, settings)
    today = pool.today(settings).isoformat()
    assert db.one("SELECT COUNT(*) n FROM posts WHERE day=?", (today,))["n"] == 8
    assert db.one("SELECT COUNT(*) n FROM posts WHERE day<>?", (today,))["n"] == 2


def test_reject_replaces_from_pool(db, settings):
    sid = add_source(db)
    pool.insert_clips(db, settings, sid, fake_clips(30))
    pool.plan(db, settings)
    p = db.one("SELECT * FROM posts ORDER BY id LIMIT 1")
    pool.reject(db, p["id"])
    assert pool.plan(db, settings) == 1
    new = db.one("SELECT * FROM posts WHERE account=? AND day=? AND slot=? AND status<>'rejected'",
                 (p["account"], p["day"], p["slot"]))
    assert new and new["clip_id"] != p["clip_id"]
    assert db.one("SELECT status FROM clips WHERE id=?", (p["clip_id"],))["status"] == "rejected"


def test_unposted_past_posts_return_to_pool(db, settings):
    sid = add_source(db)
    pool.insert_clips(db, settings, sid, fake_clips(1))
    cid = db.one("SELECT id FROM clips")["id"]
    yesterday = (pool.today(settings) - timedelta(days=1)).isoformat()
    db.exec("UPDATE clips SET status='assigned'")
    db.exec("INSERT INTO posts(clip_id, account, day, slot, hook, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (cid, "conta1", yesterday, "13:00", "h", now(), now()))
    assert pool.release_expired(db, settings) == 1
    assert db.one("SELECT status FROM clips WHERE id=?", (cid,))["status"] == "pool"
