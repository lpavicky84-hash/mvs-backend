"""
Academic Calendar — session-wise NIOS exam schedule.

- Admin defines tentative dates per exam session ("April 2027", "October 2027" ...).
- A new session is auto-generated from a fixed template (the same tentative timeline,
  shifted by the session/year), then stays fully editable by the admin.
- Students see ONLY their own session (resolved from StudentProfile.exam_session),
  with a live progress timeline, avatar, countdown and tentative-date reminders.
- Teachers see all sessions, read-only.

All dates are plain ISO date strings ("YYYY-MM-DD") inside the JSON `data` column, so there
is zero timezone ambiguity. "Today" for progress/reminders uses IST (ist_today()).
"""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
import re

from database import get_db
from security import get_student, get_teacher, get_admin
from models import (
    AcademicCalendar, AcademicCalReminderLog, StudentProfile, User, Batch,
    StudentBatch, Notification, ist_today,
)

router = APIRouter(prefix="/api", tags=["Academic Calendar"])

# ---------------------------------------------------------------------------
# Milestone definitions (fixed order + labels). Dates come from the template.
# kind: normal | window (start..end) | exam (has hall_ticket) | last_date | result
# ---------------------------------------------------------------------------
MILESTONES = [
    {"key": "tma_questions",       "title": "TMA Questions",              "subtitle": "Tutor Marked Assignment questions released",            "kind": "normal"},
    {"key": "tma_submission",      "title": "TMA Submission",             "subtitle": "Online submission commences",                           "kind": "normal"},
    {"key": "tma_last_date",       "title": "Last Date for TMA Submission","subtitle": "Final deadline to submit your assignments",             "kind": "last_date"},
    {"key": "exam_fee",            "title": "Exam Fee",                   "subtitle": "Examination fee payment window",                        "kind": "window"},
    {"key": "pcp_classes",         "title": "PCP Classes",                "subtitle": "Attend 5 PCP classes for practical subjects (10 PCP marks)","kind": "normal"},
    {"key": "practical_exam",      "title": "Practical Examination",      "subtitle": "Practical examinations commence",                       "kind": "exam"},
    {"key": "theory_exam",         "title": "Theory Examination",         "subtitle": "Theory examinations commence",                          "kind": "exam"},
    {"key": "final_result",        "title": "Final Result",               "subtitle": "Result to be declared around",                          "kind": "result"},
    {"key": "marksheet_digilocker","title": "Marksheet on DigiLocker",    "subtitle": "Digital marksheet available on DigiLocker",             "kind": "result"},
    {"key": "original_marksheet",  "title": "Original Marksheet",         "subtitle": "Original marksheet dispatched by post",                 "kind": "result"},
]
_MILE_META = {m["key"]: m for m in MILESTONES}

DEFAULT_YOUTUBE = "https://www.youtube.com/@ManishVermaOfficial"
DEFAULT_NOTE = ("All dates shown are tentative. Confirmed dates are announced on our official "
                "Manish Verma Official channel — stay connected and keep preparing.")

_MON3 = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
         7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
_MON_FULL = {"jan": "January", "feb": "February", "mar": "March", "apr": "April",
             "may": "May", "jun": "June", "jul": "July", "aug": "August",
             "sep": "September", "oct": "October", "nov": "November", "dec": "December"}


def _iso(y, m, d):
    return date(y, m, d).isoformat()


# ---------------------------------------------------------------------------
# Session parsing -> canonical "April 2027" / "October 2027"
# ---------------------------------------------------------------------------
def canon_session(s):
    """Any session value -> canonical 'Month YYYY' (e.g. 'April 2027'). '' if not parseable."""
    s = (s or "").strip()
    if not s:
        return ""
    t = re.sub(r"[\s \-_/.,]+", " ", s.lower()).strip()
    mon = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    m = re.search(r"\b" + mon + r"\s*(20\d{2})\b", t)
    if not m:
        m = re.search(r"\b(20\d{2})\s*" + mon + r"\b", t)
        if m:
            return _MON_FULL[m.group(2)[:3]] + " " + m.group(1)
        return ""
    return _MON_FULL[m.group(1)[:3]] + " " + m.group(2)


def session_parts(session):
    """'April 2027' -> ('April', 2027). Unknown -> ('', 0)."""
    c = canon_session(session)
    if not c:
        return "", 0
    word, yr = c.split(" ")
    return word, int(yr)


# ---------------------------------------------------------------------------
# Template: default tentative dates for a session type + exam year.
# April session = NIOS April <year> examination. October session = October <year>.
# ---------------------------------------------------------------------------
def default_milestones(session_type, year):
    st = (session_type or "").strip().lower()
    year = int(year)
    out = []

    def add(key, date_iso=None, end_date=None, hall_ticket=None, date_label=None, subtitle=None):
        meta = _MILE_META[key]
        out.append({
            "key": key,
            "title": meta["title"],
            "subtitle": subtitle if subtitle is not None else meta["subtitle"],
            "kind": meta["kind"],
            "date": date_iso,
            "end_date": end_date,
            "hall_ticket": hall_ticket,
            "date_label": date_label,
        })

    if st == "april":
        y = year
        add("tma_questions",        _iso(y - 1, 9, 15), subtitle="Session %d–%02d questions released" % (y - 1, y % 100))
        add("tma_submission",       _iso(y - 1, 11, 15))
        add("tma_last_date",        _iso(y, 1, 31))
        add("exam_fee",             _iso(y - 1, 11, 21), end_date=_iso(y - 1, 12, 20))
        add("pcp_classes",          _iso(y, 2, 1))
        add("practical_exam",       _iso(y, 3, 15), hall_ticket=_iso(y, 3, 10))
        add("theory_exam",          _iso(y, 4, 10), hall_ticket=_iso(y, 3, 30))
        add("final_result",         _iso(y, 6, 7))
        add("marksheet_digilocker", _iso(y, 6, 15))
        add("original_marksheet",   _iso(y, 7, 31), date_label="End of July %d" % y)
    else:  # october (default)
        y = year
        add("tma_questions",        None, date_label="Available now",
            subtitle="Session %d–%02d questions released" % (y - 1, y % 100))
        add("tma_submission",       _iso(y, 5, 15))
        add("tma_last_date",        _iso(y, 7, 31))
        add("exam_fee",             _iso(y, 5, 21), end_date=_iso(y, 6, 20))
        add("pcp_classes",          _iso(y, 8, 1))
        add("practical_exam",       _iso(y, 9, 14), hall_ticket=_iso(y, 9, 8))
        add("theory_exam",          _iso(y, 10, 14), hall_ticket=_iso(y, 10, 5))
        add("final_result",         _iso(y + 1, 1, 20))
        add("marksheet_digilocker", _iso(y + 1, 1, 30))
        add("original_marksheet",   _iso(y + 1, 3, 5))
    return out


def _get_or_create(db, session_label, create=True):
    """Fetch a calendar by canonical session, auto-creating from template if missing."""
    canon = canon_session(session_label)
    if not canon:
        return None
    cal = db.query(AcademicCalendar).filter(AcademicCalendar.session == canon).first()
    if cal or not create:
        return cal
    st, yr = session_parts(canon)
    if not st or not yr:
        return None
    cal = AcademicCalendar(
        session=canon, session_type=st, year=yr,
        data=default_milestones(st, yr),
        youtube_url=DEFAULT_YOUTUBE, note="",
    )
    db.add(cal)
    db.commit()
    db.refresh(cal)
    return cal


# ---------------------------------------------------------------------------
# Display helpers + progress state
# ---------------------------------------------------------------------------
def _pd(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except Exception:
        return None


def _fmt(iso):
    d = _pd(iso)
    if not d:
        return ""
    return "%d %s %d" % (d.day, _MON3[d.month], d.year)


def _fmt_long(iso):
    d = _pd(iso)
    if not d:
        return ""
    return "%d %s %d" % (d.day, _MON_FULL[_MON3[d.month].lower()], d.year)


def compute_state(cal, today=None):
    """Attach status + display dates to each milestone and compute hero/next/progress."""
    today = today or ist_today()
    mi = []
    for m in (cal.data or []):
        d = _pd(m.get("date"))
        end = _pd(m.get("end_date")) or d
        mi.append({**m, "_d": d, "_end": end})

    dated = [m for m in mi if m["_d"]]
    done, future = [], []
    for m in dated:
        if m["_end"] and m["_end"] < today:
            done.append(m)
        else:
            future.append(m)
    future.sort(key=lambda m: m["_d"])
    current = future[0] if future else None
    nxt = future[1] if len(future) > 1 else None

    def status_for(m):
        if current and m["key"] == current["key"]:
            return "current"
        if m["_end"] and m["_end"] < today:
            return "done"
        if nxt and m["key"] == nxt["key"]:
            return "next"
        if not m["_d"]:
            return "done"  # dateless (e.g. released) -> treated as completed
        return "upcoming"

    out_mi = []
    for m in mi:
        st = status_for(m)
        date_disp = m.get("date_label") or _fmt(m.get("date"))
        sub = m.get("subtitle") or ""
        extra = ""
        if m.get("kind") == "window" and m.get("end_date"):
            extra = "%s – %s" % (_fmt(m.get("date")), _fmt(m.get("end_date")))
        if m.get("kind") == "exam" and m.get("hall_ticket"):
            sub = "Hall ticket to be issued by %s" % _fmt_long(m.get("hall_ticket"))
        out_mi.append({
            "key": m["key"], "title": m["title"], "subtitle": sub, "kind": m.get("kind"),
            "date": m.get("date"), "end_date": m.get("end_date"), "hall_ticket": m.get("hall_ticket"),
            "date_display": date_disp, "range_display": extra, "status": st,
            "last_date": None,
        })

    # TMA submission: surface the last-date milestone inline as a red tag too
    last_tma = next((m for m in out_mi if m["key"] == "tma_last_date"), None)
    if last_tma and last_tma.get("date"):
        sub_m = next((m for m in out_mi if m["key"] == "tma_submission"), None)
        if sub_m:
            sub_m["last_date"] = _fmt_long(last_tma["date"])

    total = len(mi)
    done_ct = len([m for m in out_mi if m["status"] == "done"])

    hero = None
    if done:
        done.sort(key=lambda m: m["_d"])
        hero = {"title": done[-1]["title"], "key": done[-1]["key"]}
    elif current:
        hero = {"title": current["title"], "key": current["key"]}

    next_info = None
    if current:
        days = (current["_d"] - today).days
        window_active = current.get("_end") and current["_d"] <= today <= current["_end"]
        if window_active:
            label, when = "Ongoing now", "Open until %s" % _fmt_long(current.get("end_date"))
            days = 0
        elif days <= 0:
            label, when = "Starts today", "%s" % _fmt_long(current.get("date"))
            days = 0
        else:
            label, when = ("Next: %s begins" % current["title"]), "Starts %s" % _fmt_long(current.get("date"))
        next_info = {"key": current["key"], "title": current["title"],
                     "days_left": days, "label": label, "when": when,
                     "date_display": current.get("date_label") or _fmt(current.get("date"))}

    return {
        "milestones": out_mi,
        "hero": hero,
        "next": next_info,
        "progress": {"done": done_ct, "total": total,
                     "pct": int(round(100.0 * done_ct / total)) if total else 0},
        "today": today.isoformat(),
    }


def _cal_public(cal, with_state=True):
    out = {
        "session": cal.session, "session_type": cal.session_type, "year": cal.year,
        "youtube_url": cal.youtube_url or DEFAULT_YOUTUBE,
        "note": cal.note or DEFAULT_NOTE,
        "milestones": cal.data or [],
    }
    if with_state:
        out.update(compute_state(cal))
    return out


# ---------------------------------------------------------------------------
# Reminders — 2 days / 1 day before + on the start day of each dated milestone.
# Idempotent via AcademicCalReminderLog. Runs at startup and (throttled) on hits.
# ---------------------------------------------------------------------------
_last_run_day = {"d": None}


def _students_for_session(db, canon):
    """user ids of students whose exam_session resolves to this canonical session."""
    uids = []
    rows = db.query(StudentProfile.user_id, StudentProfile.exam_session).all()
    for uid, es in rows:
        if uid and canon_session(es) == canon:
            uids.append(uid)
    return uids


def run_calendar_reminders(db, force=False):
    today = ist_today()
    if not force and _last_run_day["d"] == today:
        return 0
    _last_run_day["d"] = today
    sent = 0
    try:
        cals = db.query(AcademicCalendar).all()
    except Exception:
        return 0
    session_uids = {}
    for cal in cals:
        for m in (cal.data or []):
            d = _pd(m.get("date"))
            if not d:
                continue
            for offset in (2, 1, 0):
                if (d - timedelta(days=offset)) != today:
                    continue
                rkey = "%s|%s|%d|%s" % (cal.session, m.get("key"), offset, today.isoformat())
                exists = db.query(AcademicCalReminderLog).filter(
                    AcademicCalReminderLog.rkey == rkey).first()
                if exists:
                    continue
                if cal.session not in session_uids:
                    session_uids[cal.session] = _students_for_session(db, cal.session)
                uids = session_uids[cal.session]
                title, msg = _reminder_text(m, offset, d)
                for uid in uids:
                    db.add(Notification(user_id=uid, title=title, message=msg,
                                        notif_type="academic_calendar", link="acalendar"))
                    sent += 1
                db.add(AcademicCalReminderLog(rkey=rkey))
                db.commit()
    return sent


def _reminder_text(m, offset, d):
    title_m = m.get("title") or "Academic milestone"
    when = _fmt_long(m.get("date"))
    if offset == 2:
        title = "%s begins in 2 days" % title_m
        lead = "is tentatively scheduled to begin in 2 days"
    elif offset == 1:
        title = "%s begins tomorrow" % title_m
        lead = "is tentatively scheduled to begin tomorrow"
    else:
        title = "%s begins today" % title_m
        lead = "tentatively begins today"
    msg = ("Reminder: %s %s (%s). Please note this is a tentative date — confirmed dates are "
           "announced on our official Manish Verma Official channel. Stay connected and keep "
           "preparing." % (title_m, lead, when))
    return title, msg


def _maybe_run_reminders(db):
    try:
        run_calendar_reminders(db)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# ===========================================================================
# STUDENT
# ===========================================================================
@router.get("/student/academic-calendar")
def student_calendar(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
    if not sp:
        raise HTTPException(404, "Student profile not found")
    raw = sp.exam_session or ""
    canon = canon_session(raw)
    if not canon:
        # fall back to the student's batch session, if any
        try:
            bid = sp.batch_id or sp.active_batch_id
            if bid:
                b = db.query(Batch).filter(Batch.id == bid).first()
                if b:
                    canon = canon_session(getattr(b, "session", ""))
        except Exception:
            canon = ""
    if not canon:
        return {"has_session": False,
                "message": "Your exam session has not been set yet. Please contact the MVS Foundation office so your academic calendar can be shown here."}
    cal = _get_or_create(db, canon)
    if not cal:
        return {"has_session": False,
                "message": "Your exam session (%s) could not be matched to a calendar yet. Please contact the office." % canon}
    _maybe_run_reminders(db)
    out = _cal_public(cal)
    out["has_session"] = True
    out["has_photo"] = bool(getattr(sp, "photo_b64", None))
    out["student_name"] = (current_user.name if getattr(current_user, "name", None) else "")
    return out


# ===========================================================================
# TEACHER (read-only, all sessions)
# ===========================================================================
@router.get("/teacher/academic-calendars")
def teacher_calendars(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    cals = db.query(AcademicCalendar).order_by(AcademicCalendar.year.desc()).all()
    cals = _sorted_cals(cals)
    return {"sessions": [{"session": c.session, "session_type": c.session_type, "year": c.year}
                         for c in cals]}


@router.get("/teacher/academic-calendar/{session}")
def teacher_calendar_one(session: str, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    cal = _get_or_create(db, session, create=False)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    return _cal_public(cal)


# ===========================================================================
# ADMIN (full control)
# ===========================================================================
def _sorted_cals(cals):
    order = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
             "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}
    return sorted(cals, key=lambda c: (-(c.year or 0), -order.get(c.session_type, 0)))


@router.get("/admin/academic-calendars")
def admin_calendars(db: Session = Depends(get_db), _=Depends(get_admin)):
    cals = _sorted_cals(db.query(AcademicCalendar).all())
    # student counts per session (one scan)
    counts = {}
    for (es,) in db.query(StudentProfile.exam_session).all():
        c = canon_session(es)
        if c:
            counts[c] = counts.get(c, 0) + 1
    _maybe_run_reminders(db)
    return {"sessions": [{"session": c.session, "session_type": c.session_type, "year": c.year,
                          "youtube_url": c.youtube_url or DEFAULT_YOUTUBE,
                          "milestones": len(c.data or []),
                          "students": counts.get(c.session, 0)} for c in cals],
            "known_student_sessions": sorted([k for k in counts.keys()]),
            "next_suggestion": _next_suggestion(db)}


@router.get("/admin/academic-calendar/{session}")
def admin_calendar_one(session: str, db: Session = Depends(get_db), _=Depends(get_admin)):
    cal = _get_or_create(db, session, create=False)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    return _cal_public(cal)


@router.post("/admin/academic-calendar")
def admin_create_calendar(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    st = (payload.get("session_type") or "").strip().title()
    yr = payload.get("year")
    # also accept a single "session" string like "April 2027"
    if (not st or not yr) and payload.get("session"):
        st, yr = session_parts(payload.get("session"))
    try:
        yr = int(yr)
    except Exception:
        raise HTTPException(400, "A valid exam year is required")
    if st not in ("April", "October"):
        raise HTTPException(400, "Session type must be April or October")
    canon = "%s %d" % (st, yr)
    if db.query(AcademicCalendar).filter(AcademicCalendar.session == canon).first():
        raise HTTPException(409, "Calendar for %s already exists" % canon)
    cal = AcademicCalendar(session=canon, session_type=st, year=yr,
                           data=default_milestones(st, yr),
                           youtube_url=DEFAULT_YOUTUBE, note="")
    db.add(cal)
    db.commit()
    db.refresh(cal)
    return _cal_public(cal)


@router.put("/admin/academic-calendar/{session}")
def admin_update_calendar(session: str, payload: dict = Body(...),
                          db: Session = Depends(get_db), _=Depends(get_admin)):
    cal = _get_or_create(db, session, create=False)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    if "milestones" in payload and isinstance(payload["milestones"], list):
        cal.data = _sanitize_milestones(payload["milestones"], cal.data or [])
    if "youtube_url" in payload:
        cal.youtube_url = (payload.get("youtube_url") or "").strip()
    if "note" in payload:
        cal.note = (payload.get("note") or "").strip()
    db.commit()
    db.refresh(cal)
    return _cal_public(cal)


@router.delete("/admin/academic-calendar/{session}")
def admin_delete_calendar(session: str, db: Session = Depends(get_db), _=Depends(get_admin)):
    cal = _get_or_create(db, session, create=False)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    db.delete(cal)
    db.commit()
    return {"ok": True}


@router.post("/admin/academic-calendar/{session}/reset")
def admin_reset_calendar(session: str, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Restore a session's dates to the default template."""
    cal = _get_or_create(db, session, create=False)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    cal.data = default_milestones(cal.session_type, cal.year)
    db.commit()
    db.refresh(cal)
    return _cal_public(cal)


def _sanitize_milestones(incoming, existing):
    """Keep the canonical milestone set/order; accept only date fields from the client."""
    by_key = {m.get("key"): m for m in incoming if isinstance(m, dict)}
    ex_by_key = {m.get("key"): m for m in existing}
    out = []
    for meta in MILESTONES:
        k = meta["key"]
        src = by_key.get(k, ex_by_key.get(k, {}))
        out.append({
            "key": k,
            "title": meta["title"],
            "subtitle": (src.get("subtitle") or ex_by_key.get(k, {}).get("subtitle") or meta["subtitle"]),
            "kind": meta["kind"],
            "date": _clean_date(src.get("date")),
            "end_date": _clean_date(src.get("end_date")) if meta["kind"] == "window" else None,
            "hall_ticket": _clean_date(src.get("hall_ticket")) if meta["kind"] == "exam" else None,
            "date_label": (src.get("date_label") or ex_by_key.get(k, {}).get("date_label")),
        })
    return out


def _clean_date(v):
    if not v:
        return None
    if _pd(v):
        return v
    return None


def _next_suggestion(db):
    """Suggest the next session to create (continues the April/October cadence)."""
    cals = db.query(AcademicCalendar).all()
    have = {c.session for c in cals}
    today = ist_today()
    # candidate order: upcoming April/October sessions not yet created
    cands = []
    for y in range(today.year, today.year + 4):
        for st in ("April", "October"):
            cands.append((st, y))
    for st, y in cands:
        lab = "%s %d" % (st, y)
        if lab not in have:
            # only suggest sessions whose exam month is the current month or later
            exam_month = 4 if st == "April" else 10
            if date(y, exam_month, 1) >= date(today.year, today.month, 1):
                return {"session_type": st, "year": y, "session": lab}
    return None
