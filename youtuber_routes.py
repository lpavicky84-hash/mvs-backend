"""YouTuber API (/api/youtuber). Independent content creators — NEVER academic.
Approval ON/OFF (creator default + per-video override) decides whether a submitted
video goes to PM review or straight into production."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from datetime import datetime

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

    return {
        "greeting_name": me.name,
        "approval_required": bool(yp.approval_required),
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


@router.get("/videos")
def yt_videos(status: str = "", db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    q = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                   VideoTask.youtuber_id == yp.id)
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
    t = VideoTask(title=title, creator_type="youtuber", youtuber_id=yp.id,
                  subject=(payload.get("subject") or "").strip(),
                  video_type=(payload.get("video_type") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  proposed_by="youtuber", proposal_ok="pending", status="proposal")
    db.add(t)
    db.flush()
    pc.ensure_ref_code(t)
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
