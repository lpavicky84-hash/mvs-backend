"""
performance_core.py — SINGLE SOURCE OF TRUTH for Editor & Graphics production
performance + ranking.

Why this module exists
----------------------
Before this, three places computed performance independently (editor_routes,
graphics_routes, production_routes.pm_analytics) with THREE different "short"
definitions and THREE different "done"/date rules, and project chapters were
only counted in PM analytics. That meant an editor's own page disagreed with
the PM page, and project work (VideoTaskChapter) silently vanished from an
editor's/graphics' numbers.

This module unifies everything:
  - get_video_format(...)        canonical long/short/reel/live/other classifier
  - get_editor_work_items(...)   normal VideoTask + project VideoTaskChapter, no double-count
  - get_graphics_work_items(...) normal GraphicsTask + project chapter thumbnails
  - compute_editor_performance(...) / compute_graphics_performance(...)  100-pt score
  - compute_editor_leaderboard(...) / compute_graphics_leaderboard(...)  fair, tie-broken
  - rank snapshots + movement (ProductionRankSnapshot)

Design rules honoured:
  * Real data only — no fabricated scores. Missing metric => N/A (never a fake 0).
  * Project PARENT containers (one_shot/rapid_revision/project) never count as an
    edited video; their CHILD chapters count instead (no double counting).
  * "rapid"/"one shot"/"project" are NOT Short by themselves -> long-form.
  * Monthly attribution uses the real completion date (editing_done_at / edited_at /
    approved_at), never updated_at.
"""
from datetime import datetime, date, timedelta

# ============================================================ CONFIG (centralized)
# Canonical video formats
FMT_LONG = "long"
FMT_SHORT = "short"
FMT_REEL = "reel"
FMT_LIVE = "live"
FMT_OTHER = "other"
_VALID_FMT = {FMT_LONG, FMT_SHORT, FMT_REEL, FMT_LIVE, FMT_OTHER}

# Editor specialization
SPEC_LONG = "long"
SPEC_SHORT = "short"
SPEC_HYBRID = "hybrid"

# Leaderboard categories
CAT_LONG = "long"
CAT_SHORT = "short"

# 100-point editor score weights
EDITOR_WEIGHTS = {"output": 35, "quality": 25, "on_time": 20, "first_pass": 10, "consistency": 10}
# 100-point graphics score weights
GRAPHICS_WEIGHTS = {"output": 30, "quality": 25, "first_pass": 20, "on_time": 15, "consistency": 10}

# Monthly output targets (configurable defaults). Short targets are higher than long.
DEFAULT_TARGET_LONG = 8          # long videos / month
DEFAULT_TARGET_SHORT = 20        # shorts+reels / month
DEFAULT_TARGET_THUMBS = 25       # thumbnails / month (graphics)

# Overachievement cap: output score caps at 1.0 (target) — quantity can't dominate.
OUTPUT_CAP = 1.0

# Consistency: completing work on this many distinct days = full marks.
CONSISTENCY_TARGET_DAYS = 12

# Minimum completed items before a rank is "real" (else Provisional / Limited Data)
MIN_SAMPLE = {CAT_LONG: 2, CAT_SHORT: 4}
MIN_SAMPLE_GRAPHICS = 3

# Badge thresholds (centralized; evaluated against real achievements)
BADGE_CFG = {
    "top_performer_score": 85.0,
    "quality_champion": 4.6,
    "deadline_master_ontime": 95,
    "first_pass_pro": 90,
    "consistency_star_days": 10,
    "long_10": 10,
    "short_20": 20,
}

QUALITY_SCALE = 5.0  # ratings are 1..5


# ============================================================ FORMAT CLASSIFIER
def get_video_format(video_type="", kind="", explicit=""):
    """Canonical format for a task/chapter.

    explicit  : per-item override (VideoTask.content_format) — wins if valid.
    video_type: free-text VideoTask.video_type ("Long Video", "Short Video", "Reel", ...).
    kind      : one_shot / rapid_revision / project / normal — NEVER forces Short.

    Returns one of long/short/reel/live/other. Default = long (most production is
    long-form; we never call something Short just because it is "rapid"/"one shot").
    """
    e = (explicit or "").strip().lower()
    if e in _VALID_FMT:
        return e
    t = (video_type or "").strip().lower()
    if "reel" in t:
        return FMT_REEL
    if "short" in t:
        return FMT_SHORT
    if "live" in t:
        return FMT_LIVE
    if "long" in t:
        return FMT_LONG
    # "rapid revision", "one shot", "project", "strategy", "" -> long-form by default.
    return FMT_LONG


def format_category(fmt):
    """Leaderboard bucket: short/reel compete together; everything else is long."""
    return CAT_SHORT if fmt in (FMT_SHORT, FMT_REEL) else CAT_LONG


def resolve_specialization(sp):
    """Editor specialization from the explicit column, else inferred from free-text
    `skills` (backward compat), else HYBRID (never guess long/short wrongly)."""
    v = (getattr(sp, "editor_specialization", "") or "").strip().lower()
    if v in (SPEC_LONG, SPEC_SHORT, SPEC_HYBRID):
        return v
    sk = (getattr(sp, "skills", "") or "").strip().lower()
    if sk:
        has_short = ("short" in sk) or ("reel" in sk)
        has_long = "long" in sk
        if has_short and not has_long:
            return SPEC_SHORT
        if has_long and not has_short:
            return SPEC_LONG
    return SPEC_HYBRID


def _target_for(sp, category):
    if category == CAT_SHORT:
        t = getattr(sp, "target_short", None)
        return int(t) if t else DEFAULT_TARGET_SHORT
    t = getattr(sp, "target_long", None)
    return int(t) if t else DEFAULT_TARGET_LONG


# ============================================================ PERIODS
def month_bounds(ref=None):
    ref = ref or datetime.utcnow()
    start = datetime(ref.year, ref.month, 1)
    end = datetime(ref.year + (1 if ref.month == 12 else 0),
                   1 if ref.month == 12 else ref.month + 1, 1)
    return start, end


def period_bounds(period="month", ref=None):
    """period: today | week | month | prev_month | YYYY-MM (custom)."""
    now = ref or datetime.utcnow()
    if period == "today":
        s = datetime(now.year, now.month, now.day)
        return s, s + timedelta(days=1), now.strftime("%d %b %Y")
    if period == "week":
        s = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
        return s, s + timedelta(days=7), "This Week"
    if period == "prev_month":
        m0, _ = month_bounds(now)
        prev_end = m0
        prev_start, _ = month_bounds(m0 - timedelta(days=1))
        return prev_start, prev_end, prev_start.strftime("%B %Y")
    if period and len(period) == 7 and period[4] == "-":
        try:
            y, m = int(period[:4]), int(period[5:7])
            s = datetime(y, m, 1)
            e = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
            return s, e, s.strftime("%B %Y")
        except Exception:
            pass
    s, e = month_bounds(now)
    return s, e, s.strftime("%B %Y")


# ============================================================ WORK-ITEM COLLECTORS
_EDITED_LC = {"qc_pending", "qc_changes", "ready_for_youtube", "uploaded", "completed"}
_APPROVED_LC = {"ready_for_youtube", "uploaded", "completed"}
_PUBLISHED_LC = {"uploaded", "completed"}
_OPEN_LC = {"editor_assigned", "editing", "editing_paused", "qc_changes"}
_SPECIAL_KINDS = {"one_shot", "rapid_revision", "project"}


def _mk_item(**k):
    it = {"id": None, "source": "normal", "format": FMT_LONG, "category": CAT_LONG,
          "title": "", "task_id": None, "edited": False, "approved": False,
          "published": False, "pending": False, "overdue": False, "revisions": 0,
          "turnaround_hours": None, "on_time": None, "has_deadline": False,
          "quality": None, "completed_at": None, "deadline": None, "lifecycle": ""}
    it.update(k)
    return it


def get_editor_work_items(db, staff_id, now=None):
    """Every production item an editor actually works on, normalized & de-duplicated.

    - Normal work: VideoTask.editor_id == staff_id AND kind NOT a project container.
    - Project work: VideoTaskChapter.editor_id == staff_id (the parent container is
      NEVER counted — avoids double counting).
    """
    from models import VideoTask, VideoTaskChapter
    now = now or datetime.utcnow()
    items = []

    # ---- normal VideoTask (exclude project containers) ----
    vts = (db.query(VideoTask)
           .filter(VideoTask.cancelled == False,  # noqa: E712
                   VideoTask.editor_id == staff_id).all())
    for t in vts:
        if (getattr(t, "kind", "normal") or "normal") in _SPECIAL_KINDS:
            continue  # container — its chapters count instead
        lc = (getattr(t, "lifecycle", "") or "")
        done_at = getattr(t, "editing_done_at", None)
        edited = bool(done_at) or lc in _EDITED_LC
        approved = lc in _APPROVED_LC or (getattr(t, "qc_status", "") == "approved")
        published = lc in _PUBLISHED_LC or bool(getattr(t, "published_at", None))
        dl = getattr(t, "editor_deadline", None) or getattr(t, "deadline", None)
        st = getattr(t, "editing_started_at", None)
        ta = ((done_at - st).total_seconds() / 3600.0) if (st and done_at and done_at >= st) else None
        on_time = None
        if edited and dl and done_at:
            on_time = bool(done_at <= dl)
        q = getattr(t, "quality_rating", None) or getattr(t, "teacher_review_rating", None)
        fmt = get_video_format(getattr(t, "video_type", ""), getattr(t, "kind", ""),
                               getattr(t, "content_format", ""))
        overdue = bool(dl and dl < now and not (published or approved))
        items.append(_mk_item(
            id=t.id, source="normal", format=fmt, category=format_category(fmt),
            title=(t.title or getattr(t, "subject", "") or "Video"), task_id=t.id,
            edited=edited, approved=approved, published=published,
            pending=(lc in _OPEN_LC and not edited), overdue=overdue,
            revisions=int(getattr(t, "revision_count", 0) or 0),
            turnaround_hours=round(ta, 1) if ta is not None else None,
            on_time=on_time, has_deadline=bool(dl),
            quality=(int(q) if q else None), completed_at=done_at, deadline=dl, lifecycle=lc))

    # ---- project chapters (parent video_type/kind inherited) ----
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.editor_id == staff_id).all())
    pids = {c.task_id for c in chs}
    pmap = {}
    if pids:
        for p in db.query(VideoTask).filter(VideoTask.id.in_(list(pids))).all():
            pmap[p.id] = p
    import video_tasks as _vt
    for c in chs:
        p = pmap.get(c.task_id)
        if p is not None and getattr(p, "cancelled", False):
            continue
        try:
            lc = _vt._chapter_lifecycle(c)
        except Exception:
            lc = (getattr(c, "lifecycle", "") or "")
        done_at = getattr(c, "edited_at", None)
        edited = bool(done_at) or lc in _EDITED_LC or (getattr(c, "edit_state", "") == "edited")
        approved = lc in _APPROVED_LC or (getattr(c, "qc_status", "") == "approved")
        published = lc in _PUBLISHED_LC or bool(getattr(c, "youtube_url", "") or getattr(c, "published_at", None))
        dl = getattr(c, "deadline", None)
        st = getattr(c, "editing_started_at", None)
        ta = ((done_at - st).total_seconds() / 3600.0) if (st and done_at and done_at >= st) else None
        on_time = None
        if edited and dl and done_at:
            on_time = bool(done_at <= dl)
        q = getattr(c, "edit_review_rating", None)
        fmt = get_video_format(getattr(p, "video_type", "") if p else "",
                               getattr(p, "kind", "") if p else "",
                               getattr(p, "content_format", "") if p else "")
        overdue = bool(dl and dl < now and not (published or approved))
        items.append(_mk_item(
            id=c.id, source="project", format=fmt, category=format_category(fmt),
            title=(c.title or "Chapter"), task_id=c.task_id,
            edited=edited, approved=approved, published=published,
            pending=(lc in _OPEN_LC and not edited), overdue=overdue,
            revisions=int(getattr(c, "qc_revision", 0) or 0),
            turnaround_hours=round(ta, 1) if ta is not None else None,
            on_time=on_time, has_deadline=bool(dl),
            quality=(int(q) if q else None), completed_at=done_at, deadline=dl, lifecycle=lc))
    return items


def get_graphics_work_items(db, staff_id, now=None):
    """Normal GraphicsTask + project chapter thumbnails, normalized & de-duplicated."""
    from models import GraphicsTask, VideoTask, VideoTaskChapter
    now = now or datetime.utcnow()
    items = []

    gts = db.query(GraphicsTask).filter(GraphicsTask.graphics_id == staff_id).all()
    for g in gts:
        status = (g.status or "")
        approved = (status == "approved")
        submitted = bool(getattr(g, "submitted_at", None)) or status in ("submitted", "approved")
        done_at = getattr(g, "approved_at", None)
        dl = getattr(g, "deadline", None)
        st = getattr(g, "started_at", None)
        sub = getattr(g, "submitted_at", None)
        ta = ((done_at - st).total_seconds() / 3600.0) if (st and done_at and done_at >= st) else None
        on_time = None
        if approved and dl and (done_at or sub):
            on_time = bool((sub or done_at) <= dl)
        # format inherited from the parent VideoTask (thumbnail belongs to that video)
        fmt = FMT_LONG
        try:
            pt = db.query(VideoTask).filter(VideoTask.id == g.task_id).first()
            if pt:
                fmt = get_video_format(pt.video_type, pt.kind, getattr(pt, "content_format", ""))
        except Exception:
            pass
        items.append(_mk_item(
            id=g.id, source="normal", format=fmt, category=format_category(fmt),
            title="Thumbnail #%s" % g.id, task_id=g.task_id,
            edited=submitted, approved=approved, published=approved,
            pending=(status in ("new", "in_progress", "changes") and not approved),
            overdue=bool(dl and dl < now and not approved),
            revisions=int(getattr(g, "revision_count", 0) or 0),
            turnaround_hours=round(ta, 1) if ta is not None else None,
            on_time=on_time, has_deadline=bool(dl),
            quality=(int(g.quality_rating) if getattr(g, "quality_rating", None) else None),
            completed_at=done_at, deadline=dl, lifecycle=status))

    # project chapter thumbnails
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.graphics_id == staff_id).all())
    pids = {c.task_id for c in chs}
    pmap = {}
    if pids:
        for p in db.query(VideoTask).filter(VideoTask.id.in_(list(pids))).all():
            pmap[p.id] = p
    for c in chs:
        p = pmap.get(c.task_id)
        if p is not None and getattr(p, "cancelled", False):
            continue
        has_thumb = bool((getattr(c, "thumbnail_link", "") or "").strip())
        gfx_done = (getattr(c, "gfx_state", "") == "done") or has_thumb
        if not (has_thumb or getattr(c, "gfx_state", "")):
            continue  # not actually worked on
        done_at = getattr(c, "thumb_approved_at", None) or (getattr(c, "edited_at", None) if gfx_done else None)
        dl = getattr(c, "deadline", None)
        fmt = get_video_format(getattr(p, "video_type", "") if p else "",
                               getattr(p, "kind", "") if p else "",
                               getattr(p, "content_format", "") if p else "")
        on_time = None
        if gfx_done and dl and done_at:
            on_time = bool(done_at <= dl)
        items.append(_mk_item(
            id="c%s" % c.id, source="project", format=fmt, category=format_category(fmt),
            title=(c.title or "Chapter thumbnail"), task_id=c.task_id,
            edited=gfx_done, approved=gfx_done, published=gfx_done,
            pending=(not gfx_done),
            overdue=bool(dl and dl < now and not gfx_done),
            revisions=int(getattr(c, "thumb_revision", 0) or 0),
            turnaround_hours=None, on_time=on_time, has_deadline=bool(dl),
            quality=(int(getattr(c, "thumb_quality", 0)) if getattr(c, "thumb_quality", None) else None),
            completed_at=done_at, deadline=dl, lifecycle=(getattr(c, "gfx_state", "") or "")))
    return items


# ============================================================ SCORING
def _component(points_ratio, weight, available, detail=""):
    """One score component. points_ratio in [0,1] (None if N/A)."""
    if not available or points_ratio is None:
        return {"points": None, "max": weight, "available": False, "detail": detail}
    pr = max(0.0, min(1.0, points_ratio))
    return {"points": round(pr * weight, 1), "max": weight, "available": True, "detail": detail}


def _total_score(components):
    """Sum available component points, renormalized to 100 over available weights.
    N/A components are excluded (never a fake 0)."""
    got = sum(c["points"] for c in components.values() if c["available"])
    mx = sum(c["max"] for c in components.values() if c["available"])
    if mx <= 0:
        return None
    return round(got / mx * 100.0, 1)


def _completed_in_period(items, start, end):
    return [it for it in items if it["edited"] and it["completed_at"]
            and start <= it["completed_at"] < end]


def compute_category_perf(items, category, start, end, target):
    """Compute one category's (long OR short) metrics + score from work items."""
    cat_items = [it for it in items if it["category"] == category]
    completed = _completed_in_period(cat_items, start, end)
    n = len(completed)

    approved = sum(1 for it in completed if it["approved"])
    published = sum(1 for it in completed if it["published"])
    # pending/overdue are CURRENT-state (not period-bound)
    pending = sum(1 for it in cat_items if it["pending"])
    overdue = sum(1 for it in cat_items if it["overdue"])
    revisions = sum(it["revisions"] for it in completed)

    # quality (only rated completed items)
    rated = [it["quality"] for it in completed if it["quality"]]
    avg_quality = round(sum(rated) / len(rated), 2) if rated else None

    # on-time (only deadline-tracked completed)
    dl_items = [it for it in completed if it["on_time"] is not None]
    ontime_ct = sum(1 for it in dl_items if it["on_time"])
    on_time_pct = round(ontime_ct * 100 / len(dl_items)) if dl_items else None

    # first-pass (0 revisions among completed)
    first_pass_ct = sum(1 for it in completed if it["revisions"] == 0)
    first_pass_pct = round(first_pass_ct * 100 / n) if n else None

    # turnaround
    turns = [it["turnaround_hours"] for it in completed if it["turnaround_hours"] is not None]
    avg_turn = round(sum(turns) / len(turns), 1) if turns else None

    # consistency: distinct completion days
    days = {it["completed_at"].date() for it in completed if it["completed_at"]}
    consistency_ratio = (len(days) / float(CONSISTENCY_TARGET_DAYS)) if n else None

    # ---- score components ----
    comps = {
        "output": _component(min(n / float(target), OUTPUT_CAP) if target else None,
                             EDITOR_WEIGHTS["output"], n >= 0 and target > 0,
                             "%d of %d monthly target" % (n, target)),
        "quality": _component((avg_quality / QUALITY_SCALE) if avg_quality else None,
                              EDITOR_WEIGHTS["quality"], bool(rated),
                              ("Avg rating %.1f/5" % avg_quality) if avg_quality else "No ratings yet"),
        "on_time": _component((ontime_ct / len(dl_items)) if dl_items else None,
                              EDITOR_WEIGHTS["on_time"], bool(dl_items),
                              ("%d of %d on time" % (ontime_ct, len(dl_items))) if dl_items else "No deadlines"),
        "first_pass": _component((first_pass_ct / n) if n else None,
                                 EDITOR_WEIGHTS["first_pass"], n > 0,
                                 ("%d of %d first-pass" % (first_pass_ct, n)) if n else ""),
        "consistency": _component(min(consistency_ratio, 1.0) if consistency_ratio is not None else None,
                                  EDITOR_WEIGHTS["consistency"], n > 0,
                                  ("Worked %d distinct days" % len(days)) if n else ""),
    }
    score = _total_score(comps)
    provisional = n < MIN_SAMPLE.get(category, 0)
    return {
        "category": category, "edited": n, "approved": approved, "published": published,
        "pending": pending, "overdue": overdue, "revisions": revisions,
        "first_pass_pct": first_pass_pct, "avg_turnaround": avg_turn,
        "on_time_pct": on_time_pct, "avg_quality": avg_quality,
        "score": score, "score_breakdown": comps, "target": target,
        "provisional": provisional, "sample": n,
        "source_normal": sum(1 for it in completed if it["source"] == "normal"),
        "source_project": sum(1 for it in completed if it["source"] == "project"),
    }


def compute_editor_performance(sp, items, period="month", ref=None):
    start, end, plabel = period_bounds(period, ref)
    spec = resolve_specialization(sp)
    long_p = compute_category_perf(items, CAT_LONG, start, end, _target_for(sp, CAT_LONG))
    short_p = compute_category_perf(items, CAT_SHORT, start, end, _target_for(sp, CAT_SHORT))

    completed = _completed_in_period(items, start, end)
    overall = {
        "edited": long_p["edited"] + short_p["edited"],
        "approved": long_p["approved"] + short_p["approved"],
        "published": long_p["published"] + short_p["published"],
        "pending": long_p["pending"] + short_p["pending"],
        "overdue": long_p["overdue"] + short_p["overdue"],
        "revisions": long_p["revisions"] + short_p["revisions"],
        "normal_work": sum(1 for it in completed if it["source"] == "normal"),
        "project_work": sum(1 for it in completed if it["source"] == "project"),
    }
    # overall score: normalize available category scores (never sum raw output)
    cat_scores = [p["score"] for p in (long_p, short_p) if p["score"] is not None and p["edited"] > 0]
    overall["score"] = round(sum(cat_scores) / len(cat_scores), 1) if cat_scores else None
    # which category is this editor's "home" for default ranking
    if spec == SPEC_LONG:
        primary = CAT_LONG
    elif spec == SPEC_SHORT:
        primary = CAT_SHORT
    else:
        primary = CAT_LONG if long_p["edited"] >= short_p["edited"] else CAT_SHORT
    return {"period": plabel, "specialization": spec, "primary_category": primary,
            "overall": overall, "long": long_p, "short": short_p}


# ---- graphics scoring (own weights) ----
def compute_graphics_performance(sp, items, period="month", ref=None):
    start, end, plabel = period_bounds(period, ref)
    completed = _completed_in_period(items, start, end)
    n = len(completed)
    approved = sum(1 for it in completed if it["approved"])
    pending = sum(1 for it in items if it["pending"])
    overdue = sum(1 for it in items if it["overdue"])
    revisions = sum(it["revisions"] for it in completed)
    rated = [it["quality"] for it in completed if it["quality"]]
    avg_quality = round(sum(rated) / len(rated), 2) if rated else None
    dl_items = [it for it in completed if it["on_time"] is not None]
    ontime_ct = sum(1 for it in dl_items if it["on_time"])
    on_time_pct = round(ontime_ct * 100 / len(dl_items)) if dl_items else None
    first_ct = sum(1 for it in completed if it["revisions"] == 0)
    first_pass_pct = round(first_ct * 100 / n) if n else None
    days = {it["completed_at"].date() for it in completed if it["completed_at"]}
    consistency_ratio = (len(days) / float(CONSISTENCY_TARGET_DAYS)) if n else None
    tgt = int(getattr(sp, "target_thumbnails", None) or DEFAULT_TARGET_THUMBS)

    comps = {
        "output": _component(min(n / float(tgt), OUTPUT_CAP) if tgt else None,
                             GRAPHICS_WEIGHTS["output"], tgt > 0, "%d of %d target" % (n, tgt)),
        "quality": _component((avg_quality / QUALITY_SCALE) if avg_quality else None,
                              GRAPHICS_WEIGHTS["quality"], bool(rated),
                              ("Avg %.1f/5" % avg_quality) if avg_quality else "No ratings yet"),
        "first_pass": _component((first_ct / n) if n else None, GRAPHICS_WEIGHTS["first_pass"],
                                 n > 0, ("%d of %d first-time" % (first_ct, n)) if n else ""),
        "on_time": _component((ontime_ct / len(dl_items)) if dl_items else None,
                              GRAPHICS_WEIGHTS["on_time"], bool(dl_items),
                              ("%d of %d on time" % (ontime_ct, len(dl_items))) if dl_items else "No deadlines"),
        "consistency": _component(min(consistency_ratio, 1.0) if consistency_ratio is not None else None,
                                  GRAPHICS_WEIGHTS["consistency"], n > 0,
                                  ("Worked %d distinct days" % len(days)) if n else ""),
    }
    return {"period": plabel, "thumbnails": n, "approved": approved, "pending": pending,
            "overdue": overdue, "revisions": revisions, "avg_quality": avg_quality,
            "on_time_pct": on_time_pct, "first_pass_pct": first_pass_pct,
            "score": _total_score(comps), "score_breakdown": comps,
            "provisional": n < MIN_SAMPLE_GRAPHICS, "sample": n, "target": tgt,
            "normal_work": sum(1 for it in completed if it["source"] == "normal"),
            "project_work": sum(1 for it in completed if it["source"] == "project")}


# ============================================================ LEADERBOARDS (tie-broken)
def _tiebreak_key(row):
    # 1 score, 2 quality, 3 on_time, 4 first_pass, 5 output, 6 staff_id (stable)
    return (-(row["score"] or 0), -(row["avg_quality"] or 0), -(row["on_time_pct"] or 0),
            -(row["first_pass_pct"] or 0), -(row["edited"] or 0), row["staff_id"])


def compute_editor_leaderboard(db, category, period="month", ref=None):
    """One category's leaderboard (long OR short). Long editors + hybrids appear in long;
    short editors + hybrids appear in short. Fair, deterministic tie-break."""
    from models import ProductionStaffProfile
    start, end, _ = period_bounds(period, ref)
    eds = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "editor",
        ProductionStaffProfile.is_active == True).all()  # noqa: E712
    rows = []
    for e in eds:
        spec = resolve_specialization(e)
        if category == CAT_LONG and spec == SPEC_SHORT:
            continue
        if category == CAT_SHORT and spec == SPEC_LONG:
            continue
        items = get_editor_work_items(db, e.id)
        p = compute_category_perf(items, category, start, end, _target_for(e, category))
        rows.append({"staff_id": e.id, "name": (e.user.name if e.user else ""),
                     "score": p["score"], "edited": p["edited"], "approved": p["approved"],
                     "avg_quality": p["avg_quality"], "on_time_pct": p["on_time_pct"],
                     "first_pass_pct": p["first_pass_pct"], "provisional": p["provisional"],
                     "category": category})
    rows.sort(key=_tiebreak_key)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def compute_graphics_leaderboard(db, period="month", ref=None):
    from models import ProductionStaffProfile
    _ = period_bounds(period, ref)
    gfx = db.query(ProductionStaffProfile).filter(
        ProductionStaffProfile.staff_role == "graphics",
        ProductionStaffProfile.is_active == True).all()  # noqa: E712
    rows = []
    for d in gfx:
        items = get_graphics_work_items(db, d.id)
        p = compute_graphics_performance(d, items, period, ref)
        rows.append({"staff_id": d.id, "name": (d.user.name if d.user else ""),
                     "score": p["score"], "edited": p["thumbnails"], "approved": p["approved"],
                     "avg_quality": p["avg_quality"], "on_time_pct": p["on_time_pct"],
                     "first_pass_pct": p["first_pass_pct"], "provisional": p["provisional"],
                     "category": "graphics"})
    rows.sort(key=_tiebreak_key)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def find_rank(rows, staff_id):
    for r in rows:
        if r["staff_id"] == staff_id:
            return r["rank"], len(rows)
    return None, len(rows)


# ============================================================ RANK SNAPSHOTS + MOVEMENT
def _period_key(ref=None):
    ref = ref or datetime.utcnow()
    return ref.strftime("%Y-%m")


def save_rank_snapshot(db, staff_id, staff_role, category, rank, score, total_ranked, ref=None):
    """Idempotent: at most one snapshot per (staff, category, day). Updates same-day."""
    from models import ProductionRankSnapshot
    ref = ref or datetime.utcnow()
    today = ref.date()
    pk = _period_key(ref)
    try:
        existing = (db.query(ProductionRankSnapshot)
                    .filter(ProductionRankSnapshot.staff_id == staff_id,
                            ProductionRankSnapshot.category == category,
                            ProductionRankSnapshot.snapshot_date == today).first())
        if existing:
            existing.rank = rank
            existing.score = score
            existing.total_ranked = total_ranked
        else:
            db.add(ProductionRankSnapshot(
                staff_id=staff_id, staff_role=staff_role, category=category,
                period=pk, snapshot_date=today, rank=rank, score=score,
                total_ranked=total_ranked, created_at=ref))
        db.commit()
    except Exception:
        db.rollback()


def rank_movement(db, staff_id, category, current_rank, ref=None):
    """Movement vs the most recent EARLIER snapshot. +ve = moved UP (toward #1)."""
    from models import ProductionRankSnapshot
    ref = ref or datetime.utcnow()
    today = ref.date()
    out = {"current_rank": current_rank, "previous_rank": None, "movement": 0,
           "movement_direction": "same", "best_rank": current_rank, "worst_rank": current_rank,
           "times_rank_up": 0, "times_rank_down": 0, "days_at_rank_1": 0,
           "highest_month_score": None}
    try:
        snaps = (db.query(ProductionRankSnapshot)
                 .filter(ProductionRankSnapshot.staff_id == staff_id,
                         ProductionRankSnapshot.category == category,
                         ProductionRankSnapshot.period == _period_key(ref))
                 .order_by(ProductionRankSnapshot.snapshot_date.asc()).all())
        prior = [s for s in snaps if s.snapshot_date < today]
        if prior:
            prev = prior[-1].rank
            out["previous_rank"] = prev
            if current_rank is not None and prev is not None:
                out["movement"] = prev - current_rank  # rank 3 -> 2 => +1 (up)
                out["movement_direction"] = ("up" if out["movement"] > 0
                                             else "down" if out["movement"] < 0 else "same")
        ranks = [s.rank for s in snaps if s.rank] + ([current_rank] if current_rank else [])
        if ranks:
            out["best_rank"] = min(ranks)
            out["worst_rank"] = max(ranks)
        out["days_at_rank_1"] = sum(1 for s in snaps if s.rank == 1) + (1 if current_rank == 1 else 0)
        for a, b in zip(snaps, snaps[1:]):
            if a.rank and b.rank:
                if b.rank < a.rank:
                    out["times_rank_up"] += 1
                elif b.rank > a.rank:
                    out["times_rank_down"] += 1
        scores = [s.score for s in snaps if s.score is not None]
        out["highest_month_score"] = max(scores) if scores else None
    except Exception:
        pass
    return out


def snapshot_all(db, ref=None):
    """Snapshot EVERY ranked editor/graphics for today (idempotent). So rank movement
    history is complete even for staff who never open their own page (perf §21)."""
    ref = ref or datetime.utcnow()
    try:
        for cat, snapcat in ((CAT_LONG, "editor_long"), (CAT_SHORT, "editor_short")):
            lb = compute_editor_leaderboard(db, cat, "month", ref=ref)
            tot = len(lb)
            for r in lb:
                save_rank_snapshot(db, r["staff_id"], "editor", snapcat, r["rank"],
                                   (r["score"] or 0), tot, ref=ref)
        glb = compute_graphics_leaderboard(db, "month", ref=ref)
        gt = len(glb)
        for r in glb:
            save_rank_snapshot(db, r["staff_id"], "graphics", "graphics", r["rank"],
                               (r["score"] or 0), gt, ref=ref)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def maybe_daily_snapshot(db, ref=None):
    """Self-healing daily job (no cron needed): the FIRST performance read each day
    snapshots everyone once. Cheap guard (one count), idempotent writes."""
    from models import ProductionRankSnapshot
    ref = ref or datetime.utcnow()
    try:
        exists = (db.query(ProductionRankSnapshot)
                  .filter(ProductionRankSnapshot.snapshot_date == ref.date()).first())
        if exists:
            return False
        snapshot_all(db, ref)
        return True
    except Exception:
        return False


def rank_trend(db, staff_id, category, days=30, ref=None):
    from models import ProductionRankSnapshot
    ref = ref or datetime.utcnow()
    try:
        since = (ref - timedelta(days=days)).date()
        snaps = (db.query(ProductionRankSnapshot)
                 .filter(ProductionRankSnapshot.staff_id == staff_id,
                         ProductionRankSnapshot.category == category,
                         ProductionRankSnapshot.snapshot_date >= since)
                 .order_by(ProductionRankSnapshot.snapshot_date.asc()).all())
        return [{"label": s.snapshot_date.strftime("%d %b"), "rank": s.rank,
                 "score": s.score} for s in snaps]
    except Exception:
        return []


# ============================================================ BADGES
def editor_badges(perf):
    out = []
    lp, spt = perf["long"], perf["short"]
    best = max([p["score"] or 0 for p in (lp, spt)] + [0])
    if best >= BADGE_CFG["top_performer_score"]:
        out.append("Top Performer")
    quals = [p["avg_quality"] for p in (lp, spt) if p["avg_quality"]]
    if quals and max(quals) >= BADGE_CFG["quality_champion"]:
        out.append("Quality Champion")
    onts = [p["on_time_pct"] for p in (lp, spt) if p["on_time_pct"] is not None]
    if onts and max(onts) >= BADGE_CFG["deadline_master_ontime"]:
        out.append("Deadline Master")
    fps = [p["first_pass_pct"] for p in (lp, spt) if p["first_pass_pct"] is not None]
    if fps and max(fps) >= BADGE_CFG["first_pass_pro"]:
        out.append("First-Pass Pro")
    if perf["overall"].get("project_work", 0) >= 3:
        out.append("Project Specialist")
    if lp["edited"] >= BADGE_CFG["long_10"]:
        out.append("10+ Long Videos")
    if spt["edited"] >= BADGE_CFG["short_20"]:
        out.append("20+ Shorts")
    return out


def filter_work_items(items, period, category, filt, ref=None):
    """Drill-down list behind a clickable metric (perf §31)."""
    start, end, _ = period_bounds(period, ref)
    rows = items
    if category in (CAT_LONG, CAT_SHORT):
        rows = [it for it in rows if it["category"] == category]

    def comp(it):
        return it["edited"] and it["completed_at"] and start <= it["completed_at"] < end
    f = (filt or "edited")
    if f == "pending":
        rows = [it for it in rows if it["pending"]]
    elif f == "overdue":
        rows = [it for it in rows if it["overdue"]]
    elif f == "approved":
        rows = [it for it in rows if comp(it) and it["approved"]]
    elif f == "published":
        rows = [it for it in rows if comp(it) and it["published"]]
    elif f == "late":
        rows = [it for it in rows if comp(it) and it["on_time"] is False]
    elif f == "revisions":
        rows = [it for it in rows if comp(it) and it["revisions"] > 0]
    elif f == "project":
        rows = [it for it in rows if comp(it) and it["source"] == "project"]
    elif f == "normal":
        rows = [it for it in rows if comp(it) and it["source"] == "normal"]
    else:
        rows = [it for it in rows if comp(it)]
    rows.sort(key=lambda it: (it["completed_at"] or datetime.min), reverse=True)
    return rows


def item_dto(it):
    return {"title": it["title"], "source": it["source"], "format": it["format"],
            "category": it["category"],
            "completed_at": it["completed_at"].strftime("%d %b %Y") if it["completed_at"] else None,
            "turnaround_hours": it["turnaround_hours"], "revisions": it["revisions"],
            "quality": it["quality"], "on_time": it["on_time"], "state": it["lifecycle"],
            "task_id": it["task_id"], "has_deadline": it["has_deadline"]}


def personal_bests(db, staff_id, category, ref=None):
    """Real personal records from persisted snapshots (perf §34) — never fabricated."""
    from models import ProductionRankSnapshot
    out = {"best_rank": None, "best_score": None, "longest_rank1_streak": 0}
    try:
        snaps = (db.query(ProductionRankSnapshot)
                 .filter(ProductionRankSnapshot.staff_id == staff_id,
                         ProductionRankSnapshot.category == category)
                 .order_by(ProductionRankSnapshot.snapshot_date.asc()).all())
        ranks = [s.rank for s in snaps if s.rank]
        scores = [s.score for s in snaps if s.score is not None]
        if ranks:
            out["best_rank"] = min(ranks)
        if scores:
            out["best_score"] = round(max(scores), 1)
        streak = 0
        best = 0
        prevd = None
        for s in snaps:
            if s.rank == 1:
                if prevd and (s.snapshot_date - prevd).days == 1:
                    streak += 1
                else:
                    streak = 1
                best = max(best, streak)
            else:
                streak = 0
            prevd = s.snapshot_date
        out["longest_rank1_streak"] = best
    except Exception:
        pass
    return out


def graphics_badges(perf):
    out = []
    if (perf["score"] or 0) >= BADGE_CFG["top_performer_score"]:
        out.append("Top Performer")
    if perf["avg_quality"] and perf["avg_quality"] >= BADGE_CFG["quality_champion"]:
        out.append("Quality Champion")
    if perf["first_pass_pct"] is not None and perf["first_pass_pct"] >= BADGE_CFG["first_pass_pro"]:
        out.append("First-Pass Pro")
    if perf["on_time_pct"] is not None and perf["on_time_pct"] >= BADGE_CFG["deadline_master_ontime"]:
        out.append("Deadline Master")
    if perf.get("project_work", 0) >= 3:
        out.append("Project Specialist")
    if perf["thumbnails"] >= BADGE_CFG["long_10"]:
        out.append("10+ Thumbnails")
    return out
