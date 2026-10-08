"""Editor API (/api/editor). Editors only see and act on their own assigned tasks.
Active editing time is measured from real EditingSession rows (excludes idle/paused)."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, or_, and_
from datetime import datetime, date, timedelta

from database import get_db
from security import get_editor
from models import VideoTask, EditingSession, ProductionStaffProfile, TaskReview
import production_core as pc
import performance_core as PC

router = APIRouter(prefix="/api/editor", tags=["Editor"])


def _me_staff(db, me):
    sp = pc.staff_profile(db, me)
    if not sp or sp.staff_role != "editor":
        raise HTTPException(403, "Editor profile not found")
    return sp


# Office closes (IST) at this hour — after it the daily report is sent, so the
# self-attendance prompt only appears before it.
_OFFICE_CLOSE_HOUR = 18


@router.get("/attendance/today")
def editor_attendance_today(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Does this editor need the daily 'are you on leave?' prompt right now?
    needs_prompt = did NO own-portal work today AND has not answered yet AND office still open."""
    sp = _me_staff(db, me)
    from models import ProductionAttendance as _ATT
    s, e, day_str = pc.ist_day_bounds_utc()
    worked = pc.editor_worked_today(db, sp, s, e)
    row = db.query(_ATT).filter(_ATT.staff_id == sp.id, _ATT.day == day_str).first()
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    before_close = now_ist.hour < _OFFICE_CLOSE_HOUR
    needs_prompt = (not worked) and (row is None) and before_close
    return {"needs_prompt": needs_prompt, "worked": worked,
            "status": (row.status if row else ""), "day": day_str}


@router.post("/attendance")
def editor_set_attendance(payload: dict = Body(...), db: Session = Depends(get_db),
                          me=Depends(get_editor)):
    """Editor self-marks today: on_leave=true -> Leave, false -> Present (admin-credited work
    then shows in the report instead of Leave)."""
    sp = _me_staff(db, me)
    from models import ProductionAttendance as _ATT
    _s, _e, day_str = pc.ist_day_bounds_utc()
    on_leave = bool(payload.get("on_leave"))
    row = db.query(_ATT).filter(_ATT.staff_id == sp.id, _ATT.day == day_str).first()
    if not row:
        row = _ATT(staff_id=sp.id, day=day_str)
        db.add(row)
    row.status = "leave" if on_leave else "present"
    row.remark = "Marked by staff: " + ("on leave" if on_leave else "present")
    row.set_by = getattr(me, "id", None)
    db.commit()
    return {"ok": True, "status": row.status}


@router.get("/project-videos")
def editor_project_videos(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Project videos assigned to this editor (single videos + whole projects). Phase 3 is a
    read view — the full start/pause/submit workflow is added in the Projects section (Phase 4)."""
    sp = _me_staff(db, me)
    from models import VideoTaskChapter as _VC
    from video_tasks import _chapter_lifecycle as _vt_life, _chapter_lifecycle_label as _vt_life_label
    proj_rows = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                           VideoTask.kind.in_(["one_shot", "rapid_revision", "project"])).all()
    pmap = {t.id: t for t in proj_rows}
    vids = []
    if pmap:
        for c in (db.query(_VC).filter(_VC.task_id.in_(list(pmap.keys())),
                                       _VC.editor_id == sp.id).all()):
            t = pmap.get(c.task_id)
            _dl = getattr(c, "deadline", None) or (t.deadline if t else None)
            _sa = getattr(c, "editing_started_at", None)
            vids.append({
                "chapter_id": c.id, "title": c.title,
                "project_id": c.task_id, "project_title": (t.title or t.subject or "Project") if t else "Project",
                "subject": (t.subject if t else ""), "kind": (t.kind if t else ""),
                "channel_name": (getattr(t, "channel_name", "") or "") if t else "",
                "link": (c.link or ""), "edited_link": (getattr(c, "edited_link", "") or ""),
                "edit_state": (getattr(c, "edit_state", "") or "") or "assigned",
                "editing_progress": int(getattr(c, "editing_progress", 0) or 0),
                "progress_note": (getattr(c, "progress_note", "") or ""),
                "review_status": (getattr(c, "review_status", "") or ""),
                "review_note": (getattr(c, "review_note", "") or ""),
                "qc_status": (getattr(c, "qc_status", "") or ""),
                "qc_note": (getattr(c, "qc_note", "") or ""),
                "lifecycle": _vt_life(c),
                "lifecycle_label": _vt_life_label(c),
                "edit_review_status": (getattr(c, "edit_review_status", "") or ""),
                "edit_review_note": (getattr(c, "edit_review_note", "") or ""),
                "thumbnail": (getattr(c, "thumbnail_link", "") or ""),
                "started_at": pc._dt(_sa),
                "started_at_iso": (_sa.strftime("%Y-%m-%dT%H:%M:%S") if _sa else ""),
                "deadline": pc._dt(_dl),
                "deadline_iso": (_dl.strftime("%Y-%m-%dT%H:%M:%S") if _dl else ""),
            })
    whole = [{"project_id": t.id, "title": t.title or t.subject or "Project",
              "subject": t.subject or "", "kind": t.kind, "deadline": pc._dt(t.deadline)}
             for t in proj_rows if getattr(t, "project_editor_id", None) == sp.id]
    return {"videos": vids, "whole_projects": whole,
            "count": len(vids), "whole_count": len(whole)}


def _my_pv_chapter(db, sp, cid):
    from models import VideoTaskChapter as _VC
    c = db.query(_VC).filter(_VC.id == int(cid or 0)).first()
    if not c or c.editor_id != sp.id:
        raise HTTPException(404, "Assigned video not found")
    return c


@router.post("/project-videos/{cid}/start")
def editor_pv_start(cid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    from video_tasks import set_chapter_state
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    set_chapter_state(db, c, "editing", actor=me, note="Editing started", force=True)
    if not c.editing_started_at:
        c.editing_started_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "edit_state": c.edit_state, "lifecycle": c.lifecycle}


def _pv_clamp_pct(v):
    try:
        v = int(v)
    except Exception:
        v = 0
    return max(0, min(100, v))


@router.post("/project-videos/{cid}/progress")
def editor_pv_progress(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                       me=Depends(get_editor)):
    """Update editing % for a project video (keeps it in the 'editing' state)."""
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    c.editing_progress = _pv_clamp_pct(payload.get("progress"))
    _note = (payload.get("remarks") or "").strip()
    if _note:
        c.progress_note = _note[:400]
    if (c.edit_state or "") not in ("editing", "paused"):
        c.edit_state = "editing"
        c.lifecycle = "editing"
    if not c.editing_started_at:
        c.editing_started_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "editing_progress": c.editing_progress, "edit_state": c.edit_state}


@router.post("/project-videos/{cid}/pause")
def editor_pv_pause(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                    me=Depends(get_editor)):
    """Pause editing — the editor must set how much % is done + a short remark (like a task)."""
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    if "progress" not in payload or str(payload.get("progress")).strip() == "":
        raise HTTPException(400, "Set how much editing is done (%) before pausing.")
    rem = (payload.get("remarks") or "").strip()
    if not rem:
        raise HTTPException(400, "Add a short remark about what is done.")
    from video_tasks import set_chapter_state
    c.editing_progress = _pv_clamp_pct(payload.get("progress"))
    c.progress_note = rem[:400]
    set_chapter_state(db, c, "editing_paused", actor=me,
                      note="Paused at %d%% — %s" % (c.editing_progress, rem), force=True)
    if not c.editing_started_at:
        c.editing_started_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "edit_state": c.edit_state, "editing_progress": c.editing_progress}


@router.post("/project-videos/{cid}/resume")
def editor_pv_resume(cid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    from video_tasks import set_chapter_state
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    set_chapter_state(db, c, "editing", actor=me, note="Editing resumed", force=True)
    if not c.editing_started_at:
        c.editing_started_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "edit_state": c.edit_state}


@router.post("/project-videos/{cid}/submit")
def editor_pv_submit(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                     me=Depends(get_editor)):
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    link = (payload.get("edited_link") or "").strip()
    if not link:
        raise HTTPException(400, "Edited video drive link is required")
    from video_tasks import set_chapter_state
    _re = (getattr(c, "qc_status", "") or "") in ("changes",)  # re-submit after QC changes?
    c.edited_link = link
    c.edited_at = datetime.utcnow()
    c.editing_progress = 100
    c.edit_review_status = "pending"      # teacher must re-check the fresh edit
    c.edit_reviewer_name = ""
    if _re:
        try: c.qc_revision = int(getattr(c, "qc_revision", 0) or 0) + 1
        except Exception: c.qc_revision = 1
    # ---- atomic transition: edited video submitted -> QC pending (NOT ready_for_youtube) ----
    set_chapter_state(db, c, "qc_pending", actor=me, note="Edited video submitted for QC", force=True)
    t = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
    proj = (t.title or t.subject or "project") if t else "project"
    _msg = f'{me.name} submitted the edited "{c.title}" from "{proj}". Please review.'
    try:
        pc.notify_pms(db, "Edited video ready for QC", _msg, "production", link=str(c.task_id))
    except Exception:
        pass
    # notify the project's teacher(s) so they can check the edit too
    try:
        for _uid in _project_teacher_user_ids(db, t):
            pc.notify(db, _uid, "Edited video ready — please review",
                      f'The edited "{c.title}" is ready. Watch and approve or request changes.',
                      "video_review", link=str(c.task_id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "edit_state": c.edit_state, "qc_status": c.qc_status, "edited_link": link}


def _project_teacher_user_ids(db, t):
    """User ids of the project's teacher + any collaborating teachers (for review notifications)."""
    if not t:
        return []
    from models import TeacherProfile as _TP
    ids = set()
    tids = []
    if getattr(t, "teacher_id", None):
        tids.append(t.teacher_id)
    try:
        from video_tasks import _collab_all_ids as _cai
        tids = list(_cai(t)) or tids
    except Exception:
        pass
    for tpid in tids:
        tp = db.query(_TP).filter(_TP.id == tpid).first()
        if tp and getattr(tp, "user_id", None):
            ids.add(tp.user_id)
    return list(ids)


@router.post("/project-videos/{cid}/reopen")
def editor_pv_reopen(cid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    from video_tasks import set_chapter_state, _chapter_lifecycle
    sp = _me_staff(db, me)
    c = _my_pv_chapter(db, sp, cid)
    # don't let an editor silently re-open a video that already passed QC / was published
    if _chapter_lifecycle(c) in ("ready_for_youtube", "uploaded", "completed"):
        raise HTTPException(400, "This video already passed QC — ask the PM to request changes to re-open it.")
    set_chapter_state(db, c, "editing", actor=me, note="Re-opened for editing", force=True)
    db.commit()
    return {"ok": True, "edit_state": c.edit_state}


def _editor_in_project(db, sp, pid):
    from models import VideoTaskChapter as _VC
    t = db.query(VideoTask).filter(VideoTask.id == int(pid or 0)).first()
    if not t:
        return False
    if getattr(t, "project_editor_id", None) == sp.id:
        return True
    return db.query(_VC).filter(_VC.task_id == int(pid), _VC.editor_id == sp.id).first() is not None


@router.get("/projects/{pid}/chat")
def editor_project_chat(pid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    if not _editor_in_project(db, sp, pid):
        raise HTTPException(403, "Not assigned to this project")
    from video_tasks import project_chat_get
    return project_chat_get(db, me, pid)


@router.post("/projects/{pid}/chat")
def editor_project_chat_add(pid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                            me=Depends(get_editor)):
    sp = _me_staff(db, me)
    if not _editor_in_project(db, sp, pid):
        raise HTTPException(403, "Not assigned to this project")
    from video_tasks import project_chat_add
    return project_chat_add(db, me, pid, payload, "editor")


@router.post("/projects/{pid}/chat-ping")
def editor_project_chat_ping(pid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                             me=Depends(get_editor)):
    sp = _me_staff(db, me)
    if not _editor_in_project(db, sp, pid):
        raise HTTPException(403, "Not assigned to this project")
    from video_tasks import project_chat_ping
    return project_chat_ping(db, me, pid, typing=bool((payload or {}).get("typing")))


def _assert_editor_chapter(db, me, cid):
    """AUTHZ: an editor may touch a chapter ONLY if it is explicitly their chapter OR they are the
    whole-project editor (inherited assignment). Owning a DIFFERENT chapter in the same project is
    NOT enough — that would leak an unrelated co-editor's chapter. (Matches the central chat guard.)"""
    from models import VideoTaskChapter as _VC
    sp = _me_staff(db, me)
    c = db.query(_VC).filter(_VC.id == int(cid or 0)).first()
    if not c:
        raise HTTPException(404, "Chapter not found")
    if getattr(c, "editor_id", None) == sp.id:
        return c
    t = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
    if t and getattr(t, "project_editor_id", None) == sp.id:
        return c
    raise HTTPException(403, "You don't have access to this chapter")


# ===== Phase 2c: editor PER-CHAPTER chat + timeline =====
@router.get("/chapters/{cid}/chat")
def editor_chapter_chat(cid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    _assert_editor_chapter(db, me, cid)
    from video_tasks import chapter_chat_get
    return chapter_chat_get(db, me, cid)


@router.post("/chapters/{cid}/chat")
def editor_chapter_chat_add(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                            me=Depends(get_editor)):
    _assert_editor_chapter(db, me, cid)
    from video_tasks import chapter_chat_add
    return chapter_chat_add(db, me, cid, payload, "editor")


@router.post("/chapters/{cid}/chat-ping")
def editor_chapter_chat_ping(cid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                             me=Depends(get_editor)):
    _assert_editor_chapter(db, me, cid)
    from video_tasks import chapter_chat_ping
    return chapter_chat_ping(db, me, cid, typing=bool((payload or {}).get("typing")))


@router.get("/chapters/{cid}/timeline")
def editor_chapter_timeline(cid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    _assert_editor_chapter(db, me, cid)
    from video_tasks import chapter_timeline
    return chapter_timeline(db, cid)



def _my_task(db, sp, tid):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
    if not t:
        raise HTTPException(404, "Task not found")
    # primary editor OR a collaborator (2-editor urgent video) can access
    if not pc.editor_can_access(t, sp.id):
        raise HTTPException(403, "This task is not assigned to you")
    return t


# ===== EDITOR: VIDEO REVIEW CHAT (teacher checks the edited video, editor replies) =====
@router.get("/tasks/{tid}/review-chat")
def editor_review_chat_get(tid: int, db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    _my_task(db, sp, tid)
    from video_tasks import _vtc_list_v, _vtc_mark_read, _chat_touch, _chat_other_presence
    _vtc_mark_read(db, me, tid, "review")
    _chat_touch(db, me, tid, "review")
    return {"comments": _vtc_list_v(db, tid, "review", getattr(me, "id", None)),
            "presence": _chat_other_presence(db, getattr(me, "id", None), tid, "review")}


@router.post("/tasks/{tid}/review-chat")
def editor_review_chat_add(tid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                           me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    from video_tasks import _review_chat_add, _chat_touch, _collab_all_ids
    msg = (payload.get("message") or "").strip()
    imgs = payload.get("images") or ([payload.get("attachment")] if payload.get("attachment") else [])
    att = (payload.get("attachment_url") or "").strip()
    comments = _review_chat_add(db, me, tid, msg, imgs, "editor", attachment_url=att)
    if not comments:
        raise HTTPException(400, "Message cannot be empty")
    try: _chat_touch(db, me, tid, "review", typing=False)
    except Exception: pass
    # notify teachers (creator + collab) and PMs — the review chat is PM/admin visible
    try:
        from models import TeacherProfile as _TP
        _snip = (msg or "📷 screenshot")[:110]
        for teach_id in _collab_all_ids(t):
            tp = db.query(_TP).filter(_TP.id == teach_id).first()
            if tp and tp.user_id:
                pc.notify(db, tp.user_id, "Editor replied on your video review",
                          f'Reply on "{t.title}": {_snip}', "video_review", link=str(tid))
        pc.notify_pms(db, "Editor replied on video review",
                      f'{getattr(me, "name", "Editor")} on "{t.title}": {_snip}',
                      "video_review", link=str(tid))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "comments": comments}


@router.post("/tasks/{tid}/review-chat-ping")
def editor_review_chat_ping(tid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                            me=Depends(get_editor)):
    sp = _me_staff(db, me)
    _my_task(db, sp, tid)
    from video_tasks import _chat_touch, _chat_other_presence
    _chat_touch(db, me, tid, "review", typing=bool((payload or {}).get("typing")))
    return {"presence": _chat_other_presence(db, getattr(me, "id", None), tid, "review")}


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

    # legacy/admin-assigned tasks: lifecycle blank par status set (editing_soon/approved) —
    # inhe bhi "to start" me count karo warna dashboard 0 dikhata hai jabki My Tasks me task hai
    _legacy_ready = and_(or_(VideoTask.lifecycle == None, VideoTask.lifecycle == "",
                             VideoTask.lifecycle == "creator_assigned"),
                         VideoTask.status.in_(["editing_soon", "approved"]))

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
        _tkdl = getattr(tk, "editor_deadline", None) or tk.deadline
        if _tkdl and tk.editing_done_at and tk.editing_done_at <= _tkdl:
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
        "assigned_today": base.filter(or_(VideoTask.lifecycle.in_(["editor_assigned", "editing_soon", "approved"]), _legacy_ready)).count(),
        "editing_now": c("editing", "editing_paused"),
        "due_soon": base.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline >= now,
                                VideoTask.editor_deadline <= soon,
                                ~VideoTask.lifecycle.in_(_not_done)).count(),
        "overdue": base.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline < now,
                               ~VideoTask.lifecycle.in_(_not_done)).count(),
        "submitted": c("qc_pending"),
        "changes": c("qc_changes"),
        "completed": c("ready_for_youtube", "uploaded", "completed"),
        "ready_for_youtube": c("ready_for_youtube"),
        "total_views": int(total_views),
    }
    # Phase 2a: include this editor's PROJECT-CHAPTER work in their own dashboard counts
    try:
        from models import VideoTaskChapter as _VCe
        _cb = db.query(_VCe).filter(_VCe.editor_id == sp.id)
        cards["assigned_today"] += _cb.filter(_VCe.edit_state.in_(["", "assigned"])).count()
        cards["editing_now"] += _cb.filter(_VCe.edit_state.in_(["editing", "paused"])).count()
        cards["completed"] += _cb.filter(_VCe.edit_state == "edited").count()
    except Exception:
        pass
    return {
        "greeting_name": me.name,
        "events": pc.active_events_for(db, "editor"),
        "appreciation": {"ontime_pct": ontime_pct, "avg_rating": avg_rating,
                         "badges": badges, "total_done": total_done, "rank": rank},
        "cards": cards,
        "kpis": {
            "assigned": base.filter(or_(VideoTask.lifecycle == "editor_assigned", _legacy_ready)).count(),
            "not_started": base.filter(or_(VideoTask.lifecycle == "editor_assigned", _legacy_ready)).count(),
            "editing": c("editing", "editing_paused"),
            "qc_pending": c("qc_pending"),
            "changes": c("qc_changes"),
            "completed": c("ready_for_youtube", "uploaded", "completed"),
            "due_today": base.filter(VideoTask.editor_deadline != None,
                                     func.date(VideoTask.editor_deadline) == today,
                                     ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube"])).count(),
            "overdue": base.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline < now,
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
    from sqlalchemy import or_ as _or
    sp = _me_staff(db, me)
    # primary editor OR a collaborator (broad LIKE narrows candidates; exact filter below)
    q = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                   _or(VideoTask.editor_id == sp.id,
                                       VideoTask.collab_editor_ids.like('%' + str(sp.id) + '%')))
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
        # editor is judged against the EDITOR deadline, not the teacher deadline
        q = q.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline < now,
                     ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube", "qc_pending"]))
    if status:
        if status == "editor_assigned":
            q = q.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing_soon", "approved"]))
        else:
            q = q.filter(VideoTask.lifecycle == status)
    rows = q.options(defer(VideoTask.thumbnail_b64)).order_by(VideoTask.updated_at.desc()).all()
    # LIKE can over-match (12 vs 120) -> exact membership check
    rows = [t for t in rows if pc.editor_can_access(t, sp.id)]
    _ccm = pc.comment_count_map(db, [t.id for t in rows])
    _tm = pc.thumb_map_for(db, [t.id for t in rows])
    _outs = [pc.task_out(db, t, light=True, viewer="editor", comment_count=_ccm.get(t.id, 0), thumb_map=_tm) for t in rows]
    try:
        from video_tasks import _vtc_unread_bulk
        _un = _vtc_unread_bulk(db, getattr(me, "id", None), [t.id for t in rows])
        for _o in _outs:
            _o["unread_total"] = (_un.get(_o.get("id"), {}) or {}).get("editor", 0)
    except Exception:
        pass
    return {"tasks": _outs}


@router.get("/review-alerts")
def editor_review_alerts(db: Session = Depends(get_db), me=Depends(get_editor)):
    """Edited videos where the TEACHER requested changes — drives the dashboard popup."""
    from sqlalchemy import or_ as _or
    sp = _me_staff(db, me)
    rows = db.query(VideoTask).filter(
        VideoTask.cancelled == False,
        _or(VideoTask.editor_id == sp.id, VideoTask.collab_editor_ids.like('%' + str(sp.id) + '%')),
        VideoTask.teacher_review_status == "changes",
        VideoTask.lifecycle.in_(["qc_pending", "qc_changes"])).all()
    rows = [t for t in rows if pc.editor_can_access(t, sp.id)]
    out = [{"id": t.id, "title": t.title or "",
            "teacher_reviewer_name": (getattr(t, "teacher_reviewer_name", "") or ""),
            "teacher_review_note": (getattr(t, "teacher_review_note", "") or "")} for t in rows]
    return {"alerts": out}


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
    _VT._chat_touch(db, me, tid, "editor")
    return {"comments": _VT._vtc_list_v(db, tid, "editor", getattr(me, "id", None)),
            "presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, "editor")}

@router.get("/tasks/{tid}/party-tasks")
def editor_party_tasks(tid: int, audience: str = "editor", db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    sp = _me_staff(db, me); _my_task(db, sp, tid)
    return {"tasks": _VT._chat_party_tasks(db, tid, (audience or "editor"))}


@router.post("/heartbeat")
def editor_heartbeat(payload: dict = Body(default={}), db: Session = Depends(get_db),
                     me=Depends(get_editor)):
    pc.touch_session(db, me, (payload or {}).get("page"), bool((payload or {}).get("active")))
    import video_tasks as _VT
    _VT._chat_touch_global(db, me)
    return {"ok": True}


@router.get("/chat/inbox")
def editor_chat_inbox(db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    _VT._chat_touch_global(db, me)
    return {"conversations": _VT._chat_inbox(db, me, "editor")}


# ---- Editor DIRECT-PAIR chats: editor<->teacher (te_ed), editor<->graphics (ed_gf) ----
@router.get("/tasks/{tid}/pair-comments")
def editor_pair_get(tid: int, audience: str = "", db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    aud = (audience or "").strip().lower()
    if not _VT._pair_check("editor", aud):
        raise HTTPException(400, "Invalid conversation")
    sp = _me_staff(db, me); _my_task(db, sp, tid)
    _VT._vtc_mark_read(db, me, tid, aud); _VT._chat_touch(db, me, tid, aud)
    return {"comments": _VT._vtc_list_v(db, tid, aud, getattr(me, "id", None)),
            "presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, aud)}


@router.post("/tasks/{tid}/pair-comments")
def editor_pair_add(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    from models import VideoTask
    aud = (payload.get("audience") or "").strip().lower()
    if not _VT._pair_check("editor", aud):
        raise HTTPException(400, "Invalid conversation")
    sp = _me_staff(db, me); _my_task(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == tid).first()
    if not t:
        raise HTTPException(404, "Task not found")
    _att = _VT._resolve_chat_att(db, t, payload, me)
    c = _VT._vtc_add(db, tid, me, payload.get("message"), "editor", attachment_url=_att, audience=aud, ref_task_id=payload.get("ref_task_id"))
    if not c:
        raise HTTPException(400, "Message cannot be empty")
    try: _VT._chat_touch(db, me, tid, aud, typing=False)
    except Exception: pass
    _VT._pair_notify(db, tid, aud, me, c.message)
    db.commit()
    return {"ok": True, "comment": _VT._vtc_out(db, c)}


@router.post("/tasks/{tid}/pair-ping")
def editor_pair_ping(tid: int, audience: str = "", payload: dict = Body(default={}),
                     db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    aud = (audience or (payload or {}).get("audience") or "").strip().lower()
    if _VT._pair_check("editor", aud):
        _VT._chat_touch(db, me, tid, aud, typing=bool((payload or {}).get("typing")))
    return {"presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, aud)}


@router.post("/tasks/{tid}/chat-ping")
def editor_chat_ping(tid: int, payload: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_editor)):
    import video_tasks as _VT
    _VT._chat_touch(db, me, tid, "editor", typing=bool((payload or {}).get("typing")))
    return {"presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, "editor")}


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
    c = _VT._vtc_add(db, tid, me, payload.get("message") or "", "editor", attachment_url=att, audience="editor", ref_task_id=payload.get("ref_task_id"))
    try: _VT._chat_touch(db, me, tid, "editor", typing=False)
    except Exception: pass
    if not c:
        raise HTTPException(400, "Empty message")
    # notify the PM/admins so the editor's message shows on their side
    try:
        pc.notify_pms(db, "Editor messaged you on a video",
                      f'{me.name} on "{t.title}": {(payload.get("message") or "").strip()[:100]}',
                      "editor_chat", link=str(t.id))
    except Exception:
        pass
    # youtuber videos: also notify the youtuber (creator)
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
    # legacy/admin task: lifecycle blank par status editing_soon/approved (video submitted) — allow
    _legacy_ready = ((t.lifecycle or "") in ("", "creator_assigned")
                     and (t.status or "") in ("editing_soon", "approved") )
    if t.lifecycle not in ("editor_assigned", "editing_soon", "approved", "editing_paused", "qc_changes") and not _legacy_ready:
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
def editor_pause(tid: int, payload: dict = Body(default={}),
                 db: Session = Depends(get_db), me=Depends(get_editor)):
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    # Normally pause tabhi jab actively editing ho. LEKIN agar PM ne is task ko pause karne ko
    # bola hai (pause_req) to kisi bhi editing-stage se pause allow karo (urgent flow).
    _has_req = bool(getattr(t, "pause_req", False))
    if t.lifecycle != "editing" and not (_has_req and t.lifecycle in ("editor_assigned", "editing_soon", "editing_paused", "approved")):
        raise HTTPException(400, "Editing is not currently active")
    payload = payload or {}
    # progress % at pause time (optional but nudged in the UI)
    _pct = None
    if payload.get("progress") is not None and str(payload.get("progress")).strip() != "":
        try:
            _pct = max(0, min(100, int(payload.get("progress"))))
            t.editing_progress = _pct
        except Exception:
            _pct = None
    _rem = (payload.get("remarks") or "").strip()[:300]
    _close_open_session(db, sp, t)
    pc.set_state(db, t, "editing_paused", actor=me, event="editing_paused", force=True)
    # ---- Urgent pause-request fulfil: PM ka diya naya editor deadline lagao + request clear ----
    if getattr(t, "pause_req", False):
        try:
            if getattr(t, "pause_req_deadline", None):
                t.editor_deadline = t.pause_req_deadline
                t.warned_24h = False
                t.warned_overdue = False
            _urgent_id = getattr(t, "pause_req_urgent_id", None)
            t.pause_req = False
            t.pause_req_deadline = None
            t.pause_req_by = ""
            t.pause_req_urgent_id = None
            t.pause_req_at = None
            pc.notify_pms(db, "Task paused (urgent)",
                          f'Editor paused "{t.title}" for the urgent task.'
                          + (f' New deadline: {t.editor_deadline.strftime("%d %b %Y, %I:%M %p")}.' if t.editor_deadline else ""),
                          link=str(t.id))
        except Exception:
            pass
    # log the pause with progress + remarks so it shows in the progress history / timeline
    try:
        _meta = {}
        if _pct is not None:
            _meta["progress"] = _pct
        if _rem:
            _meta["note"] = _rem
        pc.log_event(db, t, me, "progress_updated", new_state=t.lifecycle, meta=_meta)
    except Exception:
        pass
    db.commit()
    return {"ok": True, "editing_seconds": t.editing_seconds or 0, "progress": t.editing_progress or 0}


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
    # teacher (creator/collab) ko batao ki edited video review ke liye ready hai.
    # fresh/revised cut -> purana teacher review reset, taaki nayi video dubara check ho.
    try:
        if (getattr(t, "creator_type", "") or "teacher") != "youtuber":
            t.teacher_review_status = ""
            t.teacher_reviewed_at = None
            t.teacher_reviewed_by = None
            from models import TeacherProfile as _TP
            from video_tasks import _collab_all_ids as _cai
            _word = "revised" if is_revision else "edited"
            for teach_id in _cai(t):
                tp = db.query(_TP).filter(_TP.id == teach_id).first()
                if tp and tp.user_id:
                    pc.notify(db, tp.user_id, "Video ready for your review",
                              f'The {_word} "{t.title}" is ready — please check and Approve or ask for Changes.',
                              "video_review", link=str(t.id))
    except Exception:
        pass
    # on-time appreciation (§23) — one positive nudge, only once, only on an on-time submission
    try:
        _edl = getattr(t, "editor_deadline", None) or t.deadline
        if _edl and (not is_revision) and (not t.ontime_appreciated) and datetime.utcnow() <= _edl:
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


def _perf_legacy_bucket(p, yt_views=0):
    """Map the canonical engine category result to the legacy response keys the current
    frontend reads, while also passing through the new rich fields."""
    return {
        "videos_edited": p["edited"], "videos_approved": p["approved"],
        "videos_uploaded": p["published"], "pending": p["pending"], "overdue": p["overdue"],
        "revision_count": p["revisions"],
        "avg_turnaround_hours": p["avg_turnaround"] or 0,
        "on_time_pct": p["on_time_pct"] if p["on_time_pct"] is not None else 0,
        "avg_quality": p["avg_quality"] if p["avg_quality"] is not None else 0,
        "youtube_views": yt_views,
        # ---- new canonical fields (Phase-2 UI reads these) ----
        "edited": p["edited"], "approved": p["approved"], "published": p["published"],
        "first_pass_pct": p["first_pass_pct"], "avg_turnaround": p["avg_turnaround"],
        "score": p["score"], "score_breakdown": p["score_breakdown"],
        "provisional": p["provisional"], "sample": p["sample"], "target": p["target"],
        "normal_work": p["source_normal"], "project_work": p["source_project"],
    }


@router.get("/performance")
def editor_performance(period: str = "month", db: Session = Depends(get_db), me=Depends(get_editor)):
    """Canonical editor performance (performance_core) — counts BOTH normal VideoTasks
    and project VideoTaskChapters, no double-counting, long/short split, 100-pt score,
    server-authoritative rank + movement from persistent snapshots. Real data only."""
    sp = _me_staff(db, me)
    now = datetime.utcnow()
    try:
        PC.maybe_daily_snapshot(db, now)   # self-healing daily rank history for ALL staff
    except Exception:
        pass
    items = PC.get_editor_work_items(db, sp.id, now=now)
    perf = PC.compute_editor_performance(sp, items, period, ref=now)

    # total YouTube views (keep the legacy KPI alive — engine is item-state based)
    try:
        yt_views = sum(int(t.yt_views or 0) for t in
                       db.query(VideoTask).filter(VideoTask.cancelled == False,  # noqa: E712
                                                  VideoTask.editor_id == sp.id).all())
    except Exception:
        yt_views = 0

    ov = perf["overall"]
    # overall is a roll-up (no single score breakdown) — build its legacy shape directly
    _oq = [p["avg_quality"] for p in (perf["long"], perf["short"]) if p["avg_quality"] is not None]
    _oot = [p["on_time_pct"] for p in (perf["long"], perf["short"]) if p["on_time_pct"] is not None]
    overall = {
        "videos_edited": ov["edited"], "videos_approved": ov["approved"],
        "videos_uploaded": ov["published"], "pending": ov["pending"], "overdue": ov["overdue"],
        "revision_count": ov["revisions"],
        "avg_turnaround_hours": 0,
        "on_time_pct": round(sum(_oot) / len(_oot)) if _oot else 0,
        "avg_quality": round(sum(_oq) / len(_oq), 1) if _oq else 0,
        "youtube_views": yt_views,
        "edited": ov["edited"], "approved": ov["approved"], "published": ov["published"],
        "score": ov["score"], "normal_work": ov["normal_work"], "project_work": ov["project_work"],
    }
    long_b = _perf_legacy_bucket(perf["long"])
    short_b = _perf_legacy_bucket(perf["short"])

    # charts
    start, end, _ = PC.period_bounds(period, now)
    completed = [it for it in items if it["edited"] and it["completed_at"]]
    trend = []
    for i in range(5, -1, -1):
        m = (now.month - i - 1) % 12 + 1
        y = now.year + ((now.month - i - 1) // 12)
        mm0 = datetime(y, m, 1)
        mm1 = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        trend.append({"label": mm0.strftime("%b"),
                      "value": sum(1 for it in completed if mm0 <= it["completed_at"] < mm1)})
    donut = [
        {"label": "Editing", "value": sum(1 for it in items if it["lifecycle"] in ("editing", "editing_paused"))},
        {"label": "In QC", "value": sum(1 for it in items if it["lifecycle"] in ("qc_pending", "qc_changes"))},
        {"label": "Approved", "value": perf["overall"]["approved"]},
        {"label": "Published", "value": perf["overall"]["published"]},
    ]

    # ---- server-authoritative leaderboards + rank + movement (per category) ----
    cat = perf["primary_category"]
    lb_long = PC.compute_editor_leaderboard(db, PC.CAT_LONG, period, ref=now)
    lb_short = PC.compute_editor_leaderboard(db, PC.CAT_SHORT, period, ref=now)
    my_lb = lb_long if cat == PC.CAT_LONG else lb_short
    rank, total = PC.find_rank(my_lb, sp.id)
    snap_cat = "editor_long" if cat == PC.CAT_LONG else "editor_short"
    if rank:
        PC.save_rank_snapshot(db, sp.id, "editor", snap_cat, rank,
                              (perf[cat]["score"] or 0), total, ref=now)
    movement = PC.rank_movement(db, sp.id, snap_cat, rank, ref=now)

    def _lb_cards(rows):
        out = []
        for r in rows[:5]:
            out.append({"name": r["name"], "approved": r["approved"], "edited": r["edited"],
                        "score": r["score"], "rank": r["rank"], "me": (r["staff_id"] == sp.id),
                        "avg_quality": r["avg_quality"], "on_time_pct": r["on_time_pct"],
                        "provisional": r["provisional"], "category": r["category"]})
        return out
    ranking = _lb_cards(my_lb)

    return {
        # legacy keys (current UI)
        "overall": overall, "long": long_b, "short": short_b,
        "charts": {"bar": [{"label": "Edited", "value": overall["videos_edited"]},
                           {"label": "Approved", "value": overall["videos_approved"]},
                           {"label": "Uploaded", "value": overall["videos_uploaded"]},
                           {"label": "Pending", "value": overall["pending"]},
                           {"label": "Delayed", "value": overall["overdue"]}],
                   "donut": donut, "trend": trend},
        "rank": rank or 0, "ranking": ranking,
        # new canonical keys (Phase-2 UI)
        "period": perf["period"], "specialization": perf["specialization"],
        "primary_category": cat, "score": perf["overall"]["score"],
        "rank_movement": movement,
        "leaderboard_long": _lb_cards(lb_long), "leaderboard_short": _lb_cards(lb_short),
        "rank_trend": PC.rank_trend(db, sp.id, snap_cat, 30, ref=now),
        "badges": PC.editor_badges(perf),
        "total_ranked": total,
        "personal_bests": PC.personal_bests(db, sp.id, snap_cat, ref=now),
    }


@router.get("/performance/items")
def editor_performance_items(period: str = "month", category: str = "", filter: str = "edited",
                             db: Session = Depends(get_db), me=Depends(get_editor)):
    """Drill-down list behind a clickable performance metric (perf §31). Own data only."""
    sp = _me_staff(db, me)
    items = PC.get_editor_work_items(db, sp.id)
    rows = PC.filter_work_items(items, period, category, filter)
    return {"items": [PC.item_dto(it) for it in rows], "count": len(rows),
            "filter": filter, "category": category, "period": period}


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


@router.post("/tasks/{tid}/edit")
def editor_edit_task(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_editor)):
    """Editor apne assigned task ke universal fields edit kare (title/deadline/priority/remarks)."""
    sp = _me_staff(db, me)
    t = _my_task(db, sp, tid)
    pc.edit_task_fields(db, t, payload, me)
    db.commit()
    return {"ok": True}
