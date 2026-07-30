# video_tasks.py — VIDEO TASK MANAGER
# Production manager (admin) -> teacher video tasks: assign with thumbnail + channel +
# deadline, teacher shoots & submits drive link, admin reviews (Approved / Editing Soon /
# Editing Done / Uploaded / Rejected+reshoot), stats + ranking + CSV report + student notify.
# v71: status history timeline, admin edit, auto One Shot (per subject chapters) aur
# Rapid Revision (per subject link) special tasks — no approval, progress tracking.
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import text, or_

from database import get_db, engine
from security import get_admin, get_teacher
from models import User, TeacherProfile, Notification, VideoChannel, VideoTask, VideoType, VideoTaskChapter

router = APIRouter(prefix="/api", tags=["Video Task Manager"])

# ===== SELF-HEALING SCHEMA FIX (thumbnail_b64) =====
# Purane deploys mein video_tasks.thumbnail_b64 MySQL TEXT (64KB) bana tha — bada
# thumbnail dalte hi "Data too long for column" (error 1406) aata tha. main.py ke
# ensure_columns ke alawa YE FILE KHUD bhi import pe column MEDIUMTEXT kar deti hai,
# taaki sirf video_tasks.py deploy + restart karne se bhi permanent fix lag jaye.
# Idempotent (har boot pe run hota hai, same type pe no-op). SQLite pe skip.
def _ensure_thumbnail_column():
    try:
        if engine.dialect.name != "mysql":
            return
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE video_tasks MODIFY thumbnail_b64 MEDIUMTEXT"))
            conn.commit()
        print("[video_tasks] thumbnail_b64 ensured MEDIUMTEXT")
    except Exception as e:
        print("[video_tasks] thumbnail_b64 MEDIUMTEXT migration skipped:", e)


_ensure_thumbnail_column()


def _ensure_vtype_column():
    """video_tasks.video_type column — purane deploys pe best-effort ADD COLUMN."""
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE video_tasks ADD COLUMN video_type VARCHAR(120) DEFAULT ''"))
            conn.commit()
        print("[video_tasks] video_type column added")
    except Exception as e:
        print("[video_tasks] video_type column check skipped:", e)


_ensure_vtype_column()


def _ensure_special_columns():
    """v71 columns (kind/subject/status_history/last_link_at/admin_seen_at) +
    video_task_chapters table — purane deploys pe best-effort self-heal."""
    alters = [
        "ALTER TABLE video_tasks ADD COLUMN kind VARCHAR(20) DEFAULT 'normal'",
        "ALTER TABLE video_tasks ADD COLUMN subject VARCHAR(160) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN status_history TEXT NULL",
        "ALTER TABLE video_tasks ADD COLUMN last_link_at DATETIME NULL",
        "ALTER TABLE video_tasks ADD COLUMN admin_seen_at DATETIME NULL",
    ]
    for ddl in alters:
        try:
            with engine.connect() as conn:
                conn.execute(text(ddl))
                conn.commit()
            print("[video_tasks] special column added:", ddl.split("ADD COLUMN")[1].split()[0])
        except Exception:
            pass
    try:
        VideoTaskChapter.__table__.create(engine, checkfirst=True)
        print("[video_tasks] video_task_chapters table ready")
    except Exception as e:
        print("[video_tasks] video_task_chapters create skipped:", e)


_ensure_special_columns()

DEFAULT_CHANNELS = [
    "Manish Verma Official - Main Channel",
    "Manish Verma",
    "MVS Science",
    "MVS Commerce",
    "MVS Arts",
    "MVS Class 10th",
    "Manish Verma Shorts",
    "Dignity",
    "Dignity 11th & 12th",
]

DEFAULT_TYPES = ["Short Video", "Long Video", "One Shot Video", "Strategy Video"]

REVIEW_ACTIONS = ("approved", "editing_soon", "editing_done", "uploaded", "rejected")

# =============================================================
# SPECIAL TASKS — One Shot (per subject, chapters auto) + Rapid Revision
# (per subject, ek link). Approval NAHI chahiye; teacher chapter/subject ke
# saamne link lagata hai, progress auto. Chapters syllabus manager (overrides
# included) se aate hain; fallback = teacher ke timetable ke topics. Jo subject
# abhi syllabus/timetable me nahi hai, wo baad me upload hote hi auto-sync ho
# jayega (har list call pe missing chapters add hote hain — links kabhi nahi
# hataye jaate).
# =============================================================
ONE_SHOT_DEADLINE = "2026-09-10T23:59"     # saare One Shot chapters ki deadline
RAPID_REVISION_DEADLINE = "2026-09-30T23:59"  # Rapid Revision (per subject) deadline

# normal task filter (special One Shot / Rapid Revision tasks lists/stats se bahar)
NOT_SPECIAL = or_(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")


def _hist(t):
    try:
        h = json.loads(getattr(t, "status_history", "") or "[]")
        return h if isinstance(h, list) else []
    except Exception:
        return []


def _hist_add(t, status, note=""):
    h = _hist(t)
    h.append({"s": status, "at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
              "note": (note or "")[:300]})
    t.status_history = json.dumps(h)


def _hist_out(t):
    """Timeline modal ke liye [{s, at(nice), note}]. Purane tasks jinme history
    nahi hai, unke liye created_at/submitted_at/review se synthesize karo."""
    raw = _hist(t)
    if not raw:
        if t.created_at:
            raw.append({"s": "assigned", "at": t.created_at.strftime("%Y-%m-%dT%H:%M"),
                        "note": "Task assigned"})
        if t.submitted_at:
            raw.append({"s": "submitted", "at": t.submitted_at.strftime("%Y-%m-%dT%H:%M"),
                        "note": "Video link submitted" + (" — on time" if t.on_time else (" — delayed" if t.on_time is False else ""))})
        if t.reviewed and t.status not in ("assigned", "submitted"):
            raw.append({"s": t.status, "at": (t.updated_at or t.created_at or datetime.now()).strftime("%Y-%m-%dT%H:%M"),
                        "note": (t.review_remarks or "")[:300]})
    out = []
    for e in raw:
        at = e.get("at") or ""
        nice = at
        try:
            nice = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M").strftime("%d %b %Y, %I:%M %p")
        except Exception:
            pass
        out.append({"s": e.get("s") or "", "at": nice, "note": e.get("note") or ""})
    return out


def _teacher_subject_list(db, tp):
    """Teacher ke (subject, class) pairs — subject_classes pehle, flat subjects fallback."""
    out, seen = [], set()
    for sc in (tp.subject_classes or []):
        try:
            nm = (sc.get("subject") or "").strip()
            cl = str(sc.get("class") or "").strip()
        except Exception:
            continue
        if nm and (nm.lower(), cl) not in seen:
            seen.add((nm.lower(), cl))
            out.append((nm, cl))
    if not out:
        for nm in (tp.subjects or []):
            nm = (nm or "").strip()
            if nm and (nm.lower(), "") not in seen:
                seen.add((nm.lower(), ""))
                out.append((nm, ""))
    return out


def _chapters_for(db, tid, name, cls):
    """([chapter titles], source). Syllabus manager (admin overrides included)
    -> timetable topics -> [] (baad me auto-sync). Display naam is function ka
    kaam nahi — wo caller stable banata hai."""
    from subjects_registry import canon_subject, squash
    import syllabus_routes as SR
    import syllabus_data as SD
    from models import Timetable
    levels = []
    try:
        lv = SR.class_level_from_name(cls)
        if lv in ("10", "12"):
            levels.append(lv)
    except Exception:
        pass
    for lv in ("12", "10"):
        if lv not in levels:
            levels.append(lv)
    for lv in levels:
        code = None
        try:
            r = canon_subject(name, lv)
            if r and r.get("code"):
                code = r["code"]
        except Exception:
            pass
        if not code:
            try:
                code = SR.subject_code_for_name(db, lv, name)
            except Exception:
                code = None
        if not code:
            continue
        subj = SR.get_subject(db, lv, code)
        if not subj:
            continue
        try:
            rows = SD.chapter_master(subj)
        except Exception:
            rows = []
        titles = []
        for r in rows:
            no, ti = str(r.get("no") or "").strip(), (r.get("title") or "").strip()
            if not ti:
                continue
            titles.append(ti if (not no or ti[:1].isdigit()) else (no + ". " + ti))
        if titles:
            return titles, "syllabus"
    # fallback: timetable ke distinct topics (teacher + subject)
    try:
        sq = squash(name)
        tops, seen_t = [], set()
        for r in db.query(Timetable).filter(Timetable.teacher_id == tid,
                                            Timetable.is_active == True).all():
            if squash(r.subject or "") != sq:
                continue
            tp2 = (r.topic or "").strip()
            if tp2 and tp2.lower() not in seen_t:
                seen_t.add(tp2.lower())
                tops.append(tp2)
        if tops:
            return tops[:60], "timetable"
    except Exception:
        pass
    return [], "pending"


def _special_subject_names(db, tp, subs):
    """Teacher ke subjects ke STABLE display naam — same subject do classes me ho
    to class suffix. One Shot task key aur Rapid rows dono isi se bante hain,
    taaki baad me syllabus aane pe naam na badle (duplicate task na bane)."""
    from collections import Counter
    from subjects_registry import canon_display
    import syllabus_routes as SR
    cnt = Counter(n.lower() for n, _c in subs)
    out = []
    seen = set()
    for nm, cl in subs:
        dn = canon_display(nm, cl or None)
        if cnt[nm.lower()] > 1:
            lv = ""
            try:
                lv = SR.class_level_from_name(cl) or ""
            except Exception:
                pass
            if lv:
                dn = "%s · Class %s" % (dn, lv)
        if dn.lower() not in seen:
            seen.add(dn.lower())
            out.append((nm, cl, dn))
    return out


def _dl(val):
    return datetime.strptime(val, "%Y-%m-%dT%H:%M")


def _sync_chapters(db, t, titles):
    """Missing chapter rows add karo (jo hain unhe — khaas kar jinme link hai — kabhi
    chhedo nahi). True agar kuch badla."""
    existing = { (c.title or "").strip().lower() for c in
                 db.query(VideoTaskChapter).filter(VideoTaskChapter.task_id == t.id).all() }
    changed = False
    sort = len(existing)
    for ti in titles:
        if ti.strip().lower() in existing:
            continue
        db.add(VideoTaskChapter(task_id=t.id, title=ti.strip()[:300], sort=sort))
        sort += 1
        changed = True
    return changed


def _ensure_special_teacher(db, tp):
    """Teacher ke One Shot (per subject) + Rapid Revision tasks banao/sync karo.
    Idempotent — har list call pe chalta hai; naye subjects/chapters auto-add."""
    subs = _teacher_subject_list(db, tp)
    if not subs:
        return
    _u = db.query(User).filter(User.id == tp.user_id).first()
    if _u is not None and _u.is_active is False:
        return
    named = _special_subject_names(db, tp, subs)   # [(raw_name, cls, stable_display)]
    changed = False
    # One Shot — har subject ka ek task, chapters syllabus/timetable se
    for nm, cl, display in named:
        titles, _src = _chapters_for(db, tp.id, nm, cl)
        t = (db.query(VideoTask)
             .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "one_shot",
                     VideoTask.subject == display).first())
        if not t:
            t = VideoTask(teacher_id=tp.id, title="One Shot — %s (All Chapters)" % display,
                          kind="one_shot", subject=display, video_type="One Shot Video",
                          status="assigned", proposed_by="admin", proposal_ok="approved",
                          deadline=_dl(ONE_SHOT_DEADLINE))
            db.add(t)
            db.flush()
            _hist_add(t, "assigned", "One Shot task auto-created — %s" % display)
            if tp.user_id:
                _vt_notify(db, tp.user_id, "🎬 One Shot Task — %s" % display,
                           'A One Shot video task for %s is now in My Tasks — record one-shot '
                           'videos of every chapter and paste each chapter\'s link in front of it. '
                           'Deadline: %s.' % (display, _dl(ONE_SHOT_DEADLINE).strftime("%d %b %Y")))
            changed = True
        if titles and _sync_chapters(db, t, titles):
            changed = True
    # Rapid Revision — ek task, rows = teacher ke subjects
    rt = (db.query(VideoTask)
          .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "rapid_revision").first())
    if not rt:
        rt = VideoTask(teacher_id=tp.id, title="Rapid Revision — All Subjects",
                       kind="rapid_revision", subject="", video_type="Rapid Revision",
                       status="assigned", proposed_by="admin", proposal_ok="approved",
                       deadline=_dl(RAPID_REVISION_DEADLINE))
        db.add(rt)
        db.flush()
        _hist_add(rt, "assigned", "Rapid Revision task auto-created")
        changed = True
    # rapid rows = wahi stable display names
    rsubs = [display for _nm, _cl, display in named]
    if rsubs and _sync_chapters(db, rt, rsubs):
        changed = True
    if changed:
        try:
            db.commit()
        except Exception:
            db.rollback()

# ~2MB image ka base64 — isse bada payload proxy/DB dono ke liye risky.
# Frontend compress karta hai; ye server-side safety net hai.
MAX_B64 = 2_800_000


def _checked_b64(payload):
    b64 = payload.get("thumbnail_b64") or None
    if b64 and len(b64) > MAX_B64:
        raise HTTPException(400, "Thumbnail image is too large. Please paste a drive "
                                 "link instead, or choose a smaller image.")
    return b64


def _vt_notify(db, user_id, title, message, ntype="video_task", link=None):
    db.add(Notification(user_id=user_id, title=title, message=message,
                        notif_type=ntype, link=link or None))


def _teacher_profile(db, tid):
    return db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()


def _teacher_name(db, tid):
    tp = _teacher_profile(db, tid)
    if not tp:
        return ""
    u = db.query(User).filter(User.id == tp.user_id).first()
    return u.name if u else ""


def _seed_channels(db):
    if db.query(VideoChannel).count() == 0:
        for n in DEFAULT_CHANNELS:
            db.add(VideoChannel(name=n))
        db.commit()


def _seed_types(db):
    if db.query(VideoType).count() == 0:
        for i, n in enumerate(DEFAULT_TYPES):
            db.add(VideoType(name=n, sort=i))
        db.commit()


def _vt_sweep(db):
    """Deadline reminders — idempotent (flags se sirf ek baar jaate hain).
    Kabhi bhi caller ka main operation fail nahi hone deta."""
    try:
        _vt_sweep_inner(db)
    except Exception:
        db.rollback()


def _vt_sweep_inner(db):
    now = datetime.now()
    acts = db.query(VideoTask).filter(VideoTask.status == "assigned").all()
    changed = False
    for t in acts:
        if not t.deadline:
            continue
        secs = (t.deadline - now).total_seconds()
        tp = _teacher_profile(db, t.teacher_id)
        uid = tp.user_id if tp else None
        if not uid:
            continue
        if 0 < secs <= 86400 and not t.warned_24h:
            t.warned_24h = True
            _vt_notify(db, uid, "⏰ Deadline Reminder — Video Task",
                       f'Your video task "{t.title}" is due in less than 24 hours. '
                       f'Please record the video and submit the drive link before the deadline.')
            changed = True
        if secs < 0 and not t.warned_overdue:
            t.warned_overdue = True
            _vt_notify(db, uid, "⚠️ Video Task Overdue",
                       f'Your video task "{t.title}" has crossed its deadline. '
                       f'Repeated delays may affect your payout. Please submit the video link at the earliest.')
            changed = True
    if changed:
        db.commit()


def _task_out(db, t, with_thumb=True):
    now = datetime.now()
    secs_left = int((t.deadline - now).total_seconds()) if t.deadline else None
    out = {
        "id": t.id, "title": t.title, "teacher_id": t.teacher_id,
        "teacher": _teacher_name(db, t.teacher_id),
        "channel_id": t.channel_id, "channel": t.channel_name or "",
        "video_type": getattr(t, "video_type", "") or "",
        "has_thumbnail": bool(t.thumbnail_b64),
        "thumbnail_link": t.thumbnail_link or "",
        "reference": t.reference or "", "remarks": t.remarks or "",
        "deadline": t.deadline.strftime("%Y-%m-%dT%H:%M") if t.deadline else "",
        "deadline_nice": t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else "",
        "seconds_left": secs_left,
        "overdue": bool(secs_left is not None and secs_left < 0 and t.status == "assigned"),
        "status": t.status,
        "proposed_by": t.proposed_by, "proposal_ok": t.proposal_ok or "",
        "submitted_link": t.submitted_link or "",
        "submitted_at": t.submitted_at.strftime("%d %b %Y, %I:%M %p") if t.submitted_at else "",
        "on_time": t.on_time,
        "reviewed": bool(t.reviewed),
        "review_remarks": t.review_remarks or "",
        "reject_count": t.reject_count or 0,
        "kind": getattr(t, "kind", "normal") or "normal",
        "subject": getattr(t, "subject", "") or "",
        "created_at": t.created_at.strftime("%d %b %Y") if t.created_at else "",
        "history": _hist_out(t),
    }
    if with_thumb:
        out["thumbnail_b64"] = t.thumbnail_b64 or ""
    return out


def _special_out(db, t):
    """One Shot / Rapid Revision task — chapters ke saath progress + NEW blink."""
    out = _task_out(db, t, with_thumb=False)
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.task_id == t.id)
           .order_by(VideoTaskChapter.sort.asc(), VideoTaskChapter.id.asc()).all())
    out["chapters"] = [{
        "id": c.id, "title": c.title, "link": c.link or "",
        "submitted_at": c.submitted_at.strftime("%d %b %Y, %I:%M %p") if c.submitted_at else "",
    } for c in chs]
    done = sum(1 for c in chs if (c.link or "").strip())
    out["done"] = done
    out["total"] = len(chs)
    out["pct"] = round(100 * done / len(chs)) if chs else 0
    lla = getattr(t, "last_link_at", None)
    asa = getattr(t, "admin_seen_at", None)
    out["is_new"] = bool(lla and (not asa or lla > asa))
    out["last_link_at"] = lla.strftime("%d %b %Y, %I:%M %p") if lla else ""
    return out


def vt_task_rank_rows(db):
    """Task completion ranking — on-time delivery rate ke hisaab se."""
    rows = []
    tps = db.query(TeacherProfile).all()
    for tp in tps:
        tasks = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id,
                         VideoTask.proposal_ok != "pending", NOT_SPECIAL).all())
        if not tasks:
            continue
        subs = [t for t in tasks if t.submitted_at]
        done = len(subs)
        ontime = sum(1 for t in subs if t.on_time)
        delayed = sum(1 for t in subs if t.on_time is False)
        pending = sum(1 for t in tasks if t.status == "assigned")
        rate = round(100 * ontime / done) if done else 0
        u = db.query(User).filter(User.id == tp.user_id).first()
        rows.append({
            "teacher_id": tp.id,
            "name": (u.name if u else "") or f"Teacher #{tp.id}",
            "photo": bool(getattr(tp, "photo_b64", None)),
            "assigned": len(tasks), "done": done, "pending": pending,
            "ontime": ontime, "delayed": delayed, "rate": rate,
        })
    rows.sort(key=lambda r: (-r["rate"], -r["ontime"], -r["done"], r["name"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _parse_deadline(val):
    """'2026-07-30T18:00' (datetime-local) -> datetime. Invalid/empty -> None."""
    if not val:
        return None
    try:
        return datetime.strptime(str(val)[:16], "%Y-%m-%dT%H:%M")
    except Exception:
        return None


# =============================================================
# CHANNELS
# =============================================================
@router.get("/admin/video-channels")
def vt_list_channels(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    rows = db.query(VideoChannel).order_by(VideoChannel.id.asc()).all()
    return {"channels": [{"id": c.id, "name": c.name, "active": bool(c.active)} for c in rows]}


@router.post("/admin/video-channels")
def vt_add_channel(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Channel name is required")
    if db.query(VideoChannel).filter(VideoChannel.name == name).first():
        raise HTTPException(400, "This channel already exists")
    c = VideoChannel(name=name)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


# =============================================================
# VIDEO TYPES (Short / Long / One Shot / Strategy ... admin add kar sakta hai)
# =============================================================
@router.get("/admin/video-types")
def vt_list_types(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_types(db)
    rows = db.query(VideoType).order_by(VideoType.sort.asc(), VideoType.id.asc()).all()
    return {"types": [{"id": c.id, "name": c.name, "active": bool(c.active)} for c in rows]}


@router.post("/admin/video-types")
def vt_add_type(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_types(db)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Type name is required")
    if db.query(VideoType).filter(VideoType.name == name).first():
        raise HTTPException(400, "This type already exists")
    mx = db.query(VideoType).order_by(VideoType.sort.desc()).first()
    c = VideoType(name=name, sort=(mx.sort + 1) if mx else 0)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name}


@router.get("/teacher/video-types")
def vt_teacher_types(db: Session = Depends(get_db), _=Depends(get_teacher)):
    _seed_types(db)
    rows = (db.query(VideoType).filter(VideoType.active == True)
            .order_by(VideoType.sort.asc(), VideoType.id.asc()).all())
    return {"types": [{"id": c.id, "name": c.name} for c in rows]}


# =============================================================
# ADMIN — ASSIGN / LIST / STATS / REVIEW / PROPOSALS / REPORT
# =============================================================
@router.post("/admin/video-tasks")
def vt_assign(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    tid = int(payload.get("teacher_id") or 0)
    title = (payload.get("title") or "").strip()
    dl = _parse_deadline(payload.get("deadline"))
    if not tid or not title:
        raise HTTPException(400, "Teacher and title are required")
    if not dl:
        raise HTTPException(400, "A valid deadline is required")
    tp = _teacher_profile(db, tid)
    if not tp:
        raise HTTPException(404, "Teacher not found")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    try:
        t = VideoTask(
            teacher_id=tid, title=title,
            channel_id=ch.id if ch else None,
            channel_name=ch.name if ch else "",
            video_type=(payload.get("video_type") or "").strip(),
            thumbnail_b64=_checked_b64(payload),
            thumbnail_link=(payload.get("thumbnail_link") or "").strip(),
            reference=(payload.get("reference") or "").strip(),
            remarks=(payload.get("remarks") or "").strip(),
            deadline=dl, status="assigned", proposed_by="admin", proposal_ok="approved",
        )
        db.add(t)
        _hist_add(t, "assigned", "Deadline: %s" % dl.strftime("%d %b %Y, %I:%M %p"))
        if tp.user_id:
            _vt_notify(db, tp.user_id, "🎬 New Video Task Assigned",
                       f'You have been assigned a new video task: "{title}"'
                       + (f' for {ch.name}' if ch else '')
                       + f'. Deadline: {dl.strftime("%d %b %Y, %I:%M %p")}. '
                       f'Please check My Tasks for the thumbnail and details.')
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        msg = str(e)
        if "1406" in msg or "Data too long" in msg:
            raise HTTPException(400, "Thumbnail database ki limit se bada hai — "
                                     "drive link paste karke assign karein. (Ek baar backend "
                                     "restart karwein: column apne aap upgrade ho jayega.)")
        raise HTTPException(400, f"Could not assign the task: {e}")
    return {"ok": True, "id": t.id}


@router.get("/admin/video-tasks")
def vt_admin_list(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  video_type: str = "",
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL)
    if teacher_id:
        q = q.filter(VideoTask.teacher_id == teacher_id)
    if status:
        q = q.filter(VideoTask.status == status)
    if channel_id:
        q = q.filter(VideoTask.channel_id == channel_id)
    if video_type:
        q = q.filter(VideoTask.video_type == video_type)
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    props = (db.query(VideoTask).filter(VideoTask.proposal_ok == "pending")
             .order_by(VideoTask.created_at.desc()).all())
    return {"tasks": [_task_out(db, t) for t in tasks],
            "proposals": [_task_out(db, t) for t in props]}


@router.get("/admin/video-tasks/stats")
def vt_admin_stats(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    tasks = (db.query(VideoTask)
             .filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL).all())
    now = datetime.now()
    total = len(tasks)
    done = sum(1 for t in tasks if t.submitted_at)
    pending = sum(1 for t in tasks if t.status == "assigned")
    delayed = sum(1 for t in tasks
                  if (t.on_time is False) or
                  (t.status == "assigned" and t.deadline and t.deadline < now))
    ranks = vt_task_rank_rows(db)
    top = ranks[0] if ranks else None
    most_delayed = None
    if ranks:
        md = max(ranks, key=lambda r: r["delayed"])
        if md["delayed"] > 0:
            most_delayed = md
    proposals = db.query(VideoTask).filter(VideoTask.proposal_ok == "pending").count()
    by_type = {}
    for t in tasks:
        k = (getattr(t, "video_type", "") or "").strip() or "Uncategorized"
        by_type[k] = by_type.get(k, 0) + 1
    return {"total": total, "done": done, "pending": pending, "delayed": delayed,
            "proposals": proposals, "by_teacher": ranks, "by_type": by_type,
            "top": top, "most_delayed": most_delayed}


@router.post("/admin/video-tasks/{task_id}/review")
def vt_review(task_id: int, payload: dict = Body(...),
              db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    action = (payload.get("action") or "").strip().lower()
    if action not in REVIEW_ACTIONS:
        raise HTTPException(400, "Invalid review action")
    remarks = (payload.get("remarks") or "").strip()
    tp = _teacher_profile(db, t.teacher_id)
    uid = tp.user_id if tp else None

    if action == "rejected":
        ndl = _parse_deadline(payload.get("new_deadline"))
        if not ndl:
            raise HTTPException(400, "A new deadline is required when rejecting")
        t.status = "assigned"
        t.reject_count = (t.reject_count or 0) + 1
        t.reviewed = True
        t.review_remarks = remarks
        t.submitted_link = ""
        t.submitted_at = None
        t.on_time = None
        t.deadline = ndl
        t.warned_24h = False
        t.warned_overdue = False
        _hist_add(t, "rejected", ("Sent back for reshoot" + (f": {remarks}" if remarks else "")
                                  + " — new deadline: " + ndl.strftime("%d %b %Y, %I:%M %p")))
        if uid:
            _vt_notify(db, uid, "↩️ Video Task Sent Back for Reshoot",
                       f'Your submission for "{t.title}" was rejected'
                       + (f': {remarks}' if remarks else '.')
                       + f' New deadline: {ndl.strftime("%d %b %Y, %I:%M %p")}. '
                       f'Please reshoot and submit again from My Tasks.')
    else:
        t.status = action
        t.reviewed = True
        t.review_remarks = remarks
        label = {"approved": "Approved", "editing_soon": "Editing Soon",
                 "editing_done": "Editing Done", "uploaded": "Uploaded"}[action]
        _hist_add(t, action, remarks)
        if uid:
            _vt_notify(db, uid, f"✅ Video Task Update — {label}",
                       f'Your video "{t.title}" status is now: {label}'
                       + (f'. Remarks: {remarks}' if remarks else '.'))
    db.commit()
    return {"ok": True, "status": t.status}


@router.post("/admin/video-tasks/{task_id}/approve-proposal")
def vt_approve_proposal(task_id: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    dl = _parse_deadline(payload.get("deadline"))
    if not dl:
        raise HTTPException(400, "A valid deadline is required")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    try:
        b64 = _checked_b64(payload)
        if b64:
            t.thumbnail_b64 = b64
        if payload.get("thumbnail_link"):
            t.thumbnail_link = payload["thumbnail_link"].strip()
        if payload.get("reference"):
            t.reference = payload["reference"].strip()
        if payload.get("remarks"):
            t.remarks = payload["remarks"].strip()
        if payload.get("video_type") is not None:
            t.video_type = (payload.get("video_type") or "").strip()
        if ch:
            t.channel_id = ch.id
            t.channel_name = ch.name
        t.deadline = dl
        t.status = "assigned"
        t.proposal_ok = "approved"
        _hist_add(t, "assigned", "Proposal approved — deadline: " + dl.strftime("%d %b %Y, %I:%M %p"))
        tp = _teacher_profile(db, t.teacher_id)
        if tp and tp.user_id:
            _vt_notify(db, tp.user_id, "✅ Video Proposal Approved",
                       f'Your video proposal "{t.title}" has been approved. '
                       f'Deadline: {dl.strftime("%d %b %Y, %I:%M %p")}. '
                       f'Thumbnail and details are available in My Tasks.')
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"Could not approve the proposal: {e}")
    return {"ok": True, "id": t.id}


@router.post("/admin/video-tasks/{task_id}/reject-proposal")
def vt_reject_proposal(task_id: int, payload: dict = Body(default={}),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.proposal_ok == "pending").first()
    if not t:
        raise HTTPException(404, "Proposal not found")
    t.proposal_ok = "rejected"
    t.status = "rejected"
    t.reviewed = True
    t.review_remarks = (payload.get("remarks") or "").strip()
    _hist_add(t, "rejected", "Proposal not approved" + (f": {t.review_remarks}" if t.review_remarks else ""))
    tp = _teacher_profile(db, t.teacher_id)
    if tp and tp.user_id:
        _vt_notify(db, tp.user_id, "❌ Video Proposal Not Approved",
                   f'Your video proposal "{t.title}" was not approved'
                   + (f': {t.review_remarks}' if t.review_remarks else '.'))
    db.commit()
    return {"ok": True}


@router.post("/admin/video-tasks/{task_id}/notify-students")
def vt_notify_students(task_id: int, payload: dict = Body(default={}),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    """Video link students ko notification se — click pe link open hota hai."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    link = (payload.get("link") or t.submitted_link or "").strip()
    if not link:
        raise HTTPException(400, "No video link attached to this task yet")
    msg = (payload.get("message") or "").strip() or \
        f'A new video "{t.title}" is now available' + \
        (f' on {t.channel_name}' if t.channel_name else '') + '. Tap to watch.'
    users = db.query(User).filter(User.is_active == True, User.role == "student").all()
    for u in users:
        _vt_notify(db, u.id, f"🎬 New Video: {t.title}", msg, "video_link", link)
    db.commit()
    return {"ok": True, "count": len(users)}


@router.post("/admin/video-tasks/{task_id}/edit")
def vt_edit(task_id: int, payload: dict = Body(...),
            db: Session = Depends(get_db), _=Depends(get_admin)):
    """Assign hone ke baad bhi task ke details badal sakte ho — title, channel,
    type, deadline, reference, remarks, thumbnail. Change history me note jata hai."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    changes = []
    title = (payload.get("title") or "").strip()
    if title and title != t.title:
        changes.append("title")
        t.title = title
    if payload.get("deadline") is not None:
        ndl = _parse_deadline(payload.get("deadline"))
        if not ndl:
            raise HTTPException(400, "A valid deadline is required")
        if ndl != t.deadline:
            changes.append("deadline → " + ndl.strftime("%d %b %Y, %I:%M %p"))
            t.deadline = ndl
            t.warned_24h = False
            t.warned_overdue = False
    if payload.get("video_type") is not None:
        vt2 = (payload.get("video_type") or "").strip()
        if vt2 != (t.video_type or ""):
            changes.append("type")
            t.video_type = vt2
    if payload.get("channel_id") is not None:
        ch = None
        cid = payload.get("channel_id")
        if cid:
            ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
            if not ch:
                raise HTTPException(404, "Channel not found")
        nid = ch.id if ch else None
        if nid != t.channel_id:
            changes.append("channel")
            t.channel_id = nid
            t.channel_name = ch.name if ch else ""
    for fld, col in (("reference", "reference"), ("remarks", "remarks")):
        if payload.get(fld) is not None:
            v = (payload.get(fld) or "").strip()
            if v != (getattr(t, col) or ""):
                changes.append(fld)
                setattr(t, col, v)
    try:
        b64 = _checked_b64(payload)
        if b64:
            t.thumbnail_b64 = b64
            changes.append("thumbnail")
    except HTTPException:
        raise
    if payload.get("thumbnail_link") is not None and (payload.get("thumbnail_link") or "").strip():
        t.thumbnail_link = payload["thumbnail_link"].strip()
        if "thumbnail" not in changes:
            changes.append("thumbnail")
    if not changes:
        return {"ok": True, "changed": []}
    _hist_add(t, "edited", "Updated: " + ", ".join(changes))
    tp = _teacher_profile(db, t.teacher_id)
    if tp and tp.user_id:
        _vt_notify(db, tp.user_id, "✏️ Video Task Updated",
                   f'Your video task "{t.title}" was updated ({", ".join(changes)}). '
                   f'Deadline: {t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else "—"}. '
                   f'Check My Tasks for details.')
    db.commit()
    return {"ok": True, "changed": changes}


def _special_payload(db, kind):
    tasks = (db.query(VideoTask).filter(VideoTask.kind == kind)
             .order_by(VideoTask.created_at.asc()).all())
    outs = [_special_out(db, t) for t in tasks]
    # NEW wale pehle, phir zyada progress wale
    outs.sort(key=lambda o: (not o["is_new"], -o["pct"], o["teacher"]))
    new_count = sum(1 for o in outs if o["is_new"])
    subjects = sorted({o["subject"] for o in outs if o["subject"]})
    return {"tasks": outs, "new_count": new_count, "subjects": subjects}


@router.get("/admin/video-tasks/special")
def vt_admin_special(kind: str = "one_shot",
                     db: Session = Depends(get_db), _=Depends(get_admin)):
    """One Shot / Rapid Revision tasks sabhi teachers ke — chapters + progress +
    NEW blink (last_link_at > admin_seen_at). kind=all pe dono ek saath."""
    if kind not in ("one_shot", "rapid_revision", "all"):
        raise HTTPException(400, "Invalid kind")
    for tp in db.query(TeacherProfile).all():
        try:
            _ensure_special_teacher(db, tp)
        except Exception:
            db.rollback()
    if kind == "all":
        return {"one_shot": _special_payload(db, "one_shot"),
                "rapid_revision": _special_payload(db, "rapid_revision")}
    return _special_payload(db, kind)


@router.post("/admin/video-tasks/{task_id}/seen")
def vt_admin_seen(task_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    t.admin_seen_at = datetime.now()
    db.commit()
    return {"ok": True}


@router.get("/admin/video-tasks/report.csv")
def vt_report_csv(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  video_type: str = "",
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    import csv
    import io
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL)
    if teacher_id:
        q = q.filter(VideoTask.teacher_id == teacher_id)
    if status:
        q = q.filter(VideoTask.status == status)
    if channel_id:
        q = q.filter(VideoTask.channel_id == channel_id)
    if video_type:
        q = q.filter(VideoTask.video_type == video_type)
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Title", "Teacher", "Channel", "Type", "Deadline", "Status",
                "Submitted At", "On Time", "Reshoots", "Review Remarks", "Created"])
    for t in tasks:
        w.writerow([
            t.id, t.title, _teacher_name(db, t.teacher_id), t.channel_name or "",
            getattr(t, "video_type", "") or "",
            t.deadline.strftime("%d %b %Y %H:%M") if t.deadline else "",
            t.status,
            t.submitted_at.strftime("%d %b %Y %H:%M") if t.submitted_at else "",
            ("Yes" if t.on_time else ("No" if t.on_time is False else "")),
            t.reject_count or 0, (t.review_remarks or "").replace("\n", " "),
            t.created_at.strftime("%d %b %Y") if t.created_at else "",
        ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=video_tasks_report.csv"})


# =============================================================
# TEACHER — MY TASKS / PROPOSE / SUBMIT
# =============================================================
def _get_tp(current_user, db):
    tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == current_user.id).first()
    if not tp:
        raise HTTPException(404, "Teacher profile not found")
    return tp


@router.get("/teacher/video-tasks/my")
def vt_my_tasks(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _vt_sweep(db)
    tp = _get_tp(current_user, db)
    _ensure_special_teacher(db, tp)
    tasks = (db.query(VideoTask)
             .filter(VideoTask.teacher_id == tp.id, NOT_SPECIAL)
             .order_by(VideoTask.created_at.desc()).all())
    active = [t for t in tasks if t.status == "assigned" and t.proposal_ok != "pending"]
    active.sort(key=lambda t: t.deadline or datetime.max)
    rest = [t for t in tasks if t not in active]
    out = [_task_out(db, t) for t in active + rest]
    nxt = active[0] if active else None
    # teacher ke apne stats: kitni upload hui, pending, on-time, delayed + is mahine type-wise
    now = datetime.now()
    real = [t for t in tasks if t.proposal_ok != "pending"]
    subs = [t for t in real if t.submitted_at]
    month_type = {}
    for t in subs:
        if t.submitted_at and t.submitted_at.year == now.year and t.submitted_at.month == now.month:
            k = (getattr(t, "video_type", "") or "").strip() or "Uncategorized"
            month_type[k] = month_type.get(k, 0) + 1
    stats = {
        "assigned": len(real),
        "uploaded": sum(1 for t in real if t.status == "uploaded"),
        "submitted": len(subs),
        "pending": sum(1 for t in real if t.status == "assigned"),
        "on_time": sum(1 for t in subs if t.on_time),
        "delayed": sum(1 for t in subs if t.on_time is False),
        "month_types": month_type,
    }
    # special tasks (One Shot per subject + Rapid Revision) — chapters ke saath
    spts = (db.query(VideoTask)
            .filter(VideoTask.teacher_id == tp.id,
                    VideoTask.kind.in_(["one_shot", "rapid_revision"]))
            .order_by(VideoTask.kind.asc(), VideoTask.subject.asc()).all())
    special = [_special_out(db, t) for t in spts]
    return {"tasks": out, "stats": stats, "special": special,
            "next_deadline": (_task_out(db, nxt) if nxt else None)}


@router.post("/teacher/video-tasks/propose")
def vt_propose(payload: dict = Body(...), db: Session = Depends(get_db),
               current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title is required")
    ch = None
    cid = payload.get("channel_id")
    if cid:
        ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first()
    t = VideoTask(teacher_id=tp.id, title=title,
                  channel_id=ch.id if ch else None,
                  channel_name=ch.name if ch else "",
                  video_type=(payload.get("video_type") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  status="proposal", proposed_by="teacher", proposal_ok="pending")
    db.add(t)
    _hist_add(t, "proposal", "Proposed by teacher")
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "🎬 New Video Proposal",
                   f'{(uname.name if uname else "A teacher")} proposed a video: "{title}". '
                   f'Review it in Task Manager to assign a thumbnail and deadline.')
    db.commit()
    return {"ok": True, "id": t.id}


@router.post("/teacher/video-tasks/{task_id}/submit")
def vt_submit(task_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
              current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.teacher_id == tp.id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    if (getattr(t, "kind", "normal") or "normal") != "normal":
        raise HTTPException(400, "One Shot / Rapid Revision task me chapter ke saamne link lagao")
    if t.status != "assigned":
        raise HTTPException(400, "This task is not open for submission")
    link = (payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "Please paste the drive link of your video")
    now = datetime.now()
    t.submitted_link = link
    t.submitted_at = now
    t.status = "submitted"
    t.reviewed = False
    t.on_time = bool(t.deadline and now <= t.deadline)
    _hist_add(t, "submitted", "Video link submitted — " + ("on time" if t.on_time else "delayed"))
    if t.on_time:
        _vt_notify(db, tp.user_id, "🎉 Great Job — Submitted On Time",
                   f'Excellent work! Your video "{t.title}" was submitted before the deadline. '
                   f'Keep up the great consistency!')
    elif not t.warned_overdue:
        t.warned_overdue = True
        _vt_notify(db, tp.user_id, "⚠️ Late Submission Noted",
                   f'Your video "{t.title}" was submitted after the deadline. '
                   f'Repeated delays may affect your payout.')
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "📥 Video Submitted for Checking",
                   f'{(uname.name if uname else "A teacher")} submitted the video '
                   f'"{t.title}" ({"on time" if t.on_time else "delayed"}). '
                   f'Please review it in Task Manager.')
    db.commit()
    return {"ok": True, "on_time": t.on_time}


@router.post("/teacher/video-tasks/{task_id}/chapter-link")
def vt_chapter_link(task_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
                    current_user=Depends(get_teacher)):
    """One Shot / Rapid Revision special task — chapter/subject row pe video link.
    Approval NAHI chahiye; progress auto. 100% pe task complete + admin ko NEW blink."""
    tp = _get_tp(current_user, db)
    t = db.query(VideoTask).filter(VideoTask.id == task_id,
                                   VideoTask.teacher_id == tp.id).first()
    if not t or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision"):
        raise HTTPException(404, "Special task not found")
    cid = int(payload.get("chapter_id") or 0)
    row = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.id == cid,
                   VideoTaskChapter.task_id == t.id).first())
    if not row:
        raise HTTPException(404, "Chapter not found in this task")
    link = (payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "Please paste the video link")
    now = datetime.now()
    first_time = not (row.link or "").strip()
    row.link = link
    row.submitted_at = now
    t.last_link_at = now
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.task_id == t.id).all())
    done = sum(1 for c in chs if (c.link or "").strip())
    total = len(chs)
    just_completed = bool(total and done == total and t.status == "assigned")
    if first_time:
        _hist_add(t, "progress", '"%s" link added (%d/%d)' % (row.title, done, total))
    else:
        _hist_add(t, "progress", '"%s" link updated (%d/%d)' % (row.title, done, total))
    if just_completed:
        t.status = "submitted"
        t.submitted_at = now
        t.on_time = bool(t.deadline and now <= t.deadline)
        _hist_add(t, "submitted", "All %d %s linked — %s" % (
            total, "chapters" if t.kind == "one_shot" else "subjects",
            "on time" if t.on_time else "delayed"))
    if just_completed:
        uname = db.query(User).filter(User.id == tp.user_id).first()
        label = "One Shot — %s" % t.subject if t.kind == "one_shot" else "Rapid Revision"
        for a in db.query(User).filter(User.role == "admin", User.is_active == True).all():
            _vt_notify(db, a.id, "🎉 %s Complete" % label,
                       f'{(uname.name if uname else "A teacher")} ne {label} ke saare '
                       f'{total} {"chapters" if t.kind == "one_shot" else "subjects"} ke links '
                       f'daale ({"on time" if t.on_time else "delayed"}). Task Manager me dekhein.')
    db.commit()
    return {"ok": True, "done": done, "total": total,
            "completed": bool(total and done == total)}
