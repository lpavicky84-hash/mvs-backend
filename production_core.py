"""Production ecosystem — shared state engine & helpers.

One VideoTask row flows through the production lifecycle. Status is DERIVED from
real actions (never a free dropdown). Every important action writes an immutable
ProductionEvent. The legacy `status` field is kept loosely in sync so the existing
Task Manager UI does not break (see LEGACY_MAP).

Used by: production_routes, editor_routes, youtuber_routes, graphics_routes.
"""
from datetime import datetime
import json

from models import (
    User, UserRole, VideoTask, GraphicsTask, EditingSession, ProductionEvent,
    TaskReview, TaskAttachment, YouTuberProfile, ProductionStaffProfile,
    TeacherProfile, Notification,
)

# ---------------------------------------------------------------- lifecycle
# Canonical lifecycle states (VideoTask.lifecycle). Graphics runs in PARALLEL and
# is tracked on GraphicsTask.status — it is intentionally NOT in this list.
LC = {
    "created":            "Task Created",
    "creator_assigned":   "Creator Assigned",
    "creator_working":    "Creator Shooting",
    "creator_submitted":  "Video Submitted",
    "pm_review":          "PM Review",
    "approved":           "Approved",
    "changes_required":   "Changes Requested (Creator)",
    "rejected":           "Rejected / Reshoot",
    "editor_assigned":    "Editor Assigned",
    "editing":            "Editing In Progress",
    "editing_paused":     "Editing Paused",
    "editing_done":       "Editing Completed",
    "qc_pending":         "QC Pending",
    "qc_changes":         "Changes Requested (Editor)",
    "ready_for_youtube":  "Ready for YouTube",
    "uploaded":           "Uploaded",
    "completed":          "Completed",
}

# Best-effort mapping to the EXISTING (legacy) VideoTask.status vocabulary so the
# old Task Manager counters keep working. Only a subset maps cleanly.
LEGACY_MAP = {
    "creator_submitted": "submitted",
    "pm_review":         "submitted",
    "approved":          "approved",
    "changes_required":  "assigned",
    "rejected":          "reshoot",
    "editor_assigned":   "approved",
    "editing":           "editing_soon",
    "editing_paused":    "editing_soon",
    "editing_done":      "editing_done",
    "qc_pending":        "editing_done",
    "qc_changes":        "editing_done",
    "ready_for_youtube": "editing_done",
    "uploaded":          "uploaded",
    "completed":         "uploaded",
}


def lc_label(state):
    return LC.get(state or "", state or "")


# ---------------------------------------------------------------- ref codes
def ensure_ref_code(t):
    """Assign a readable id (VID-YYYY-000123) once. Numeric PK stays the source of truth."""
    if getattr(t, "ref_code", ""):
        return t.ref_code
    yr = (t.created_at or datetime.utcnow()).year
    t.ref_code = "VID-%d-%06d" % (yr, int(t.id or 0))
    return t.ref_code


# ---------------------------------------------------------------- events
def log_event(db, t, actor, event, new_state=None, prev_state=None, meta=None):
    """Write an immutable production timeline event."""
    role = ""
    name = ""
    aid = None
    if actor is not None:
        aid = getattr(actor, "id", None)
        role = getattr(actor.role, "value", str(getattr(actor, "role", ""))) if getattr(actor, "role", None) else ""
        name = getattr(actor, "name", "") or ""
    db.add(ProductionEvent(
        task_id=t.id, actor_user_id=aid, actor_role=role, actor_name=name,
        event=event, prev_state=(prev_state or ""), new_state=(new_state or ""),
        meta=(json.dumps(meta) if meta else ""),
    ))


def set_state(db, t, new_state, actor=None, event=None):
    """Move the task to a new lifecycle state, keep legacy status in sync, log event."""
    prev = t.lifecycle or ""
    t.lifecycle = new_state
    leg = LEGACY_MAP.get(new_state)
    if leg:
        t.status = leg
    log_event(db, t, actor, event or new_state, new_state=new_state, prev_state=prev)


# ---------------------------------------------------------------- notifications
def notify(db, user_id, title, message, ntype="production", link=None):
    if not user_id:
        return
    db.add(Notification(user_id=user_id, title=title, message=message,
                        notif_type=ntype, link=link or None))


def notify_pms(db, title, message, ntype="production", link=None):
    """Notify all active Production Managers (admins monitor via their own panel)."""
    pms = db.query(User).filter(User.role == UserRole.production_manager,
                                User.is_active == True).all()
    for u in pms:
        notify(db, u.id, title, message, ntype, link)


# ---------------------------------------------------------------- profiles
def staff_profile(db, user):
    """ProductionStaffProfile for the logged-in editor/graphics/PM user."""
    if user is None:
        return None
    return db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.user_id == user.id).first()


def youtuber_profile(db, user):
    if user is None:
        return None
    return db.query(YouTuberProfile).filter(YouTuberProfile.user_id == user.id).first()


def graphics_task(db, t, create=False):
    """Get (or create) the GraphicsTask row attached to a video task."""
    g = db.query(GraphicsTask).filter(GraphicsTask.task_id == t.id).first()
    if not g and create:
        g = GraphicsTask(task_id=t.id, status="new")
        db.add(g)
        db.flush()
    return g


# ---------------------------------------------------------------- names
def _name_for_staff(db, sid):
    if not sid:
        return ""
    sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == sid).first()
    if sp and sp.user:
        return sp.user.name or ""
    return ""


def _name_for_youtuber(db, yid):
    if not yid:
        return ""
    yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == yid).first()
    if yp and yp.user:
        return yp.user.name or ""
    return ""


def _name_for_teacher(db, tid):
    if not tid:
        return ""
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if tp and tp.user:
        return tp.user.name or ""
    return ""


def creator_info(db, t):
    """(name, type_label) for the task's creator."""
    if (t.creator_type or "teacher") == "youtuber":
        return _name_for_youtuber(db, t.youtuber_id), "YOUTUBER"
    return _name_for_teacher(db, t.teacher_id), "TEACHER"


# ---------------------------------------------------------------- approval
def needs_pm_approval(db, t):
    """Per-video override wins; else the creator's default. Teachers always go via PM."""
    if t.approval_required is not None:
        return bool(t.approval_required)
    if (t.creator_type or "teacher") == "youtuber" and t.youtuber_id:
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
        return bool(yp.approval_required) if yp else True
    return True   # teacher submissions default to PM review


# ---------------------------------------------------------------- next action
def next_action(db, t, g=None):
    """A clear, human 'what happens next' for the current state."""
    s = t.lifecycle or ""
    m = {
        "created":           "Assign a creator",
        "creator_assigned":  "Waiting for creator to shoot & submit",
        "creator_working":   "Waiting for creator to submit the video",
        "creator_submitted": "Waiting for PM approval",
        "pm_review":         "Waiting for PM approval",
        "approved":          "Assign an editor",
        "changes_required":  "Waiting for creator to resubmit",
        "rejected":          "Reshoot required",
        "editor_assigned":   "Waiting for editor to start",
        "editing":           "Editing in progress",
        "editing_paused":    "Editing paused",
        "editing_done":      "Waiting for editor to submit the edited video",
        "qc_pending":        "Waiting for QC",
        "qc_changes":        "Waiting for editor to resubmit",
        "ready_for_youtube": "Ready for YouTube — add the published link",
        "uploaded":          "Published on YouTube",
        "completed":         "Completed",
    }
    return m.get(s, "In production")


def waiting_since(t):
    """Datetime the task entered its current state (best-effort from updated_at)."""
    return t.updated_at or t.created_at


# ---------------------------------------------------------------- attachments
def save_images(db, t, images, kind, review_id, uploader):
    """Store base64/dataURL images to R2 (fallback base64) as TaskAttachment rows.
    Never raises — a bad image is skipped so the parent action still succeeds."""
    if not images:
        return 0
    import base64 as _b64
    try:
        import r2_storage as _r2
    except Exception:
        _r2 = None
    n = 0
    for img in list(images)[:8]:
        try:
            s = img or ""
            mime = "image/png"
            if isinstance(s, str) and s.startswith("data:"):
                head, s = s.split(",", 1)
                try:
                    mime = head.split(":", 1)[1].split(";", 1)[0] or mime
                except Exception:
                    pass
            s = "".join(str(s).split())
            raw = _b64.b64decode(s + "=" * (-len(s) % 4))
            if not raw or len(raw) < 8:
                continue
            ext = (mime.split("/")[-1] or "png")[:5]
            if _r2 is not None:
                url = _r2.store_file_value(_r2.new_key("production/qc", "img." + ext), raw, mime)
            else:
                url = _b64.b64encode(raw).decode("ascii")
        except Exception:
            continue
        db.add(TaskAttachment(task_id=t.id, review_id=review_id, kind=kind, url=url,
                              mime=mime, uploader_user_id=getattr(uploader, "id", None)))
        n += 1
    return n


def attachments_out(db, t):
    rows = (db.query(TaskAttachment).filter(TaskAttachment.task_id == t.id)
            .order_by(TaskAttachment.id.desc()).all())
    out = []
    for a in rows:
        url = a.url or ""
        if url and not url.startswith("http"):
            url = "data:" + (a.mime or "image/png") + ";base64," + url
        out.append({"id": a.id, "kind": a.kind or "review", "url": url,
                    "mime": a.mime or "", "at": _dt(a.created_at)})
    return out


# ---------------------------------------------------------------- serializers
def _dt(x):
    return x.strftime("%d %b %Y, %I:%M %p") if x else ""


def task_out(db, t, g=None, timeline=False, light=False):
    """Production-facing task serializer (no heavy base64 blobs)."""
    if g is None:
        g = db.query(GraphicsTask).filter(GraphicsTask.task_id == t.id).first()
    cname, ctype = creator_info(db, t)
    out = {
        "id": t.id,
        "ref_code": t.ref_code or ensure_ref_code(t),
        "title": t.title or "",
        "creator_type": (t.creator_type or "teacher"),
        "creator_name": cname,
        "creator_badge": ("%s \u00b7 %s" % (cname, ctype)) if cname else ctype,
        "subject": t.subject or "",
        "video_type": t.video_type or "",
        "channel_name": t.channel_name or "",
        "streaming": t.streaming or "",
        "priority": t.priority or "normal",
        "is_old": bool(getattr(t, "is_old", False)),
        "lifecycle": t.lifecycle or "",
        "lifecycle_label": lc_label(t.lifecycle),
        "legacy_status": t.status or "",
        "next_action": next_action(db, t, g),
        "deadline": _dt(t.deadline),
        "deadline_flag": (lambda f: {"kind": f[0], "label": f[1]})(deadline_flag(t)),
        "editor_id": t.editor_id,
        "editor_name": _name_for_staff(db, t.editor_id),
        "editing_progress": t.editing_progress or 0,
        "editing_seconds": t.editing_seconds or 0,
        "edited_link": t.edited_link or "",
        "qc_status": t.qc_status or "",
        "revision_count": t.revision_count or 0,
        "approval_required": needs_pm_approval(db, t),
        "on_hold": bool(t.on_hold),
        "cancelled": bool(t.cancelled),
        "youtube_url": t.youtube_url or "",
        "yt_video_id": t.yt_video_id or "",
        "yt_views": (t.yt_views if t.yt_views is not None else None),
        "yt_views_at": _dt(t.yt_views_at),
        "published_at": _dt(t.published_at),
        "reference": t.reference or "",
        "submitted_link": t.submitted_link or "",
        "created_at": _dt(t.created_at),
        # card thumbnail: graphics-made thumbnail first, else the one uploaded at assign time
        "thumbnail": ((g.thumbnail_url if g else "") or getattr(t, "thumbnail_b64", "") or (t.thumbnail_link or "")),
        "graphics": {
            "id": (g.id if g else None),
            "graphics_id": (g.graphics_id if g else None),
            "graphics_name": (_name_for_staff(db, g.graphics_id) if g else ""),
            "status": (g.status if g else "new"),
            "thumbnail_url": (g.thumbnail_url if g else ""),
            "reference_image": (g.reference_image if g else ""),
            "instructions": (g.instructions if g else ""),
            "revision_count": (g.revision_count if g else 0),
        },
    }
    if not light:
        out["thumbnail_link"] = t.thumbnail_link or ""
        out["deadline_iso"] = (t.deadline.strftime("%Y-%m-%dT%H:%M") if t.deadline else "")
        out["reference"] = t.reference or ""
        out["remarks"] = t.remarks or ""
    if timeline:
        out["timeline"] = timeline_out(db, t)
        out["attachments"] = attachments_out(db, t)
    return out


def timeline_out(db, t):
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id)
            .order_by(ProductionEvent.created_at.asc(), ProductionEvent.id.asc()).all())
    merged = []
    for e in rows:
        merged.append((e.created_at, {
            "event": e.event, "label": _event_label(e.event),
            "actor": e.actor_name or "", "role": e.actor_role or "",
            "prev": e.prev_state or "", "new": e.new_state or "",
            "note": getattr(e, "note", "") or "",
            "at": _dt(e.created_at),
        }))
    # merge admin Task-Manager history (status_history JSON) so tasks created or updated
    # in the admin panel also show a full timeline in the production portal.
    _AH = {"assigned": "Assigned", "submitted": "Submitted", "approved": "Approved",
           "reshoot": "Reshoot Requested", "rejected": "Rejected", "editing_soon": "Editing Soon",
           "editing_done": "Editing Done", "uploaded": "Uploaded", "verify": "Verified",
           "progress": "Progress Update", "proposal": "Proposed", "changes": "Changes Requested"}
    try:
        import json as _jh
        from datetime import datetime as _dtc
        hist = _jh.loads(t.status_history) if getattr(t, "status_history", "") else []
    except Exception:
        hist = []
    for h in hist:
        raw = h.get("at", "")
        ts = None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                ts = _dtc.strptime(raw, fmt); break
            except Exception:
                continue
        s = h.get("s", "")
        merged.append((ts or _dtc.min, {
            "event": s, "label": _AH.get(s, s.replace("_", " ").title() if s else "Update"),
            "actor": "", "role": "", "prev": "", "new": s,
            "note": h.get("note", "") or "", "at": (ts.strftime("%d %b %Y, %I:%M %p") if ts else raw),
        }))
    merged.sort(key=lambda x: (x[0] is None, x[0]))
    # de-dup exact same label+at (production event + admin history overlap)
    seen = set(); out = []
    for _, d in merged:
        k = (d["label"], d["at"])
        if k in seen:
            continue
        seen.add(k); out.append(d)
    return out


_EVENT_LABELS = {
    "task_created": "Task Created",
    "creator_assigned": "Creator Assigned",
    "teacher_submitted": "Video Submitted",
    "youtuber_submitted": "Video Submitted",
    "approval_requested": "Sent for PM Approval",
    "approved": "Approved",
    "changes_requested": "Changes Requested",
    "rejected": "Rejected / Reshoot",
    "editor_assigned": "Editor Assigned",
    "graphics_assigned": "Graphics Assigned",
    "editing_started": "Editing Started",
    "editing_paused": "Editing Paused",
    "editing_resumed": "Editing Resumed",
    "progress_updated": "Progress Updated",
    "editing_completed": "Editing Completed",
    "edited_video_submitted": "Edited Video Submitted",
    "thumbnail_started": "Thumbnail Started",
    "thumbnail_submitted": "Thumbnail Submitted",
    "thumbnail_approved": "Thumbnail Approved",
    "thumbnail_changes_requested": "Thumbnail Changes Requested",
    "qc_approved": "QC Approved",
    "revision_submitted": "Revision Submitted",
    "youtube_link_added": "YouTube Link Added",
    "uploaded": "Uploaded",
    "youtube_metrics_updated": "YouTube Metrics Updated",
}


def _event_label(ev):
    return _EVENT_LABELS.get(ev, (ev or "").replace("_", " ").title())


# ---------------------------------------------------------------- notifications
def notifications_out(db, user, limit=40):
    rows = (db.query(Notification).filter(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc()).limit(limit).all())
    out = []
    for n in rows:
        tid = None
        try:
            if n.link and str(n.link).isdigit():
                tid = int(n.link)
        except Exception:
            tid = None
        out.append({"id": n.id, "title": n.title or "", "message": n.message or "",
                    "type": n.notif_type or "", "is_read": bool(n.is_read),
                    "task_id": tid, "at": _dt(n.created_at)})
    return out


def unread_count(db, user):
    return db.query(Notification).filter(Notification.user_id == user.id,
                                         Notification.is_read == False).count()


def mark_read(db, user, nid=None):
    q = db.query(Notification).filter(Notification.user_id == user.id,
                                      Notification.is_read == False)
    if nid:
        q = q.filter(Notification.id == nid)
    for n in q.all():
        n.is_read = True


# ---------------------------------------------------------------- deadline
def deadline_flag(t):
    """Short human deadline signal for cards/filters."""
    if not t.deadline:
        return ("none", "No deadline")
    now = datetime.utcnow()
    delta = (t.deadline - now).total_seconds()
    if t.lifecycle in ("uploaded", "completed"):
        return ("done", "Completed")
    if delta < 0:
        h = int(-delta // 3600)
        return ("overdue", "Overdue by %dh" % h if h else "Overdue")
    if delta < 3600:
        return ("soon", "Due in %dm" % int(delta // 60))
    if delta < 86400:
        return ("today", "Due in %dh" % int(delta // 3600))
    return ("later", "Due in %dd" % int(delta // 86400))
