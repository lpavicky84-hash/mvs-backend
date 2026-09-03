"""Production ecosystem — shared state engine & helpers.

One VideoTask row flows through the production lifecycle. Status is DERIVED from
real actions (never a free dropdown). Every important action writes an immutable
ProductionEvent. The legacy `status` field is kept loosely in sync so the existing
Task Manager UI does not break (see LEGACY_MAP).

Used by: production_routes, editor_routes, youtuber_routes, graphics_routes.
"""
from datetime import datetime, timezone, timedelta
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
    "created":              "Task Created",
    "creator_assigned":     "Task Assigned",
    "thumbnail_pending":    "Thumbnail Pending",
    "thumbnail_in_progress":"Thumbnail In Progress",
    "thumbnail_submitted":  "Thumbnail Submitted",
    "thumbnail_approved":   "Thumbnail Approved",
    "creator_working":      "Shooting",
    "creator_submitted":    "Video Submitted",
    "pm_review":            "PM Review",
    "changes_required":     "Resubmit Required",
    "reshoot_required":     "Reshoot Required",
    "rejected":             "Rejected",
    "approved":             "Approved",
    "editor_assigned":      "Editing Soon",
    "editing_soon":         "Editing Soon",
    "editing":              "Editing In Progress",
    "editing_paused":       "Editing Paused",
    "editing_done":         "Editing Done",
    "qc_pending":           "Editor Submitted",
    "qc_changes":           "Changes Required",
    "qc_approved":          "QC Approved",
    "ready_for_youtube":    "Ready for YouTube",
    "uploaded":             "Uploaded",
    "completed":            "Completed",
}

# Best-effort mapping to the EXISTING (legacy) VideoTask.status vocabulary so the
# old Task Manager counters keep working. Only a subset maps cleanly.
LEGACY_MAP = {
    "creator_assigned":     "assigned",
    "thumbnail_pending":    "assigned",
    "thumbnail_in_progress":"assigned",
    "thumbnail_submitted":  "assigned",
    "thumbnail_approved":   "assigned",
    "creator_working":      "assigned",
    "creator_submitted":    "submitted",
    "pm_review":            "submitted",
    "approved":             "approved",
    "changes_required":     "assigned",
    "reshoot_required":     "reshoot",
    "rejected":             "reshoot",
    "editor_assigned":      "approved",
    "editing_soon":         "editing_soon",
    "editing":              "editing_soon",
    "editing_paused":       "editing_soon",
    "editing_done":         "editing_done",
    "qc_pending":           "editing_done",
    "qc_changes":           "editing_done",
    "qc_approved":          "editing_done",
    "ready_for_youtube":    "editing_done",
    "uploaded":             "uploaded",
    "completed":            "uploaded",
}

# ---------------------------------------------------------------- state machine
# One production task = one source of truth (VideoTask.lifecycle). Transitions are
# CONTROLLED: only the moves below are legal. Admins can override (oversight), and a
# fresh task (empty state) may enter at any initial state. Everything else is rejected
# server-side so no portal can push a task into an impossible state.
ALLOWED_TRANSITIONS = {
    "created":              {"creator_assigned", "editor_assigned", "rejected"},
    # Task assigned to a creator: may run the optional thumbnail sub-flow, start
    # shooting, be submitted, or (youtuber direct/approval paths) jump ahead.
    "creator_assigned":     {"thumbnail_pending", "thumbnail_approved", "creator_working",
                             "creator_submitted", "pm_review", "approved", "editor_assigned",
                             "changes_required", "reshoot_required", "rejected"},
    # ---- optional thumbnail sub-flow (graphics) ----
    "thumbnail_pending":    {"thumbnail_in_progress", "thumbnail_approved", "creator_working"},
    "thumbnail_in_progress":{"thumbnail_submitted"},
    "thumbnail_submitted":  {"thumbnail_approved", "thumbnail_in_progress"},
    "thumbnail_approved":   {"creator_working", "creator_submitted", "creator_assigned"},
    # ---- shooting / submission ----
    "creator_working":      {"creator_submitted", "pm_review", "approved", "thumbnail_pending"},
    "creator_submitted":    {"pm_review", "approved", "changes_required",
                             "reshoot_required", "rejected"},
    "pm_review":            {"approved", "changes_required", "reshoot_required",
                             "rejected", "editor_assigned"},
    # ---- creator rework branches ----
    "changes_required":     {"creator_working", "creator_submitted", "pm_review", "approved"},
    "reshoot_required":     {"creator_working", "creator_submitted", "pm_review"},
    "rejected":             {"creator_assigned", "creator_working"},   # reopen (admin flows)
    # ---- editing ----
    "approved":             {"editor_assigned", "editing_soon"},
    "editor_assigned":      {"editing_soon", "editing", "editing_paused"},
    "editing_soon":         {"editing", "editing_paused"},
    "editing":              {"editing_paused", "editing_done"},
    "editing_paused":       {"editing", "editing_done"},
    "editing_done":         {"qc_pending"},
    # ---- QC ----
    "qc_pending":           {"ready_for_youtube", "qc_approved", "qc_changes"},
    "qc_approved":          {"ready_for_youtube"},
    "qc_changes":           {"editing", "editing_done", "qc_pending"},
    # ---- publish ----
    "ready_for_youtube":    {"uploaded"},
    "uploaded":             {"completed"},
    "completed":            set(),
}
# States reachable from ANYWHERE (safety valves). Kept intentionally small.
ALWAYS_ALLOWED = set()


class TransitionError(Exception):
    """Raised when a lifecycle transition is not permitted by the state machine."""
    pass


def can_transition(prev, new_state):
    """True if moving prev -> new_state is a legal controlled transition."""
    prev = prev or ""
    new_state = new_state or ""
    if not new_state:
        return False
    if prev == new_state:
        return True                      # idempotent no-op
    if not prev:
        return True                      # fresh task may enter at any state
    if new_state in ALWAYS_ALLOWED:
        return True
    return new_state in ALLOWED_TRANSITIONS.get(prev, set())


def allowed_next(state):
    """The set of legal next states from `state` (for UIs / validation)."""
    return sorted(ALLOWED_TRANSITIONS.get(state or "", set()) | ALWAYS_ALLOWED)


def _actor_is_admin(actor):
    if actor is None:
        return False
    r = getattr(actor, "role", None)
    r = getattr(r, "value", r)
    return str(r) == "admin"


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


def set_state(db, t, new_state, actor=None, event=None, meta=None, force=False):
    """Move a task to a new lifecycle state through the CONTROLLED state machine.

    - Validates the transition (raises TransitionError if illegal) unless `force=True`
      or the actor is an admin (oversight override).
    - Keeps the legacy VideoTask.status in sync via LEGACY_MAP (old Task Manager).
    - Appends an immutable timeline event. History is never overwritten.
    Returns True on success.
    """
    prev = t.lifecycle or ""
    if not force and not _actor_is_admin(actor) and not can_transition(prev, new_state):
        raise TransitionError(
            "Illegal transition %s -> %s (allowed: %s)"
            % (prev or "(new)", new_state, ", ".join(allowed_next(prev)) or "none"))
    t.lifecycle = new_state
    leg = LEGACY_MAP.get(new_state)
    if leg:
        t.status = leg
    if prev != new_state:
        log_event(db, t, actor, event or new_state, new_state=new_state,
                  prev_state=prev, meta=meta)
    return True


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


def _json_list(s, fallback=""):
    """Parse a JSON array of URLs; fall back to a single-item list."""
    import json as _j
    try:
        v = _j.loads(s) if s else []
        if isinstance(v, list):
            out = [x for x in v if x]
            if out:
                return out
    except Exception:
        pass
    return [fallback] if fallback else []


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
def save_images(db, t, images, kind, review_id, uploader, return_urls=False):
    """Store base64/dataURL images to R2 (fallback base64) as TaskAttachment rows.
    Never raises — a bad image is skipped so the parent action still succeeds.
    return_urls=True returns the list of stored URLs (for thumbnails) instead of a count."""
    if not images:
        return [] if return_urls else 0
    import base64 as _b64
    try:
        import r2_storage as _r2
    except Exception:
        _r2 = None
    n = 0
    urls = []
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
        # for return_urls, expose a directly-usable URL (data-uri for base64 fallback)
        urls.append(url if str(url).startswith("http") else ("data:" + mime + ";base64," + url))
    return urls if return_urls else n


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
    """UTC-stored times (created / submitted / events / progress) -> IST display."""
    if not x:
        return ""
    try:
        y = x.replace(tzinfo=timezone.utc) if getattr(x, "tzinfo", None) is None else x
        y = y.astimezone(timezone(timedelta(hours=5, minutes=30)))
    except Exception:
        y = x
    return y.strftime("%d %b %Y, %I:%M %p")


def _dt_raw(x):
    """Already-local (IST) times like deadline -> show as-is (no shift)."""
    return x.strftime("%d %b %Y, %I:%M %p") if x else ""


# ==================================================================== SELF-HEALING SCHEMA
# Bulletproof: even if main.py's migration did not run (e.g. only these files were
# deployed), make sure the new production columns exist. Idempotent + safe on every boot
# (duplicate-column errors are ignored). Prevents "Unknown column" crashes that would
# otherwise flood logs and exhaust the DB pool.
def _ensure_production_columns():
    try:
        from database import engine
        from sqlalchemy import text as _sql_text
    except Exception:
        return
    _stmts = [
        "ALTER TABLE video_tasks ADD COLUMN deadline_req DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN deadline_req_reason VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN deadline_req_status VARCHAR(20)",
        "ALTER TABLE video_tasks ADD COLUMN quality_rating INTEGER",
        "ALTER TABLE video_tasks ADD COLUMN quality_note VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN remarks_audience VARCHAR(10)",
        "ALTER TABLE production_staff_profiles ADD COLUMN rank1_since DATETIME",
        "ALTER TABLE production_staff_profiles ADD COLUMN rank_appreciated_at DATETIME",
        "ALTER TABLE graphics_tasks ADD COLUMN drive_link VARCHAR(600)",
        "ALTER TABLE graphics_tasks ADD COLUMN deadline DATETIME",
        "ALTER TABLE graphics_tasks ADD COLUMN priority VARCHAR(12)",
        "ALTER TABLE graphics_tasks ADD COLUMN quality_rating INTEGER",
        "ALTER TABLE graphics_tasks ADD COLUMN quality_note VARCHAR(400)",
        "ALTER TABLE graphics_tasks ADD COLUMN reference_images TEXT",
        "ALTER TABLE graphics_tasks ADD COLUMN thumbnail_candidates TEXT",
        "ALTER TABLE graphics_tasks ADD COLUMN final_note VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN quality_dims TEXT",
        "ALTER TABLE video_tasks ADD COLUMN ontime_appreciated BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN description TEXT",
        "ALTER TABLE video_tasks ADD COLUMN thumbnail_required BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN no_resubmit BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN reference_video TEXT",
        "ALTER TABLE video_tasks ADD COLUMN series_name VARCHAR(200)",
        "ALTER TABLE youtuber_profiles ADD COLUMN monthly_target INTEGER",
        "ALTER TABLE video_task_comments ADD COLUMN attachment_url VARCHAR(600)",
        "ALTER TABLE video_task_comments ADD COLUMN audience VARCHAR(20)",
    ]
    for _s in _stmts:
        try:
            with engine.connect() as _conn:
                _conn.execute(_sql_text(_s))
                _conn.commit()
        except Exception:
            pass
    # ensure the pm_events table exists (announcements/events)
    try:
        from models import PmEvent
        PmEvent.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass


try:
    _ensure_production_columns()
except Exception:
    pass


def _comment_count(db, task_id):
    try:
        from models import VideoTaskComment as _VTC
        return db.query(_VTC).filter(_VTC.task_id == task_id).count()
    except Exception:
        return 0


def comment_count_map(db, task_ids):
    """Comment counts for many tasks in ONE grouped query -> {task_id: count}.
    Pass the result as task_out(..., comment_count=...) to avoid the per-task COUNT (N+1)."""
    out = {}
    if not task_ids:
        return out
    try:
        from models import VideoTaskComment as _VTC
        from sqlalchemy import func as _f
        for tid, cnt in (db.query(_VTC.task_id, _f.count(_VTC.id))
                         .filter(_VTC.task_id.in_(list(task_ids)))
                         .group_by(_VTC.task_id)):
            out[tid] = cnt
    except Exception:
        pass
    return out


def editing_time_state(db, t):
    """Live active-editing time for a task.

    = accumulated closed-session seconds (t.editing_seconds)
      + the gap of the CURRENTLY running session (only when lifecycle == 'editing').
    So the number keeps growing live while an editor is editing, and freezes the
    moment they pause / complete / submit (session gets closed then).

    Returns (live_seconds:int, running:bool).
    """
    acc = int(getattr(t, "editing_seconds", 0) or 0)
    running = False
    if (t.lifecycle or "") == "editing":
        s = (db.query(EditingSession)
             .filter(EditingSession.task_id == t.id,
                     EditingSession.ended_at == None)          # noqa: E711
             .order_by(EditingSession.started_at.desc()).first())
        if s and s.started_at:
            running = True
            gap = int((datetime.utcnow() - s.started_at).total_seconds())
            if gap > 0:
                acc += gap
    return acc, running


def _prefs_out(t):
    import json as _j
    raw = (getattr(t, "proposal_refs", "") or "").strip()
    if raw.startswith("["):
        try:
            v = _j.loads(raw)
            if isinstance(v, list):
                return [x for x in v if x]
        except Exception:
            pass
    return []


def _pslides_out(t):
    import json as _j
    raw = (getattr(t, "proposal_slides", "") or "").strip()
    if raw.startswith("["):
        try:
            v = _j.loads(raw)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict) and x.get("url")]
        except Exception:
            pass
    s = getattr(t, "proposal_slide", "") or ""
    return [{"url": s, "name": getattr(t, "proposal_slide_name", "") or "slide"}] if s else []


def task_out(db, t, g=None, timeline=False, light=False, viewer=None, comment_count=None):
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
        "series_name": (getattr(t, "series_name", "") or ""),
        "streaming": t.streaming or "",
        "priority": t.priority or "normal",
        "is_old": bool(getattr(t, "is_old", False)),
        "status": (t.status or ""),
        "deadline_req_status": (getattr(t, "deadline_req_status", "") or ""),
        "quality_rating": getattr(t, "quality_rating", None),
        "quality_note": (getattr(t, "quality_note", "") or ""),
        "lifecycle": t.lifecycle or "",
        "lifecycle_label": lc_label(t.lifecycle),
        "legacy_status": t.status or "",
        "next_action": next_action(db, t, g),
        "deadline": _dt_raw(t.deadline),
        "proposal_slide": (getattr(t, "proposal_slide", "") or ""),
        "proposal_slide_name": (getattr(t, "proposal_slide_name", "") or ""),
        "proposal_refs": _prefs_out(t),
        "proposal_slides": _pslides_out(t),
        "proposal_media_note": (getattr(t, "proposal_media_note", "") or ""),
        "deadline_iso": (t.deadline.strftime("%Y-%m-%dT%H:%M:%S") if t.deadline else ""),  # LOCAL (IST), no Z — deadlines are already local; a Z made new Date() shift by the tz offset
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
        "reference_video": getattr(t, "reference_video", "") or "",
        "remarks": t.remarks or "",
        "comment_count": (comment_count if comment_count is not None else _comment_count(db, t.id)),
        "submitted_link": t.submitted_link or "",
        "created_at": _dt(t.created_at),
        # card thumbnail: graphics-made thumbnail first, else the one uploaded at assign time
        "thumbnail": (((g.thumbnail_url or "") if (g and (g.status or "") == "approved") else "") if g else (getattr(t, "thumbnail_b64", "") or (t.thumbnail_link or ""))),
        "graphics": {
            "id": (g.id if g else None),
            "graphics_id": (g.graphics_id if g else None),
            "graphics_name": (_name_for_staff(db, g.graphics_id) if g else ""),
            "status": (g.status if g else "new"),
            "thumbnail_url": (g.thumbnail_url if g else ""),
            "reference_image": (g.reference_image if g else ""),
            "reference_images": (_json_list(g.reference_images, g.reference_image) if g else []),
            "thumbnail_candidates": (_json_list(g.thumbnail_candidates) if g else []),
            "final_note": (getattr(g, "final_note", "") if g else ""),
            "instructions": (g.instructions if g else ""),
            "remarks": (g.remarks if g else ""),
            "quality_rating": (g.quality_rating if g else None),
            "drive_link": (getattr(g, "drive_link", "") if g else ""),
            "revision_count": (g.revision_count if g else 0),
        },
    }
    # live editing timer: seconds keep counting while lifecycle == 'editing'
    _live_secs, _editing_running = editing_time_state(db, t)
    out["live_editing_seconds"] = _live_secs
    out["editing_running"] = _editing_running
    if not light:
        out["thumbnail_link"] = t.thumbnail_link or ""
        out["deadline_iso"] = (t.deadline.strftime("%Y-%m-%dT%H:%M") if t.deadline else "")
        out["reference"] = t.reference or ""
        # §31 remarks audience: editors don't see PM-only remarks
        _aud = getattr(t, "remarks_audience", "both") or "both"
        if viewer == "editor" and _aud == "pm":
            out["remarks"] = ""
        else:
            out["remarks"] = t.remarks or ""
        out["remarks_audience"] = _aud
    if timeline:
        out["timeline"] = timeline_out(db, t)
        out["attachments"] = attachments_out(db, t)
        out["submissions"] = submissions_out(db, t)
        out["review_history"] = review_history_out(db, t)
    return out


def submissions_out(db, t):
    """Previous video submissions (append-only) from the teacher/youtuber, newest first.
    Reconstructed from the immutable timeline so nothing is ever overwritten."""
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id,
                    ProductionEvent.event.in_(["teacher_submitted", "youtuber_submitted"]))
            .order_by(ProductionEvent.created_at.desc(), ProductionEvent.id.desc()).all())
    out = []
    for e in rows:
        link = ""
        try:
            link = (json.loads(e.meta) if e.meta else {}).get("link", "")
        except Exception:
            link = ""
        out.append({"at": _dt(e.created_at), "by": e.actor_name or "",
                    "link": link, "event": e.event})
    # include the current link even if the event meta didn't carry it
    if t.submitted_link and (not out or out[0].get("link") != t.submitted_link):
        out.insert(0, {"at": _dt(t.submitted_at), "by": "", "link": t.submitted_link,
                       "event": "current"})
    return out


def edit_reviews_out(db, t):
    """PM QC decisions on the editor's work (changes / rejected / approved) with remarks."""
    rows = (db.query(TaskReview)
            .filter(TaskReview.task_id == t.id, TaskReview.kind == "edit")
            .order_by(TaskReview.created_at.desc(), TaskReview.id.desc()).all())
    _lbl = {"changes": "Changes Required", "rejected": "Rejected", "approved": "Approved",
            "submitted": "Submitted"}
    out = []
    for r in rows:
        out.append({"decision": _lbl.get(r.decision, r.decision or ""),
                    "remarks": r.remarks or "", "at": _dt(r.created_at),
                    "revision_no": r.revision_no or 0})
    return out


def edit_submissions_out(db, t):
    """Previous edited-video submissions (append-only) from the timeline, newest first."""
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id,
                    ProductionEvent.event.in_(["edited_video_submitted", "revision_submitted"]))
            .order_by(ProductionEvent.created_at.desc(), ProductionEvent.id.desc()).all())
    out = []
    for e in rows:
        link = ""
        try:
            link = (json.loads(e.meta) if e.meta else {}).get("link", "")
        except Exception:
            link = ""
        out.append({"at": _dt(e.created_at), "link": link,
                    "kind": ("Revision" if e.event == "revision_submitted" else "Submission")})
    if t.edited_link and (not out or out[0].get("link") != t.edited_link):
        out.insert(0, {"at": "", "link": t.edited_link, "kind": "Current"})
    return out


def progress_history_out(db, t):
    """Editing progress timeline: Assigned -> Started -> each % update, with timestamps.
    Reconstructed from the immutable ProductionEvent log (never overwritten)."""
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id,
                    ProductionEvent.event.in_(["editor_assigned", "editing_started",
                                               "editing_resumed", "editing_paused",
                                               "progress_updated", "editing_completed",
                                               "edited_video_submitted", "revision_submitted"]))
            .order_by(ProductionEvent.created_at.asc(), ProductionEvent.id.asc()).all())
    _lbl = {"editor_assigned": "Assigned", "editing_started": "Started",
            "editing_resumed": "Resumed", "editing_paused": "Paused",
            "editing_completed": "Editing Done", "edited_video_submitted": "Submitted",
            "revision_submitted": "Re-submitted"}
    out = []
    for e in rows:
        pct = None
        if e.event == "progress_updated":
            try:
                pct = (json.loads(e.meta) if e.meta else {}).get("progress")
            except Exception:
                pct = None
        label = _lbl.get(e.event, "") or ((str(pct) + "%") if pct is not None else e.event)
        out.append({"label": (str(pct) + "%") if pct is not None else label,
                    "progress": pct, "at": _dt(e.created_at)})
    return out


def review_history_out(db, t):
    """Previous PM review decisions on the creator's video (approve/changes/reshoot/reject)."""
    rows = (db.query(TaskReview)
            .filter(TaskReview.task_id == t.id, TaskReview.kind == "creator")
            .order_by(TaskReview.created_at.desc(), TaskReview.id.desc()).all())
    _lbl = {"changes": "Resubmit", "reshoot": "Reshoot", "rejected": "Rejected", "approved": "Approved"}
    out = []
    for r in rows:
        out.append({"decision": _lbl.get(r.decision, r.decision or ""),
                    "remarks": r.remarks or "", "at": _dt(r.created_at),
                    "revision_no": r.revision_no or 0})
    return out


def timeline_out(db, t):
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id)
            .order_by(ProductionEvent.created_at.asc(), ProductionEvent.id.asc()).all())
    merged = []
    for e in rows:
        _note = ""
        _pct = None
        try:
            import json as _jm
            _mm = _jm.loads(e.meta) if e.meta else {}
            if isinstance(_mm, dict):
                _note = _mm.get("note", "") or ""
                _pct = _mm.get("progress")
        except Exception:
            _note = ""
        _lbl = _event_label(e.event)
        if e.event == "progress_updated" and _pct is not None:
            _lbl = "Editing " + str(_pct) + "%"
        merged.append((e.created_at, {
            "event": e.event, "label": _lbl,
            "actor": e.actor_name or "", "role": e.actor_role or "",
            "prev": e.prev_state or "", "new": e.new_state or "",
            "note": _note,
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
            "note": h.get("note", "") or "", "at": (_dt(ts) if ts else raw),
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
    "deadline_requested": "Deadline Extension Requested",
    "deadline_extended": "Deadline Extended",
    "deadline_rejected": "Deadline Request Rejected",
    "quality_rated": "Quality Rated",
    "task_created": "Task Created",
    "creator_assigned": "Creator Assigned",
    "teacher_submitted": "Video Submitted",
    "youtuber_submitted": "Video Submitted",
    "approval_requested": "Sent for PM Approval",
    "approved": "Approved",
    "changes_requested": "Changes Requested",
    "rejected": "Rejected",
    "reshoot_required": "Reshoot Required",
    "editor_assigned": "Editor Assigned",
    "graphics_assigned": "Graphics Assigned",
    "thumbnail_assigned": "Thumbnail Assigned",
    "thumbnail_pending": "Thumbnail Pending",
    "editing_started": "Editing Started",
    "editing_paused": "Editing Paused",
    "editing_resumed": "Editing Resumed",
    "progress_updated": "Progress Updated",
    "editing_completed": "Editing Completed",
    "editor_submitted": "Editor Submitted",
    "qc_pending": "Editor Submitted",
    "ready_for_youtube": "Ready for YouTube",
    "completed": "Completed",
    "edited_video_submitted": "Edited Video Submitted",
    "thumbnail_started": "Thumbnail Started",
    "thumbnail_submitted": "Thumbnail Submitted",
    "thumbnail_approved": "Thumbnail Approved",
    "thumbnail_changes_requested": "Thumbnail Changes Requested",
    "thumbnail_rejected": "Thumbnail Rejected",
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
    """Human-readable deadline signal for cards/filters (spec §38). Canonical UTC stored;
    labels are plain English, never raw timer text."""
    if not t.deadline:
        return ("none", "No deadline")
    if t.lifecycle in ("uploaded", "completed"):
        return ("done", "Completed")
    now = datetime.utcnow()
    delta = (t.deadline - now).total_seconds()
    ad = abs(delta)
    d = int(ad // 86400); h = int((ad % 86400) // 3600); m = int((ad % 3600) // 60)
    if delta < 0:
        # overdue: hours for the first 2 days (e.g. "34h 12m overdue"), then days
        if ad < 172800:
            th = int(ad // 3600)
            s = "%dh %02dm overdue" % (th, m)
        else:
            s = "%dd %02dh overdue" % (d, h)
        return ("overdue", s)
    if delta < 7200:          # under 2 hours -> DUE SOON
        return ("soon", ("Due soon %dh %02dm" % (h, m)) if h else ("Due soon %dm" % m))
    if delta < 86400:         # under 24 hours -> DUE TODAY
        return ("today", "Due today %dh %02dm" % (h, m))
    return ("later", "Due in %dd %02dh" % (d, h))


# ---------------------------------------------------------------- announcements / events (§35)
_AUDIENCE_ROLE = {"teachers": "teacher", "editors": "editor", "graphics": "graphics",
                  "youtubers": "youtuber"}


def audience_user_ids(db, audience):
    """User ids for an announcement audience ('all' or a role group). Never raises."""
    try:
        from models import User, ProductionStaffProfile, TeacherProfile, YouTuberProfile
        ids = []
        aud = (audience or "all").lower()
        if aud in ("all", "teachers"):
            for u in db.query(User).filter(User.is_active == True, User.role == "teacher").all():
                ids.append(u.id)
        if aud in ("all", "editors", "graphics"):
            want = None if aud == "all" else _AUDIENCE_ROLE.get(aud)
            q = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.is_active == True)
            for sp in q.all():
                if want and (sp.staff_role or "") != want:
                    continue
                if sp.user_id:
                    ids.append(sp.user_id)
        if aud in ("all", "youtubers"):
            for yp in db.query(YouTuberProfile).all():
                if getattr(yp, "user_id", None):
                    ids.append(yp.user_id)
        return list(dict.fromkeys(ids))
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return []


def active_events_for(db, role):
    """Upcoming/active PM events visible to a given role, with a countdown label. Never raises."""
    try:
        from models import PmEvent
        role_aud = {"teacher": "teachers", "editor": "editors", "graphics": "graphics",
                    "youtuber": "youtubers"}.get(role, "")
        rows = (db.query(PmEvent).filter(PmEvent.active == True)
                .order_by(PmEvent.event_at.asc()).all())
        out = []
        now = datetime.utcnow()
        for e in rows:
            if e.audience not in ("all", role_aud):
                continue
            cd = ""
            if e.event_at:
                delta = (e.event_at - now).total_seconds()
                if delta < 0:
                    cd = "Happening now / passed"
                else:
                    d = int(delta // 86400); h = int((delta % 86400) // 3600); m = int((delta % 3600) // 60)
                    cd = ("in %dd %02dh" % (d, h)) if d else (("in %dh %02dm" % (h, m)) if h else ("in %dm" % m))
            out.append({"id": e.id, "title": e.title or "", "description": e.description or "",
                        "image_url": e.image_url or "", "at": _dt(e.event_at), "countdown": cd,
                        "audience": e.audience})
        return out
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return []


# ---------------------------------------------------------------- rank streak appreciation (§23)
def editor_rank_and_streak(db, sp):
    """Return this editor's current rank (1-based) among editors and lazily maintain a
    7-day #1 streak appreciation. Never raises — degrades to rank 0 on any error."""
    try:
        from models import ProductionStaffProfile, VideoTask
        now = datetime.utcnow()
        month_start = datetime(now.year, now.month, 1)
        done_states = ["ready_for_youtube", "uploaded", "completed"]
        eds = db.query(ProductionStaffProfile).filter(
            ProductionStaffProfile.staff_role == "editor",
            ProductionStaffProfile.is_active == True).all()
        scored = []
        for e in eds:
            cnt = db.query(VideoTask).filter(VideoTask.editor_id == e.id,
                                             VideoTask.lifecycle.in_(done_states),
                                             VideoTask.updated_at >= month_start).count()
            scored.append((e.id, cnt))
        scored.sort(key=lambda x: -x[1])
        rank = 0; my_cnt = 0
        for i, (eid, cnt) in enumerate(scored):
            if eid == sp.id:
                rank = i + 1; my_cnt = cnt
                break
        is_top = (rank == 1 and my_cnt > 0)
        if is_top:
            if not sp.rank1_since:
                sp.rank1_since = now
            elif (now - sp.rank1_since).days >= 7:
                already = sp.rank_appreciated_at and sp.rank_appreciated_at >= sp.rank1_since
                if not already:
                    notify(db, sp.user_id, "Top Performer!",
                           "You have stayed at Rank #1 among editors for 7 days straight. Outstanding consistency!",
                           "appreciation")
                    sp.rank_appreciated_at = now
        else:
            sp.rank1_since = None
        try:
            db.commit()
        except Exception:
            db.rollback()
        return rank
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0
