"""Production ecosystem — shared state engine & helpers.

One VideoTask row flows through the production lifecycle. Status is DERIVED from
real actions (never a free dropdown). Every important action writes an immutable
ProductionEvent. The legacy `status` field is kept loosely in sync so the existing
Task Manager UI does not break (see LEGACY_MAP).

Used by: production_routes, editor_routes, youtuber_routes, graphics_routes.
"""
from datetime import datetime, timezone, timedelta
import json
import re as _re
from fastapi import HTTPException

from models import (
    User, UserRole, VideoTask, GraphicsTask, EditingSession, ProductionEvent,
    TaskReview, TaskAttachment, YouTuberProfile, ProductionStaffProfile,
    TeacherProfile, Notification, VideoChannel, VideoType, ist_now,
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


# ---------------------------------------------------------------- presence
def touch_session(db, user, page=None, active=False):
    """Record/refresh a UserSession for ANY logged-in user (production team included).
    `active` = the client reported it is genuinely active (tab focused + recent interaction).
    "Live" is judged on last_active, so an open-but-backgrounded/idle tab (or an old client that
    doesn't report activity) no longer counts as online."""
    try:
        from models import UserSession
        now = datetime.now()
        role = getattr(getattr(user, "role", None), "value", str(getattr(user, "role", "")))
        pg = (str(page).strip()[:40] if page else None)
        s = (db.query(UserSession).filter(UserSession.user_id == user.id,
                                          UserSession.ended_at == None)
             .order_by(UserSession.last_seen.desc()).first())
        if s and s.last_seen and (now - s.last_seen) <= timedelta(minutes=3):
            # THROTTLE: agar abhi-abhi (<15s) update hui hai, aur page/active bhi wahi hai, to
            # dobara mat likho — multiple tabs ek hi session row ko concurrently na thokein
            # (yahi user_sessions par deadlock 1213 la raha tha). Presence best-effort hai.
            _fresh = (now - s.last_seen).total_seconds() < 15
            _same_page = (not pg) or (s.current_page == pg)
            _active_fresh = (not active) or (s.last_active and (now - s.last_active).total_seconds() < 15)
            if _fresh and _same_page and _active_fresh:
                return
            s.last_seen = now
            if pg:
                s.current_page = pg
            if active:
                s.last_active = now
        else:
            db.add(UserSession(user_id=user.id, role=role, started_at=now,
                               last_seen=now, last_active=(now if active else None),
                               current_page=pg))
        db.commit()
    except Exception:
        # deadlock / lock-wait / anything — presence write kabhi request na tode
        try:
            db.rollback()
        except Exception:
            pass


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


# ---- editor collab (2 editors on one urgent video) --------------------------
def collab_editor_ids(t):
    """ADDITIONAL editor ids (beyond the primary t.editor_id), as a clean int list."""
    raw = getattr(t, "collab_editor_ids", "") or ""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except Exception:
        v = []
    out = []
    for x in (v or []):
        try:
            xi = int(x)
        except Exception:
            continue
        if xi and xi not in out:
            out.append(xi)
    return out


def all_editor_ids(t):
    """Primary + collab editor ids (deduped, primary first)."""
    ids = []
    if getattr(t, "editor_id", None):
        ids.append(int(t.editor_id))
    for i in collab_editor_ids(t):
        if i not in ids:
            ids.append(i)
    return ids


def editor_can_access(t, editor_pid):
    """Is this editor the primary OR a collaborator on the task?"""
    try:
        editor_pid = int(editor_pid or 0)
    except Exception:
        return False
    return bool(editor_pid) and editor_pid in all_editor_ids(t)


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


def _dims_out(s):
    """Parse a JSON object of per-dimension sub-ratings -> {dim: int}. Safe on empty/garbage."""
    import json as _j
    try:
        v = _j.loads(s) if s else {}
        if isinstance(v, dict):
            out = {}
            for k, n in v.items():
                try:
                    n = int(n)
                    if 1 <= n <= 5:
                        out[str(k)] = n
                except Exception:
                    pass
            return out
    except Exception:
        pass
    return {}


def creator_info(db, t):
    """(name, type_label) for the task's creator."""
    if (t.creator_type or "teacher") == "youtuber":
        return _name_for_youtuber(db, t.youtuber_id), "YOUTUBER"
    return _name_for_teacher(db, t.teacher_id), "TEACHER"


# ---------------------------------------------------------------- approval
def submit_creator_link(db, t, link, actor_name="", actor_role=""):
    """MASTER submit: teacher/youtuber/PM/admin — koi bhi awaiting video ka drive link submit
    kar sakta hai. Attribution (role + naam) card pe sabko dikhta hai. Submit hote hi creator/
    PM/admin sabse option hat jaata hai (lifecycle aage badh jaata hai). on_time/delayed set."""
    from datetime import datetime, timezone, timedelta
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(tzinfo=None)
    t.submitted_link = (link or "").strip()
    t.submitted_at = now_ist
    try:
        t.submitted_by_role = actor_role or ""
        t.submitted_by_name = actor_name or ""
    except Exception:
        pass
    try:
        t.on_time = bool(t.deadline and now_ist <= t.deadline)
    except Exception:
        t.on_time = None
    # teacher legacy status bridge (taaki Task Manager me bhi 'submitted' dikhe)
    if (t.creator_type or "teacher") != "youtuber":
        try:
            t.status = "submitted"; t.reviewed = False
        except Exception:
            pass
    # lifecycle: approval on -> PM Review, warna seedha production (graphics start)
    try:
        if needs_pm_approval(db, t):
            set_state(db, t, "pm_review", actor=None, event="link_submitted", force=True)
        else:
            set_state(db, t, "approved", actor=None, event="link_submitted", force=True)
            graphics_task(db, t, create=True)
    except Exception:
        pass
    try:
        _who = ("%s (%s)" % (actor_name, actor_role.replace("production_manager", "PM").title())) if actor_name else actor_role
        notify_pms(db, "Video link submitted",
                   '"%s" ka drive link submit ho gaya%s — %s.'
                   % (t.title or "video", (" by " + _who) if _who else ""),
                   "production", link=str(t.id))
    except Exception:
        pass
    return t.on_time


def needs_pm_approval(db, t):
    """Per-video override wins; else the creator's default. Teachers always go via PM."""
    if t.approval_required is not None:
        return bool(t.approval_required)
    if (t.creator_type or "teacher") == "youtuber" and t.youtuber_id:
        yp = db.query(YouTuberProfile).filter(YouTuberProfile.id == t.youtuber_id).first()
        return bool(yp.approval_required) if yp else True
    return True   # teacher submissions default to PM review


# ------------------------------------------------- attendance / work detection
def ist_day_bounds_utc(day_str=None):
    """(start_utc, end_utc, 'YYYY-MM-DD') for an IST calendar day (default: today IST).
    DB stores UTC, so callers compare timestamps against these UTC bounds."""
    IST = timedelta(hours=5, minutes=30)
    try:
        anchor = datetime.fromisoformat(day_str) if day_str else (datetime.utcnow() + IST)
    except Exception:
        anchor = datetime.utcnow() + IST
    anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    s = anchor - IST
    e = anchor + timedelta(days=1) - IST
    return s, e, anchor.strftime("%Y-%m-%d")


def editor_worked_today(db, sp, s=None, e=None):
    """True if this editor did REAL work in the IST window [s,e): an editing session,
    a VideoTask currently editing/paused or finished in the window, OR a project video
    (chapter) they are editing / paused / started / finished in the window.
    PM/admin actions are NOT counted — this is the editor's own-portal activity."""
    from sqlalchemy import or_ as _or, and_ as _and
    from models import VideoTaskChapter as _VC
    if s is None or e is None:
        s, e, _ = ist_day_bounds_utc()
    eid = sp.id
    if db.query(EditingSession.id).filter(
            EditingSession.editor_id == eid,
            EditingSession.started_at >= s, EditingSession.started_at < e).first():
        return True
    if db.query(VideoTask.id).filter(
            VideoTask.cancelled.isnot(True),
            _or(VideoTask.editor_id == eid, VideoTask.collab_editor_ids.like("%" + str(eid) + "%")),
            _or(VideoTask.lifecycle.in_(["editing", "editing_paused"]),
                _and(VideoTask.editing_done_at != None,  # noqa: E711
                     VideoTask.editing_done_at >= s, VideoTask.editing_done_at < e))).first():
        return True
    if db.query(_VC.id).filter(
            _VC.editor_id == eid,
            _or(_VC.edit_state.in_(["editing", "paused"]),
                _and(_VC.editing_started_at != None,  # noqa: E711
                     _VC.editing_started_at >= s, _VC.editing_started_at < e),
                _and(_VC.edited_at != None,  # noqa: E711
                     _VC.edited_at >= s, _VC.edited_at < e))).first():
        return True
    return False


def graphics_worked_today(db, sp, s=None, e=None):
    """True if this designer SUBMITTED a thumbnail from their OWN portal in [s,e).
    A PM/admin crediting a pre-made thumbnail does NOT count (that logs a credit event,
    not 'thumbnail_submitted'), so an absent designer stays a leave candidate."""
    if s is None or e is None:
        s, e, _ = ist_day_bounds_utc()
    uid = getattr(sp, "user_id", None)
    base = db.query(ProductionEvent.id).filter(
        ProductionEvent.event == "thumbnail_submitted",
        ProductionEvent.created_at >= s, ProductionEvent.created_at < e)
    if uid and base.filter(ProductionEvent.actor_user_id == uid).first():
        return True
    nm = (sp.user.name if getattr(sp, "user", None) else "")
    if nm and base.filter(ProductionEvent.actor_name == nm).first():
        return True
    return False


def staff_worked_today(db, sp, s=None, e=None):
    """Role-aware own-portal work check for a production staff member (editor/graphics)."""
    role = getattr(sp, "staff_role", "")
    if role == "graphics":
        return graphics_worked_today(db, sp, s, e)
    return editor_worked_today(db, sp, s, e)


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


# ============================================================ TEACHER RECORDING FEEDBACK (Editor -> Teacher)
# A one-directional, constructive channel: the Editor rates the ORIGINAL recording the Teacher made.
# Captured atomically at edited-video submission. Kept entirely separate from PM Editor rating,
# Teacher edit review and Graphics rating; it NEVER affects editor performance scores.
REC_FB_CRITERIA = ["fluency", "introduction", "audio", "presentation", "visual", "editing_readiness"]
_REC_FB_COL = {"fluency": "fluency_rating", "introduction": "introduction_rating", "audio": "audio_rating",
               "presentation": "presentation_rating", "visual": "visual_rating",
               "editing_readiness": "editing_readiness_rating"}
REC_FB_CRIT_LABELS = {"fluency": "Fluency & Repetition", "introduction": "Video Introduction",
                      "audio": "Audio Clarity", "presentation": "Presentation & Flow",
                      "visual": "Visual & Recording Quality", "editing_readiness": "Editing Readiness"}
REC_FB_TAGS = ["excessive_fumbling", "intro_needs_improvement", "long_pauses_retakes", "audio_mic_issues",
               "background_noise", "screen_board_visibility", "poor_camera_framing", "unnecessary_length",
               "frequent_interruptions", "inconsistent_volume", "topic_transitions", "other", "no_major_issues"]
_REC_FB_TAG_LABELS = {
    "excessive_fumbling": "Excessive Fumbling / Repetition", "intro_needs_improvement": "Intro Needs Improvement",
    "long_pauses_retakes": "Long Pauses / Retakes", "audio_mic_issues": "Audio / Microphone Issues",
    "background_noise": "Background Noise", "screen_board_visibility": "Screen / Board Visibility",
    "poor_camera_framing": "Poor Camera Framing", "unnecessary_length": "Unnecessary Recording Length",
    "frequent_interruptions": "Frequent Recording Interruptions", "inconsistent_volume": "Inconsistent Voice Volume",
    "topic_transitions": "Topic Transitions Need Improvement", "other": "Other", "no_major_issues": "No Major Issues"}
_REC_FB_PLACEHOLDERS = {"na", "n/a", "none", "nil", "ok", "okay", "good", "nice", "fine", "great", "bad", "test",
                        "testing", "asdf", "qwerty", "aaa", "xxx", "---", "...", ".", "..", "no comment", "nothing"}
_REC_FB_TS_RE = _re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def rec_fb_applicable(t):
    """Recording feedback only applies when a TEACHER made the recording (not youtuber-created)."""
    if not t:
        return False
    if (getattr(t, "creator_type", "") or "teacher") == "youtuber":
        return False
    return bool(getattr(t, "teacher_id", None))


def _rec_fb_parse_ratings(payload):
    raw = (payload.get("ratings") or {}) if isinstance(payload.get("ratings"), dict) else {}
    na = (payload.get("na_reasons") or {}) if isinstance(payload.get("na_reasons"), dict) else {}
    out, na_out = {}, {}
    for crit in REC_FB_CRITERIA:
        v = raw.get(crit, None)
        lbl = REC_FB_CRIT_LABELS.get(crit, crit)
        if v in (None, "", "na", "NA", "N/A", "n/a"):
            reason = (na.get(crit) or "").strip()
            if not reason:
                raise HTTPException(400, f"Rate '{lbl}' 1-5, or mark it N/A with a short reason.")
            out[crit] = None
            na_out[crit] = reason[:200]
        else:
            try:
                iv = int(v)
            except Exception:
                raise HTTPException(400, f"Invalid rating for '{lbl}'.")
            if iv < 1 or iv > 5:
                raise HTTPException(400, f"Rating for '{lbl}' must be 1-5 (or N/A).")
            out[crit] = iv
    return out, na_out


def _rec_fb_valid_remark(remarks):
    s = (remarks or "").strip()
    if len(s) < 15:
        raise HTTPException(400, "Write a constructive improvement remark (at least 15 characters).")
    if len(s) > 1200:
        raise HTTPException(400, "Remark is too long (max 1200 characters).")
    low = s.lower()
    if low in _REC_FB_PLACEHOLDERS:
        raise HTTPException(400, "Please write a meaningful, actionable remark — not placeholder text.")
    compact = "".join(low.split())
    if len(set(compact)) < 5:
        raise HTTPException(400, "Please write a meaningful remark explaining what to improve next time.")
    words = [w for w in _re.split(r"\s+", s) if w]
    if len(words) < 3:
        raise HTTPException(400, "Please write a fuller remark (a few words at least).")
    return s[:1200]


def _rec_fb_tags(payload):
    tags = payload.get("issue_tags") or []
    if not isinstance(tags, list):
        tags = []
    seen, clean = set(), []
    for t in tags:
        t = str(t)
        if t in REC_FB_TAGS and t not in seen:
            seen.add(t)
            clean.append(t)
    if "no_major_issues" in clean and len(clean) > 1:
        raise HTTPException(400, "'No Major Issues' can't be combined with other issue tags.")
    return clean


def _rec_fb_notes(payload):
    notes = payload.get("timestamped_notes") or []
    if not isinstance(notes, list):
        return []
    out = []
    for n in notes[:30]:
        if not isinstance(n, dict):
            continue
        ts = (str(n.get("ts") or n.get("timestamp") or "")).strip()
        note = (str(n.get("note") or "")).strip()
        cat = (str(n.get("category") or "")).strip()
        if not ts and not note:
            continue
        if ts and not _REC_FB_TS_RE.match(ts):
            raise HTTPException(400, f"Invalid timestamp '{ts}' — use mm:ss or hh:mm:ss.")
        out.append({"ts": ts[:9], "category": cat[:40], "note": note[:300]})
    return out


def _rec_fb_overall(ratings):
    vals = [v for v in ratings.values() if isinstance(v, int)]
    if not vals:
        return None
    return round(sum(vals) / float(len(vals)), 2)


def _rec_fb_sig(ratings, na, tags, remarks, notes):
    return json.dumps({"r": ratings, "na": na, "t": sorted(tags),
                       "rem": (remarks or "").strip(), "n": notes}, sort_keys=True)


def validate_recording_feedback(payload):
    """Run all validators WITHOUT touching the DB — call this before mutating submission state so a
    bad feedback payload rolls the whole submit back (editing is never marked complete on failure).
    Raises HTTPException(400) on invalid input."""
    if not isinstance(payload, dict):
        raise HTTPException(400, "Recording feedback is required to submit the edited video.")
    _rec_fb_parse_ratings(payload)
    _rec_fb_valid_remark(payload.get("remarks"))
    _rec_fb_tags(payload)
    _rec_fb_notes(payload)
    return True


def save_recording_feedback(db, work_type, video_task_id, chapter_id, editor_id, teacher_id, revision, payload):
    """Validate + upsert the ACTIVE recording feedback for a work-item, preserving history.
    Raises HTTPException(400) on invalid input (so the whole submit transaction rolls back)."""
    from models import TeacherRecordingFeedback as _FB
    ratings, na = _rec_fb_parse_ratings(payload)
    remarks = _rec_fb_valid_remark(payload.get("remarks"))
    tags = _rec_fb_tags(payload)
    notes = _rec_fb_notes(payload)
    overall = _rec_fb_overall(ratings)
    sig = _rec_fb_sig(ratings, na, tags, remarks, notes)
    q = db.query(_FB).filter(_FB.is_active == True)
    if work_type == "chapter":
        q = q.filter(_FB.chapter_id == chapter_id)
    else:
        q = q.filter(_FB.video_task_id == video_task_id, _FB.chapter_id == None)
    cur = q.first()
    if cur is not None:
        cur_sig = _rec_fb_sig(
            {c: getattr(cur, _REC_FB_COL[c]) for c in REC_FB_CRITERIA},
            json.loads(cur.na_reasons_json or "{}"),
            json.loads(cur.issue_tags_json or "[]"),
            cur.remarks or "",
            json.loads(cur.timestamped_notes_json or "[]"))
        if cur_sig == sig:
            cur.submission_revision = revision  # unchanged on resubmit -> no new version/history
            db.flush()
            return cur
        cur.is_active = False  # changed -> archive old version, keep history
        db.flush()
    row = _FB(work_type=work_type, video_task_id=video_task_id, chapter_id=chapter_id,
              editor_id=editor_id, teacher_id=teacher_id, submission_revision=revision,
              na_reasons_json=json.dumps(na), overall_rating=overall,
              issue_tags_json=json.dumps(tags), remarks=remarks,
              timestamped_notes_json=json.dumps(notes), is_active=True)
    for c in REC_FB_CRITERIA:
        setattr(row, _REC_FB_COL[c], ratings[c])
    db.add(row)
    db.flush()
    return row


def get_active_recording_feedback(db, video_task_id=None, chapter_id=None):
    from models import TeacherRecordingFeedback as _FB
    q = db.query(_FB).filter(_FB.is_active == True)
    if chapter_id:
        q = q.filter(_FB.chapter_id == chapter_id)
    elif video_task_id:
        q = q.filter(_FB.video_task_id == video_task_id, _FB.chapter_id == None)
    else:
        return None
    return q.order_by(_FB.id.desc()).first()


def recording_feedback_out(db, row):
    if row is None:
        return None
    def _staffn(sid):
        try:
            sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == sid).first()
            if sp:
                u = db.query(User).filter(User.id == sp.user_id).first()
                return (u.name if u else "") or ""
        except Exception:
            pass
        return ""
    ratings = {c: getattr(row, _REC_FB_COL[c]) for c in REC_FB_CRITERIA}
    tags = json.loads(row.issue_tags_json or "[]")
    return {
        "id": row.id, "work_type": row.work_type,
        "video_task_id": row.video_task_id, "chapter_id": row.chapter_id,
        "editor_id": row.editor_id, "editor_name": _staffn(row.editor_id),
        "teacher_id": row.teacher_id, "revision": row.submission_revision,
        "criteria_labels": REC_FB_CRIT_LABELS,
        "ratings": ratings, "na_reasons": json.loads(row.na_reasons_json or "{}"),
        "overall": row.overall_rating,
        "issue_tags": tags, "issue_tag_labels": [_REC_FB_TAG_LABELS.get(t, t) for t in tags],
        "remarks": row.remarks or "",
        "timestamped_notes": json.loads(row.timestamped_notes_json or "[]"),
        "submitted_at": _dt(row.created_at), "updated_at": _dt(row.updated_at),
        "acknowledged": bool(row.acknowledged_at), "acknowledged_at": _dt(row.acknowledged_at),
    }


def recording_feedback_summary(db, row):
    """Compact card summary (overall + #improvement areas + ack state). None when no feedback exists
    (so legacy videos show "no feedback" rather than a fake zero-star rating)."""
    if row is None:
        return None
    tags = json.loads(row.issue_tags_json or "[]")
    rvals = [getattr(row, _REC_FB_COL[c]) for c in REC_FB_CRITERIA]
    low = sum(1 for v in rvals if isinstance(v, int) and v <= 2)
    areas = len([t for t in tags if t != "no_major_issues"])
    return {"has": True, "overall": row.overall_rating,
            "improvement_areas": max(areas, low),
            "acknowledged": bool(row.acknowledged_at)}


# ============================================================ CREATIVE EDITING BRIEF (source submitter -> Editor)
# OPTIONAL editing ideas attached at source-video submission. Never blocks submission. Distinct from
# the PM's Editor Assignment Brief (VideoTask.editor_instructions).
CREATIVE_STYLE_TAGS = ["clean_transitions", "minimal_animation", "formula_highlight", "short_intro",
                       "chapter_name_first", "captions", "subtle_bgm", "fast_paced", "color_grade",
                       "remove_silences", "zoom_emphasis", "lower_thirds"]
_CREATIVE_STYLE_LABELS = {
    "clean_transitions": "Clean transitions", "minimal_animation": "Minimal animation",
    "formula_highlight": "Highlight formulas (zoom)", "short_intro": "Keep intro short",
    "chapter_name_first": "Show chapter name first", "captions": "Add captions",
    "subtle_bgm": "Subtle background music", "fast_paced": "Fast paced",
    "color_grade": "Colour grading", "remove_silences": "Remove long silences",
    "zoom_emphasis": "Zoom to emphasise", "lower_thirds": "Lower-thirds / labels"}


def _url_safe(u):
    """Only http/https external links allowed (no javascript:, data:, file:, etc.)."""
    u = (u or "").strip()
    if not u:
        return ""
    if not _re.match(r"^https?://", u, _re.I):
        return ""
    if len(u) > 800:
        u = u[:800]
    return u


def _creative_tags(payload):
    tags = payload.get("editing_style_tags") or payload.get("style_tags") or []
    if not isinstance(tags, list):
        tags = []
    seen, out = set(), []
    for t in tags:
        t = str(t)
        if t in CREATIVE_STYLE_TAGS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _creative_links(payload):
    raw = payload.get("reference_video_links") or payload.get("reference_links") or []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:12]:
        if isinstance(item, str):
            url = _url_safe(item)
            if url:
                out.append({"url": url, "title": "", "note": ""})
        elif isinstance(item, dict):
            url = _url_safe(item.get("url") or "")
            if not url:
                continue
            out.append({"url": url, "title": (str(item.get("title") or "")).strip()[:160],
                        "note": (str(item.get("note") or "")).strip()[:400]})
    return out


def _creative_timestamps(payload):
    raw = payload.get("timestamped_instructions") or payload.get("timestamped_notes") or []
    if not isinstance(raw, list):
        return []
    out = []
    for n in raw[:40]:
        if not isinstance(n, dict):
            continue
        ts = (str(n.get("ts") or n.get("timestamp") or "")).strip()
        note = (str(n.get("note") or n.get("instruction") or "")).strip()
        if not ts and not note:
            continue
        if ts and not _REC_FB_TS_RE.match(ts):
            raise HTTPException(400, f"Invalid timestamp '{ts}' — use mm:ss or hh:mm:ss.")
        out.append({"ts": ts[:9], "note": note[:400]})
    return out


def _creative_images(db, t, payload):
    """Accept a mix of already-stored URLs and new base64 data-URIs; return the final URL list.
    New images go through the existing R2 save_images (kind 'brief'); never store base64 in the row."""
    raw = payload.get("reference_images") or []
    if not isinstance(raw, list):
        return []
    urls, to_upload = [], []
    for img in raw[:12]:
        s = img if isinstance(img, str) else (img.get("url") if isinstance(img, dict) else "")
        s = s or ""
        if s.startswith("http"):
            urls.append(s)
        elif s.startswith("data:") or len(s) > 200:
            to_upload.append(s)
    if to_upload and t is not None:
        try:
            new_urls = save_images(db, t, to_upload, "brief", None, None, return_urls=True) or []
            urls.extend([u for u in new_urls if u])
        except Exception:
            pass
    # cap total and keep only http(s)/data uris
    return [u for u in urls if (str(u).startswith("http") or str(u).startswith("data:"))][:12]


def creative_brief_is_empty(payload):
    """A brief with no content at all -> treat as 'no brief' (skip silently, never a fake brief)."""
    if not isinstance(payload, dict):
        return True
    if (payload.get("instructions") or "").strip():
        return False
    for key in ("editing_style_tags", "style_tags", "reference_images",
                "reference_video_links", "reference_links", "timestamped_instructions"):
        v = payload.get(key)
        if isinstance(v, list) and len(v) > 0:
            return False
    return True


def save_creative_brief(db, work_type, video_task_id, chapter_id, author_user, author_role,
                        payload, editor_started=False):
    """Validate + upsert the ACTIVE creative brief for a work-item. Returns (row, changed_bool).
    Returns (None, False) when the payload is empty (no brief). Raises HTTPException(400) only on
    genuinely invalid structured data (e.g. a bad timestamp) — it never blocks on an empty brief."""
    from models import CreativeEditingBrief as _CB
    if creative_brief_is_empty(payload):
        return None, False
    t = db.query(VideoTask).filter(VideoTask.id == (video_task_id if work_type != "project_chapter" else video_task_id)).first()
    instructions = (payload.get("instructions") or "").strip()[:4000]
    tags = _creative_tags(payload)
    links = _creative_links(payload)
    stamps = _creative_timestamps(payload)
    images = _creative_images(db, t, payload)
    sig = json.dumps({"i": instructions, "t": sorted(tags), "l": links, "s": stamps, "im": images}, sort_keys=True)
    q = db.query(_CB).filter(_CB.is_active == True)
    if work_type == "project_chapter":
        q = q.filter(_CB.chapter_id == chapter_id)
    else:
        q = q.filter(_CB.video_task_id == video_task_id, _CB.chapter_id == None)
    cur = q.first()
    author_name = (getattr(author_user, "name", "") or "") if author_user else ""
    if cur is not None:
        cur_sig = json.dumps({"i": cur.instructions or "", "t": sorted(json.loads(cur.editing_style_tags_json or "[]")),
                              "l": json.loads(cur.reference_video_links_json or "[]"),
                              "s": json.loads(cur.timestamped_instructions_json or "[]"),
                              "im": json.loads(cur.reference_images_json or "[]")}, sort_keys=True)
        if cur_sig == sig:
            return cur, False   # unchanged -> no new version
        if editor_started:
            # editor already started -> keep the old version for accountability, create a new one
            cur.is_active = False
            db.flush()
            row = _CB(work_type=work_type, video_task_id=video_task_id, chapter_id=chapter_id,
                      submitted_by_user_id=getattr(author_user, "id", None), submitted_by_role=author_role,
                      submitted_by_name=author_name, instructions=instructions,
                      editing_style_tags_json=json.dumps(tags), reference_images_json=json.dumps(images),
                      reference_video_links_json=json.dumps(links),
                      timestamped_instructions_json=json.dumps(stamps),
                      version=(cur.version or 1) + 1, is_active=True)
            db.add(row); db.flush()
            return row, True
        # before editor starts -> update in place
        cur.instructions = instructions
        cur.editing_style_tags_json = json.dumps(tags)
        cur.reference_images_json = json.dumps(images)
        cur.reference_video_links_json = json.dumps(links)
        cur.timestamped_instructions_json = json.dumps(stamps)
        cur.submitted_by_user_id = getattr(author_user, "id", None) or cur.submitted_by_user_id
        cur.submitted_by_role = author_role or cur.submitted_by_role
        cur.submitted_by_name = author_name or cur.submitted_by_name
        db.flush()
        return cur, True
    row = _CB(work_type=work_type, video_task_id=video_task_id, chapter_id=chapter_id,
              submitted_by_user_id=getattr(author_user, "id", None), submitted_by_role=author_role,
              submitted_by_name=author_name, instructions=instructions,
              editing_style_tags_json=json.dumps(tags), reference_images_json=json.dumps(images),
              reference_video_links_json=json.dumps(links),
              timestamped_instructions_json=json.dumps(stamps), version=1, is_active=True)
    db.add(row); db.flush()
    return row, True


def get_active_creative_brief(db, video_task_id=None, chapter_id=None):
    from models import CreativeEditingBrief as _CB
    q = db.query(_CB).filter(_CB.is_active == True)
    if chapter_id:
        q = q.filter(_CB.chapter_id == chapter_id)
    elif video_task_id:
        q = q.filter(_CB.video_task_id == video_task_id, _CB.chapter_id == None)
    else:
        return None
    return q.order_by(_CB.id.desc()).first()


def creative_brief_out(db, row):
    if row is None:
        return None
    tags = json.loads(row.editing_style_tags_json or "[]")
    return {
        "id": row.id, "work_type": row.work_type,
        "video_task_id": row.video_task_id, "chapter_id": row.chapter_id,
        "author": row.submitted_by_name or "", "author_role": row.submitted_by_role or "",
        "instructions": row.instructions or "",
        "style_tags": tags, "style_tag_labels": [_CREATIVE_STYLE_LABELS.get(t, t) for t in tags],
        "reference_images": json.loads(row.reference_images_json or "[]"),
        "reference_links": json.loads(row.reference_video_links_json or "[]"),
        "timestamped_instructions": json.loads(row.timestamped_instructions_json or "[]"),
        "version": row.version or 1,
        "submitted_at": _dt(row.created_at), "updated_at": _dt(row.updated_at),
    }


def creative_brief_summary(db, row):
    """Compact card summary (counts). None when no brief -> card shows 'No Creative Brief Added'."""
    if row is None:
        return None
    imgs = len(json.loads(row.reference_images_json or "[]"))
    links = len(json.loads(row.reference_video_links_json or "[]"))
    stamps = len(json.loads(row.timestamped_instructions_json or "[]"))
    has_instr = bool((row.instructions or "").strip())
    tags = len(json.loads(row.editing_style_tags_json or "[]"))
    return {"has": True, "images": imgs, "videos": links, "timestamps": stamps,
            "instructions": has_instr, "style_tags": tags, "version": row.version or 1,
            "author": row.submitted_by_name or "", "author_role": row.submitted_by_role or ""}


def creative_editor_started_task(t):
    return (getattr(t, "lifecycle", "") or "") in (
        "editing", "editing_paused", "editing_done", "qc_pending", "qc_changes",
        "ready_for_youtube", "uploaded", "completed")


def creative_editor_started_chapter(c):
    return (getattr(c, "edit_state", "") or "") in ("editing", "paused", "edited") or \
           (getattr(c, "qc_status", "") or "") in ("pending", "approved", "changes")


def _editor_user_id_for(db, editor_staff_id):
    if not editor_staff_id:
        return None
    try:
        sp = db.query(ProductionStaffProfile).filter(ProductionStaffProfile.id == editor_staff_id).first()
        return sp.user_id if sp else None
    except Exception:
        return None


def notify_brief_stakeholders(db, work_type, video_task_id, chapter_id, editor_staff_id, author_name, title, is_update=False):
    """When a TEACHER attaches/updates a creative brief, let everyone who needs to know see it:
    the assigned editor (if any), the admins, and the PMs. Notification deep-links to the brief."""
    link = ("%d:%d" % (video_task_id, chapter_id)) if work_type == "project_chapter" else str(video_task_id)
    ttl = "Creative Brief Updated" if is_update else "Creative Brief Added"
    msg = f'{author_name or "The teacher"} {"updated" if is_update else "added"} an editing brief for "{title}".'
    try:
        euid = _editor_user_id_for(db, editor_staff_id)
        if euid:
            notify(db, euid, ttl, msg, "creative_brief", link=link)
    except Exception:
        pass
    try:
        for a in db.query(User).filter(User.role == "admin", User.is_active == True).all():
            notify(db, a.id, ttl, msg, "creative_brief", link=link)
    except Exception:
        pass
    try:
        notify_pms(db, ttl, msg, "creative_brief", link=link)
    except Exception:
        pass


def attach_creative_brief(db, work_type, video_task_id, chapter_id, author_user, author_role,
                          payload, task_for_timeline=None, editor_started=False,
                          editor_staff_id=None, title=""):
    """Save (optional) brief + write a timeline event + notify the assigned editor on a post-start
    change. Returns (row, changed). (None, False) when there's no brief content."""
    row, changed = save_creative_brief(db, work_type, video_task_id, chapter_id, author_user,
                                       author_role, payload, editor_started=editor_started)
    if row is None:
        return None, False
    try:
        if task_for_timeline is not None:
            ev = "creative_brief_updated" if (changed and (row.version or 1) > 1) else "creative_brief_added"
            log_event(db, task_for_timeline, author_user, ev,
                      meta={"version": row.version, "work": work_type, "chapter_id": chapter_id})
    except Exception:
        pass
    try:
        if changed and (row.version or 1) > 1:
            euid = _editor_user_id_for(db, editor_staff_id)
            if euid:
                notify(db, euid, "Creative Brief Updated",
                       f'The creative editing brief for "{title}" was updated — please re-check.',
                       "creative_brief",
                       link=(("%d:%d" % (video_task_id, chapter_id)) if work_type == "project_chapter" else str(video_task_id)))
    except Exception:
        pass
    return row, changed


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
        # per-role editor fields (Assign Work "Editor" section) + editor collab
        "ALTER TABLE video_tasks ADD COLUMN editor_deadline DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN editor_instructions TEXT",
        "ALTER TABLE video_tasks ADD COLUMN editor_reference TEXT",
        "ALTER TABLE video_tasks ADD COLUMN collab_editor_ids TEXT",
        "ALTER TABLE video_tasks ADD COLUMN upload_date DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN upload_remarks TEXT",
        # ---- performance engine (perf §4/§36/§41) ----
        "ALTER TABLE video_tasks ADD COLUMN content_format VARCHAR(12)",
        "ALTER TABLE production_staff_profiles ADD COLUMN editor_specialization VARCHAR(12)",
        "ALTER TABLE production_staff_profiles ADD COLUMN target_long INTEGER",
        "ALTER TABLE production_staff_profiles ADD COLUMN target_short INTEGER",
        "ALTER TABLE production_staff_profiles ADD COLUMN target_thumbnails INTEGER",
        # project-chapter graphics tracking (perf §9) — backward compatible, nullable
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_approved_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_revision INTEGER DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_quality INTEGER",
        # auto-notify students on publish (idempotent flag)
        "ALTER TABLE video_tasks ADD COLUMN students_notified BOOLEAN DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN students_notified BOOLEAN DEFAULT 0",
        # ---- project-chapter FIRST-CLASS parity (additive; backward compatible) ----
        "ALTER TABLE video_task_chapters ADD COLUMN submitted_by_role VARCHAR(30)",
        "ALTER TABLE video_task_chapters ADD COLUMN submitted_by_name VARCHAR(160)",
        "ALTER TABLE video_task_chapters ADD COLUMN on_time BOOLEAN",
        "ALTER TABLE video_task_chapters ADD COLUMN reject_count INTEGER DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN revision_count INTEGER DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN no_resubmit BOOLEAN DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN editor_deadline DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN editor_instructions TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN editor_reference TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN priority VARCHAR(10)",
        "ALTER TABLE video_task_chapters ADD COLUMN quality_rating INTEGER",
        "ALTER TABLE video_task_chapters ADD COLUMN quality_note VARCHAR(400)",
        "ALTER TABLE video_task_chapters ADD COLUMN quality_dims TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN editor_credited_by VARCHAR(160)",
        "ALTER TABLE video_task_chapters ADD COLUMN edited_direct BOOLEAN DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_instructions TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_candidates TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_candidate_history TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_quality_note VARCHAR(400)",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_started_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_submitted_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_credited_by VARCHAR(160)",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_direct BOOLEAN DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN reconciled BOOLEAN DEFAULT 0",
        "ALTER TABLE video_task_chapters ADD COLUMN reconciled_by VARCHAR(160)",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_change_note VARCHAR(600)",
        "ALTER TABLE video_task_chapters ADD COLUMN graphics_deadline DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN editor_assigned_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN graphics_assigned_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN thumb_quality_dims TEXT",
        "ALTER TABLE graphics_tasks ADD COLUMN quality_dims TEXT",
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
    # ensure the rank-snapshot table exists (perf §21 — persistent rank history)
    try:
        from models import ProductionRankSnapshot
        ProductionRankSnapshot.__table__.create(bind=engine, checkfirst=True)
    except Exception:
        pass


# ============================================================================
# CANONICAL PRODUCTION-TASKS FILTER CONTRACT
# ----------------------------------------------------------------------------
# ONE place that defines how each Production → Tasks UI filter maps to the DB.
# The GET /api/production/tasks endpoint (and nothing else) builds its WHERE out
# of these helpers. Rules the whole filter obeys:
#   • IDs over mutable names (teacher/editor/graphics/channel/video_type).
#   • id 1 must NEVER match id 11 (boundary-safe collaborator matching).
#   • case / whitespace normalised for legacy string columns.
#   • status maps to BOTH the new `lifecycle` and the legacy admin `status`.
#   • every helper returns a SQLAlchemy clause (or None) — pure, read-only.
# ============================================================================

# status (UI dropdown value) -> (lifecycle values, legacy admin status values).
# OR-combined so legacy/admin tasks (lifecycle blank, state in `status`) and new
# production tasks both match the same UI option. THE ONLY COPY of this mapping.
STATUS_LIFECYCLE_MAP = {
    "assigned":          (["creator_assigned", "creator_working", "changes_required"], ["assigned", "reshoot", "rejected", "new", "in_progress"]),
    "pm_review":         (["pm_review", "creator_submitted"],                          ["submitted"]),
    "approved":          (["approved"],                                               ["approved"]),
    "editor_assigned":   (["editor_assigned"],                                        ["editing_soon"]),
    "editing":           (["editing", "editing_paused"],                              []),
    "editing_done":      (["editing_done"],                                           ["editing_done"]),
    "qc_pending":        (["qc_pending"],                                             []),
    "ready_for_youtube": (["ready_for_youtube"],                                      []),
    "uploaded":          (["uploaded", "completed"],                                  ["uploaded"]),
    "changes_required":  (["changes_required", "qc_changes"],                         ["reshoot", "rejected"]),
}

# Statuses that mean the task is finished — the default Tasks view hides these, so the
# status dropdown's "all" option is labelled "All Active", not "All Status".
FINAL_STATUS_KEYS = ("uploaded",)


def stage_filter(status):
    """UI status value -> precise SQLAlchemy clause, or None for blank/all. Single source of
    truth for the status↔lifecycle mapping.

    The production pipeline is a strict ladder; each filter shows ONLY its own rung, never a
    later one leaking in through a stale legacy ``status`` column:
      approved          -> PM-approved, NOT yet assigned to an editor (editor_id is NULL).
      editor_assigned   -> assigned to an editor, editing NOT started yet (queue).
      editing           -> editor is actively editing (editing / paused / marked-done-not-submitted).
      qc_pending        -> editor submitted the edited video; waiting for PM QC — nothing else.
      ready_for_youtube -> PM QC-approved.
      uploaded          -> published. changes_required -> any changes requested.
    """
    from sqlalchemy import or_ as _or, and_ as _and
    s = (status or "").strip()
    if not s or s == "all":
        return None
    _blank = _or(VideoTask.lifecycle == None, VideoTask.lifecycle == "")      # noqa: E711

    if s == "approved":
        # teacher shot + PM approved, editor NOT assigned yet
        return _and(
            _or(VideoTask.lifecycle == "approved",
                _and(_blank, VideoTask.status == "approved")),
            VideoTask.editor_id == None)                                      # noqa: E711
    if s == "editor_assigned":
        # assigned to editor, editing NOT started (legacy match only when lifecycle is blank,
        # so a task that has really moved to 'editing' can never leak in via a stale status)
        return _and(
            _or(VideoTask.lifecycle == "editor_assigned",
                _and(_blank, VideoTask.status == "editing_soon")),
            VideoTask.editing_started_at == None)                            # noqa: E711
    if s == "editing":
        # actively editing: started -> editing / paused, and editor-marked-done-not-submitted
        return VideoTask.lifecycle.in_(["editing", "editing_paused", "editing_done"])
    if s == "qc_pending":
        # ONLY editor-submitted-for-check (nothing that already passed QC / uploaded)
        return VideoTask.lifecycle == "qc_pending"
    if s == "ready_for_youtube":
        return VideoTask.lifecycle == "ready_for_youtube"

    # generic buckets (pm_review, uploaded, changes_required, assigned, …) from the map
    lcs, sts = STATUS_LIFECYCLE_MAP.get(s, ([s], [s]))
    conds = []
    if lcs:
        conds.append(VideoTask.lifecycle.in_(lcs))
    if sts:
        conds.append(VideoTask.status.in_(sts))
    if not conds:
        return None
    return _or(*conds)


def _collab_norm():
    """SQL expression: collab_teacher_ids with spaces removed, so matching is independent of
    json.dumps spacing. '[1, 11, 2]' -> '[1,11,2]'."""
    from sqlalchemy import func as _f
    return _f.replace(_f.coalesce(VideoTask.collab_teacher_ids, ""), " ", "")


def teacher_filter(teacher_id):
    """Match primary teacher OR any collaborator by ID — boundary-safe (id 1 never matches 11)
    and spacing-independent. collab_teacher_ids is a json.dumps([...]) Text column; we strip
    spaces in SQL and anchor on comma/bracket boundaries."""
    from sqlalchemy import or_ as _or
    try:
        tid = int(teacher_id)
    except Exception:
        return None
    if not tid:
        return None
    ts = str(tid)
    norm = _collab_norm()
    return _or(
        VideoTask.teacher_id == tid,
        norm == "[" + ts + "]",
        norm.like("[" + ts + ",%"),
        norm.like("%," + ts + ",%"),
        norm.like("%," + ts + "]"),
    )


def channel_filter(db, channel_id=0, channel=""):
    """Match by VideoChannel id (preferred) with a normalised legacy channel_name fallback.
    `channel` may be a legacy name OR a numeric id (frontend transition). Matches tasks that
    carry either channel_id or the denormalised channel_name."""
    from sqlalchemy import or_ as _or, func as _f
    try:
        cid = int(channel_id or 0)
    except Exception:
        cid = 0
    raw = (channel or "").strip()
    if not cid and raw.isdigit():
        cid = int(raw); raw = ""
    name = None
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == cid).first()
        if ch:
            name = (ch.name or "").strip()
        conds = [VideoTask.channel_id == cid]
        if name:
            conds.append(_f.lower(_f.trim(VideoTask.channel_name)) == name.lower())
        return _or(*conds)
    if raw:
        low = raw.lower()
        sub = db.query(VideoChannel.id).filter(_f.lower(_f.trim(VideoChannel.name)) == low)
        return _or(_f.lower(_f.trim(VideoTask.channel_name)) == low,
                   VideoTask.channel_id.in_(sub))
    return None


def video_type_filter(db, video_type_id=0, video_type=""):
    """Match by VideoType id (preferred, resolved to its name) with a normalised legacy
    video_type string fallback. VideoTask has no video_type_id column, so the actual match is
    always on the denormalised video_type string (case/whitespace-insensitive)."""
    from sqlalchemy import func as _f
    try:
        vid = int(video_type_id or 0)
    except Exception:
        vid = 0
    raw = (video_type or "").strip()
    if not vid and raw.isdigit():
        vid = int(raw); raw = ""
    name = None
    if vid:
        vt = db.query(VideoType).filter(VideoType.id == vid).first()
        if vt:
            name = (vt.name or "").strip()
    target = (name or raw).strip().lower()
    if not target:
        return None
    return _f.lower(_f.trim(VideoTask.video_type)) == target


# date_field (UI selector) -> the VideoTask column the date-range filter runs on.
# created_at is stored UTC-naive; deadline / upload_date / editing_done_at are stored
# IST-naive-local (per the serializer). date_window_ist() returns IST calendar bounds and
# the caller shifts only the UTC-stored column.
DATE_FIELDS = {
    "created":      ("created_at",     True),
    "deadline":     ("deadline",       False),
    "upload":       ("upload_date",    False),
    "editing_done": ("editing_done_at", False),
}


def date_window_ist(date_range="", date_from="", date_to=""):
    """IST calendar window (start inclusive, end EXCLUSIVE) as naive-IST datetimes, or
    (None, None). Supports today / yesterday / week / month / custom. The caller subtracts
    the IST offset only for UTC-stored columns."""
    dr = (date_range or "").strip().lower()
    df = (date_from or "").strip()
    dt2 = (date_to or "").strip()
    try:
        if dr == "custom" or df or dt2:
            s = e = None
            if df:
                s = datetime.strptime(df[:10], "%Y-%m-%d")
            if dt2:
                e = datetime.strptime(dt2[:10], "%Y-%m-%d") + timedelta(days=1)
            return s, e
        if not dr or dr == "all":
            return None, None
        today = ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
        if dr == "today":
            return today, today + timedelta(days=1)
        if dr == "yesterday":
            return today - timedelta(days=1), today
        if dr in ("week", "weekly", "7d"):
            return today - timedelta(days=6), today + timedelta(days=1)
        if dr in ("month", "monthly", "30d"):
            return today - timedelta(days=29), today + timedelta(days=1)
        return None, None
    except Exception:
        return None, None


IST_OFFSET = timedelta(hours=5, minutes=30)


def date_range_clauses(date_field="", date_range="", date_from="", date_to=""):
    """Return a list of SQLAlchemy clauses for the date-range master filter on the chosen
    date_field, timezone-correct for that column's storage. Empty list = no date filter."""
    fld_name, is_utc = DATE_FIELDS.get((date_field or "").strip().lower(),
                                       DATE_FIELDS["created"])
    col = getattr(VideoTask, fld_name)
    ws, we = date_window_ist(date_range, date_from, date_to)
    if ws is None and we is None:
        return []
    if is_utc:
        ws = (ws - IST_OFFSET) if ws is not None else None
        we = (we - IST_OFFSET) if we is not None else None
    out = []
    if ws is not None:
        out.append(col != None); out.append(col >= ws)   # noqa: E711
    if we is not None:
        out.append(col != None); out.append(col < we)     # noqa: E711
    return out


def repair_legacy_production_state():
    """One-time idempotent sync of legacy admin state -> production lifecycle. Runs at STARTUP
    (main.py), NOT inside GET /tasks — the task list is now strictly read-only. Does exactly the
    healing the read path used to do on every request:
      • status=approved but lifecycle stuck in review  -> lifecycle=approved
      • status=uploaded but lifecycle not final         -> lifecycle=uploaded
      • youtuber_id set but creator_type != youtuber    -> creator_type=youtuber
    Safe to call repeatedly; never raises."""
    try:
        from database import SessionLocal
        from sqlalchemy import or_ as _or
    except Exception:
        return
    db = SessionLocal()
    try:
        changed = False
        for s in db.query(VideoTask).filter(
                VideoTask.status == "approved",
                VideoTask.lifecycle.in_(["pm_review", "creator_submitted"])).all():
            s.lifecycle = "approved"; changed = True
        for s in db.query(VideoTask).filter(
                VideoTask.status == "uploaded",
                VideoTask.lifecycle.isnot(None), VideoTask.lifecycle != "",
                ~VideoTask.lifecycle.in_(["uploaded", "completed"])).all():
            s.lifecycle = "uploaded"; changed = True
        for s in db.query(VideoTask).filter(
                VideoTask.youtuber_id.isnot(None),
                _or(VideoTask.creator_type == None,                      # noqa: E711
                    VideoTask.creator_type != "youtuber")).all():
            s.creator_type = "youtuber"; changed = True
        # Backfill: uploaded videos ka upload_date = publish date (IST-local), agar set nahi hai.
        _IST = timedelta(hours=5, minutes=30)
        for s in db.query(VideoTask).filter(
                VideoTask.lifecycle.in_(["uploaded", "completed"]),
                VideoTask.published_at.isnot(None),
                _or(VideoTask.upload_date == None, VideoTask.upload_date == "")).all():  # noqa: E711
            try:
                s.upload_date = s.published_at + _IST; changed = True
            except Exception:
                pass
        if changed:
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
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


def _resolve_thumb(t, g, thumb_map=None):
    """Card thumbnail as a URL — kabhi bhi base64 blob JSON me inline NAHI karta
    (warna har list response me MBs ka base64 jaata tha -> egress + RAM). base64 ho to
    /api/vt-thumb/{id} endpoint se serve; R2 URL ho to seedha CDN URL."""
    if g:
        return (g.thumbnail_url or "") if ((g.status or "") == "approved") else ""
    if thumb_map is not None:
        v = thumb_map.get(t.id, "")
        return v if v else (t.thumbnail_link or "")
    tb = getattr(t, "thumbnail_b64", "") or ""
    if tb.startswith("http"):
        try:
            return __import__("r2_storage").to_custom_domain(tb)
        except Exception:
            return tb
    if tb:
        return "/api/vt-thumb/%d" % t.id
    return t.thumbnail_link or ""


def thumb_map_for(db, ids):
    """Bulk: {task_id: thumbnail_url_field} — base64 column ka CONTENT load kiye bina.
    MySQL me CASE se sirf http-URL (chhota) ya '#b64' marker aata hai, 16MB base64 nahi."""
    out = {}
    ids = [int(x) for x in set(ids or []) if x]
    if not ids:
        return out
    try:
        from sqlalchemy import text, bindparam
        rows = db.execute(text(
            "SELECT id, "
            "CASE WHEN thumbnail_b64 LIKE 'http%' THEN thumbnail_b64 "
            "     WHEN thumbnail_b64 IS NOT NULL AND thumbnail_b64 <> '' THEN '#b64' "
            "     ELSE '' END AS tb, "
            "COALESCE(thumbnail_link,'') AS tl "
            "FROM video_tasks WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).fetchall()
        r2 = __import__("r2_storage")
        for rid, tb, tl in rows:
            if tb and tb != '#b64':
                try: out[rid] = r2.to_custom_domain(tb)
                except Exception: out[rid] = tb
            elif tb == '#b64':
                out[rid] = "/api/vt-thumb/%d" % rid
            elif tl:
                out[rid] = tl
            else:
                out[rid] = ""
    except Exception:
        out = {}
    return out


def _task_is_short(t):
    """True for short-form videos (Short / Reel), using the canonical format helper so the UI
    can de-emphasise thumbnail actions for shorts. Tolerant if the perf module is unavailable."""
    try:
        from performance_core import get_video_format, format_category, CAT_SHORT
        fmt = get_video_format(getattr(t, "video_type", "") or "",
                               getattr(t, "kind", "") or "",
                               getattr(t, "content_format", "") or "")
        return format_category(fmt) == CAT_SHORT
    except Exception:
        _v = (getattr(t, "video_type", "") or "").lower()
        return ("short" in _v) or ("reel" in _v)


def task_out(db, t, g=None, timeline=False, light=False, viewer=None, comment_count=None, thumb_map=None):
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
        "is_short": _task_is_short(t),
        "channel_name": t.channel_name or "",
        "series_name": (getattr(t, "series_name", "") or ""),
        "streaming": t.streaming or "",
        "priority": t.priority or "normal",
        "is_old": bool(getattr(t, "is_old", False)),
        "status": (t.status or ""),
        "deadline_req_status": (getattr(t, "deadline_req_status", "") or ""),
        "quality_rating": getattr(t, "quality_rating", None),
        "quality_note": (getattr(t, "quality_note", "") or ""),
        "quality_dims": _dims_out(getattr(t, "quality_dims", "")),
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
        "proposal_ok": (getattr(t, "proposal_ok", "") or ""),
        "is_proposal": ((getattr(t, "proposal_ok", "") or "") == "pending"),
        "deadline_iso": (t.deadline.strftime("%Y-%m-%dT%H:%M:%S") if t.deadline else ""),  # LOCAL (IST), no Z — deadlines are already local; a Z made new Date() shift by the tz offset
        # delay badge current STAGE ke deadline se (teacher on-time submit kar chuka to
        # editing stage me ab editor_deadline lagta hai, teacher wala nahi)
        "deadline_flag": (lambda f: {"kind": f[0], "label": f[1]})(deadline_flag(t, deadline=current_stage_deadline(t))),
        "editor_id": t.editor_id,
        "editor_name": _name_for_staff(db, t.editor_id),
        "collab_editor_ids": collab_editor_ids(t),
        "collab_editor_names": [_name_for_staff(db, _ei) for _ei in collab_editor_ids(t)],
        "is_editor_collab": len(all_editor_ids(t)) > 1,
        "editor_instructions": (getattr(t, "editor_instructions", "") or ""),
        "editor_reference": (getattr(t, "editor_reference", "") or ""),
        "editor_deadline": _dt_raw(getattr(t, "editor_deadline", None)),
        "editor_deadline_iso": (t.editor_deadline.strftime("%Y-%m-%dT%H:%M:%S") if getattr(t, "editor_deadline", None) else ""),
        "editing_progress": t.editing_progress or 0,
        "editing_seconds": t.editing_seconds or 0,
        "edited_link": t.edited_link or "",
        "qc_status": t.qc_status or "",
        "revision_count": t.revision_count or 0,
        # teacher (creator / collab) review of the edited video, BEFORE PM approves
        "teacher_review_status": (getattr(t, "teacher_review_status", "") or ""),
        "teacher_review_note": (getattr(t, "teacher_review_note", "") or ""),
        "teacher_review_rating": (getattr(t, "teacher_review_rating", None)),
        "teacher_reviewer_name": (getattr(t, "teacher_reviewer_name", "") or ""),
        "teacher_reviewed_at": (t.teacher_reviewed_at.strftime("%d %b %Y, %I:%M %p") if getattr(t, "teacher_reviewed_at", None) else ""),
        # youtuber videos skip the teacher gate; teacher/collab videos need it
        "teacher_review_required": ((getattr(t, "creator_type", "") or "teacher") != "youtuber"),
        "approval_required": needs_pm_approval(db, t),
        "on_hold": bool(t.on_hold),
        "cancelled": bool(t.cancelled),
        "youtube_url": t.youtube_url or "",
        "yt_video_id": t.yt_video_id or "",
        "upload_date": _dt_raw(getattr(t, "upload_date", None)),
        "upload_date_iso": (t.upload_date.strftime("%Y-%m-%dT%H:%M:%S") if getattr(t, "upload_date", None) else ""),
        "upload_remarks": (getattr(t, "upload_remarks", "") or ""),
        "yt_views": (t.yt_views if t.yt_views is not None else None),
        "yt_views_at": _dt(t.yt_views_at),
        "published_at": _dt(t.published_at),
        "reference": t.reference or "",
        "reference_video": getattr(t, "reference_video", "") or "",
        "remarks": t.remarks or "",
        "comment_count": (comment_count if comment_count is not None else _comment_count(db, t.id)),
        "submitted_link": t.submitted_link or "",
        "submitted_by_role": (getattr(t, "submitted_by_role", "") or ""),
        "submitted_by_name": (getattr(t, "submitted_by_name", "") or ""),
        "submitted_at": (t.submitted_at.strftime("%d %b %Y, %I:%M %p") if getattr(t, "submitted_at", None) else ""),
        # teacher ki submission on-time thi ya nahi — HAMESHA current deadline se (live)
        "on_time": (bool(t.submitted_at <= t.deadline) if (getattr(t, "submitted_at", None) and t.deadline) else None),
        "created_at": _dt(t.created_at),
        # card thumbnail: graphics-made thumbnail first, else the one uploaded at assign time
        "thumbnail": _resolve_thumb(t, g, thumb_map),
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
            "deadline_iso": ((g.deadline.strftime("%Y-%m-%dT%H:%M:%S") if getattr(g, "deadline", None) else "") if g else ""),
            "deadline": (_dt_raw(getattr(g, "deadline", None)) if g else ""),
            "instructions": (g.instructions if g else ""),
            "remarks": (g.remarks if g else ""),
            "quality_rating": (g.quality_rating if g else None),
            "quality_note": (getattr(g, "quality_note", "") if g else ""),
            "quality_dims": (_dims_out(getattr(g, "quality_dims", "")) if g else {}),
            "drive_link": (getattr(g, "drive_link", "") if g else ""),
            "revision_count": (g.revision_count if g else 0),
        },
    }
    # §38b — the editor is judged against the EDITOR's own deadline, never the
    # teacher deadline. An editor with no editor_deadline set has no personal
    # deadline, so nothing is ever shown "delayed" against the teacher's date.
    if viewer == "editor":
        _ed = getattr(t, "editor_deadline", None)
        out["deadline"] = _dt_raw(_ed)
        out["deadline_iso"] = (_ed.strftime("%Y-%m-%dT%H:%M:%S") if _ed else "")
        out["deadline_flag"] = (lambda f: {"kind": f[0], "label": f[1]})(deadline_flag(t, deadline=_ed))
    # live editing timer: seconds keep counting while lifecycle == 'editing'
    _live_secs, _editing_running = editing_time_state(db, t)
    out["live_editing_seconds"] = _live_secs
    out["editing_running"] = _editing_running
    # ---- Urgent pause-request (PM -> editor) ----
    _prq = bool(getattr(t, "pause_req", False))
    out["pause_req"] = _prq
    if _prq:
        _prd = getattr(t, "pause_req_deadline", None)
        out["pause_req_by"] = getattr(t, "pause_req_by", "") or ""
        out["pause_req_deadline"] = _dt_raw(_prd)
        out["pause_req_deadline_iso"] = (_prd.strftime("%Y-%m-%dT%H:%M") if _prd else "")
        out["pause_req_urgent_id"] = getattr(t, "pause_req_urgent_id", None)
    # edited-link version history (Version 1 / Version 2 ...) — shown in every portal
    out["edited_versions"] = edited_versions_out(db, t) if t.edited_link else []
    if not light:
        out["thumbnail_link"] = t.thumbnail_link or ""
        if viewer == "editor":
            _ed = getattr(t, "editor_deadline", None)
            out["deadline_iso"] = (_ed.strftime("%Y-%m-%dT%H:%M") if _ed else "")
        else:
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
        # Editor -> Teacher recording feedback (full + compact summary). Only ever set when real
        # feedback exists, so legacy videos show "no feedback" rather than a fake zero rating.
        try:
            _rfb = get_active_recording_feedback(db, video_task_id=t.id)
            out["recording_feedback"] = recording_feedback_out(db, _rfb)
            out["recording_feedback_summary"] = recording_feedback_summary(db, _rfb)
            out["recording_feedback_applicable"] = rec_fb_applicable(t)
        except Exception:
            out["recording_feedback"] = None
            out["recording_feedback_summary"] = None
        # OPTIONAL creative editing brief (source submitter -> editor). Distinct from editor_instructions.
        try:
            _cb = get_active_creative_brief(db, video_task_id=t.id)
            out["creative_brief"] = creative_brief_out(db, _cb)
        except Exception:
            out["creative_brief"] = None
    # brief summary is cheap + wanted on list cards too (light mode), so compute it outside `timeline`
    try:
        out["creative_brief_summary"] = creative_brief_summary(db, get_active_creative_brief(db, video_task_id=t.id))
    except Exception:
        out["creative_brief_summary"] = None
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


def edited_versions_out(db, t):
    """All edited-video links the editor has submitted, oldest first, numbered Version 1..N.
    Powers the 'Edited Link' view (Version 1 / Version 2 ...) across editor, admin & production."""
    rows = (db.query(ProductionEvent)
            .filter(ProductionEvent.task_id == t.id,
                    ProductionEvent.event.in_(["edited_video_submitted", "revision_submitted"]))
            .order_by(ProductionEvent.created_at.asc(), ProductionEvent.id.asc()).all())
    seq = []
    for e in rows:
        try:
            link = (json.loads(e.meta) if e.meta else {}).get("link", "")
        except Exception:
            link = ""
        if link:
            seq.append({"link": link, "at": _dt(e.created_at)})
    # make sure the current edited_link is represented (older data without events)
    if t.edited_link and (not seq or seq[-1]["link"] != t.edited_link):
        # only append if this exact link isn't already the last one recorded
        if not any(s["link"] == t.edited_link for s in seq):
            seq.append({"link": t.edited_link, "at": _dt(getattr(t, "editing_done_at", None))})
    # collapse consecutive duplicate links, then number
    out = []
    last = None
    for s in seq:
        if s["link"] == last:
            out[-1]["at"] = s["at"] or out[-1]["at"]
            continue
        out.append({"version": len(out) + 1, "link": s["link"], "at": s["at"]})
        last = s["link"]
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
        note = ""
        if e.event in ("progress_updated", "editing_paused"):
            try:
                _m = (json.loads(e.meta) if e.meta else {}) or {}
                pct = _m.get("progress")
                note = _m.get("note") or ""
            except Exception:
                pct = None
        base = _lbl.get(e.event, "") or ((str(pct) + "%") if pct is not None else e.event)
        # for a pause we keep the "Paused" label and expose the % separately
        if e.event == "progress_updated":
            label = (str(pct) + "%") if pct is not None else base
        else:
            label = base
        out.append({"label": label, "progress": pct, "note": note, "at": _dt(e.created_at)})
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
    "deadline_changed": "Deadline Changed",
    "deadline_rejected": "Deadline Request Rejected",
    "task_edited": "Task Edited",
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
    "teacher_review_approved": "Teacher Approved Edited Video",
    "teacher_review_changes": "Teacher Requested Changes",
    "revision_submitted": "Revision Submitted",
    "youtube_link_added": "YouTube Link Added",
    "uploaded": "Uploaded",
    "youtube_metrics_updated": "YouTube Metrics Updated",
    "sent_to_students": "Sent to Students",
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
        cid = None
        try:
            lk = str(n.link or "")
            if lk.isdigit():
                tid = int(lk)
            else:
                # chapter deep-link "taskId:chapterId" (creative_brief / recording_feedback on a chapter)
                m = _re.match(r"^(\d+):(\d+)$", lk)
                if m:
                    tid = int(m.group(1)); cid = int(m.group(2))
        except Exception:
            tid = None; cid = None
        out.append({"id": n.id, "title": n.title or "", "message": n.message or "",
                    "type": n.notif_type or "", "is_read": bool(n.is_read),
                    "task_id": tid, "chapter_id": cid, "at": _dt(n.created_at)})
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
# Har stage ka apna deadline hota hai. Delay HAMESHA current stage ke deadline se
# naapo — teacher ke deadline se nahi. Jaise hi teacher on-time submit kar deta hai,
# task editor stage me chala jaata hai aur ab editor_deadline lagta hai (teacher wala nahi).
_STAGE_EDITOR_ACTIVE = {"editor_assigned", "editing_soon", "editing",
                        "editing_paused", "editing_done", "qc_changes"}
_STAGE_UPLOAD = {"qc_approved", "ready_for_youtube"}
# in stages me kisi ka active countdown nahi (kaam submit ho chuka / PM review me hai)
_STAGE_NO_COUNTDOWN = {"creator_submitted", "pm_review", "approved", "qc_pending",
                       "uploaded", "completed"}


def recompute_on_time(t):
    """Jab deadline change ho (PM/admin edit, extension approve): agar video PEHLE hi
    submit ho chuki hai to on_time ko NAYI deadline ke hisaab se dobara set karo.
    Matlab admin ne submission ke baad deadline aage badha di -> jo 'delayed' tha ab
    'on time' ho jaaye (aur ulta bhi). Sirf submitted tasks par asar; warna kuch nahi."""
    try:
        st = getattr(t, "submitted_at", None)
        dl = getattr(t, "deadline", None)
        if st and dl:
            t.on_time = bool(st <= dl)
    except Exception:
        pass


def current_stage_deadline(t):
    """The deadline actually IN FORCE right now, chosen by lifecycle stage:
    - editor stages -> editor_deadline
    - ready/qc_approved -> upload_date
    - teacher (re)work stages -> teacher deadline
    - submitted / PM review / done -> None (koi delay nahi)."""
    lc = (getattr(t, "lifecycle", "") or "")
    if lc in _STAGE_NO_COUNTDOWN:
        return None
    if lc in _STAGE_EDITOR_ACTIVE:
        return getattr(t, "editor_deadline", None)
    if lc in _STAGE_UPLOAD:
        return getattr(t, "upload_date", None)
    # teacher/creator stages + changes_required/reshoot_required/rejected
    return getattr(t, "deadline", None)


# Active pipeline stages jinme koi na koi countdown chal raha ho sakta hai (single-video).
_ACTIVE_PIPELINE = ["creator_assigned", "creator_working", "creator_submitted", "pm_review",
                    "approved", "editor_assigned", "editing", "editing_paused", "editing_done",
                    "qc_pending", "qc_changes", "ready_for_youtube", "changes_required"]


def _bucket_deadline(t):
    """The ONE stage-aware deadline used for overdue / today / this-week / no-deadline buckets.
    Legacy blank-lifecycle tasks fall back to the admin `status` (only genuinely-open states
    carry a deadline). Mirrors the dashboard KPI exactly — single source of truth so the Tasks
    list deadline filter and the KPI count never disagree."""
    lc = t.lifecycle or ""
    if not lc:
        st = (t.status or "").lower()
        return getattr(t, "deadline", None) if st in ("assigned", "reshoot", "rejected") else None
    if lc in _STAGE_UPLOAD:
        return getattr(t, "upload_date", None)
    if lc in _STAGE_NO_COUNTDOWN:
        return None
    if lc in _STAGE_EDITOR_ACTIVE:
        return getattr(t, "editor_deadline", None)
    return getattr(t, "deadline", None)


def _active_single_rows(db):
    """Normal-kind, not-cancelled, not-old, active-or-legacy-blank lifecycle single videos —
    the population the deadline buckets scan."""
    from sqlalchemy import or_ as _or
    from sqlalchemy.orm import defer as _defer
    _NS = _or(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")  # noqa: E711
    _LC_OK = _or(VideoTask.lifecycle.in_(_ACTIVE_PIPELINE),
                 VideoTask.lifecycle == None, VideoTask.lifecycle == "")  # noqa: E711
    return (db.query(VideoTask)
            .options(_defer(VideoTask.thumbnail_b64))
            .filter(VideoTask.cancelled == False, _NS, VideoTask.is_old == False,  # noqa: E712
                    _LC_OK).all())


def overdue_today_ids(db):
    """SINGLE SOURCE OF TRUTH for stage-aware DELAYED + DUE-TODAY task ids.

    Dashboard KPI (Delayed / Due Today), Tasks list ka 'Delayed'/'Due Today' filter, aur cards —
    sab isi se chalte hain -> count + list HAMESHA match. Normal-kind single videos only; teacher +
    youtuber dono. Delay hamesha CURRENT STAGE ke deadline se.

    IMPORTANT fixes:
    - deadline/editor_deadline/upload_date SAB IST-naive store hote hain (serializer: 'already local'),
      isliye comparison IST-now se (ist_now) — pehle teacher/editor ke liye utcnow use hota tha (5.5h
      galat) jisse boundary tasks chhoot jaate the.
    - Legacy/admin-created tasks ka lifecycle BLANK hota hai -> unhe bhi shaamil karo (status se stage
      decide karke). Pehle sirf lifecycle.in_(pipeline) tha -> Vicky Verma jaise blank-lifecycle
      overdue tasks PM par dikhte hi nahi the (admin par dikhte the) -> mismatch."""
    ref = ist_now()   # IST-naive "now" — kyunki deadlines IST-local store hote hain
    overdue, today = [], []
    for t in _active_single_rows(db):
        dl = _bucket_deadline(t)
        if not dl:
            continue
        if dl < ref:
            overdue.append(t.id)
        elif dl.date() == ref.date():
            today.append(t.id)
    return overdue, today


def deadline_state_ids(db, state):
    """Stage-aware deadline bucket -> task ids (IST). States: overdue | today | week | none.
    'week' = current-stage deadline falls within the next 7 IST days (today included, not
    overdue). 'none' = active task with no deadline set for its current stage. Uses the SAME
    stage-aware deadline as the dashboard KPI, so list and count always match. (Date-range and
    deadline-state are independent filters — this covers only the deadline-state one.)"""
    st = (state or "").strip().lower()
    if st in ("overdue", "today"):
        ovd, tod = overdue_today_ids(db)
        return ovd if st == "overdue" else tod
    if st not in ("week", "none"):
        return []
    ref = ist_now()
    today0 = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = today0 + timedelta(days=7)
    out = []
    for t in _active_single_rows(db):
        dl = _bucket_deadline(t)
        if st == "none":
            if not dl:
                out.append(t.id)
        else:  # week — upcoming (not already overdue)
            if dl and today0 <= dl < week_end:
                out.append(t.id)
    return out


_DL_UNSET = object()


def deadline_flag(t, deadline=_DL_UNSET):
    """Human-readable deadline signal for cards/filters (spec §38). Canonical UTC stored;
    labels are plain English, never raw timer text.

    ``deadline`` overrides ``t.deadline`` so a role can be judged against its own
    deadline (e.g. editors against ``editor_deadline``, not the teacher deadline).
    An EXPLICIT ``None`` means "no active deadline for this stage" -> no delay shown
    (different from omitting the arg, which falls back to the teacher deadline)."""
    dl = t.deadline if deadline is _DL_UNSET else deadline
    if not dl:
        return ("none", "No deadline")
    if t.lifecycle in ("uploaded", "completed"):
        return ("done", "Completed")
    now = datetime.utcnow()
    delta = (dl - now).total_seconds()
    ad = abs(delta)
    d = int(ad // 86400); h = int((ad % 86400) // 3600); m = int((ad % 3600) // 60)
    if delta < 0:
        # delayed: hours for the first 2 days (e.g. "34h 12m delayed"), then days
        # (kind stays "overdue" internally for colours/filters; only the label reads "delayed")
        if ad < 172800:
            th = int(ad // 3600)
            s = "%dh %02dm delayed" % (th, m)
        else:
            s = "%dd %02dh delayed" % (d, h)
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


# ==================================================================== LIVE TEAM TRACKER
# One read-only snapshot of the whole production team: who is editing what right now,
# for how long (live), what is paused, what is queued, and completed counts/lists.
# Safe for PM, admin and youtuber portals (no payout/financial data exposed).
def _tt_editor_brief(db, t, now):
    live, running = editing_time_state(db, t)
    since = None
    if running:
        s = (db.query(EditingSession)
             .filter(EditingSession.task_id == t.id, EditingSession.ended_at == None)  # noqa: E711
             .order_by(EditingSession.started_at.desc()).first())
        since = (s.started_at if s else t.editing_started_at)
    else:
        since = t.editing_started_at
    lc = t.lifecycle or ""
    # editor tracker judges the EDITOR against the editor's own deadline, never the teacher deadline
    _edl = getattr(t, "editor_deadline", None)
    overdue = bool(_edl and _edl < now and lc not in ("ready_for_youtube", "uploaded", "completed"))
    return {
        "id": t.id, "title": (t.title or "Untitled"), "ref_code": (t.ref_code or ""),
        "lifecycle": lc, "priority": (t.priority or "normal"),
        "progress": int(t.editing_progress or 0),
        "deadline": (_dt_raw(_edl) if _edl else ""),
        "overdue": overdue,
        "since": (_dt(since) if since else ""),
        "live_seconds": int(live), "running": bool(running),
    }


def _tt_chapter_brief(db, c, now):
    """Phase 2a: an editor's PROJECT-CHAPTER rendered like a normal editing task for the
    live tracker (so project editing no longer shows the editor as 'free'). Read-only."""
    est = (getattr(c, "edit_state", "") or "")
    _dl = getattr(c, "deadline", None)
    overdue = bool(_dl and _dl < now and est != "edited")
    running = (est == "editing")
    live = 0
    _started = getattr(c, "editing_started_at", None)
    if running and _started:
        try:
            live = max(0, int((now - _started).total_seconds()))
        except Exception:
            live = 0
    ptitle = ""
    try:
        t = db.query(VideoTask).filter(VideoTask.id == c.task_id).first()
        ptitle = ((t.subject or t.title or "") if t else "")
    except Exception:
        ptitle = ""
    lc = ("editing" if est == "editing" else ("editing_paused" if est == "paused"
          else ("editing_done" if est == "edited" else "editor_assigned")))
    return {
        "id": c.task_id, "chapter_id": c.id,
        "title": (c.title or "Chapter") + (" — " + ptitle if ptitle else ""),
        "ref_code": "PROJECT", "lifecycle": lc, "priority": "normal",
        "progress": int(getattr(c, "editing_progress", 0) or 0),
        "deadline": (_dt_raw(_dl) if _dl else ""),
        "overdue": overdue,
        "since": (_dt(_started) if _started else ""),
        "live_seconds": int(live), "running": bool(running),
        "is_project": True,
    }


def _tt_graphics_brief(db, g, now):
    t = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
    st = g.status or ""
    overdue = bool(g.deadline and g.deadline < now and st != "approved")
    return {
        "id": g.task_id, "title": ((t.title if t else "") or "Untitled"),
        "ref_code": ((t.ref_code if t else "") or ""),
        "status": st, "priority": (g.priority or "normal"),
        "deadline": (_dt_raw(g.deadline) if g.deadline else ""),
        "overdue": overdue,
        "since": (_dt(g.started_at) if g.started_at else ""),
    }


def build_team_tracker(db):
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    DONE = ["ready_for_youtube", "uploaded", "completed"]
    QUEUE = ["editor_assigned", "editing_soon", "approved", "qc_changes"]

    editors = []
    alerts = []
    s_editing = s_idle = s_paused = s_overdue = s_queued = s_need = 0
    for sp in (db.query(ProductionStaffProfile)
               .filter(ProductionStaffProfile.staff_role == "editor",
                       ProductionStaffProfile.is_active == True)  # noqa: E712
               .order_by(ProductionStaffProfile.id.asc()).all()):
        base = db.query(VideoTask).filter(VideoTask.cancelled == False,  # noqa: E712
                                          VideoTask.editor_id == sp.id)
        cur_t = (base.filter(VideoTask.lifecycle == "editing")
                 .order_by(VideoTask.editing_started_at.desc()).first())
        current = _tt_editor_brief(db, cur_t, now) if cur_t else None
        paused = [_tt_editor_brief(db, t, now) for t in
                  base.filter(VideoTask.lifecycle == "editing_paused").order_by(VideoTask.updated_at.desc()).all()]
        queue = [_tt_editor_brief(db, t, now) for t in
                 base.filter(VideoTask.lifecycle.in_(QUEUE)).order_by(VideoTask.editor_deadline.asc()).all()]
        queue.sort(key=lambda x: 0 if x["priority"] == "urgent" else 1)  # urgent first (stable: keeps deadline order)
        review = [_tt_editor_brief(db, t, now) for t in
                  base.filter(VideoTask.lifecycle.in_(["editing_done", "qc_pending"])).order_by(VideoTask.updated_at.desc()).all()]
        # --- Phase 2a bridge: fold PROJECT-CHAPTER editing into this editor's live load ---
        # (additive + guarded: a chapter the editor is editing now shows them as "editing",
        #  not "free". Never throws into the main tracker.)
        try:
            from models import VideoTaskChapter as _VC
            for _c in db.query(_VC).filter(_VC.editor_id == sp.id).all():
                _est = (getattr(_c, "edit_state", "") or "")
                if _est == "editing":
                    if current is None:
                        current = _tt_chapter_brief(db, _c, now)
                    else:
                        queue.append(_tt_chapter_brief(db, _c, now))
                elif _est == "paused":
                    paused.append(_tt_chapter_brief(db, _c, now))
                elif _est == "edited":
                    review.append(_tt_chapter_brief(db, _c, now))
                elif _est in ("", "assigned"):
                    queue.append(_tt_chapter_brief(db, _c, now))
        except Exception:
            pass
        completed_recent = [_tt_editor_brief(db, t, now) for t in
                            base.filter(VideoTask.lifecycle.in_(DONE)).order_by(VideoTask.updated_at.desc()).limit(10).all()]
        completed_count = base.filter(VideoTask.lifecycle.in_(DONE)).count()
        completed_month = base.filter(VideoTask.lifecycle.in_(DONE), VideoTask.updated_at >= month_start).count()
        # Phase 2a: also credit finished PROJECT CHAPTERS (edited / uploaded) to this editor
        try:
            from models import VideoTaskChapter as _VCd
            _cb = db.query(_VCd).filter(_VCd.editor_id == sp.id)
            completed_count += _cb.filter(_VCd.edit_state == "edited").count()
            completed_month += _cb.filter(_VCd.edited_at != None, _VCd.edited_at >= month_start).count()  # noqa: E711
        except Exception:
            pass
        overdue_count = base.filter(VideoTask.editor_deadline != None, VideoTask.editor_deadline < now,  # noqa: E711
                                    ~VideoTask.lifecycle.in_(DONE)).count()
        active_count = (1 if current else 0) + len(paused) + len(queue) + len(review)
        rec = sp.recommended_load or 5
        status = ("editing" if current else ("paused" if paused else
                  ("review" if review else ("queued" if queue else "idle"))))
        # --- "about to run out of work" detection (alert PM to assign the next task) ---
        need = False
        need_reason = ""
        need_progress = 0
        if active_count == 0:
            need, need_reason = True, "idle"
        elif active_count == 1:
            if current:
                need_progress = int(current.get("progress") or 0)
                if need_progress >= 50:
                    need, need_reason = True, "finishing"
            elif review:
                need, need_reason = True, "free"
            elif paused:
                need_progress = int(paused[0].get("progress") or 0)
                if need_progress >= 50:
                    need, need_reason = True, "finishing"
        if need:
            alerts.append({"id": sp.id, "name": (sp.user.name if sp.user else ""),
                           "reason": need_reason, "progress": need_progress})
            s_need += 1
        editors.append({
            "id": sp.id, "name": (sp.user.name if sp.user else ""),
            "recommended": rec, "overloaded": active_count > rec, "active_count": active_count,
            "status": status, "current": current, "paused": paused, "queue": queue, "review": review,
            "completed_recent": completed_recent, "completed_count": completed_count,
            "completed_month": completed_month, "overdue_count": overdue_count,
            "needs_task": need, "need_reason": need_reason, "need_progress": need_progress,
        })
        if current:
            s_editing += 1
        elif not (paused or queue or review):
            s_idle += 1
        if paused:
            s_paused += 1
        s_overdue += overdue_count
        s_queued += len(queue)

    graphics = []
    g_working = g_idle = 0
    for sp in (db.query(ProductionStaffProfile)
               .filter(ProductionStaffProfile.staff_role == "graphics",
                       ProductionStaffProfile.is_active == True)  # noqa: E712
               .order_by(ProductionStaffProfile.id.asc()).all()):
        gbase = db.query(GraphicsTask).filter(
            GraphicsTask.graphics_id == sp.id,
            GraphicsTask.task_id.in_(db.query(VideoTask.id).filter(VideoTask.cancelled == False)))  # noqa: E712
        cur_g = gbase.filter(GraphicsTask.status == "in_progress").order_by(GraphicsTask.started_at.desc()).first()
        current = _tt_graphics_brief(db, cur_g, now) if cur_g else None
        queue = [_tt_graphics_brief(db, g, now) for g in gbase.filter(GraphicsTask.status == "new").all()]
        changes = [_tt_graphics_brief(db, g, now) for g in gbase.filter(GraphicsTask.status == "changes").all()]
        submitted = [_tt_graphics_brief(db, g, now) for g in gbase.filter(GraphicsTask.status == "submitted").all()]
        for lst in (queue, changes):
            lst.sort(key=lambda x: 0 if x["priority"] == "urgent" else 1)
        completed_recent = [_tt_graphics_brief(db, g, now) for g in
                            gbase.filter(GraphicsTask.status == "approved").order_by(GraphicsTask.approved_at.desc()).limit(10).all()]
        completed_count = gbase.filter(GraphicsTask.status == "approved").count()
        completed_month = gbase.filter(GraphicsTask.status == "approved", GraphicsTask.approved_at != None,  # noqa: E711
                                       GraphicsTask.approved_at >= month_start).count()
        active_count = (1 if current else 0) + len(queue) + len(changes) + len(submitted)
        rec = sp.recommended_load or 5
        status = ("working" if current else ("changes" if changes else
                  ("submitted" if submitted else ("queued" if queue else "idle"))))
        graphics.append({
            "id": sp.id, "name": (sp.user.name if sp.user else ""),
            "recommended": rec, "overloaded": active_count > rec, "active_count": active_count,
            "status": status, "current": current, "queue": queue, "changes": changes,
            "submitted": submitted, "completed_recent": completed_recent,
            "completed_count": completed_count, "completed_month": completed_month,
        })
        if current:
            g_working += 1
        elif not (queue or changes or submitted):
            g_idle += 1

    _need_order = {"idle": 0, "free": 1, "finishing": 2}
    alerts.sort(key=lambda a: (_need_order.get(a["reason"], 9), -(a["progress"] or 0)))
    return {
        "server_now": _dt(now),
        "summary": {
            "editors_total": len(editors), "editing_now": s_editing, "idle": s_idle,
            "paused": s_paused, "overdue": s_overdue, "queued": s_queued,
            "needs_assignment": s_need,
            "graphics_total": len(graphics), "graphics_working": g_working, "graphics_idle": g_idle,
        },
        "alerts": alerts,
        "editors": editors, "graphics": graphics,
    }
