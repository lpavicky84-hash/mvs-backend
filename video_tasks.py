# video_tasks.py — VIDEO TASK MANAGER
# Production manager (admin) -> teacher video tasks: assign with thumbnail + channel +
# deadline, teacher shoots & submits drive link, admin reviews (Approved / Editing Soon /
# Editing Done / Uploaded / Rejected+reshoot), stats + ranking + CSV report + student notify.
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_db
from security import get_admin, get_teacher
from models import User, TeacherProfile, Notification, VideoChannel, VideoTask

router = APIRouter(prefix="/api", tags=["Video Task Manager"])

DEFAULT_CHANNELS = [
    "Manish Verma Official - Main Channel",
    "Manish Verma",
    "MVS Science",
    "MVS Commerce",
    "MVS Arts",
    "MVS Class 10th",
    "Manish Verma Shorts",
    "Dignity",
    "Dignity 11th & 12th",
]

REVIEW_ACTIONS = ("approved", "editing_soon", "editing_done", "uploaded", "rejected")


def _vt_notify(db, user_id, title, message, ntype="video_task", link=None):
    db.add(Notification(user_id=user_id, title=title, message=message,
                        notif_type=ntype, link=link or None))


def _teacher_profile(db, tid):
    return db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()


def _teacher_name(db, tid):
    tp = _teacher_profile(db, tid)
    if not tp:
        return ""
    u = db.query(User).filter(User.id == tp.user_id).first()
    return u.name if u else ""


def _seed_channels(db):
    if db.query(VideoChannel).count() == 0:
        for n in DEFAULT_CHANNELS:
            db.add(VideoChannel(name=n))
        db.commit()


def _vt_sweep(db):
    """Deadline reminders — idempotent (flags se sirf ek baar jaate hain)."""
    now = datetime.now()
    acts = db.query(VideoTask).filter(VideoTask.status == "assigned").all()
    changed = False
    for t in acts:
        if not t.deadline:
            continue
        secs = (t.deadline - now).total_seconds()
        tp = _teacher_profile(db, t.teacher_id)
        uid = tp.user_id if tp else None
        if not uid:
            continue
        if 0 < secs <= 86400 and not t.warned_24h:
            t.warned_24h = True
            _vt_notify(db, uid, "⏰ Deadline Reminder — Video Task",
                       f'Your video task "{t.title}" is due in less than 24 hours. '
                       f'Please record the video and submit the drive link before the deadline.')
            changed = True
        if secs < 0 and not t.warned_overdue:
            t.warned_overdue = True
            _vt_notify(db, uid, "⚠️ Video Task Overdue",
                       f'Your video task "{t.title}" has crossed its deadline. '
                       f'Repeated delays may affect your payout. Please submit the video link at the earliest.')
            changed = True
    if changed:
        db.commit()


def _task_out(db, t, with_thumb=True):
    now = datetime.now()
    secs_left = int((t.deadline - now).total_seconds()) if t.deadline else None
    out = {
        "id": t.id, "title": t.title, "teacher_id": t.teacher_id,
        "teacher": _teacher_name(db, t.teacher_id),
        "channel_id": t.channel_id, "channel": t.channel_name or "",
        "has_thumbnail": bool(t.thumbnail_b64),
        "thumbnail_link": t.thumbnail_link or "",
        "reference": t.reference or "", "remarks": t.remarks or "",
        "deadline": t.deadline.strftime("%Y-%m-%dT%H:%M") if t.deadline else "",
        "deadline_nice": t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else "",
        "seconds_left": secs_left,
        "overdue": bool(secs_left is not None and secs_left < 0 and t.status == "assigned"),
        "status": t.status,
        "proposed_by": t.proposed_by, "proposal_ok": t.proposal_ok or "",
        "submitted_link": t.submitted_link or "",
        "submitted_at": t.submitted_at.strftime("%d %b %Y, %I:%M %p") if t.submitted_at else "",
        "on_time": t.on_time,
        "reviewed": bool(t.reviewed),
        "review_remarks": t.review_remarks or "",
        "reject_count": t.reject_count or 0,
        "created_at": t.created_at.strftime("%d %b %Y") if t.created_at else "",
    }
    if with_thumb:
        out["thumbnail_b64"] = t.thumbnail_b64 or ""
    return out


def vt_task_rank_rows(db):
    """Task completion ranking — on-time delivery rate ke hisaab se."""
    rows = []
    tps = db.query(TeacherProfile).all()
    for tp in tps:
        tasks = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id,
                         VideoTask.proposal_ok != "pending").all())
        if not tasks:
            continue
        subs = [t for t in tasks if t.submitted_at]
        done = len(subs)
        ontime = sum(1 for t in subs if t.on_time)
        delayed = sum(1 for t in subs if t.on_time is False)
        pending = sum(1 for t in tasks if t.status == "assigned")
        rate = round(100 * ontime / done) if done else 0
        u = db.query(User).filter(User.id == tp.user_id).first()
        rows.append({
            "teacher_id": tp.id,
            "name": (u.name if u else "") or f"Teacher #{tp.id}",
            "photo": bool(getattr(tp, "photo_b64", None)),
            "assigned": len(tasks), "done": done, "pending": pending,
            "ontime": ontime, "delayed": delayed, "rate": rate,
        })
    rows.sort(key=lambda r: (-r["rate"], -r["ontime"], -r["done"], r["name"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _parse_deadline(val):
    """'2026-07-30T18:00' (datetime-local) -> datetime. Invalid/empty -> None."""
    if not val:
        return None
    try:
        return datetime.strptime(str(val)[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None


# =============================================================
# CHANNELS
# =============================================================
@router.get("/admin/video-channels")
def vt_list_channels(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    rows = db.query(VideoChannel).order_by(VideoChannel.id.asc()).all()
    return {"channels": [{"id": c.id, "name": c.name, "active": bool(c.active)} for c in rows]}


@router.post("/admin/video-channels")
def vt_add_channel(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Channel name is required")
    if db.query(VideoChannel).filter(VideoChannel.name == name).first():
        raise HTTPException(400, "This channel already exists")
    c = VideoChannel(name=name)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


# =============================================================
# ADMIN — ASSIGN / LIST / STATS / REVIEW / PROPOSALS / REPORT
# =============================================================
@router.post("/admin/video-tasks")
def vt_assign(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    tid = int(payload.get("teacher_id") or 0)
    title = (payload.get("title") or "").strip()
    dl = _parse_deadline(payload.get("deadline"))
    if not tid or not title:
        raise HTTPException(400, "Teacher and title are required")
    if not dl:
        raise HTTPException(400, "A valid deadline is required")
    tp = _teacher_profile(db, tid)
    if not tp:
        raise HTTPException(404, "Teacher not found")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    t = VideoTask(
        teacher_id=tid, title=title,
        channel_id=ch.id if ch else None,
        channel_name=ch.name if ch else "",
        thumbnail_b64=(payload.get("thumbnail_b64") or None),
        thumbnail_link=(payload.get("thumbnail_link") or "").strip(),
        reference=(payload.get("reference") or "").strip(),
        remarks=(payload.get("remarks") or "").strip(),
        deadline=dl, status="assigned", proposed_by="admin", proposal_ok="approved",
    )
    db.add(t)
    if tp.user_id:
        _vt_notify(db, tp.user_id, "🎬 New Video Task Assigned",
                   f'You have been assigned a new video task: "{title}"'
                   + (f' for {ch.name}' if ch else '')
                   + f'. Deadline: {dl.strftime("%d %b %Y, %I:%M %p")}. '
                   f'Please check My Tasks for the thumbnail and details.')
    db.commit()
    return {"ok": True, "id": t.id}


@router.get("/admin/video-tasks")
def vt_admin_list(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending")
    if teacher_id:
        q = q.filter(VideoTask.teacher_id == teacher_id)
    if status:
        q = q.filter(VideoTask.status == status)
    if channel_id:
        q = q.filter(VideoTask.channel_id == channel_id)
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    props = (db.query(VideoTask).filter(VideoTask.proposal_ok == "pending")
             .order_by(VideoTask.created_at.desc()).all())
    return {"tasks": [_task_out(db, t) for t in tasks],
            "proposals": [_task_out(db, t) for t in props]}


@router.get("/admin/video-tasks/stats")
def vt_admin_stats(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    tasks = (db.query(VideoTask)
             .filter(VideoTask.proposal_ok != "pending").all())
    now = datetime.now()
    total = len(tasks)
    done = sum(1 for t in tasks if t.submitted_at)
    pending = sum(1 for t in tasks if t.status == "assigned")
    delayed = sum(1 for t in tasks
                  if (t.on_time is False) or
                  (t.status == "assigned" and t.deadline and t.deadline < now))
    ranks = vt_task_rank_rows(db)
    top = ranks[0] if ranks else None
    most_delayed = None
    if ranks:
        md = max(ranks, key=lambda r: r["delayed"])
        if md["delayed"] > 0:
            most_delayed = md
    proposals = db.query(VideoTask).filter(VideoTask.proposal_ok == "pending").count()
    return {"total": total, "done": done, "pending": pending, "delayed": delayed,
            "proposals": proposals, "by_teacher": ranks,
            "top": top, "most_delayed": most_delayed}


@router.post("/admin/video-tasks/{task_id}/review")
def vt_review(task_id: int, payload: dict = Body(...),
              db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    action = (payload.get("action") or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise HTTPException(400, "Invalid review action")
    remarks = (payload.get("remarks") or "").strip()
    tp = _teacher_profile(db, t.teacher_id)
    uid = tp.user_id if tp else None

    if action == "rejected":
        ndl = _parse_deadline(payload.get("new_deadline"))
        if not ndl:
            raise HTTPException(400, "A new deadline is required when rejecting")
        t.status = "assigned"
        t.reject_count = (t.reject_count or 0) + 1
        t.reviewed = True
        t.review_remarks = remarks
        t.submitted_link = ""
        t.submitted_at = None
        t.on_time = None
        t.deadline = ndl
        t.warned_24h = False
        t.warned_overdue = False
        if uid:
            _vt_notify(db, uid, "↩️ Video Task Sent Back for Reshoot",
                       f'Your submission for "{t.title}" was rejected'
                       + (f': {remarks}' if remarks else '.')
                       + f' New deadline: {ndl.strftime("%d %b %Y, %I:%M %p")}. '
                       f'Please reshoot and submit again from My Tasks.')
    else:
        t.status = action
        t.reviewed = True
        t.review_remarks = remarks
        label = {"approved": "Approved", "editing_soon": "Editing Soon",
                 "editing_done": "Editing Done", "uploaded": "Uploaded"}[action]
        if uid:
            _vt_notify(db, uid, f"✅ Video Task Update — {label}",
                       f'Your video "{t.title}" status is now: {label}'
                       + (f'. Remarks: {remarks}' if remarks else '.'))
    db.commit()
    return {"ok": True, "status": t.status}


@router.post("/admin/video-tasks/{task_id}/approve-proposal")
def vt_approve_proposal(task_id: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    dl = _parse_deadline(payload.get("deadline"))
    if not dl:
        raise HTTPException(400, "A valid deadline is required")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    if payload.get("thumbnail_b64"):
        t.thumbnail_b64 = payload["thumbnail_b64"]
    if payload.get("thumbnail_link"):
        t.thumbnail_link = payload["thumbnail_link"].strip()
    if payload.get("reference"):
        t.reference = payload["reference"].strip()
    if payload.get("remarks"):
        t.remarks = payload["remarks"].strip()
    if ch:
        t.channel_id = ch.id
        t.channel_name = ch.name
    t.deadline = dl
    t.status = "assigned"
    t.proposal_ok = "approved"
    tp = _teacher_profile(db, t.teacher_id)
    if tp and tp.user_id:
        _vt_notify(db, tp.user_id, "✅ Video Proposal Approved",
                   f'Your video proposal "{t.title}" has been approved. '
                   f'Deadline: {dl.strftime("%d %b %Y, %I:%M %p")}. '
                   f'Thumbnail and details are available in My Tasks.')
    db.commit()
    return {"ok": True, "id": t.id}


@router.post("/admin/video-tasks/{task_id}/reject-proposal")
def vt_reject_proposal(task_id: int, payload: dict = Body(default={}),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    t.proposal_ok = "rejected"
    t.status = "rejected"
    t.reviewed = True
    t.review_remarks = (payload.get("remarks") or "").strip()
    tp = _teacher_profile(db, t.teacher_id)
    if tp and tp.user_id:
        _vt_notify(db, tp.user_id, "❌ Video Proposal Not Approved",
                   f'Your video proposal "{t.title}" was not approved'
                   + (f': {t.review_remarks}' if t.review_remarks else '.'))
    db.commit()
    return {"ok": True}


@router.post("/admin/video-tasks/{task_id}/notify-students")
def vt_notify_students(task_id: int, payload: dict = Body(default={}),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    """Video link students ko notification se — click pe link open hota hai."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    link = (payload.get("link") or t.submitted_link or "").strip()
    if not link:
        raise HTTPException(400, "No video link attached to this task yet")
    msg = (payload.get("message") or "").strip() or \
        f'A new video "{t.title}" is now available' + \
        (f' on {t.channel_name}' if t.channel_name else '') + '. Tap to watch.'
    users = db.query(User).filter(User.is_active == True, User.role == "student").all()
    for u in users:
        _vt_notify(db, u.id, f"🎬 New Video: {t.title}", msg, "video_link", link)
    db.commit()
    return {"ok": True, "count": len(users)}


@router.get("/admin/video-tasks/report.csv")
def vt_report_csv(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    import csv
    import io
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending")
    if teacher_id:
        q = q.filter(VideoTask.teacher_id == teacher_id)
    if status:
        q = q.filter(VideoTask.status == status)
    if channel_id:
        q = q.filter(VideoTask.channel_id == channel_id)
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Teacher", "Channel", "Deadline", "Status",
                "Submitted At", "On Time", "Reshoots", "Review Remarks", "Created"])
    for t in tasks:
        w.writerow([
            t.id, t.title, _teacher_name(db, t.teacher_id), t.channel_name or "",
            t.deadline.strftime("%d %b %Y %H:%M") if t.deadline else "",
            t.status,
            t.submitted_at.strftime("%d %b %Y %H:%M") if t.submitted_at else "",
            ("Yes" if t.on_time else ("No" if t.on_time is False else "")),
            t.reject_count or 0, (t.review_remarks or "").replace("\n", " "),
            t.created_at.strftime("%d %b %Y") if t.created_at else "",
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=video_tasks_report.csv"})


# =============================================================
# TEACHER — MY TASKS / PROPOSE / SUBMIT
# =============================================================
def _get_tp(current_user, db):
    tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == current_user.id).first()
    if not tp:
        raise HTTPException(404, "Teacher profile not found")
    return tp


@router.get("/teacher/video-tasks/my")
def vt_my_tasks(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _vt_sweep(db)
    tp = _get_tp(current_user, db)
    tasks = (db.query(VideoTask)
             .filter(VideoTask.teacher_id == tp.id)
             .order_by(VideoTask.created_at.desc()).all())
    active = [t for t in tasks if t.status == "assigned" and t.proposal_ok != "pending"]
    active.sort(key=lambda t: t.deadline or datetime.max)
    rest = [t for t in tasks if t not in active]
    out = [_task_out(db, t) for t in active + rest]
    nxt = active[0] if active else None
    return {"tasks": out,
            "next_deadline": (_task_out(db, nxt) if nxt else None)}


@router.post("/teacher/video-tasks/propose")
def vt_propose(payload: dict = Body(...), db: Session = Depends(get_db),
               current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title is required")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    t = VideoTask(teacher_id=tp.id, title=title,
                  channel_id=ch.id if ch else None,
                  channel_name=ch.name if ch else "",
                  reference=(payload.get("reference") or "").strip(),
                  status="proposal", proposed_by="teacher", proposal_ok="pending")
    db.add(t)
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "🎬 New Video Proposal",
                   f'{(uname.name if uname else "A teacher")} proposed a video: "{title}". '
                   f'Review it in Task Manager to assign a thumbnail and deadline.')
    db.commit()
    return {"ok": True, "id": t.id}


@router.post("/teacher/video-tasks/{task_id}/submit")
def vt_submit(task_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
              current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.teacher_id == tp.id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status != "assigned":
        raise HTTPException(400, "This task is not open for submission")
    link = (payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "Please paste the drive link of your video")
    now = datetime.now()
    t.submitted_link = link
    t.submitted_at = now
    t.status = "submitted"
    t.reviewed = False
    t.on_time = bool(t.deadline and now <= t.deadline)
    if t.on_time:
        _vt_notify(db, tp.user_id, "🎉 Great Job — Submitted On Time",
                   f'Excellent work! Your video "{t.title}" was submitted before the deadline. '
                   f'Keep up the great consistency!')
    elif not t.warned_overdue:
        t.warned_overdue = True
        _vt_notify(db, tp.user_id, "⚠️ Late Submission Noted",
                   f'Your video "{t.title}" was submitted after the deadline. '
                   f'Repeated delays may affect your payout.')
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "📥 Video Submitted for Checking",
                   f'{(uname.name if uname else "A teacher")} submitted the video '
                   f'"{t.title}" ({"on time" if t.on_time else "delayed"}). '
                   f'Please review it in Task Manager.')
    db.commit()
    return {"ok": True, "on_time": t.on_time}
