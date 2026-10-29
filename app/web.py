"""Review app: see each day's posts per account, swap photo/crop/hook, copy caption, download."""
from __future__ import annotations

import json
import re
import secrets
import shutil
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pool
from .config import get_settings
from .db import DB, now

HERE = Path(__file__).parent
settings = get_settings()
db = DB(settings.db_path)
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(creds.username, settings.app_user)
    ok_pass = bool(settings.app_password) and secrets.compare_digest(creds.password, settings.app_password)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username


app = FastAPI(title="live-clipper", dependencies=[Depends(auth)])
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def day_label(d: str) -> str:
    dd = date.fromisoformat(d)
    t = pool.today(settings)
    if dd == t:
        return "Today"
    if dd == t + timedelta(days=1):
        return "Tomorrow"
    return f"{WEEKDAYS[dd.weekday()]} {dd.day}/{dd.month}"


templates.env.filters["day_label"] = day_label
templates.env.filters["mmss"] = lambda x: f"{int(x) // 60}:{int(x) % 60:02d}"


def back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def get_post(post_id: int):
    row = db.one(
        """SELECT p.*, c.start, c.end, c.text, c.dialect, c.score, c.category, c.reason, c.main_speaker,
                  c.source_id, s.filename AS src_name
           FROM posts p JOIN clips c ON c.id=p.clip_id JOIN sources s ON s.id=c.source_id WHERE p.id=?""",
        (post_id,),
    )
    if not row:
        raise HTTPException(404)
    return row


def queue_render(post_id: int) -> None:
    db.exec("UPDATE posts SET render_status='queued', updated_at=? WHERE id=?", (now(), post_id))
    db.enqueue("render", post_id=post_id)


# --- pages ---------------------------------------------------------------------------------------

@app.get("/")
def home():
    return RedirectResponse(f"/day/{pool.today(settings).isoformat()}")


@app.get("/day/{day}")
def day_view(request: Request, day: str):
    t = pool.today(settings)
    n = int(settings.section("planning").get("days_ahead", 3))
    days = [(t + timedelta(days=i)).isoformat() for i in range(n)]
    if day not in days:
        days = sorted(set(days + [day]))
    rows = db.q(
        """SELECT p.*, c.start, c.end, c.dialect, c.score FROM posts p JOIN clips c ON c.id=p.clip_id
           WHERE p.day=? AND p.status NOT IN ('rejected','expired') ORDER BY p.slot""",
        (day,),
    )
    by_acc = {a.name: [] for a in settings.accounts}
    for r in rows:
        by_acc.setdefault(r["account"], []).append(r)
    done = sum(1 for r in rows if r["status"] == "posted")
    sources_busy = db.q("SELECT filename, stage FROM sources WHERE status IN ('new','processing')")
    return templates.TemplateResponse(request, "day.html", {
        "day": day, "days": days, "by_acc": by_acc, "accounts": settings.accounts,
        "runway": pool.runway(db, settings), "done": done, "total": len(rows),
        "busy": sources_busy,
    })


@app.get("/post/{post_id}")
def post_view(request: Request, post_id: int):
    p = get_post(post_id)
    photos = db.q(
        """SELECT * FROM photos WHERE kind='upload' OR clip_id=? ORDER BY kind='frame' DESC, id DESC LIMIT 60""",
        (p["clip_id"],),
    )
    crop = json.loads(p["crop"] or "{}")
    return templates.TemplateResponse(request, "post.html", {
        "p": p, "photos": photos, "crop": crop, "presets": settings.raw.get("crop_presets", {}),
    })


@app.get("/sources")
def sources_view(request: Request):
    rows = db.q(
        """SELECT s.*, (SELECT COUNT(*) FROM clips c WHERE c.source_id=s.id) AS n_clips
           FROM sources s ORDER BY s.id DESC LIMIT 100"""
    )
    return templates.TemplateResponse(request, "sources.html", {
        "sources": rows, "runway": pool.runway(db, settings),
    })


@app.get("/photos")
def photos_view(request: Request):
    rows = db.q("SELECT * FROM photos WHERE kind='upload' ORDER BY id DESC")
    return templates.TemplateResponse(request, "photos.html", {"photos": rows})


# --- media ---------------------------------------------------------------------------------------

@app.get("/media/post/{post_id}")
def media_post(post_id: int, download: int = 0):
    p = get_post(post_id)
    if not p["render_path"] or not Path(p["render_path"]).exists():
        raise HTTPException(404, "not rendered yet")
    name = f"{p['account']}_{p['day']}_{p['slot'].replace(':', 'h')}.mp4"
    return FileResponse(
        p["render_path"], media_type="video/mp4",
        filename=name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


@app.get("/media/photo/{photo_id}")
def media_photo(photo_id: int):
    r = db.one("SELECT path FROM photos WHERE id=?", (photo_id,))
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404)
    return FileResponse(r["path"])


@app.get("/post/{post_id}/status")
def post_status(post_id: int):
    p = get_post(post_id)
    return JSONResponse({
        "render_status": p["render_status"], "render_error": p["render_error"],
        "caption": p["caption"], "updated_at": p["updated_at"], "status": p["status"],
    })


# --- actions -------------------------------------------------------------------------------------

@app.post("/post/{post_id}/hook")
def set_hook(request: Request, post_id: int, hook: str = Form(...)):
    get_post(post_id)
    db.exec("UPDATE posts SET hook=?, updated_at=? WHERE id=?", (hook.strip()[:120], now(), post_id))
    queue_render(post_id)
    return back(request)


@app.post("/post/{post_id}/caption")
def set_caption(request: Request, post_id: int, caption: str = Form(...)):
    get_post(post_id)
    db.exec("UPDATE posts SET caption=?, updated_at=? WHERE id=?", (caption.strip(), now(), post_id))
    return back(request)


@app.post("/post/{post_id}/regen-caption")
def regen_caption(request: Request, post_id: int):
    get_post(post_id)
    db.exec("UPDATE posts SET caption='', updated_at=? WHERE id=?", (now(), post_id))
    db.enqueue("caption", post_id=post_id)
    return back(request)


def _save_upload(f: UploadFile) -> int:
    ext = Path(f.filename or "x.jpg").suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        raise HTTPException(400, "unsupported image type")
    dest = settings.photos / f"up_{secrets.token_hex(6)}{ext}"
    with open(dest, "wb") as out:
        shutil.copyfileobj(f.file, out)
    return db.exec("INSERT INTO photos(path, kind, created_at) VALUES (?, 'upload', ?)", (str(dest), now()))


@app.post("/post/{post_id}/photo")
def set_photo(request: Request, post_id: int, photo_id: int | None = Form(None),
              file: UploadFile | None = File(None)):
    get_post(post_id)
    if file is not None and file.filename:
        photo_id = _save_upload(file)
    if not photo_id:
        raise HTTPException(400, "choose or upload a photo")
    db.exec("UPDATE posts SET photo_id=?, updated_at=? WHERE id=?", (photo_id, now(), post_id))
    queue_render(post_id)
    return back(request)


@app.post("/post/{post_id}/crop")
def set_crop(request: Request, post_id: int, preset: str = Form(""),
             cx: float = Form(0.5), cy: float = Form(0.5), zoom: float = Form(1.0)):
    get_post(post_id)
    presets = settings.raw.get("crop_presets", {})
    crop = presets.get(preset) or {"cx": cx, "cy": cy, "zoom": zoom}
    db.exec("UPDATE posts SET crop=?, updated_at=? WHERE id=?", (json.dumps(crop), now(), post_id))
    queue_render(post_id)
    return back(request)


@app.post("/post/{post_id}/reject")
def reject_post(post_id: int):
    p = get_post(post_id)
    pool.reject(db, post_id)
    db.enqueue("plan")
    return RedirectResponse(f"/day/{p['day']}", status_code=303)


@app.post("/post/{post_id}/posted")
def toggle_posted(request: Request, post_id: int):
    p = get_post(post_id)
    pool.mark_posted(db, post_id, posted=p["status"] != "posted")
    return back(request)


@app.post("/sources/upload")
def upload_source(file: UploadFile = File(...)):
    name = re.sub(r"[^\w.\-]", "_", Path(file.filename or "live.mp4").name)
    if Path(name).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".flv", ".m4v", ".ts"}:
        raise HTTPException(400, "unsupported video type")
    dest = settings.inbox / f"{secrets.token_hex(3)}_{name}"
    tmp = dest.with_name("." + dest.name + ".part")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(file.file, out, length=8 * 1024 * 1024)
    tmp.rename(dest)
    sid = db.exec(
        "INSERT INTO sources(path, filename, kind, status, created_at) VALUES (?,?, 'upload', 'new', ?)",
        (str(dest), dest.name, now()),
    )
    db.enqueue("process_source", source_id=sid)
    return RedirectResponse("/sources", status_code=303)


@app.post("/sources/{source_id}/retry")
def retry_source(source_id: int):
    db.exec("UPDATE sources SET status='new', error='' WHERE id=?", (source_id,))
    db.enqueue("process_source", source_id=source_id)
    return RedirectResponse("/sources", status_code=303)


@app.post("/photos/upload")
def upload_photos(files: list[UploadFile] = File(...)):
    for f in files:
        if f.filename:
            _save_upload(f)
    return RedirectResponse("/photos", status_code=303)


@app.post("/photos/{photo_id}/delete")
def delete_photo(photo_id: int):
    r = db.one("SELECT path FROM photos WHERE id=? AND kind='upload'", (photo_id,))
    if r and not db.one("SELECT id FROM posts WHERE photo_id=? AND status IN ('draft','ready')", (photo_id,)):
        Path(r["path"]).unlink(missing_ok=True)
        db.exec("DELETE FROM photos WHERE id=?", (photo_id,))
    return RedirectResponse("/photos", status_code=303)
