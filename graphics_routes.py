"""Graphics API (/api/graphics). Thumbnail work is tracked independently of the
video's editing lifecycle (a video can be Editing while its thumbnail is Approved)."""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from datetime import datetime, date, timedelta

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
        for t in db.query(VideoTask).filter(VideoTask.id.in_(_tids)):
            _vmap[t.id] = t
    _ccm = pc.comment_count_map(db, _tids)       # batch comment counts (was 1 COUNT per task)
    for g in gts:
        t = _vmap.get(g.task_id)
        if not t or getattr(t, 'cancelled', False):
            continue
        row = pc.task_out(db, t, light=True, comment_count=_ccm.get(t.id, 0))
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

@router.post("/heartbeat")
def gfx_heartbeat(db: Session = Depends(get_db), me=Depends(get_graphics)):
    from video_tasks import _chat_touch_global
    _chat_touch_global(db, me)
    return {"ok": True}


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
    c = _vtc_add(db, tid, me, payload.get("message"), "graphics", _att, "internal")
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
def gfx_performance(db: Session = Depends(get_db), me=Depends(get_graphics)):
    """Full graphics performance — real data only: output (day/week/month), approvals,
    revisions, avg turnaround, PM quality (avg of the PM's 1-5 thumbnail ratings),
    first-time approval rate, rank, plus chart data + ranking cards."""
    sp = _me_staff(db, me)
    now = datetime.utcnow()
    today0 = datetime(now.year, now.month, now.day)
    week0 = today0 - timedelta(days=today0.weekday())          # Monday 00:00
    month0 = datetime(now.year, now.month, 1)

    base = db.query(GraphicsTask).filter(
        GraphicsTask.graphics_id == sp.id,
        GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))
    all_g = base.all()

    approved = [g for g in all_g if g.status == "approved"]
    def _out_since(dt):
        return sum(1 for g in approved if g.approved_at and g.approved_at >= dt)
    daily_output   = _out_since(today0)
    weekly_output  = _out_since(week0)
    monthly_output = _out_since(month0)

    revisions = sum(int(g.revision_count or 0) for g in all_g)
    first_time = sum(1 for g in approved if int(g.revision_count or 0) == 0)
    approval_rate = round(first_time * 100 / len(approved)) if approved else 0

    # PM quality = average of the PM's thumbnail ratings (set at approval time)
    ratings = [int(g.quality_rating) for g in approved if g.quality_rating]
    pm_quality = round(sum(ratings) / len(ratings), 1) if ratings else 0

    # turnaround: started_at -> approved_at (fallback created_at -> approved_at)
    turns = []
    for g in approved:
        st = g.started_at or g.created_at
        if st and g.approved_at and g.approved_at >= st:
            turns.append((g.approved_at - st).total_seconds() / 3600.0)
    avg_turnaround = round(sum(turns) / len(turns), 1) if turns else 0

    # rank among active graphics designers by this month's approvals
    rank = 0
    ranking = []
    try:
        gfx = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "graphics",
            ProductionStaffProfile.is_active == True).all()
        for d in gfx:
            appr = db.query(GraphicsTask).filter(
                GraphicsTask.graphics_id == d.id,
                GraphicsTask.status == "approved",
                GraphicsTask.approved_at != None,
                GraphicsTask.approved_at >= month0).count()
            ranking.append({"name": (d.user.name if d.user else ""), "approved": appr,
                            "me": (d.id == sp.id)})
        ranking.sort(key=lambda x: -x["approved"])
        for i, r in enumerate(ranking, 1):
            if r["me"]:
                rank = i
        ranking = ranking[:5]
    except Exception:
        ranking, rank = [], 0

    # charts
    def _cnt(*st):
        return sum(1 for g in all_g if g.status in st)
    donut = [
        {"label": "In Progress", "value": _cnt("in_progress")},
        {"label": "In Review",   "value": _cnt("submitted")},
        {"label": "Changes",     "value": _cnt("changes")},
        {"label": "Approved",    "value": len(approved)},
    ]
    bar = [
        {"label": "New",      "value": _cnt("new", "pending")},
        {"label": "Working",  "value": _cnt("in_progress")},
        {"label": "Review",   "value": _cnt("submitted")},
        {"label": "Changes",  "value": _cnt("changes")},
        {"label": "Approved", "value": len(approved)},
    ]
    trend = []
    for i in range(5, -1, -1):
        m = (now.month - i - 1) % 12 + 1
        y = now.year + ((now.month - i - 1) // 12)
        m0 = datetime(y, m, 1)
        m1 = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        cnt = sum(1 for g in approved if g.approved_at and m0 <= g.approved_at < m1)
        trend.append({"label": m0.strftime("%b"), "value": cnt})

    total_done = len(approved)
    badges = []
    if total_done >= 3 and approval_rate >= 90:
        badges.append("First-time Approved")
    if pm_quality >= 4.5 and len(ratings) >= 3:
        badges.append("Top Quality")
    if total_done >= 10:
        badges.append("10+ Thumbnails")
    if rank == 1 and total_done > 0:
        badges.insert(0, "Top Performer")

    return {
        "daily_output": daily_output,
        "weekly_output": weekly_output,
        "monthly_output": monthly_output,
        "approved_count": total_done,
        "revision_count": revisions,
        "avg_turnaround_hours": avg_turnaround,
        "pm_quality_rating": pm_quality,
        "approval_rate": approval_rate,
        "rank": rank,
        "charts": {"bar": bar, "donut": donut, "trend": trend},
        "ranking": ranking,
        "appreciation": {"ontime_pct": approval_rate, "avg_rating": pm_quality,
                         "rate_label": "First-time approved", "revisions": revisions,
                         "badges": badges, "total_done": total_done},
    }


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
