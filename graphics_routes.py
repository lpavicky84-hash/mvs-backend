"""Graphics API (/api/graphics). Thumbnail work is tracked independently of the
video's editing lifecycle (a video can be Editing while its thumbnail is Approved)."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date

from database import get_db
from security import get_graphics
from models import VideoTask, GraphicsTask, ProductionStaffProfile
import production_core as pc

router = APIRouter(prefix="/api/graphics", tags=["Graphics"])


def _me_staff(db, me):
    sp = pc.staff_profile(db, me)
    if not sp or sp.staff_role != "graphics":
        raise HTTPException(403, "Graphics profile not found")
    return sp


def _my_gtask(db, sp, tid):
    g = db.query(GraphicsTask).filter(GraphicsTask.task_id == int(tid)).first()
    if not g:
        raise HTTPException(404, "Thumbnail task not found")
    if g.graphics_id != sp.id:
        raise HTTPException(403, "This thumbnail is not assigned to you")
    return g


@router.get("/dashboard")
def gfx_dashboard(db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    base = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id)

    def c(*st):
        return base.filter(GraphicsTask.status.in_(st)).count()

    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    done_m = base.filter(GraphicsTask.status == "approved",
                         GraphicsTask.approved_at != None,
                         GraphicsTask.approved_at >= month_start).count()
    # appreciation / achievements (§10, §23) from real graphics data
    all_done = base.filter(GraphicsTask.status == "approved").all()
    total_done = len(all_done)
    revisions = sum(int(g.revision_count or 0) for g in all_done)
    first_time = sum(1 for g in all_done if int(g.revision_count or 0) == 0)
    approval_rate = round(first_time * 100 / total_done) if total_done else 0
    badges = []
    if total_done >= 3 and approval_rate >= 90:
        badges.append("First-time Approved")
    if total_done >= 10:
        badges.append("10+ Thumbnails")
    if total_done >= 25:
        badges.append("25+ Thumbnails")
    return {
        "greeting_name": me.name,
        "events": pc.active_events_for(db, "graphics"),
        "appreciation": {"ontime_pct": approval_rate, "avg_rating": 0,
                         "rate_label": "First-time approved", "revisions": revisions,
                         "badges": badges, "total_done": total_done},
        "kpis": {
            "new": c("new", "pending"),
            "in_progress": c("in_progress"),
            "changes": c("changes"),
            "submitted": c("submitted"),
            "approved": c("approved"),
        },
        "monthly": {"thumbnails_completed": done_m},
    }


@router.get("/tasks")
def gfx_tasks(status: str = "", filter: str = "", db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    q = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id)
    if status:
        q = q.filter(GraphicsTask.status == status)
    # Frontend tabs bhejte hain ?filter=<preset> — inhe status pe map karo (warna har tab pe
    # saare tasks dikhte the). 'pending' me purane 'pending' aur naye 'new' dono aate hain.
    f = (filter or "").strip().lower()
    now = datetime.utcnow()
    today = date.today()
    if f == "pending":
        q = q.filter(GraphicsTask.status.in_(["new", "pending", "in_progress"]))
    elif f == "review":
        q = q.filter(GraphicsTask.status == "submitted")
    elif f == "changes":
        q = q.filter(GraphicsTask.status == "changes")
    elif f == "completed":
        q = q.filter(GraphicsTask.status == "approved")
    elif f == "overdue":
        q = q.filter(GraphicsTask.status != "approved",
                     GraphicsTask.deadline != None, GraphicsTask.deadline < now)
    elif f == "today":
        q = q.filter(GraphicsTask.status != "approved",
                     GraphicsTask.deadline != None,
                     func.date(GraphicsTask.deadline) == today)
    out = []
    for g in q.order_by(GraphicsTask.created_at.desc()).all():
        t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
        if not t:
            continue
        row = pc.task_out(db, t, light=True)
        out.append(row)
    return {"tasks": out}


@router.get("/tasks/{tid}")
def gfx_task_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    g = _my_gtask(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    out = pc.task_out(db, t, timeline=True)
    return out


@router.post("/tasks/{tid}/start")
def gfx_start(tid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    g = _my_gtask(db, sp, tid)
    if g.status not in ("new", "pending", "changes"):
        raise HTTPException(400, "Thumbnail is not ready to start")
    g.status = "in_progress"
    if not g.started_at:
        g.started_at = datetime.utcnow()
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    pc.log_event(db, t, me, "thumbnail_started", new_state=t.lifecycle)
    db.commit()
    return {"ok": True, "status": g.status}


@router.post("/tasks/{tid}/submit")
def gfx_submit(tid: int, payload: dict = Body(...),
               db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    g = _my_gtask(db, sp, tid)
    url = (payload.get("thumbnail_url") or "").strip()
    if not url:
        raise HTTPException(400, "Thumbnail image/URL is required")
    g.thumbnail_url = url
    g.status = "submitted"
    g.submitted_at = datetime.utcnow()
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    pc.log_event(db, t, me, "thumbnail_submitted", new_state=t.lifecycle)
    pc.notify_pms(db, "Thumbnail Submitted",
                  f'{me.name} submitted a thumbnail for "{t.title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "status": g.status}


# Resubmit after PM change request (alias of submit — kept for API clarity).
@router.post("/tasks/{tid}/resubmit")
def gfx_resubmit(tid: int, payload: dict = Body(...),
                 db: Session = Depends(get_db), me=Depends(get_graphics)):
    return gfx_submit(tid, payload, db, me)


# ============================================================ NOTIFICATIONS
@router.get("/notifications")
def _pnotifs(db: Session = Depends(get_db), me=Depends(get_graphics)):
    return {"notifications": pc.notifications_out(db, me), "unread": pc.unread_count(db, me)}


@router.post("/notifications/{nid}/read")
def _pnotif_read(nid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    pc.mark_read(db, me, nid); db.commit(); return {"ok": True}


@router.post("/notifications/read-all")
def _pnotif_read_all(db: Session = Depends(get_db), me=Depends(get_graphics)):
    pc.mark_read(db, me); db.commit(); return {"ok": True}


# ============================================================ THUMBNAIL LIBRARY
@router.get("/library")
def graphics_library(db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = pc.staff_profile(db, me)
    if not sp:
        raise HTTPException(403, "Graphics profile not found")
    rows = (db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id,
                                          GraphicsTask.thumbnail_url != None,
                                          GraphicsTask.thumbnail_url != "")
            .order_by(GraphicsTask.id.desc())
            .limit(60).all())
    out = []
    for g in rows:
        t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
        out.append({"task_id": g.task_id, "title": (t.title if t else "") or "Untitled",
                    "ref_code": (t.ref_code if t else "") or "", "status": g.status or "",
                    "thumbnail_url": g.thumbnail_url, "at": pc._dt(g.submitted_at or g.created_at)})
    return {"thumbnails": out}
