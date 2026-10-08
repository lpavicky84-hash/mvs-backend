"""Graphics API (/api/graphics). Thumbnail work is tracked independently of the
video's editing lifecycle (a video can be Editing while its thumbnail is Approved)."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session, defer
from sqlalchemy import func, or_
from datetime import datetime, date, timedelta

from database import get_db
from security import get_graphics
from models import VideoTask, GraphicsTask, ProductionStaffProfile
import production_core as pc
import performance_core as PC

router = APIRouter(prefix="/api/graphics", tags=["Graphics"])


def _me_staff(db, me):
    sp = pc.staff_profile(db, me)
    if not sp or sp.staff_role != "graphics":
        raise HTTPException(403, "Graphics profile not found")
    return sp


_OFFICE_CLOSE_HOUR = 18   # IST; daily report is sent after this, so prompt only before it


@router.get("/attendance/today")
def gfx_attendance_today(db: Session = Depends(get_db), me=Depends(get_graphics)):
    """needs_prompt = designer submitted NO thumbnail from their own portal today AND has not
    answered yet AND office still open. (PM crediting a pre-made thumbnail does NOT count.)"""
    sp = _me_staff(db, me)
    from models import ProductionAttendance as _ATT
    s, e, day_str = pc.ist_day_bounds_utc()
    worked = pc.graphics_worked_today(db, sp, s, e)
    row = db.query(_ATT).filter(_ATT.staff_id == sp.id, _ATT.day == day_str).first()
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    before_close = now_ist.hour < _OFFICE_CLOSE_HOUR
    needs_prompt = (not worked) and (row is None) and before_close
    return {"needs_prompt": needs_prompt, "worked": worked,
            "status": (row.status if row else ""), "day": day_str}


@router.post("/attendance")
def gfx_set_attendance(payload: dict = Body(...), db: Session = Depends(get_db),
                       me=Depends(get_graphics)):
    """Designer self-marks today: on_leave=true -> Leave, false -> Present."""
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


def _my_gfx_chapter(db, sp, cid):
    from models import VideoTaskChapter as _VC
    c = db.query(_VC).filter(_VC.id == int(cid or 0)).first()
    if not c or c.graphics_id != sp.id:
        raise HTTPException(404, "Assigned thumbnail not found")
    return c


@router.get("/project-thumbnails")
def gfx_project_thumbnails(db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Project-video thumbnails assigned to this graphics designer (Phase 4)."""
    sp = _me_staff(db, me)
    from models import VideoTaskChapter as _VC
    import json as _json
    proj_rows = db.query(VideoTask).filter(VideoTask.cancelled == False,
                                           VideoTask.kind.in_(["one_shot", "rapid_revision", "project"])).all()
    pmap = {t.id: t for t in proj_rows}
    out = []
    if pmap:
        for c in db.query(_VC).filter(_VC.task_id.in_(list(pmap.keys())),
                                      _VC.graphics_id == sp.id).all():
            t = pmap.get(c.task_id)
            refs = []
            try:
                refs = _json.loads(c.thumb_refs) if (getattr(c, "thumb_refs", "") or "").strip() else []
            except Exception:
                refs = []
            if not isinstance(refs, list):
                refs = []
            cands = []
            try:
                cands = _json.loads(c.thumb_candidates) if (getattr(c, "thumb_candidates", "") or "").strip() else []
            except Exception:
                cands = []
            if not isinstance(cands, list):
                cands = []
            hist = []
            try:
                hist = _json.loads(c.thumb_candidate_history) if (getattr(c, "thumb_candidate_history", "") or "").strip() else []
            except Exception:
                hist = []
            if not isinstance(hist, list):
                hist = []
            _gdl = getattr(c, "graphics_deadline", None) or getattr(c, "deadline", None) or (t.deadline if t else None)
            out.append({
                "chapter_id": c.id, "title": c.title,
                "project_id": c.task_id, "project_title": (t.title or t.subject or "Project") if t else "Project",
                "subject": (t.subject if t else ""), "kind": (t.kind if t else ""),
                "video_link": (c.link or ""), "thumbnail_link": (getattr(c, "thumbnail_link", "") or ""),
                "gfx_state": (getattr(c, "gfx_state", "") or "") or "assigned",
                "candidates": cands, "candidate_history": hist,
                "instructions": (getattr(c, "thumb_instructions", "") or ""),
                "change_note": (getattr(c, "thumb_change_note", "") or ""),
                "thumb_revision": int(getattr(c, "thumb_revision", 0) or 0),
                "thumb_quality": getattr(c, "thumb_quality", None),
                "priority": (getattr(c, "priority", "") or "normal"),
                "refs": refs,
                "deadline": pc._dt(_gdl),
                "overdue": bool(_gdl and _gdl < datetime.utcnow() and (getattr(c, "gfx_state", "") or "") not in ("done",)),
            })
    return {"thumbnails": out, "count": len(out)}


@router.post("/project-thumbnails/{cid}/start")
def gfx_project_thumb_start(cid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Designer starts (or resumes after changes) work on a chapter thumbnail -> in_progress."""
    sp = _me_staff(db, me)
    c = _my_gfx_chapter(db, sp, cid)
    _cur = (getattr(c, "gfx_state", "") or "")
    if _cur in ("", "assigned", "changes"):
        _resuming = (_cur == "changes")
        c.gfx_state = "in_progress"
        if not getattr(c, "thumb_started_at", None):
            c.thumb_started_at = datetime.utcnow()
        try:
            import video_tasks as _vt
            _vt._chap_event(c, "thumbnail_started",
                            "Designer resumed after changes" if _resuming else "Designer started the thumbnail",
                            actor=me)
        except Exception:
            pass
        db.commit()
    return {"ok": True, "gfx_state": c.gfx_state}


@router.post("/project-thumbnails/{cid}/submit")
def gfx_project_thumb_submit(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                             me=Depends(get_graphics)):
    """Designer submits one OR MULTIPLE candidate thumbnails for a chapter -> PM review.
    Candidates (paste/upload/URL) are stored; a previous round is kept in history. The chapter
    is NOT marked done here — the PM reviews, selects the final one and rates it."""
    import json as _json
    import video_tasks as _vt
    sp = _me_staff(db, me)
    c = _my_gfx_chapter(db, sp, cid)
    # RACE GUARD: once the PM has finalised a thumbnail (approved / direct / credited) or the video
    # is already published, a designer must NOT silently resubmit and reopen it. The PM has to
    # request changes first (which sets gfx_state='changes' and reopens the loop).
    if (getattr(c, "gfx_state", "") or "") == "done" or getattr(c, "thumb_approved_at", None):
        raise HTTPException(409, "This thumbnail is already approved. Ask the PM to request changes before resubmitting.")
    if _vt._chapter_lifecycle(c) in ("uploaded", "completed"):
        raise HTTPException(409, "This video is already published — the thumbnail is locked.")
    raw = payload.get("candidates")
    if not isinstance(raw, list) or not raw:
        single = (payload.get("thumbnail_link") or payload.get("thumbnail") or "").strip()
        raw = [single] if single else []
    # normalise (data-URL -> R2) via the production helper
    try:
        from production_routes import _chap_norm_images
        urls = _chap_norm_images(raw)
    except Exception:
        urls = [str(x).strip() for x in raw if str(x).strip()]
    if not urls:
        raise HTTPException(400, "At least one thumbnail image or link is required")
    # keep previous submitted set in history before replacing
    try:
        import video_tasks as _vt
        prev = _json.loads(c.thumb_candidates) if (getattr(c, "thumb_candidates", "") or "").strip() else []
        if isinstance(prev, list) and prev:
            hist = _json.loads(c.thumb_candidate_history) if (getattr(c, "thumb_candidate_history", "") or "").strip() else []
            if not isinstance(hist, list):
                hist = []
            hist.append({"round": len(hist) + 1,
                         "at": _vt._now_ist().strftime("%d %b %Y, %I:%M %p"),
                         "urls": prev})
            c.thumb_candidate_history = _json.dumps(hist[-30:])
    except Exception:
        pass
    c.thumb_candidates = _json.dumps(urls[:12])
    # keep the first candidate on thumbnail_link ONLY as a preview hint; it is NOT final until
    # the PM approves (final sets thumb_approved_at). gfx_state -> submitted (review stage).
    c.gfx_state = "submitted"
    c.thumb_submitted_at = datetime.utcnow()
    c.thumb_change_note = ""   # a fresh submission clears the previous "changes requested" note
    try:
        import video_tasks as _vt
        _vt._chap_event(c, "thumbnail_submitted",
                        "Designer submitted %d thumbnail option%s" % (len(urls), "s" if len(urls) != 1 else ""),
                        actor=me)
    except Exception:
        pass
    t = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
    proj = (t.title or t.subject or "project") if t else "project"
    try:
        pc.notify_pms(db, "Project thumbnail submitted",
                      f'{me.name} submitted {len(urls)} thumbnail option(s) for "{c.title}" from "{proj}" — review needed.',
                      "production", link=str(c.task_id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "gfx_state": c.gfx_state, "candidates": urls}


def _gfx_in_project(db, sp, pid):
    from models import VideoTaskChapter as _VC
    return db.query(_VC).filter(_VC.task_id == int(pid or 0), _VC.graphics_id == sp.id).first() is not None


@router.get("/projects/{pid}/chat")
def gfx_project_chat(pid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    if not _gfx_in_project(db, sp, pid):
        raise HTTPException(403, "Not assigned to this project")
    from video_tasks import project_chat_get
    return project_chat_get(db, me, pid)


@router.post("/projects/{pid}/chat")
def gfx_project_chat_add(pid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                         me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    if not _gfx_in_project(db, sp, pid):
        raise HTTPException(403, "Not assigned to this project")
    from video_tasks import project_chat_add
    return project_chat_add(db, me, pid, payload, "graphics")


@router.post("/projects/{pid}/chat-ping")
def gfx_project_chat_ping(pid: int, payload: dict = Body(default={}), db: Session = Depends(get_db),
                          me=Depends(get_graphics)):
    from video_tasks import project_chat_ping
    return project_chat_ping(db, me, pid, typing=bool((payload or {}).get("typing")))


def _my_gtask(db, sp, tid):
    g = db.query(GraphicsTask).filter(GraphicsTask.task_id == int(tid)).first()
    if not g:
        # Self-heal (permanent fix): kabhi GraphicsTask row nahi banta (edit / alag creation path),
        # par video is designer ko assigned hai -> row bana do taaki Start/Submit/Chat kabhi 404 na de.
        t = db.query(VideoTask).filter(VideoTask.id == int(tid)).first()
        if t and getattr(t, "graphics_id", None) == sp.id:
            g = GraphicsTask(task_id=t.id, graphics_id=sp.id, status="new")
            db.add(g)
            db.commit()
            db.refresh(g)
        else:
            raise HTTPException(404, "Thumbnail task not found")
    if g.graphics_id != sp.id:
        raise HTTPException(403, "This thumbnail is not assigned to you")
    return g


@router.get("/dashboard")
def gfx_dashboard(db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    base = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id, GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))

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


@router.post("/me/photo")
def gfx_photo_set(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    sp.photo_b64 = (payload.get("photo") or "").strip() or None
    db.commit()
    return {"ok": True, "has_photo": bool(sp.photo_b64)}


@router.get("/me/photo")
def gfx_photo_get(db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    return {"photo": (sp.photo_b64 if sp else "") or "", "name": getattr(me, "name", ""), "role": "graphics"}


@router.get("/tasks")
def gfx_tasks(status: str = "", filter: str = "", db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    q = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id, GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))
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
        # Aaj ka sab kaam — assign/due/submit/approve me se kuch bhi aaj hua ho (completed bhi dikhe).
        q = q.filter(or_(
            func.date(GraphicsTask.created_at) == today,
            func.date(GraphicsTask.deadline) == today,
            func.date(GraphicsTask.submitted_at) == today,
            func.date(GraphicsTask.approved_at) == today,
        ))
    out = []
    gts = q.order_by(GraphicsTask.created_at.desc()).all()
    _tids = [g.task_id for g in gts if g.task_id]
    _vmap = {}                                   # batch the VideoTask lookup (was 1 query per graphics task)
    if _tids:
        for t in db.query(VideoTask).options(defer(VideoTask.thumbnail_b64)).filter(VideoTask.id.in_(_tids)):
            _vmap[t.id] = t
    _ccm = pc.comment_count_map(db, _tids)       # batch comment counts (was 1 COUNT per task)
    _tm = pc.thumb_map_for(db, _tids)            # thumbnail URLs — base64 load kiye bina
    for g in gts:
        t = _vmap.get(g.task_id)
        if not t or getattr(t, 'cancelled', False):
            continue
        row = pc.task_out(db, t, light=True, comment_count=_ccm.get(t.id, 0), thumb_map=_tm)
        out.append(row)
    try:
        from video_tasks import _vtc_unread_bulk
        _un = _vtc_unread_bulk(db, getattr(me, "id", None), [_o.get("id") for _o in out if _o.get("id")])
        for _o in out:
            _o["unread_total"] = (_un.get(_o.get("id"), {}) or {}).get("graphics", 0)
    except Exception:
        pass
    return {"tasks": out}


@router.get("/tasks/{tid}")
def gfx_task_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    g = _my_gtask(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    out = pc.task_out(db, t, timeline=True)
    return out


@router.get("/tasks/{tid}/comments")
def gfx_comments(tid: int, db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    _my_gtask(db, sp, tid)  # ensures this designer owns the thumbnail task
    from video_tasks import _vtc_list_v, _vtc_mark_read, _chat_touch, _chat_other_presence
    _vtc_mark_read(db, me, tid, "internal")
    _chat_touch(db, me, tid, "internal")
    return {"comments": _vtc_list_v(db, tid, "internal", getattr(me, "id", None)),
            "presence": _chat_other_presence(db, getattr(me, "id", None), tid, "internal")}

@router.get("/tasks/{tid}/party-tasks")
def gfx_party_tasks(tid: int, audience: str = "internal", db: Session = Depends(get_db), me=Depends(get_graphics)):
    from video_tasks import _chat_party_tasks
    sp = _me_staff(db, me); _my_gtask(db, sp, tid)
    return {"tasks": _chat_party_tasks(db, tid, (audience or "internal"))}


@router.post("/heartbeat")
def gfx_heartbeat(payload: dict = Body(default={}), db: Session = Depends(get_db),
                  me=Depends(get_graphics)):
    pc.touch_session(db, me, (payload or {}).get("page"), bool((payload or {}).get("active")))
    from video_tasks import _chat_touch_global
    _chat_touch_global(db, me)
    return {"ok": True}


@router.get("/chat/inbox")
def gfx_chat_inbox(db: Session = Depends(get_db), me=Depends(get_graphics)):
    from video_tasks import _chat_inbox, _chat_touch_global
    _chat_touch_global(db, me)
    return {"conversations": _chat_inbox(db, me, "graphics")}


# ---- Graphics DIRECT-PAIR chats: graphics<->teacher (te_gf), graphics<->editor (ed_gf) ----
@router.get("/tasks/{tid}/pair-comments")
def gfx_pair_get(tid: int, audience: str = "", db: Session = Depends(get_db), me=Depends(get_graphics)):
    import video_tasks as _VT
    aud = (audience or "").strip().lower()
    if not _VT._pair_check("graphics", aud):
        raise HTTPException(400, "Invalid conversation")
    sp = _me_staff(db, me); _my_gtask(db, sp, tid)
    _VT._vtc_mark_read(db, me, tid, aud); _VT._chat_touch(db, me, tid, aud)
    return {"comments": _VT._vtc_list_v(db, tid, aud, getattr(me, "id", None)),
            "presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, aud)}


@router.post("/tasks/{tid}/pair-comments")
def gfx_pair_add(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_graphics)):
    import video_tasks as _VT
    from models import VideoTask
    aud = (payload.get("audience") or "").strip().lower()
    if not _VT._pair_check("graphics", aud):
        raise HTTPException(400, "Invalid conversation")
    sp = _me_staff(db, me); _my_gtask(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == tid).first()
    if not t:
        raise HTTPException(404, "Task not found")
    _att = _VT._resolve_chat_att(db, t, payload, me)
    c = _VT._vtc_add(db, tid, me, payload.get("message"), "graphics", attachment_url=_att, audience=aud, ref_task_id=payload.get("ref_task_id"))
    if not c:
        raise HTTPException(400, "Message cannot be empty")
    try: _VT._chat_touch(db, me, tid, aud, typing=False)
    except Exception: pass
    _VT._pair_notify(db, tid, aud, me, c.message)
    db.commit()
    return {"ok": True, "comment": _VT._vtc_out(db, c)}


@router.post("/tasks/{tid}/pair-ping")
def gfx_pair_ping(tid: int, audience: str = "", payload: dict = Body(default={}),
                  db: Session = Depends(get_db), me=Depends(get_graphics)):
    import video_tasks as _VT
    aud = (audience or (payload or {}).get("audience") or "").strip().lower()
    if _VT._pair_check("graphics", aud):
        _VT._chat_touch(db, me, tid, aud, typing=bool((payload or {}).get("typing")))
    return {"presence": _VT._chat_other_presence(db, getattr(me, "id", None), tid, aud)}


@router.post("/tasks/{tid}/chat-ping")
def gfx_chat_ping(tid: int, payload: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_graphics)):
    from video_tasks import _chat_touch, _chat_other_presence
    _chat_touch(db, me, tid, "internal", typing=bool((payload or {}).get("typing")))
    return {"presence": _chat_other_presence(db, getattr(me, "id", None), tid, "internal")}


@router.post("/tasks/{tid}/comments")
def gfx_comment_add(tid: int, payload: dict = Body(...),
                    db: Session = Depends(get_db), me=Depends(get_graphics)):
    sp = _me_staff(db, me)
    _my_gtask(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == tid).first()
    from video_tasks import _vtc_add, _vtc_out
    _att = ""
    _imgs = payload.get("images") or ([payload.get("attachment")] if payload.get("attachment") else [])
    if _imgs:
        try:
            urls = pc.save_images(db, t, _imgs[:1], "chat", None, me, return_urls=True) or []
            if urls:
                _att = urls[0]
        except Exception:
            _att = ""
    c = _vtc_add(db, tid, me, payload.get("message"), "graphics", _att, "internal", ref_task_id=payload.get("ref_task_id"))
    from video_tasks import _chat_touch as _ctg
    try: _ctg(db, me, tid, "internal", typing=False)
    except Exception: pass
    if not c:
        raise HTTPException(400, "Message or image required")
    pc.notify_pms(db, "Graphics replied on thumbnail",
                  f'{getattr(me, "name", "Designer")} on "{t.title if t else ""}": {c.message[:110]}',
                  "gfx_chat", link=str(tid))
    db.commit()
    return {"ok": True, "comment": _vtc_out(db, c)}


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
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    # Frontend bhejta hai: images[] (pasted/uploaded base64) aur/ya drive_link. Base64 ko R2 pe
    # upload karke URL banao (VARCHAR me base64 fit nahi hota). thumbnail_url bhi accept karo.
    url = (payload.get("thumbnail_url") or payload.get("drive_link") or "").strip()
    images = payload.get("images") or []
    _all_urls = []
    if images:
        try:
            _all_urls = pc.save_images(db, t, images, "thumbnail", None, me, return_urls=True) or []
            if _all_urls and not url:
                url = _all_urls[0]
        except Exception:
            _all_urls = []
    if not url:
        raise HTTPException(400, "Thumbnail image/URL is required")
    g.thumbnail_url = url
    # Multiple thumbnails submitted -> keep them all as candidates so the PM can pick the final one.
    try:
        import json as _jt
        _cands = list(_all_urls)
        if url and url not in _cands:
            _cands = [url] + _cands
        if _cands:
            g.thumbnail_candidates = _jt.dumps(_cands)
    except Exception:
        pass
    _drive = (payload.get("drive_link") or "").strip()
    if _drive:
        g.drive_link = _drive
    g.status = "submitted"
    g.submitted_at = datetime.utcnow()
    _note = (payload.get("remarks") or "").strip()
    _ref = (payload.get("reference") or "").strip()
    _meta = {}
    if _note:
        _meta["note"] = _note
    if _ref:
        _meta["reference"] = _ref
    pc.log_event(db, t, me, "thumbnail_submitted", new_state=t.lifecycle, meta=(_meta or None))
    pc.notify_pms(db, "Thumbnail Submitted",
                  f'{me.name} submitted a thumbnail for "{t.title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "status": g.status}


# Resubmit after PM change request (alias of submit — kept for API clarity).
@router.post("/tasks/{tid}/resubmit")
def gfx_resubmit(tid: int, payload: dict = Body(...),
                 db: Session = Depends(get_db), me=Depends(get_graphics)):
    return gfx_submit(tid, payload, db, me)


# ============================================================ PERFORMANCE
@router.get("/performance")
def gfx_performance(period: str = "month", db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Canonical graphics performance (performance_core) — counts BOTH normal GraphicsTasks
    and project chapter thumbnails, no double-counting, 100-pt score, server-authoritative
    rank + movement from persistent snapshots. Real data only."""
    sp = _me_staff(db, me)
    now = datetime.utcnow()
    try:
        PC.maybe_daily_snapshot(db, now)   # self-healing daily rank history for ALL staff
    except Exception:
        pass
    items = PC.get_graphics_work_items(db, sp.id, now=now)
    perf = PC.compute_graphics_performance(sp, items, period, ref=now)

    today0 = datetime(now.year, now.month, now.day)
    week0 = today0 - timedelta(days=today0.weekday())
    month0 = datetime(now.year, now.month, 1)
    done = [it for it in items if it["edited"] and it["completed_at"]]

    def _out_since(dt):
        return sum(1 for it in done if it["completed_at"] >= dt)

    # ---- server-authoritative leaderboard + rank + movement ----
    lb = PC.compute_graphics_leaderboard(db, period, ref=now)
    rank, total = PC.find_rank(lb, sp.id)
    if rank:
        PC.save_rank_snapshot(db, sp.id, "graphics", "graphics", rank, (perf["score"] or 0), total, ref=now)
    movement = PC.rank_movement(db, sp.id, "graphics", rank, ref=now)
    ranking = [{"name": r["name"], "approved": r["approved"], "edited": r["edited"],
                "score": r["score"], "rank": r["rank"], "me": (r["staff_id"] == sp.id),
                "avg_quality": r["avg_quality"], "on_time_pct": r["on_time_pct"],
                "provisional": r["provisional"]} for r in lb[:5]]

    # charts
    donut = [
        {"label": "Pending", "value": sum(1 for it in items if it["pending"])},
        {"label": "Approved", "value": perf["approved"]},
        {"label": "Project", "value": perf["project_work"]},
        {"label": "Normal", "value": perf["normal_work"]},
    ]
    bar = [{"label": "Today", "value": _out_since(today0)},
           {"label": "Week", "value": _out_since(week0)},
           {"label": "Month", "value": _out_since(month0)}]
    trend = []
    for i in range(5, -1, -1):
        m = (now.month - i - 1) % 12 + 1
        y = now.year + ((now.month - i - 1) // 12)
        mm0 = datetime(y, m, 1)
        mm1 = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        trend.append({"label": mm0.strftime("%b"),
                      "value": sum(1 for it in done if mm0 <= it["completed_at"] < mm1)})

    return {
        # legacy keys (current UI)
        "daily_output": _out_since(today0), "weekly_output": _out_since(week0),
        "monthly_output": perf["thumbnails"], "approved_count": perf["approved"],
        "revision_count": perf["revisions"],
        "avg_turnaround_hours": 0,
        "pm_quality_rating": perf["avg_quality"] if perf["avg_quality"] is not None else 0,
        "approval_rate": perf["first_pass_pct"] if perf["first_pass_pct"] is not None else 0,
        "rank": rank or 0,
        "charts": {"bar": bar, "donut": donut, "trend": trend},
        "ranking": ranking,
        "appreciation": {"ontime_pct": perf["on_time_pct"] or 0,
                         "avg_rating": perf["avg_quality"] or 0,
                         "rate_label": "First-time approved", "revisions": perf["revisions"],
                         "badges": [], "total_done": perf["thumbnails"]},
        # new canonical keys (Phase-2 UI)
        "period": perf["period"], "score": perf["score"],
        "score_breakdown": perf["score_breakdown"], "provisional": perf["provisional"],
        "on_time_pct": perf["on_time_pct"], "first_pass_pct": perf["first_pass_pct"],
        "normal_work": perf["normal_work"], "project_work": perf["project_work"],
        "pending": perf["pending"], "overdue": perf["overdue"], "target": perf["target"],
        "rank_movement": movement, "total_ranked": total,
        "rank_trend": PC.rank_trend(db, sp.id, "graphics", 30, ref=now),
        "badges": PC.graphics_badges(perf),
        "personal_bests": PC.personal_bests(db, sp.id, "graphics", ref=now),
    }


@router.get("/performance/items")
def gfx_performance_items(period: str = "month", category: str = "", filter: str = "edited",
                          db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Drill-down list behind a clickable graphics metric (perf §31). Own data only."""
    sp = _me_staff(db, me)
    items = PC.get_graphics_work_items(db, sp.id)
    rows = PC.filter_work_items(items, period, category, filter)
    return {"items": [PC.item_dto(it) for it in rows], "count": len(rows),
            "filter": filter, "category": category, "period": period}


# ============================================================ REALTIME VIEWS
@router.get("/uploads")
def gfx_uploads(db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Videos this designer made a thumbnail for, that are published on YouTube,
    with their realtime view counts. Reuses the shared yt_views data (no separate API)."""
    sp = _me_staff(db, me)
    gts = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id).all()
    task_ids = [g.task_id for g in gts if g.task_id]
    thumb_map = {g.task_id: (g.thumbnail_url or "") for g in gts}
    total_thumbs = len(task_ids)
    videos = []
    total_views = 0
    if task_ids:
        rows = db.query(VideoTask).filter(
            VideoTask.id.in_(task_ids), VideoTask.cancelled == False,
            VideoTask.youtube_url != None, VideoTask.youtube_url != "").all()
        for t in rows:
            vv = int(t.yt_views or 0)
            total_views += vv
            # prefer the designer's own thumbnail; fall back to the YouTube frame
            yt_thumb = ("https://img.youtube.com/vi/%s/mqdefault.jpg" % t.yt_video_id) if t.yt_video_id else ""
            videos.append({
                "id": t.id, "title": t.title or "", "youtube_url": t.youtube_url or "",
                "yt_video_id": t.yt_video_id or "", "video_type": t.video_type or "",
                "views": vv,
                "published_at": pc._dt(t.published_at) if t.published_at else "",
                "my_thumbnail": thumb_map.get(t.id) or "",
                "thumbnail": (thumb_map.get(t.id) or yt_thumb),
            })
    videos.sort(key=lambda v: -v["views"])
    highest = videos[0] if videos else None
    return {
        "total_thumbnails": total_thumbs,
        "published": len(videos),
        "total_views": total_views,
        "highest": highest,
        "videos": videos,
    }


@router.post("/refresh-views")
def gfx_refresh_views(db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Refresh realtime views for videos THIS designer made thumbnails for.
    Reuses the shared YouTube fetch + snapshot system (no duplicate API)."""
    sp = _me_staff(db, me)
    try:
        from video_tasks import _yt_get_key, _yt_fetch_views
        from models import VideoViewSnapshot
    except Exception:
        return {"ok": False, "updated": 0}
    task_ids = [g.task_id for g in db.query(GraphicsTask).filter(GraphicsTask.graphics_id == sp.id).all() if g.task_id]
    if not task_ids:
        return {"ok": True, "updated": 0}
    rows = db.query(VideoTask).filter(
        VideoTask.id.in_(task_ids), VideoTask.cancelled == False,
        VideoTask.yt_video_id != None, VideoTask.yt_video_id != "").all()
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


@router.post("/tasks/{tid}/edit")
def gfx_edit_task(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Graphics apne assigned task ke universal fields edit kare (title/deadline/priority/remarks)."""
    sp = _me_staff(db, me)
    g = _my_gtask(db, sp, tid)
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    pc.edit_task_fields(db, t, payload, me)
    db.commit()
    return {"ok": True}
