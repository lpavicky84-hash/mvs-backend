"""Editor API (/api/editor). Editors only see and act on their own assigned tasks.
Active editing time is measured from real EditingSession rows (excludes idle/paused)."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta

from database import get_db
from security import get_editor
from models import VideoTask, EditingSession, ProductionStaffProfile, TaskReview
import production_core as pc

router = APIRouter(prefix="/api/editor", tags=["Editor"])


def _me_staff(db, me):
    sp = pc.staff_profile(db, me)
    if not sp or sp.staff_role != "editor":
        raise HTTPException(403, "Editor profile not found")
    return sp


def _my_task(db, sp, tid):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if t.editor_id != sp.id:
        raise HTTPException(403, "This task is not assigned to you")
    return t


def _open_session(db, sp, tid):
    return (db.query(EditingSession)
            .filter(EditingSession.task_id == tid, EditingSession.editor_id == sp.id,
                    EditingSession.ended_at == None)
            .order_by(EditingSession.started_at.desc()).first())


def _close_open_session(db, sp, t):
    s = _open_session(db, sp, t.id)
    if s:
        now = datetime.utcnow()
        s.ended_at = now
        s.duration_seconds = int((now - (s.started_at or now)).total_seconds())
        t.editing_seconds = (t.editing_seconds or 0) + max(0, s.duration_seconds)


# ============================================================ DASHBOARD
@router.get("/dashboard")
def editor_dashboard(db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    now = datetime.utcnow()
    today = date.today()
    base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id)

    def c(*st):
        return base.filter(VideoTask.lifecycle.in_(st)).count()

    month_start = datetime(now.year, now.month, 1)
    edited_m = base.filter(VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"]),
                           VideoTask.updated_at >= month_start).count()
    total_secs = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(
        EditingSession.editor_id == sp.id).scalar() or 0
    # appreciation / achievements (§23) — from real task data, no shaming
    done_tasks = base.filter(VideoTask.lifecycle.in_(
        ["editing_done", "qc_pending", "ready_for_youtube", "uploaded", "completed"])).all()
    ontime = 0; total_done = 0; ratings = []
    for tk in done_tasks:
        total_done += 1
        if tk.deadline and tk.editing_done_at and tk.editing_done_at <= tk.deadline:
            ontime += 1
        if getattr(tk, "quality_rating", None):
            ratings.append(tk.quality_rating)
    ontime_pct = round(ontime * 100 / total_done) if total_done else 0
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else 0
    badges = []
    if total_done >= 3 and ontime_pct >= 90:
        badges.append("On-time Pro")
    if avg_rating >= 4.5 and len(ratings) >= 3:
        badges.append("Top Quality")
    if total_done >= 10:
        badges.append("10+ Delivered")
    # rank #1 streak appreciation + top-performer badge (§23)
    try:
        rank = pc.editor_rank_and_streak(db, sp)
        if rank == 1 and total_done > 0:
            badges.insert(0, "Top Performer")
    except Exception:
        rank = 0
    today0 = datetime(now.year, now.month, now.day)
    soon = now + timedelta(hours=24)
    _not_done = ["uploaded", "completed", "ready_for_youtube", "qc_pending"]
    total_views = db.query(func.coalesce(func.sum(VideoTask.yt_views), 0)).filter(
        VideoTask.cancelled == False, VideoTask.editor_id == sp.id).scalar() or 0
    cards = {
        "assigned_today": base.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing_soon", "approved"])).count(),
        "editing_now": c("editing", "editing_paused"),
        "due_soon": base.filter(VideoTask.deadline != None, VideoTask.deadline >= now,
                                VideoTask.deadline <= soon,
                                ~VideoTask.lifecycle.in_(_not_done)).count(),
        "overdue": base.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                               ~VideoTask.lifecycle.in_(_not_done)).count(),
        "submitted": c("qc_pending"),
        "changes": c("qc_changes"),
        "completed": c("ready_for_youtube", "uploaded", "completed"),
        "ready_for_youtube": c("ready_for_youtube"),
        "total_views": int(total_views),
    }
    return {
        "greeting_name": me.name,
        "events": pc.active_events_for(db, "editor"),
        "appreciation": {"ontime_pct": ontime_pct, "avg_rating": avg_rating,
                         "badges": badges, "total_done": total_done, "rank": rank},
        "cards": cards,
        "kpis": {
            "assigned": c("editor_assigned"),
            "not_started": c("editor_assigned"),
            "editing": c("editing", "editing_paused"),
            "qc_pending": c("qc_pending"),
            "changes": c("qc_changes"),
            "completed": c("ready_for_youtube", "uploaded", "completed"),
            "due_today": base.filter(VideoTask.deadline != None,
                                     func.date(VideoTask.deadline) == today,
                                     ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube"])).count(),
            "overdue": base.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                                   ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube"])).count(),
        },
        "monthly": {
            "videos_edited": edited_m,
            "active_editing_seconds": int(total_secs),
        },
    }


@router.post("/me/photo")
def editor_photo_set(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    sp.photo_b64 = (payload.get("photo") or "").strip() or None
    db.commit()
    return {"ok": True, "has_photo": bool(sp.photo_b64)}


@router.get("/me/photo")
def editor_photo_get(db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    return {"photo": (sp.photo_b64 if sp else "") or "", "name": getattr(me, "name", ""), "role": "editor"}


@router.get("/tasks")
def editor_tasks(status: str = "", filter: str = "", db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    q = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id)
    now = datetime.utcnow()
    preset = (filter or "").lower()
    if preset == "editing":
        q = q.filter(VideoTask.lifecycle.in_(["editing", "editing_paused"]))
    elif preset == "ready":            # ready for submission (editing done, not yet submitted)
        q = q.filter(VideoTask.lifecycle == "editing_done")
    elif preset == "changes":
        q = q.filter(VideoTask.lifecycle == "qc_changes")
    elif preset == "completed":
        q = q.filter(VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"]))
    elif preset == "submitted":
        q = q.filter(VideoTask.lifecycle == "qc_pending")
    elif preset == "assigned":
        q = q.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing_soon", "approved"]))
    elif preset == "overdue":
        q = q.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                     ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube", "qc_pending"]))
    if status:
        if status == "editor_assigned":
            q = q.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing_soon", "approved"]))
        else:
            q = q.filter(VideoTask.lifecycle == status)
    rows = q.order_by(VideoTask.updated_at.desc()).all()
    _outs = [pc.task_out(db, t, light=True) for t in rows]
    try:
        from video_tasks import _vtc_unread_bulk
        _un = _vtc_unread_bulk(db, getattr(me, "id", None), [t.id for t in rows])
        for _o in _outs:
            _o["unread_total"] = (_un.get(_o.get("id"), {}) or {}).get("editor", 0)
    except Exception:
        pass
    return {"tasks": _outs}


@router.get("/tasks/{tid}")
def editor_task_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    out = pc.task_out(db, t, timeline=True, viewer="editor")
    out["source_link"] = t.submitted_link or ""   # editor needs the creator's raw video
    out["progress_history"] = pc.progress_history_out(db, t)
    # Changes Required view: PM remarks + attachments + previous submissions + change history
    out["edit_reviews"] = pc.edit_reviews_out(db, t)
    out["edit_attachments"] = [a for a in pc.attachments_out(db, t) if a.get("kind") == "edit"]
    out["edit_submissions"] = pc.edit_submissions_out(db, t)
    return out


# ============================================================ ACTIONS
@router.get("/tasks/{tid}/comments")
def editor_comments(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    sp = _me_staff(db, me)
    _my_task(db, sp, tid)
    _VT._vtc_mark_read(db, me, tid, "editor")
    return {"comments": _VT._vtc_list(db, tid, "editor")}


@router.post("/tasks/{tid}/comments")
def editor_comment_add(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    from models import VideoTask, YouTuberProfile
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    att = (payload.get("attachment_url") or "").strip()
    imgs = payload.get("images")
    if imgs and not att:
        try:
            urls = pc.save_images(db, t, imgs if isinstance(imgs, list) else [imgs], "chat", None, me, return_urls=True) or []
            if urls:
                att = urls[0]
        except Exception:
            pass
    c = _VT._vtc_add(db, tid, me, payload.get("message") or "", "editor", attachment_url=att, audience="editor")
    if not c:
        raise HTTPException(400, "Empty message")
    # notify the creator (youtuber)
    if getattr(t, "creator_type", "") == "youtuber" and t.youtuber_id:
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
        if yp and yp.user_id:
            pc.notify(db, yp.user_id, "Message from Editor",
                      f'{me.name}: "{(payload.get("message") or "").strip()[:60]}"', "editor_chat", link=str(t.id))
    db.commit()
    return {"ok": True, "comment": _VT._vtc_out(db, c)}


@router.post("/tasks/{tid}/start")
def editor_start(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    if t.lifecycle not in ("editor_assigned", "editing_soon", "approved", "editing_paused", "qc_changes"):
        raise HTTPException(400, "Task is not ready to start editing")
    if not _open_session(db, sp, t.id):
        db.add(EditingSession(task_id=t.id, editor_id=sp.id, started_at=datetime.utcnow()))
    if not t.editing_started_at:
        t.editing_started_at = datetime.utcnow()
    pc.set_state(db, t, "editing", actor=me, event="editing_started", force=True)
    pc.notify_pms(db, "Editing Started", f'{me.name} started editing "{t.title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/pause")
def editor_pause(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    if t.lifecycle != "editing":
        raise HTTPException(400, "Editing is not currently active")
    _close_open_session(db, sp, t)
    pc.set_state(db, t, "editing_paused", actor=me, event="editing_paused")
    db.commit()
    return {"ok": True, "editing_seconds": t.editing_seconds or 0}


@router.post("/tasks/{tid}/resume")
def editor_resume(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    if t.lifecycle != "editing_paused":
        raise HTTPException(400, "Task is not paused")
    if not _open_session(db, sp, t.id):
        db.add(EditingSession(task_id=t.id, editor_id=sp.id, started_at=datetime.utcnow()))
    pc.set_state(db, t, "editing", actor=me, event="editing_resumed")
    db.commit()
    return {"ok": True}


@router.post("/tasks/{tid}/progress")
def editor_progress(tid: int, payload: dict = Body(...),
                    db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    try:
        pct = int(payload.get("progress"))
    except Exception:
        raise HTTPException(400, "progress (0-100) required")
    pct = max(0, min(100, pct))
    t.editing_progress = pct
    pc.log_event(db, t, me, "progress_updated", new_state=t.lifecycle,
                 meta={"progress": pct, "note": (payload.get("remarks") or "")[:200]})
    db.commit()
    return {"ok": True, "progress": pct}


@router.post("/tasks/{tid}/complete")
def editor_complete(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    if t.lifecycle not in ("editing", "editing_paused"):
        raise HTTPException(400, "Editing is not in progress")
    _close_open_session(db, sp, t)
    t.editing_progress = 100
    t.editing_done_at = datetime.utcnow()
    pc.set_state(db, t, "editing_done", actor=me, event="editing_completed")
    pc.notify_pms(db, "Editing Completed", f'{me.name} finished editing "{t.title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle, "editing_seconds": t.editing_seconds or 0}


@router.post("/tasks/{tid}/submit")
def editor_submit(tid: int, payload: dict = Body(...),
                  db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    link = (payload.get("edited_link") or "").strip()
    if not link:
        raise HTTPException(400, "Edited video drive link is required")
    if t.lifecycle not in ("editing_done", "editing", "editing_paused", "qc_changes"):
        raise HTTPException(400, "Task is not ready to submit")
    if t.lifecycle in ("editing", "editing_paused"):
        _close_open_session(db, sp, t)
    t.edited_link = link
    t.qc_status = "pending"
    is_revision = (t.lifecycle == "qc_changes")
    # optional remarks + attachments (screenshots) from the editor
    _rem = (payload.get("remarks") or "").strip()
    rv = TaskReview(task_id=t.id, kind="editor", reviewer_user_id=me.id,
                    decision="submitted", remarks=_rem)
    db.add(rv); db.flush()
    if payload.get("images"):
        pc.save_images(db, t, payload.get("images"), "editor", rv.id, me)
    pc.set_state(db, t, "qc_pending", actor=me,
                 event="revision_submitted" if is_revision else "edited_video_submitted",
                 meta={"link": link, "note": _rem[:200]})
    pc.notify_pms(db, "Edited Video Submitted",
                  f'{me.name} submitted the edited "{t.title}" for QC.', "production", link=str(t.id))
    # on-time appreciation (§23) — one positive nudge, only once, only on an on-time submission
    try:
        if t.deadline and (not is_revision) and (not t.ontime_appreciated) and datetime.utcnow() <= t.deadline:
            t.ontime_appreciated = True
            pc.notify(db, me.id, "Great work!",
                      'Your edited "%s" was submitted on time. Keep it up!' % (t.title or ""),
                      "appreciation", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


# ============================================================ NOTIFICATIONS
@router.get("/notifications")
def _pnotifs(db: Session = Depends(get_db), me=Depends(get_editor)):
    return {"notifications": pc.notifications_out(db, me), "unread": pc.unread_count(db, me)}


@router.post("/notifications/{nid}/read")
def _pnotif_read(nid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    pc.mark_read(db, me, nid); db.commit(); return {"ok": True}


@router.post("/notifications/read-all")
def _pnotif_read_all(db: Session = Depends(get_db), me=Depends(get_editor)):
    pc.mark_read(db, me); db.commit(); return {"ok": True}


# ============================================================ TIME ANALYTICS
@router.get("/time-analytics")
def editor_time_analytics(db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    done = ["editing_done", "qc_pending", "qc_changes", "ready_for_youtube", "uploaded", "completed"]
    completed = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id, VideoTask.lifecycle.in_(done)).count()
    total_secs = int(db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(
        EditingSession.editor_id == sp.id).scalar() or 0)
    # per-task active seconds (from sessions), joined to type
    rows = (db.query(VideoTask.id, VideoTask.video_type,
                     func.coalesce(func.sum(EditingSession.duration_seconds), 0))
            .join(EditingSession, EditingSession.task_id == VideoTask.id)
            .filter(EditingSession.editor_id == sp.id)
            .group_by(VideoTask.id, VideoTask.video_type).all())
    per_task = [(r[1] or "Other", int(r[2] or 0)) for r in rows if r[2]]
    by_type = {}
    for vt, secs in per_task:
        d = by_type.setdefault(vt, {"type": vt, "videos": 0, "seconds": 0})
        d["videos"] += 1; d["seconds"] += secs
    by_type_list = sorted(by_type.values(), key=lambda x: x["seconds"], reverse=True)
    for d in by_type_list:
        d["hours"] = round(d["seconds"] / 3600.0, 1)
        d["avg_hours"] = round(d["seconds"] / 3600.0 / d["videos"], 1) if d["videos"] else 0
        d.pop("seconds", None)
    task_secs = [s for _, s in per_task]
    n = len(task_secs)
    return {
        "total_active_hours": round(total_secs / 3600.0, 1),
        "videos_with_time": n,
        "videos_completed": completed,
        "avg_per_video_hours": round((sum(task_secs) / n) / 3600.0, 1) if n else 0,
        "longest_hours": round(max(task_secs) / 3600.0, 1) if task_secs else 0,
        "shortest_hours": round(min(task_secs) / 3600.0, 1) if task_secs else 0,
        "by_type": by_type_list,
    }


def _is_short(vt):
    v = (vt or "").lower()
    return any(k in v for k in ("short", "reel", "rapid"))


@router.get("/uploads")
def editor_uploads(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Editor's published videos + realtime views. Reuses the shared YouTube views data
    (yt_views), never a separate API. Real data only."""
    sp = _me_staff(db, me)
    base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id)
    _edited = ["editing_done", "qc_pending", "qc_changes", "ready_for_youtube", "uploaded", "completed"]
    total_edited = base.filter(VideoTask.lifecycle.in_(_edited)).count()
    uploaded_rows = base.filter(VideoTask.lifecycle.in_(["uploaded", "completed"]),
                               VideoTask.youtube_url != None, VideoTask.youtube_url != "").all()
    pending_upload = base.filter(VideoTask.lifecycle == "ready_for_youtube").count()
    total_views = sum(int(t.yt_views or 0) for t in uploaded_rows)
    videos = []
    for t in uploaded_rows:
        videos.append({
            "id": t.id, "title": t.title or "", "youtube_url": t.youtube_url or "",
            "yt_video_id": t.yt_video_id or "", "video_type": t.video_type or "",
            "views": int(t.yt_views or 0),
            "published_at": pc._dt(t.published_at) if t.published_at else "",
            "thumbnail": ("https://img.youtube.com/vi/%s/mqdefault.jpg" % t.yt_video_id) if t.yt_video_id else "",
        })
    videos.sort(key=lambda v: -v["views"])
    highest = videos[0] if videos else None
    return {
        "total_edited": total_edited,
        "uploaded": len(uploaded_rows),
        "pending_upload": pending_upload,
        "total_views": total_views,
        "highest": highest,
        "videos": videos,
    }


@router.post("/refresh-views")
def editor_refresh_views(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Refresh realtime views for THIS editor's uploaded videos. Reuses the existing
    shared YouTube fetch + snapshot system (no duplicate API)."""
    sp = _me_staff(db, me)
    try:
        from video_tasks import _yt_get_key, _yt_fetch_views
        from models import VideoViewSnapshot
    except Exception:
        return {"ok": False, "updated": 0}
    rows = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id,
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


@router.get("/performance")
def editor_performance(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Full editor performance: quantity, quality, timeliness; split LONG vs SHORT;
    plus chart data (bar / donut / 6-month trend) and ranking. Real data only."""
    sp = _me_staff(db, me)
    now = datetime.utcnow()
    base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id)
    all_tasks = base.all()
    _done = ["editing_done", "qc_pending", "ready_for_youtube", "uploaded", "completed"]
    _uploaded = ["uploaded", "completed"]
    _pending = ["editor_assigned", "editing", "editing_paused", "qc_changes"]

    def bucket(tasks):
        edited = sum(1 for t in tasks if t.lifecycle in _done or t.lifecycle == "qc_changes")
        approved = sum(1 for t in tasks if t.lifecycle in ["ready_for_youtube", "uploaded", "completed"])
        uploaded = sum(1 for t in tasks if t.lifecycle in _uploaded)
        pending = sum(1 for t in tasks if t.lifecycle in _pending)
        overdue = sum(1 for t in tasks if t.deadline and t.deadline < now and t.lifecycle not in _uploaded + ["ready_for_youtube"])
        revisions = sum(int(t.revision_count or 0) for t in tasks)
        views = sum(int(t.yt_views or 0) for t in tasks)
        # turnaround: start -> editing_done
        turns = []
        for t in tasks:
            if t.editing_started_at and t.editing_done_at and t.editing_done_at >= t.editing_started_at:
                turns.append((t.editing_done_at - t.editing_started_at).total_seconds() / 3600.0)
        # on-time: editing_done_at <= deadline
        done_with_dl = [t for t in tasks if t.editing_done_at and t.deadline]
        ontime = sum(1 for t in done_with_dl if t.editing_done_at <= t.deadline)
        ratings = [t.quality_rating for t in tasks if t.quality_rating]
        return {
            "videos_edited": edited, "videos_approved": approved, "videos_uploaded": uploaded,
            "pending": pending, "overdue": overdue, "revision_count": revisions,
            "avg_turnaround_hours": round(sum(turns) / len(turns), 1) if turns else 0,
            "on_time_pct": round(ontime * 100 / len(done_with_dl)) if done_with_dl else 0,
            "avg_quality": round(sum(ratings) / len(ratings), 1) if ratings else 0,
            "youtube_views": views,
        }

    longs = [t for t in all_tasks if not _is_short(t.video_type)]
    shorts = [t for t in all_tasks if _is_short(t.video_type)]
    overall = bucket(all_tasks)

    # 6-month trend (videos edited per month)
    trend = []
    for i in range(5, -1, -1):
        m = (now.month - i - 1) % 12 + 1
        y = now.year + ((now.month - i - 1) // 12)
        m0 = datetime(y, m, 1)
        m1 = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        cnt = sum(1 for t in all_tasks if t.editing_done_at and m0 <= t.editing_done_at < m1)
        trend.append({"label": m0.strftime("%b"), "value": cnt})

    # donut: status distribution
    donut = [
        {"label": "Editing", "value": sum(1 for t in all_tasks if t.lifecycle in ["editing", "editing_paused"])},
        {"label": "In QC", "value": sum(1 for t in all_tasks if t.lifecycle == "qc_pending")},
        {"label": "Changes", "value": sum(1 for t in all_tasks if t.lifecycle == "qc_changes")},
        {"label": "Approved", "value": overall["videos_approved"]},
    ]

    try:
        rank = pc.editor_rank_and_streak(db, sp)
    except Exception:
        rank = 0
    # ranking cards: top editors this month by approvals
    ranking = []
    try:
        month0 = datetime(now.year, now.month, 1)
        eds = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "editor",
            ProductionStaffProfile.is_active == True).all()
        for e in eds:
            appr = db.query(VideoTask).filter(
                VideoTask.editor_id == e.id,
                VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"]),
                VideoTask.updated_at >= month0).count()
            ranking.append({"name": e.user.name if e.user else "", "approved": appr,
                            "me": (e.id == sp.id)})
        ranking.sort(key=lambda x: -x["approved"])
        ranking = ranking[:5]
    except Exception:
        ranking = []

    return {
        "overall": overall,
        "long": bucket(longs),
        "short": bucket(shorts),
        "charts": {
            "bar": [
                {"label": "Edited", "value": overall["videos_edited"]},
                {"label": "Approved", "value": overall["videos_approved"]},
                {"label": "Uploaded", "value": overall["videos_uploaded"]},
                {"label": "Pending", "value": overall["pending"]},
                {"label": "Overdue", "value": overall["overdue"]},
            ],
            "donut": donut,
            "trend": trend,
        },
        "rank": rank,
        "ranking": ranking,
    }


@router.post("/tasks/{tid}/request-deadline")
def editor_request_deadline(tid: int, payload: dict = Body(...),
                            db: Session = Depends(get_db), me=Depends(get_editor)):
    """Editor asks the PM for a new deadline (§19). PM must approve before it changes."""
    sp = pc.staff_profile(db, me)
    t = _my_task(db, sp, tid)
    raw = (payload.get("deadline") or "").strip()
    reason = (payload.get("reason") or "").strip()
    if not raw:
        raise HTTPException(400, "Please choose the new deadline you need.")
    if not reason:
        raise HTTPException(400, "Please add a short reason for the PM.")
    try:
        newdl = datetime.fromisoformat(raw.replace("Z", ""))
    except Exception:
        raise HTTPException(400, "Invalid date/time.")
    t.deadline_req = newdl
    t.deadline_req_reason = reason[:400]
    t.deadline_req_status = "pending"
    pc.log_event(db, t, me, "deadline_requested",
                 meta={"note": 'Deadline extension requested to %s \u2014 %s' % (newdl.strftime("%d %b %Y, %I:%M %p"), reason[:120])})
    pc.notify_pms(db, "Deadline Extension Requested",
                  '%s requested a new deadline for "%s".' % (me.name, t.title or ""),
                  "production", link=str(t.id))
    db.commit()
    return {"ok": True, "requested": newdl.strftime("%d %b %Y, %I:%M %p")}
