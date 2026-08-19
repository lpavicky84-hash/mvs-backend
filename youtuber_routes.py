"""YouTuber API (/api/youtuber). Independent content creators — NEVER academic.
Approval ON/OFF (creator default + per-video override) decides whether a submitted
video goes to PM review or straight into production."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from database import get_db
from security import get_youtuber
from models import VideoTask, YouTuberProfile
import production_core as pc

router = APIRouter(prefix="/api/youtuber", tags=["YouTuber"])


def _me_yt(db, me):
    yp = pc.youtuber_profile(db, me)
    if not yp:
        raise HTTPException(403, "YouTuber profile not found")
    return yp


def _my_task(db, yp, tid):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
    if not t:
        raise HTTPException(404, "Video not found")
    if t.creator_type != "youtuber" or t.youtuber_id != yp.id:
        raise HTTPException(403, "This video is not yours")
    return t


@router.get("/dashboard")
def yt_dashboard(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                      VideoTask.youtuber_id == yp.id)

    def c(*st):
        return base.filter(VideoTask.lifecycle.in_(st)).count()

    now = datetime.utcnow()
    today0 = datetime(now.year, now.month, now.day)
    week0 = today0 - timedelta(days=today0.weekday())
    month0 = datetime(now.year, now.month, 1)
    all_tasks = base.all()
    total_views = sum(int(t.yt_views or 0) for t in all_tasks)
    highest = max((int(t.yt_views or 0) for t in all_tasks), default=0)
    _uploaded = ["uploaded", "completed"]
    _editing = ["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"]
    _pending = ["created", "creator_assigned", "creator_working", "creator_submitted",
                "pm_review", "changes_required", "approved"]
    weekly_uploads = base.filter(VideoTask.lifecycle.in_(_uploaded),
                                 VideoTask.published_at != None,
                                 VideoTask.published_at >= week0).count()
    monthly_uploads = base.filter(VideoTask.lifecycle.in_(_uploaded),
                                  VideoTask.published_at != None,
                                  VideoTask.published_at >= month0).count()
    return {
        "greeting_name": me.name,
        "events": pc.active_events_for(db, "youtuber"),
        "approval_required": bool(yp.approval_required),
        "cards": {
            "total_videos": len(all_tasks),
            "uploaded": c(*_uploaded),
            "editing": c(*_editing),
            "pending": c(*_pending),
            "total_views": total_views,
            "highest_views": highest,
            "weekly_uploads": weekly_uploads,
            "monthly_uploads": monthly_uploads,
        },
        "kpis": {
            "requests": c("creator_assigned", "creator_working"),
            "pending_submission": c("creator_assigned", "creator_working", "changes_required"),
            "submitted": c("creator_submitted", "pm_review"),
            "in_production": c("approved", "editor_assigned", "editing", "editing_paused",
                               "editing_done", "qc_pending", "qc_changes", "ready_for_youtube"),
            "qc": c("qc_pending", "qc_changes"),
            "published": c("uploaded", "completed"),
        },
    }


@router.get("/views")
def yt_views(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Realtime views for THIS YouTuber's videos only. Reuses the shared yt_views data."""
    yp = _me_yt(db, me)
    now = datetime.utcnow()
    today0 = datetime(now.year, now.month, now.day)
    week0 = today0 - timedelta(days=today0.weekday())
    month0 = datetime(now.year, now.month, 1)
    tasks = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                       VideoTask.youtuber_id == yp.id).all()
    _uploaded = ["uploaded", "completed"]
    _editing = ["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"]
    uploaded = [t for t in tasks if t.lifecycle in _uploaded and t.youtube_url]
    total_views = sum(int(t.yt_views or 0) for t in uploaded)
    videos = []
    for t in uploaded:
        videos.append({
            "id": t.id, "title": t.title or "", "youtube_url": t.youtube_url or "",
            "yt_video_id": t.yt_video_id or "", "video_type": t.video_type or "",
            "views": int(t.yt_views or 0),
            "published_at": pc._dt(t.published_at) if t.published_at else "",
            "thumbnail": ("https://img.youtube.com/vi/%s/mqdefault.jpg" % t.yt_video_id) if t.yt_video_id else "",
        })
    videos.sort(key=lambda v: -v["views"])
    return {
        "total_videos": len(tasks),
        "uploaded": len(uploaded),
        "editing": sum(1 for t in tasks if t.lifecycle in _editing),
        "pending": len(tasks) - len(uploaded) - sum(1 for t in tasks if t.lifecycle in _editing),
        "total_views": total_views,
        "highest_views": videos[0]["views"] if videos else 0,
        "weekly_uploads": sum(1 for t in uploaded if t.published_at and t.published_at >= week0),
        "monthly_uploads": sum(1 for t in uploaded if t.published_at and t.published_at >= month0),
        "highest": videos[0] if videos else None,
        "videos": videos,
    }


@router.post("/refresh-views")
def yt_refresh_views(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Refresh realtime views for THIS YouTuber's uploaded videos. Reuses the shared
    YouTube fetch + snapshot system (no duplicate API)."""
    yp = _me_yt(db, me)
    try:
        from video_tasks import _yt_get_key, _yt_fetch_views
        from models import VideoViewSnapshot
    except Exception:
        return {"ok": False, "updated": 0}
    rows = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                      VideoTask.youtuber_id == yp.id,
                                      VideoTask.yt_video_id != None,
                                      VideoTask.yt_video_id != "").all()
    idmap = {t.yt_video_id: t for t in rows if t.yt_video_id}
    if not idmap:
        return {"ok": True, "updated": 0}
    updated = 0
    try:
        key = _yt_get_key(db)
        got = _yt_fetch_views(list(idmap.keys()), key)
        for vid, views in (got or {}).items():
            t = idmap.get(vid)
            if t is not None:
                t.yt_views = views
                t.yt_views_at = datetime.utcnow()
                db.add(VideoViewSnapshot(task_id=t.id, views=views))
                updated += 1
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True, "updated": updated}


@router.get("/videos")
def yt_videos(status: str = "", filter: str = "", db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    q = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                   VideoTask.youtuber_id == yp.id)
    preset = (filter or "").lower()
    if preset == "proposal":
        q = q.filter(VideoTask.lifecycle.in_(["created", "creator_assigned", "creator_working", "pm_review", "changes_required"]))
    elif preset == "urgent":
        q = q.filter(VideoTask.priority == "urgent",
                     ~VideoTask.lifecycle.in_(["uploaded", "completed"]))
    elif preset == "editing":
        q = q.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing", "editing_paused",
                                              "editing_done", "qc_pending", "qc_changes"]))
    elif preset == "ready":
        q = q.filter(VideoTask.lifecycle == "ready_for_youtube")
    if status:
        q = q.filter(VideoTask.lifecycle == status)
    rows = q.order_by(VideoTask.updated_at.desc()).all()
    return {"videos": [pc.task_out(db, t, light=True) for t in rows]}


@router.get("/videos/{tid}")
def yt_video_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    # Creators get a clean timeline — internal churn hidden, milestones shown.
    return pc.task_out(db, t, timeline=True)


@router.post("/videos/{tid}/submit")
def yt_submit(tid: int, payload: dict = Body(...),
              db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    link = (payload.get("drive_link") or "").strip()
    if not link:
        raise HTTPException(400, "A Drive link is required")
    if t.lifecycle not in ("creator_assigned", "creator_working", "changes_required"):
        raise HTTPException(400, "This video is not awaiting your submission")
    t.submitted_link = link
    t.submitted_at = datetime.utcnow()
    if pc.needs_pm_approval(db, t):
        pc.set_state(db, t, "pm_review", actor=me, event="youtuber_submitted")
        pc.log_event(db, t, me, "approval_requested", new_state="pm_review")
        pc.notify_pms(db, "Video Submitted (Approval)",
                      f'{me.name} submitted "{t.title}" — approval required.', "production", link=str(t.id))
    else:
        # Approval OFF -> directly enter production; graphics can begin immediately.
        pc.set_state(db, t, "approved", actor=me, event="youtuber_submitted")
        pc.graphics_task(db, t, create=True)
        pc.notify_pms(db, "Video Received",
                      f'{me.name} submitted "{t.title}" — entered production directly.', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle, "approval_required": pc.needs_pm_approval(db, t)}


@router.post("/proposals")
def yt_propose(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title is required")
    dl = None
    raw = (payload.get("deadline") or "").strip()
    if raw:
        try:
            dl = datetime.fromisoformat(raw.replace("Z", ""))
        except Exception:
            pass
    t = VideoTask(title=title, creator_type="youtuber", youtuber_id=yp.id,
                  subject=(payload.get("subject") or "").strip(),
                  video_type=(payload.get("video_type") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  description=(payload.get("description") or "").strip(),
                  remarks=(payload.get("remarks") or "").strip(), deadline=dl,
                  remarks_audience=(payload.get("remarks_audience") or "both"),
                  priority=("urgent" if payload.get("urgent") else "normal"),
                  proposed_by="youtuber", proposal_ok="pending", status="proposal")
    db.add(t)
    db.flush()
    pc.ensure_ref_code(t)
    # optional reference thumbnail (clipboard/upload) -> stored as the task thumbnail
    _imgs = payload.get("images")
    if _imgs:
        try:
            urls = pc.save_images(db, t, _imgs, "reference", None, me, return_urls=True) or []
            if urls:
                t.thumbnail_link = urls[0]
        except Exception:
            pass
    pc.set_state(db, t, "created", actor=me, event="task_created")
    pc.notify_pms(db, "New YouTuber Proposal",
                  f'{me.name} proposed a video: "{title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "id": t.id, "ref_code": t.ref_code}


# ============================================================ NOTIFICATIONS
@router.get("/notifications")
def _pnotifs(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    return {"notifications": pc.notifications_out(db, me), "unread": pc.unread_count(db, me)}


@router.post("/notifications/{nid}/read")
def _pnotif_read(nid: int, db: Session = Depends(get_db), me=Depends(get_youtuber)):
    pc.mark_read(db, me, nid); db.commit(); return {"ok": True}


@router.post("/notifications/read-all")
def _pnotif_read_all(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    pc.mark_read(db, me); db.commit(); return {"ok": True}


# ============================================================ EDITOR LIST (for direct assign, §32)
@router.get("/editors")
def yt_editors(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Active editors with a light workload hint, for direct assignment."""
    from models import ProductionStaffProfile, User, VideoTask
    rows = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "editor",
        ProductionStaffProfile.is_active == True).all()
    out = []
    for sp in rows:
        active = db.query(VideoTask).filter(
            VideoTask.editor_id == sp.id,
            VideoTask.lifecycle.in_(["editor_assigned", "editing", "editing_paused", "editing_done", "qc_changes"])
        ).count()
        u = db.query(User).filter(User.id == sp.user_id).first()
        out.append({"id": sp.id, "name": (u.name if u else "Editor"), "active": active})
    out.sort(key=lambda x: x["active"])
    return {"editors": out}


# ============================================================ DIRECT EDITOR ASSIGN (§32)
@router.post("/assign-editor")
def yt_assign_editor(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Approval-not-required YouTubers can create a task and assign an editor directly.
    The PM still sees the task and can monitor it."""
    from models import ProductionStaffProfile, VideoTask
    yp = _me_yt(db, me)
    if yp.approval_required:
        raise HTTPException(403, "Your workflow needs PM approval. Please submit a proposal instead.")
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title is required.")
    eid = int(payload.get("editor_id") or 0)
    ed = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.id == eid, ProductionStaffProfile.staff_role == "editor").first()
    if not ed:
        raise HTTPException(400, "Please choose a valid editor.")
    from datetime import datetime as _dtc
    dl = None
    raw = (payload.get("deadline") or "").strip()
    if raw:
        try:
            dl = _dtc.fromisoformat(raw.replace("Z", ""))
        except Exception:
            pass
    remarks = (payload.get("remarks") or "").strip()
    t = VideoTask(title=title, creator_type="youtuber", youtuber_id=yp.id,
                  subject=(payload.get("subject") or "").strip(),
                  video_type=(payload.get("video_type") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  description=(payload.get("description") or "").strip(),
                  remarks=remarks, remarks_audience=(payload.get("remarks_audience") or "both"),
                  deadline=dl, editor_id=eid,
                  priority=("urgent" if payload.get("urgent") else "normal"),
                  proposed_by="youtuber", proposal_ok="approved", status="editing_soon")
    db.add(t); db.flush()
    pc.ensure_ref_code(t)
    _imgs = payload.get("images")
    if _imgs:
        try:
            urls = pc.save_images(db, t, _imgs, "reference", None, me, return_urls=True) or []
            if urls:
                t.thumbnail_link = urls[0]
        except Exception:
            pass
    pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned")
    if ed.user_id:
        pc.notify(db, ed.user_id, "New Editing Task",
                  'You have been assigned to edit "%s" (from %s).' % (title, me.name), "video_task", link=str(t.id))
    pc.notify_pms(db, "YouTuber Assigned an Editor",
                  '%s directly assigned an editor for "%s".' % (me.name, title), "production", link=str(t.id))
    db.commit()
    return {"ok": True, "id": t.id, "ref_code": t.ref_code}
