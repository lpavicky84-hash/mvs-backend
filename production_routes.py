"""Production Manager API (/api/production).

The PM is the operational owner. Admin also has access (oversight). Every mutation
is authorised server-side and updates the shared state engine in production_core.
"""
from fastapi import APIRouter, Depends, HTTPException, Body, Response
import json
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, or_, and_
from datetime import datetime, date, timedelta

from database import get_db
from security import get_pm_or_admin
from models import (
    User, UserRole, VideoTask, GraphicsTask, EditingSession, ProductionEvent,
    TaskReview, YouTuberProfile, ProductionStaffProfile, TeacherProfile, Notification,
    ist_now,
)
import production_core as pc

router = APIRouter(prefix="/api/production", tags=["Production"])


def _task(db, tid):
    t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return t


def _apply_pause_request(db, urgent_task, editor_id, payload, me):
    """PM/admin ne urgent editor-task dete waqt editor ke ek ACTIVE task ko pause + naya deadline
    set kiya. Us active task par pause_req flag laga do + editor ko notify. assign-editor AUR
    edit-task DONO isko call karte hain — kahi se bhi editor assign karo, same premium behaviour."""
    try:
        _pause_tid = int(payload.get("pause_task_id") or 0)
    except Exception:
        _pause_tid = 0
    if not _pause_tid or not editor_id or _pause_tid == getattr(urgent_task, "id", 0):
        return
    _pt = db.query(VideoTask).filter(VideoTask.id == _pause_tid,
                                     VideoTask.editor_id == editor_id).first()
    if not _pt:
        return
    _pdl_raw = (payload.get("pause_deadline") or payload.get("pause_new_deadline") or "").strip()
    _pdl = None
    if _pdl_raw:
        try:
            _pdl = datetime.fromisoformat(_pdl_raw.replace("Z", ""))
        except Exception:
            _pdl = None
    _pt.pause_req = True
    _pt.pause_req_deadline = _pdl
    _pt.pause_req_by = (getattr(me, "name", "") or ("Admin" if getattr(me, "role", "") == "admin" else "Production Manager"))
    _pt.pause_req_urgent_id = urgent_task.id
    _pt.pause_req_at = datetime.utcnow()
    try:
        pc.log_event(db, _pt, me, "pause_requested", new_state=_pt.lifecycle,
                     meta={"note": "Pause requested for urgent task", "urgent_id": urgent_task.id,
                           "new_deadline": (_pdl.strftime("%d %b %Y, %I:%M %p") if _pdl else "")})
    except Exception:
        pass
    _ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == editor_id).first()
    if _ed and _ed.user_id:
        _dlmsg = (f' New deadline: {_pdl.strftime("%d %b %Y, %I:%M %p")}.' if _pdl else "")
        pc.notify(db, _ed.user_id, "⚡ Urgent — pause your current task",
                  f'Please PAUSE "{_pt.title}" and start the urgent video "{urgent_task.title}".{_dlmsg}',
                  "video_task", link=str(_pt.id))


def _apply_new_deadline(t, payload, require=False):
    """Set a new deadline while PRESERVING the old one (returned for the timeline).
    History is never overwritten — the previous deadline is recorded in the event meta."""
    from datetime import datetime as _dt
    old_dl = t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else ""
    raw = (payload.get("new_deadline") or payload.get("deadline") or "").strip()
    if not raw:
        if require:
            raise HTTPException(400, "A new deadline is required")
        return old_dl, ""
    try:
        nd = _dt.fromisoformat(raw.replace("Z", ""))
    except Exception:
        raise HTTPException(400, "Invalid new deadline")
    t.deadline = nd
    try:
        pc.recompute_on_time(t)   # deadline change ke saath on_time bhi refresh
    except Exception:
        pass
    return old_dl, nd.strftime("%d %b %Y, %I:%M %p")


# ============================================================ DASHBOARD
@router.get("/dashboard")
def pm_dashboard(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    today = date.today()
    # Pipeline KPIs (aur nav badges) SINGLE-VIDEO tasks ke liye hain — projects (one_shot /
    # rapid_revision / project) apne section me count hote hain. Isliye special kinds yahan
    # se hata do, warna PM Review list khali dikhe par badge me count aa jaata tha.
    _NS = or_(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")
    q = db.query(VideoTask).filter(VideoTask.cancelled == False, _NS)

    def c(*states):
        return q.filter(VideoTask.lifecycle.in_(states)).count()

    active_states = ["creator_assigned", "creator_working", "creator_submitted",
                     "pm_review", "approved", "editor_assigned", "editing",
                     "editing_paused", "editing_done", "qc_pending", "qc_changes",
                     "ready_for_youtube", "changes_required"]
    _live = db.query(VideoTask.id).filter(VideoTask.cancelled == False)
    kpis = {
        "active": q.filter(VideoTask.is_old == False, or_(
                     VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"]),
                     and_(or_(VideoTask.lifecycle == None, VideoTask.lifecycle == ""),
                          VideoTask.status.in_(["assigned", "reshoot", "rejected", "new", "in_progress"])))).count(),
        "teacher_pending": q.filter(VideoTask.creator_type == "teacher",
                                    VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
        "youtuber_pending": q.filter(VideoTask.creator_type == "youtuber",
                                     VideoTask.lifecycle.in_(["creator_assigned", "creator_working", "changes_required"])).count(),
        "pm_review": q.filter(or_(VideoTask.lifecycle.in_(["creator_submitted", "pm_review"]),
                                  VideoTask.status == "submitted")).count(),
        "thumb_review": db.query(GraphicsTask).filter(GraphicsTask.status == "submitted",
                                                      GraphicsTask.task_id.in_(_live)).count(),
        "thumb_changes": db.query(GraphicsTask).filter(GraphicsTask.status == "changes",
                                                       GraphicsTask.task_id.in_(_live)).count(),
        "editing": c("editing", "editing_paused", "editing_done"),
        "graphics": db.query(GraphicsTask).filter(GraphicsTask.status.in_(["in_progress", "submitted"]),
                                                  GraphicsTask.task_id.in_(_live)).count(),
        "qc_pending": c("qc_pending"),
        "ready_for_youtube": c("ready_for_youtube"),
        "due_today": 0,   # stage-aware, neeche compute hota hai
        "overdue": 0,     # stage-aware, neeche compute hota hai
    }
    # ---- DELAYED / DUE TODAY: shared stage-aware helper (Tasks list ka 'Delayed' filter bhi
    # yahi helper use karta hai -> KPI count aur list HAMESHA barabar).
    _ovd_ids, _tod_ids = pc.overdue_today_ids(db)
    kpis["overdue"] = len(_ovd_ids)
    kpis["due_today"] = len(_tod_ids)
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
    # ---- Pending YouTube links: scheduled upload time has passed but the link isn't posted yet.
    # upload_date is stored as IST-local (naive), so compare against IST "now", not UTC.
    now_ist = now + timedelta(hours=5, minutes=30)
    pend_rows = (db.query(VideoTask).options(defer(VideoTask.thumbnail_b64))
                 .filter(VideoTask.cancelled == False,
                         VideoTask.upload_date != None,          # noqa: E711
                         VideoTask.upload_date <= now_ist,
                         or_(VideoTask.youtube_url == None, VideoTask.youtube_url == ""),  # noqa: E711
                         or_(VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded"]),
                             VideoTask.lifecycle == None, VideoTask.lifecycle == ""))       # noqa: E711
                 .order_by(VideoTask.upload_date.asc()).all())
    _pend_tm = pc.thumb_map_for(db, [t.id for t in pend_rows])
    pending_yt = [pc.task_out(db, t, light=True, thumb_map=_pend_tm) for t in pend_rows]
    return {"greeting_name": me.name, "date": today.isoformat(),
            "kpis": kpis, "secondary": secondary,
            "events": pc.active_events_for(db, "all"),
            "pending_yt": pending_yt,
            "bottleneck": bottleneck, "buckets": buckets}


# ============================================================ TASK LIST
@router.get("/tasks")
def pm_tasks(status: str = "", creator_type: str = "", editor_id: int = 0,
             graphics_id: int = 0, priority: str = "", q: str = "",
             deadline: str = "", teacher_id: int = 0, channel: str = "",
             channel_id: int = 0, video_type: str = "", video_type_id: int = 0,
             date_field: str = "", date_range: str = "", date_from: str = "",
             date_to: str = "", thumb: str = "", page: int = 1, size: int = 40,
             db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    # READ-ONLY: this endpoint NEVER writes. Legacy admin->lifecycle healing runs once at
    # startup (production_core.repair_legacy_production_state), not here.
    #
    # Canonical filter contract — every UI filter maps to exactly one param below and is built
    # from the single-source-of-truth helpers in production_core (status/teacher/channel/
    # video_type/date). All conditions AND together; none resets another.
    # Pagination guard: default 40, hard max 100, page >= 1.
    try:
        size = max(1, min(100, int(size or 40)))
    except Exception:
        size = 40
    try:
        page = max(1, int(page or 1))
    except Exception:
        page = 1
    query = db.query(VideoTask).filter(VideoTask.cancelled == False)
    # Teacher/general list: single-video TASKS only — projects (one_shot / rapid_revision /
    # project) live in their own Projects section. BUT the YouTuber Tasks section has NO separate
    # projects area, so for creator_type=youtuber we show EVERY kind (warna youtuber ke
    # one-shot/rapid/project tasks kahin nahi dikhte).
    _ct = (creator_type or "").strip().lower()
    # Delayed / Due-Today drill = CROSS-CUTTING view: youtuber ke delayed videos bhi dikhne
    # chahiye (dashboard KPI unhe ginta hai). Isliye in drills me youtuber ko exclude MAT karo,
    # sirf normal-kind rakho (dashboard se exactly match). Baaki har general list me youtuber
    # apni "YouTuber Tasks" section me hi rehta hai.
    _deadline_drill = deadline in ("overdue", "today")
    if _ct == "youtuber":
        pass  # YouTuber section: har kind dikhao
    elif _deadline_drill:
        # normal-kind only, par youtuber included (id.in_ filter neeche exact match dega)
        query = query.filter(or_(VideoTask.kind == None, VideoTask.kind == "",
                                 VideoTask.kind == "normal"))
    else:
        # YouTuber tasks live in their own "YouTuber Tasks" section — never in the general list.
        # youtuber_id set ho to bhi general list se bahar rakho (creator_type kharab ho tab bhi).
        query = query.filter(or_(VideoTask.creator_type == None,
                                 VideoTask.creator_type != "youtuber"))
        query = query.filter(VideoTask.youtuber_id == None)
        # Projects / one-shot / rapid-revision ki apni "Projects" section hai — general Tasks
        # list (PM Review, Editing, Uploaded, sab) me kabhi na dikhein. Pehle sirf no-status
        # view me hide hote the, isliye PM Review filter par project bhi aa jaata tha.
        query = query.filter(or_(VideoTask.kind == None, VideoTask.kind == "",
                                 VideoTask.kind == "normal"))
    if teacher_id:
        # Primary teacher OR any collaborator — boundary-safe (id 1 never matches 11),
        # spacing-independent. Single source of truth: production_core.teacher_filter.
        _tf = pc.teacher_filter(teacher_id)
        if _tf is not None:
            query = query.filter(_tf)
    # Channel: prefer VideoChannel id, fall back to normalised legacy channel_name string.
    _chf = pc.channel_filter(db, channel_id=channel_id, channel=channel)
    if _chf is not None:
        query = query.filter(_chf)
    # Video type: prefer VideoType id (resolved to name), normalised legacy string fallback.
    _vtf = pc.video_type_filter(db, video_type_id=video_type_id, video_type=video_type)
    if _vtf is not None:
        query = query.filter(_vtf)
    # Thumbnail state filter (independent of the lifecycle status filter): pending / assigned /
    # review / changes / done. "Has a thumbnail" matches the card logic EXACTLY — a thumbnail can
    # live in VideoTask.thumbnail_b64 (PM uploaded directly), VideoTask.thumbnail_link, OR an
    # approved GraphicsTask.thumbnail_url. Pending = genuinely nothing yet.
    _tb = (thumb or "").strip().lower()
    if _tb:
        _THUMB_STAGE = ["approved", "editor_assigned", "editing", "editing_paused",
                        "editing_done", "qc_pending", "qc_changes", "ready_for_youtube"]
        # task HAS a usable thumbnail already (any source)
        _has_b64  = and_(VideoTask.thumbnail_b64 != None, VideoTask.thumbnail_b64 != "")   # noqa: E711
        _has_link = and_(VideoTask.thumbnail_link != None, VideoTask.thumbnail_link != "")  # noqa: E711
        _gfx_done_sub = db.query(GraphicsTask.task_id).filter(GraphicsTask.status == "approved")
        _has_thumb = or_(_has_b64, _has_link, VideoTask.id.in_(_gfx_done_sub))
        # any graphics activity at all (assigned to a designer OR a row past 'new')
        _gfx_any_sub = db.query(GraphicsTask.task_id).filter(
            or_(GraphicsTask.graphics_id != None,                                          # noqa: E711
                GraphicsTask.status.in_(["in_progress", "submitted", "changes", "approved"])))
        if _tb == "pending":
            # thumbnail-stage video, NO thumbnail yet, NO graphics assigned/started
            query = query.filter(
                or_(VideoTask.lifecycle.in_(_THUMB_STAGE),
                    and_(or_(VideoTask.lifecycle == None, VideoTask.lifecycle == ""),      # noqa: E711
                         VideoTask.status == "approved")),
                ~_has_thumb,
                ~VideoTask.id.in_(_gfx_any_sub))
        elif _tb == "assigned":
            _sub = db.query(GraphicsTask.task_id).filter(
                GraphicsTask.graphics_id != None,                                          # noqa: E711
                GraphicsTask.status.in_(["new", "in_progress"]))
            query = query.filter(VideoTask.id.in_(_sub), ~_has_thumb)
        elif _tb == "review":
            _sub = db.query(GraphicsTask.task_id).filter(GraphicsTask.status == "submitted")
            query = query.filter(VideoTask.id.in_(_sub))
        elif _tb == "changes":
            _sub = db.query(GraphicsTask.task_id).filter(GraphicsTask.status == "changes")
            query = query.filter(VideoTask.id.in_(_sub))
        elif _tb == "done":
            query = query.filter(_has_thumb)
    if not status and _ct != "youtuber":
        # Default Tasks view me uploaded/completed nahi — wo alag "Uploaded Videos" section me hain.
        # LEKIN YouTuber Tasks section me poora pipeline dikhta hai (Published/uploaded bhi) taaki
        # "Published" count aur uploaded videos wahin dikhein.
        query = query.filter(or_(VideoTask.lifecycle == None, ~VideoTask.lifecycle.in_(["uploaded", "completed"])),
                             or_(VideoTask.status == None, ~VideoTask.status.in_(["uploaded", "completed"])))
    if status == "thumb_changes":
        # Thumbnail Changes section — jin thumbnails ko PM ne changes ke liye wapas bheja.
        _csub = db.query(GraphicsTask.task_id).filter(GraphicsTask.status == "changes")
        query = query.filter(VideoTask.id.in_(_csub))
        status = ""
    if status == "thumb_review":
        # Thumbnail Review section — jin tasks ke thumbnail graphics designer ne submit kiye,
        # wo PM ke review ke liye. (Graphics status = submitted.)
        _tsub = db.query(GraphicsTask.task_id).filter(GraphicsTask.status == "submitted")
        query = query.filter(VideoTask.id.in_(_tsub))
        status = ""
    if status:
        # Canonical status↔lifecycle mapping (matches BOTH new lifecycle and legacy admin
        # status). Single source of truth: production_core.stage_filter.
        _sf = pc.stage_filter(status)
        if _sf is not None:
            query = query.filter(_sf)
    if creator_type:
        if creator_type == "youtuber":
            # Bulletproof: creator_type ya youtuber_id — dono me se koi bhi youtuber ho to YouTuber
            # Tasks section me dikhe (kisi task ka creator_type kharab ho tab bhi gayab na ho).
            query = query.filter(or_(VideoTask.creator_type == "youtuber",
                                     VideoTask.youtuber_id != None))
        else:
            query = query.filter(VideoTask.creator_type == creator_type,
                                 VideoTask.youtuber_id == None)
    if editor_id:
        query = query.filter(VideoTask.editor_id == editor_id)
    if graphics_id:
        query = query.filter(VideoTask.graphics_id == graphics_id)
    if priority:
        query = query.filter(VideoTask.priority == priority)
    if q:
        # Expanded search: title / ref / subject / channel / type / series, PLUS people by
        # name — teacher (primary + collaborator), editor and graphics. Name->id resolved via
        # small subqueries; all OR-combined into one clause.
        qq = q.strip()
        like = "%" + qq + "%"
        conds = [VideoTask.title.like(like), VideoTask.ref_code.like(like),
                 VideoTask.subject.like(like), VideoTask.channel_name.like(like),
                 VideoTask.video_type.like(like), VideoTask.series_name.like(like)]
        try:
            _tp_ids = [r[0] for r in db.query(TeacherProfile.id)
                       .join(User, User.id == TeacherProfile.user_id)
                       .filter(User.name.like(like)).limit(30).all()]
            for _tid in _tp_ids:
                _tf2 = pc.teacher_filter(_tid)      # primary + collaborator, boundary-safe
                if _tf2 is not None:
                    conds.append(_tf2)
        except Exception:
            pass
        try:
            _ps_ids = [r[0] for r in db.query(ProductionStaffProfile.id)
                       .join(User, User.id == ProductionStaffProfile.user_id)
                       .filter(User.name.like(like)).limit(30).all()]
            if _ps_ids:
                conds.append(VideoTask.editor_id.in_(_ps_ids))
                conds.append(VideoTask.graphics_id.in_(_ps_ids))
        except Exception:
            pass
        query = query.filter(or_(*conds))
    if deadline in ("overdue", "today", "week", "none"):
        # Deadline STATE (separate from date-range): stage-aware, IST-correct, matches the
        # dashboard KPI exactly. Single source of truth: production_core.deadline_state_ids.
        _ids = pc.deadline_state_ids(db, deadline)
        query = query.filter(VideoTask.id.in_(_ids or [-1]))
    # Date-wise master filter (Created / Deadline / Upload / Editing Done) × (Today / Yesterday
    # / Weekly / Monthly / Custom). IST-correct per column storage. Independent of deadline-state.
    for _dc in pc.date_range_clauses(date_field, date_range, date_from, date_to):
        query = query.filter(_dc)
    total = query.count()
    # base64 thumbnail column ka CONTENT list me load MAT karo (RAM + speed) — thumbnail
    # ka URL alag se thumb_map se aata hai.
    rows = (query.options(defer(VideoTask.thumbnail_b64))
            .order_by(VideoTask.updated_at.desc())
            .offset(max(0, page - 1) * size).limit(size).all())

    # Collab info for list cards (parses the task's JSON fields — no extra DB queries).
    try:
        from video_tasks import _collab_all_ids as _cai, _collab_vmap as _cvm
    except Exception:
        _cai = _cvm = None

    _cc_map = pc.comment_count_map(db, [t.id for t in rows])   # 1 query, was 1 COUNT per task
    _thumb_map = pc.thumb_map_for(db, [t.id for t in rows])    # thumbnail URLs — base64 load kiye bina

    def _task_out_collab(t):
        o = pc.task_out(db, t, light=True, comment_count=_cc_map.get(t.id, 0), thumb_map=_thumb_map)
        if _cai:
            try:
                allids = _cai(t)
                vmap = _cvm(t) or {}
                o["is_collab"] = len(allids) > 1
                o["collab_total"] = len(allids)
                o["collab_verified"] = sum(1 for i in allids if vmap.get(str(i)))
            except Exception:
                pass
        return o

    _outs = [_task_out_collab(t) for t in rows]
    try:
        from video_tasks import _vtc_unread_bulk
        _uid = getattr(me, "id", None)
        _unread = _vtc_unread_bulk(db, _uid, [t.id for t in rows])
        for _o in _outs:
            _u = _unread.get(_o.get("id"), {})
            _o["unread"] = _u
            _o["unread_total"] = sum(_u.values()) if _u else 0
    except Exception:
        pass
    _has_more = (page * size) < total
    return {"total": total, "page": page, "size": size, "page_size": size,
            "has_more": _has_more, "returned": len(_outs), "tasks": _outs}


@router.get("/tasks/{tid}/comments")
def pm_task_comments(tid: int, audience: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from video_tasks import _vtc_list_v, _vtc_mark_read, _chat_touch, _chat_other_presence
    _aud = (audience or "creator")
    _vtc_mark_read(db, me, tid, _aud)
    _chat_touch(db, me, tid, _aud)
    return {"comments": _vtc_list_v(db, tid, (audience or None), getattr(me, "id", None)),
            "presence": _chat_other_presence(db, getattr(me, "id", None), tid, _aud)}

@router.get("/tasks/{tid}/party-tasks")
def pm_party_tasks(tid: int, audience: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from video_tasks import _chat_party_tasks
    return {"tasks": _chat_party_tasks(db, tid, (audience or "creator"))}


@router.get("/review-alerts")
def pm_review_alerts(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Edited videos where a TEACHER requested changes — drives the PM/Admin dashboard popup."""
    rows = db.query(VideoTask).filter(
        VideoTask.cancelled == False,
        VideoTask.teacher_review_status == "changes",
        VideoTask.lifecycle.in_(["qc_pending", "qc_changes"])).all()
    out = [{"id": t.id, "title": t.title or "",
            "teacher_reviewer_name": (getattr(t, "teacher_reviewer_name", "") or ""),
            "editor_name": (pc._name_for_staff(db, t.editor_id) if t.editor_id else "")} for t in rows]
    return {"alerts": out}


@router.post("/heartbeat")
def pm_heartbeat(payload: dict = Body(default={}), db: Session = Depends(get_db),
                 me=Depends(get_pm_or_admin)):
    pc.touch_session(db, me, (payload or {}).get("page"), bool((payload or {}).get("active")))
    from video_tasks import _chat_touch_global
    _chat_touch_global(db, me)
    return {"ok": True}


@router.get("/chat/inbox")
def pm_chat_inbox(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Manager oversight inbox — every task chat thread (teacher/editor/graphics)."""
    from video_tasks import _chat_inbox, _chat_touch_global
    _chat_touch_global(db, me)
    return {"conversations": _chat_inbox(db, me, "manager")}


@router.post("/tasks/{tid}/chat-ping")
def pm_chat_ping(tid: int, audience: str = "", payload: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from video_tasks import _chat_touch, _chat_other_presence
    _aud = (audience or (payload or {}).get("audience") or "creator")
    _chat_touch(db, me, tid, _aud, typing=bool((payload or {}).get("typing")))
    return {"presence": _chat_other_presence(db, getattr(me, "id", None), tid, _aud)}


@router.post("/tasks/{tid}/comments")
def pm_task_comment_add(tid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from video_tasks import _vtc_add, _vtc_out
    t = _task(db, tid)
    _att = ""
    _url_att = (payload.get("attachment_url") or "").strip()
    if _url_att:
        _att = _url_att  # already-hosted image (e.g. an existing thumbnail) — no re-upload
    _imgs = [] if _att else (payload.get("images") or ([payload.get("attachment")] if payload.get("attachment") else []))
    if _imgs:
        try:
            urls = pc.save_images(db, t, _imgs[:1], "chat", None, me, return_urls=True) or []
            if urls:
                _att = urls[0]
        except Exception:
            _att = ""
    _aud = (payload.get("audience") or "creator").strip().lower()
    if _aud not in ("creator", "internal", "editor", "te_ed", "te_gf", "ed_gf", "review"):
        _aud = "creator"
    _crole = "admin" if getattr(me, "role", "") == "admin" else "production_manager"
    c = _vtc_add(db, tid, me, payload.get("message"), _crole, _att, _aud, ref_task_id=payload.get("ref_task_id"))
    from video_tasks import _chat_touch as _ct0
    try: _ct0(db, me, tid, _aud, typing=False)
    except Exception: pass
    if not c:
        raise HTTPException(400, "Message cannot be empty")
    if _aud in ("te_ed", "te_gf", "ed_gf"):
        # Manager stepping into a direct-pair thread (oversight) — notify both pair members.
        try:
            from video_tasks import _pair_members
            for oid in _pair_members(db, tid, _aud, getattr(me, "id", None)):
                pc.notify(db, oid, "Manager messaged on a video",
                          f'{getattr(me, "name", "Manager")} on "{t.title}": {c.message[:110]}',
                          "pair_chat", link=str(tid))
        except Exception:
            pass
        db.commit()
        return {"ok": True, "comment": _vtc_out(db, c)}
    if _aud == "editor":
        # PM <-> Editor thread — notify the assigned editor.
        try:
            from models import ProductionStaffProfile
            if t.editor_id:
                sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
                if sp and sp.user_id:
                    pc.notify(db, sp.user_id, "PM messaged you on a video",
                              f'{getattr(me, "name", "PM")} on "{t.title}": {c.message[:110]}',
                              "editor_chat", link=str(tid))
        except Exception:
            pass
        db.commit()
        return {"ok": True, "comment": _vtc_out(db, c)}
    if _aud == "review":
        # Teacher <-> Editor review thread (PM/admin oversight) — notify editor AND teachers.
        try:
            from models import ProductionStaffProfile, TeacherProfile as _TP
            from video_tasks import _collab_all_ids as _cai
            if t.editor_id:
                sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
                if sp and sp.user_id:
                    pc.notify(db, sp.user_id, "Message on video review",
                              f'{getattr(me, "name", "PM")} on "{t.title}": {c.message[:110]}',
                              "video_review", link=str(tid))
            for teach_id in _cai(t):
                tp = db.query(_TP).filter(_TP.id == teach_id).first()
                if tp and tp.user_id:
                    pc.notify(db, tp.user_id, "Message on your video review",
                              f'{getattr(me, "name", "PM")} on "{t.title}": {c.message[:110]}',
                              "video_review", link=str(tid))
        except Exception:
            pass
        db.commit()
        return {"ok": True, "comment": _vtc_out(db, c)}
    if _aud == "internal":
        # Internal thumbnail chat — notify the graphics designer, NOT the teacher.
        try:
            from models import GraphicsTask, ProductionStaffProfile
            g = db.query(GraphicsTask).filter(GraphicsTask.task_id == tid).first()
            if g and g.graphics_id:
                sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
                if sp and sp.user_id:
                    pc.notify(db, sp.user_id, "PM replied on thumbnail",
                              f'{getattr(me, "name", "PM")} on "{t.title}": {c.message[:110]}',
                              "gfx_chat", link=str(tid))
        except Exception:
            pass
        db.commit()
        return {"ok": True, "comment": _vtc_out(db, c)}
    # creator thread → notify the creator (and collaborators)
    try:
        from video_tasks import _collab_all_ids as _cai
        from models import TeacherProfile as _TP
        for teach_id in _cai(t):
            tp = db.query(_TP).filter(_TP.id == teach_id).first()
            if tp and tp.user_id:
                pc.notify(db, tp.user_id, "Manager replied on your video task",
                          f'Message on "{t.title}": {c.message[:120]}', "video_task", link=str(tid))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "comment": _vtc_out(db, c)}


@router.post("/tasks/{tid}/submit-link")
def pm_submit_link(tid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                   me=Depends(get_pm_or_admin)):
    """PM/Admin urgent case me creator (teacher/youtuber) ka drive link khud submit kar sakta hai
    (jab WhatsApp pe link aa jaata hai). Attribution card pe dikhta hai."""
    t = _task(db, tid)
    link = (payload.get("drive_link") or payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "A Drive link is required")
    lc = (t.lifecycle or ""); stt = (t.status or "")
    awaiting = (lc in ("creator_assigned", "creator_working", "changes_required", "reshoot_required")) \
        or ((not lc) and stt in ("assigned", "reshoot", "rejected", ""))
    if not awaiting:
        raise HTTPException(400, "This video is not awaiting submission anymore.")
    role = "admin" if getattr(me, "role", "") == "admin" else "production_manager"
    pc.submit_creator_link(db, t, link, actor_name=(getattr(me, "name", "") or role), actor_role=role)
    db.commit()
    return {"ok": True, "on_time": t.on_time, "lifecycle": t.lifecycle,
            "submitted_by_name": t.submitted_by_name or "", "submitted_by_role": t.submitted_by_role or ""}


@router.get("/tasks/{tid}")
def pm_task_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    out = pc.task_out(db, t, timeline=True)
    # collab info for the edit modal (pre-checks collaborators)
    try:
        from video_tasks import (_collab_all_ids as _cai, _collab_vmap as _cvm,
                                  _collab_extra_ids as _cei, _teacher_name as _ctn)
        allids = _cai(t)
        vmap = _cvm(t)
        out["is_collab"] = len(allids) > 1
        out["collab_teacher_ids"] = _cei(t)
        out["collaborators"] = [{"id": i, "name": _ctn(db, i),
                                 "verified": bool(vmap.get(str(i))),
                                 "primary": (i == t.teacher_id)} for i in allids]
    except Exception:
        pass
    out["thumbnail_required"] = bool(getattr(t, "thumbnail_required", False))
    out["graphics_id"] = getattr(t, "graphics_id", None)
    out["editor_id"] = getattr(t, "editor_id", None)
    # creator ids so the edit wizard can pre-select the teacher / youtuber
    out["teacher_id"] = getattr(t, "teacher_id", None)
    out["youtuber_id"] = getattr(t, "youtuber_id", None)
    return out


# ============================================================ CREATE TASK
@router.get("/youtuber-targets")
def prod_youtuber_targets(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Per-youtuber monthly target + is-mahine kitne publish hue (progress)."""
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    out = []
    for yp in db.query(YouTuberProfile).filter(YouTuberProfile.is_active == True).all():
        base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber",
                                          VideoTask.youtuber_id == yp.id)
        done = base.filter(VideoTask.published_at != None,
                           VideoTask.published_at >= month_start).count()
        active = base.filter(~VideoTask.lifecycle.in_(["uploaded", "completed"]),
                             VideoTask.cancelled == False).count()
        tgt = int(getattr(yp, "monthly_target", 0) or 0)
        pct = round(100.0 * done / tgt) if tgt else 0
        out.append({"id": yp.id, "name": (yp.user.name if yp.user else ""),
                    "target": tgt, "done_this_month": done, "active": active,
                    "pct": pct, "met": bool(tgt and done >= tgt)})
    out.sort(key=lambda x: (-(x["target"] > 0), -x["pct"], -x["done_this_month"]))
    return {"month": month_start.strftime("%B %Y"), "youtubers": out}


@router.post("/youtuber-target")
def prod_set_youtuber_target(payload: dict = Body(...), db: Session = Depends(get_db),
                             me=Depends(get_pm_or_admin)):
    yid = int(payload.get("youtuber_id") or 0)
    yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == yid).first()
    if not yp:
        raise HTTPException(404, "YouTuber not found")
    try:
        tgt = max(0, min(500, int(payload.get("target") or 0)))
    except Exception:
        tgt = 0
    yp.monthly_target = tgt
    db.commit()
    return {"ok": True, "youtuber_id": yid, "target": tgt}


@router.post("/youtuber-series")
def prod_create_youtuber_series(payload: dict = Body(...), db: Session = Depends(get_db),
                                me=Depends(get_pm_or_admin)):
    """Assign a SERIES/BATCH of videos to one youtuber. Each video becomes a normal
    single-video task (apna lifecycle), sabhi ek shared series_name ke andar group hote hain.
    YouTuber ke liye chapters nahi — bas series."""
    import re as _re
    series = (payload.get("series_name") or "").strip()
    if not series:
        raise HTTPException(400, "A series name is required")
    yid = int(payload.get("youtuber_id") or 0)
    yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == yid).first()
    if not yp:
        raise HTTPException(400, "Valid youtuber_id required")
    vids, seen = [], set()
    for it in (payload.get("videos") or []):
        s = _re.sub(r"\s+", " ", str(it or "")).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower()); vids.append(s[:300])
        if len(vids) >= 50:
            break
    if not vids:
        raise HTTPException(400, "Add at least one video title")
    channel = (payload.get("channel_name") or "").strip()
    vtype = (payload.get("video_type") or "").strip()
    streaming = (payload.get("streaming") or "").strip()
    priority = (payload.get("priority") or "normal").strip()
    remarks = (payload.get("remarks") or "").strip()
    reference = (payload.get("reference") or "").strip()
    appr = payload.get("approval_required")
    deadline = None
    dls = (payload.get("deadline") or "").strip()
    if dls:
        try:
            deadline = datetime.fromisoformat(dls.replace("Z", ""))
        except Exception:
            deadline = None
    created = []
    for title in vids:
        t = VideoTask(title=title, creator_type="youtuber", youtuber_id=yid,
                      series_name=series, channel_name=channel, video_type=vtype,
                      streaming=streaming, priority=priority, remarks=remarks,
                      reference=reference, proposed_by="admin", status="assigned",
                      deadline=deadline)
        if appr is not None:
            t.approval_required = bool(appr)
        db.add(t); db.flush()
        try:
            pc.ensure_ref_code(t)
        except Exception:
            pass
        pc.set_state(db, t, "creator_assigned", actor=me, event="task_created")
        pc.log_event(db, t, me, "creator_assigned", new_state="creator_assigned")
        created.append(t.id)
    try:
        if yp.user_id:
            pc.notify(db, yp.user_id, "New Series — %s" % series,
                      'You have been assigned a new series: "%s" (%d videos). Check My Tasks.'
                      % (series, len(created)), "video_task")
    except Exception:
        pass
    db.commit()
    return {"ok": True, "series": series, "count": len(created), "ids": created}


def _apply_upload_done(db, t, payload, me):
    """Assign Work / Edit Task ke 'Upload' section ka handler.
    Agar video already edit + YouTube pe live hai (video_status=='done') to seedha publish:
    lifecycle -> uploaded, live views fetch, aur jis editor ne edit kiya usko credit + rating.
    Returns True agar upload-done apply hua (warna normal flow chalta rahe)."""
    vs = (payload.get("video_status") or "").strip().lower()
    if vs != "done":
        return False
    url = (payload.get("youtube_url") or "").strip()
    if not url:
        return False
    from video_tasks import _yt_extract_id, _yt_get_key, _yt_fetch_views
    vid = _yt_extract_id(url)
    if not vid:
        raise HTTPException(400, "Could not read a valid YouTube video id from that URL")
    # editor who actually edited this video (credit) — optional, overrides assigned editor
    try:
        ueid = int(payload.get("upload_editor_id") or 0)
    except Exception:
        ueid = 0
    if ueid:
        ep = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.id == ueid,
            ProductionStaffProfile.staff_role == "editor").first()
        if ep:
            t.editor_id = ueid
    # IDEMPOTENCY: har edit par video_status='done' + wahi prefilled URL dubara aata hai.
    # Agar video pehle se isi URL par LIVE hai to publish ke side-effects (timeline event,
    # editor/creator notification, views re-fetch) DOBARA mat chalao -> warna har unrelated
    # edit par editor ko "your video is live" spam jaata aur timeline me duplicate events bante.
    _url_changed = ((t.youtube_url or "").strip() != url)
    _was_live = ((t.lifecycle or "") in ("uploaded", "completed")) and bool((t.youtube_url or "").strip())
    _fresh_publish = _url_changed or not _was_live
    # editor rating (optional) — idempotent assignment, safe to re-apply
    try:
        rt = int(payload.get("upload_rating") or 0)
    except Exception:
        rt = 0
    if rt and 1 <= rt <= 5:
        t.quality_rating = rt
    t.youtube_url = url
    t.yt_video_id = vid
    if not _fresh_publish:
        # already live on the same link — lifecycle ensure karo, baaki kuch mat chhedo
        if (t.lifecycle or "") not in ("uploaded", "completed"):
            pc.set_state(db, t, "uploaded", actor=me, event="youtube_link_added", force=True)
        return True
    # ---- genuine publish (naya link ya pehli baar) ----
    t.published_at = datetime.utcnow()
    # Uploaded video ka "upload date" = actual publish date (IST-local), automatic. PM ko ab
    # manually set karne ki zaroorat nahi — jo publish hua wahi upload date.
    t.upload_date = t.published_at + timedelta(hours=5, minutes=30)
    pc.set_state(db, t, "uploaded", actor=me, event="youtube_link_added", force=True)
    pc.log_event(db, t, me, "uploaded", new_state="uploaded")
    # notify editor + (youtuber) creator that it's live
    try:
        if t.editor_id:
            ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ed and ed.user_id:
                _msg = f'"{t.title}" you edited is now live on YouTube.'
                if rt:
                    _msg += " Rated %d/5." % rt
                pc.notify(db, ed.user_id, "Your video is live", _msg,
                          "appreciation" if (rt or 0) >= 4 else "video_task", link=str(t.id))
        if (t.creator_type or "") == "youtuber" and t.youtuber_id:
            yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
            if yp and yp.user_id:
                pc.notify(db, yp.user_id, "Your video is live",
                          f'"{t.title}" was uploaded to YouTube.', "video_request", link=str(t.id))
    except Exception:
        pass
    # AUTO: teacher ke sabhi students ko published YouTube link turant bhej do (sirf ek baar)
    try:
        if not bool(getattr(t, "students_notified", False)):
            _n = auto_notify_students_video(db, t.teacher_id, t.youtube_url, t.title,
                                            t.channel_name or "", actor_id=getattr(me, "id", None))
            t.students_notified = True
            try:
                pc.log_event(db, t, me, "auto_sent_to_students",
                             meta={"note": "Auto-sent to %d student%s" % (_n, "" if _n == 1 else "s")})
            except Exception:
                pass
    except Exception:
        pass
    # initial live views (best-effort)
    try:
        key = _yt_get_key(db)
        got = _yt_fetch_views([vid], key)
        if vid in got:
            t.yt_views = got[vid]
            t.yt_views_at = datetime.utcnow()
    except Exception:
        pass
    return True


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
                  reference_video=(payload.get("reference_video") or "").strip(),
                  remarks=(payload.get("remarks") or "").strip(),
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
        # optional collaborators (multi-teacher video)
        import json as _jc
        collab = []
        for x in (payload.get("collab_teacher_ids") or []):
            try:
                xi = int(x)
            except Exception:
                continue
            if xi and xi != tid and xi not in collab and db.query(TeacherProfile).filter(TeacherProfile.id == xi).first():
                collab.append(xi)
        if collab:
            try:
                t.collab_teacher_ids = _jc.dumps(collab)
            except Exception:
                pass
    else:
        yid = int(payload.get("youtuber_id") or 0)
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == yid).first()
        if not yp:
            raise HTTPException(400, "Valid youtuber_id required")
        t.youtuber_id = yid
    db.add(t)
    db.flush()
    pc.ensure_ref_code(t)
    # thumbnail requirement + optional pre-assignment of graphics designer and editor
    t.thumbnail_required = bool(payload.get("thumbnail_required"))
    try:
        gid = int(payload.get("graphics_id") or 0)
    except Exception:
        gid = 0
    if t.thumbnail_required and gid:
        gp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == gid,
                                                     ProductionStaffProfile.staff_role == "graphics").first()
        if gp:
            g = GraphicsTask(task_id=None, graphics_id=gid, status="new",
                             priority=(payload.get("priority") or "normal"))
            # will be linked after flush; set fk once task has id
            t.graphics_id = gid
    try:
        eid = int(payload.get("editor_id") or 0)
    except Exception:
        eid = 0
    if eid:
        ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == eid,
                                                     ProductionStaffProfile.staff_role == "editor").first()
        if ep:
            t.editor_id = eid
    # collab editors (2 editors on an urgent video) — additional editor ids beyond the primary
    import json as _jce
    _ced = []
    for x in (payload.get("collab_editor_ids") or []):
        try:
            xi = int(x)
        except Exception:
            continue
        if xi and xi != t.editor_id and xi not in _ced and db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.id == xi, ProductionStaffProfile.staff_role == "editor").first():
            _ced.append(xi)
    if _ced:
        try:
            t.collab_editor_ids = _jce.dumps(_ced)
        except Exception:
            pass
    # editor ke liye alag deadline + instructions + reference (Assign Work "Editor" section)
    _edl = (payload.get("editor_deadline") or "").strip()
    if _edl:
        try:
            t.editor_deadline = datetime.fromisoformat(_edl.replace("Z", ""))
        except Exception:
            pass
    _ein = (payload.get("editor_instructions") or "").strip()
    if _ein:
        t.editor_instructions = _ein
    _eref = (payload.get("editor_reference") or "").strip()
    if _eref:
        t.editor_reference = _eref
    pc.set_state(db, t, "creator_assigned", actor=me, event="task_created")
    pc.log_event(db, t, me, "creator_assigned", new_state="creator_assigned")
    db.flush()
    # create the graphics sub-task now that the task has an id, and notify the designer
    if t.thumbnail_required and t.graphics_id:
        gp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.graphics_id).first()
        existing = db.query(GraphicsTask).filter(GraphicsTask.task_id == t.id).first()
        if not existing:
            g = GraphicsTask(task_id=t.id, graphics_id=t.graphics_id, status="new",
                             priority=(payload.get("priority") or "normal"),
                             instructions=(payload.get("graphics_instructions") or payload.get("graphics_notes") or "").strip())
            # Reference thumbnails: PM ki TYPED link(s) AUR clipboard/upload ki images -> DONO ko
            # ek hi reference_images array me merge karo. Pehle uploads typed link ko overwrite kar
            # deti thi -> graphics portal (jo reference_images padhta hai) ko link dikhta hi nahi tha.
            import json as _json
            _ref_urls = []
            # typed Drive/image link(s) — single field, but split on newline in case of multiple
            _typed = (payload.get("graphics_reference") or payload.get("reference_image") or "").strip()
            if _typed:
                for _ln in _typed.replace(",", "\n").split("\n"):
                    _ln = _ln.strip()
                    if _ln and _ln not in _ref_urls:
                        _ref_urls.append(_ln)
            # pasted / uploaded reference images -> R2
            _refups = payload.get("graphics_reference_uploads")
            if not _refups:
                _one = payload.get("graphics_reference_upload")
                _refups = [_one] if _one else []
            if _refups:
                try:
                    _rurls = pc.save_images(db, t, list(_refups), "reference", None, me, return_urls=True) or []
                    for _u in _rurls:
                        if _u and _u not in _ref_urls:
                            _ref_urls.append(_u)
                except Exception:
                    pass
            if _ref_urls:
                g.reference_image = _ref_urls[0]
                g.reference_images = _json.dumps(_ref_urls[:8])
            gdl = (payload.get("graphics_deadline") or payload.get("deadline") or "").strip()
            if gdl:
                try:
                    g.deadline = datetime.fromisoformat(gdl.replace("Z", ""))
                except Exception:
                    pass
            db.add(g)
            db.flush()
        else:
            g = existing
        # PM already has the finished thumbnail -> upload now, auto-approve, optional rating
        _upload = payload.get("thumbnail_upload")
        if _upload:
            try:
                urls = pc.save_images(db, t, [_upload], "thumbnail", None, me, return_urls=True) or []
                if urls:
                    g.thumbnail_url = urls[0]
                    t.thumbnail_link = urls[0]
            except Exception:
                pass
            g.status = "approved"
            try:
                g.submitted_at = datetime.utcnow()
            except Exception:
                pass
            _rating = payload.get("thumbnail_rating")
            if _rating:
                try:
                    g.quality_rating = int(_rating)
                except Exception:
                    pass
            pc.log_event(db, t, me, "thumbnail_approved", new_state=t.lifecycle,
                         meta={"note": "Thumbnail uploaded and auto-approved by production manager"
                               + ((" (rated %s/5)" % int(_rating)) if _rating else "")})
        if gp and gp.user_id:
            if _upload:
                pc.notify(db, gp.user_id, "Thumbnail recorded",
                          f'Your thumbnail for "{title}" was uploaded and approved.', "graphics_task", link=str(t.id))
            else:
                pc.notify(db, gp.user_id, "New Thumbnail Task",
                          f'A thumbnail has been requested for "{title}".', "graphics_task", link=str(t.id))
    if t.editor_id:
        ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ep and ep.user_id:
            pc.notify(db, ep.user_id, "You are the editor for an upcoming video",
                      f'You have been pre-assigned to edit "{title}" once it is ready.', "video_task", link=str(t.id))
    for _cei in pc.collab_editor_ids(t):
        _cep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == _cei).first()
        if _cep and _cep.user_id:
            pc.notify(db, _cep.user_id, "You are a co-editor for an upcoming video",
                      f'You have been added as a co-editor on "{title}".', "video_task", link=str(t.id))
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
    # Upload section: agar PM/admin ne "Upload done" chuna (video already edited + live) to
    # seedha publish + editor credit/rating (editor-assign flow skip).
    try:
        _apply_upload_done(db, t, payload, me)
    except HTTPException:
        raise
    except Exception:
        pass
    db.commit()
    return {"ok": True, "id": t.id, "ref_code": t.ref_code}


# ============================================================ CREATOR REVIEW
@router.post("/tasks/{tid}/approve-creator")
def approve_creator(tid: int, payload: dict = Body(default={}),
                    db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    # Legacy / admin-assigned tasks ka production lifecycle blank ho sakta hai jabki status
    # 'submitted' hota hai (card badge bhi "PM REVIEW" isi status se dikhata hai). Unhe bhi
    # approve karne do — warna production portal se approve nahi ho paate the.
    _legacy_submitted = ((t.lifecycle or "") in ("", "created", "creator_assigned", "creator_working")
                         and (t.status or "") == "submitted")
    if t.lifecycle not in ("creator_submitted", "pm_review") and not _legacy_submitted:
        raise HTTPException(400, "Task is not awaiting creator approval")
    db.add(TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                      decision="approved", remarks=(payload.get("remarks") or "")))
    pc.set_state(db, t, "approved", actor=me, event="approved")
    # editor pehle se assign hai to seedha editor_assigned — warna editor ko task dikhega hi nahi
    if t.editor_id:
        try:
            pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned", force=True)
            ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ep and ep.user_id:
                pc.notify(db, ep.user_id, "New editing task",
                          f'"{t.title}" is approved and ready for you to edit.', "editor_task", link=str(t.id))
        except Exception:
            pass
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
    old_dl, new_dl_str = _apply_new_deadline(t, payload, require=True)
    rv = TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                    decision="changes", remarks=remarks)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "creator", rv.id, me)
    pc.set_state(db, t, "changes_required", actor=me, event="changes_requested",
                 meta={"note": remarks[:200], "old_deadline": old_dl, "new_deadline": new_dl_str})
    _notify_creator(db, t, "Resubmit Required",
                    (remarks[:160] + " — new deadline: " + new_dl_str) if new_dl_str else remarks[:180])
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/reject-creator")
def reject_creator(tid: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for rejection")
    t = _task(db, tid)
    # If a new deadline is given (or resubmit requested), send it back for re-submission
    # instead of a final reject.
    want_resubmit = bool(payload.get("allow_resubmit")) or bool((payload.get("new_deadline") or "").strip())
    db.add(TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                      decision="rejected", remarks=remarks))
    if want_resubmit:
        old_dl, new_dl_str = _apply_new_deadline(t, payload, require=True)
        try:
            t.no_resubmit = False
        except Exception:
            pass
        pc.set_state(db, t, "changes_required", actor=me, event="changes_requested",
                     meta={"note": remarks[:200], "old_deadline": old_dl, "new_deadline": new_dl_str})
        _notify_creator(db, t, "Resubmit Required",
                        (remarks[:160] + " — new deadline: " + new_dl_str) if new_dl_str else remarks[:180])
    else:
        try:
            t.no_resubmit = True
        except Exception:
            pass
        pc.set_state(db, t, "rejected", actor=me, event="rejected")
        _notify_creator(db, t, "Rejected — no re-submission",
                        remarks[:180] + " No re-submission is required.")
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/reshoot-creator")
def reshoot_creator(tid: int, payload: dict = Body(...),
                    db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Distinct from reject: the video must be re-shot (not discarded). Keeps the task
    and its history; the creator re-shoots and resubmits."""
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for a reshoot")
    t = _task(db, tid)
    old_dl, new_dl_str = _apply_new_deadline(t, payload, require=True)
    db.add(TaskReview(task_id=t.id, kind="creator", reviewer_user_id=me.id,
                      decision="reshoot", remarks=remarks))
    pc.set_state(db, t, "reshoot_required", actor=me, event="reshoot_required",
                 meta={"note": remarks[:200], "old_deadline": old_dl, "new_deadline": new_dl_str})
    _notify_creator(db, t, "Reshoot Required",
                    (remarks[:160] + " — new deadline: " + new_dl_str) if new_dl_str else remarks[:180])
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


# ============================================================ EDITOR ASSIGN
@router.get("/editors/{eid}/active-tasks")
def editor_active_tasks(eid: int, exclude: int = 0,
                        db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Ek editor ke abhi active editing tasks + unke editor-deadline — PM/admin ko assign karte
    waqt dikhta hai (kis task ko pause karna hai decide karne ke liye). Sirf editing-stage tasks."""
    _active_lc = ["editor_assigned", "editing_soon", "editing", "editing_paused",
                  "editing_done", "qc_changes"]
    rows = (db.query(VideoTask)
            .options(defer(VideoTask.thumbnail_b64))
            .filter(VideoTask.editor_id == eid, VideoTask.cancelled == False,   # noqa: E712
                    VideoTask.lifecycle.in_(_active_lc))
            .order_by(VideoTask.editor_deadline.asc()).all())
    out = []
    for t in rows:
        if exclude and t.id == exclude:
            continue
        _edl = getattr(t, "editor_deadline", None)
        out.append({
            "id": t.id, "title": t.title or "", "ref_code": t.ref_code or "",
            "lifecycle": t.lifecycle or "", "channel": t.channel_name or "",
            "editor_deadline": (_edl.strftime("%d %b %Y, %I:%M %p") if _edl else ""),
            "editor_deadline_iso": (_edl.strftime("%Y-%m-%dT%H:%M") if _edl else ""),
            "progress": (t.editing_progress or 0),
            "pause_req": bool(getattr(t, "pause_req", False)),
            "deadline_flag": (lambda f: {"kind": f[0], "label": f[1]})(pc.deadline_flag(t, deadline=_edl)),
        })
    return {"tasks": out}


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
    # optional: admin/PM editor ko brief/instruction + reference de sakta hai (editor portal
    # isse "PM Brief" me dekhta hai) + optional editor deadline. Ye editor-specific fields hain
    # -> teacher ka reference/deadline chhedte NAHI. Blank ho to overwrite nahi karte.
    _eins = (payload.get("instructions") or payload.get("editor_instructions") or "").strip()
    if _eins:
        t.editor_instructions = _eins
    _eref = (payload.get("editor_reference") or payload.get("reference") or "").strip()
    if _eref:
        t.editor_reference = _eref
    _edl = (payload.get("editor_deadline") or payload.get("deadline") or "").strip()
    if _edl:
        try:
            from datetime import datetime as _dte
            t.editor_deadline = _dte.fromisoformat(_edl.replace("Z", ""))
        except Exception:
            pass
    # PM manually assigned an editor. Internal state stays 'editor_assigned' (what the
    # editor portal reads); it is DISPLAYED as "Editing Soon". Normal path from Approved
    # is validated; a late re-assignment from a deeper state is a PM oversight action.
    _prev = t.lifecycle or ""
    _ename = ed.user.name if ed.user else "editor"
    # PM/admin assign (ya re-assign) hamesha allow — chahe task kisi bhi state me ho
    # (approved / blank legacy / deeper state). force=True se invalid-transition error nahi aayega.
    pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned",
                 meta={"note": "Assigned to " + _ename}, force=True)
    if _prev == "editor_assigned":
        # state didn't change -> set_state skipped the timeline event; log it so the
        # (re)assignment always shows up in the timeline.
        pc.log_event(db, t, me, "editor_assigned", new_state="editor_assigned",
                     meta={"note": "Editor changed to " + _ename})
    if ed.user_id:
        pc.notify(db, ed.user_id, "New Editing Task",
                  f'You have been assigned to edit: "{t.title}".', "video_task", link=str(t.id))
    # teacher sees updated status
    _notify_task_teacher(db, t, "Editor Assigned",
                         f'Your video "{t.title}" was approved and assigned to an editor.', link=str(t.id))
    # ---- URGENT PAUSE-REQUEST (shared helper): editor ke ek active task ko pause + new deadline ----
    _apply_pause_request(db, t, eid, payload, me)
    db.commit()
    return {"ok": True, "editor": ed.user.name if ed.user else "", "lifecycle": t.lifecycle}


# ============================================================ GRAPHICS ASSIGN
@router.post("/tasks/{tid}/thumbnail")
def pm_set_thumbnail(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """PM/admin uploads (or replaces) the thumbnail directly — auto-approves it."""
    t = _task(db, tid)
    _up = payload.get("thumbnail") or payload.get("thumbnail_upload") or payload.get("images")
    if not _up:
        raise HTTPException(400, "A thumbnail image is required")
    up = _up[0] if isinstance(_up, list) else _up
    urls = pc.save_images(db, t, [up], "thumbnail", None, me, return_urls=True) or []
    g = pc.graphics_task(db, t, create=True)
    if urls:
        g.thumbnail_url = urls[0]
        t.thumbnail_link = urls[0]
    g.status = "approved"
    try:
        g.submitted_at = datetime.utcnow()
    except Exception:
        pass
    pc.log_event(db, t, me, "thumbnail_uploaded", new_state=t.lifecycle)
    db.commit()
    return {"ok": True, "thumbnail": t.thumbnail_link or ""}


@router.delete("/tasks/{tid}/thumbnail")
def pm_del_thumbnail(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    t.thumbnail_link = ""
    g = pc.graphics_task(db, t, create=False)
    if g:
        g.thumbnail_url = ""
        if g.status == "approved":
            g.status = "new"
    pc.log_event(db, t, me, "thumbnail_removed", new_state=t.lifecycle)
    db.commit()
    return {"ok": True}


@router.post("/tasks/{tid}/credit-thumbnail")
def pm_credit_thumbnail(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Thumbnail already made (by youtuber/PM) -> credit a designer + rate, auto-approve.
    Designer does NOT need to re-upload for review — goes straight to their completed."""
    from models import ProductionStaffProfile
    t = _task(db, tid)
    gid = int(payload.get("graphics_id") or 0)
    gr = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.id == gid, ProductionStaffProfile.staff_role == "graphics").first()
    if not gr:
        raise HTTPException(400, "Valid graphics designer required")
    g = pc.graphics_task(db, t, create=True)
    g.graphics_id = gid
    t.graphics_id = gid
    _up = payload.get("thumbnail")
    if _up:
        try:
            urls = pc.save_images(db, t, [_up[0] if isinstance(_up, list) else _up],
                                  "thumbnail", None, me, return_urls=True) or []
            if urls:
                g.thumbnail_url = urls[0]
                t.thumbnail_link = urls[0]
        except Exception:
            pass
    g.status = "approved"
    try:
        g.submitted_at = datetime.utcnow()
    except Exception:
        pass
    try:
        rating = int(payload.get("rating") or 0)
        if rating:
            g.quality_rating = rating
    except Exception:
        pass
    pc.log_event(db, t, me, "thumbnail_credited", new_state=t.lifecycle)
    if gr.user_id:
        _rt = int(payload.get("rating") or 0)
        pc.notify(db, gr.user_id, "Thumbnail credited",
                  f'Your thumbnail for "{t.title}" was approved' + (f" ({_rt}\u2605)" if _rt else "") + ".",
                  "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


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
    g.instructions = (payload.get("instructions") or payload.get("notes") or g.instructions or "")
    g.reference_image = (payload.get("reference_image") or payload.get("reference") or g.reference_image or "")
    g.priority = (payload.get("priority") or g.priority or "normal")
    _gdl = (payload.get("deadline") or "").strip()
    if _gdl:
        try:
            from datetime import datetime as _dtg
            g.deadline = _dtg.fromisoformat(_gdl.replace("Z", ""))
        except Exception:
            pass
    t.graphics_id = gid
    pc.log_event(db, t, me, "graphics_assigned", new_state=t.lifecycle,
                 meta={"note": "Assigned to graphics" + (" (urgent)" if g.priority == "urgent" else "")})
    if gr.user_id:
        pc.notify(db, gr.user_id, "New Thumbnail Task",
                  f'You have a thumbnail to design for: "{t.title}".', "video_task", link=str(t.id))
    # teacher sees THUMBNAIL PENDING
    _notify_task_teacher(db, t, "Thumbnail Pending",
                         f'A thumbnail is being prepared for "{t.title}".', link=str(t.id))
    db.commit()
    return {"ok": True, "graphics": gr.user.name if gr.user else ""}


# ============================================================ THUMBNAIL QC
@router.post("/tasks/{tid}/thumbnail-approve")
def thumbnail_approve(tid: int, payload: dict = Body(default={}),
                      db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    g = pc.graphics_task(db, t)
    if not g or g.status != "submitted":
        raise HTTPException(400, "No submitted thumbnail to approve")
    g.status = "approved"
    g.approved_at = datetime.utcnow()
    # PM may pick ONE of several submitted thumbnails as the final — that becomes the approved one
    # (and the only one the teacher/students see).
    _sel = (payload.get("selected_thumbnail") or "").strip()
    if _sel:
        g.thumbnail_url = _sel
    t.thumbnail_link = g.thumbnail_url or t.thumbnail_link
    # PM must rate the designer to approve — no rating, no approval.
    try:
        rt = int(payload.get("quality_rating") or 0)
    except Exception:
        rt = 0
    if not (1 <= rt <= 5):
        raise HTTPException(400, "A 1\u20135 star rating is required to approve the thumbnail")
    g.quality_rating = rt
    g.quality_note = (payload.get("quality_note") or payload.get("remarks") or g.quality_note or "")[:400]
    db.add(TaskReview(task_id=t.id, kind="thumbnail", reviewer_user_id=me.id,
                      decision="approved", remarks=g.quality_note or "",
                      revision_no=g.revision_count or 0))
    pc.log_event(db, t, me, "thumbnail_approved", new_state=t.lifecycle,
                 meta={"note": (("Rated %d/5. " % g.quality_rating) if g.quality_rating else "") + (g.quality_note or "")})
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            _msg = f'Your thumbnail for "{t.title}" was approved.'
            if g.quality_rating:
                _msg += " Rated %d/5." % g.quality_rating
            pc.notify(db, sp.user_id, "Thumbnail Approved", _msg,
                      "appreciation" if (g.quality_rating or 0) >= 4 else "video_task", link=str(t.id))
    # teacher sees the approved thumbnail
    _notify_task_teacher(db, t, "Thumbnail Approved",
                         f'The thumbnail for "{t.title}" is approved and ready.', link=str(t.id))
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
    # optional additional reference from the PM
    _ref = (payload.get("reference") or payload.get("reference_image") or "").strip()
    if _ref:
        g.reference_image = _ref
    g.revision_count = (g.revision_count or 0) + 1
    rv = TaskReview(task_id=t.id, kind="thumbnail", reviewer_user_id=me.id,
                    decision="changes", remarks=remarks, revision_no=g.revision_count)
    db.add(rv); db.flush()
    # PM screenshots / clipboard attachments (previous thumbnail_url is preserved, not overwritten)
    pc.save_images(db, t, payload.get("images"), "thumbnail", rv.id, me)
    pc.log_event(db, t, me, "thumbnail_changes_requested", new_state=t.lifecycle,
                 meta={"note": remarks[:200]})
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            pc.notify(db, sp.user_id, "Thumbnail Changes Requested", remarks[:180], "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


@router.post("/tasks/{tid}/thumbnail-reject")
def thumbnail_reject(tid: int, payload: dict = Body(...),
                     db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Reject the thumbnail entirely — the designer must redo it from scratch.
    Distinct from 'changes' (which tweaks the existing submission). Previous submission
    is preserved as history via TaskReview/attachments; the working thumbnail is cleared."""
    remarks = (payload.get("remarks") or "").strip()
    if not remarks:
        raise HTTPException(400, "Remarks are required for rejection")
    t = _task(db, tid)
    g = pc.graphics_task(db, t)
    if not g:
        raise HTTPException(400, "No thumbnail task")
    g.revision_count = (g.revision_count or 0) + 1
    rv = TaskReview(task_id=t.id, kind="thumbnail", reviewer_user_id=me.id,
                    decision="rejected", remarks=remarks, revision_no=g.revision_count)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "thumbnail", rv.id, me)
    g.status = "new"            # back to the start of the thumbnail sub-flow
    g.remarks = remarks
    g.thumbnail_url = ""        # clear working thumbnail (history kept in attachments)
    g.drive_link = ""
    pc.log_event(db, t, me, "thumbnail_rejected", new_state=t.lifecycle, meta={"note": remarks[:200]})
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            pc.notify(db, sp.user_id, "Thumbnail Rejected — Redo Required", remarks[:180], "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


# ============================================================ QC (edited video)
@router.post("/tasks/{tid}/qc-approve")
def qc_approve(tid: int, payload: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if t.lifecycle != "qc_pending":
        raise HTTPException(400, "Task is not in QC")
    # NOTE: teacher review is informational only — PM/Admin can approve at any time.
    # (The teacher's approval + note is surfaced in the QC modal, but never blocks the PM.)
    t.qc_status = "approved"
    db.add(TaskReview(task_id=t.id, kind="edit", reviewer_user_id=me.id, decision="approved",
                      revision_no=t.revision_count or 0))
    pc.set_state(db, t, "ready_for_youtube", actor=me, event="qc_approved")
    # approve ke saath hi tentative upload date + remarks set ho jaaye (smooth transition)
    _ud = (payload.get("upload_date") or "").strip()
    if _ud:
        try:
            t.upload_date = datetime.fromisoformat(_ud.replace("Z", ""))
        except Exception:
            pass
    if "upload_remarks" in payload:
        t.upload_remarks = (payload.get("upload_remarks") or "").strip()
    if t.editor_id:
        ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ed and ed.user_id:
            pc.notify(db, ed.user_id, "QC Approved", f'Your edit of "{t.title}" passed QC.', "video_task", link=str(t.id))
    # youtuber ko tentative upload date bata do
    try:
        if (t.creator_type or "") == "youtuber" and t.youtuber_id and t.upload_date:
            yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
            if yp and yp.user_id:
                pc.notify(db, yp.user_id, "Upload scheduled",
                          f'"{t.title}" is scheduled to upload on {t.upload_date.strftime("%d %b %Y, %I:%M %p")}.', "video_request", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


@router.post("/tasks/{tid}/request-edit-changes")
def request_edit_changes(tid: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    remarks = (payload.get("remarks") or "").strip()
    _note = remarks or "Changes requested — details in chat with editor."
    t = _task(db, tid)
    if t.lifecycle != "qc_pending":
        raise HTTPException(400, "Task is not in QC")
    # a fresh deadline for the editor to finish the changes (mandatory from the new flow)
    _new_dl = None
    _dl_raw = (payload.get("editor_deadline") or payload.get("new_deadline") or "").strip()
    if _dl_raw:
        try:
            _new_dl = datetime.fromisoformat(_dl_raw.replace("Z", ""))
        except Exception:
            _new_dl = None
    t.qc_status = "changes"
    t.revision_count = (t.revision_count or 0) + 1
    if _new_dl:
        t.editor_deadline = _new_dl
    rv = TaskReview(task_id=t.id, kind="edit", reviewer_user_id=me.id, decision="changes",
                    remarks=_note, revision_no=t.revision_count)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "edit", rv.id, me)
    _refs = (payload.get("references") or payload.get("reference") or "").strip()
    pc.set_state(db, t, "qc_changes", actor=me, event="changes_requested",
                 meta={"note": _note[:200], "references": _refs,
                       "new_deadline": (_new_dl.strftime("%d %b %Y, %I:%M %p") if _new_dl else "")})
    # post the change details (+ new deadline) into the editor chat so there's a clear record
    try:
        from video_tasks import _vtc_add
        _dl_line = ("\nNew deadline: " + _new_dl.strftime("%d %b %Y, %I:%M %p")) if _new_dl else ""
        _crole = "admin" if getattr(me, "role", "") == "admin" else "production_manager"
        _vtc_add(db, t.id, me, "Changes required in this video. Details below:\n" + _note + _dl_line,
                 _crole, "", "editor")
    except Exception:
        pass
    if t.editor_id:
        ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ed and ed.user_id:
            _msg = _note[:160]
            if _new_dl:
                _msg += " — New deadline: " + _new_dl.strftime("%d %b %Y, %I:%M %p")
            pc.notify(db, ed.user_id, "Changes Required", _msg, "video_task", link=str(t.id))
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle, "revision": t.revision_count,
            "editor_deadline": (t.editor_deadline.strftime("%d %b %Y, %I:%M %p") if t.editor_deadline else "")}


@router.post("/tasks/{tid}/qc-reject")
def qc_reject(tid: int, payload: dict = Body(...),
              db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Reject the edit outright (redo). Distinct from 'changes' — the edit must be redone.
    Never creates a new task; full revision history is preserved."""
    remarks = (payload.get("remarks") or "").strip()
    # remarks optional \u2014 PM chat me redo samjhata hai
    _note = remarks or "Rejected \u2014 redo. Details in chat with editor."
    t = _task(db, tid)
    if t.lifecycle != "qc_pending":
        raise HTTPException(400, "Task is not in QC")
    t.qc_status = "changes"
    t.revision_count = (t.revision_count or 0) + 1
    rv = TaskReview(task_id=t.id, kind="edit", reviewer_user_id=me.id, decision="rejected",
                    remarks=_note, revision_no=t.revision_count)
    db.add(rv); db.flush()
    pc.save_images(db, t, payload.get("images"), "edit", rv.id, me)
    pc.set_state(db, t, "qc_changes", actor=me, event="changes_requested",
                 meta={"note": ("Rejected \u2014 redo. " + _note[:180])})
    if t.editor_id:
        ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
        if ed and ed.user_id:
            pc.notify(db, ed.user_id, "Edit Rejected \u2014 Redo Required", _note[:180], "video_task", link=str(t.id))
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
    # upload date = actual publish date (IST-local), set automatically on publish.
    t.upload_date = t.published_at + timedelta(hours=5, minutes=30)
    pc.set_state(db, t, "uploaded", actor=me, event="youtube_link_added")
    pc.log_event(db, t, me, "uploaded", new_state="uploaded")
    # notify the editor + (if applicable) the youtuber creator that their video is live
    try:
        if t.editor_id:
            ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ed and ed.user_id:
                pc.notify(db, ed.user_id, "Your video is live",
                          f'"{t.title}" you edited was uploaded to YouTube.', "appreciation", link=str(t.id))
        if (t.creator_type or "") == "youtuber" and t.youtuber_id:
            yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
            if yp and yp.user_id:
                pc.notify(db, yp.user_id, "Your video is live",
                          f'"{t.title}" was uploaded to YouTube.', "video_request", link=str(t.id))
    except Exception:
        pass
    # AUTO: teacher ke sabhi students ko published YouTube link turant bhej do (sirf ek baar)
    try:
        if not bool(getattr(t, "students_notified", False)):
            _n = auto_notify_students_video(db, t.teacher_id, t.youtube_url, t.title,
                                            t.channel_name or "", actor_id=getattr(me, "id", None))
            t.students_notified = True
            try:
                pc.log_event(db, t, me, "auto_sent_to_students",
                             meta={"note": "Auto-sent to %d student%s" % (_n, "" if _n == 1 else "s")})
            except Exception:
                pass
    except Exception:
        pass
    # fetch initial metrics (best-effort) — reuses the shared YouTube views system
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


# ============================================================ UPLOAD SCHEDULE
@router.post("/tasks/{tid}/upload-schedule")
def pm_set_upload_schedule(tid: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """PM/admin sets a tentative YouTube upload date + remarks (editable anytime)."""
    t = _task(db, tid)
    if "upload_date" in payload:
        _ud = (payload.get("upload_date") or "").strip()
        if _ud:
            try:
                t.upload_date = datetime.fromisoformat(_ud.replace("Z", ""))
            except Exception:
                pass
        else:
            t.upload_date = None
    if "upload_remarks" in payload:
        t.upload_remarks = (payload.get("upload_remarks") or "").strip()
    try:
        pc.log_event(db, t, me, "upload_scheduled", new_state=t.lifecycle,
                     meta={"date": (t.upload_date.strftime("%Y-%m-%d %H:%M") if t.upload_date else ""),
                           "remarks": (t.upload_remarks or "")[:160]})
    except Exception:
        pass
    # notify the youtuber creator about their upload schedule
    try:
        if (t.creator_type or "") == "youtuber" and t.youtuber_id:
            yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
            if yp and yp.user_id:
                _when = t.upload_date.strftime("%d %b %Y, %I:%M %p") if t.upload_date else "TBD"
                pc.notify(db, yp.user_id, "Upload schedule set",
                          f'"{t.title}" is scheduled to upload on {_when}.', "video_request", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "upload_date": (t.upload_date.strftime("%d %b %Y, %I:%M %p") if t.upload_date else ""),
            "upload_remarks": t.upload_remarks or ""}


@router.get("/upload-schedule")
def pm_upload_schedule(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """All tasks that have an upload date OR are ready/uploaded — for the weekly/monthly calendar."""
    rows = (db.query(VideoTask).options(defer(VideoTask.thumbnail_b64))
            .filter(VideoTask.cancelled == False,
                    or_(VideoTask.upload_date != None,
                        VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded"])))
            .order_by(VideoTask.upload_date.asc()).all())
    _tm = pc.thumb_map_for(db, [t.id for t in rows])
    out = [pc.task_out(db, t, light=True, thumb_map=_tm) for t in rows]
    # ---- PROJECT CHAPTERS are uploads too: one chapter = one upload ----
    if _PROJECT_OK:
        import video_tasks as _vt
        chs = (db.query(_PVChapter)
               .filter(or_(_PVChapter.upload_date != None,                       # noqa: E711
                           _PVChapter.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"])))
               .order_by(_PVChapter.upload_date.asc()).all())
        if chs:
            pids = list({c.task_id for c in chs})
            pmap = {t.id: t for t in db.query(VideoTask).filter(
                VideoTask.id.in_(pids), VideoTask.cancelled == False).all()}
            tname = {}
            for c in chs:
                t = pmap.get(c.task_id)
                if not t:
                    continue
                if t.id not in tname:
                    try:
                        tname[t.id] = pc.creator_info(db, t)[0] if hasattr(pc, "creator_info") else ""
                    except Exception:
                        tname[t.id] = ""
                proj = (t.title or t.subject or "Project")
                out.append({
                    "work_type": "project_chapter",
                    "id": c.id, "chapter_id": c.id, "task_id": t.id, "parent_project_id": t.id,
                    "title": proj + " · " + (c.title or "Chapter"),
                    "channel_name": t.channel_name or "",
                    "creator_name": tname.get(t.id, ""),
                    "video_type": t.video_type or "",
                    "lifecycle": _vt._chapter_lifecycle(c),
                    "upload_date": (c.upload_date.strftime("%d %b %Y, %I:%M %p") if getattr(c, "upload_date", None) else ""),
                    "upload_date_iso": (c.upload_date.strftime("%Y-%m-%dT%H:%M:%S") if getattr(c, "upload_date", None) else ""),
                    "upload_remarks": getattr(c, "upload_remarks", "") or "",
                    "youtube_url": getattr(c, "youtube_url", "") or "",
                    "thumbnail": getattr(c, "thumbnail_link", "") or "",
                })
    return {"tasks": out}


# ============================================================ DAILY / WEEKLY / MONTHLY REPORT
@router.get("/report")
def pm_report(period: str = "daily", date: str = "",
              db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Editor/graphics/production ka daily/weekly/monthly report data — portal se
    hi image/PDF banane ke liye. Sab IST day boundaries pe compute hota hai."""
    from models import ProductionStaffProfile as _SP
    IST = timedelta(hours=5, minutes=30)
    try:
        anchor = datetime.fromisoformat(date) if date else (datetime.utcnow() + IST)
    except Exception:
        anchor = datetime.utcnow() + IST
    anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        start_ist = anchor - timedelta(days=6)
        end_ist = anchor + timedelta(days=1)
        range_label = start_ist.strftime("%d %b") + " – " + anchor.strftime("%d %b %Y")
    elif period == "monthly":
        start_ist = anchor.replace(day=1)
        end_ist = (start_ist.replace(year=start_ist.year + 1, month=1) if start_ist.month == 12
                   else start_ist.replace(month=start_ist.month + 1))
        range_label = start_ist.strftime("%B %Y")
    else:
        period = "daily"
        start_ist = anchor
        end_ist = anchor + timedelta(days=1)
        range_label = anchor.strftime("%d %b %Y")
    s = start_ist - IST   # UTC bounds (DB stores UTC)
    e = end_ist - IST

    from models import ProductionAttendance as _ATT
    day_str = anchor.strftime("%Y-%m-%d")
    is_daily = (period == "daily")

    def _ch(t):
        return ((t.channel_name if t else "") or "").strip() or "No channel"

    def _vt(t):
        """Normalized short video-type label (Long / Short / One Shot / Strategy / ...)."""
        raw = ((getattr(t, "video_type", "") if t else "") or "").strip()
        if not raw:
            return "Other"
        low = raw.lower()
        if "one" in low and "shot" in low:
            return "One Shot"
        if "short" in low:
            return "Short"
        if "long" in low:
            return "Long"
        if "strateg" in low:
            return "Strategy"
        # keep it compact for the report chips
        return raw[:18]

    # active staff (name kept even with zero work) + attendance overrides (daily)
    ed_rows = db.query(_SP).filter(_SP.staff_role == "editor", _SP.is_active == True).all()  # noqa: E712
    gf_rows = db.query(_SP).filter(_SP.staff_role == "graphics", _SP.is_active == True).all()  # noqa: E712
    att = {}
    if is_daily:
        for a in db.query(_ATT).filter(_ATT.day == day_str).all():
            att[a.staff_id] = {"status": (a.status or "present"), "remark": (a.remark or "")}

    def _status_for(sid, has_work):
        """Daily attendance status when a staff member has no work in the period."""
        if not is_daily or has_work:
            return ("active", "")
        o = att.get(sid)
        if o and o.get("status") == "present":
            return ("present", o.get("remark") or "")
        if o and o.get("status") == "leave":
            return ("leave", o.get("remark") or "")
        return ("leave", "")   # no work + no override -> Leave (default)

    # ---- editors: completed (period, with channel+title) + current working (snapshot) ----
    editors = []
    tot_completed = 0
    for sp in ed_rows:
        eid = sp.id
        enm = sp.user.name if sp.user else ("#" + str(eid))
        comp_rows = db.query(VideoTask).filter(
            VideoTask.cancelled.isnot(True),
            or_(VideoTask.editor_id == eid,
                VideoTask.collab_editor_ids.like("%" + str(eid) + "%")),
            VideoTask.editing_done_at != None,
            VideoTask.editing_done_at >= s, VideoTask.editing_done_at < e).all()
        completed = [{"title": (t.title or "Untitled")[:90], "channel": _ch(t),
                      "type": _vt(t)} for t in comp_rows]
        working = db.query(VideoTask).filter(
            VideoTask.cancelled.isnot(True),
            or_(VideoTask.editor_id == eid,
                VideoTask.collab_editor_ids.like("%" + str(eid) + "%")),
            VideoTask.lifecycle.in_(["editing", "editing_paused"])).all()
        wl = [{"title": (t.title or "Untitled")[:90], "channel": _ch(t),
               "type": _vt(t),
               "pct": int(t.editing_progress or 0),
               "paused": (t.lifecycle == "editing_paused")} for t in working]
        # ---- project videos (chapters) this editor is editing / finished in the window ----
        # (Jannat etc. edit project videos via start/pause — these must show, not read as Leave.)
        from models import VideoTaskChapter as _VC
        for c in db.query(_VC).filter(_VC.editor_id == eid).all():
            pt = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
            if pt is not None and getattr(pt, "cancelled", False):
                continue
            ptitle = ((pt.title if pt else "") or (pt.subject if pt else "") or "Project")
            lbl = (ptitle + (" — " + c.title if c.title else "")).strip()[:90]
            pch = _ch(pt)
            est = (c.edit_state or "")
            if est in ("editing", "paused"):
                wl.append({"title": lbl, "channel": pch, "type": "Project",
                           "pct": int(getattr(c, "editing_progress", 0) or 0),
                           "paused": (est == "paused")})
            elif getattr(c, "edited_at", None) and s <= c.edited_at < e:
                completed.append({"title": lbl, "channel": pch, "type": "Project"})
        # smart metrics: on-time % (editor deadline) + avg turnaround (start -> done) for the period
        _ot = 0
        _dl = 0
        _turn = []
        for t in comp_rows:
            _edl = getattr(t, "editor_deadline", None) or t.deadline
            if _edl:
                _dl += 1
                if t.editing_done_at and t.editing_done_at <= _edl:
                    _ot += 1
            if t.editing_started_at and t.editing_done_at and t.editing_done_at >= t.editing_started_at:
                _turn.append((t.editing_done_at - t.editing_started_at).total_seconds() / 3600.0)
        ontime_pct = int(round(_ot * 100.0 / _dl)) if _dl else None
        avg_hours = round(sum(_turn) / len(_turn), 1) if _turn else None
        tot_completed += len(completed)
        # Own-portal work today (editing sessions / project edits / completions) decides Leave —
        # PM actions never count. If nothing, _status_for falls back to the attendance override.
        has_work = bool(completed or wl) or pc.editor_worked_today(db, sp, s, e)
        st, rem = _status_for(eid, has_work)
        editors.append({"name": enm, "staff_id": eid, "completed_count": len(completed),
                        "completed": completed, "working": wl, "status": st, "remark": rem,
                        "ontime_pct": ontime_pct, "avg_hours": avg_hours, "top": False})
    editors.sort(key=lambda x: (0 if x["status"] in ("active",) else 1,
                                -x["completed_count"], -len(x["working"]), x["name"].lower()))
    # top performer = most completed in the period (only if >0)
    _best = max((x["completed_count"] for x in editors), default=0)
    if _best > 0:
        for x in editors:
            if x["completed_count"] == _best and x["status"] == "active":
                x["top"] = True
                break

    # ---- graphics: thumbnails produced (period, with channel) + pending (snapshot, per channel) ----
    graphics = []
    for sp in gf_rows:
        gid = sp.id
        gnm = sp.user.name if sp.user else ("#" + str(gid))
        guid = sp.user_id
        # DONE = thumbnails produced/credited in the window (own submissions AND PM-credited
        # pre-made thumbnails). Counted off GraphicsTask.submitted_at so nothing "freezes".
        done = []
        _seen_dt = set()
        for g in db.query(GraphicsTask).filter(
                GraphicsTask.graphics_id == gid,
                GraphicsTask.status.in_(["submitted", "approved"]),
                GraphicsTask.submitted_at != None,  # noqa: E711
                GraphicsTask.submitted_at >= s, GraphicsTask.submitted_at < e).all():
            if g.task_id in _seen_dt:
                continue
            _seen_dt.add(g.task_id)
            t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
            if t is not None and getattr(t, "cancelled", False):
                continue
            done.append({"title": ((t.title if t else "") or "Untitled")[:90], "channel": _ch(t)})
        pend_rows = db.query(GraphicsTask).filter(
            GraphicsTask.graphics_id == gid,
            GraphicsTask.status.in_(["new", "in_progress", "changes"])).all()
        pend_by_ch = {}
        for g in pend_rows:
            t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
            c = _ch(t)
            pend_by_ch[c] = pend_by_ch.get(c, 0) + 1
        # Leave is decided ONLY by the designer's OWN-portal work today (a thumbnail they
        # submitted themselves). Pending tasks or PM-credited thumbnails do NOT count as present.
        has_work = pc.graphics_worked_today(db, sp, s, e)
        st, rem = _status_for(gid, has_work)
        graphics.append({"name": gnm, "staff_id": gid, "done_count": len(done), "done": done,
                         "pending": len(pend_rows),
                         "pending_by_channel": sorted(({"channel": k, "count": v} for k, v in pend_by_ch.items()),
                                                      key=lambda x: -x["count"]),
                         "status": st, "remark": rem})
    graphics.sort(key=lambda x: (0 if x["status"] == "active" else 1,
                                 -x["done_count"], -x["pending"], x["name"].lower()))

    # ---- production manager: assigned (period) + uploaded per channel (period, with titles) ----
    assigned = db.query(ProductionEvent).filter(
        ProductionEvent.event == "editor_assigned",
        ProductionEvent.created_at >= s, ProductionEvent.created_at < e).count()
    # LIVE VIEWS REFRESH: report 6 PM se pehle dikhta hai -> is period ke uploaded videos ki
    # views abhi refresh kar do taaki number current rahe. Batched, best-effort (fail -> purani
    # value). Jinki views pichhle ~5 min me already refresh hui, unhe skip (rapid reload guard).
    try:
        from video_tasks import _yt_get_key, _yt_fetch_views
        _up_tids = [row[0] for row in db.query(ProductionEvent.task_id).filter(
            ProductionEvent.event == "youtube_link_added",
            ProductionEvent.created_at >= s, ProductionEvent.created_at < e).distinct().all()]
        if _up_tids:
            _now_utc = datetime.utcnow()
            _vid_map = {}   # yt_video_id -> task
            for _t in db.query(VideoTask).filter(VideoTask.id.in_(_up_tids)).all():
                _vid = (getattr(_t, "yt_video_id", "") or "").strip()
                if not _vid:
                    continue
                _va = getattr(_t, "yt_views_at", None)
                if _va and (_now_utc - _va).total_seconds() < 300:
                    continue   # refreshed very recently
                _vid_map[_vid] = _t
            if _vid_map:
                _key = _yt_get_key(db)
                _got = _yt_fetch_views(list(_vid_map.keys()), _key)
                for _vid, _views in _got.items():
                    _tt = _vid_map.get(_vid)
                    if _tt is not None:
                        _tt.yt_views = _views
                        _tt.yt_views_at = _now_utc
                db.commit()
    except Exception:
        db.rollback()
    up_by_channel = {}
    tot_uploaded = 0
    tot_views = 0
    all_vids = []
    _seen_up = set()
    for uev in db.query(ProductionEvent).filter(
            ProductionEvent.event == "youtube_link_added",
            ProductionEvent.created_at >= s, ProductionEvent.created_at < e).all():
        if uev.task_id in _seen_up:   # a video can get the "added" event more than once
            continue
        _seen_up.add(uev.task_id)
        t = db.query(VideoTask).filter(VideoTask.id == uev.task_id).first()
        ch = _ch(t)
        title = ((t.title if t else "") or "Untitled")[:90]
        views = int(getattr(t, "yt_views", 0) or 0) if t else 0
        d = up_by_channel.setdefault(ch, {"channel": ch, "count": 0, "views": 0, "videos": []})
        d["count"] += 1
        d["views"] += views
        d["videos"].append({"title": title, "views": views})
        all_vids.append({"title": title, "channel": ch, "views": views})
        tot_uploaded += 1
        tot_views += views
    for d in up_by_channel.values():
        d["videos"].sort(key=lambda v: -v["views"])
    uploaded = sorted(up_by_channel.values(), key=lambda x: (-x["views"], -x["count"]))
    top_videos = sorted(all_vids, key=lambda v: -v["views"])[:(10 if period == "monthly" else 5)]

    # ---- graphics channel-wise totals (thumbnails done + pending per channel) ----
    gfx_ch = {}
    for g in graphics:
        for d in g.get("done", []):
            c = d.get("channel") or "No channel"
            gfx_ch.setdefault(c, {"channel": c, "done": 0, "pending": 0})["done"] += 1
        for p in g.get("pending_by_channel", []):
            c = p.get("channel") or "No channel"
            gfx_ch.setdefault(c, {"channel": c, "done": 0, "pending": 0})["pending"] += p.get("count", 0)
    graphics_channels = sorted(gfx_ch.values(), key=lambda x: (-(x["done"] + x["pending"]), x["channel"]))
    tot_thumbs = sum(g.get("done_count", 0) for g in graphics)

    # currently editing / paused totals
    now_editing = db.query(VideoTask).filter(VideoTask.cancelled.isnot(True),
                                             VideoTask.lifecycle == "editing").count()
    now_paused = db.query(VideoTask).filter(VideoTask.cancelled.isnot(True),
                                            VideoTask.lifecycle == "editing_paused").count()

    return {
        "period": period, "range_label": range_label, "date": day_str, "is_daily": is_daily,
        "generated_at": (datetime.utcnow() + IST).strftime("%d %b %Y, %I:%M %p"),
        "editors": editors, "graphics": graphics, "graphics_channels": graphics_channels,
        "production": {"assigned": assigned, "uploaded": uploaded,
                       "total_uploaded": tot_uploaded, "total_views": tot_views,
                       "top_videos": top_videos},
        "totals": {"completed": tot_completed, "assigned": assigned,
                   "uploaded": tot_uploaded, "editing": now_editing, "paused": now_paused,
                   "views": tot_views, "thumbnails": tot_thumbs},
    }


@router.get("/report/attendance")
def pm_report_attendance(date: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """All active editors + graphics with their attendance override for a given IST day (for the PM control)."""
    from models import ProductionStaffProfile as _SP, ProductionAttendance as _ATT
    IST = timedelta(hours=5, minutes=30)
    try:
        anchor = datetime.fromisoformat(date) if date else (datetime.utcnow() + IST)
    except Exception:
        anchor = datetime.utcnow() + IST
    day_str = anchor.strftime("%Y-%m-%d")
    att = {a.staff_id: a for a in db.query(_ATT).filter(_ATT.day == day_str).all()}
    out = []
    for sp in db.query(_SP).filter(_SP.staff_role.in_(["editor", "graphics"]),
                                   _SP.is_active == True).all():  # noqa: E712
        a = att.get(sp.id)
        out.append({"staff_id": sp.id, "name": (sp.user.name if sp.user else "#" + str(sp.id)),
                    "role": sp.staff_role,
                    "status": (a.status if a else ""), "remark": (a.remark if a else "")})
    out.sort(key=lambda x: (x["role"], x["name"].lower()))
    return {"date": day_str, "staff": out}


@router.post("/report/attendance")
def pm_set_attendance(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """PM marks a staff member present (+remark) or on leave for a day; '' clears the override."""
    from models import ProductionAttendance as _ATT
    IST = timedelta(hours=5, minutes=30)
    sid = int(payload.get("staff_id") or 0)
    if not sid:
        raise HTTPException(400, "staff_id required")
    day_str = (payload.get("date") or "").strip() or (datetime.utcnow() + IST).strftime("%Y-%m-%d")
    status = (payload.get("status") or "").strip().lower()
    remark = (payload.get("remark") or "").strip()[:400]
    row = db.query(_ATT).filter(_ATT.staff_id == sid, _ATT.day == day_str).first()
    if status not in ("present", "leave"):
        # clear override
        if row:
            db.delete(row)
            db.commit()
        return {"ok": True, "cleared": True}
    if not row:
        row = _ATT(staff_id=sid, day=day_str)
        db.add(row)
    row.status = status
    row.remark = remark
    row.set_by = getattr(me, "id", None)
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "status": status, "remark": remark}


@router.post("/tasks/{tid}/complete")
def mark_completed(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if t.lifecycle != "uploaded":
        raise HTTPException(400, "Only uploaded tasks can be completed")
    pc.set_state(db, t, "completed", actor=me, event="uploaded")
    db.commit()
    return {"ok": True, "lifecycle": t.lifecycle}


# ============================================================ TEAM / WORKLOAD
# ---- Production Team management (PM-accessible; reuses the admin logic) ----
@router.get("/team-users")
def pm_team_users(role: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import admin_routes as _ar
    return _ar.list_production_users(role=role, db=db, _=None)


@router.post("/team-users")
def pm_team_users_create(payload: dict = Body(...), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    import admin_routes as _ar
    return _ar.create_production_user(payload=payload, db=db, _=None)


@router.patch("/team-users/{uid}")
def pm_team_users_update(uid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    import admin_routes as _ar
    return _ar.update_production_user(uid=uid, payload=payload, db=db, _=None)


@router.delete("/team-users/{uid}")
def pm_team_users_delete(uid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import admin_routes as _ar
    return _ar.delete_production_user(uid=uid, db=db, _=None)


@router.post("/team-users/{uid}/reset-password")
def pm_team_users_reset(uid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import admin_routes as _ar
    return _ar.reset_production_password(uid=uid, payload=None, db=db, _=None)


@router.get("/live-team")
def pm_live_team(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Who on the production team is online right now — editors, graphics, YouTubers and PMs.
    Light: only recent sessions + the matching users (no full-table scan)."""
    from models import UserSession, User, UserRole
    now = datetime.now()
    cutoff = now - timedelta(minutes=3)
    # "Live" is judged on last_active (tab focused + interacting), NOT just an open tab.
    # An idle/backgrounded portal, or an old client that never reports activity, is not counted.
    live_ids = {}
    for uid, last, page, started in db.query(
            UserSession.user_id, func.max(UserSession.last_active),
            func.max(UserSession.current_page), func.max(UserSession.started_at)
        ).filter(UserSession.last_active != None, UserSession.last_active >= cutoff,  # noqa: E711
                 UserSession.ended_at == None).group_by(UserSession.user_id).all():
        live_ids[uid] = (last, page, started)
    _roles = [UserRole.editor, UserRole.graphics, UserRole.youtuber, UserRole.production_manager]
    people = []
    if live_ids:
        for u in db.query(User).filter(User.id.in_(list(live_ids.keys())),
                                       User.role.in_(_roles)).all():
            last, page, started = live_ids.get(u.id, (None, None, None))
            role = getattr(u.role, "value", str(u.role))
            people.append({"user_id": u.id, "name": u.name or "", "code": u.user_id or "",
                           "role": role, "page": page or "—",
                           "duration_min": max(0, int((now - (started or last or now)).total_seconds() // 60))})
    people.sort(key=lambda x: -x["duration_min"])
    counts = {"editors": sum(1 for p in people if p["role"] == "editor"),
              "graphics": sum(1 for p in people if p["role"] == "graphics"),
              "youtubers": sum(1 for p in people if p["role"] == "youtuber"),
              "pms": sum(1 for p in people if p["role"] == "production_manager"),
              "total": len(people)}
    return {"people": people, "counts": counts}


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
                base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == sp.id)
                active = base.filter(VideoTask.lifecycle.in_(
                    ["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"])).count()
                completed = base.filter(VideoTask.lifecycle.in_(["ready_for_youtube", "uploaded", "completed"])).count()
                # editor judged on the editor's own deadline, not the teacher deadline
                overdue = base.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline < now,
                                      ~VideoTask.lifecycle.in_(["uploaded", "completed", "ready_for_youtube"])).count()
                due_today = base.filter(VideoTask.editor_deadline != None, func.date(VideoTask.editor_deadline) == today).count()
            else:
                gbase = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id, GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))
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
        base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.creator_type == "youtuber",
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


@router.get("/team-tracker")
def pm_team_tracker(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Live snapshot of the whole team — current/paused/queued/completed per editor and
    graphics designer, with live active-editing time. Read-only."""
    return pc.build_team_tracker(db)


# ============================================================ PEOPLE (dropdowns)
@router.get("/people")
def pm_people(role: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Light lists for assignment dropdowns: editors, graphics, youtubers, teachers."""
    out = {}
    if role in ("", "editor"):
        _ed_active = ["editor_assigned", "editing_soon", "editing", "editing_paused",
                      "editing_done", "qc_pending", "qc_changes"]
        eds = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "editor",
            ProductionStaffProfile.is_active == True).all()
        out["editors"] = []
        for s in eds:
            active = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == s.id,
                                                VideoTask.lifecycle.in_(_ed_active)).count()
            pending = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == s.id,
                                                 VideoTask.lifecycle.in_(["editor_assigned", "editing_soon"])).count()
            out["editors"].append({"id": s.id, "name": s.user.name if s.user else "",
                                   "recommended": s.recommended_load or 5,
                                   "active": active, "pending": pending})
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
        try:
            from category_models import teacher_assigned_subjects as _tas
        except Exception:
            _tas = None
        teachers = []
        for t in rows:
            subs = []
            nios_subjects = []
            cats = []
            in_nios = True
            info = _tas(db, t.id) if _tas else None
            if info and info.get("has_any"):
                # AUTHORITATIVE: strictly Category Access (image: "Category Access").
                in_nios = bool(info.get("in_nios"))
                for nm, cl in info.get("nios", []):
                    if nm:
                        nios_subjects.append({"name": nm, "class": cl})
                        lbl = (nm + (" " + cl if cl else "")).strip()
                        if lbl not in subs:
                            subs.append(lbl)
                cats = info.get("cats", [])
                for c in cats:
                    for nm in c.get("subjects", []):
                        if nm and nm not in subs:
                            subs.append(nm)
            else:
                # legacy fallback — teacher has NO category rows (older/un-migrated)
                try:
                    for sc in (t.subject_classes or []):
                        nm = (sc.get("subject") or "").strip()
                        cl = str(sc.get("class") or "").strip()
                        label = (nm + (" " + cl if cl else "")).strip()
                        if label and label not in subs:
                            subs.append(label)
                            nios_subjects.append({"name": nm, "class": cl})
                except Exception:
                    pass
                if not subs:
                    try:
                        for nm in (t.subjects or []):
                            nm = (nm or "").strip()
                            if nm and nm not in subs:
                                subs.append(nm)
                                nios_subjects.append({"name": nm, "class": ""})
                    except Exception:
                        pass
            teachers.append({"id": t.id, "name": t.user.name if t.user else "",
                             "subjects": subs, "nios_subjects": nios_subjects,
                             "categories": cats, "in_nios": in_nios})
        out["teachers"] = teachers
    return out


# ============================================================ CREATOR PERFORMANCE
@router.get("/creators")
def pm_creators(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    now = datetime.utcnow()
    try:
        from video_tasks import (VT_COMPLETED_STATUSES as _COMPLETED, _collab_all_ids as _cai,
                                 NOT_SPECIAL as _NS)
    except Exception:
        _COMPLETED = {"approved", "editing_soon", "editing_done", "uploaded"}
        _cai = None
        _NS = None

    def _is_completed(t):
        return (getattr(t, "status", "") or "") in _COMPLETED

    def _is_overdue(t):
        return bool(t.deadline and t.deadline < now and not _is_completed(t))

    # Teacher name map
    tname = {}
    for tp in db.query(TeacherProfile).all():
        tname[tp.id] = (tp.user.name if tp.user else "") or ""

    # All eligible teacher tasks (mirror admin's teacher stats: normal kind, not a pending
    # proposal, not cancelled). Special/project tasks use chapter approval, not status.
    tq = db.query(VideoTask).filter(VideoTask.creator_type == "teacher",
                                    VideoTask.cancelled.isnot(True),
                                    VideoTask.proposal_ok != "pending")
    if _NS is not None:
        tq = tq.filter(_NS)
    all_tasks = tq.all()

    def _blank(nm):
        return {"name": nm, "videos": 0, "completed": 0, "pending": 0, "overdue": 0,
                "solo_videos": 0, "individual_views": 0, "collab_views": 0, "collab_videos": 0,
                "_otd": 0, "_oth": 0}

    tstats = {}
    collab = {"name": "Collab", "is_collab": True, "videos": 0, "completed": 0,
              "pending": 0, "overdue": 0, "views": 0, "individual_views": 0,
              "collab_views": 0, "collab_videos": 0, "on_time_pct": None}
    collab_seen = set()
    _otd_c = 0
    _oth_c = 0

    for t in all_tasks:
        ids = _cai(t) if _cai else ([t.teacher_id] if t.teacher_id else [])
        is_collab = len(ids) > 1
        v = int(getattr(t, "yt_views", 0) or 0)
        comp = _is_completed(t)
        over = _is_overdue(t)
        if is_collab:
            # count the shared video ONCE in the Collab row
            if t.id not in collab_seen:
                collab_seen.add(t.id)
                collab["videos"] += 1
                collab["collab_videos"] += 1
                if comp:
                    collab["completed"] += 1
                else:
                    collab["pending"] += 1
                if over:
                    collab["overdue"] += 1
                collab["views"] += v
                collab["collab_views"] += v
                _otc = getattr(t, "on_time", None)
                if _otc is None and getattr(t, "submitted_at", None) and t.deadline:
                    _otc = (t.submitted_at <= t.deadline)
                if comp and _otc is not None:
                    _otd_c += 1
                    if _otc:
                        _oth_c += 1
            # each collaborator: the collab video counts toward THEIR totals too (same as views),
            # so a teacher who only does collab work still shows their completions.
            for tid in ids:
                s = tstats.setdefault(tid, _blank(tname.get(tid, "")))
                s["collab_videos"] += 1
                s["videos"] += 1
                s["collab_views"] += v
                if comp:
                    s["completed"] += 1
                else:
                    s["pending"] += 1
                if over:
                    s["overdue"] += 1
                _otm = getattr(t, "on_time", None)
                if _otm is None and getattr(t, "submitted_at", None) and t.deadline:
                    _otm = (t.submitted_at <= t.deadline)
                if comp and _otm is not None:
                    s["_otd"] += 1
                    if _otm:
                        s["_oth"] += 1
        else:
            tid = t.teacher_id
            if not tid:
                continue
            s = tstats.setdefault(tid, _blank(tname.get(tid, "")))
            s["videos"] += 1
            s["solo_videos"] += 1
            if comp:
                s["completed"] += 1
            else:
                s["pending"] += 1
            if over:
                s["overdue"] += 1
            s["individual_views"] += v
            _ot = getattr(t, "on_time", None)
            if _ot is None and getattr(t, "submitted_at", None) and t.deadline:
                _ot = (t.submitted_at <= t.deadline)
            if comp and _ot is not None:
                s["_otd"] += 1
                if _ot:
                    s["_oth"] += 1

    teachers = []
    for tid, s in tstats.items():
        s["id"] = tid
        s["views"] = s["individual_views"] + s["collab_views"]
        s["on_time_pct"] = round(100.0 * s["_oth"] / s["_otd"]) if s["_otd"] else None
        s.pop("_otd", None)
        s.pop("_oth", None)
        if s["videos"] or s["collab_videos"]:
            teachers.append(s)
    teachers.sort(key=lambda x: (x["videos"] + x["collab_videos"]), reverse=True)

    if _otd_c:
        collab["on_time_pct"] = round(100.0 * _oth_c / _otd_c)
    collab_out = [collab] if collab["videos"] else []

    youtubers = []
    for yp in db.query(YouTuberProfile).all():
        base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.creator_type == "youtuber", VideoTask.youtuber_id == yp.id)
        if base.count() == 0:
            continue
        done = ["uploaded", "completed", "ready_for_youtube"]
        total = base.count()
        completed = base.filter(VideoTask.lifecycle.in_(done)).count()
        views = int(base.with_entities(func.coalesce(func.sum(VideoTask.yt_views), 0)).scalar() or 0)
        comp = base.filter(VideoTask.lifecycle.in_(done), VideoTask.published_at != None, VideoTask.deadline != None).all()
        den = len(comp); hit = sum(1 for t in comp if t.published_at <= t.deadline)
        youtubers.append({"videos": total, "completed": completed,
                          "pending": total - completed,
                          "overdue": base.filter(VideoTask.deadline != None, VideoTask.deadline < now,
                                                 ~VideoTask.lifecycle.in_(done)).count(),
                          "views": views, "on_time_pct": round(100.0 * hit / den) if den else None,
                          "name": yp.user.name if yp.user else "", "id": yp.id,
                          "published": base.filter(VideoTask.lifecycle.in_(["uploaded", "completed"])).count()})
    youtubers.sort(key=lambda x: x["videos"], reverse=True)

    return {"teachers": teachers, "collab": collab_out, "youtubers": youtubers}


@router.get("/creator-videos")
def pm_creator_videos(teacher_id: int = 0, cat: str = "",
                      db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Drill-down for the Creator Performance chips — the videos behind a teacher's (or the
    Collab row's) Completed / Pending / Overdue count. teacher_id=0 => the Collab bucket."""
    now = datetime.utcnow()
    try:
        from video_tasks import (VT_COMPLETED_STATUSES as _COMPLETED, _collab_all_ids as _cai,
                                 NOT_SPECIAL as _NS)
    except Exception:
        _COMPLETED = {"approved", "editing_soon", "editing_done", "uploaded"}
        _cai = None
        _NS = None
    def _comp(t):
        return (getattr(t, "status", "") or "") in _COMPLETED
    def _over(t):
        return bool(t.deadline and t.deadline < now and not _comp(t))
    tq = db.query(VideoTask).filter(VideoTask.creator_type == "teacher",
                                    VideoTask.cancelled.isnot(True),
                                    VideoTask.proposal_ok != "pending")
    if _NS is not None:
        tq = tq.filter(_NS)
    cat = (cat or "").strip().lower()
    out = []
    for t in tq.all():
        ids = _cai(t) if _cai else ([t.teacher_id] if t.teacher_id else [])
        is_collab = len(ids) > 1
        if teacher_id > 0:
            if teacher_id not in ids:
                continue
        else:
            if not is_collab:
                continue
        if cat == "completed" and not _comp(t):
            continue
        if cat == "pending" and _comp(t):
            continue
        if cat == "overdue" and not _over(t):
            continue
        st = "completed" if _comp(t) else ("overdue" if _over(t) else "pending")
        out.append({"id": t.id, "title": t.title or "Untitled", "state": st,
                    "status": (getattr(t, "status", "") or ""), "views": int(getattr(t, "yt_views", 0) or 0),
                    "on_youtube": bool((getattr(t, "yt_video_id", "") or "").strip() or (t.youtube_url or "").strip()),
                    "deadline": pc._dt(t.deadline), "subject": t.subject or "",
                    "is_collab": is_collab, "youtube_url": t.youtube_url or ""})
    out.sort(key=lambda x: x["views"], reverse=True)
    return {"videos": out, "count": len(out)}


# ============================================================ REAL-TIME VIEWS
@router.get("/views")
def pm_views(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    uploaded_q = db.query(VideoTask).filter(VideoTask.yt_video_id != None, VideoTask.yt_video_id != "")
    uploaded = uploaded_q.count()
    total_views = int(db.query(func.coalesce(func.sum(VideoTask.yt_views), 0)).filter(
        VideoTask.yt_video_id != None, VideoTask.yt_video_id != "").scalar() or 0)
    pending_upload = db.query(VideoTask).filter(VideoTask.lifecycle == "ready_for_youtube").count()

    vids = uploaded_q.all()
    try:
        from video_tasks import _collab_all_ids as _cai, _teacher_name as _ctn
    except Exception:
        _cai = _ctn = None
    by_creator = {}
    videos = []
    for t in vids:
        v = int(t.yt_views or 0)
        real_name, real_ctype = pc.creator_info(db, t)
        is_collab = False
        team = []
        if _cai:
            try:
                _ids = _cai(t)
                is_collab = len(_ids) > 1
                if is_collab and _ctn:
                    team = sorted({(_ctn(db, _i) or "") for _i in _ids if (_ctn(db, _i) or "")})
            except Exception:
                is_collab = False
        # collab video -> TEAM-specific bucket: same set of teachers merge into one "Collab"
        # row, a different set of teachers becomes its own separate "Collab" row (no mixing).
        if is_collab:
            gname = ("Collab: " + " + ".join(team)) if team else "Collab"
            gctype = "collab"
        else:
            gname, gctype = (real_name or "Unknown"), real_ctype
        key = (gname or "Unknown") + "|" + gctype
        c = by_creator.setdefault(key, {"name": gname or "Unknown", "creator_type": gctype.lower(),
                                        "views": 0, "videos": 0, "is_collab": is_collab,
                                        "collab_names": team})
        c["views"] += v; c["videos"] += 1
        videos.append({"id": t.id, "title": t.title or "Untitled", "ref_code": t.ref_code or "",
                       "creator": real_name or "Unknown", "creator_type": real_ctype.lower(),
                       "is_collab": is_collab, "collab_names": team,
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
        add(db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id.in_(eids)).limit(30).all(), "Editor")
    if gids:
        add(db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.graphics_id.in_(gids)).limit(30).all(), "Graphics")

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
            base = db.query(VideoTask).filter(VideoTask.cancelled == False, VideoTask.editor_id == pid)
            active = base.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"])).count()
            completed = base.filter(VideoTask.lifecycle.in_(done)).count()
            completed_m = base.filter(VideoTask.lifecycle.in_(done), VideoTask.updated_at >= month_start).count()
            overdue = base.filter(VideoTask.deadline != None, VideoTask.deadline < now, ~VideoTask.lifecycle.in_(done)).count()
            secs = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(EditingSession.editor_id == pid).scalar() or 0
            comp = base.filter(VideoTask.lifecycle.in_(done), VideoTask.published_at != None, VideoTask.deadline != None).all()
            ot_den = len(comp); ot_hit = sum(1 for t in comp if t.published_at <= t.deadline)
            stats = {"active": active, "completed": completed, "completed_this_month": completed_m,
                     "overdue": overdue, "active_hours": round(float(secs) / 3600.0, 1),
                     "on_time_pct": round(100.0 * ot_hit / ot_den) if ot_den else None,
                     "recommended_load": sp.recommended_load or 5}
            recent = base.order_by(VideoTask.updated_at.desc()).limit(8).all()
            _act = base.filter(VideoTask.lifecycle.in_(["editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes"])).order_by(VideoTask.updated_at.desc()).all()
            active_tasks = []
            for _t in _act:
                _ts = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(
                    EditingSession.editor_id == pid, EditingSession.task_id == _t.id).scalar() or 0
                active_tasks.append({
                    "id": _t.id, "title": _t.title or "", "ref_code": _t.ref_code or "",
                    "lifecycle": _t.lifecycle or "",
                    "deadline": pc._dt_raw(_t.deadline) if _t.deadline else "",
                    "editing_hours": round(float(_ts) / 3600.0, 1),
                    "editing_started": (pc._dt(_t.editing_started_at) if getattr(_t, "editing_started_at", None) else ""),
                })
            _all = base.order_by(VideoTask.updated_at.desc()).limit(80).all()
            all_tasks = []
            for _t in _all:
                _done2 = _t.lifecycle in done
                _act2 = _t.lifecycle in ("editor_assigned", "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes")
                _ov2 = (_t.deadline is not None and _t.deadline < now and not _done2)
                _ts2 = db.query(func.coalesce(func.sum(EditingSession.duration_seconds), 0)).filter(
                    EditingSession.editor_id == pid, EditingSession.task_id == _t.id).scalar() or 0
                all_tasks.append({
                    "id": _t.id, "title": _t.title or "", "ref_code": _t.ref_code or "",
                    "lifecycle": _t.lifecycle or "",
                    "deadline": pc._dt_raw(_t.deadline) if _t.deadline else "",
                    "editing_hours": round(float(_ts2) / 3600.0, 1),
                    "active": _act2, "completed": _done2, "overdue": _ov2,
                    "this_month": bool(_done2 and _t.updated_at and _t.updated_at >= month_start),
                })
        else:
            gbase = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == pid, GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))
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
            active_tasks = [{"id": t.id, "title": t.title or "", "ref_code": t.ref_code or "",
                             "lifecycle": t.lifecycle or "", "editing_hours": None,
                             "deadline": pc._dt_raw(t.deadline) if t.deadline else "", "editing_started": ""}
                            for t in recent]
            all_tasks = []
            for g in gbase.order_by(GraphicsTask.created_at.desc()).limit(80).all():
                gt = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
                if not gt:
                    continue
                _doneg = (g.status == "approved")
                all_tasks.append({
                    "id": gt.id, "title": gt.title or "", "ref_code": gt.ref_code or "",
                    "lifecycle": gt.lifecycle or "", "editing_hours": None,
                    "deadline": pc._dt_raw(gt.deadline) if gt.deadline else "",
                    "active": g.status in ("new", "in_progress", "changes"),
                    "completed": _doneg, "overdue": False,
                    "this_month": bool(_doneg and g.approved_at and g.approved_at >= month_start),
                })
        return {"kind": kind, "name": name, "stats": stats,
                "active_tasks": active_tasks, "all_tasks": all_tasks,
                "recent": [pc.task_out(db, t, light=True) for t in recent]}

    if kind == "youtuber":
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == pid).first()
        if not yp:
            raise HTTPException(404, "Not found")
        base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.youtuber_id == pid, VideoTask.cancelled == False)
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
@router.get("/admin-analytics")
def admin_analytics(days: int = 30, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """System-level production analytics for Admin oversight. Real task data only —
    every metric is traceable to VideoTask / GraphicsTask / EditingSession rows."""
    now = datetime.utcnow()
    start = now - timedelta(days=max(1, min(365, days)))
    _done = ["uploaded", "completed"]
    _editing_states = ["editor_assigned", "editing", "editing_paused", "editing_done"]

    all_active = db.query(VideoTask).filter(VideoTask.cancelled == False).all()

    def has(t, *st):
        return t.lifecycle in st

    # ---- TASK HEALTH (9 canonical buckets) ----
    task_health = {
        "assigned": sum(1 for t in all_active if t.lifecycle not in ("", "created")),
        "completed": sum(1 for t in all_active if has(t, "completed", "uploaded")),
        "pending": sum(1 for t in all_active if t.lifecycle not in _done and t.lifecycle not in ("", "created")),
        "overdue": sum(1 for t in all_active if t.deadline and t.deadline < now and t.lifecycle not in _done),
        "pm_review": sum(1 for t in all_active if has(t, "pm_review", "creator_submitted")),
        "editing": sum(1 for t in all_active if t.lifecycle in _editing_states),
        "qc_pending": sum(1 for t in all_active if has(t, "qc_pending")),
        "ready_for_youtube": sum(1 for t in all_active if has(t, "ready_for_youtube")),
        "uploaded": sum(1 for t in all_active if has(t, "uploaded", "completed")),
    }

    # ---- TEACHERS (assigned / submitted / approved / reshoot / overdue / output) ----
    teacher_tasks = [t for t in all_active if (t.creator_type or "teacher") == "teacher"]
    # BATCH teacher names for the DISTINCT teachers (was 1 query PER task = N+1)
    _atids = list({t.teacher_id for t in teacher_tasks if t.teacher_id})
    _aname = {}
    if _atids:
        for _tid, _nm in db.query(TeacherProfile.id, User.name).join(
                User, TeacherProfile.user_id == User.id).filter(TeacherProfile.id.in_(_atids)):
            _aname[_tid] = _nm or ""
    tmap = {}
    for t in teacher_tasks:
        name = (_aname.get(t.teacher_id) or "Unassigned") if t.teacher_id else "Unassigned"
        d = tmap.setdefault(t.teacher_id or 0, {"name": name, "assigned": 0, "submitted": 0,
                                                "approved": 0, "reshoot": 0, "overdue": 0, "output": 0})
        d["assigned"] += 1
        if t.submitted_at or t.lifecycle not in ("", "created", "creator_assigned", "creator_working"):
            d["submitted"] += 1
        if t.lifecycle not in ("", "created", "creator_assigned", "creator_working", "pm_review", "changes_required", "reshoot_required"):
            d["approved"] += 1
        if t.lifecycle == "reshoot_required":
            d["reshoot"] += 1
        if t.deadline and t.deadline < now and t.lifecycle not in _done:
            d["overdue"] += 1
        if t.lifecycle in _done:
            d["output"] += 1
    teachers = sorted(tmap.values(), key=lambda x: -x["output"])
    teachers_total = {k: sum(r[k] for r in teachers) for k in ("assigned", "submitted", "approved", "reshoot", "overdue", "output")}

    # ---- GRAPHICS (assigned / completed / approval_pending / changes / rating / turnaround) ----
    gfx = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.staff_role == "graphics").all()
    gfx_rows = []
    for sp in gfx:
        gts = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id).all()
        completed_g = [g for g in gts if g.status == "approved"]
        turns = [((g.approved_at - g.started_at).total_seconds() / 3600.0)
                 for g in completed_g if g.started_at and g.approved_at]
        ratings = [g.quality_rating for g in completed_g if g.quality_rating]
        gfx_rows.append({
            "name": sp.user.name if sp.user else "",
            "assigned": len(gts),
            "completed": len(completed_g),
            "approval_pending": sum(1 for g in gts if g.status == "submitted"),
            "changes": sum(1 for g in gts if g.status == "changes"),
            "rating": round(sum(ratings) / len(ratings), 1) if ratings else 0,
            "turnaround": round(sum(turns) / len(turns), 1) if turns else 0,
        })
    gfx_rows.sort(key=lambda x: -x["completed"])
    gfx_total = {"assigned": sum(r["assigned"] for r in gfx_rows), "completed": sum(r["completed"] for r in gfx_rows),
                 "approval_pending": sum(r["approval_pending"] for r in gfx_rows), "changes": sum(r["changes"] for r in gfx_rows),
                 "rating": round(sum(r["rating"] for r in gfx_rows if r["rating"]) / max(1, sum(1 for r in gfx_rows if r["rating"])), 1) if any(r["rating"] for r in gfx_rows) else 0}

    # ---- EDITORS (assigned / active / completed / changes / overdue / quality / turnaround) ----
    eds = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.staff_role == "editor").all()
    ed_rows = []
    for sp in eds:
        ets = [t for t in all_active if t.editor_id == sp.id]
        completed_e = [t for t in ets if t.lifecycle in ["editing_done", "qc_pending", "ready_for_youtube", "uploaded", "completed"]]
        turns = [((t.editing_done_at - t.editing_started_at).total_seconds() / 3600.0)
                 for t in ets if t.editing_started_at and t.editing_done_at and t.editing_done_at >= t.editing_started_at]
        ratings = [t.quality_rating for t in ets if t.quality_rating]
        ed_rows.append({
            "name": sp.user.name if sp.user else "",
            "assigned": len(ets),
            "active": sum(1 for t in ets if t.lifecycle in ["editing", "editing_paused"]),
            "completed": len(completed_e),
            "changes": sum(1 for t in ets if t.lifecycle == "qc_changes"),
            "overdue": sum(1 for t in ets if t.deadline and t.deadline < now and t.lifecycle not in _done),
            "quality": round(sum(ratings) / len(ratings), 1) if ratings else 0,
            "turnaround": round(sum(turns) / len(turns), 1) if turns else 0,
        })
    ed_rows.sort(key=lambda x: -x["completed"])
    ed_total = {k: sum(r[k] for r in ed_rows) for k in ("assigned", "active", "completed", "changes", "overdue")}
    ed_total["quality"] = round(sum(r["quality"] for r in ed_rows if r["quality"]) / max(1, sum(1 for r in ed_rows if r["quality"])), 1) if any(r["quality"] for r in ed_rows) else 0

    # ---- YOUTUBERS (proposed / assigned / active / completed / uploaded / views) ----
    yts = db.query(YouTuberProfile).all()
    yt_rows = []
    for yp in yts:
        yts_tasks = [t for t in all_active if (t.creator_type or "") == "youtuber" and t.youtuber_id == yp.id]
        yt_rows.append({
            "name": yp.user.name if yp.user else "",
            "proposed": sum(1 for t in yts_tasks if t.lifecycle in ["created", "pm_review"]),
            "assigned": len(yts_tasks),
            "active": sum(1 for t in yts_tasks if t.lifecycle in _editing_states + ["editing", "editing_paused", "qc_pending", "qc_changes"]),
            "completed": sum(1 for t in yts_tasks if t.lifecycle in _done),
            "uploaded": sum(1 for t in yts_tasks if t.lifecycle in _done and t.youtube_url),
            "views": sum(int(t.yt_views or 0) for t in yts_tasks),
        })
    yt_rows.sort(key=lambda x: -x["views"])
    yt_total = {k: sum(r[k] for r in yt_rows) for k in ("proposed", "assigned", "active", "completed", "uploaded", "views")}

    # ---- MAJOR DELAYS (most overdue active tasks) ----
    delays = []
    for t in all_active:
        if t.deadline and t.deadline < now and t.lifecycle not in _done:
            od_h = (now - t.deadline).total_seconds() / 3600.0
            cname = ""
            if (t.creator_type or "") == "youtuber" and t.youtuber_id:
                yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
                cname = (yp.user.name if (yp and yp.user) else "")
            elif t.teacher_id:
                tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
                cname = (tp.user.name if (tp and tp.user) else "")
            delays.append({"id": t.id, "title": t.title or "", "ref_code": t.ref_code or "",
                           "stage": pc.LC.get(t.lifecycle, t.lifecycle), "creator": cname,
                           "overdue_hours": round(od_h, 1)})
    delays.sort(key=lambda x: -x["overdue_hours"])
    delays = delays[:20]

    # ---- REVIEW QUEUES (pending decisions) ----
    review_queues = {
        "pm_review": sum(1 for t in all_active if t.lifecycle in ["pm_review", "creator_submitted"]),
        "thumbnail_review": db.query(GraphicsTask).filter(GraphicsTask.status == "submitted").count(),
        "qc_review": sum(1 for t in all_active if t.lifecycle == "qc_pending"),
        "proposals": sum(1 for t in all_active if t.lifecycle == "created" and (t.creator_type or "") == "youtuber"),
    }

    # ---- TREND (weekly created vs completed, last 8 weeks) ----
    trend = []
    for w in range(7, -1, -1):
        wk_start = now - timedelta(days=(w + 1) * 7)
        wk_end = now - timedelta(days=w * 7)
        c_created = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                               VideoTask.created_at >= wk_start, VideoTask.created_at < wk_end).count()
        c_done = db.query(VideoTask).filter(VideoTask.published_at != None,
                                            VideoTask.published_at >= wk_start, VideoTask.published_at < wk_end).count()
        trend.append({"label": wk_end.strftime("%d %b"), "created": c_created, "completed": c_done})

    return {
        "task_health": task_health,
        "teachers": {"total": teachers_total, "rows": teachers[:10]},
        "graphics": {"total": gfx_total, "rows": gfx_rows},
        "editors": {"total": ed_total, "rows": ed_rows},
        "youtubers": {"total": yt_total, "rows": yt_rows},
        "major_delays": delays,
        "review_queues": review_queues,
        "trend": trend,
    }


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

    # ---- long vs short classification (CANONICAL — same rule everywhere; rapid != short) ----
    import performance_core as _PC
    def _is_short(vt):
        return _PC.format_category(_PC.get_video_format(vt)) == "short"
    # task_id -> video_type for EVERY task (cheap 2-col pull) so project chapters inherit
    # their parent project's video_type for long/short classification.
    try:
        _vt_of = dict(db.query(VideoTask.id, VideoTask.video_type).all())
    except Exception:
        _vt_of = {}
    from models import VideoTaskChapter as _VCp
    # finished project chapters grouped by editor (edit submitted = a completed video for the editor)
    _ed_chaps = {}
    try:
        for _c in db.query(_VCp).filter(_VCp.edit_state == "edited", _VCp.editor_id != None).all():
            _ed_chaps.setdefault(_c.editor_id, []).append(_c)
    except Exception:
        pass

    def _secs_task(t):
        try:
            return int(getattr(t, "editing_seconds", 0) or 0)
        except Exception:
            return 0

    def _secs_chap(c):
        st = getattr(c, "editing_started_at", None); en = getattr(c, "edited_at", None)
        return int((en - st).total_seconds()) if (st and en and en > st) else 0

    editors = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "editor").all()

    def _ed_row(sp, cat):
        want_short = (cat == "short")
        etasks = [t for t in completed if t.editor_id == sp.id and _is_short(t.video_type) == want_short]
        chaps = [c for c in _ed_chaps.get(sp.id, []) if _is_short(_vt_of.get(c.task_id, "")) == want_short]
        vids = len(etasks) + len(chaps)
        if not vids:
            return None
        secs = sum(_secs_task(t) for t in etasks) + sum(_secs_chap(c) for c in chaps)
        ot_den = sum(1 for t in etasks if t.deadline) + sum(1 for c in chaps if getattr(c, "deadline", None))
        ot_hit = sum(1 for t in etasks if t.deadline and t.published_at and t.published_at <= t.deadline)
        ot_hit += sum(1 for c in chaps if getattr(c, "deadline", None) and getattr(c, "edited_at", None) and c.edited_at <= c.deadline)
        revs = sum((t.revision_count or 0) for t in etasks) + sum((getattr(c, "qc_revision", 0) or 0) for c in chaps)
        return {"name": sp.user.name if sp.user else "", "videos": vids,
                "active_hours": round(secs / 3600.0, 1),
                "avg_hours": round((secs / 3600.0) / vids, 1) if vids else 0,
                "on_time_pct": round(100.0 * ot_hit / ot_den) if ot_den else None,
                "revisions": revs}

    editors_long, editors_short = [], []
    for sp in editors:
        rl = _ed_row(sp, "long"); rs = _ed_row(sp, "short")
        if rl:
            editors_long.append(rl)
        if rs:
            editors_short.append(rs)
    editors_long.sort(key=lambda x: -x["videos"])
    editors_short.sort(key=lambda x: -x["videos"])
    # combined list (backward compatible) — long + short merged per editor
    _ed_comb = {}
    for r in editors_long + editors_short:
        c = _ed_comb.setdefault(r["name"], {"name": r["name"], "videos": 0, "active_hours": 0.0,
                                            "revisions": 0, "_othit": 0, "_otden": 0})
        c["videos"] += r["videos"]; c["active_hours"] += r["active_hours"]; c["revisions"] += r["revisions"]
    ed_rows = sorted(_ed_comb.values(), key=lambda x: -x["videos"])
    for r in ed_rows:
        r["active_hours"] = round(r["active_hours"], 1)
        r["avg_hours"] = round(r["active_hours"] / r["videos"], 1) if r["videos"] else 0
        r.pop("_othit", None); r.pop("_otden", None)

    # ---- graphics performance (also split long vs short by the task's video_type) ----
    gfx = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "graphics").all()
    # finished thumbnails this graphics member made on project chapters
    _gfx_chaps = {}
    try:
        for _c in db.query(_VCp).filter(_VCp.graphics_id != None,
                                        _VCp.thumbnail_link != "").all():
            if (getattr(_c, "thumbnail_link", "") or "").strip():
                _gfx_chaps.setdefault(_c.graphics_id, []).append(_c)
    except Exception:
        pass

    def _gfx_row(sp, cat, gts_all):
        want_short = (cat == "short")
        gts = [g for g in gts_all if _is_short(_vt_of.get(g.task_id, "")) == want_short]
        chaps = [c for c in _gfx_chaps.get(sp.id, []) if _is_short(_vt_of.get(c.task_id, "")) == want_short]
        cnt = len(gts) + len(chaps)
        if not cnt:
            return None
        design_h = [((g.approved_at - g.started_at).total_seconds() / 3600.0)
                    for g in gts if g.started_at and g.approved_at]
        revs = sum((g.revision_count or 0) for g in gts)
        return {"name": sp.user.name if sp.user else "", "thumbnails": cnt,
                "avg_hours": round(sum(design_h) / len(design_h), 1) if design_h else 0,
                "revisions": revs}

    gfx_rows, gfx_long, gfx_short = [], [], []
    for sp in gfx:
        gts_all = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id,
                                                GraphicsTask.status == "approved",
                                                GraphicsTask.approved_at != None,
                                                GraphicsTask.approved_at >= start).all()
        rl = _gfx_row(sp, "long", gts_all); rs = _gfx_row(sp, "short", gts_all)
        if rl:
            gfx_long.append(rl)
        if rs:
            gfx_short.append(rs)
        tot = (len(gts_all) + len(_gfx_chaps.get(sp.id, [])))
        if tot:
            design_h = [((g.approved_at - g.started_at).total_seconds() / 3600.0)
                        for g in gts_all if g.started_at and g.approved_at]
            gfx_rows.append({"name": sp.user.name if sp.user else "", "thumbnails": tot,
                             "avg_hours": round(sum(design_h) / len(design_h), 1) if design_h else 0,
                             "revisions": sum((g.revision_count or 0) for g in gts_all)})
    gfx_rows.sort(key=lambda x: -x["thumbnails"])
    gfx_long.sort(key=lambda x: -x["thumbnails"])
    gfx_short.sort(key=lambda x: -x["thumbnails"])

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
            "editors_long": editors_long, "editors_short": editors_short,
            "graphics": gfx_rows, "graphics_long": gfx_long, "graphics_short": gfx_short,
            "content_mix": content_mix, "trend": trend}


# ============================================================ UNIFIED PERFORMANCE (perf §35)
# Admin/PM see the SAME engine as the staff — one source of truth. Leaderboards + per-staff drilldown.
@router.get("/performance")
def pm_performance(period: str = "month", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import performance_core as PC
    now = datetime.utcnow()
    try:
        PC.maybe_daily_snapshot(db, now)
    except Exception:
        pass
    _, _, plabel = PC.period_bounds(period, now)

    def _rows(lb, snapcat):
        out = []
        for r in lb:
            mv = PC.rank_movement(db, r["staff_id"], snapcat, r["rank"], ref=now)
            out.append({"staff_id": r["staff_id"], "name": r["name"], "rank": r["rank"],
                        "score": r["score"], "edited": r["edited"], "approved": r["approved"],
                        "avg_quality": r["avg_quality"], "on_time_pct": r["on_time_pct"],
                        "first_pass_pct": r.get("first_pass_pct"), "provisional": r["provisional"],
                        "movement": mv.get("movement", 0), "previous_rank": mv.get("previous_rank")})
        return out
    lb_long = PC.compute_editor_leaderboard(db, PC.CAT_LONG, period, ref=now)
    lb_short = PC.compute_editor_leaderboard(db, PC.CAT_SHORT, period, ref=now)
    lb_gfx = PC.compute_graphics_leaderboard(db, period, ref=now)
    return {"period": plabel, "period_key": period,
            "editors_long": _rows(lb_long, "editor_long"),
            "editors_short": _rows(lb_short, "editor_short"),
            "graphics": _rows(lb_gfx, "graphics")}


@router.get("/performance/staff")
def pm_performance_staff(role: str, id: int, period: str = "month",
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Full premium performance for ONE staff member (PM/Admin drilldown). Same engine."""
    import performance_core as PC
    sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == id).first()
    if not sp:
        raise HTTPException(404, "Staff not found")
    now = datetime.utcnow()
    name = sp.user.name if sp.user else ""
    if role == "graphics":
        items = PC.get_graphics_work_items(db, sp.id, now=now)
        perf = PC.compute_graphics_performance(sp, items, period, ref=now)
        lb = PC.compute_graphics_leaderboard(db, period, ref=now)
        rank, total = PC.find_rank(lb, sp.id)
        return {"role": "graphics", "name": name, "period": perf["period"],
                "score": perf["score"], "score_breakdown": perf["score_breakdown"],
                "thumbnails": perf["thumbnails"], "approved": perf["approved"],
                "pending": perf["pending"], "overdue": perf["overdue"],
                "revisions": perf["revisions"], "avg_quality": perf["avg_quality"],
                "on_time_pct": perf["on_time_pct"], "first_pass_pct": perf["first_pass_pct"],
                "normal_work": perf["normal_work"], "project_work": perf["project_work"],
                "provisional": perf["provisional"], "rank": rank, "total_ranked": total,
                "rank_movement": PC.rank_movement(db, sp.id, "graphics", rank, ref=now),
                "badges": PC.graphics_badges(perf),
                "personal_bests": PC.personal_bests(db, sp.id, "graphics", ref=now)}
    # editor
    items = PC.get_editor_work_items(db, sp.id, now=now)
    perf = PC.compute_editor_performance(sp, items, period, ref=now)
    cat = perf["primary_category"]
    snapcat = "editor_long" if cat == PC.CAT_LONG else "editor_short"
    lb = PC.compute_editor_leaderboard(db, cat, period, ref=now)
    rank, total = PC.find_rank(lb, sp.id)
    return {"role": "editor", "name": name, "period": perf["period"],
            "specialization": perf["specialization"], "primary_category": cat,
            "overall": perf["overall"], "long": perf["long"], "short": perf["short"],
            "rank": rank, "total_ranked": total,
            "rank_movement": PC.rank_movement(db, sp.id, snapcat, rank, ref=now),
            "badges": PC.editor_badges(perf),
            "personal_bests": PC.personal_bests(db, sp.id, snapcat, ref=now)}


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


def _notify_task_teacher(db, t, title, msg, link=None):
    """Notify the video's teacher (or youtuber creator) — used by the thumbnail flow."""
    _notify_creator(db, t, title, msg)


def _notify_teacher_by_profile(db, teacher_profile_id, title, msg, task_id=None):
    """Notify a teacher directly by their TeacherProfile id (used by collab edit)."""
    try:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_profile_id).first()
        if tp and tp.user_id:
            pc.notify(db, tp.user_id, title, msg, "video_task", link=(str(task_id) if task_id else None))
    except Exception:
        pass


# ============================================================ MY PROFILE (photo)
def _pm_staff(db, me):
    from models import ProductionStaffProfile
    return db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.user_id == getattr(me, "id", None)).first()


@router.post("/me/photo")
def pm_photo_set(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    sp = _pm_staff(db, me)
    if not sp:
        raise HTTPException(400, "No production profile for this account")
    sp.photo_b64 = (payload.get("photo") or "").strip() or None
    db.commit()
    return {"ok": True, "has_photo": bool(sp.photo_b64)}


@router.get("/me/photo")
def pm_photo_get(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    sp = _pm_staff(db, me)
    return {"photo": (sp.photo_b64 if sp else "") or "", "name": getattr(me, "name", ""), "role": "production_manager"}


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


@router.patch("/channels/{cid}")
def prod_rename_channel(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    c = db.query(VideoChannel).filter(VideoChannel.id == cid).first()
    if not c:
        raise HTTPException(404, "Channel not found")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Channel name is required")
    if db.query(VideoChannel).filter(VideoChannel.name == name, VideoChannel.id != cid).first():
        raise HTTPException(400, "Another channel already has this name")
    c.name = name
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


@router.delete("/channels/{cid}")
def prod_delete_channel(cid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    c = db.query(VideoChannel).filter(VideoChannel.id == cid).first()
    if not c:
        raise HTTPException(404, "Channel not found")
    c.active = False
    db.commit()
    return {"ok": True}


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


@router.patch("/video-types/{tid}")
def prod_rename_type(tid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                     me=Depends(get_pm_or_admin)):
    c = db.query(VideoType).filter(VideoType.id == tid).first()
    if not c:
        raise HTTPException(404, "Video type not found")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Type name is required")
    if db.query(VideoType).filter(VideoType.name == name, VideoType.id != tid).first():
        raise HTTPException(400, "Another type already has this name")
    c.name = name
    scope = (payload.get("streaming_scope") or "").strip().lower()
    if scope in ("both", "live", "recorded"):
        c.streaming_scope = scope
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


@router.delete("/video-types/{tid}")
def prod_delete_type(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    c = db.query(VideoType).filter(VideoType.id == tid).first()
    if not c:
        raise HTTPException(404, "Video type not found")
    c.active = False
    db.commit()
    return {"ok": True}


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


def _prod_active_students(db):
    from models import StudentProfile
    return (db.query(StudentProfile).join(User, StudentProfile.user_id == User.id)
            .filter(User.is_active == True, User.role == "student").all())  # noqa: E712


def _teacher_students(db, teacher_id):
    """All active students of a teacher (same rule as the teacher's own notify: subject overlap;
    if the teacher has no subjects on file, fall back to all active students)."""
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_id).first() if teacher_id else None
    if tp is not None:
        try:
            from teacher_routes import _my_students
            got = _my_students(db, tp)
            if got is not None:
                return got
        except Exception:
            pass
    return _prod_active_students(db)


def auto_notify_students_video(db, teacher_id, link, title, channel_name="", actor_id=None):
    """AUTO-send a PUBLISHED YouTube link to all of a teacher's students as a portal
    notification. Only the YouTube link is ever shared. Returns the number sent."""
    link = (link or "").strip()
    if not (teacher_id and link):
        return 0
    import uuid
    batch = uuid.uuid4().hex[:24]
    ntitle = "New Video: %s" % (title or "")
    msg = ('New video "%s" is now live%s. Tap to watch on YouTube.'
           % (title or "", (" on %s" % channel_name) if channel_name else ""))
    sent = 0
    for sp in _teacher_students(db, teacher_id):
        uid = getattr(sp, "user_id", None)
        if not uid:
            continue
        db.add(Notification(user_id=uid, title=ntitle, message=msg, notif_type="video_link",
                            link=link, image_url=None, sender_id=actor_id,
                            sender_role="production", batch_key=batch,
                            batch_label="Auto · Students notified on publish"))
        sent += 1
    return sent


@router.get("/tasks/{tid}/notify-targets")
def prod_notify_targets(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Recipient options for 'Send to Students' — all / by class / by subject, with live counts."""
    _task(db, tid)
    students = _prod_active_students(db)
    cls_ids, subj, all_ids = {}, {}, set()
    for sp in students:
        if not sp.user_id:
            continue
        all_ids.add(sp.id)
        cl = str(getattr(sp, "class_level", "") or "").strip() or "?"
        cls_ids.setdefault(cl, set()).add(sp.id)
        for s in (sp.subjects or []):
            nm = str(s or "").strip()
            if not nm:
                continue
            key = nm + "|" + cl
            d = subj.get(key)
            if not d:
                d = {"key": key, "name": nm, "class": cl, "ids": set()}
                subj[key] = d
            d["ids"].add(sp.id)
    classes = [{"class_level": c, "count": len(ids)} for c, ids in sorted(cls_ids.items())]
    subjects = [{"key": d["key"], "name": d["name"], "class": d["class"], "count": len(d["ids"])}
                for d in subj.values()]
    subjects.sort(key=lambda x: (-x["count"], x["name"], x["class"]))
    return {"classes": classes, "subjects": subjects, "all_count": len(all_ids)}


@router.post("/tasks/{tid}/notify-students")
def prod_notify_students(tid: int, payload: dict = Body(default={}),
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Send the PUBLISHED YouTube link to selected students (all / class / subject / custom).
    Only the YouTube link is ever sent — never the teacher/editor raw upload. Tracked per batch."""
    import uuid
    t = _task(db, tid)
    link = (t.youtube_url or "").strip()
    if not link:
        raise HTTPException(400, "Post the YouTube link first — students receive the published video, not the raw upload.")
    mode = (payload.get("mode") or "all").strip()
    classes = [str(c).strip() for c in (payload.get("classes") or []) if str(c).strip()]
    subj_keys = [str(s).strip() for s in (payload.get("subjects") or []) if str(s).strip()]
    student_ids = set(int(x) for x in (payload.get("student_ids") or []) if str(x).strip().isdigit())
    students = _prod_active_students(db)
    picked, label = {}, ""
    if mode == "classes":
        cset = set(classes)
        for sp in students:
            if (str(getattr(sp, "class_level", "") or "").strip()) in cset:
                picked[sp.id] = sp
        label = ("Class " + ", ".join(classes)) if classes else "Selected classes"
    elif mode == "subjects":
        kset = set(subj_keys)
        for sp in students:
            cl = str(getattr(sp, "class_level", "") or "").strip() or "?"
            for s in (sp.subjects or []):
                if (str(s).strip() + "|" + cl) in kset:
                    picked[sp.id] = sp
                    break
        _names = []
        for k in subj_keys:
            nm = k.split("|")[0]
            if nm not in _names:
                _names.append(nm)
        label = (", ".join(_names[:3]) + (" +%d more" % (len(_names) - 3) if len(_names) > 3 else "")) or "Selected subjects"
    elif mode == "custom":
        for sp in students:
            if sp.id in student_ids:
                picked[sp.id] = sp
        label = "%d selected student%s" % (len(picked), "" if len(picked) == 1 else "s")
    else:  # all
        for sp in students:
            picked[sp.id] = sp
        label = "All Students"
    if not picked:
        raise HTTPException(400, "No students matched your selection.")
    msg = (payload.get("message") or "").strip() or \
        ('New video "%s" is now live%s. Tap to watch on YouTube.' %
         (t.title or "", (" on %s" % t.channel_name) if t.channel_name else ""))
    batch = uuid.uuid4().hex[:24]
    title = "New Video: %s" % (t.title or "")
    is_admin = (getattr(me, "role", None) == UserRole.admin)
    sent = 0
    for sp in picked.values():
        if not sp.user_id:
            continue
        db.add(Notification(user_id=sp.user_id, title=title, message=msg, notif_type="video_link",
                            link=link, image_url=None, sender_id=me.id,
                            sender_role=("admin" if is_admin else "production"),
                            batch_key=batch, batch_label=label))
        sent += 1
    try:
        pc.log_event(db, t, me, "sent_to_students",
                     meta={"note": "Sent to %d student%s (%s)" % (sent, "" if sent == 1 else "s", label),
                           "batch": batch})
    except Exception:
        pass
    db.commit()
    return {"ok": True, "count": sent, "batch_key": batch, "label": label}


@router.get("/tasks/{tid}/notify-log")
def prod_notify_log(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """All 'Send to Students' batches for this task's published video, with reached/viewed/clicked."""
    t = _task(db, tid)
    link = (t.youtube_url or "").strip()
    if not link:
        return {"campaigns": []}
    rows = (db.query(Notification)
            .filter(Notification.notif_type == "video_link", Notification.link == link,
                    Notification.batch_key.isnot(None))
            .order_by(Notification.created_at.desc()).all())
    batches = {}
    for n in rows:
        b = batches.get(n.batch_key)
        if not b:
            b = {"batch_key": n.batch_key, "label": n.batch_label or "Students", "title": n.title,
                 "message": n.message, "sent": 0, "viewed": 0, "clicked": 0,
                 "created_at": n.created_at.isoformat() if n.created_at else None}
            batches[n.batch_key] = b
        b["sent"] += 1
        if n.is_read:
            b["viewed"] += 1
        if n.clicked_at:
            b["clicked"] += 1
    out = sorted(batches.values(), key=lambda b: b["created_at"] or "", reverse=True)
    return {"campaigns": out}


@router.get("/notify-log/{batch_key}")
def prod_notify_log_detail(batch_key: str, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Recipient list for one batch — who it reached, who viewed, who clicked the link."""
    from models import StudentProfile
    rows = (db.query(Notification)
            .filter(Notification.batch_key == batch_key, Notification.notif_type == "video_link")
            .order_by(Notification.created_at.asc()).all())
    if not rows:
        raise HTTPException(404, "Batch not found")
    uids = list({n.user_id for n in rows if n.user_id})
    umap, clsmap = {}, {}
    if uids:
        for uid, nm in db.query(User.id, User.name).filter(User.id.in_(uids)):
            umap[uid] = nm or "Student"
        for sp in db.query(StudentProfile).filter(StudentProfile.user_id.in_(uids)).all():
            clsmap[sp.user_id] = str(getattr(sp, "class_level", "") or "")
    out = []
    for n in rows:
        out.append({"name": umap.get(n.user_id, "Student"), "class": clsmap.get(n.user_id, ""),
                    "read": bool(n.is_read), "read_at": n.read_at.isoformat() if n.read_at else None,
                    "clicked": bool(n.clicked_at), "clicked_at": n.clicked_at.isoformat() if n.clicked_at else None})
    out.sort(key=lambda r: (r["clicked"], r["read"], r["name"].lower()), reverse=True)
    return {"batch_key": batch_key, "title": rows[0].title, "label": rows[0].batch_label or "Students",
            "sent": len(out), "viewed": sum(1 for r in out if r["read"]),
            "clicked": sum(1 for r in out if r["clicked"]), "recipients": out}


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
    if (payload.get("title") or "").strip():
        t.title = payload["title"].strip()
    for f in ("channel_name", "video_type", "subject", "reference", "streaming",
              "reference_video", "remarks"):
        v = (payload.get(f) or "").strip()
        if v:
            setattr(t, f, v)
    _cl = (payload.get("class_level") or "").strip()
    if _cl:
        try:
            t.class_level = _cl
        except Exception:
            pass
    _treq = payload.get("thumbnail_required")
    if _treq is not None:
        try:
            t.thumbnail_required = bool(_treq)
        except Exception:
            pass
    # Final thumbnail: uploaded image or a drive link
    _thumb = payload.get("thumbnail")
    if _thumb:
        try:
            _tu = pc.save_images(db, t, _thumb if isinstance(_thumb, list) else [_thumb],
                                 "thumbnail", None, me, return_urls=True) or []
            if _tu:
                t.thumbnail_link = _tu[0]
        except Exception:
            pass
    _tlink = (payload.get("thumbnail_link") or "").strip()
    if _tlink:
        t.thumbnail_link = _tlink
    # Prepared thumbnail: record who made it + rating, so it shows in that designer's portal
    _mb = str(payload.get("thumbnail_made_by") or "").strip()
    if _mb:
        try:
            from models import ProductionStaffProfile as _PSP
            _mkr = db.query(_PSP).filter(_PSP.id == int(_mb), _PSP.staff_role == "graphics").first()
            if _mkr:
                g = pc.graphics_task(db, t, create=True)
                g.graphics_id = _mkr.id
                t.graphics_id = _mkr.id
                if t.thumbnail_link:
                    g.thumbnail_url = t.thumbnail_link
                g.status = "approved"
                g.approved_at = datetime.utcnow()
                try:
                    _rt = int(payload.get("thumbnail_rating") or 0)
                    if 1 <= _rt <= 5:
                        g.quality_rating = _rt
                except Exception:
                    pass
                pc.log_event(db, t, me, "thumbnail_approved", new_state=t.lifecycle,
                             meta={"note": "Prepared thumbnail credited to designer" + ((" (%d/5)" % g.quality_rating) if getattr(g, "quality_rating", 0) else "")})
                if _mkr.user_id:
                    pc.notify(db, _mkr.user_id, "Thumbnail Credited",
                              'Your thumbnail for "%s" was used.' % t.title,
                              "appreciation" if (getattr(g, "quality_rating", 0) or 0) >= 4 else "video_task", link=str(t.id))
        except Exception:
            pass
    # Assign to graphics (with multiple reference images) if the PM chose a designer
    _gid = str(payload.get("graphics_id") or "").strip()
    if _gid:
        try:
            from models import ProductionStaffProfile
            gr = db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.id == int(_gid),
                ProductionStaffProfile.staff_role == "graphics").first()
            if gr:
                g = pc.graphics_task(db, t, create=True)
                g.graphics_id = gr.id
                t.graphics_id = gr.id
                if (g.status or "") in ("", "new"):
                    g.status = "new"
                # teacher's reference is only a reference for the designer — it must NOT act as
                # the final thumbnail, so clear the task's thumbnail fields until graphics delivers.
                t.thumbnail_b64 = None
                t.thumbnail_link = ""
                _all_refs = []
                _refs = payload.get("reference_images")
                if _refs:
                    _ru = pc.save_images(db, t, _refs if isinstance(_refs, list) else [_refs],
                                         "reference", None, me, return_urls=True) or []
                    _all_refs.extend(_ru)
                # teacher's proposed reference thumbnail (from proposal time) -> designer ko auto mile
                # teacher's proposed reference thumbnails (multiple, from proposal) -> designer ko auto milein
                try:
                    import json as _jp
                    _pr = _jp.loads(getattr(t, "proposal_refs", "") or "[]")
                    if isinstance(_pr, list):
                        for _pu in _pr:
                            if _pu and _pu not in _all_refs:
                                _all_refs.append(_pu)
                except Exception:
                    pass
                _tlnk = (t.thumbnail_link or "").strip()
                if _tlnk and _tlnk.startswith("http") and _tlnk not in _all_refs:
                    _all_refs.append(_tlnk)
                _tb = getattr(t, "thumbnail_b64", "") or ""
                if _tb and isinstance(_tb, str) and _tb.startswith("data:"):
                    try:
                        _tu = pc.save_images(db, t, [_tb], "reference", None, me, return_urls=True) or []
                        for _u in _tu:
                            if _u not in _all_refs:
                                _all_refs.append(_u)
                    except Exception:
                        pass
                if _all_refs:
                    g.reference_image = _all_refs[0]
                    try:
                        import json as _jr
                        g.reference_images = _jr.dumps(_all_refs)
                    except Exception:
                        pass
                if gr.user_id:
                    pc.notify(db, gr.user_id, "New Thumbnail Task",
                              'You were assigned a thumbnail for "%s".' % t.title, "video_task", link=str(t.id))
        except Exception:
            pass
    # collab teachers (primary stays the proposer)
    raw = payload.get("collab_teacher_ids")
    if isinstance(raw, list):
        import json as _jc
        ids = []
        for x in raw:
            try:
                xi = int(x)
                if xi and xi != t.teacher_id and xi not in ids:
                    ids.append(xi)
            except Exception:
                pass
        t.collab_teacher_ids = _jc.dumps(ids) if ids else None
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
def prod_report_csv(creator_type: str = "", db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import csv, io
    from sqlalchemy import or_ as _or
    NOT_SPECIAL = _or(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")
    try:
        from video_tasks import _teacher_name as _tn
    except Exception:
        def _tn(db, tid): return ""
    _is_yt = (creator_type or "").strip().lower() == "youtuber"
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending")
    if not _is_yt:
        q = q.filter(NOT_SPECIAL)
    if (creator_type or "").strip().lower() in ("teacher", "youtuber"):
        q = q.filter(VideoTask.creator_type == creator_type.strip().lower())
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    _fname = ("youtuber_report.csv" if _is_yt else "production_report.csv")
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
                    headers={"Content-Disposition": "attachment; filename=" + _fname})


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
             "verified": bool(vmap.get(str(i)))} for i in ids]
    _sub = getattr(t, "submitted_by", None)
    return {"is_collab": True, "collaborators": cols,
            "submitted_by_name": (_c_tname(db, _sub) if _sub else "") or "",
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


@router.post("/tasks/{tid}/edit-collab")
def prod_edit_collab(tid: int, payload: dict = Body(...),
                     db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Add or remove collaborating teachers on an existing task (PM/Admin).
    The primary teacher cannot be removed. Payout logic is untouched — this only
    edits the collaborator list and cleans per-teacher verify/not-completed flags."""
    import json as _j
    t = _task(db, tid)
    primary = t.teacher_id
    # desired ADDITIONAL collaborators (excluding primary)
    raw = payload.get("teacher_ids")
    if raw is None:
        raw = payload.get("collab_teacher_ids") or []
    new_ids = []
    for x in raw:
        try:
            xi = int(x)
        except Exception:
            continue
        if xi and xi != primary and xi not in new_ids:
            new_ids.append(xi)
    old_ids = []
    try:
        old_ids = [int(x) for x in (_j.loads(t.collab_teacher_ids) if t.collab_teacher_ids else [])]
    except Exception:
        old_ids = []
    added = [i for i in new_ids if i not in old_ids]
    removed = [i for i in old_ids if i not in new_ids]
    t.collab_teacher_ids = _j.dumps(new_ids)
    # clean verify / not-completed maps for removed teachers
    for field in ("collab_verified", "collab_not_completed"):
        try:
            m = _j.loads(getattr(t, field) or "{}")
        except Exception:
            m = {}
        for rid in removed:
            m.pop(str(rid), None)
        setattr(t, field, _j.dumps(m))
    # history + notifications
    for i in added:
        nm = _c_tname(db, i) or ("Teacher #%s" % i)
        try: _c_hist(t, "collab_added", "%s added to collaboration by production manager" % nm)
        except Exception: pass
        _notify_teacher_by_profile(db, i, "Added to a collaboration",
                                   'You have been added to "%s".' % (t.title or "a task"), t.id)
    for i in removed:
        nm = _c_tname(db, i) or ("Teacher #%s" % i)
        try: _c_hist(t, "collab_removed", "%s removed from collaboration by production manager" % nm)
        except Exception: pass
        _notify_teacher_by_profile(db, i, "Removed from a collaboration",
                                   'You are no longer part of "%s".' % (t.title or "a task"), t.id)
    db.commit()
    ids = _c_all_ids(t)
    vmap = _c_vmap(t)
    return {"ok": True, "added": len(added), "removed": len(removed),
            "collaborators": [{"id": i, "name": _c_tname(db, i) or ("Teacher #%s" % i),
                               "verified": bool(vmap.get(str(i))), "primary": (i == primary)} for i in ids]}


@router.get("/collab-teachers")
def prod_collab_teachers(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """All active teachers (id + name) for the collab add/remove picker."""
    out = []
    for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
        out.append({"id": tp.id, "name": (tp.user.name if tp.user else ("Teacher #%s" % tp.id))})
    out.sort(key=lambda x: x["name"].lower())
    return {"teachers": out}


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
    from models import AvailableSubject, TeacherProfile
    out = {"10": [], "12": [], "UG-PG": []}
    _catalog = set()   # NIOS 10/12 subject names (lowercased) — sab yahan aate hain
    for s in db.query(AvailableSubject).filter(AvailableSubject.is_active == True).all():
        out.setdefault(s.class_level, []).append(
            {"id": s.id, "name": s.name, "code": s.code, "mode": (s.mode or "live")})
        _catalog.add((s.name or "").strip().lower())
    # UG-PG (college) subjects — AUTHORITATIVE: Category Access ke non-NIOS categories
    # ke CategorySubject (du_sol / UG-PG papers). Wahi naam jo admin ne banaye hain.
    _seen = set()
    _ug = []
    try:
        from category_models import Category, CategorySubject
        noncat_ids = [c.id for c in db.query(Category).filter(
            Category.internal_key != "nios").all()]
        if noncat_ids:
            for cs in db.query(CategorySubject).filter(
                    CategorySubject.category_id.in_(noncat_ids)).all():
                _nm = (cs.name or "").strip()
                if _nm and _nm.lower() not in _seen:
                    _seen.add(_nm.lower()); _ug.append(_nm)
    except Exception:
        pass
    out["UG-PG"] = [{"id": 0, "name": n, "code": "", "mode": "recorded"} for n in sorted(_ug)]
    return out


@router.get("/project/subject-teachers")
def prod_project_subject_teachers(subject: str = "", class_level: str = "",
                                  db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        return {"teachers": []}
    return {"teachers": _p_subject_teachers(db, subject, class_level)}


@router.get("/project/chapters-preview")
def prod_project_chapters_preview(subject: str = "", class_level: str = "", scope: str = "",
                                  group: str = "", teacher_id: int = 0, db: Session = Depends(get_db),
                                  me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        return {"count": 0, "titles": [], "source": "none"}
    subject = (subject or "").strip()
    if not subject:
        return {"count": 0, "titles": [], "source": "none"}
    if class_level not in ("10", "12"):
        class_level = ""
    tp = _p_teacher_profile(db, teacher_id) if teacher_id else None
    titles, src = _p_chapters_for(db, tp.id if tp else 0, subject, class_level, scope, group)
    _catp, _ = _p_chapters_for(db, tp.id if tp else 0, subject, class_level, "", "categories")
    return {"count": len(titles), "titles": titles[:8], "source": src,
            "has_categories": len(_catp) > 0}


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
    p_group = (payload.get("chapter_group") or "").strip().lower()
    if p_group not in ("chapters", "categories"):
        p_group = ""
    item_source, items = "custom", []
    if connect and subject:
        items, _src = _p_chapters_for(db, tp.id, subject, class_level, scope, p_group)
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
    def _rv(c):
        rs = (getattr(c, "review_status", "") or "").strip()
        if rs in ("pending", "approved", "changes"):
            return rs
        return "approved" if (c.link or "").strip() else ""
    _pe = getattr(t, "project_editor_id", None)
    import video_tasks as _vt
    nm = _staff_name_map(db, [c.editor_id for c in rows] + [c.graphics_id for c in rows] + [_pe])
    return {"is_project": (getattr(t, "kind", "") == "project"),
            "project_editor_id": _pe, "project_editor_name": nm.get(_pe, ""),
            "chapters": [{"id": c.id, "title": c.title, "link": (c.link or ""),
                          "status": _p_ch_status(c),
                          "review": _rv(c),
                          "lifecycle": _vt._chapter_lifecycle(c),
                          "lifecycle_label": _vt._chapter_lifecycle_label(c),
                          "youtube_url": (getattr(c, "youtube_url", "") or ""),
                          "editor_inherited": bool(getattr(c, "editor_inherited", False)),
                          "review_note": (getattr(c, "review_note", "") or ""),
                          "editor_id": c.editor_id, "editor_name": nm.get(c.editor_id, ""),
                          "graphics_id": c.graphics_id, "graphics_name": nm.get(c.graphics_id, ""),
                          "edit_state": (getattr(c, "edit_state", "") or ""),
                          "edited_link": (getattr(c, "edited_link", "") or ""),
                          "edited_at": (c.edited_at.strftime("%d %b %Y, %I:%M %p") if getattr(c, "edited_at", None) else ""),
                          "qc_status": (getattr(c, "qc_status", "") or ""),
                          "qc_note": (getattr(c, "qc_note", "") or ""),
                          "edit_review_status": (getattr(c, "edit_review_status", "") or ""),
                          "edit_review_note": (getattr(c, "edit_review_note", "") or ""),
                          "edit_reviewer_name": (getattr(c, "edit_reviewer_name", "") or ""),
                          "edit_review_rating": (getattr(c, "edit_review_rating", None)),
                          "thumbnail_link": (getattr(c, "thumbnail_link", "") or ""),
                          "gfx_state": (getattr(c, "gfx_state", "") or ""),
                          "assigned": bool(c.editor_id or c.graphics_id)} for c in rows]}


@router.get("/board-chapters")
def pm_board_chapters(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Phase 2b: submitted project CHAPTERS as board items, mapped to the board's lifecycle
    vocabulary so they slot into Editing / QC / Ready / Uploaded columns next to normal tasks.
    Shoot-pending chapters (no link) stay only in the Projects screen. Read-only, additive."""
    if not _PROJECT_OK:
        return {"chapters": []}
    import video_tasks as pc_vt
    rows = db.query(VideoTask).filter(VideoTask.cancelled == False,  # noqa: E712
                                      VideoTask.kind.in_(["one_shot", "rapid_revision", "project"])).all()
    tmap = {t.id: t for t in rows}
    ids = list(tmap.keys())
    out = []
    if ids:
        chs = db.query(_PVChapter).filter(_PVChapter.task_id.in_(ids)).all()
        nm = _staff_name_map(db, [c.editor_id for c in chs] + [c.graphics_id for c in chs])
        ccache = {}
        for c in chs:
            link = (c.link or "").strip()
            if not link:
                continue  # shoot pending -> not in the production pipeline yet
            rs = (getattr(c, "review_status", "") or "").strip()
            es = (getattr(c, "edit_state", "") or "")
            est = (getattr(c, "edit_status", "") or "")
            # CANONICAL lifecycle — fixes the old bug where a submitted edit (qc pending)
            # was shown as "ready_for_youtube" before QC approval.
            lc = pc_vt._chapter_lifecycle(c)
            t = tmap.get(c.task_id)
            cname = ""
            try:
                cname, _ = pc.creator_info(db, t)
            except Exception:
                pass
            out.append({
                "cid": c.id, "task_id": c.task_id, "title": c.title or "Chapter",
                "subject": (t.subject if t else ""), "teacher": cname, "ref_code": "PROJECT",
                "lifecycle": lc, "lifecycle_label": pc_vt.CHAPTER_STATE_LABELS.get(lc, ""), "link": link,
                "edited_link": (getattr(c, "edited_link", "") or ""),
                "thumbnail_link": (getattr(c, "thumbnail_link", "") or ""),
                "youtube_url": (getattr(c, "youtube_url", "") or ""),
                "editor_id": c.editor_id, "editor_name": nm.get(c.editor_id, ""),
                "graphics_id": c.graphics_id, "graphics_name": nm.get(c.graphics_id, ""),
                "review_status": rs, "edit_state": es, "edit_status": est,
                "qc_status": (getattr(c, "qc_status", "") or ""),
                "qc_note": (getattr(c, "qc_note", "") or ""),
                "edit_review_status": (getattr(c, "edit_review_status", "") or ""),
                "edit_review_note": (getattr(c, "edit_review_note", "") or ""),
                "deadline": pc._dt(getattr(c, "deadline", None)),
            })
    return {"chapters": out}


@router.get("/chapters/{cid}/chat")
def pm_chapter_chat(cid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import video_tasks as _vt
    return _vt.chapter_chat_get(db, me, cid)


@router.post("/chapters/{cid}/chat")
def pm_chapter_chat_add(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    import video_tasks as _vt
    return _vt.chapter_chat_add(db, me, cid, payload, "production_manager")


@router.post("/chapters/{cid}/chat-ping")
def pm_chapter_chat_ping(cid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    import video_tasks as _vt
    return _vt.chapter_chat_ping(db, me, cid, typing=bool((payload or {}).get("typing")))


@router.get("/chapters/{cid}/timeline")
def pm_chapter_timeline(cid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    import video_tasks as _vt
    return _vt.chapter_timeline(db, cid)


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


# ============================================================ PROJECTS LIST (premium section)
# Projects = one_shot / rapid_revision / project. Each shows chapter progress so the PM can
# see, per subject, how many videos are done. Same VideoTask + VideoTaskChapter data as admin.
@router.post("/chapter-review")
def prod_chapter_review(payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    """PM/admin approves or sends back a single project video (mirrors task approval)."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    return _vt._do_chapter_review(db, payload.get("chapter_id"), payload.get("action"),
                                  payload.get("note") or "")


@router.post("/chapter-qc")
def prod_chapter_qc(payload: dict = Body(...), db: Session = Depends(get_db),
                    me=Depends(get_pm_or_admin)):
    """PM/Admin QC on an editor's EDITED project video.
    approve -> QC passed (ready); changes -> sent back to the editor to redo."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    row = db.query(_PVChapter).filter(_PVChapter.id == int(payload.get("chapter_id") or 0)).first()
    if not row:
        raise HTTPException(404, "Video not found")
    if (getattr(row, "edit_state", "") or "") != "edited":
        raise HTTPException(400, "This video is not submitted for QC yet")
    action = (payload.get("action") or "").strip()
    note = (payload.get("note") or "").strip()
    t = db.query(VideoTask).filter(VideoTask.id == row.task_id).first()
    _ed_uid = None
    if row.editor_id:
        _sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == row.editor_id).first()
        _ed_uid = _sp.user_id if _sp else None
    import video_tasks as _vt
    if action == "approve":
        # QC pass -> ready_for_youtube (ONLY here; submitting an edit never jumps here)
        _vt.set_chapter_state(db, row, "ready_for_youtube", actor=me, note="QC approved", force=True)
        if _ed_uid:
            pc.notify(db, _ed_uid, "QC Approved",
                      f'Your edit of "{row.title}" passed QC.', "video_task", link=str(row.task_id))
        db.commit()
        return {"ok": True, "qc_status": row.qc_status, "lifecycle": row.lifecycle}
    if action == "changes":
        if not note:
            raise HTTPException(400, "Please add a short note about the changes")
        try:
            row.qc_revision = int(getattr(row, "qc_revision", 0) or 0) + 1
        except Exception:
            row.qc_revision = 1
        _vt.set_chapter_state(db, row, "qc_changes", actor=me, note=note[:600], force=True)
        if _ed_uid:
            pc.notify(db, _ed_uid, "Changes Required in your edit",
                      f'"{row.title}": {note[:140]}', "video_task", link=str(row.task_id))
        # record the change note in the chapter chat so the editor sees details
        try:
            _crole = "admin" if getattr(me, "role", "") == "admin" else "production_manager"
            _vt._vtc_add(db, row.task_id, me, "Changes required in the edited video:\n" + note,
                         _crole, "", _vt._chap_aud(row.id))
        except Exception:
            pass
        db.commit()
        return {"ok": True, "qc_status": row.qc_status, "lifecycle": row.lifecycle}
    raise HTTPException(400, "Unknown action")


@router.post("/chapter-upload-schedule")
def prod_chapter_upload_schedule(payload: dict = Body(...), db: Session = Depends(get_db),
                                 me=Depends(get_pm_or_admin)):
    """PM/Admin sets a tentative upload date + remarks for a QC-approved chapter."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    row = db.query(_PVChapter).filter(_PVChapter.id == int(payload.get("chapter_id") or 0)).first()
    if not row:
        raise HTTPException(404, "Video not found")
    _d = (payload.get("upload_date") or "").strip()
    if _d:
        try:
            row.upload_date = datetime.fromisoformat(_d.replace("Z", ""))
        except Exception:
            pass
    if "upload_remarks" in payload:
        row.upload_remarks = (payload.get("upload_remarks") or "").strip()[:600]
    db.commit()
    return {"ok": True, "chapter_id": row.id}


def _chapter_set_youtube(db, me, cid, url, youtuber_id=None):
    """Shared: a chapter's YouTube URL is posted -> uploaded -> completed. One chapter = one
    upload; stored at CHAPTER level (never the parent project's url). Used by PM/admin + YouTuber."""
    import video_tasks as _vt
    from video_tasks import _yt_extract_id, _yt_get_key, _yt_fetch_views
    row = db.query(_PVChapter).filter(_PVChapter.id == int(cid or 0)).first()
    if not row:
        raise HTTPException(404, "Video not found")
    lc = _vt._chapter_lifecycle(row)
    if lc not in ("ready_for_youtube", "upload_scheduled", "uploaded"):
        raise HTTPException(400, "This video is not QC-approved / ready for YouTube yet")
    url = (url or "").strip()
    vid = _yt_extract_id(url)
    if not vid:
        raise HTTPException(400, "Could not read a valid YouTube video id from that URL")
    row.youtube_url = url
    row.yt_video_id = vid
    if youtuber_id:
        row.youtuber_id = youtuber_id
    row.uploaded_at = datetime.utcnow()
    _vt.set_chapter_state(db, row, "uploaded", actor=me, note="YouTube URL added", force=True)
    _vt.set_chapter_state(db, row, "completed", actor=me, note="Published", force=True)
    t = db.query(VideoTask).filter(VideoTask.id == row.task_id).first()
    # notify editor + teacher(s) that the chapter is live
    try:
        if row.editor_id:
            ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == row.editor_id).first()
            if ep and ep.user_id:
                pc.notify(db, ep.user_id, "Your video is live",
                          f'"{row.title}" you edited is now on YouTube.', "appreciation", link=str(row.task_id))
    except Exception:
        pass
    # AUTO: teacher ke sabhi students ko chapter ka published YouTube link bhej do (sirf ek baar)
    try:
        if t is not None and not bool(getattr(row, "students_notified", False)):
            auto_notify_students_video(db, t.teacher_id, row.youtube_url, row.title,
                                       getattr(t, "channel_name", "") or "", actor_id=getattr(me, "id", None))
            row.students_notified = True
    except Exception:
        pass
    db.commit()
    return {"ok": True, "chapter_id": row.id, "youtube_url": url, "yt_video_id": vid,
            "lifecycle": row.lifecycle}


@router.post("/chapter-youtube")
def prod_chapter_youtube(payload: dict = Body(...), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    """PM/Admin posts the published YouTube URL for a ready chapter -> uploaded/completed."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    return _chapter_set_youtube(db, me, payload.get("chapter_id"), payload.get("youtube_url"))


# ============================================================================
# CHAPTER = FIRST-CLASS PRODUCTION TASK — parity actions (Phase 2/3)
# ----------------------------------------------------------------------------
# Shared helpers + the actions a normal VideoTask has that chapters were missing:
# submit-link (PM on behalf, audited), reshoot/reject with counts, rich editor
# assign, multi-candidate thumbnail submit + PM review/select-final/rate, PM
# direct thumbnail upload, credit-existing thumbnail/edit, link-existing YouTube
# reconciliation, and a separate PM editor quality rating. Every action is atomic,
# authorised (get_pm_or_admin), and records a timeline event via set_chapter_state /
# _chap_event. The business rules mirror the normal task; only the storage differs.
# ============================================================================
def _chap(db, cid):
    row = db.query(_PVChapter).filter(_PVChapter.id == int(cid or 0)).first()
    if not row:
        raise HTTPException(404, "Chapter not found")
    return row


def _actor_role(me):
    return "admin" if (getattr(me, "role", "") == "admin") else "production_manager"


def _actor_name(me):
    return (getattr(me, "name", "") or ("Admin" if getattr(me, "role", "") == "admin" else "Production Manager"))


def _chap_norm_images(imgs, bucket="thumbnails"):
    """Normalise a list of image inputs (data-URL / http URL) to stored URLs via R2.
    Mirrors the normal flow — never store raw base64 blobs in text columns."""
    out = []
    if not isinstance(imgs, list):
        return out
    r2 = None
    try:
        r2 = __import__("r2_storage")
    except Exception:
        r2 = None
    for im in imgs[:12]:
        s = (str(im or "")).strip()
        if not s:
            continue
        if s.startswith("http"):
            out.append(s); continue
        if r2 is not None and s.startswith("data:"):
            try:
                hint = s.split(",", 1)[0].split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
                out.append(r2.normalize(s, bucket, hint)); continue
            except Exception:
                pass
        out.append(s)   # last resort: keep as-is (short URL / drive link)
    return out


def _chap_thumb_final(c):
    """A chapter HAS a final thumbnail if the PM uploaded/credited/approved one."""
    return bool((getattr(c, "thumbnail_link", "") or "").strip())


def _chap_json_list(v):
    try:
        x = json.loads(v or "[]")
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _chap_push_candidate_history(c, urls, note=""):
    hist = _chap_json_list(getattr(c, "thumb_candidate_history", ""))
    import video_tasks as _vt
    hist.append({"round": len(hist) + 1,
                 "at": _vt._now_ist().strftime("%d %b %Y, %I:%M %p"),
                 "urls": list(urls or []), "note": (note or "")[:200]})
    c.thumb_candidate_history = json.dumps(hist[-30:])


def chapter_allowed_actions(c, role="pm"):
    """ONE allowed-actions engine for a chapter work-item (parity with the normal task's
    next_action). Returns {action: bool} so the UI never shows an impossible button and
    JS never re-implements lifecycle rules. role: pm | admin | editor | graphics | teacher | youtuber."""
    import video_tasks as _vt
    lc = _vt._chapter_lifecycle(c)
    link = bool((getattr(c, "link", "") or "").strip())
    edited = bool((getattr(c, "edited_link", "") or "").strip())
    gfx = (getattr(c, "gfx_state", "") or "")
    has_final = _chap_thumb_final(c)
    cands = _chap_json_list(getattr(c, "thumb_candidates", ""))
    is_pm = role in ("pm", "admin", "production_manager")
    A = {k: False for k in (
        "submit_link", "update_link", "approve_creator", "request_changes", "reshoot",
        "assign_editor", "assign_graphics", "credit_edit", "credit_thumbnail",
        "upload_thumbnail", "thumbnail_review", "qc_approve", "request_edit_changes",
        "rate_editor", "schedule_upload", "post_youtube", "link_existing_youtube",
        "start_editing", "submit_edit", "teacher_review")}
    if is_pm:
        A["submit_link"] = (not link) or lc in ("awaiting_creator", "changes_required")
        A["update_link"] = link and lc not in ("uploaded", "completed")
        if lc == "pm_review":
            A["approve_creator"] = A["request_changes"] = A["reshoot"] = True
        if lc in ("approved", "editor_assigned", "editing", "editing_paused", "qc_changes"):
            A["assign_editor"] = True
        if lc in ("approved", "editor_assigned", "editing", "editing_paused",
                  "qc_pending", "qc_changes", "ready_for_youtube") and not has_final:
            A["assign_graphics"] = True
            A["upload_thumbnail"] = True
            A["credit_thumbnail"] = True
        # PM can review thumbnails once the designer has submitted candidates
        if cands and gfx in ("submitted", "assigned") and not has_final:
            A["thumbnail_review"] = True
        # credit an off-portal edit when none has come through the pipeline
        if lc in ("approved", "editor_assigned") and not edited:
            A["credit_edit"] = True
        if lc == "qc_pending":
            A["qc_approve"] = A["request_edit_changes"] = True
        if edited and lc in ("qc_pending", "ready_for_youtube", "uploaded", "completed") and getattr(c, "editor_id", None):
            A["rate_editor"] = True
        if lc == "ready_for_youtube":
            A["schedule_upload"] = A["post_youtube"] = True
        if lc in ("uploaded",):
            A["post_youtube"] = True
        # reconcile an already-live video for any not-yet-published chapter
        if lc not in ("completed",) and not (getattr(c, "youtube_url", "") or "").strip():
            A["link_existing_youtube"] = True
    if role == "editor":
        if lc in ("editor_assigned",):
            A["start_editing"] = True
        if lc in ("editing", "editing_paused", "qc_changes"):
            A["submit_edit"] = True
    if role == "teacher":
        if edited and lc in ("qc_pending", "ready_for_youtube") and (getattr(c, "edit_review_status", "") or "") in ("", "pending"):
            A["teacher_review"] = True
    return A


def chapter_work_item(db, c, t=None, role="pm", name_map=None):
    """Normalised production work-item for a chapter (parity shape with a normal task), so the
    frontend and the live-progress dashboard render from ONE representation with no special cases."""
    import video_tasks as _vt
    if t is None:
        t = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
    nm = name_map if name_map is not None else _staff_name_map(
        db, [getattr(c, "editor_id", None), getattr(c, "graphics_id", None)])
    lc = _vt._chapter_lifecycle(c)
    teacher_name = ""
    try:
        if t is not None:
            teacher_name = pc.creator_info(db, t)[0] if hasattr(pc, "creator_info") else ""
    except Exception:
        teacher_name = ""
    _dt = lambda d: (d.strftime("%d %b %Y, %I:%M %p") if d else "")
    return {
        "work_type": "project_chapter",
        "id": c.id,
        "parent_project_id": c.task_id,
        "project_title": (getattr(t, "title", "") or getattr(t, "subject", "") or "Project") if t else "Project",
        "title": c.title or "",
        "subject": (getattr(t, "subject", "") or "") if t else "",
        "sort": getattr(c, "sort", 0) or 0,
        "lifecycle": lc,
        "lifecycle_label": _vt.CHAPTER_STATE_LABELS.get(lc, lc),
        "teacher": teacher_name,
        "channel": (getattr(t, "channel_name", "") or "") if t else "",
        "video_type": (getattr(t, "video_type", "") or "") if t else "",
        "source_video": c.link or "",
        "submitted_by_name": getattr(c, "submitted_by_name", "") or "",
        "submitted_by_role": getattr(c, "submitted_by_role", "") or "",
        "submitted_at": _dt(getattr(c, "submitted_at", None)),
        "on_time": getattr(c, "on_time", None),
        "review_status": getattr(c, "review_status", "") or "",
        "review_note": getattr(c, "review_note", "") or "",
        "reject_count": getattr(c, "reject_count", 0) or 0,
        "editor_id": getattr(c, "editor_id", None),
        "editor": nm.get(getattr(c, "editor_id", None), ""),
        "editor_deadline": _dt(getattr(c, "editor_deadline", None)),
        "editor_instructions": getattr(c, "editor_instructions", "") or "",
        "editor_reference": getattr(c, "editor_reference", "") or "",
        "editing_progress": getattr(c, "editing_progress", 0) or 0,
        "edited_link": getattr(c, "edited_link", "") or "",
        "edited_direct": bool(getattr(c, "edited_direct", False)),
        "qc_status": getattr(c, "qc_status", "") or "",
        "qc_note": getattr(c, "qc_note", "") or "",
        "teacher_review_status": getattr(c, "edit_review_status", "") or "",
        "teacher_review_note": getattr(c, "edit_review_note", "") or "",
        "revision_count": getattr(c, "revision_count", 0) or getattr(c, "qc_revision", 0) or 0,
        "graphics_id": getattr(c, "graphics_id", None),
        "graphics": nm.get(getattr(c, "graphics_id", None), ""),
        "graphics_state": getattr(c, "gfx_state", "") or "",
        "thumbnail": getattr(c, "thumbnail_link", "") or "",
        "thumb_refs": _chap_json_list(getattr(c, "thumb_refs", "")),
        "thumb_candidates": _chap_json_list(getattr(c, "thumb_candidates", "")),
        "thumb_instructions": getattr(c, "thumb_instructions", "") or "",
        "thumb_quality": getattr(c, "thumb_quality", None),
        "thumb_direct": bool(getattr(c, "thumb_direct", False)),
        "priority": getattr(c, "priority", "") or "normal",
        "upload_date": _dt(getattr(c, "upload_date", None)),
        "upload_remarks": getattr(c, "upload_remarks", "") or "",
        "youtube_url": getattr(c, "youtube_url", "") or "",
        "published_at": _dt(getattr(c, "published_at", None)),
        "reconciled": bool(getattr(c, "reconciled", False)),
        "deadline": _dt(getattr(c, "deadline", None)),
        "ratings": {"pm_editor": getattr(c, "quality_rating", None),
                    "teacher_edit": getattr(c, "edit_review_rating", None),
                    "pm_thumbnail": getattr(c, "thumb_quality", None)},
        "allowed_actions": chapter_allowed_actions(c, role),
        "updated_at": _dt(getattr(c, "changed_at", None)),
    }


@router.get("/chapters/{cid}/work-item")
def pm_chapter_work_item(cid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Full normalised work-item for one chapter (fetch-by-id) — powers the chapter drawer and
    deep-links (Project -> Chapter) from notifications / upload schedule / live progress."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    c = _chap(db, cid)
    return chapter_work_item(db, c, role=_actor_role(me))


@router.post("/chapter-submit-link")
def prod_chapter_submit_link(payload: dict = Body(...), db: Session = Depends(get_db),
                             me=Depends(get_pm_or_admin)):
    """PM/Admin submits (or updates) a chapter's source video link ON BEHALF of the teacher.
    Records the real audit identity — never pretends the teacher submitted it."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    link = (payload.get("link") or payload.get("url") or "").strip()
    if not link:
        raise HTTPException(400, "Video link is required")
    _pre = _vt._chapter_lifecycle(c)   # BEFORE mutating the link
    _was = bool((c.link or "").strip())
    c.link = link
    c.submitted_at = datetime.utcnow()
    c.submitted_by_role = _actor_role(me)
    c.submitted_by_name = _actor_name(me)
    if hasattr(c, "changed_at"):
        c.changed_at = _vt._now_ist()
    # on-time vs the chapter deadline (IST-local), if a deadline exists
    try:
        _dl = getattr(c, "deadline", None)
        if _dl:
            c.on_time = (_vt._now_ist() <= _dl)
    except Exception:
        pass
    # fresh submission -> PM review (keep it state-safe; only forward from pre-review states).
    # Don't pre-set review_status here: set_chapter_state derives the current state, flips it to
    # pm_review and logs the timeline event (pre-setting would make cur==target and skip the event).
    if _pre in ("awaiting_creator", "changes_required", "pm_review"):
        _vt.set_chapter_state(db, c, "pm_review", actor=me,
                              note=("Video link %s by %s · %s" % (
                                  "updated" if _was else "submitted",
                                  _actor_name(me),
                                  "Admin" if _actor_role(me) == "admin" else "Production Manager")),
                              force=True)
    else:
        _vt._chap_event(c, "link_updated",
                        "Video link updated by %s · %s" % (_actor_name(me),
                        "Admin" if _actor_role(me) == "admin" else "Production Manager"))
    db.commit()
    return {"ok": True, "chapter_id": c.id, "lifecycle": c.lifecycle}


@router.post("/chapter-reshoot")
def prod_chapter_reshoot(payload: dict = Body(...), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    """Reject / reshoot the SOURCE video (parity with reshoot-creator). Optional new deadline +
    reason; increments reject_count; sends the chapter back for a fresh recording."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    if _vt._chapter_lifecycle(c) != "pm_review":
        raise HTTPException(400, "This video is not in PM review")
    reason = (payload.get("reason") or payload.get("note") or "").strip()
    _dl = (payload.get("deadline") or payload.get("new_deadline") or "").strip()
    if _dl:
        try:
            c.deadline = datetime.fromisoformat(_dl.replace("Z", ""))
        except Exception:
            pass
    c.reject_count = int(getattr(c, "reject_count", 0) or 0) + 1
    if bool(payload.get("no_resubmit")):
        c.no_resubmit = True
    # reshoot: clear the old link so the teacher re-records; send to changes_required
    c.link = ""
    c.review_status = "changes"
    _vt.set_chapter_state(db, c, "changes_required", actor=me,
                          note=("Reshoot requested" + ((" — " + reason) if reason else "")), force=True)
    db.commit()
    return {"ok": True, "chapter_id": c.id, "lifecycle": c.lifecycle, "reject_count": c.reject_count}


@router.post("/chapter-thumbnail-review")
def prod_chapter_thumbnail_review(payload: dict = Body(...), db: Session = Depends(get_db),
                                  me=Depends(get_pm_or_admin)):
    """PM reviews the designer's submitted thumbnail candidates: select ONE as final + a
    mandatory 1-5 rating (approve), OR request changes, OR reject. Mirrors thumbnail-approve."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    action = (payload.get("action") or "approve").strip()
    note = (payload.get("note") or "").strip()
    cands = _chap_json_list(getattr(c, "thumb_candidates", ""))
    gid_uid = None
    if getattr(c, "graphics_id", None):
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == c.graphics_id).first()
        gid_uid = sp.user_id if sp else None
    if action == "approve":
        final = (payload.get("selected_thumbnail") or payload.get("final") or "").strip()
        if not final and cands:
            final = cands[0]
        if not final:
            raise HTTPException(400, "Select a thumbnail to approve")
        try:
            rating = int(payload.get("quality_rating") or payload.get("rating") or 0)
        except Exception:
            rating = 0
        if not (1 <= rating <= 5):
            raise HTTPException(400, "A 1-5 quality rating is required to approve a thumbnail")
        c.thumbnail_link = final
        c.thumb_quality = rating
        c.thumb_quality_note = (payload.get("quality_note") or "")[:400]
        c.thumb_approved_at = datetime.utcnow()
        c.gfx_state = "done"
        _vt._chap_event(c, "thumbnail_approved",
                        "Thumbnail selected as final & approved (%d★) by %s" % (rating, _actor_name(me)))
        if gid_uid:
            pc.notify(db, gid_uid, "Thumbnail Approved",
                      f'Your thumbnail for "{c.title}" was approved ({rating}★).', "video_task", link=str(c.task_id))
        db.commit()
        return {"ok": True, "chapter_id": c.id, "thumbnail": c.thumbnail_link, "thumb_quality": rating}
    if action == "changes":
        if not note:
            raise HTTPException(400, "Please add a short note about the thumbnail changes")
        c.thumb_revision = int(getattr(c, "thumb_revision", 0) or 0) + 1
        c.gfx_state = "assigned"   # back to the designer for a new set (history preserved)
        _vt._chap_event(c, "thumbnail_changes", "Thumbnail changes requested: " + note[:200])
        if gid_uid:
            pc.notify(db, gid_uid, "Thumbnail Changes Requested",
                      f'"{c.title}": {note[:140]}', "video_task", link=str(c.task_id))
        db.commit()
        return {"ok": True, "chapter_id": c.id, "thumb_revision": c.thumb_revision}
    if action == "reject":
        c.gfx_state = "assigned"
        _vt._chap_event(c, "thumbnail_rejected", "Thumbnail rejected" + ((" — " + note) if note else ""))
        if gid_uid:
            pc.notify(db, gid_uid, "Thumbnail Rejected", (note or "Please redo the thumbnail.")[:160],
                      "video_task", link=str(c.task_id))
        db.commit()
        return {"ok": True, "chapter_id": c.id}
    raise HTTPException(400, "Unknown action")


@router.post("/chapter-thumbnail-upload")
def prod_chapter_thumbnail_upload(payload: dict = Body(...), db: Session = Depends(get_db),
                                  me=Depends(get_pm_or_admin)):
    """PM uploads a thumbnail DIRECTLY (already made) -> becomes the final approved thumbnail
    with no graphics review loop. Mirrors the normal task's pm_set_thumbnail."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    img = (payload.get("thumbnail") or payload.get("image") or payload.get("url") or "").strip()
    urls = _chap_norm_images([img]) if img else []
    if not urls:
        raise HTTPException(400, "A thumbnail image or URL is required")
    c.thumbnail_link = urls[0]
    c.thumb_direct = True
    c.thumb_approved_at = datetime.utcnow()
    c.gfx_state = "done"
    _vt._chap_event(c, "thumbnail_uploaded", "Thumbnail uploaded directly by %s" % _actor_name(me))
    db.commit()
    return {"ok": True, "chapter_id": c.id, "thumbnail": c.thumbnail_link}


@router.post("/chapter-credit-thumbnail")
def prod_chapter_credit_thumbnail(payload: dict = Body(...), db: Session = Depends(get_db),
                                  me=Depends(get_pm_or_admin)):
    """Credit a designer for a pre-made (off-portal) thumbnail: set final thumbnail + designer +
    1-5 rating, no assignment/submission/review loop. Mirrors credit-thumbnail. Counts once."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    gid = _valid_staff(db, payload.get("graphics_id"), "graphics")
    if not gid:
        raise HTTPException(400, "Select the graphics designer to credit")
    img = (payload.get("thumbnail") or payload.get("image") or payload.get("url") or "").strip()
    urls = _chap_norm_images([img]) if img else []
    if not urls:
        raise HTTPException(400, "A thumbnail image or URL is required")
    try:
        rating = int(payload.get("quality_rating") or payload.get("rating") or 0)
    except Exception:
        rating = 0
    c.graphics_id = gid
    c.thumbnail_link = urls[0]
    c.thumb_direct = True
    c.thumb_credited_by = _actor_name(me)
    c.thumb_approved_at = datetime.utcnow()
    c.gfx_state = "done"
    if 1 <= rating <= 5:
        c.thumb_quality = rating
    if "quality_note" in payload:
        c.thumb_quality_note = (payload.get("quality_note") or "")[:400]
    nm = _staff_name_map(db, [gid]).get(gid, "")
    _vt._chap_event(c, "thumbnail_credited",
                    "Existing thumbnail credited to %s by %s%s" % (nm, _actor_name(me),
                    (" (%d★)" % rating) if 1 <= rating <= 5 else ""))
    db.commit()
    return {"ok": True, "chapter_id": c.id, "graphics_id": gid, "thumbnail": c.thumbnail_link}


@router.post("/chapter-credit-edit")
def prod_chapter_credit_edit(payload: dict = Body(...), db: Session = Depends(get_db),
                             me=Depends(get_pm_or_admin)):
    """Credit an editor for an already-made (off-portal) edit: set editor + edited link + PM
    rating, skipping Assign->Start->Progress->Submit->QC. Chapter moves to Ready for YouTube."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    eid = _valid_staff(db, payload.get("editor_id"), "editor")
    if not eid:
        raise HTTPException(400, "Select the editor to credit")
    link = (payload.get("edited_link") or payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "The edited video link is required")
    try:
        rating = int(payload.get("quality_rating") or payload.get("rating") or 0)
    except Exception:
        rating = 0
    c.editor_id = eid
    c.editor_inherited = False
    c.edited_link = link
    c.edited_direct = True
    c.editor_credited_by = _actor_name(me)
    c.edit_state = "edited"
    _done = (payload.get("completion_date") or payload.get("edited_at") or "").strip()
    if _done:
        try:
            c.edited_at = datetime.fromisoformat(_done.replace("Z", ""))
        except Exception:
            c.edited_at = datetime.utcnow()
    elif not getattr(c, "edited_at", None):
        c.edited_at = datetime.utcnow()
    if 1 <= rating <= 5:
        c.quality_rating = rating
    if "quality_note" in payload:
        c.quality_note = (payload.get("quality_note") or "")[:400]
    nm = _staff_name_map(db, [eid]).get(eid, "")
    # PM-credited edit is QC-satisfied by definition -> ready for YouTube
    _vt.set_chapter_state(db, c, "ready_for_youtube", actor=me,
                          note="Existing edit credited to %s by %s%s" % (
                              nm, _actor_name(me), (" (%d★)" % rating) if 1 <= rating <= 5 else ""),
                          force=True)
    db.commit()
    return {"ok": True, "chapter_id": c.id, "editor_id": eid, "lifecycle": c.lifecycle}


@router.post("/chapter-link-youtube")
def prod_chapter_link_youtube(payload: dict = Body(...), db: Session = Depends(get_db),
                              me=Depends(get_pm_or_admin)):
    """Administrative reconciliation: attach an ALREADY-LIVE YouTube URL to a chapter that
    skipped the pipeline. Validates the URL, optionally credits editor/graphics, marks the
    chapter reconciled+completed. Never invents missing historical timestamps."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    from video_tasks import _yt_extract_id
    c = _chap(db, payload.get("chapter_id"))
    url = (payload.get("youtube_url") or "").strip()
    vid = _yt_extract_id(url)
    if not vid:
        raise HTTPException(400, "Could not read a valid YouTube video id from that URL")
    c.youtube_url = url
    c.yt_video_id = vid
    c.reconciled = True
    c.reconciled_by = _actor_name(me)
    # optional credit (counts once via the credited staff id)
    eid = _valid_staff(db, payload.get("editor_id"), "editor")
    gid = _valid_staff(db, payload.get("graphics_id"), "graphics")
    if eid:
        c.editor_id = eid; c.editor_inherited = False
        try:
            er = int(payload.get("editor_rating") or 0)
            if 1 <= er <= 5:
                c.quality_rating = er
        except Exception:
            pass
    if gid:
        c.graphics_id = gid
        try:
            gr = int(payload.get("graphics_rating") or 0)
            if 1 <= gr <= 5:
                c.thumb_quality = gr
        except Exception:
            pass
    _vt.set_chapter_state(db, c, "completed", actor=me,
                          note="Existing YouTube video linked by %s (reconciliation)" % _actor_name(me),
                          force=True)
    db.commit()
    return {"ok": True, "chapter_id": c.id, "youtube_url": url, "lifecycle": c.lifecycle, "reconciled": True}


@router.post("/chapter-rate")
def prod_chapter_rate(payload: dict = Body(...), db: Session = Depends(get_db),
                      me=Depends(get_pm_or_admin)):
    """PM editor QUALITY rating for a chapter (SEPARATE from the teacher's edit rating).
    1-5 + optional note + optional per-dimension quality_dims. rating=0 clears it."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    c = _chap(db, payload.get("chapter_id"))
    try:
        rating = int(payload.get("quality_rating") or payload.get("rating") or 0)
    except Exception:
        rating = 0
    if rating and not (1 <= rating <= 5):
        raise HTTPException(400, "Rating must be 1-5")
    c.quality_rating = (rating or None)
    if "quality_note" in payload:
        c.quality_note = (payload.get("quality_note") or "")[:400]
    dims = payload.get("quality_dims")
    if isinstance(dims, dict):
        c.quality_dims = json.dumps(dims)
    if rating:
        _vt._chap_event(c, "editor_rated", "PM rated the edit %d★" % rating)
    db.commit()
    return {"ok": True, "chapter_id": c.id, "quality_rating": c.quality_rating}


# ============================================================================
# PROJECT LIVE PROGRESS — one efficient endpoint powering the command center.
# ============================================================================
# Weighted production progress per chapter (centralised; thumbnail weight is
# redistributed when a thumbnail isn't part of this chapter's pipeline).
_PROGRESS_WEIGHTS = {"submitted": 15, "approved": 15, "thumbnail": 15,
                     "editing": 25, "qc": 15, "published": 15}
# pipeline bucket a chapter's main lifecycle falls into
_PIPE_OF = {
    "awaiting_creator": "recording", "changes_required": "recording",
    "pm_review": "pm_review", "approved": "approved",
    "editor_assigned": "editing", "editing": "editing", "editing_paused": "editing",
    "qc_pending": "qc", "qc_changes": "qc",
    "ready_for_youtube": "ready", "uploaded": "published", "completed": "published",
}
_PIPE_ORDER = ["recording", "pm_review", "approved", "editing", "qc", "ready", "published"]


def _chap_thumb_required(c):
    """A chapter's pipeline includes a thumbnail when any graphics involvement exists
    (assigned designer, references, instructions, or a thumbnail already present)."""
    return bool(getattr(c, "graphics_id", None) or _chap_thumb_final(c)
                or (getattr(c, "thumb_refs", "") or "").strip()
                or (getattr(c, "thumb_instructions", "") or "").strip()
                or (getattr(c, "thumb_candidates", "") or "").strip())


def _chapter_production_progress(c):
    """TOTAL production progress 0-100 (not just published/total, not editing_progress alone).
    Weighted milestones; thumbnail weight redistributed if thumbnail isn't in this pipeline."""
    import video_tasks as _vt
    lc = _vt._chapter_lifecycle(c)
    rank = {s: i for i, s in enumerate(_vt.CHAPTER_STATES)}
    r = rank.get(lc, 0)
    thumb_req = _chap_thumb_required(c)
    w = dict(_PROGRESS_WEIGHTS)
    if not thumb_req:
        # redistribute the thumbnail weight across the other five milestones
        share = w.pop("thumbnail") / 5.0
        for k in list(w.keys()):
            w[k] += share
    done = {
        "submitted": bool((getattr(c, "link", "") or "").strip()) or r >= rank["pm_review"],
        "approved": r >= rank["approved"],
        "thumbnail": _chap_thumb_final(c),
        "editing": bool((getattr(c, "edited_link", "") or "").strip()) or r >= rank["ready_for_youtube"],
        "qc": r >= rank["ready_for_youtube"],
        "published": lc in ("uploaded", "completed"),
    }
    total = 0.0
    for k, wt in w.items():
        if done.get(k):
            total += wt
    return int(round(min(100.0, total)))


def _chap_stage_deadline(c):
    """The deadline in force for a chapter's current stage (editor_deadline while editing,
    upload_date while ready, else the chapter deadline). Mirrors the task stage-deadline idea."""
    import video_tasks as _vt
    lc = _vt._chapter_lifecycle(c)
    if lc in ("editor_assigned", "editing", "editing_paused", "qc_changes"):
        return getattr(c, "editor_deadline", None) or getattr(c, "deadline", None)
    if lc in ("ready_for_youtube",):
        return getattr(c, "upload_date", None) or getattr(c, "deadline", None)
    if lc in ("uploaded", "completed"):
        return None
    return getattr(c, "deadline", None)


def _chapter_health(c, now_ist):
    """Operational status derived from lifecycle + deadline (never stored)."""
    import video_tasks as _vt
    lc = _vt._chapter_lifecycle(c)
    if lc in ("uploaded", "completed"):
        return "published"
    dl = _chap_stage_deadline(c)
    if dl:
        try:
            if dl < now_ist:
                return "overdue"
            if dl <= now_ist + timedelta(hours=24):
                return "due_soon"
        except Exception:
            pass
    base = {"awaiting_creator": "waiting_teacher", "changes_required": "waiting_teacher",
            "pm_review": "waiting_pm_review", "approved": "waiting_editor",
            "editor_assigned": "waiting_editor", "editing": "editing", "editing_paused": "editing",
            "qc_pending": "waiting_qc", "qc_changes": "editing", "ready_for_youtube": "waiting_upload"}
    return base.get(lc, "on_track")


@router.get("/projects/{project_id}/live-progress")
def pm_project_live_progress(project_id: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """ONE request -> everything the Live Progress command center needs: project summary,
    weighted overall progress, pipeline counts, attention + bottleneck, team load, and every
    chapter row (with its own progress, health and allowed_actions). No N+1."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    import video_tasks as _vt
    t = db.query(VideoTask).filter(VideoTask.id == int(project_id)).first()
    if not t or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision", "project"):
        raise HTTPException(404, "Project not found")
    chs = (db.query(_PVChapter).filter(_PVChapter.task_id == t.id)
           .order_by(_PVChapter.sort.asc(), _PVChapter.id.asc()).all())
    # bulk-load staff names (no N+1)
    sids = set()
    for c in chs:
        if getattr(c, "editor_id", None):
            sids.add(c.editor_id)
        if getattr(c, "graphics_id", None):
            sids.add(c.graphics_id)
    nm = _staff_name_map(db, list(sids))
    now_ist = _vt._now_ist()
    role = _actor_role(me)
    pipeline = {k: 0 for k in _PIPE_ORDER}
    attention = {"overdue": 0, "waiting_pm_review": 0, "waiting_qc": 0, "upload_due_today": 0,
                 "waiting_graphics": 0, "waiting_editor": 0, "waiting_teacher": 0}
    team_ed, team_gf = {}, {}
    rows = []
    prog_sum = 0
    completed = 0
    overdue = 0
    comp_week = 0
    week_ago = now_ist - timedelta(days=7)
    for c in chs:
        lc = _vt._chapter_lifecycle(c)
        prog = _chapter_production_progress(c)
        health = _chapter_health(c, now_ist)
        prog_sum += prog
        pipeline[_PIPE_OF.get(lc, "recording")] = pipeline.get(_PIPE_OF.get(lc, "recording"), 0) + 1
        if lc in ("uploaded", "completed"):
            completed += 1
            _pub = getattr(c, "published_at", None) or getattr(c, "uploaded_at", None)
            if _pub and _pub >= week_ago:
                comp_week += 1
        if health == "overdue":
            overdue += 1; attention["overdue"] += 1
        if lc == "pm_review":
            attention["waiting_pm_review"] += 1
        if lc == "qc_pending":
            attention["waiting_qc"] += 1
        if lc in ("awaiting_creator", "changes_required"):
            attention["waiting_teacher"] += 1
        if lc == "approved":
            attention["waiting_editor"] += 1
        # graphics waiting for PM review (candidates submitted, not finalised)
        if (getattr(c, "gfx_state", "") or "") == "submitted" and not _chap_thumb_final(c):
            attention["waiting_graphics"] += 1
        if lc == "ready_for_youtube":
            _ud = getattr(c, "upload_date", None)
            if _ud and _ud.date() == now_ist.date():
                attention["upload_due_today"] += 1
        # team load (active, not published)
        if getattr(c, "editor_id", None) and lc not in ("uploaded", "completed"):
            team_ed[c.editor_id] = team_ed.get(c.editor_id, 0) + 1
        if getattr(c, "graphics_id", None) and (getattr(c, "gfx_state", "") or "") != "done":
            team_gf[c.graphics_id] = team_gf.get(c.graphics_id, 0) + 1
        rows.append({
            "id": c.id, "title": c.title or "", "sort": getattr(c, "sort", 0) or 0,
            "lifecycle": lc, "lifecycle_label": _vt.CHAPTER_STATE_LABELS.get(lc, lc),
            "production_progress": prog, "health": health,
            "teacher": "", "editor": nm.get(getattr(c, "editor_id", None), ""),
            "graphics": nm.get(getattr(c, "graphics_id", None), ""),
            "graphics_state": getattr(c, "gfx_state", "") or "",
            "thumbnail": getattr(c, "thumbnail_link", "") or "",
            "deadline": (_chap_stage_deadline(c).strftime("%d %b %Y, %I:%M %p") if _chap_stage_deadline(c) else ""),
            "upload_date": (c.upload_date.strftime("%d %b %Y, %I:%M %p") if getattr(c, "upload_date", None) else ""),
            "youtube_url": getattr(c, "youtube_url", "") or "",
            "priority": getattr(c, "priority", "") or "normal",
            "allowed_actions": chapter_allowed_actions(c, role),
        })
    total_ch = len(chs)
    overall = int(round(prog_sum / total_ch)) if total_ch else 0
    # bottleneck = the biggest non-terminal waiting bucket
    _bn_pool = {k: pipeline.get(k, 0) for k in ("recording", "pm_review", "approved", "editing", "qc", "ready")}
    bn_stage = max(_bn_pool, key=_bn_pool.get) if any(_bn_pool.values()) else ""
    bottleneck = {"stage": bn_stage, "count": _bn_pool.get(bn_stage, 0)} if bn_stage and _bn_pool[bn_stage] else {"stage": "", "count": 0}
    # project health (aggregate)
    if total_ch == 0:
        phealth = "not_started"
    elif completed == total_ch:
        phealth = "completed"
    elif overdue > 0:
        phealth = "at_risk"
    elif pipeline.get("ready", 0) + pipeline.get("published", 0) >= max(1, total_ch // 2):
        phealth = "publishing"
    elif completed == 0 and (pipeline.get("recording", 0) == total_ch):
        phealth = "not_started"
    else:
        phealth = "in_production"
    # analytics (deterministic; no fake predictions)
    remaining = total_ch - completed
    first_created = min([c.task_id and getattr(c, "assigned_at", None) or None for c in chs] + [None]) if False else None
    weeks_active = None
    try:
        if getattr(t, "created_at", None):
            weeks_active = max(1.0, (datetime.utcnow() - t.created_at).days / 7.0)
    except Exception:
        weeks_active = None
    avg_per_week = round(completed / weeks_active, 1) if (weeks_active and completed) else None
    est_completion = "Not enough data"
    if avg_per_week and avg_per_week > 0 and remaining > 0 and completed >= 3:
        try:
            eta = now_ist + timedelta(days=int(round(remaining / avg_per_week * 7)))
            est_completion = eta.strftime("%d %b %Y")
        except Exception:
            est_completion = "Not enough data"
    elif remaining == 0 and total_ch:
        est_completion = "Completed"
    # deterministic summary line
    _parts = ["%s has %d chapter%s" % (t.title or t.subject or "This project", total_ch, "s" if total_ch != 1 else "")]
    if completed:
        _parts.append("%d published" % completed)
    if pipeline.get("editing"):
        _parts.append("%d in editing" % pipeline["editing"])
    if pipeline.get("qc"):
        _parts.append("%d waiting for QC" % pipeline["qc"])
    if overdue:
        _parts.append("%d overdue" % overdue)
    summary = ". ".join([_parts[0], ", ".join(_parts[1:])]).strip().rstrip(",") + "." if len(_parts) > 1 else (_parts[0] + ".")
    _lblmap = _staff_name_map(db, list(team_ed.keys()) + list(team_gf.keys()))
    return {
        "project": {"id": t.id, "title": t.title or t.subject or "Project", "subject": t.subject or "",
                    "kind": t.kind or "project", "total_chapters": total_ch, "overall_progress": overall,
                    "health": phealth, "completed": completed, "overdue": overdue},
        "pipeline": pipeline,
        "attention": attention,
        "bottleneck": bottleneck,
        "team_load": {"editors": [{"id": k, "name": _lblmap.get(k, "#%d" % k), "count": v} for k, v in sorted(team_ed.items(), key=lambda x: -x[1])],
                      "graphics": [{"id": k, "name": _lblmap.get(k, "#%d" % k), "count": v} for k, v in sorted(team_gf.items(), key=lambda x: -x[1])]},
        "analytics": {"published_this_week": comp_week, "completed_this_week": comp_week,
                      "remaining": remaining, "avg_per_week": avg_per_week, "est_completion": est_completion},
        "summary": summary,
        "chapters": rows,
    }


# ============================================================ PROJECT VIDEO ASSIGNMENT (Phase 3)
# Two modes: (a) assign a single approved project-video to an editor (+ optional graphics),
# (b) assign a whole project to one editor with a deadline. Names show on the card; PM/admin
# get counts + a filter of which project's which chapter is in editing / edited.
def _staff_name_map(db, ids=None):
    from models import ProductionStaffProfile as _SP
    q = db.query(_SP)
    if ids:
        q = q.filter(_SP.id.in_([i for i in ids if i]))
    m = {}
    for s in q.all():
        m[s.id] = (s.user.name if s.user else "") or ("#%d" % s.id)
    return m


def _valid_staff(db, sid, role):
    from models import ProductionStaffProfile as _SP
    if not sid:
        return None
    try:
        s = db.query(_SP).filter(_SP.id == int(sid), _SP.staff_role == role).first()
    except Exception:
        return None
    return s.id if s else None


@router.post("/assign-project-video")
def pm_assign_project_video(payload: dict = Body(...), db: Session = Depends(get_db),
                            me=Depends(get_pm_or_admin)):
    """Assign ONE approved project video to an editor (and optionally a graphics designer)."""
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    row = db.query(_PVChapter).filter(_PVChapter.id == int(payload.get("chapter_id") or 0)).first()
    if not row:
        raise HTTPException(404, "Video not found")
    rs = (getattr(row, "review_status", "") or "").strip()
    approved = (rs == "approved") or (rs == "" and (row.link or "").strip())
    if not approved:
        raise HTTPException(400, "Approve this video first, then assign it for editing")
    eid = _valid_staff(db, payload.get("editor_id"), "editor")
    gid = _valid_staff(db, payload.get("graphics_id"), "graphics")
    if not eid and not gid:
        raise HTTPException(400, "Choose an editor and/or a graphics designer")
    t = db.query(VideoTask).filter(VideoTask.id == row.task_id).first()
    proj = (t.title or t.subject or "project") if t else "project"
    import video_tasks as _vt
    if eid:
        row.editor_id = eid
        row.editor_inherited = False   # EXPLICIT per-chapter pick — project-sync must never steal it
        # move to editor_assigned only if the chapter hasn't started editing yet
        if _vt._chapter_lifecycle(row) in ("approved", "editor_assigned"):
            _vt.set_chapter_state(db, row, "editor_assigned", actor=me,
                                  note="Editor assigned", force=True)
    # rich editor-assignment fields (parity with the normal task's assign-editor)
    if eid:
        if "editor_instructions" in payload:
            row.editor_instructions = (payload.get("editor_instructions") or "")[:4000]
        if "editor_reference" in payload:
            row.editor_reference = (payload.get("editor_reference") or "")[:1000]
        _edl = (payload.get("editor_deadline") or "").strip()
        if _edl:
            try:
                row.editor_deadline = datetime.fromisoformat(_edl.replace("Z", ""))
            except Exception:
                pass
    _pri = (payload.get("priority") or "").strip().lower()
    if _pri in ("normal", "urgent"):
        row.priority = _pri
    if gid:
        row.graphics_id = gid
        if (getattr(row, "gfx_state", "") or "") in ("", "assigned"):
            row.gfx_state = "assigned"
        if "thumb_instructions" in payload:
            row.thumb_instructions = (payload.get("thumb_instructions") or "")[:4000]
    refs = payload.get("thumb_refs")
    if isinstance(refs, list):
        import json as _json
        # accept data-URLs too (store in R2) so PM can PASTE/upload multiple references
        row.thumb_refs = _json.dumps(_chap_norm_images(refs)[:10])
    # per-video deadline (editor/graphics ko is date tak submit karna hoga)
    _dl = (payload.get("deadline") or "").strip()
    if _dl:
        try:
            row.deadline = datetime.fromisoformat(_dl.replace("Z", ""))
        except Exception:
            pass
    row.assigned_at = datetime.utcnow()
    from models import ProductionStaffProfile as _SP
    try:
        if eid:
            ep = db.query(_SP).filter(_SP.id == eid).first()
            if ep and ep.user_id:
                pc.notify(db, ep.user_id, "Project video assigned for editing",
                          f'"{row.title}" from "{proj}" has been assigned to you for editing.',
                          "video_task", link=str(row.task_id))
        if gid:
            gp = db.query(_SP).filter(_SP.id == gid).first()
            if gp and gp.user_id:
                pc.notify(db, gp.user_id, "Project thumbnail assigned",
                          f'A thumbnail for "{row.title}" from "{proj}" has been assigned to you.',
                          "graphics_task", link=str(row.task_id))
    except Exception:
        pass
    db.commit()
    nm = _staff_name_map(db, [row.editor_id, row.graphics_id])
    return {"ok": True, "chapter_id": row.id,
            "editor_id": row.editor_id, "editor_name": nm.get(row.editor_id, ""),
            "graphics_id": row.graphics_id, "graphics_name": nm.get(row.graphics_id, ""),
            "edit_state": (getattr(row, "edit_state", "") or "")}


@router.post("/unassign-project-video")
def pm_unassign_project_video(payload: dict = Body(...), db: Session = Depends(get_db),
                              me=Depends(get_pm_or_admin)):
    if not _PROJECT_OK:
        raise HTTPException(400, "Not available on this server build.")
    row = db.query(_PVChapter).filter(_PVChapter.id == int(payload.get("chapter_id") or 0)).first()
    if not row:
        raise HTTPException(404, "Video not found")
    which = (payload.get("which") or "both").strip().lower()
    if which in ("editor", "both"):
        row.editor_id = None
        row.edit_state = ""
    if which in ("graphics", "both"):
        row.graphics_id = None
    if not row.editor_id and not row.graphics_id:
        row.assigned_at = None
    db.commit()
    return {"ok": True, "chapter_id": row.id}


@router.post("/assign-project")
def pm_assign_project(payload: dict = Body(...), db: Session = Depends(get_db),
                      me=Depends(get_pm_or_admin)):
    """Assign a WHOLE project to one editor (they will work each video in their Projects section)."""
    t = db.query(VideoTask).filter(VideoTask.id == int(payload.get("task_id") or 0)).first()
    if not t or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision", "project"):
        raise HTTPException(404, "Project not found")
    eid = _valid_staff(db, payload.get("editor_id"), "editor")
    if not eid:
        raise HTTPException(400, "Choose an editor")
    t.project_editor_id = eid
    dl = (payload.get("deadline") or "").strip()
    if dl:
        try:
            t.deadline = datetime.fromisoformat(dl.replace("Z", ""))
        except Exception:
            pass
    # CRITICAL FIX: whole-project assignment now actually makes the approved chapters
    # actionable for this editor (inherit editor_id + lifecycle editor_assigned), without
    # ever overwriting a chapter that was explicitly assigned to someone else.
    import video_tasks as _vt
    _applied = _vt._sync_project_editor(db, t)
    from models import ProductionStaffProfile as _SP
    try:
        ep = db.query(_SP).filter(_SP.id == eid).first()
        if ep and ep.user_id:
            pc.notify(db, ep.user_id, "Whole project assigned to you",
                      f'The project "{t.title or t.subject}" has been assigned to you. '
                      f'{_applied} ready video(s) are in your tasks now; new ones appear as they are approved.',
                      "video_task", link=str(t.id))
    except Exception:
        pass
    try:
        pc.log_event(db, t, me, t.lifecycle, note="Whole project assigned to an editor")
    except Exception:
        pass
    db.commit()
    nm = _staff_name_map(db, [eid])
    return {"ok": True, "task_id": t.id, "project_editor_id": eid,
            "editor_name": nm.get(eid, ""), "chapters_assigned": _applied}


@router.post("/unassign-project")
def pm_unassign_project(payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == int(payload.get("task_id") or 0)).first()
    if not t:
        raise HTTPException(404, "Project not found")
    t.project_editor_id = None
    db.commit()
    return {"ok": True, "task_id": t.id}


@router.get("/project-videos")
def pm_project_videos(state: str = "", project_id: int = 0,
                      db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Every assigned project video across projects — for the PM/admin 'in editing / edited'
    view with a filter (which project's which chapter is where)."""
    if not _PROJECT_OK:
        return {"videos": [], "summary": {"assigned": 0, "editing": 0, "edited": 0}}
    projq = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                       VideoTask.kind.in_(["one_shot", "rapid_revision", "project"]))
    projs = {t.id: t for t in projq.all()}
    if not projs:
        return {"videos": [], "summary": {"assigned": 0, "editing": 0, "edited": 0}}
    rows = (db.query(_PVChapter)
            .filter(_PVChapter.task_id.in_(list(projs.keys())),
                    _PVChapter.editor_id.isnot(None)).all())
    nm = _staff_name_map(db)
    summary = {"assigned": 0, "editing": 0, "edited": 0}
    out = []
    for c in rows:
        st = (getattr(c, "edit_state", "") or "") or "assigned"
        bucket = "editing" if st == "editing" else ("edited" if st == "edited" else "assigned")
        summary[bucket] = summary.get(bucket, 0) + 1
        if state and bucket != state:
            continue
        if project_id and c.task_id != project_id:
            continue
        t = projs.get(c.task_id)
        out.append({
            "chapter_id": c.id, "chapter_title": c.title,
            "project_id": c.task_id, "project_title": (t.title or t.subject or "Project") if t else "Project",
            "kind": (t.kind if t else ""), "subject": (t.subject if t else ""),
            "editor_id": c.editor_id, "editor_name": nm.get(c.editor_id, ""),
            "graphics_id": c.graphics_id, "graphics_name": nm.get(c.graphics_id, ""),
            "edit_state": bucket, "link": (c.link or ""),
            "edited_link": (getattr(c, "edited_link", "") or ""),
        })
    return {"videos": out, "summary": summary}


@router.get("/projects/{pid}/chat")
def pm_project_chat(pid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from video_tasks import project_chat_get
    return project_chat_get(db, me, pid)


@router.post("/projects/{pid}/chat")
def pm_project_chat_add(pid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                        me=Depends(get_pm_or_admin)):
    from video_tasks import project_chat_add
    role = "admin" if getattr(me, "role", "") == "admin" else "production_manager"
    return project_chat_add(db, me, pid, payload, role)


@router.post("/projects/{pid}/chat-ping")
def pm_project_chat_ping(pid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                         me=Depends(get_pm_or_admin)):
    from video_tasks import project_chat_ping
    return project_chat_ping(db, me, pid, typing=bool((payload or {}).get("typing")))


@router.get("/projects")
def pm_projects(kind: str = "", class_level: str = "", subject: str = "", q: str = "",
                db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from sqlalchemy import or_ as _or
    query = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                       VideoTask.kind.in_(["one_shot", "rapid_revision", "project"]))
    if kind in ("one_shot", "rapid_revision", "project"):
        query = query.filter(VideoTask.kind == kind)
    if subject:
        query = query.filter(VideoTask.subject == subject)
    if q:
        like = "%" + q.strip() + "%"
        query = query.filter(_or(VideoTask.title.like(like), VideoTask.subject.like(like)))
    rows = query.order_by(VideoTask.created_at.desc()).all()
    # chapter progress per project (single grouped query)
    prog = {}
    _now = datetime.utcnow()
    _dl_map = {t.id: getattr(t, "deadline", None) for t in rows}
    import video_tasks as pc_vt
    # canonical lifecycle -> aggregate production-stage bucket (section 7/8)
    _STAGE_OF = {
        "awaiting_creator": "recording_pending", "changes_required": "recording_pending",
        "pm_review": "pm_review", "approved": "approved",
        "editor_assigned": "editing", "editing": "editing", "editing_paused": "editing",
        "qc_pending": "qc", "qc_changes": "qc", "ready_for_youtube": "ready",
        "uploaded": "published", "completed": "published",
    }
    try:
        from models import VideoTaskChapter as _VC
        ids = [t.id for t in rows]
        if ids:
            for c in db.query(_VC).filter(_VC.task_id.in_(ids)).all():
                p = prog.setdefault(c.task_id, {"total": 0, "done": 0, "pending": 0,
                                                "shoot_pending": 0, "delayed": 0,
                                                "assigned": 0, "editing": 0, "edited": 0, "uploaded": 0,
                                                "st_recording_pending": 0, "st_pm_review": 0,
                                                "st_approved": 0, "st_editing": 0, "st_qc": 0,
                                                "st_ready": 0, "st_published": 0})
                p["total"] += 1
                try:
                    _stg = _STAGE_OF.get(pc_vt._chapter_lifecycle(c), "recording_pending")
                    p["st_" + _stg] += 1
                except Exception:
                    pass
                rs = (getattr(c, "review_status", "") or "").strip()
                _link = (c.link or "").strip()
                _done = (rs == "approved" or (rs == "" and _link))
                if _done:
                    p["done"] += 1
                elif rs == "pending":
                    p["pending"] += 1          # waiting for PM review
                else:
                    p["shoot_pending"] += 1    # no video uploaded yet (shoot pending)
                # delayed: video not done and its deadline (chapter or project) has passed
                _dl = getattr(c, "deadline", None) or _dl_map.get(c.task_id)
                if (not _done) and _dl and _dl < _now:
                    p["delayed"] += 1
                # editing pipeline buckets (per-chapter, pre-migration granularity)
                _es2 = (getattr(c, "edit_status", "") or "").strip()
                if _es2 == "uploaded":
                    p["uploaded"] += 1
                if getattr(c, "editor_id", None):
                    est = (getattr(c, "edit_state", "") or "") or "assigned"
                    p[("editing" if est == "editing" else ("edited" if est == "edited" else "assigned"))] += 1
    except Exception:
        pass
    _pe_ids = [getattr(t, "project_editor_id", None) for t in rows]
    _pe_nm = _staff_name_map(db, [i for i in _pe_ids if i])
    out = []
    counts = {"one_shot": 0, "rapid_revision": 0, "project": 0}
    subjects = set()
    for t in rows:
        counts[t.kind] = counts.get(t.kind, 0) + 1
        if t.subject:
            subjects.add(t.subject)
        p = prog.get(t.id, {"total": 0, "done": 0, "pending": 0, "shoot_pending": 0, "delayed": 0, "assigned": 0, "editing": 0, "edited": 0, "uploaded": 0, "st_recording_pending": 0, "st_pm_review": 0, "st_approved": 0, "st_editing": 0, "st_qc": 0, "st_ready": 0, "st_published": 0})
        cname = ""
        try:
            cname, _ = pc.creator_info(db, t)
        except Exception:
            pass
        pct = round(100.0 * p["done"] / p["total"]) if p["total"] else 0
        _pe = getattr(t, "project_editor_id", None)
        out.append({
            "id": t.id, "kind": t.kind, "title": t.title or "Untitled",
            "subject": t.subject or "", "creator": cname,
            "teacher_id": getattr(t, "teacher_id", None),
            "class_level": ("12" if "12" in (t.subject or "") else ("10" if "10" in (t.subject or "") else "")),
            "deadline": pc._dt(t.deadline), "updated": pc._dt(t.updated_at),
            "weekly_quota": getattr(t, "weekly_quota", 0) or 0,
            "chapters_total": p["total"], "chapters_done": p["done"],
            "chapters_pending": p.get("pending", 0),
            "chapters_shoot_pending": p.get("shoot_pending", 0),
            "chapters_delayed": p.get("delayed", 0),
            "vids_assigned": p.get("assigned", 0), "vids_editing": p.get("editing", 0),
            "vids_edited": p.get("edited", 0), "vids_uploaded": p.get("uploaded", 0),
            # ---- canonical production-stage aggregate (section 7/8) ----
            "stage_recording_pending": p.get("st_recording_pending", 0),
            "stage_pm_review": p.get("st_pm_review", 0),
            "stage_approved": p.get("st_approved", 0),
            "stage_editing": p.get("st_editing", 0),
            "stage_qc": p.get("st_qc", 0),
            "stage_ready": p.get("st_ready", 0),
            "stage_published": p.get("st_published", 0),
            "published_count": p.get("st_published", 0),
            "recording_complete": bool(p["total"] and (p.get("st_recording_pending", 0) + p.get("st_pm_review", 0)) == 0),
            "production_complete": bool(p["total"] and p.get("st_published", 0) == p["total"]),
            "prod_pct": (round(100.0 * p.get("st_published", 0) / p["total"]) if p["total"] else 0),
            "project_editor_id": _pe, "project_editor_name": _pe_nm.get(_pe, ""),
            "pct": pct, "is_old": bool(getattr(t, "is_old", False)),
        })
    # teacher facet (distinct creators that have projects) for the master Teacher filter
    _teachers = sorted({(o.get("creator") or "").strip() for o in out if (o.get("creator") or "").strip()})
    return {"projects": out, "counts": counts,
            "subjects": sorted(subjects),
            "teachers": _teachers,
            "total": len(out)}


# ============================================================ EDIT / DELETE (tasks & projects)
@router.post("/tasks/{tid}/edit")
def pm_edit_task(tid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                 me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    # remember old deadline so a change can be recorded in the timeline
    _old_dl_dt = t.deadline
    _old_dl_str = t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else ""
    _dl_changed = False
    _new_dl_str = ""
    if (payload.get("title") or "").strip():
        t.title = payload["title"].strip()
    dl = (payload.get("deadline") or "").strip()
    if dl:
        try:
            _nd = datetime.fromisoformat(dl.replace("Z", ""))
            if _nd != _old_dl_dt:
                _dl_changed = True
                _new_dl_str = _nd.strftime("%d %b %Y, %I:%M %p")
            t.deadline = _nd
            # submission ke baad deadline change -> on_time dobara compute (delayed auto-hat jaaye)
            pc.recompute_on_time(t)
        except Exception:
            pass
    for f in ("subject", "video_type", "channel_name", "reference", "reference_video", "remarks", "streaming", "thumbnail_link"):
        if f in payload:
            setattr(t, f, (payload.get(f) or "").strip())
    if "priority" in payload:
        _pr = (payload.get("priority") or "normal").strip() or "normal"
        t.priority = _pr
    # thumbnail requirement + graphics designer (create/assign the sub-task if needed)
    if "thumbnail_required" in payload:
        t.thumbnail_required = bool(payload.get("thumbnail_required"))
    if "graphics_id" in payload:
        try:
            gid = int(payload.get("graphics_id") or 0)
        except Exception:
            gid = 0
        if gid:
            gp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == gid,
                                                         ProductionStaffProfile.staff_role == "graphics").first()
            if gp:
                t.graphics_id = gid
                g = db.query(GraphicsTask).filter(GraphicsTask.task_id == t.id).first()
                if not g:
                    g = GraphicsTask(task_id=t.id, graphics_id=gid, status="pending",
                                     priority=(t.priority or "normal"))
                    db.add(g)
                else:
                    g.graphics_id = gid
                import json as _jr
                _gstatus = (payload.get("graphics_status") or "").strip().lower()
                _gi = (payload.get("graphics_instructions") or payload.get("instructions") or "")
                _gd = (payload.get("graphics_deadline") or "").strip()

                def _merge_refs(existing):
                    refs = list(existing or [])
                    # wizard se aayi "kept" existing references (jo PM ne hataayi nahi) — preserve
                    for _ke in (payload.get("graphics_reference_existing") or []):
                        _ke = str(_ke or "").strip()
                        if _ke and _ke not in refs:
                            refs.append(_ke)
                    _typed = (payload.get("graphics_reference") or payload.get("reference_image") or "").strip()
                    if _typed:
                        for _ln in _typed.replace(",", "\n").split("\n"):
                            _ln = _ln.strip()
                            if _ln and _ln not in refs:
                                refs.append(_ln)
                    _refups = payload.get("graphics_reference_uploads") or []
                    if not _refups and payload.get("graphics_reference_upload"):
                        _refups = [payload.get("graphics_reference_upload")]
                    if _refups:
                        try:
                            _ru = pc.save_images(db, t, list(_refups), "reference", None, me, return_urls=True) or []
                            for _u in _ru:
                                if _u and _u not in refs:
                                    refs.append(_u)
                        except Exception:
                            pass
                    return refs

                if _gstatus == "pending":
                    # PM ne thumbnail DUBARA banwane ke liye pending kiya -> purana submitted
                    # thumbnail + rating + candidates SAB clear, fresh reference + brief set.
                    g.thumbnail_url = ""
                    for _f in ("thumbnail_candidates", "quality_note"):
                        try:
                            setattr(g, _f, "")
                        except Exception:
                            pass
                    try:
                        g.quality_rating = None
                    except Exception:
                        pass
                    try:
                        g.submitted_at = None
                    except Exception:
                        pass
                    g.status = "new"
                    t.thumbnail_link = ""
                    g.instructions = _gi.strip()
                    _refs = _merge_refs([])
                    g.reference_image = _refs[0] if _refs else ""
                    try:
                        g.reference_images = _jr.dumps(_refs[:8]) if _refs else ""
                    except Exception:
                        pass
                    if _gd:
                        try:
                            g.deadline = datetime.fromisoformat(_gd.replace("Z", ""))
                        except Exception:
                            pass
                    if gp.user_id:
                        pc.notify(db, gp.user_id, "New Thumbnail Task",
                                  f'A fresh thumbnail is needed for "{t.title}".', "graphics_task", link=str(t.id))
                else:
                    if _gi.strip():
                        g.instructions = _gi.strip()
                    _existing = []
                    if getattr(g, "reference_images", ""):
                        try:
                            _existing = _jr.loads(g.reference_images) or []
                        except Exception:
                            _existing = []
                    _refs2 = _merge_refs(_existing)
                    if _refs2:
                        g.reference_image = _refs2[0]
                        try:
                            g.reference_images = _jr.dumps(_refs2[:8])
                        except Exception:
                            pass
                    if _gd:
                        try:
                            g.deadline = datetime.fromisoformat(_gd.replace("Z", ""))
                        except Exception:
                            pass
                    # edit-mode "Thumbnail done": PM ne naya final thumbnail upload kiya -> auto-approve + rating
                    _upl = payload.get("thumbnail_upload")
                    if _upl:
                        try:
                            _uu = pc.save_images(db, t, [_upl], "thumbnail", None, me, return_urls=True) or []
                            if _uu:
                                g.thumbnail_url = _uu[0]
                                t.thumbnail_link = _uu[0]
                                g.status = "approved"
                                try:
                                    g.submitted_at = datetime.utcnow()
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    _rt = payload.get("thumbnail_rating")
                    if _rt:
                        try:
                            g.quality_rating = int(_rt)
                        except Exception:
                            pass
                    if gp.user_id:
                        pc.notify(db, gp.user_id, "Thumbnail task assigned",
                                  f'You have been assigned the thumbnail for "{t.title}".', "graphics_task", link=str(t.id))
        else:
            t.graphics_id = None
    if "editor_id" in payload:
        try:
            eid = int(payload.get("editor_id") or 0)
        except Exception:
            eid = 0
        if eid:
            ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == eid,
                                                         ProductionStaffProfile.staff_role == "editor").first()
            if ep:
                prev = t.editor_id
                t.editor_id = eid
                if ep.user_id and prev != eid:
                    pc.notify(db, ep.user_id, "You are the editor for a video",
                              f'You have been assigned to edit "{t.title}".', "video_task", link=str(t.id))
        else:
            t.editor_id = None
    # editor deadline / instructions / reference (Assign Work "Editor" section)
    if "editor_deadline" in payload:
        _edl = (payload.get("editor_deadline") or "").strip()
        if _edl:
            try:
                t.editor_deadline = datetime.fromisoformat(_edl.replace("Z", ""))
            except Exception:
                pass
        else:
            t.editor_deadline = None
    if "editor_instructions" in payload:
        t.editor_instructions = (payload.get("editor_instructions") or "").strip()
    if "editor_reference" in payload:
        t.editor_reference = (payload.get("editor_reference") or "").strip()
    if "collab_editor_ids" in payload:
        import json as _jce2
        _ced2 = []
        for x in (payload.get("collab_editor_ids") or []):
            try:
                xi = int(x)
            except Exception:
                continue
            if xi and xi != t.editor_id and xi not in _ced2 and db.query(ProductionStaffProfile).filter(
                    ProductionStaffProfile.id == xi, ProductionStaffProfile.staff_role == "editor").first():
                _ced2.append(xi)
        try:
            t.collab_editor_ids = _jce2.dumps(_ced2) if _ced2 else ""
        except Exception:
            pass
    # chapter edit (projects / One Shot / Rapid Revision) — remove unticked chapters and
    # remember the removals in chapter_excludes so the syllabus auto-sync never re-adds
    # them (this is what makes a PM's chapter removal actually stick after refresh).
    sel = payload.get("chapters")
    if isinstance(sel, list) and _PROJECT_OK:
        def _pnorm_ch(s):
            return " ".join(str(s or "").split()).lower()
        keep, seen_k = [], set()
        for x in sel:
            s2 = " ".join(str(x or "").split())[:300]
            if s2 and s2.lower() not in seen_k:
                seen_k.add(s2.lower())
                keep.append(s2)
        if keep:
            try:
                excl = set(y for y in json.loads(getattr(t, "chapter_excludes", "") or "[]") if isinstance(y, str))
            except Exception:
                excl = set()
            rows = db.query(_PVChapter).filter(_PVChapter.task_id == t.id).all()
            existing_norm = set()
            for crow in rows:
                cn = _pnorm_ch(crow.title)
                if cn not in seen_k:
                    excl.add(cn)
                    db.delete(crow)
                else:
                    existing_norm.add(cn)
            sort = max([getattr(crow, "sort", 0) or 0 for crow in rows] + [-1]) + 1
            for s2 in keep:
                cn = _pnorm_ch(s2)
                if cn not in existing_norm:
                    db.add(_PVChapter(task_id=t.id, title=s2, sort=sort))
                    sort += 1
                excl.discard(cn)
            try:
                t.chapter_excludes = json.dumps(sorted(excl))
            except Exception:
                pass
    try:
        if _dl_changed:
            # deadline badla -> timeline me old -> new clearly dikhe
            _dnote = ("Deadline: " + (_old_dl_str or "not set") + "  →  " + (_new_dl_str or "not set"))
            pc.log_event(db, t, me, "deadline_changed", new_state=t.lifecycle,
                         meta={"note": _dnote, "old_deadline": _old_dl_str, "new_deadline": _new_dl_str})
        else:
            pc.log_event(db, t, me, "task_edited", new_state=t.lifecycle,
                         meta={"note": "Edited by " + (getattr(me, "name", "") or "production manager")})
    except Exception:
        pass
    # ---- URGENT PAUSE-REQUEST: Edit Task flow se bhi editor ke active task ko pause + new deadline ----
    _apply_pause_request(db, t, (t.editor_id or 0), payload, me)
    # ---- UPLOAD section: video already edited + live -> publish + editor credit/rating ----
    try:
        _apply_upload_done(db, t, payload, me)
    except HTTPException:
        raise
    except Exception:
        pass
    db.commit()
    return {"ok": True, "id": t.id}


_MOVE_STAGES = {
    "pm_review": "PM Review",
    "editor_assigned": "Editor Assignment",
    "editing": "Editing",
    "qc_pending": "QC",
    "ready_for_youtube": "Ready for YouTube",
    "uploaded": "Uploaded",
    "completed": "Completed",
}


@router.get("/move-stages")
def pm_move_stages(me=Depends(get_pm_or_admin)):
    """Board stages jinpe koi bhi video move ki ja sakti hai (admin/PM)."""
    return {"stages": [{"key": k, "label": v} for k, v in _MOVE_STAGES.items()]}


@router.post("/tasks/{tid}/move-stage")
def pm_move_stage(tid: int, payload: dict = Body(...),
                  db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Kisi bhi video ko kisi bhi board/stage par bhejo — process wahi se firse start.
    Data destroy NAHI hota (links/history preserve); sirf lifecycle set + re-activate."""
    t = _task(db, tid)
    stage = (payload.get("stage") or "").strip()
    if stage not in _MOVE_STAGES:
        raise HTTPException(status_code=400, detail="Invalid stage")
    t.cancelled = False
    try:
        t.on_hold = False
    except Exception:
        pass
    # jab wapas (PM/editing) le jaate hain to QC ka purana verdict clear -> dobara QC hoga
    if stage in ("pm_review", "editor_assigned", "editing"):
        try:
            t.qc_status = ""
        except Exception:
            pass
    t.lifecycle = stage
    db.commit()
    return {"ok": True, "lifecycle": stage, "label": _MOVE_STAGES[stage]}


@router.delete("/tasks/{tid}")
def pm_delete_task(tid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    # Soft delete (reversible): removed from every list but data is preserved.
    t = _task(db, tid)
    t.cancelled = True
    # Associated thumbnail/graphics task ko bhi active se hata do — warna graphics portal/count
    # me deleted task ka thumbnail dikhta reh jaata tha. (Approved rehne do — history.)
    try:
        for g in db.query(GraphicsTask).filter(GraphicsTask.task_id == tid,
                                               GraphicsTask.status != "approved").all():
            g.status = "cancelled"
    except Exception:
        pass
    db.commit()
    return {"ok": True}


# ============================================================ DEADLINE EXTENSION (PM side)
# Phase A: editors/youtubers request a new deadline; the PM approves or rejects. The old
# deadline is preserved in the timeline — nothing is overwritten silently.
@router.get("/deadline-requests")
def pm_deadline_requests(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    rows = (db.query(VideoTask).filter(VideoTask.deadline_req_status == "pending",
                                       VideoTask.cancelled == False)
            .order_by(VideoTask.updated_at.desc()).all())
    out = []
    for t in rows:
        d = pc.task_out(db, t, light=True)
        d["deadline_req"] = pc._dt(t.deadline_req)
        d["deadline_req_reason"] = t.deadline_req_reason or ""
        # previous deadline the editor currently has (falls back to the task deadline)
        _prev = getattr(t, "editor_deadline", None) or t.deadline
        d["deadline_prev"] = pc._dt(_prev)
        d["requested_by"] = pc._name_for_staff(db, t.editor_id) if t.editor_id else ""
        out.append(d)
    return {"requests": out, "count": len(out)}


@router.post("/tasks/{tid}/deadline-decision")
def pm_deadline_decision(tid: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    if (t.deadline_req_status or "") != "pending":
        raise HTTPException(400, "No pending deadline request on this task.")
    decision = (payload.get("decision") or "").strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve or reject")
    # the request comes from the editor, so it extends the EDITOR's deadline (not the teacher deadline)
    old = getattr(t, "editor_deadline", None) or t.deadline
    new = t.deadline_req
    if decision == "approve":
        t.editor_deadline = new
        t.deadline_req_status = "approved"
        pc.log_event(db, t, me, "deadline_extended", meta={"note": 'Editor deadline extended: %s \u2192 %s (old deadline kept in history)' % (
            (old.strftime("%d %b %Y, %I:%M %p") if old else "none"),
            (new.strftime("%d %b %Y, %I:%M %p") if new else "none"))})
        msg = 'Your deadline request for "%s" was approved. New deadline: %s.' % (
            t.title or "", new.strftime("%d %b %Y, %I:%M %p") if new else "")
    else:
        t.deadline_req_status = "rejected"
        pc.log_event(db, t, me, "deadline_rejected", meta={"note": 'Deadline extension rejected by production manager'})
        msg = 'Your deadline request for "%s" was not approved. Current deadline stands.' % (t.title or "")
    # notify the editor who owns the task
    try:
        if t.editor_id:
            ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ed and ed.user_id:
                pc.notify(db, ed.user_id, "Deadline Request " + ("Approved" if decision == "approve" else "Rejected"),
                          msg, "production", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "decision": decision}


# ============================================================ QUALITY RATING (PM rates work)
# Phase B: after editing/graphics work is done, the PM can rate quality 1..5 with a note.
# Feeds the editor/graphics performance averages. Does NOT touch teacher payout logic.
@router.post("/tasks/{tid}/rate")
def pm_rate(tid: int, payload: dict = Body(...), db: Session = Depends(get_db),
            me=Depends(get_pm_or_admin)):
    t = _task(db, tid)
    try:
        rating = int(payload.get("rating") or 0)
    except Exception:
        rating = 0
    if rating == 0:
        # rating=0 -> remove/clear the quality rating entirely
        t.quality_rating = None
        t.quality_note = ""
        try:
            t.quality_dims = ""
        except Exception:
            pass
        pc.log_event(db, t, me, "quality_rating_removed", meta={"note": "Quality rating removed"})
        db.commit()
        return {"ok": True, "quality_rating": None, "cleared": True}
    if rating < 1 or rating > 5:
        raise HTTPException(400, "Rating must be between 1 and 5.")
    t.quality_rating = rating
    t.quality_note = (payload.get("note") or "").strip()[:400]
    # optional per-dimension sub-ratings (pacing, cuts, audio, graphics, captions, storytelling, technical)
    _DIMS = ["pacing", "cuts", "audio", "graphics", "captions", "storytelling", "technical"]
    dims = {}
    src = payload.get("dimensions") or payload.get("dims") or {}
    if isinstance(src, dict):
        for k in _DIMS:
            try:
                v = int(src.get(k) or 0)
                if 1 <= v <= 5:
                    dims[k] = v
            except Exception:
                pass
    t.quality_dims = json.dumps(dims) if dims else ""
    pc.log_event(db, t, me, "quality_rated",
                 meta={"note": "Quality rated %d/5%s" % (rating, (" \u2014 " + t.quality_note) if t.quality_note else ""),
                       "dims": dims})
    try:
        if t.editor_id:
            ed = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ed and ed.user_id:
                if rating >= 5:
                    ttl = "Excellent work!"
                    msg = 'The PM rated "%s" a perfect 5/5. Outstanding!%s' % (t.title or "", (" " + t.quality_note) if t.quality_note else "")
                elif rating >= 4:
                    ttl = "Great work!"
                    msg = 'The PM rated "%s": %d/5.%s' % (t.title or "", rating, (" " + t.quality_note) if t.quality_note else "")
                else:
                    ttl = "Your work was rated"
                    msg = 'The PM rated "%s": %d/5.%s' % (t.title or "", rating, (" " + t.quality_note) if t.quality_note else "")
                pc.notify(db, ed.user_id, ttl, msg, "appreciation" if rating >= 4 else "production", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "rating": rating}


# ============================================================ ANNOUNCEMENTS + EVENTS (§35)
@router.get("/announce-targets")
def pm_announce_targets(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """People the PM can send an individual announcement to (user_id + name), by role."""
    out = {"teachers": [], "editors": [], "graphics": [], "youtubers": []}
    try:
        for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
            out["teachers"].append({"user_id": tp.user_id, "name": tp.user.name if tp.user else ""})
        for sp in db.query(ProductionStaffProfile).filter(ProductionStaffProfile.is_active == True).all():
            grp = "editors" if sp.staff_role == "editor" else ("graphics" if sp.staff_role == "graphics" else None)
            if grp and sp.user_id:
                out[grp].append({"user_id": sp.user_id, "name": sp.user.name if sp.user else ""})
        for yp in db.query(YouTuberProfile).all():
            if getattr(yp, "user_id", None):
                out["youtubers"].append({"user_id": yp.user_id, "name": yp.user.name if yp.user else ""})
    except Exception:
        pass
    return out


@router.post("/announce")
def pm_announce(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Send a notification to a whole group (or one person)."""
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    if not title or not message:
        raise HTTPException(400, "Title and message are required.")
    image = (payload.get("image_url") or "").strip()
    one = payload.get("user_id")
    if one:
        ids = [int(one)]
    else:
        ids = pc.audience_user_ids(db, payload.get("audience") or "all")
    for uid in ids:
        try:
            n = Notification(user_id=uid, title=title, message=message, notif_type="announcement",
                             image_url=image or None, sender_id=getattr(me, "id", None),
                             sender_role="production_manager")
            db.add(n)
        except Exception:
            pass
    db.commit()
    return {"ok": True, "sent": len(ids)}


@router.get("/events")
def pm_events_list(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from models import PmEvent
    rows = db.query(PmEvent).filter(PmEvent.active == True).order_by(PmEvent.event_at.asc()).all()
    return {"events": [{"id": e.id, "title": e.title, "description": e.description,
                        "at": pc._dt(e.event_at), "image_url": e.image_url or "",
                        "audience": e.audience} for e in rows]}


@router.post("/events")
def pm_event_create(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from models import PmEvent
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Event title is required.")
    at = None
    raw = (payload.get("event_at") or "").strip()
    if raw:
        try:
            at = datetime.fromisoformat(raw.replace("Z", ""))
        except Exception:
            pass
    aud = (payload.get("audience") or "all").lower()
    if aud not in ("all", "teachers", "editors", "graphics", "youtubers"):
        aud = "all"
    e = PmEvent(title=title, description=(payload.get("description") or "").strip()[:1200],
                event_at=at, image_url=(payload.get("image_url") or "").strip(),
                audience=aud, created_by=getattr(me, "id", None), active=True)
    db.add(e); db.commit()
    # optional: notify the audience about the new event
    if payload.get("notify"):
        for uid in pc.audience_user_ids(db, aud):
            try:
                db.add(Notification(user_id=uid, title="New Event: " + title,
                                    message=(payload.get("description") or "")[:300],
                                    notif_type="event", image_url=(payload.get("image_url") or None),
                                    sender_id=getattr(me, "id", None), sender_role="production_manager"))
            except Exception:
                pass
        db.commit()
    return {"ok": True, "id": e.id}


@router.delete("/events/{eid}")
def pm_event_delete(eid: int, db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    from models import PmEvent
    e = db.query(PmEvent).filter(PmEvent.id == int(eid)).first()
    if e:
        e.active = False; db.commit()
    return {"ok": True}


@router.get("/my-events")
def pm_my_events(db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    return {"events": pc.active_events_for(db, "all")}


# ============================================================ TEAM MEMBER PHOTO
@router.get("/member-photo")
def pm_member_photo(role: str = "", profile_id: int = 0,
                    db: Session = Depends(get_db), me=Depends(get_pm_or_admin)):
    """Admin/PM: fetch a production team member's profile photo (data URL) by role +
    profile_id, so the Production Team cards show real photos instead of just initials.
    Returns {'photo': ''} when none — the UI then keeps the initials avatar."""
    photo = ""
    r = (role or "").strip()
    try:
        if r == "youtuber":
            yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == profile_id).first()
            photo = (getattr(yp, "photo_b64", "") if yp else "") or ""
        else:  # editor | graphics | production_manager
            sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == profile_id).first()
            photo = (getattr(sp, "photo_b64", "") if sp else "") or ""
    except Exception:
        photo = ""
    return {"photo": photo}
