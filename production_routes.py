"""Production Manager API (/api/production).

The PM is the operational owner. Admin also has access (oversight). Every mutation
is authorised server-side and updates the shared state engine in production_core.
"""
from fastapi import APIRouter, Depends, HTTPException, Body, Response
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from datetime import datetime, date, timedelta

from database import get_db
from security import get_pm_or_admin
from models import (
    User, UserRole, VideoTask, GraphicsTask, EditingSession, ProductionEvent,
    TaskReview, YouTuberProfile, ProductionStaffProfile, TeacherProfile,
)
import production_core as pc

router = APIRouter(prefix="/api/production", tags=["Production"])


def _task(db, tid):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return t


# ============================================================ DASHBOARD
@router.get("/dashboard")
def pm_dashboard(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    today = date.today()
    q = db.query(VideoTask).filter(VideoTask.cancelled == False)

    def c(*states):
        return q.filter(VideoTask.lifecycle.in_(states)).count()

    active_states = ["creator_assigned", "creator_working", "creator_submitted",
                     "pm_review", "approved", "editor_assigned", "editing",
                     "editing_paused", "editing_done", "qc_pending", "qc_changes",
                     "ready_for_youtube", "changes_required"]
    kpis = {
        "active": q.filter(VideoTask.lifecycle.in_(active_states)).count(),
        "teacher_pending": q.filter(VideoTask.creator_type == "teacher",
                                    VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
        "youtuber_pending": q.filter(VideoTask.creator_type == "youtuber",
                                     VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
        "pm_review": c("creator_submitted", "pm_review"),
        "editing": c("editing", "editing_paused"),
        "graphics": db.query(GraphicsTask).filter(GraphicsTask.status.in_(["in_progress", "submitted"])).count(),
        "qc_pending": c("qc_pending"),
        "ready_for_youtube": c("ready_for_youtube"),
        "due_today": q.filter(VideoTask.deadline != None,
                              func.date(VideoTask.deadline) == today,
                              ~VideoTask.lifecycle.in_(["uploaded", "completed"])).count(),
        "overdue": q.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                            ~VideoTask.lifecycle.in_(["uploaded", "completed"])).count(),
    }
    # This-month metrics
    month_start = datetime(now.year, now.month, 1)
    created_m = q.filter(VideoTask.created_at >= month_start).count()
    completed_m = q.filter(VideoTask.published_at != None,
                           VideoTask.published_at >= month_start).count()
    secondary = {
        "videos_this_month": created_m,
        "completed_this_month": completed_m,
        "active_editors": db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "editor",
            ProductionStaffProfile.is_active == True).count(),
        "active_graphics": db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "graphics",
            ProductionStaffProfile.is_active == True).count(),
    }
    # Bottleneck = biggest waiting bucket
    buckets = {"Creator": kpis["teacher_pending"] + kpis["youtuber_pending"],
               "PM Review": kpis["pm_review"], "Editing": kpis["editing"],
               "Graphics": kpis["graphics"], "QC": kpis["qc_pending"],
               "Ready for YouTube": kpis["ready_for_youtube"]}
    bottleneck = max(buckets, key=buckets.get) if any(buckets.values()) else "None"
    return {"greeting_name": me.name, "date": today.isoformat(),
            "kpis": kpis, "secondary": secondary,
            "bottleneck": bottleneck, "buckets": buckets}


# ============================================================ TASK LIST
@router.get("/tasks")
def pm_tasks(status: str = "", creator_type: str = "", editor_id: int = 0,
             graphics_id: int = 0, priority: str = "", q: str = "",
             deadline: str = "", page: int = 1, size: int = 40,
             db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    query = db.query(VideoTask).filter(VideoTask.cancelled == False)
    if status:
        query = query.filter(VideoTask.lifecycle == status)
    if creator_type:
        query = query.filter(VideoTask.creator_type == creator_type)
    if editor_id:
        query = query.filter(VideoTask.editor_id == editor_id)
    if graphics_id:
        query = query.filter(VideoTask.graphics_id == graphics_id)
    if priority:
        query = query.filter(VideoTask.priority == priority)
    if q:
        like = "%" + q.strip() + "%"
        query = query.filter(or_(VideoTask.title.like(like),
                                 VideoTask.ref_code.like(like),
                                 VideoTask.subject.like(like)))
    now = datetime.utcnow()
    if deadline == "overdue":
        query = query.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                             ~VideoTask.lifecycle.in_(["uploaded", "completed"]))
    elif deadline == "today":
        query = query.filter(VideoTask.deadline != None,
                             func.date(VideoTask.deadline) == date.today())
    elif deadline == "week":
        query = query.filter(VideoTask.deadline != None,
                             VideoTask.deadline <= now + timedelta(days=7),
                             VideoTask.deadline >= now)
    total = query.count()
    rows = (query.order_by(VideoTask.updated_at.desc())
            .offset(max(0, page - 1) * size).limit(size).all())
    return {"total": total, "page": page, "size": size,
            "tasks": [pc.task_out(db, t, light=True) for t in rows]}


@router.get("/tasks/{tid}")
def pm_task_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    return pc.task_out(db, t, timeline=True)


# ============================================================ CREATE TASK
@router.post("/tasks")
def pm_create_task(payload: dict = Body(...), db: Session = Depends(get_db),
                   me=Depends(get_pm_or_admin)):
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A title is required")
    ctype = (payload.get("creator_type") or "teacher").strip().lower()
    if ctype not in ("teacher", "youtuber"):
        raise HTTPException(400, "creator_type must be teacher or youtuber")
    t = VideoTask(title=title, creator_type=ctype,
                  subject=(payload.get("subject") or "").strip(),
                  video_type=(payload.get("video_type") or "").strip(),
                  channel_name=(payload.get("channel_name") or "").strip(),
                  streaming=(payload.get("streaming") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  priority=(payload.get("priority") or "normal").strip(),
                  proposed_by="admin", status="assigned")
    if payload.get("approval_required") is not None:
        t.approval_required = bool(payload.get("approval_required"))
    # deadline
    dl = (payload.get("deadline") or "").strip()
    if dl:
        try:
            t.deadline = datetime.fromisoformat(dl.replace("Z", ""))
        except Exception:
            pass
    # creator
    if ctype == "teacher":
        tid = int(payload.get("teacher_id") or 0)
        if not tid or not db.query(TeacherProfile).filter(TeacherProfile.id == tid).first():
            raise HTTPException(400, "Valid teacher_id required")
        t.teacher_id = tid
    else:
        yid = int(payload.get("youtuber_id") or 0)
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == yid).first()
        if not yp:
            raise HTTPException(400, "Valid youtuber_id required")
        t.youtuber_id = yid
    db.add(t)
    db.flush()
    pc.ensure_ref_code(t)
    pc.set_state(db, t, "creator_assigned", actor=me, event="task_created")
    pc.log_event(db, t, me, "creator_assigned", new_state="creator_assigned")
    # notify creator
    if ctype == "teacher":
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
        if tp and tp.user_id:
            pc.notify(db, tp.user_id, "New Video Task", f'You have been assigned: "{title}".',
                      "video_task", link=str(t.id))
    else:
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
        if yp and yp.user_id:
            pc.notify(db, yp.user_id, "New Video Request", f'A new video has been requested: "{title}".',
                      "video_request", link=str(t.id))
    db.commit()
    return {"ok": True, "id": t.id, "ref_code": t.ref_code}


# ============================================================ CREATOR REVIEW
@router.post("/tasks/{tid}/approve-creator")
def approve_creator(tid: int, payload: dict = Body(default={}),
                    db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if t.lifecycle not in ("creator_submitted", "pm_review"):
        raise HTTPException(400, "Task is not awaiting creator approval")
    db.add(TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                      decision="approved", remarks=(payload.get("remarks") or "")))
    pc.set_state(db, t, "approved", actor=me, event="approved")
    _notify_creator(db, t, "Video Approved", "Your video has been approved and entered production.")
    pc.graphics_task(db, t, create=True)   # graphics can begin in parallel
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/request-creator-changes")
def request_creator_changes(tid: int, payload: dict = Body(...),
                            db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for changes")
    t = _task(db, tid)
    rv = TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                    decision="changes", remarks=remarks)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "creator", rv.id, me)
    pc.set_state(db, t, "changes_required", actor=me, event="changes_requested")
    _notify_creator(db, t, "Changes Requested", remarks[:180])
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/reject-creator")
def reject_creator(tid: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for rejection")
    t = _task(db, tid)
    db.add(TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                      decision="rejected", remarks=remarks))
    pc.set_state(db, t, "rejected", actor=me, event="rejected")
    _notify_creator(db, t, "Reshoot Required", remarks[:180])
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


# ============================================================ EDITOR ASSIGN
@router.post("/tasks/{tid}/assign-editor")
def assign_editor(tid: int, payload: dict = Body(...),
                  db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    eid = int(payload.get("editor_id") or 0)
    ed = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.id == eid,
        ProductionStaffProfile.staff_role == "editor").first()
    if not ed:
        raise HTTPException(400, "Valid editor_id required")
    t.editor_id = eid
    pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned")
    if ed.user_id:
        pc.notify(db, ed.user_id, "New Editing Task",
                  f'You have been assigned to edit: "{t.title}".', "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "editor": ed.user.name if ed.user else ""}


# ============================================================ GRAPHICS ASSIGN
@router.post("/tasks/{tid}/assign-graphics")
def assign_graphics(tid: int, payload: dict = Body(...),
                    db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    gid = int(payload.get("graphics_id") or 0)
    gr = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.id == gid,
        ProductionStaffProfile.staff_role == "graphics").first()
    if not gr:
        raise HTTPException(400, "Valid graphics_id required")
    g = pc.graphics_task(db, t, create=True)
    g.graphics_id = gid
    g.status = "new"
    g.instructions = (payload.get("instructions") or g.instructions or "")
    g.reference_image = (payload.get("reference_image") or g.reference_image or "")
    t.graphics_id = gid
    pc.log_event(db, t, me, "graphics_assigned", new_state=t.lifecycle)
    if gr.user_id:
        pc.notify(db, gr.user_id, "New Thumbnail Task",
                  f'You have a thumbnail to design for: "{t.title}".', "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "graphics": gr.user.name if gr.user else ""}


# ============================================================ THUMBNAIL QC
@router.post("/tasks/{tid}/thumbnail-approve")
def thumbnail_approve(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    g = pc.graphics_task(db, t)
    if not g or g.status != "submitted":
        raise HTTPException(400, "No submitted thumbnail to approve")
    g.status = "approved"
    g.approved_at = datetime.utcnow()
    pc.log_event(db, t, me, "thumbnail_approved", new_state=t.lifecycle)
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            pc.notify(db, sp.user_id, "Thumbnail Approved", f'Your thumbnail for "{t.title}" was approved.', "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


@router.post("/tasks/{tid}/thumbnail-changes")
def thumbnail_changes(tid: int, payload: dict = Body(...),
                      db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required")
    t = _task(db, tid)
    g = pc.graphics_task(db, t)
    if not g:
        raise HTTPException(400, "No thumbnail task")
    g.status = "changes"
    g.remarks = remarks
    g.revision_count = (g.revision_count or 0) + 1
    rv = TaskReview(task_id=t.id, kind="thumbnail", reviewer_user_id=me.id,
                    decision="changes", remarks=remarks, revision_no=g.revision_count)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "thumbnail", rv.id, me)
    pc.log_event(db, t, me, "thumbnail_changes_requested", new_state=t.lifecycle)
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            pc.notify(db, sp.user_id, "Thumbnail Changes Requested", remarks[:180], "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


# ============================================================ QC (edited video)
@router.post("/tasks/{tid}/qc-approve")
def qc_approve(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if t.lifecycle != "qc_pending":
        raise HTTPException(400, "Task is not in QC")
    t.qc_status = "approved"
    db.add(TaskReview(task_id=t.id, kind="edit", reviewer_user_id=me.id, decision="approved",
                      revision_no=t.revision_count or 0))
    pc.set_state(db, t, "ready_for_youtube", actor=me, event="qc_approved")
    if t.editor_id:
        ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ed and ed.user_id:
            pc.notify(db, ed.user_id, "QC Approved", f'Your edit of "{t.title}" passed QC.', "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/request-edit-changes")
def request_edit_changes(tid: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for changes")
    t = _task(db, tid)
    if t.lifecycle != "qc_pending":
        raise HTTPException(400, "Task is not in QC")
    t.qc_status = "changes"
    t.revision_count = (t.revision_count or 0) + 1
    rv = TaskReview(task_id=t.id, kind="edit", reviewer_user_id=me.id, decision="changes",
                    remarks=remarks, revision_no=t.revision_count)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "edit", rv.id, me)
    pc.set_state(db, t, "qc_changes", actor=me, event="changes_requested")
    if t.editor_id:
        ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ed and ed.user_id:
            pc.notify(db, ed.user_id, "Edit Changes Requested", remarks[:180], "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle, "revision": t.revision_count}


# ============================================================ YOUTUBE PUBLISH
@router.post("/tasks/{tid}/youtube")
def add_youtube(tid: int, payload: dict = Body(...),
                db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Adding a valid YouTube URL is the trigger — state becomes UPLOADED automatically."""
    from video_tasks import _yt_extract_id, _yt_get_key, _yt_fetch_views
    url = (payload.get("youtube_url") or "").strip()
    if not url:
        raise HTTPException(400, "youtube_url required")
    vid = _yt_extract_id(url)
    if not vid:
        raise HTTPException(400, "Could not read a valid YouTube video id from that URL")
    t = _task(db, tid)
    t.youtube_url = url
    t.yt_video_id = vid
    t.published_at = datetime.utcnow()
    pc.set_state(db, t, "uploaded", actor=me, event="youtube_link_added")
    pc.log_event(db, t, me, "uploaded", new_state="uploaded")
    # fetch initial metrics (best-effort)
    try:
        key = _yt_get_key(db)
        got = _yt_fetch_views([vid], key)
        if vid in got:
            t.yt_views = got[vid]
            t.yt_views_at = datetime.utcnow()
            pc.log_event(db, t, me, "youtube_metrics_updated", new_state="uploaded",
                         meta={"views": got[vid]})
    except Exception:
        pass
    db.commit()
    return {"ok": True, "video_id": vid, "views": t.yt_views, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/complete")
def mark_completed(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if t.lifecycle != "uploaded":
        raise HTTPException(400, "Only uploaded tasks can be completed")
    pc.set_state(db, t, "completed", actor=me, event="uploaded")
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


# ============================================================ TEAM / WORKLOAD
@router.get("/team")
def pm_team(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    today = date.today()

    def staff_block(role):
        out = []
        for sp in db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.staff_role == role,
                ProductionStaffProfile.is_active == True).all():
            if role == "editor":
                base = db.query(VideoTask).filter(VideoTask.editor_id == sp.id)
                active = base.filter(VideoTask.lifecycle.in_(
                    ["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"])).count()
                completed = base.filter(VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"])).count()
                overdue = base.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                                      ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube"])).count()
                due_today = base.filter(VideoTask.deadline != None, func.date(VideoTask.deadline) == today).count()
            else:
                gbase = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id)
                active = gbase.filter(GraphicsTask.status.in_(["new", "in_progress", "changes"])).count()
                completed = gbase.filter(GraphicsTask.status == "approved").count()
                overdue = 0
                due_today = 0
            out.append({"id": sp.id, "name": sp.user.name if sp.user else "",
                        "active": active, "recommended": sp.recommended_load or 5,
                        "completed": completed, "overdue": overdue, "due_today": due_today})
        return out

    yts = []
    for yp in db.query(YouTuberProfile).filter(YouTuberProfile.is_active == True).all():
        base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                          VideoTask.youtuber_id == yp.id)
        yts.append({
            "id": yp.id, "name": yp.user.name if yp.user else "",
            "approval_required": bool(yp.approval_required),
            "pending": base.filter(VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
            "submitted": base.filter(VideoTask.lifecycle.in_(["creator_submitted", "pm_review"])).count(),
            "in_production": base.filter(VideoTask.lifecycle.in_(
                ["approved", "editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes", "ready_for_youtube"])).count(),
            "published": base.filter(VideoTask.lifecycle.in_(["uploaded", "completed"])).count(),
        })
    return {"editors": staff_block("editor"), "graphics": staff_block("graphics"),
            "youtubers": yts}


# ============================================================ PEOPLE (dropdowns)
@router.get("/people")
def pm_people(role: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Light lists for assignment dropdowns: editors, graphics, youtubers, teachers."""
    out = {}
    if role in ("", "editor"):
        out["editors"] = [{"id": s.id, "name": s.user.name if s.user else "",
                           "recommended": s.recommended_load or 5}
                          for s in db.query(ProductionStaffProfile).filter(
                              ProductionStaffProfile.staff_role == "editor",
                              ProductionStaffProfile.is_active == True).all()]
    if role in ("", "graphics"):
        out["graphics"] = [{"id": s.id, "name": s.user.name if s.user else "",
                           "recommended": s.recommended_load or 5}
                          for s in db.query(ProductionStaffProfile).filter(
                              ProductionStaffProfile.staff_role == "graphics",
                              ProductionStaffProfile.is_active == True).all()]
    if role in ("", "youtuber"):
        out["youtubers"] = [{"id": y.id, "name": y.user.name if y.user else "",
                            "approval_required": bool(y.approval_required)}
                           for y in db.query(YouTuberProfile).filter(
                               YouTuberProfile.is_active == True).all()]
    if role in ("", "teacher"):
        rows = (db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id)
                .filter(User.is_active == True).order_by(User.name.asc()).all())
        out["teachers"] = [{"id": t.id, "name": t.user.name if t.user else ""} for t in rows]
    return out


# ============================================================ CREATOR PERFORMANCE
@router.get("/creators")
def pm_creators(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    done = ["uploaded", "completed", "ready_for_youtube"]

    def stats_for(base):
        total = base.count()
        completed = base.filter(VideoTask.lifecycle.in_(done)).count()
        pending = base.filter(~VideoTask.lifecycle.in_(done)).count()
        overdue = base.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                              ~VideoTask.lifecycle.in_(done)).count()
        views = int(base.with_entities(func.coalesce(func.sum(VideoTask.yt_views), 0)).scalar() or 0)
        comp = base.filter(VideoTask.lifecycle.in_(done), VideoTask.published_at != None,
                           VideoTask.deadline != None).all()
        den = len(comp); hit = sum(1 for t in comp if t.published_at <= t.deadline)
        return {"videos": total, "completed": completed, "pending": pending, "overdue": overdue,
                "views": views, "on_time_pct": round(100.0 * hit / den) if den else None}

    teachers = []
    for tp in db.query(TeacherProfile).all():
        base = db.query(VideoTask).filter(VideoTask.creator_type == "teacher", VideoTask.teacher_id == tp.id)
        if base.count() == 0:
            continue
        s = stats_for(base); s["name"] = tp.user.name if tp.user else ""; s["id"] = tp.id
        teachers.append(s)
    teachers.sort(key=lambda x: x["videos"], reverse=True)

    youtubers = []
    for yp in db.query(YouTuberProfile).all():
        base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.youtuber_id == yp.id)
        if base.count() == 0:
            continue
        s = stats_for(base); s["name"] = yp.user.name if yp.user else ""; s["id"] = yp.id
        s["published"] = base.filter(VideoTask.lifecycle.in_(["uploaded", "completed"])).count()
        youtubers.append(s)
    youtubers.sort(key=lambda x: x["videos"], reverse=True)

    return {"teachers": teachers, "youtubers": youtubers}


# ============================================================ REAL-TIME VIEWS
@router.get("/views")
def pm_views(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    uploaded_q = db.query(VideoTask).filter(VideoTask.yt_video_id != None, VideoTask.yt_video_id != "")
    uploaded = uploaded_q.count()
    total_views = int(db.query(func.coalesce(func.sum(VideoTask.yt_views), 0)).filter(
        VideoTask.yt_video_id != None, VideoTask.yt_video_id != "").scalar() or 0)
    pending_upload = db.query(VideoTask).filter(VideoTask.lifecycle == "ready_for_youtube").count()

    vids = uploaded_q.all()
    by_creator = {}
    videos = []
    for t in vids:
        name, ctype = pc.creator_info(db, t)
        v = int(t.yt_views or 0)
        key = (name or "Unknown") + "|" + ctype
        c = by_creator.setdefault(key, {"name": name or "Unknown", "creator_type": ctype.lower(), "views": 0, "videos": 0})
        c["views"] += v; c["videos"] += 1
        videos.append({"id": t.id, "title": t.title or "Untitled", "ref_code": t.ref_code or "",
                       "creator": name or "Unknown", "creator_type": ctype.lower(),
                       "video_type": t.video_type or "", "views": v,
                       "youtube_url": t.youtube_url or "", "published_at": pc._dt(t.published_at)})
    creators = sorted(by_creator.values(), key=lambda x: x["views"], reverse=True)
    for c in creators:
        c["share"] = round(100.0 * c["views"] / total_views, 1) if total_views else 0
    videos.sort(key=lambda x: x["views"], reverse=True)
    highest = videos[0] if videos else None
    return {"total_views": total_views, "uploaded": uploaded, "pending_upload": pending_upload,
            "highest": highest, "by_creator": creators, "videos": videos[:50]}


# ============================================================ GLOBAL SEARCH
@router.get("/search")
def pm_search(q: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    like = "%" + q + "%"
    ids = {}  # id -> matched-on label

    def add(rows, label):
        for t in rows:
            ids.setdefault(t.id, label)

    # direct fields on the task
    add(db.query(VideoTask).filter(or_(
        VideoTask.ref_code.like(like), VideoTask.title.like(like),
        VideoTask.yt_video_id.like(like), VideoTask.subject.like(like),
        VideoTask.channel_name.like(like))).limit(30).all(), "Task")

    # by teacher name
    tps = db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.name.like(like)).all()
    if tps:
        tids = [t.id for t in tps]
        add(db.query(VideoTask).filter(VideoTask.teacher_id.in_(tids)).limit(30).all(), "Teacher")

    # by youtuber name
    yps = db.query(YouTuberProfile).join(User, YouTuberProfile.user_id == User.id).filter(User.name.like(like)).all()
    if yps:
        yids = [y.id for y in yps]
        add(db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.youtuber_id.in_(yids)).limit(30).all(), "YouTuber")

    # by editor / graphics name
    sps = db.query(ProductionStaffProfile).join(User, ProductionStaffProfile.user_id == User.id).filter(User.name.like(like)).all()
    eids = [s.id for s in sps if s.staff_role == "editor"]
    gids = [s.id for s in sps if s.staff_role == "graphics"]
    if eids:
        add(db.query(VideoTask).filter(VideoTask.editor_id.in_(eids)).limit(30).all(), "Editor")
    if gids:
        add(db.query(VideoTask).filter(VideoTask.graphics_id.in_(gids)).limit(30).all(), "Graphics")

    if not ids:
        return {"results": []}
    tasks = db.query(VideoTask).filter(VideoTask.id.in_(list(ids.keys()))).order_by(VideoTask.updated_at.desc()).limit(20).all()
    out = []
    for t in tasks:
        o = pc.task_out(db, t, light=True)
        o["match"] = ids.get(t.id, "Task")
        out.append(o)
    return {"results": out}


# ============================================================ PERSON PROFILE
@router.get("/person/{kind}/{pid}")
def pm_person(kind: str, pid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    kind = (kind or "").lower()
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    done = ["uploaded", "completed", "ready_for_youtube"]

    if kind in ("editor", "graphics"):
        sp = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.id == pid, ProductionStaffProfile.staff_role == kind).first()
        if not sp:
            raise HTTPException(404, "Not found")
        name = sp.user.name if sp.user else ""
        if kind == "editor":
            base = db.query(VideoTask).filter(VideoTask.editor_id == pid)
            active = base.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"])).count()
            completed = base.filter(VideoTask.lifecycle.in_(done)).count()
            completed_m = base.filter(VideoTask.lifecycle.in_(done), VideoTask.updated_at >= month_start).count()
            overdue = base.filter(VideoTask.deadline != None, VideoTask.deadline < now, ~VideoTask.lifecycle.in_(done)).count()
            secs = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(EditingSession.editor_id == pid).scalar() or 0
            comp = base.filter(VideoTask.lifecycle.in_(done), VideoTask.published_at != None, VideoTask.deadline != None).all()
            ot_den = len(comp); ot_hit = sum(1 for t in comp if t.published_at <= t.deadline)
            stats = {"active": active, "completed": completed, "completed_this_month": completed_m,
                     "overdue": overdue, "active_hours": round(secs / 3600.0, 1),
                     "on_time_pct": round(100.0 * ot_hit / ot_den) if ot_den else None,
                     "recommended_load": sp.recommended_load or 5}
            recent = base.order_by(VideoTask.updated_at.desc()).limit(8).all()
        else:
            gbase = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == pid)
            active = gbase.filter(GraphicsTask.status.in_(["new", "in_progress", "changes"])).count()
            completed = gbase.filter(GraphicsTask.status == "approved").count()
            completed_m = gbase.filter(GraphicsTask.status == "approved", GraphicsTask.approved_at != None, GraphicsTask.approved_at >= month_start).count()
            gts = gbase.filter(GraphicsTask.status == "approved", GraphicsTask.started_at != None, GraphicsTask.approved_at != None).all()
            hrs = [((g.approved_at - g.started_at).total_seconds() / 3600.0) for g in gts]
            stats = {"active": active, "completed": completed, "completed_this_month": completed_m,
                     "overdue": 0, "avg_hours": round(sum(hrs) / len(hrs), 1) if hrs else 0,
                     "on_time_pct": None, "recommended_load": sp.recommended_load or 5}
            task_ids = [g.task_id for g in gbase.order_by(GraphicsTask.created_at.desc()).limit(8).all()]
            recent = db.query(VideoTask).filter(VideoTask.id.in_(task_ids)).all() if task_ids else []
        return {"kind": kind, "name": name, "stats": stats,
                "recent": [pc.task_out(db, t, light=True) for t in recent]}

    if kind == "youtuber":
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == pid).first()
        if not yp:
            raise HTTPException(404, "Not found")
        base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.youtuber_id == pid)
        stats = {"pending": base.filter(VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
                 "submitted": base.filter(VideoTask.lifecycle.in_(["creator_submitted", "pm_review"])).count(),
                 "in_production": base.filter(VideoTask.lifecycle.in_(["approved", "editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes", "ready_for_youtube"])).count(),
                 "published": base.filter(VideoTask.lifecycle.in_(["uploaded", "completed"])).count(),
                 "total_views": db.query(func.coalesce(func.sum(VideoTask.yt_views), 0)).filter(VideoTask.creator_type == "youtuber", VideoTask.youtuber_id == pid).scalar() or 0,
                 "approval_required": bool(yp.approval_required)}
        recent = base.order_by(VideoTask.updated_at.desc()).limit(8).all()
        return {"kind": kind, "name": yp.user.name if yp.user else "", "stats": stats,
                "recent": [pc.task_out(db, t, light=True) for t in recent]}

    raise HTTPException(400, "Invalid person kind")


# ============================================================ ANALYTICS
@router.get("/analytics")
def pm_analytics(days: int = 30, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    start = now - timedelta(days=max(1, min(365, days)))
    done_states = ["uploaded", "completed"]

    # ---- pull a modest working set, compute in Python (dialect-safe) ----
    completed = (db.query(VideoTask)
                 .filter(VideoTask.cancelled == False, VideoTask.published_at != None,
                         VideoTask.published_at >= start).all())
    created_ct = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                            VideoTask.created_at >= start).count()
    active_ct = db.query(VideoTask).filter(
        VideoTask.cancelled == False,
        ~VideoTask.lifecycle.in_(done_states + [""])).count()
    overdue_ct = db.query(VideoTask).filter(
        VideoTask.cancelled == False, VideoTask.deadline != None, VideoTask.deadline < now,
        ~VideoTask.lifecycle.in_(done_states)).count()

    # overview aggregates
    prod_hours = []
    on_time_hit = on_time_den = 0
    first_pass = 0
    for t in completed:
        if t.created_at and t.published_at:
            prod_hours.append((t.published_at - t.created_at).total_seconds() / 3600.0)
        if t.deadline:
            on_time_den += 1
            if t.published_at <= t.deadline:
                on_time_hit += 1
        if (t.revision_count or 0) == 0:
            first_pass += 1
    n_done = len(completed)
    overview = {
        "created": created_ct,
        "completed": n_done,
        "pending": active_ct,
        "overdue": overdue_ct,
        "on_time_pct": round(100.0 * on_time_hit / on_time_den) if on_time_den else None,
        "avg_production_hours": round(sum(prod_hours) / len(prod_hours), 1) if prod_hours else None,
        "qc_first_pass_pct": round(100.0 * first_pass / n_done) if n_done else None,
    }

    # ---- editor performance ----
    editors = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "editor").all()
    ed_rows = []
    for sp in editors:
        etasks = [t for t in completed if t.editor_id == sp.id]
        secs = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(
            EditingSession.editor_id == sp.id).scalar() or 0
        vids = len(etasks)
        ot_den = sum(1 for t in etasks if t.deadline)
        ot_hit = sum(1 for t in etasks if t.deadline and t.published_at <= t.deadline)
        revs = sum((t.revision_count or 0) for t in etasks)
        ed_rows.append({
            "name": sp.user.name if sp.user else "",
            "videos": vids,
            "active_hours": round(secs / 3600.0, 1),
            "avg_hours": round((secs / 3600.0) / vids, 1) if vids else 0,
            "on_time_pct": round(100.0 * ot_hit / ot_den) if ot_den else None,
            "revisions": revs,
        })
    ed_rows.sort(key=lambda x: -x["videos"])

    # ---- graphics performance ----
    gfx = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "graphics").all()
    gfx_rows = []
    for sp in gfx:
        gts = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id,
                                            GraphicsTask.status == "approved",
                                            GraphicsTask.approved_at != None,
                                            GraphicsTask.approved_at >= start).all()
        design_h = [((g.approved_at - g.started_at).total_seconds() / 3600.0)
                    for g in gts if g.started_at and g.approved_at]
        revs = sum((g.revision_count or 0) for g in gts)
        gfx_rows.append({
            "name": sp.user.name if sp.user else "",
            "thumbnails": len(gts),
            "avg_hours": round(sum(design_h) / len(design_h), 1) if design_h else 0,
            "revisions": revs,
        })
    gfx_rows.sort(key=lambda x: -x["thumbnails"])

    # ---- content mix (by video_type) ----
    mix = {}
    for t in completed:
        k = (t.video_type or "Other").strip() or "Other"
        mix[k] = mix.get(k, 0) + 1
    content_mix = sorted([{"type": k, "count": v} for k, v in mix.items()],
                         key=lambda x: -x["count"])

    # ---- weekly trend (last 8 weeks): created vs completed ----
    trend = []
    for w in range(7, -1, -1):
        wk_start = now - timedelta(days=(w + 1) * 7)
        wk_end = now - timedelta(days=w * 7)
        c_created = db.query(VideoTask).filter(
            VideoTask.cancelled == False, VideoTask.created_at >= wk_start,
            VideoTask.created_at < wk_end).count()
        c_done = db.query(VideoTask).filter(
            VideoTask.published_at != None, VideoTask.published_at >= wk_start,
            VideoTask.published_at < wk_end).count()
        trend.append({"label": wk_end.strftime("%d %b"), "created": c_created, "completed": c_done})

    return {"days": days, "overview": overview, "editors": ed_rows,
            "graphics": gfx_rows, "content_mix": content_mix, "trend": trend}


# ============================================================ helpers
def _notify_creator(db, t, title, msg):
    if (t.creator_type or "teacher") == "youtuber" and t.youtuber_id:
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
        if yp and yp.user_id:
            pc.notify(db, yp.user_id, title, msg, "video_request", link=str(t.id))
    elif t.teacher_id:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
        if tp and tp.user_id:
            pc.notify(db, tp.user_id, title, msg, "video_task", link=str(t.id))


# ============================================================ NOTIFICATIONS
@router.get("/notifications")
def _pnotifs(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    return {"notifications": pc.notifications_out(db, me), "unread": pc.unread_count(db, me)}


@router.post("/notifications/{nid}/read")
def _pnotif_read(nid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    pc.mark_read(db, me, nid); db.commit(); return {"ok": True}


@router.post("/notifications/read-all")
def _pnotif_read_all(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    pc.mark_read(db, me); db.commit(); return {"ok": True}


# ============================================================ CHANNELS & VIDEO TYPES
# Slice 1 of Task-Manager parity: the PM can manage channels & video types and use them
# as real dropdowns in Assign Work (same underlying tables as the admin Task Manager).
from models import VideoChannel, VideoType
try:
    from video_tasks import _seed_channels as _vt_seed_channels, _seed_types as _vt_seed_types
except Exception:   # pragma: no cover
    def _vt_seed_channels(db): pass
    def _vt_seed_types(db): pass


@router.get("/channels")
def prod_list_channels(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    try:
        _vt_seed_channels(db)
    except Exception:
        pass
    rows = (db.query(VideoChannel).filter(VideoChannel.active == True)
            .order_by(VideoChannel.id.asc()).all())
    return {"channels": [{"id": c.id, "name": c.name} for c in rows]}


@router.post("/channels")
def prod_add_channel(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    try:
        _vt_seed_channels(db)
    except Exception:
        pass
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Channel name is required")
    if db.query(VideoChannel).filter(VideoChannel.name == name).first():
        raise HTTPException(400, "This channel already exists")
    c = VideoChannel(name=name)
    db.add(c); db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


@router.get("/video-types")
def prod_list_types(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    try:
        _vt_seed_types(db)
    except Exception:
        pass
    rows = (db.query(VideoType).filter(VideoType.active == True)
            .order_by(VideoType.sort.asc(), VideoType.id.asc()).all())
    return {"types": [{"id": c.id, "name": c.name,
                       "streaming_scope": getattr(c, "streaming_scope", "both") or "both"} for c in rows]}


@router.post("/video-types")
def prod_add_type(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    try:
        _vt_seed_types(db)
    except Exception:
        pass
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Type name is required")
    if db.query(VideoType).filter(VideoType.name == name).first():
        raise HTTPException(400, "This type already exists")
    mx = db.query(VideoType).order_by(VideoType.sort.desc()).first()
    scope = (payload.get("streaming_scope") or "both").strip().lower()
    if scope not in ("both", "live", "recorded"):
        scope = "both"
    c = VideoType(name=name, sort=(mx.sort + 1) if mx else 0, streaming_scope=scope)
    db.add(c); db.commit()
    return {"ok": True, "id": c.id, "name": c.name, "streaming_scope": scope}


# ============================================================ REAL-TIME VIEWS + NOTIFY STUDENTS
# Slice 3: PM can refresh live YouTube views and push a published video to students.
try:
    from video_tasks import _yt_get_key as _vt_yt_get_key, _yt_fetch_views as _vt_yt_fetch_views, _vt_notify as _vt_notify_fn
except Exception:   # pragma: no cover
    _vt_yt_get_key = lambda db: None
    _vt_yt_fetch_views = lambda ids, key: {}
    def _vt_notify_fn(db, user_id, title, message, ntype="video_task", link=None): pass


@router.post("/refresh-views")
def prod_refresh_views(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Pull live YouTube view counts for every published task (same source as admin)."""
    from models import VideoViewSnapshot
    key = _vt_yt_get_key(db)
    if not key:
        raise HTTPException(400, "No YouTube API key is set yet. Ask the admin to add it in the Task Manager settings.")
    tasks = db.query(VideoTask).filter(VideoTask.yt_video_id != "", VideoTask.yt_video_id != None).all()
    idmap = {}
    for t in tasks:
        idmap.setdefault(t.yt_video_id, []).append(t)
    if not idmap:
        return {"ok": True, "updated": 0, "fetched": 0, "total": 0}
    got = _vt_yt_fetch_views(list(idmap.keys()), key)
    now = datetime.utcnow(); n = 0
    for vid, views in got.items():
        for t in idmap.get(vid, []):
            t.yt_views = views; t.yt_views_at = now
            try:
                db.add(VideoViewSnapshot(task_id=t.id, views=views))
            except Exception:
                pass
            n += 1
    db.commit()
    return {"ok": True, "updated": n, "fetched": len(got), "total": len(idmap)}


@router.post("/tasks/{tid}/notify-students")
def prod_notify_students(tid: int, payload: dict = Body(default={}),
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Send the published video link to all students as a tappable notification."""
    t = _task(db, tid)
    link = (payload.get("link") or t.youtube_url or t.submitted_link or "").strip()
    if not link:
        raise HTTPException(400, "No video link is attached to this task yet.")
    msg = (payload.get("message") or "").strip() or \
        ('A new video "%s" is now available' % (t.title or "")) + \
        ((" on %s" % t.channel_name) if t.channel_name else "") + ". Tap to watch."
    users = db.query(User).filter(User.is_active == True, User.role == "student").all()
    for u in users:
        _vt_notify_fn(db, u.id, "New Video: %s" % (t.title or ""), msg, "video_link", link)
    db.commit()
    return {"ok": True, "count": len(users)}


# ============================================================ PROPOSALS + URGENT QUEUE
# Slice 4: teacher-proposed videos (proposal_ok == "pending") and teacher-flagged urgent
# requests (kind == "urgent") — the PM can approve into the pipeline or decline.
@router.get("/queues")
def prod_queues(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    props = (db.query(VideoTask).filter(VideoTask.proposal_ok == "pending")
             .order_by(VideoTask.created_at.desc()).all())
    urgent = (db.query(VideoTask).filter(VideoTask.kind == "urgent")
              .order_by(VideoTask.created_at.desc()).all())
    return {"proposals": [pc.task_out(db, t, light=True) for t in props],
            "urgent": [pc.task_out(db, t, light=True) for t in urgent]}


@router.post("/proposals/{tid}/approve")
def prod_approve_proposal(tid: int, payload: dict = Body(default={}),
                          db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid),
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    dl = (payload.get("deadline") or "").strip()
    if dl:
        try:
            t.deadline = datetime.fromisoformat(dl.replace("Z", ""))
        except Exception:
            pass
    for f in ("channel_name", "video_type", "subject", "reference"):
        v = (payload.get(f) or "").strip()
        if v:
            setattr(t, f, v)
    t.proposal_ok = "approved"
    t.status = "assigned"
    pc.ensure_ref_code(t)
    pc.set_state(db, t, "creator_assigned", actor=me, event="proposal_approved")
    pc.log_event(db, t, me, "creator_assigned", new_state="creator_assigned")
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
    if tp and tp.user_id:
        pc.notify(db, tp.user_id, "Proposal Approved",
                  'Your video proposal "%s" has been approved. Check My Tasks.' % (t.title or ""),
                  "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "id": t.id}


@router.post("/proposals/{tid}/decline")
def prod_decline_proposal(tid: int, payload: dict = Body(default={}),
                          db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid),
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    t.proposal_ok = "rejected"
    t.status = "rejected"
    rem = (payload.get("remarks") or "").strip()
    if hasattr(t, "review_remarks"):
        t.review_remarks = rem
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
    if tp and tp.user_id:
        pc.notify(db, tp.user_id, "Proposal Not Approved",
                  ('Your video proposal "%s" was not approved' % (t.title or "")) + ((": " + rem) if rem else "."),
                  "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


# ============================================================ TARGETS · RANKING · CSV REPORT
# Slice 5: teacher monthly targets, task-completion ranking, and a CSV export — the same
# numbers the admin Task Manager shows (shared VideoTask data).
@router.get("/teacher-targets")
def prod_teacher_targets(month: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    try:
        from teacher_routes import _month_range
        from video_tasks import _vt_targets_for
    except Exception:
        return {"month": "", "teachers": []}
    start, end = _month_range(month)
    dt0 = datetime(start.year, start.month, start.day)
    dt1 = datetime(end.year, end.month, end.day)
    out = []
    for tp in db.query(TeacherProfile).all():
        try:
            row = _vt_targets_for(db, tp, dt0, dt1)
        except Exception:
            continue
        if any(r.get("target", 0) > 0 for r in row.get("rows", [])) or row.get("has_tasks"):
            out.append(row)
    out.sort(key=lambda x: (x.get("name") or "").lower())
    return {"month": "%04d-%02d" % (start.year, start.month), "teachers": out}


@router.get("/ranking")
def prod_ranking(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Per-teacher task completion ranking (done / assigned / on-time / delayed / rate)."""
    from sqlalchemy import or_ as _or
    NOT_SPECIAL = _or(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")
    tasks = (db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                                        VideoTask.teacher_id != None).all())
    agg = {}
    for t in tasks:
        a = agg.setdefault(t.teacher_id, {"assigned": 0, "done": 0, "ontime": 0, "delayed": 0})
        a["assigned"] += 1
        if t.submitted_at:
            a["done"] += 1
            if t.on_time is True:
                a["ontime"] += 1
            elif t.on_time is False:
                a["delayed"] += 1
    rows = []
    for tid, a in agg.items():
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
        nm = ""
        try:
            nm = tp.user.name if (tp and tp.user) else ""
        except Exception:
            nm = ""
        den = a["ontime"] + a["delayed"]
        rate = round(100.0 * a["ontime"] / den) if den else 0
        rows.append({"name": nm or ("Teacher #%s" % tid), "assigned": a["assigned"],
                     "done": a["done"], "ontime": a["ontime"], "delayed": a["delayed"], "rate": rate})
    rows.sort(key=lambda x: (x["rate"], x["done"]), reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"ranking": rows}


@router.get("/report.csv")
def prod_report_csv(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import csv, io
    from sqlalchemy import or_ as _or
    NOT_SPECIAL = _or(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")
    try:
        from video_tasks import _teacher_name as _tn
    except Exception:
        def _tn(db, tid): return ""
    tasks = (db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL)
             .order_by(VideoTask.created_at.desc()).all())
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["ID", "Title", "Creator", "Channel", "Type", "Stage", "Deadline",
                "Submitted At", "On Time", "Revisions", "YouTube Views", "Created"])
    for t in tasks:
        try:
            cname, _ct = pc.creator_info(db, t)
        except Exception:
            cname = _tn(db, t.teacher_id)
        w.writerow([
            t.id, t.title or "", cname or "", t.channel_name or "", t.video_type or "",
            pc.lc_label(t.lifecycle) if hasattr(pc, "lc_label") else (t.lifecycle or ""),
            t.deadline.strftime("%d %b %Y %H:%M") if t.deadline else "",
            t.submitted_at.strftime("%d %b %Y %H:%M") if t.submitted_at else "",
            ("Yes" if t.on_time else ("No" if t.on_time is False else "")),
            t.revision_count or 0, (t.yt_views if t.yt_views is not None else ""),
            t.created_at.strftime("%d %b %Y") if t.created_at else "",
        ])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=production_report.csv"})


# ============================================================ COLLAB (multi-teacher verify)
# Slice 6: show collaborators and verify each one. This ONLY sets the verification flags
# (collab_verified) exactly like the admin Task Manager — it does NOT compute or touch any
# payout/performance numbers (those are derived from these flags elsewhere, untouched).
try:
    from video_tasks import (_collab_all_ids as _c_all_ids, _collab_vmap as _c_vmap,
                             _teacher_name as _c_tname, _hist_add as _c_hist)
except Exception:   # pragma: no cover
    def _c_all_ids(t): return [t.teacher_id] if getattr(t, "teacher_id", None) else []
    def _c_vmap(t):
        import json
        try: return json.loads(t.collab_verified) if getattr(t, "collab_verified", "") else {}
        except Exception: return {}
    def _c_tname(db, tid): return ""
    def _c_hist(t, *a, **k): pass


@router.get("/tasks/{tid}/collab")
def prod_task_collab(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    ids = _c_all_ids(t)
    if len(ids) <= 1:
        return {"is_collab": False, "collaborators": []}
    vmap = _c_vmap(t)
    cols = [{"id": i, "name": _c_tname(db, i) or ("Teacher #%s" % i),
             "verified": bool(vmap.get(str(i))), "primary": (i == t.teacher_id)} for i in ids]
    return {"is_collab": True, "collaborators": cols,
            "all_verified": all(vmap.get(str(i)) for i in ids)}


@router.post("/tasks/{tid}/verify-teacher")
def prod_verify_teacher(tid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    teacher_id = int(payload.get("teacher_id") or 0)
    verified = payload.get("verified", True)
    ids = _c_all_ids(t)
    if teacher_id not in ids:
        raise HTTPException(400, "This teacher is not part of this task.")
    import json as _j
    vmap = _c_vmap(t)
    if verified:
        vmap[str(teacher_id)] = True
    else:
        vmap.pop(str(teacher_id), None)
    t.collab_verified = _j.dumps(vmap)     # flags only — payout logic reads these, unchanged
    all_ok = bool(ids) and all(vmap.get(str(i)) for i in ids)
    try:
        _c_hist(t, "verify", "%s %s by production manager" % (
            _c_tname(db, teacher_id), "verified" if verified else "verification removed"))
        if all_ok:
            _c_hist(t, "approved", "All collab teachers verified")
    except Exception:
        pass
    db.commit()
    return {"ok": True, "all_verified": all_ok,
            "collaborators": [{"id": i, "name": _c_tname(db, i) or ("Teacher #%s" % i),
                               "verified": bool(vmap.get(str(i)))} for i in ids]}


# ============================================================ VINTAGE (Old / New)
# Slice 7: mark a video as Old (pre-portal) so it does NOT count toward this month's
# performance, or New (default). This ONLY sets the is_old flag and busts the board cache
# exactly like the admin — it does NOT change any performance/payout calculation.
@router.post("/tasks/{tid}/mark-old")
def prod_mark_old(tid: int, payload: dict = Body(default={}),
                  db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    t.is_old = bool((payload or {}).get("is_old", True))
    db.commit()
    try:
        import perf_engine as _pe
        _pe.bust_board_cache()
    except Exception:
        pass
    return {"ok": True, "id": t.id, "is_old": bool(t.is_old)}


# ============================================================ PROJECT ASSIGN (syllabus)
# Slice 8: assign a whole subject's worth of videos as a PROJECT — items generated from the
# syllabus chapters (PE / TMA scope) or a custom list, with a weekly quota and final
# deadline. Reuses the admin Task Manager's exact helpers (no duplicate logic), so admin and
# production stay perfectly in sync.
try:
    from video_tasks import (_subject_teachers as _p_subject_teachers,
                             _chapters_for as _p_chapters_for,
                             _sync_chapters as _p_sync_chapters,
                             _stable_subject_display as _p_subj_display,
                             _parse_deadline as _p_parse_dl,
                             _teacher_profile as _p_teacher_profile,
                             _teacher_name as _p_teacher_name,
                             _hist_add as _p_hist, _vt_notify as _p_notify,
                             _ch_status as _p_ch_status,
                             WEEK_DAYS as _P_WEEK_DAYS,
                             CHAPTER_EDIT_STATUSES as _P_CH_STATUSES)
    from models import VideoTaskChapter as _PVChapter
    _PROJECT_OK = True
except Exception:   # pragma: no cover
    _PROJECT_OK = False


@router.get("/subjects")
def prod_subjects(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from models import AvailableSubject
    out = {"10": [], "12": []}
    for s in db.query(AvailableSubject).filter(AvailableSubject.is_active == True).all():
        out.get(s.class_level, out.setdefault(s.class_level, [])).append(
            {"id": s.id, "name": s.name, "code": s.code, "mode": (s.mode or "live")})
    return out


@router.get("/project/subject-teachers")
def prod_project_subject_teachers(subject: str = "", class_level: str = "",
                                  db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        return {"teachers": []}
    return {"teachers": _p_subject_teachers(db, subject, class_level)}


@router.get("/project/chapters-preview")
def prod_project_chapters_preview(subject: str = "", class_level: str = "", scope: str = "",
                                  teacher_id: int = 0, db: Session = Depends(get_db),
                                  me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        return {"count": 0, "titles": [], "source": "none"}
    subject = (subject or "").strip()
    if not subject:
        return {"count": 0, "titles": [], "source": "none"}
    if class_level not in ("10", "12"):
        class_level = ""
    tp = _p_teacher_profile(db, teacher_id) if teacher_id else None
    titles, src = _p_chapters_for(db, tp.id if tp else 0, subject, class_level, scope)
    return {"count": len(titles), "titles": titles[:8], "source": src}


@router.post("/project")
def prod_create_project(payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        raise HTTPException(400, "Project assignment is not available on this server build.")
    import json as _pj, re as _pre
    subject = (payload.get("subject") or "").strip()
    class_level = (payload.get("class_level") or "").strip()
    if class_level not in ("10", "12"):
        class_level = ""
    connect = bool(payload.get("connect"))
    final_dl = _p_parse_dl(payload.get("deadline"))
    if not final_dl:
        raise HTTPException(400, "Final deadline is required")
    tp = None
    tid = int(payload.get("teacher_id") or 0)
    if tid:
        tp = _p_teacher_profile(db, tid)
        if not tp:
            raise HTTPException(404, "Teacher not found")
    elif subject:
        matches = _p_subject_teachers(db, subject, class_level)
        if not matches:
            raise HTTPException(400, "No active teacher found for this subject — please select one manually.")
        tp = _p_teacher_profile(db, matches[0]["profile_id"])
    if not tp:
        raise HTTPException(400, "Select a teacher, or choose a subject for auto-fetch.")
    display = _p_subj_display(subject, class_level) if subject else ""
    title = (payload.get("title") or "").strip() or (("Project — %s" % display) if display else "")
    if not title:
        raise HTTPException(400, "A subject or a project title is required")
    try:
        weekly_quota = max(0, min(50, int(payload.get("weekly_quota") or 0)))
    except Exception:
        weekly_quota = 0
    weekly_day = (payload.get("weekly_day") or "").strip().lower()
    if weekly_day and weekly_day not in _P_WEEK_DAYS:
        raise HTTPException(400, "Invalid weekly day — use monday..sunday")
    scope = (payload.get("chapter_scope") or "").strip().lower()
    if scope not in ("pe", "tma"):
        scope = ""
    item_source, items = "custom", []
    if connect and subject:
        items, _src = _p_chapters_for(db, tp.id, subject, class_level, scope)
        item_source = "syllabus"
        if not items:
            raise HTTPException(400, "No chapters found for this scope in the syllabus manager — "
                                     "choose a different scope or enter video names manually (Connect: No).")
    else:
        seen = set()
        for it in (payload.get("items") or []):
            s2 = _pre.sub(r"\s+", " ", str(it or "")).strip()
            if s2 and s2.lower() not in seen:
                seen.add(s2.lower()); items.append(s2[:300])
            if len(items) >= 100:
                break
        if not items:
            raise HTTPException(400, "Add at least one video/item name (or turn on syllabus connect).")
    t = VideoTask(teacher_id=tp.id, title=title, kind="project", subject=display,
                  video_type="Project", status="assigned", proposed_by="admin",
                  proposal_ok="approved", deadline=final_dl,
                  remarks=(payload.get("remarks") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  weekly_quota=weekly_quota, weekly_day=weekly_day, item_source=item_source)
    db.add(t); db.flush()
    if items:
        _p_sync_chapters(db, t, items)
    try:
        pc.ensure_ref_code(t)
    except Exception:
        pass
    wk = []
    if weekly_quota:
        wk.append("%d videos/week" % weekly_quota)
    if weekly_day:
        wk.append("due every %s" % weekly_day.title())
    try:
        _p_hist(t, "assigned", "Project assigned — %d video items. Final deadline: %s" % (
            len(items), final_dl.strftime("%d %b %Y, %I:%M %p")))
    except Exception:
        pass
    if tp.user_id:
        try:
            _p_notify(db, tp.user_id, "New Project — %s" % title,
                      'You have been assigned a new project: "%s" (%d videos). Final deadline: %s.'
                      % (title, len(items), final_dl.strftime("%d %b %Y, %I:%M %p")))
        except Exception:
            pass
    db.commit()
    return {"ok": True, "id": t.id, "teacher": _p_teacher_name(db, tp.id), "total": len(items)}


@router.get("/tasks/{tid}/chapters")
def prod_task_chapters(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        return {"chapters": []}
    t = _task(db, tid)
    rows = (db.query(_PVChapter).filter(_PVChapter.task_id == t.id)
            .order_by(_PVChapter.sort.asc(), _PVChapter.id.asc()).all())
    return {"is_project": (getattr(t, "kind", "") == "project"),
            "chapters": [{"id": c.id, "title": c.title, "link": (c.link or ""),
                          "status": _p_ch_status(c)} for c in rows]}


@router.post("/chapter-status")
def prod_chapter_status(payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    cid = int(payload.get("chapter_id") or 0)
    status = (payload.get("status") or "").strip()
    if status not in _P_CH_STATUSES:
        raise HTTPException(400, "Invalid status — use editing_soon / editing_done / uploaded")
    row = db.query(_PVChapter).filter(_PVChapter.id == cid).first()
    if not row:
        raise HTTPException(404, "Chapter not found")
    if not (row.link or "").strip():
        raise HTTPException(400, "Video link is not submitted yet — status can be set only after that.")
    t = db.query(VideoTask).filter(VideoTask.id == row.task_id).first()
    if not t or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision", "project"):
        raise HTTPException(404, "Project not found")
    row.edit_status = status
    try:
        _p_hist(t, "progress", '"%s" production status set to %s' % (row.title, status))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "chapter_id": cid, "status": status}
