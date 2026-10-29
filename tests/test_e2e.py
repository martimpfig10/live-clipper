"""Full pipeline on a synthetic 3-minute 'live', with the STT + LLM APIs faked."""
import base64
import importlib
import shutil

from app import captions, highlights, transcribe
from app.media import probe
from tests.conftest import make_video


def fake_stt(s, path):
    # one word every 0.5s, a segment every 10s
    words = [{"word": f"palavra{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(1200)]
    segs = [{"start": i * 10.0, "end": i * 10.0 + 9.5, "text": " ".join(w["word"] for w in words[i * 20:(i + 1) * 20])}
            for i in range(60)]
    return {"words": words, "segments": segs}


def fake_llm(s, system, user, **kw):
    if '"clips": [' in system:
        return {"clips": [
            {"start": 5, "end": 35, "hook": "Ele não esperava esta resposta", "category": "engraçado",
             "main_speaker": "host", "dialect": "pt-PT", "score": 9, "reason": "remate forte"},
            {"start": 60, "end": 95, "hook": "O convidado brasileiro ficou sem palavras", "category": "reação",
             "main_speaker": "guest", "dialect": "pt-BR", "score": 8, "reason": "reação"},
            {"start": 120, "end": 160, "hook": "Conselho que ninguém te dá", "category": "conselho",
             "main_speaker": "host", "dialect": "pt-PT", "score": 7, "reason": "valor"},
            {"start": 10, "end": 30, "hook": "duplicado", "score": 6},  # overlaps first
            {"start": 170, "end": 172, "hook": "curto", "score": 9},     # too short
        ]}
    return {"description": "Ninguém estava à espera disto 😂 E tu, o que fazias?", "hashtags": ["#portugal", "#brasil", "#humor"]}


def test_pipeline_and_review_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setattr(transcribe, "_transcribe_file", fake_stt)
    monkeypatch.setattr(highlights, "chat_json", fake_llm)
    monkeypatch.setattr(captions, "chat_json", fake_llm)

    from app import config
    config.get_settings.cache_clear()
    s = config.get_settings()
    from app import worker
    from app.db import DB

    db = DB(s.db_path)
    vid = make_video(tmp_path / "src.mp4", seconds=180)
    dest = s.recordings / "TK_wazeoficial_2026.10.29_21-00-00.mp4"
    shutil.copy(vid, dest)
    # recorder temp file must be ignored
    shutil.copy(vid, s.recordings / "TK_wazeoficial_2026.10.29_22-00-00_flv.mp4")
    import os, time
    old = time.time() - 600
    for p in s.recordings.iterdir():
        os.utime(p, (old, old))

    assert len(worker.scan(db, s)) == 1
    while worker.run_once(db, s):
        pass

    src = db.one("SELECT * FROM sources")
    assert src["status"] == "done", src["error"]
    clips = db.q("SELECT * FROM clips")
    assert len(clips) == 3
    posts = db.q("SELECT * FROM posts")
    assert len(posts) == 3  # pool of 3 -> first 3 slots of today
    for p in posts:
        assert p["render_status"] == "done", p["render_error"]
        assert p["caption"].startswith("Ninguém") and "#wazeoficial" in p["caption"] and "@wazeoficial" in p["caption"]
        info = probe(p["render_path"])
        assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
        assert abs(info["duration"] - (db.one("SELECT end-start d FROM clips WHERE id=?", (p["clip_id"],))["d"])) < 0.6
    br = db.one("SELECT p.caption FROM posts p JOIN clips c ON c.id=p.clip_id WHERE c.dialect='pt-BR'")
    assert br is not None

    # --- review app ---
    import app.web as web
    importlib.reload(web)
    from fastapi.testclient import TestClient

    c = TestClient(web.app)
    assert c.get("/").status_code == 401
    h = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    r = c.get("/", headers=h)
    assert r.status_code == 200 and "conta1" in r.text and "Copy caption" in r.text
    pid = posts[0]["id"]
    r = c.get(f"/post/{pid}", headers=h)
    assert r.status_code == 200 and "Reject" in r.text
    assert c.get(f"/media/post/{pid}?download=1", headers=h).headers["content-disposition"].startswith("attachment")

    # swap photo by upload -> re-render queued
    from PIL import Image
    img = tmp_path / "p.jpg"
    Image.new("RGB", (800, 1000), (200, 40, 80)).save(img)
    with open(img, "rb") as f:
        r = c.post(f"/post/{pid}/photo", files={"file": ("p.jpg", f, "image/jpeg")}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT render_status FROM posts WHERE id=?", (pid,))["render_status"] == "queued"
    c.post(f"/post/{pid}/crop", data={"preset": "guest_left"}, headers=h, follow_redirects=False)
    c.post(f"/post/{pid}/hook", data={"hook": "Novo título"}, headers=h, follow_redirects=False)
    while worker.run_once(db, s):
        pass
    p = db.one("SELECT * FROM posts WHERE id=?", (pid,))
    assert p["render_status"] == "done" and p["hook"] == "Novo título"

    # reject -> slot refilled? pool is empty, so slot stays empty but clip is rejected
    c.post(f"/post/{pid}/reject", headers=h, follow_redirects=False)
    assert db.one("SELECT status FROM posts WHERE id=?", (pid,))["status"] == "rejected"

    # mark posted toggles
    pid2 = posts[1]["id"]
    c.post(f"/post/{pid2}/posted", headers=h, follow_redirects=False)
    assert db.one("SELECT status FROM posts WHERE id=?", (pid2,))["status"] == "posted"
    assert c.get("/sources", headers=h).status_code == 200
    assert c.get("/photos", headers=h).status_code == 200
