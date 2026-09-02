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


@router.post("/heartbeat")
def yt_heartbeat(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    from video_tasks import _chat_touch_global
    _chat_touch_global(db, me)
    return {"ok": True}


@router.get("/dashboard")
def yt_dashboard(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    base = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.cancelled == False,
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
    tasks = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.cancelled == False,
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
    rows = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.cancelled == False,
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
    q = db.query(VideoTask).filter(VideoTask.creator_type == "youtuber", VideoTask.cancelled == False,
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
    _ccm = pc.comment_count_map(db, [t.id for t in rows])
    return {"videos": [pc.task_out(db, t, light=True, comment_count=_ccm.get(t.id, 0)) for t in rows]}


@router.get("/videos/{tid}")
def yt_video_detail(tid: int, db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    # Creators get a clean timeline — internal churn hidden, milestones shown.
    return pc.task_out(db, t, timeline=True)


@router.get("/thumbnail-reviews")
def yt_thumbnail_reviews(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Creator ke apne tasks jinke thumbnail review ke liye pending hain (ya changes me)."""
    from models import GraphicsTask
    yp = _me_yt(db, me)
    tids = [r[0] for r in db.query(VideoTask.id).filter(
        VideoTask.youtuber_id == yp.id, VideoTask.cancelled == False)]
    if not tids:
        return {"pending": [], "changes": []}
    gts = db.query(GraphicsTask).filter(GraphicsTask.task_id.in_(tids)).all()
    pend = [g.task_id for g in gts if g.status == "submitted"]
    chg = [g.task_id for g in gts if g.status == "changes"]

    def _out(ids):
        if not ids:
            return []
        rows = db.query(VideoTask).filter(VideoTask.id.in_(ids)).order_by(VideoTask.updated_at.desc()).all()
        return [pc.task_out(db, t, light=True) for t in rows]
    return {"pending": _out(pend), "changes": _out(chg)}


@router.post("/videos/{tid}/thumbnail-approve")
def yt_thumbnail_approve(tid: int, payload: dict = Body(default={}),
                         db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Creator apne task ki thumbnail approve + rate kare (PM jaisa). Rating 1-5 zaroori."""
    from models import ProductionStaffProfile, TaskReview
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    g = pc.graphics_task(db, t)
    if not g or g.status != "submitted":
        raise HTTPException(400, "No submitted thumbnail to review")
    _sel = (payload.get("selected_thumbnail") or "").strip()
    if _sel:
        g.thumbnail_url = _sel
    t.thumbnail_link = g.thumbnail_url or t.thumbnail_link
    try:
        rt = int(payload.get("quality_rating") or 0)
    except Exception:
        rt = 0
    if not (1 <= rt <= 5):
        raise HTTPException(400, "A 1\u20135 star rating is required to approve the thumbnail")
    g.status = "approved"
    g.approved_at = datetime.utcnow()
    g.quality_rating = rt
    g.quality_note = (payload.get("quality_note") or payload.get("remarks") or g.quality_note or "")[:400]
    db.add(TaskReview(task_id=t.id, kind="thumbnail", reviewer_user_id=me.id,
                      decision="approved", remarks=g.quality_note or "",
                      revision_no=g.revision_count or 0))
    pc.log_event(db, t, me, "thumbnail_approved", new_state=t.lifecycle,
                 meta={"note": ("Rated %d/5. " % rt) + (g.quality_note or "") + " (by creator)"})
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            _m = 'Your thumbnail for "%s" was approved by the creator. Rated %d/5.' % (t.title, rt)
            pc.notify(db, sp.user_id, "Thumbnail Approved", _m,
                      "appreciation" if rt >= 4 else "video_task", link=str(t.id))
    pc.notify_pms(db, "Thumbnail Approved by Creator",
                  '%s approved the thumbnail for "%s".' % (me.name, t.title), "production", link=str(t.id))
    db.commit()
    return {"ok": True}


@router.post("/videos/{tid}/thumbnail-changes")
def yt_thumbnail_changes(tid: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Creator thumbnail me changes maange (graphics ko wapas)."""
    from models import ProductionStaffProfile
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    g = pc.graphics_task(db, t)
    if not g:
        raise HTTPException(400, "No thumbnail task")
    rem = (payload.get("remarks") or "Changes needed").strip()
    g.status = "changes"
    g.revision_count = (g.revision_count or 0) + 1
    pc.log_event(db, t, me, "thumbnail_changes", new_state=t.lifecycle,
                 meta={"note": rem + " (creator requested changes)"})
    if g.graphics_id:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == g.graphics_id).first()
        if sp and sp.user_id:
            pc.notify(db, sp.user_id, "Thumbnail Changes Requested",
                      'Creator ne "%s" ke thumbnail me changes maange: %s' % (t.title, rem[:120]),
                      "video_task", link=str(t.id))
    pc.notify_pms(db, "Thumbnail Changes (by Creator)",
                  '%s requested thumbnail changes for "%s".' % (me.name, t.title), "production", link=str(t.id))
    db.commit()
    return {"ok": True}


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


@router.delete("/videos/{tid}")
def yt_delete_video(tid: int, db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    t.cancelled = True
    try:
        pc.log_event(db, t, me, "task_deleted", new_state=t.lifecycle)
    except Exception:
        pass
    pc.notify_pms(db, "Video Deleted", f'{me.name} deleted "{t.title}".', "production", link=str(t.id))
    db.commit()
    return {"ok": True}


@router.get("/channels")
def yt_channels(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    from models import VideoChannel
    rows = db.query(VideoChannel).filter(VideoChannel.active == True).order_by(VideoChannel.id.asc()).all()
    return {"channels": [{"id": c.id, "name": c.name} for c in rows]}


@router.post("/channels")
def yt_create_channel(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """YouTuber apne channel ka naam khud add kar sake (jo tasks/filters me dikhega).
    Duplicate name ho to reactivate + return (crash nahi)."""
    from models import VideoChannel
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Channel name daaliye")
    ex = db.query(VideoChannel).filter(VideoChannel.name == name).first()
    if ex:
        if not ex.active:
            ex.active = True
            db.commit()
        return {"ok": True, "id": ex.id, "name": ex.name, "existed": True}
    c = VideoChannel(name=name, active=True)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


@router.get("/video-types")
def yt_video_types(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    from models import VideoType
    rows = db.query(VideoType).filter(VideoType.active == True).order_by(VideoType.sort.asc(), VideoType.id.asc()).all()
    return {"types": [{"id": c.id, "name": c.name} for c in rows]}


@router.get("/graphics")
def yt_graphics_people(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    from models import ProductionStaffProfile
    rows = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "graphics",
        ProductionStaffProfile.is_active == True).all()
    return {"graphics": [{"id": g.id, "name": (g.user.name if g.user else "")} for g in rows]}


@router.post("/new-task")
def yt_new_task(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Direct task creation (no proposal). mode='ready' -> submit link+thumbnail;
    mode='thumbnail' -> topic + reference for graphics; video link later."""
    from models import ProductionStaffProfile
    yp = _me_yt(db, me)
    mode = (payload.get("mode") or "ready").strip().lower()
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title / topic is required")
    dl = None
    raw = (payload.get("deadline") or "").strip()
    if raw:
        try:
            dl = datetime.fromisoformat(raw.replace("Z", ""))
        except Exception:
            pass
    t = VideoTask(title=title, creator_type="youtuber", youtuber_id=yp.id,
                  video_type=(payload.get("video_type") or "").strip(),
                  channel_name=(payload.get("channel") or "").strip(),
                  streaming=(payload.get("streaming") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  remarks=(payload.get("remarks") or "").strip(),
                  deadline=dl,
                  priority=((payload.get("priority") or "").strip() or "normal"))
    db.add(t)
    db.flush()
    pc.ensure_ref_code(t)
    pc.set_state(db, t, "created", actor=me, event="task_created")
    # optional editor pre-assign (both modes; PM/editor can also do it later)
    _eid = payload.get("editor_id")
    if _eid:
        try:
            ep = db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.id == int(_eid),
                ProductionStaffProfile.staff_role == "editor").first()
            if ep:
                t.editor_id = ep.id
                try:
                    pc.log_event(db, t, me, "editor_assigned", new_state=t.lifecycle,
                                 meta={"note": "Editor assigned: " + (ep.user.name if ep.user else "editor")})
                except Exception:
                    pass
        except Exception:
            pass

    if mode == "thumbnail":
        # topic + reference thumbnail -> assign graphics; youtuber submits the video link later
        g = pc.graphics_task(db, t, create=True)
        g.status = "new"
        g.instructions = (payload.get("instructions") or payload.get("remarks") or "").strip()
        _ref = payload.get("reference_thumbnail") or payload.get("images")
        if _ref:
            try:
                urls = pc.save_images(db, t, _ref if isinstance(_ref, list) else [_ref],
                                      "reference", None, me, return_urls=True) or []
                if urls:
                    g.reference_image = urls[0]
                    try:
                        import json as _json
                        g.reference_images = _json.dumps(urls)   # designer ko SAARI references dikhein
                    except Exception:
                        pass
            except Exception:
                pass
        _gid = payload.get("graphics_id")
        if _gid:
            try:
                gr = db.query(ProductionStaffProfile).filter(
                    ProductionStaffProfile.id == int(_gid),
                    ProductionStaffProfile.staff_role == "graphics").first()
                if gr:
                    g.graphics_id = gr.id
                    t.graphics_id = gr.id
                    if gr.user_id:
                        pc.notify(db, gr.user_id, "New Thumbnail Task",
                                  f'Design a thumbnail for: "{t.title}".', "video_task", link=str(t.id))
            except Exception:
                pass
        pc.set_state(db, t, "creator_working", actor=me, event="youtuber_task_created", force=True)
        pc.notify_pms(db, "YouTuber Task Created",
                      f'{me.name} started "{t.title}" — thumbnail in progress.', "production", link=str(t.id))
    else:
        # ready: video shot + thumbnail -> submit directly
        link = (payload.get("drive_link") or "").strip()
        if not link:
            raise HTTPException(400, "A Drive link is required for a ready video")
        t.submitted_link = link
        t.submitted_at = datetime.utcnow()
        _th = payload.get("thumbnail") or payload.get("images")
        if _th:
            try:
                urls = pc.save_images(db, t, _th if isinstance(_th, list) else [_th],
                                      "thumbnail", None, me, return_urls=True) or []
                if urls:
                    t.thumbnail_link = urls[0]
            except Exception:
                pass
        if pc.needs_pm_approval(db, t):
            pc.set_state(db, t, "pm_review", actor=me, event="youtuber_submitted", force=True)
            pc.notify_pms(db, "Video Submitted (Review)",
                          f'{me.name} submitted "{t.title}" for review.', "production", link=str(t.id))
        else:
            pc.set_state(db, t, "approved", actor=me, event="youtuber_submitted", force=True)
            try:
                pc.graphics_task(db, t, create=True)
            except Exception:
                pass
            if t.editor_id:
                pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned", force=True)
                ep2 = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
                if ep2 and ep2.user_id:
                    pc.notify(db, ep2.user_id, "New editing task",
                              f'"{t.title}" is ready to edit.', "editor_task", link=str(t.id))
            pc.notify_pms(db, "Video Received",
                          f'{me.name} submitted "{t.title}" — entered production.', "production", link=str(t.id))
    db.commit()
    return {"ok": True, "id": t.id, "ref_code": t.ref_code, "lifecycle": t.lifecycle}


@router.post("/videos/{tid}/thumbnail")
def yt_thumbnail(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    _th = payload.get("thumbnail") or payload.get("images")
    if not _th:
        raise HTTPException(400, "A thumbnail image is required")
    urls = pc.save_images(db, t, _th if isinstance(_th, list) else [_th],
                          "thumbnail", None, me, return_urls=True) or []
    if urls:
        t.thumbnail_link = urls[0]
    pc.log_event(db, t, me, "thumbnail_uploaded", new_state=t.lifecycle)
    db.commit()
    return {"ok": True, "thumbnail": t.thumbnail_link or ""}


@router.post("/videos/{tid}/assign-graphics")
def yt_assign_graphics(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    from models import ProductionStaffProfile
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    gid = int(payload.get("graphics_id") or 0)
    gr = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.id == gid, ProductionStaffProfile.staff_role == "graphics").first()
    if not gr:
        raise HTTPException(400, "Valid graphics_id required")
    g = pc.graphics_task(db, t, create=True)
    g.graphics_id = gid
    g.status = "new"
    g.instructions = (payload.get("instructions") or g.instructions or "").strip()
    _ref = payload.get("reference_thumbnail") or payload.get("images")
    if _ref:
        try:
            urls = pc.save_images(db, t, _ref if isinstance(_ref, list) else [_ref],
                                  "reference", None, me, return_urls=True) or []
            if urls:
                g.reference_image = urls[0]
        except Exception:
            pass
    t.graphics_id = gid
    pc.log_event(db, t, me, "graphics_assigned", new_state=t.lifecycle)
    if gr.user_id:
        pc.notify(db, gr.user_id, "New Thumbnail Task",
                  f'Design a thumbnail for: "{t.title}".', "video_task", link=str(t.id))
    db.commit()
    return {"ok": True}


@router.get("/videos/{tid}/comments")
def yt_comments(tid: int, audience: str = "creator", db: Session = Depends(get_db), me=Depends(get_youtuber)):
    import video_tasks as _VT
    yp = _me_yt(db, me)
    _my_task(db, yp, tid)
    aud = (audience or "creator").strip().lower()
    if aud not in ("creator", "editor"):
        aud = "creator"
    return {"comments": _VT._vtc_list(db, tid, aud)}


@router.post("/videos/{tid}/comments")
def yt_comment_add(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    import video_tasks as _VT
    from models import ProductionStaffProfile
    yp = _me_yt(db, me)
    t = _my_task(db, yp, tid)
    aud = (payload.get("audience") or "creator").strip().lower()
    if aud not in ("creator", "editor"):
        aud = "creator"
    att = (payload.get("attachment_url") or "").strip()
    imgs = payload.get("images")
    if imgs and not att:
        try:
            urls = pc.save_images(db, t, imgs if isinstance(imgs, list) else [imgs], "chat", None, me, return_urls=True) or []
            if urls:
                att = urls[0]
        except Exception:
            pass
    c = _VT._vtc_add(db, tid, me, payload.get("message") or "", "youtuber", attachment_url=att, audience=aud)
    if not c:
        raise HTTPException(400, "Empty message")
    if aud == "editor":
        if t.editor_id:
            ep = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == t.editor_id).first()
            if ep and ep.user_id:
                pc.notify(db, ep.user_id, "Message from YouTuber",
                          f'{me.name}: "{(payload.get("message") or "").strip()[:60]}"', "creator_chat", link=str(t.id))
    else:
        pc.notify_pms(db, "Message from YouTuber",
                      f'{me.name}: "{(payload.get("message") or "").strip()[:60]}"', "creator_chat", link=str(t.id))
    db.commit()
    return {"ok": True, "comment": _VT._vtc_out(db, c)}


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
                  priority=((payload.get("priority") or "").strip() or "normal"),
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
                  priority=((payload.get("priority") or "").strip() or "normal"),
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


# ============================================================ PROFILE PHOTO
@router.post("/me/photo")
def yt_photo_set(payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """Store the youtuber's profile photo (data URL, any image format). Bulletproof:
    accepts whatever the client sends; empty string clears it."""
    yp = _me_yt(db, me)
    yp.photo_b64 = (payload.get("photo") or "").strip() or None
    db.commit()
    return {"ok": True, "has_photo": bool(yp.photo_b64)}


@router.get("/me/photo")
def yt_photo_get(db: Session = Depends(get_db), me=Depends(get_youtuber)):
    yp = _me_yt(db, me)
    return {"photo": (yp.photo_b64 if yp else "") or "", "name": getattr(me, "name", ""), "role": "youtuber"}


@router.post("/videos/{tid}/edit")
def yt_edit_task(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_youtuber)):
    """YouTuber apna task edit kare (title/deadline/channel/type/priority/remarks) —
    sirf apna task, aur sirf jab tak publish (uploaded/completed) na ho."""
    yp = _me_yt(db, me)
    t = db.query(VideoTask).filter(VideoTask.id == tid, VideoTask.youtuber_id == yp.id).first()
    if not t:
        raise HTTPException(404, "Task nahi mila")
    if (t.lifecycle or "") in ("uploaded", "completed"):
        raise HTTPException(400, "Ye task publish ho chuka hai — ab edit nahi ho sakta")
    changes = []

    def _set(field, key, label):
        if key in payload:
            v = payload.get(key)
            v = v.strip() if isinstance(v, str) else v
            if getattr(t, field) != v:
                setattr(t, field, v)
                changes.append(label)

    _set("title", "title", "title")
    _set("video_type", "video_type", "type")
    _set("channel_name", "channel", "channel")
    _set("streaming", "streaming", "streaming")
    _set("remarks", "remarks", "remarks")
    _set("reference", "reference", "reference")
    pri = (payload.get("priority") or "").strip()
    if pri and pri != (t.priority or ""):
        t.priority = pri
        changes.append("priority")
    if "deadline" in payload:
        raw = (payload.get("deadline") or "").strip()
        newdl = t.deadline
        if raw:
            try:
                newdl = datetime.fromisoformat(raw.replace("Z", ""))
            except Exception:
                newdl = t.deadline
        else:
            newdl = None
        if newdl != t.deadline:
            t.deadline = newdl
            changes.append("deadline")
    if not (t.title or "").strip():
        raise HTTPException(400, "Title zaroori hai")
    # From the Edit modal: assign an editor / graphics ONLY if none is assigned yet.
    from models import ProductionStaffProfile
    _eid = str(payload.get("editor_id") or "").strip()
    if _eid and not t.editor_id:
        try:
            ed = db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.id == int(_eid),
                ProductionStaffProfile.staff_role == "editor").first()
            if ed:
                t.editor_id = ed.id
                try:
                    pc.set_state(db, t, "editor_assigned", actor=me, event="editor_assigned", force=True)
                except Exception:
                    pass
                if ed.user_id:
                    pc.notify(db, ed.user_id, "New Editing Task",
                              'You were assigned to edit "%s".' % t.title, "video_task", link=str(t.id))
                pc.notify_pms(db, "Editor Assigned by Creator",
                              '%s assigned an editor to "%s".' % (me.name, t.title), "production", link=str(t.id))
                changes.append("editor")
        except Exception:
            pass
    _gid = str(payload.get("graphics_id") or "").strip()
    if _gid and not t.graphics_id:
        try:
            gr = db.query(ProductionStaffProfile).filter(
                ProductionStaffProfile.id == int(_gid),
                ProductionStaffProfile.staff_role == "graphics").first()
            if gr:
                g = pc.graphics_task(db, t, create=True)
                g.graphics_id = gr.id
                t.graphics_id = gr.id
                if (g.status or "") in ("", "new"):
                    g.status = "new"
                if gr.user_id:
                    pc.notify(db, gr.user_id, "New Thumbnail Task",
                              'You were assigned a thumbnail for "%s".' % t.title, "video_task", link=str(t.id))
                pc.notify_pms(db, "Graphics Assigned by Creator",
                              '%s assigned graphics to "%s".' % (me.name, t.title), "production", link=str(t.id))
                changes.append("graphics")
        except Exception:
            pass
    if changes:
        try:
            pc.log_event(db, t, me, "task_edited", new_state=t.lifecycle,
                         meta={"note": "Edited: " + ", ".join(changes)})
        except Exception:
            pass
    db.commit()
    return {"ok": True, "changed": changes}
