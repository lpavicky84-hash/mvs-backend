# ===========================================================================
# app_content_routes.py — Admin-managed video content (App Tutorial / Live Classes)
# Admin ek YouTube link + title deta hai. Thumbnail YouTube se auto (video_id se);
# title fetch NAHI hota (admin manual). Link na ho to app/portal us section pe 'SOON'
# dikhata hai; link aate hi 'NEW' (per-student seen-set se clear hota hai).
# Modular + additive. Reuses get_student/get_admin auth. No prefix; per-endpoint guards.
# ===========================================================================
import re
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session

from database import get_db
from security import get_student, get_admin
from models import AppVideo

router = APIRouter(tags=["AppContent"])

_SECTIONS = ("tutorial", "live")


def _norm_section(s):
    s = str(s or "").strip().lower()
    return s if s in _SECTIONS else "tutorial"


def _yt_id(url):
    """YouTube video id nikaalo — watch?v=, youtu.be/, /shorts/, /live/, /embed/, /v/,
    ya seedha 11-char id. Na mile to None."""
    u = str(url or "").strip()
    if not u:
        return None
    m = re.search(
        r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^&]*&)*v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})",
        u)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):
        return u
    return None


def _thumb(vid):
    return ("https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid) if vid else None


def _out(v, admin=False):
    d = {"id": v.id, "section": v.section, "title": v.title or "",
         "youtube_url": v.youtube_url or "", "video_id": v.video_id or "",
         "thumb": _thumb(v.video_id),
         "created_at": str(v.created_at or "")[:19]}
    if admin:
        d["active"] = bool(v.active)
    return d


# ---------------------------------------------------------------------------
# STUDENT — list active videos of a section (newest first)
# ---------------------------------------------------------------------------
@router.get("/api/student/app-videos")
def student_app_videos(section: str = "tutorial", db: Session = Depends(get_db),
                       _=Depends(get_student)):
    sec = _norm_section(section)
    rows = (db.query(AppVideo)
            .filter(AppVideo.section == sec, AppVideo.active == True)
            .order_by(AppVideo.sort_order.asc(), AppVideo.created_at.desc())
            .all())
    return {"section": sec, "videos": [_out(v) for v in rows]}


# ---------------------------------------------------------------------------
# ADMIN — manage videos
# ---------------------------------------------------------------------------
@router.get("/api/admin/app-videos")
def admin_app_videos(section: str = "", db: Session = Depends(get_db),
                     _=Depends(get_admin)):
    q = db.query(AppVideo)
    if section:
        q = q.filter(AppVideo.section == _norm_section(section))
    rows = q.order_by(AppVideo.section.asc(), AppVideo.sort_order.asc(),
                      AppVideo.created_at.desc()).all()
    return {"videos": [_out(v, admin=True) for v in rows]}


@router.post("/api/admin/app-videos")
def admin_app_video_create(payload: dict = Body(...), db: Session = Depends(get_db),
                           me=Depends(get_admin)):
    sec = _norm_section(payload.get("section"))
    title = str(payload.get("title") or "").strip()
    url = str(payload.get("youtube_url") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required.")
    if not url:
        raise HTTPException(status_code=400, detail="YouTube link is required.")
    vid = _yt_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="That doesn't look like a valid YouTube link.")
    v = AppVideo(section=sec, title=title[:300], youtube_url=url[:500], video_id=vid,
                 active=bool(payload.get("active", True)),
                 sort_order=int(payload.get("sort_order") or 0),
                 created_by=getattr(me, "id", None))
    db.add(v)
    db.commit()
    return {"ok": True, "video": _out(v, admin=True)}


@router.patch("/api/admin/app-videos/{vid_id}")
def admin_app_video_update(vid_id: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), _=Depends(get_admin)):
    v = db.query(AppVideo).filter(AppVideo.id == vid_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Video not found.")
    if "title" in payload:
        t = str(payload.get("title") or "").strip()
        if not t:
            raise HTTPException(status_code=400, detail="Title cannot be empty.")
        v.title = t[:300]
    if "youtube_url" in payload:
        url = str(payload.get("youtube_url") or "").strip()
        nid = _yt_id(url)
        if not nid:
            raise HTTPException(status_code=400, detail="That doesn't look like a valid YouTube link.")
        v.youtube_url = url[:500]
        v.video_id = nid
    if "active" in payload:
        v.active = bool(payload.get("active"))
    if "sort_order" in payload:
        try:
            v.sort_order = int(payload.get("sort_order") or 0)
        except Exception:
            pass
    db.commit()
    return {"ok": True, "video": _out(v, admin=True)}


@router.delete("/api/admin/app-videos/{vid_id}")
def admin_app_video_delete(vid_id: int, db: Session = Depends(get_db),
                           _=Depends(get_admin)):
    v = db.query(AppVideo).filter(AppVideo.id == vid_id).first()
    if not v:
        raise HTTPException(status_code=404, detail="Video not found.")
    db.delete(v)
    db.commit()
    return {"ok": True}
