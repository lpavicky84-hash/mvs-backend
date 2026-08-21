# video_tasks.py — VIDEO TASK MANAGER
# Production manager (admin) -> teacher video tasks: assign with thumbnail + channel +
# deadline, teacher shoots & submits drive link, admin reviews (Approved / Editing Soon /
# Editing Done / Uploaded / Rejected+reshoot), stats + ranking + CSV report + student notify.
# v71: status history timeline, admin edit, auto One Shot (per subject chapters) aur
# Rapid Revision (per subject link) special tasks — no approval, progress tracking.
import json
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import text, or_

from database import get_db, engine
from security import get_admin, get_teacher
# v94: restricted sub-admin — /api/admin/* video endpoints bhi section guard se cover
from admin_routes import admin_section_guard as _admin_section_guard
from models import User, TeacherProfile, Notification, VideoChannel, VideoTask, VideoType, VideoTaskChapter, VideoTaskComment

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


def _ensure_vtype_scope_column():
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE video_types ADD COLUMN streaming_scope VARCHAR(12) DEFAULT 'both'"))
            conn.commit()
        print("[video_tasks] video_types.streaming_scope added")
    except Exception:
        pass


_ensure_vtype_scope_column()


def _ensure_special_columns():
    """v71 columns (kind/subject/status_history/last_link_at/admin_seen_at) +
    video_task_chapters table — purane deploys pe best-effort self-heal."""
    alters = [
        "ALTER TABLE video_tasks ADD COLUMN kind VARCHAR(20) DEFAULT 'normal'",
        "ALTER TABLE video_tasks ADD COLUMN is_old BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN subject VARCHAR(160) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN status_history TEXT NULL",
        "ALTER TABLE video_tasks ADD COLUMN last_link_at DATETIME NULL",
        "ALTER TABLE video_tasks ADD COLUMN admin_seen_at DATETIME NULL",
        "ALTER TABLE video_tasks ADD COLUMN weekly_quota INTEGER DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN weekly_day VARCHAR(12) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN item_source VARCHAR(12) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN streaming VARCHAR(20) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN youtube_url VARCHAR(600) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN yt_video_id VARCHAR(40) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN yt_views INTEGER NULL",
        "ALTER TABLE video_tasks ADD COLUMN yt_views_at DATETIME NULL",
        "ALTER TABLE video_task_chapters ADD COLUMN edit_status VARCHAR(20) DEFAULT ''",
        "ALTER TABLE video_task_chapters ADD COLUMN changed_at DATETIME NULL",
        "ALTER TABLE video_task_chapters ADD COLUMN vintage VARCHAR(10) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN vintage VARCHAR(10) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN collab_teacher_ids TEXT NULL",
        "ALTER TABLE video_tasks ADD COLUMN collab_verified TEXT NULL",
        "ALTER TABLE video_tasks ADD COLUMN collab_not_completed TEXT NULL",
        "ALTER TABLE video_tasks ADD COLUMN submitted_by INTEGER NULL",
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

REVIEW_ACTIONS = ("approved", "editing_soon", "editing_done", "uploaded", "rejected", "reshoot")

# Chapter-level production status (admin/production team set karti hai):
# link lagte hi chapter "editing_soon" (editing karwani hai) — phir admin
# "editing_done" (edited rakhi hai) -> "uploaded" (upload ho gayi) karta hai.
CHAPTER_EDIT_STATUSES = ("editing_soon", "editing_done", "uploaded")


def _ch_status(c):
    """Chapter ka logical production status — purana data (edit_status khali)
    link wale rows pe 'editing_soon' mana jata hai."""
    es = (getattr(c, "edit_status", "") or "").strip()
    if es in CHAPTER_EDIT_STATUSES:
        return es
    return "editing_soon" if (c.link or "").strip() else ""


def _subject_cls(subject):
    """'Physics 12' -> '12', 'Social Science 10' -> '10', 'Mathematics' -> '' —
    class filter ke liye display naam se class nikaalna."""
    m = re.search(r"(\d{1,2})\s*$", (subject or "").strip())
    if m and m.group(1) in ("10", "12"):
        return m.group(1)
    return ""

# =============================================================
# SPECIAL TASKS — One Shot (per subject, chapters auto) + Rapid Revision
# (per subject, ek link). Approval NAHI chahiye; teacher chapter/subject ke
# saamne link lagata hai, progress auto. Chapters syllabus manager (overrides
# included) se aate hain; fallback = teacher ke timetable ke topics. Jo subject
# abhi syllabus/timetable me nahi hai, wo baad me upload hote hi auto-sync ho
# jayega (har list call pe missing chapters add hote hain — links kabhi nahi
# hataye jaate).
# =============================================================
WEEK_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ONE_SHOT_DEADLINE = "2026-09-10T23:59"     # saare One Shot chapters ki deadline
RAPID_REVISION_DEADLINE = "2026-09-30T23:59"  # Rapid Revision (per subject) deadline

# normal task filter (special One Shot / Rapid Revision tasks lists/stats se bahar)
def _now_ist():
    # Railway server UTC pe chalta hai; portal IST me dikhana/compute karna hai.
    # Deadlines user-entered (IST-naive) hain — isliye "now" bhi IST-naive rakhte hain
    # taaki submitted-time display, on-time credit aur countdown sab consistent rahein.
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


NOT_SPECIAL = or_(VideoTask.kind == None, VideoTask.kind == "", VideoTask.kind == "normal")


def _hist(t):
    try:
        h = json.loads(getattr(t, "status_history", "") or "[]")
        return h if isinstance(h, list) else []
    except Exception:
        return []


def _hist_add(t, status, note=""):
    h = _hist(t)
    h.append({"s": status, "at": _now_ist().strftime("%Y-%m-%dT%H:%M"),
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
            raw.append({"s": t.status, "at": (t.updated_at or t.created_at or _now_ist()).strftime("%Y-%m-%dT%H:%M"),
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


def _chapters_for(db, tid, name, cls, scope=""):
    """([chapter titles], source). Syllabus manager (admin overrides included)
    -> timetable topics -> [] (baad me auto-sync). Display naam is function ka
    kaam nahi — wo caller stable banata hai.
    scope: '' / 'all' = saare chapters; 'pe' = sirf Public Exam chapters;
    'tma' = sirf TMA chapters (kind flag syllabus manager se aata hai).
    Timetable fallback pe kind pata nahi hota — wahan scope filter nahi lagta."""
    from subjects_registry import canon_subject, squash
    import syllabus_routes as SR
    import syllabus_data as SD
    from models import Timetable
    scope = (scope or "").strip().lower()
    if scope not in ("pe", "tma"):
        scope = ""
    lv0 = _class_level(cls)
    if lv0:
        levels = [lv0]   # class pata ho to SIRF usi class ka syllabus — doosri
                         # class ke chapters kabhi merge nahi (bulletproof)
    else:
        levels = ["12", "10"]
    for lv in levels:
        code = None
        # AUTHORITATIVE: (class_level, exact name) from the DB subject list — unambiguous
        # even when the same name exists in both classes (Painting 225 vs 332).
        try:
            from models import AvailableSubject as _AVS
            _av = db.query(_AVS).filter(_AVS.class_level == lv, _AVS.name == name).first()
            if _av and _av.code:
                code = _av.code
        except Exception:
            code = None
        if not code:
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
        had_any = False
        for r in rows:
            no, ti = str(r.get("no") or "").strip(), (r.get("title") or "").strip()
            if not ti:
                continue
            had_any = True
            if scope and (r.get("kind") or "").strip().lower() != scope:
                continue   # PE/TMA scope — sirf wahi chapters
            titles.append(ti if (not no or ti[:1].isdigit()) else (no + ". " + ti))
        if titles:
            return titles, "syllabus"
        if had_any and scope:
            return [], "syllabus"   # chapters hain par is scope me koi nahi — fallback mat jao
    # fallback: timetable ke distinct topics (teacher + subject)
    try:
        sq = squash(name)
        lv_want = _class_level(cls)
        tops, seen_t = [], set()
        for r in db.query(Timetable).filter(Timetable.teacher_id == tid,
                                            Timetable.is_active == True).all():
            if squash(r.subject or "") != sq:
                continue
            if lv_want:
                rl = _class_level(getattr(r, "class_name", ""))
                if rl and rl != lv_want:
                    continue  # doosri class ka period — merge bilkul nahi
            tp2 = (r.topic or "").strip()
            if tp2 and tp2.lower() not in seen_t:
                seen_t.add(tp2.lower())
                tops.append(tp2)
        if tops:
            return tops[:60], "timetable"
    except Exception:
        pass
    return [], "pending"


def _class_level(cl):
    """'Class 12' / '12' / 'XII' → '12'; samajh na aaye to ''."""
    try:
        import syllabus_routes as SR
        lv = SR.class_level_from_name(cl)
        if lv in ("10", "12"):
            return lv
    except Exception:
        pass
    d = re.sub(r"\D", "", str(cl or ""))
    return d if d in ("10", "12") else ""


def _stable_subject_display(nm, cl):
    """HAMESHA stable display naam: class pata ho to 'Physics 12' / 'Physics 10'.
    Baad me doosri class add/remove hone pe bhi naam WAHY rehta hai — isi se
    duplicate/merge task ka jad kaaran khatam hota hai."""
    from subjects_registry import canon_display
    base = (canon_display(nm, cl or None) or "").strip() or (nm or "").strip()
    lv = _class_level(cl)
    return ("%s %s" % (base, lv)).strip() if lv else base


def _legacy_subject_names(nm, cl, display):
    """Purane naam formats (v71: plain 'Physics' ya 'Physics · Class 12') —
    self-heal rename ke candidates; naya stable naam isme shamil nahi."""
    from subjects_registry import canon_display
    base = (canon_display(nm, cl or None) or "").strip() or (nm or "").strip()
    lv = _class_level(cl)
    cands = {base}
    if lv:
        cands.add("%s · Class %s" % (base, lv))
    cands.discard(display)
    return [c for c in cands if c]


def _subj_ident(name, cls):
    """Class-AWARE, alias-robust identity for special-task subjects.
    Registry code mile -> 'c<code>'; warna alias-expand+squash+class-digits.
    'Maths 10'=='Mathematics 10'; Mathematics 10 != Mathematics 12."""
    base = re.sub(r"(?i)\bclass\b", " ", str(name or ""))
    c = re.sub(r"[^0-9]", "", str(cls or ""))
    try:
        from subjects_registry import canon_subject, squash, _expand_words
        r = canon_subject(base, cls or None)
        if r and r.get("code"):
            return "c" + r["code"]
        return squash(_expand_words(base)) + "|" + c
    except Exception:
        return re.sub(r"[^a-z0-9]", "", base.lower()) + "|" + c


def _special_subject_names(db, tp, subs):
    out, seen = [], set()
    for nm, cl in subs:
        dn = _stable_subject_display(nm, cl)
        ident = _subj_ident(nm, cl)
        if dn and ident not in seen:
            seen.add(ident)
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


def _tma_titles_for(db, tid, name, cls):
    """(titles, ok) — syllabus ke SIRF TMA chapters; ok True tabhi jab subject ka
    syllabus resolve ho (timetable fallback pe kind pata nahi — wahan prune skip)."""
    titles, src = _chapters_for(db, tid, name, cls, "tma")
    return titles, (src == "syllabus")


def _prune_tma(db, t, tma_titles):
    """v92: special tasks se TMA chapters hatao — sirf Public Examination chapters
    shoot karne hain. Custom (syllabus se bahar ke) titles chhuwe nahi jaate —
    sirf wo rows hat ti hain jo syllabus ke TMA list se exact match karti hain."""
    tset = {x.strip().lower() for x in tma_titles if (x or "").strip()}
    if not tset:
        return False
    rows = (db.query(VideoTaskChapter)
            .filter(VideoTaskChapter.task_id == t.id).all())
    hit = [c for c in rows if (c.title or "").strip().lower() in tset]
    if not hit:
        return False
    for c in hit:
        db.delete(c)
    _hist_add(t, "edited",
              "Chapters trimmed to Public Examination only — %d TMA chapter(s) removed"
              % len(hit))
    return True


def _pe_sync_prune(db, t, tid, name, cls):
    """v92: task ke chapters PE-only banao — missing PE chapters add + TMA rows prune.
    Return None = syllabus resolve nahi hua (caller apne fallback pe jaye);
    True/False = PE scope apply hua, kuch badla ya nahi."""
    pe_titles, pe_src = _chapters_for(db, tid, name, cls, "pe")
    if pe_src != "syllabus" or not pe_titles:
        return None
    changed = _sync_chapters(db, t, pe_titles)
    tma_titles, tma_ok = _tma_titles_for(db, tid, name, cls)
    if tma_ok and _prune_tma(db, t, tma_titles):
        changed = True
    return changed


def _display_base_cls(display):
    """'Physics 12' -> ('Physics', '12'); class na ho to ('Physics', '')."""
    subj = (display or "").strip()
    cls = _subject_cls(subj)
    base = re.sub(r"\s*\d{1,2}\s*$", "", subj).strip() if cls else subj
    return base, cls


def _tagged_chapters_for(db, name, cls):
    """([{title, kind}], source) — syllabus chapter master PE/TMA tag ke saath,
    _chapters_for jaisi hi resolution + title format. Resolve na ho to ([], '')."""
    from subjects_registry import canon_subject
    import syllabus_routes as SR
    import syllabus_data as SD
    lv0 = _class_level(cls)
    levels = [lv0] if lv0 else ["12", "10"]
    for lv in levels:
        code = None
        # AUTHORITATIVE: (class_level, exact name) from DB — unambiguous across classes.
        try:
            from models import AvailableSubject as _AVS
            _av = db.query(_AVS).filter(_AVS.class_level == lv, _AVS.name == name).first()
            if _av and _av.code:
                code = _av.code
        except Exception:
            code = None
        if not code:
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
        out = []
        for r in rows:
            no, ti = str(r.get("no") or "").strip(), (r.get("title") or "").strip()
            if not ti:
                continue
            out.append({
                "title": ti if (not no or ti[:1].isdigit()) else (no + ". " + ti),
                "kind": (r.get("kind") or "").strip().upper()})
        if out:
            return out, "syllabus"
    return [], ""


def _dedupe_special(db, teacher_id, kind):
    """Same teacher+kind+subject ke duplicate tasks self-heal merge (kahin purana
    bug ya double-create ho to bhi): sabse purana task rakho, baaki ke chapters
    move karke (link wale preserve) task delete. History bhi merge hoti hai."""
    def _nk(x):
        # v186: shared class-aware alias-robust identity (Maths 10 == Mathematics 10;
        # Mathematics 10 != Mathematics 12). Duplicate cards permanent khatam.
        return _subj_ident(*_display_base_cls(x))
    def _tk(s):
        return re.sub(r"\s+", " ", (s or "")).strip().lower()
    tasks = (db.query(VideoTask)
             .filter(VideoTask.teacher_id == teacher_id, VideoTask.kind == kind)
             .order_by(VideoTask.created_at.asc(), VideoTask.id.asc()).all())
    groups = {}
    for t in tasks:
        groups.setdefault(_nk(t.subject), []).append(t)
    changed = False
    for _k, grp in groups.items():
        if len(grp) < 2:
            continue
        keep = grp[0]
        existing = { _tk(c.title): c for c in
                     db.query(VideoTaskChapter)
                     .filter(VideoTaskChapter.task_id == keep.id).all() }
        for extra in grp[1:]:
            for c in (db.query(VideoTaskChapter)
                      .filter(VideoTaskChapter.task_id == extra.id).all()):
                key = _tk(c.title)
                tgt = existing.get(key)
                if tgt is None:
                    c.task_id = keep.id
                    existing[key] = c
                else:
                    if (c.link or "").strip() and not (tgt.link or "").strip():
                        tgt.link, tgt.submitted_at = c.link, c.submitted_at
                    db.delete(c)
            if (getattr(extra, "last_link_at", None) or datetime.min) > \
               (getattr(keep, "last_link_at", None) or datetime.min):
                keep.last_link_at = extra.last_link_at
            # timelines merge — purane task ki history bhi survive kare
            mh = { (h.get("s"), h.get("at"), h.get("note")): h for h in _hist(keep) }
            for h in _hist(extra):
                mh.setdefault((h.get("s"), h.get("at"), h.get("note")), h)
            merged = sorted(mh.values(), key=lambda h: h.get("at") or "")
            keep.status_history = json.dumps(merged)
            db.delete(extra)
            _hist_add(keep, "edited", "Duplicate task merged automatically")
            changed = True
        # merged card ka naam canonical stable display pe le aao
        try:
            _b, _c = _display_base_cls(keep.subject)
            _disp = _stable_subject_display(_b, _c)
            if _disp and _disp != keep.subject:
                keep.subject = _disp
                keep.title = ("One Shot — %s (All Chapters)" if kind == "one_shot"
                              else "Rapid Revision — %s (All Chapters)") % _disp
        except Exception:
            pass
    return changed


def _ensure_kind_parity(db, tp):
    """v78: One Shot jis-jis subject ka hai, un sabka Rapid Revision bhi ho.
    Profile subjects baad me clear/change ho jayein to purane (orphan) One Shot
    tasks ke liye RR kabhi banta hi nahi tha (early return) — isliye admin
    monitor me One Shot 24 vs Rapid Revision 21 jaisa gap aa jata tha.
    Ye pass existing One Shot tasks se chalti hai (profile subjects pe depend
    nahi), idempotent hai — RR already ho to kuch nahi karti."""
    os_subjects = [s for (s,) in db.query(VideoTask.subject)
                   .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "one_shot")
                   .distinct().all() if (s or "").strip()]
    if not os_subjects:
        return False
    rr_subjects = {s for (s,) in db.query(VideoTask.subject)
                   .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "rapid_revision")
                   .distinct().all()}
    missing = [s for s in os_subjects if s not in rr_subjects]
    if not missing:
        return False
    _u = db.query(User).filter(User.id == tp.user_id).first()
    _inactive = bool(_u is not None and _u.is_active is False)
    for subj in missing:
        rt = VideoTask(teacher_id=tp.id,
                       title="Rapid Revision — %s (All Chapters)" % subj,
                       kind="rapid_revision", subject=subj,
                       video_type="Rapid Revision",
                       status="assigned", proposed_by="admin", proposal_ok="approved",
                       deadline=_dl(RAPID_REVISION_DEADLINE))
        db.add(rt)
        db.flush()
        _hist_add(rt, "assigned", "Rapid Revision task auto-created — %s (One Shot parity)" % subj)
        if tp.user_id and not _inactive:
            _vt_notify(db, tp.user_id, "Rapid Revision Task — %s" % subj,
                       'A Rapid Revision task for %s is now in My Tasks — record a rapid '
                       'revision video of every chapter and paste each chapter\'s link in '
                       'front of it. Deadline: %s.'
                       % (subj, _dl(RAPID_REVISION_DEADLINE).strftime("%d %b %Y")))
    return True


def _ensure_special_teacher(db, tp):
    """Teacher ke One Shot (per subject) + Rapid Revision tasks banao/sync karo.
    Idempotent + self-heal: purane naam formats rename, duplicate tasks merge,
    links/history kabhi delete nahi hote."""
    changed = _ensure_kind_parity(db, tp)
    subs = _teacher_subject_list(db, tp)
    if not subs:
        # v115: subjects empty ho tab bhi duplicate-merge chalao — warna pehle bane
        # duplicate special tasks (subject baad me hatne par) kabhi heal nahi hote.
        try:
            if _dedupe_special(db, tp.id, "one_shot"):
                changed = True
            if _dedupe_special(db, tp.id, "rapid_revision"):
                changed = True
        except Exception:
            db.rollback()
            changed = False
        if changed:
            try:
                db.commit()
            except Exception:
                db.rollback()
        return
    _u = db.query(User).filter(User.id == tp.user_id).first()
    # Inactive teacher ke tasks bhi sync/create hote rahenge (admin monitor me
    # One Shot vs Rapid Revision parity ke liye) — bas notification nahi jayega.
    _inactive = bool(_u is not None and _u.is_active is False)
    named = _special_subject_names(db, tp, subs)   # [(raw_name, cls, stable_display)]
    # One Shot — har subject ka ek task, chapters syllabus/timetable se
    for nm, cl, display in named:
        titles, _src = _chapters_for(db, tp.id, nm, cl)
        t = (db.query(VideoTask)
             .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "one_shot",
                     VideoTask.subject == display)
             .order_by(VideoTask.id.asc()).first())
        # purane naam ('Physics' / 'Physics · Class 12') ke tasks HAMESHA rename —
        # canonical task pehle se ho tab bhi, warna legacy kabhi heal nahi hoga
        legacy = _legacy_subject_names(nm, cl, display)
        if legacy:
            q = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "one_shot",
                         VideoTask.subject.in_(legacy))
                 .order_by(VideoTask.id.asc()))
            if t:
                q = q.filter(VideoTask.id != t.id)
            for lt in q.all():
                lt.subject = display
                lt.title = "One Shot — %s (All Chapters)" % display
                _hist_add(lt, "edited", "Subject name standardized: %s" % display)
                changed = True
                if t is None:
                    t = lt
        if not t:
            t = VideoTask(teacher_id=tp.id, title="One Shot — %s (All Chapters)" % display,
                          kind="one_shot", subject=display, video_type="One Shot Video",
                          status="assigned", proposed_by="admin", proposal_ok="approved",
                          deadline=_dl(ONE_SHOT_DEADLINE))
            db.add(t)
            db.flush()
            _hist_add(t, "assigned", "One Shot task auto-created — %s" % display)
            if tp.user_id and not _inactive:
                _vt_notify(db, tp.user_id, "One Shot Task — %s" % display,
                           'A One Shot video task for %s is now in My Tasks — record one-shot '
                           'videos of every chapter and paste each chapter\'s link in front of it. '
                           'Deadline: %s.' % (display, _dl(ONE_SHOT_DEADLINE).strftime("%d %b %Y")))
            changed = True
        _psr = _pe_sync_prune(db, t, tp.id, nm, cl)
        if _psr is None:
            if titles and _sync_chapters(db, t, titles):
                changed = True
        elif _psr:
            changed = True
    # Rapid Revision — har subject ka ek task, chapters syllabus/timetable se (One Shot jaisa)
    for nm, cl, display in named:
        titles, _src = _chapters_for(db, tp.id, nm, cl)
        rt = (db.query(VideoTask)
              .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "rapid_revision",
                      VideoTask.subject == display)
              .order_by(VideoTask.id.asc()).first())
        legacy = _legacy_subject_names(nm, cl, display)
        if legacy:
            q = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "rapid_revision",
                         VideoTask.subject.in_(legacy))
                 .order_by(VideoTask.id.asc()))
            if rt:
                q = q.filter(VideoTask.id != rt.id)
            for lt in q.all():
                lt.subject = display
                lt.title = "Rapid Revision — %s (All Chapters)" % display
                _hist_add(lt, "edited", "Subject name standardized: %s" % display)
                changed = True
                if rt is None:
                    rt = lt
        if not rt:
            rt = VideoTask(teacher_id=tp.id,
                           title="Rapid Revision — %s (All Chapters)" % display,
                           kind="rapid_revision", subject=display,
                           video_type="Rapid Revision",
                           status="assigned", proposed_by="admin", proposal_ok="approved",
                           deadline=_dl(RAPID_REVISION_DEADLINE))
            db.add(rt)
            db.flush()
            _hist_add(rt, "assigned", "Rapid Revision task auto-created — %s" % display)
            if tp.user_id and not _inactive:
                _vt_notify(db, tp.user_id, "Rapid Revision Task — %s" % display,
                           'A Rapid Revision task for %s is now in My Tasks — record a rapid '
                           'revision video of every chapter and paste each chapter\'s link in '
                           'front of it. Deadline: %s.'
                           % (display, _dl(RAPID_REVISION_DEADLINE).strftime("%d %b %Y")))
            changed = True
        _psr = _pe_sync_prune(db, rt, tp.id, nm, cl)
        if _psr is None:
            if titles and _sync_chapters(db, rt, titles):
                changed = True
        elif _psr:
            changed = True
    # v92: syllabus-connected custom PROJECTS bhi PE-only (One Shot/RR jaisa) —
    # item_source 'custom' wale projects ke items admin ke banaye hue hain, unhe chhedo nahi.
    for pt in (db.query(VideoTask)
               .filter(VideoTask.teacher_id == tp.id, VideoTask.kind == "project",
                       VideoTask.item_source == "syllabus").all()):
        base, pcls = _display_base_cls(pt.subject)
        if base and _pe_sync_prune(db, pt, tp.id, base, pcls):
            changed = True
    # Legacy single-task format (subject="" — ek task jisme har subject ki ek row thi)
    # migrate: purane links naye per-subject task ki history me note karke task delete.
    # v79: purane format me subject "" ke saath-saath "All Subjects" bhi aata
    # tha (case/space koi bhi) — dono pakdo, warna legacy card kabhi delete
    # nahi hota aur saare subjects ek hi card me dikhte rehte hain.
    legacy_single = [t for t in (db.query(VideoTask)
                     .filter(VideoTask.teacher_id == tp.id,
                             VideoTask.kind == "rapid_revision")
                     .all())
                     if (t.subject or "").strip().lower() in ("", "all subjects")]
    if legacy_single:
        legacy_map = {}
        for nm, cl, display in named:
            legacy_map[display.lower()] = display
            for lg in _legacy_subject_names(nm, cl, display):
                legacy_map[lg.lower()] = display
        for old in legacy_single:
            rows = (db.query(VideoTaskChapter)
                    .filter(VideoTaskChapter.task_id == old.id).all())
            for crow in rows:
                tgt_disp = legacy_map.get((crow.title or "").strip().lower())
                if not tgt_disp or not (crow.link or "").strip():
                    continue
                nrt = (db.query(VideoTask)
                       .filter(VideoTask.teacher_id == tp.id,
                               VideoTask.kind == "rapid_revision",
                               VideoTask.subject == tgt_disp)
                       .order_by(VideoTask.id.asc()).first())
                if nrt is not None:
                    _hist_add(nrt, "progress",
                              'Migrated subject-level link for %s: %s' % (tgt_disp, crow.link))
            for crow in rows:
                db.delete(crow)
            db.delete(old)
            changed = True
    # task-level duplicate merge (rename ke baad bhi ban sakte hain)
    if _dedupe_special(db, tp.id, "one_shot"):
        changed = True
    if _dedupe_special(db, tp.id, "rapid_revision"):
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
    if b64:
        # seedha R2 par bhej do (base64 store na ho) — fail ho to base64 hi rahe
        try:
            return __import__("r2_storage").normalize(b64, "thumbnails", "image/jpeg")
        except Exception:
            return b64
    return b64


def _vt_notify(db, user_id, title, message, ntype="video_task", link=None):
    db.add(Notification(user_id=user_id, title=title, message=message,
                        notif_type=ntype, link=link or None))


def _vtc_out(db, c):
    return {"id": c.id, "task_id": c.task_id, "user_id": c.user_id,
            "author": c.author_name or "", "role": c.author_role or "",
            "message": c.message or "",
            "at": c.created_at.strftime("%d %b %Y, %I:%M %p") if c.created_at else ""}


def _vtc_list(db, task_id):
    rows = (db.query(VideoTaskComment)
            .filter(VideoTaskComment.task_id == task_id)
            .order_by(VideoTaskComment.id.asc()).all())
    return [_vtc_out(db, c) for c in rows]


def _vtc_add(db, task_id, user, message, role):
    msg = (message or "").strip()
    if not msg:
        return None
    c = VideoTaskComment(task_id=task_id, user_id=getattr(user, "id", None),
                         author_name=getattr(user, "name", "") or "",
                         author_role=role, message=msg)
    db.add(c); db.flush()
    return c


@router.get("/teacher/video-tasks/{task_id}/comments")
def vt_teacher_comments(task_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return {"comments": _vtc_list(db, task_id)}


@router.post("/teacher/video-tasks/{task_id}/comments")
def vt_teacher_comment_add(task_id: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    c = _vtc_add(db, task_id, current_user, payload.get("message"), "teacher")
    if not c:
        raise HTTPException(400, "Message cannot be empty")
    # notify every admin so the reply shows up on their side
    for adm in db.query(User).filter(User.role == "admin", User.is_active == True).all():
        _vt_notify(db, adm.id, "Teacher replied on a video task",
                   f'{current_user.name} replied on "{t.title}": {c.message[:120]}',
                   "video_task", link=str(task_id))
    db.commit()
    return {"ok": True, "comment": _vtc_out(db, c)}


@router.get("/admin/video-tasks/{task_id}/comments", dependencies=[Depends(_admin_section_guard)])
def vt_admin_comments(task_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    return {"comments": _vtc_list(db, task_id)}


@router.post("/admin/video-tasks/{task_id}/comments", dependencies=[Depends(_admin_section_guard)])
def vt_admin_comment_add(task_id: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), me=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    c = _vtc_add(db, task_id, me, payload.get("message"), "admin")
    if not c:
        raise HTTPException(400, "Message cannot be empty")
    # notify the creator (and collaborators) that the manager replied
    for tid in _collab_all_ids(t):
        tp = _teacher_profile(db, tid)
        if tp and tp.user_id:
            _vt_notify(db, tp.user_id, "Manager replied on your video task",
                       f'Message on "{t.title}": {c.message[:120]}',
                       "video_task", link=str(task_id))
    db.commit()
    return {"ok": True, "comment": _vtc_out(db, c)}


def _vt_notify_UNUSED(db, user_id, title, message, ntype="video_task", link=None):
    pass


_TP_CACHE = {"t": 0.0, "map": {}}   # legacy (ab use nahi — cross-request ORM cache DetachedInstanceError deta tha)


def _teacher_profile(db, tid):
    """REQUEST-scoped cache (db session par). Pehle module-level _TP_CACHE ORM objects
    ko requests ke beech cache karta tha -> session band hone par woh detach ho jaate the
    -> tp.user_id/tp.subjects access par DetachedInstanceError (baar-baar crash + slow).
    Ab cache db session ke saath hi jeeta/marta hai: ek request me har teacher ek hi baar
    load (N+1 nahi), aur object hamesha live session se bandha (kabhi detached nahi)."""
    if not tid:
        return None
    cache = getattr(db, "_tp_req_cache", None)
    if cache is None:
        cache = {}
        try:
            db._tp_req_cache = cache
        except Exception:
            cache = None
    if cache is not None and tid in cache:
        return cache[tid]
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if cache is not None:
        cache[tid] = tp
    return tp


def _teacher_name(db, tid):
    """Request-scoped name cache — session ke saath. Detach-safe + no per-task N+1."""
    if not tid:
        return ""
    cache = getattr(db, "_tn_req_cache", None)
    if cache is None:
        cache = {}
        try:
            db._tn_req_cache = cache
        except Exception:
            cache = None
    if cache is not None and tid in cache:
        return cache[tid]
    tp = _teacher_profile(db, tid)
    name = ""
    if tp:
        u = db.query(User).filter(User.id == tp.user_id).first()
        name = (u.name if u else "") or ""
    if cache is not None:
        cache[tid] = name
    return name

def _all_teacher_names(db):
    """Ek hi query me saare teacher names (tid -> name) — task list me per-task
    _teacher_name (N+1, 2 query/task) ki jagah. Task Manager fast."""
    m = {}
    try:
        for tp_id, name in (db.query(TeacherProfile.id, User.name)
                            .join(User, TeacherProfile.user_id == User.id).all()):
            m[tp_id] = name or ""
    except Exception:
        pass
    return m


def _seed_channels(db):
    if db.query(VideoChannel).count() == 0:
        for n in DEFAULT_CHANNELS:
            db.add(VideoChannel(name=n))
        db.commit()


def _seed_types(db):
    if db.query(VideoType).count() == 0:
        for i, n in enumerate(DEFAULT_TYPES):
            sc = "recorded" if n == "Short Video" else "both"
            db.add(VideoType(name=n, sort=i, streaming_scope=sc))
        db.commit()
    # Purane installs: Short Video ko EK baar 'recorded' tag karo (Live me na dikhe).
    # Flag se — taaki admin baad me badle to dobara override na ho.
    try:
        from models import AppSetting
        flag = db.query(AppSetting).filter(AppSetting.key == "vt_sv_scoped").first()
        if not flag:
            sv = db.query(VideoType).filter(VideoType.name == "Short Video").first()
            if sv and (getattr(sv, "streaming_scope", "both") or "both") == "both":
                sv.streaming_scope = "recorded"
            db.add(AppSetting(key="vt_sv_scoped", value="1"))
            db.commit()
    except Exception:
        db.rollback()


def _vt_sweep(db):
    """Deadline reminders — idempotent (flags se sirf ek baar jaate hain).
    Kabhi bhi caller ka main operation fail nahi hone deta.
    THROTTLE: har list call par nahi — 90s me ek baar (Task Manager fast)."""
    try:
        import time as _t
        _last = globals().get("_VT_SWEEP_LAST", 0)
        if _t.time() - _last < 90:
            return
        globals()["_VT_SWEEP_LAST"] = _t.time()
        _vt_sweep_inner(db)
    except Exception:
        db.rollback()


def _vt_sweep_inner(db):
    now = _now_ist()
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
            _vt_notify(db, uid, "Video Task Overdue",
                       f'Your video task "{t.title}" has crossed its deadline. '
                       f'Repeated delays may affect your payout. Please submit the video link at the earliest.')
            changed = True
    if changed:
        db.commit()


def _collab_extra_ids(t):
    """Task ke ADDITIONAL (collab) teacher ids — primary ke alawa."""
    import json as _j
    try:
        v = _j.loads(t.collab_teacher_ids) if getattr(t, "collab_teacher_ids", "") else []
        return [int(x) for x in v if x]
    except Exception:
        return []


def _collab_all_ids(t):
    """Primary + collab teacher ids (unique, order preserved)."""
    ids = [t.teacher_id] if t.teacher_id else []
    for i in _collab_extra_ids(t):
        if i not in ids:
            ids.append(i)
    return ids


def _collab_vmap(t):
    import json as _j
    try:
        return _j.loads(t.collab_verified) if getattr(t, "collab_verified", "") else {}
    except Exception:
        return {}


def _collab_ncmap(t):
    """Per-teacher 'task not completed' map {teacher_id_str: True} — collab task me kisi
    ek teacher ne apna kaam nahi kiya to sirf usko not-completed mark karte hain."""
    import json as _j
    try:
        return _j.loads(t.collab_not_completed) if getattr(t, "collab_not_completed", "") else {}
    except Exception:
        return {}


def _task_out(db, t, with_thumb=True, tname_map=None):
    def _tn(tid):
        if tname_map is not None:
            return tname_map.get(tid, "")
        return _teacher_name(db, tid)
    now = _now_ist()
    secs_left = int((t.deadline - now).total_seconds()) if t.deadline else None
    out = {
        "id": t.id, "title": t.title, "teacher_id": t.teacher_id,
        "teacher": _tn(t.teacher_id),
        "channel_id": t.channel_id, "channel": t.channel_name or "",
        "video_type": getattr(t, "video_type", "") or "",
        "has_thumbnail": bool(t.thumbnail_b64),
        "thumbnail_link": t.thumbnail_link or "",
        "reference": t.reference or "", "remarks": t.remarks or "",
        "reference_video": getattr(t, "reference_video", "") or "",
        "deadline": t.deadline.strftime("%Y-%m-%dT%H:%M") if t.deadline else "",
        "deadline_nice": t.deadline.strftime("%d %b %Y, %I:%M %p") if t.deadline else "",
        "expected_deadline": (t.deadline.strftime("%Y-%m-%dT%H:%M") if (t.deadline and (t.proposal_ok or "") == "pending") else ""),
        "expected_deadline_nice": (t.deadline.strftime("%d %b %Y, %I:%M %p") if (t.deadline and (t.proposal_ok or "") == "pending") else ""),
        "seconds_left": secs_left,
        "overdue": bool(secs_left is not None and secs_left < 0 and t.status == "assigned"),
        "status": t.status,
        "proposed_by": t.proposed_by, "proposal_ok": t.proposal_ok or "",
        "is_old": bool(getattr(t, "is_old", False)),
        "submitted_link": t.submitted_link or "",
        "submitted_at": t.submitted_at.strftime("%d %b %Y, %I:%M %p") if t.submitted_at else "",
        "on_time": t.on_time,
        "reviewed": bool(t.reviewed),
        "review_remarks": t.review_remarks or "",
        "reject_count": t.reject_count or 0,
        "no_resubmit": bool(getattr(t, "no_resubmit", False)),
        "comment_count": db.query(VideoTaskComment).filter(VideoTaskComment.task_id == t.id).count(),
        "kind": getattr(t, "kind", "normal") or "normal",
        "subject": getattr(t, "subject", "") or "",
        "weekly_quota": getattr(t, "weekly_quota", 0) or 0,
        "weekly_day": getattr(t, "weekly_day", "") or "",
        "item_source": getattr(t, "item_source", "") or "",
        "streaming": getattr(t, "streaming", "") or "",
        "youtube_url": getattr(t, "youtube_url", "") or "",
        "yt_video_id": getattr(t, "yt_video_id", "") or "",
        "yt_views": (t.yt_views if getattr(t, "yt_views", None) is not None else None),
        "yt_views_at": t.yt_views_at.strftime("%d %b %Y, %I:%M %p") if getattr(t, "yt_views_at", None) else "",
        "created_at": t.created_at.strftime("%d %b %Y") if t.created_at else "",
        "history": _hist_out(t),
    }
    # ---- collab (multi-teacher) info
    _allids = _collab_all_ids(t)
    _vmap = _collab_vmap(t)
    _ncmap = _collab_ncmap(t)
    out["is_collab"] = len(_allids) > 1
    out["collab_teachers"] = [{"id": i, "name": _tn(i),
                               "verified": bool(_vmap.get(str(i))),
                               "primary": (i == t.teacher_id),
                               "not_completed": bool(_ncmap.get(str(i)))} for i in _allids]
    out["collab_teacher_ids"] = _collab_extra_ids(t)
    out["collab_all_verified"] = bool(_allids) and all(
        (_vmap.get(str(i)) or _ncmap.get(str(i))) for i in _allids)
    # Kisne actually submit kiya (collab me koi bhi teacher kar sakta hai). Na ho to primary.
    _sub_by = getattr(t, "submitted_by", None)
    out["submitted_by"] = _sub_by or (t.teacher_id if t.submitted_at else None)
    out["submitted_by_name"] = _tn(out["submitted_by"]) if out["submitted_by"] else ""
    if with_thumb:
        out["thumbnail_b64"] = t.thumbnail_b64 or ""
    # ---- thumbnail (graphics) status so the creator knows if a thumbnail is coming
    out["thumbnail_required"] = bool(getattr(t, "thumbnail_required", False))
    # ---- production assignment info (so admin/PM cards can assign editor & graphics)
    out["lifecycle"] = getattr(t, "lifecycle", "") or ""
    out["editor_id"] = getattr(t, "editor_id", None)
    out["graphics_id"] = getattr(t, "graphics_id", None)
    try:
        from models import ProductionStaffProfile as _PSPx
        if out["editor_id"]:
            ep = db.query(_PSPx).filter(_PSPx.id == out["editor_id"]).first()
            out["editor_name"] = (ep.user.name if ep and ep.user else "") or ""
        else:
            out["editor_name"] = ""
        if out["graphics_id"]:
            gpx = db.query(_PSPx).filter(_PSPx.id == out["graphics_id"]).first()
            out["graphics_name"] = (gpx.user.name if gpx and gpx.user else "") or ""
        else:
            out["graphics_name"] = ""
    except Exception:
        out["editor_name"] = ""; out["graphics_name"] = ""
    try:
        if out["thumbnail_required"] or getattr(t, "graphics_id", None):
            from models import GraphicsTask as _GTt, ProductionStaffProfile as _PSP
            g = db.query(_GTt).filter(_GTt.task_id == t.id).order_by(_GTt.id.desc()).first()
            if g:
                designer = ""
                gp = db.query(_PSP).filter(_PSP.id == g.graphics_id).first()
                if gp and gp.user:
                    designer = gp.user.name or ""
                _tsecs = int((g.deadline - _now_ist()).total_seconds()) if g.deadline else None
                out["thumbnail"] = {
                    "status": g.status or "pending",
                    "designer": designer,
                    "deadline": g.deadline.strftime("%d %b %Y, %I:%M %p") if g.deadline else "",
                    "seconds_left": _tsecs,
                    "overdue": bool(_tsecs is not None and _tsecs < 0 and (g.status or "") not in ("approved", "submitted")),
                    "url": g.thumbnail_url or g.drive_link or "",
                    "approved": (g.status or "") == "approved",
                    "pending": (g.status or "pending") not in ("approved",),
                }
    except Exception:
        pass
    return out


def _special_out(db, t, tname_map=None):
    """One Shot / Rapid Revision task — chapters ke saath progress + NEW blink."""
    out = _task_out(db, t, with_thumb=False, tname_map=tname_map)
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.task_id == t.id)
           .order_by(VideoTaskChapter.sort.asc(), VideoTaskChapter.id.asc()).all())
    lla = getattr(t, "last_link_at", None)
    asa = getattr(t, "admin_seen_at", None)
    # v91: chapter-level change flag — link add/update/remove admin_seen ke baad hua ho
    out["chapters"] = [{
        "id": c.id, "title": c.title, "link": c.link or "",
        "submitted_at": c.submitted_at.strftime("%d %b %Y, %I:%M %p") if c.submitted_at else "",
        "edit_status": _ch_status(c),
        "vintage": (getattr(c, "vintage", "") or ""),
        "changed": bool(getattr(c, "changed_at", None) and (not asa or c.changed_at > asa)),
        "changed_at": c.changed_at.strftime("%d %b %Y, %I:%M %p") if getattr(c, "changed_at", None) else "",
    } for c in chs]
    done = sum(1 for c in chs if (c.link or "").strip())
    out["done"] = done
    out["total"] = len(chs)
    out["pct"] = round(100 * done / len(chs)) if chs else 0
    out["cls"] = _subject_cls(out.get("subject") or "")
    out["is_new"] = bool(lla and (not asa or lla > asa))
    out["last_link_at"] = lla.strftime("%d %b %Y, %I:%M %p") if lla else ""
    return out


def vt_task_rank_rows(db):
    """Task completion ranking — on-time delivery rate. Collab tasks kisi ek teacher ko
    nahi, ek alag 'Collab' row me (click par saare teacher naam)."""
    rows = []
    tps = db.query(TeacherProfile).all()
    _name = {}
    for tp in tps:
        u = db.query(User).filter(User.id == tp.user_id).first()
        _name[tp.id] = (u.name if u else "") or ("Teacher #%d" % tp.id)
    collab_seen = set()
    collab_all = []
    for tp in tps:
        tasks = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id,
                         VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                         VideoTask.cancelled.isnot(True)).all())
        # collab tasks alag — Collab row me jaayenge (primary ko attribute nahi)
        solo = []
        for t in tasks:
            if _collab_extra_ids(t):
                if t.id not in collab_seen:
                    collab_seen.add(t.id); collab_all.append(t)
            else:
                solo.append(t)
        if not solo:
            continue
        subs = [t for t in solo if t.submitted_at]
        done = len(subs)
        ontime = sum(1 for t in subs if t.on_time)
        delayed = sum(1 for t in subs if t.on_time is False)
        not_completed = sum(1 for t in solo if t.status == "not_completed")
        pending = sum(1 for t in solo if t.status == "assigned")
        rate = round(100 * ontime / done) if done else 0
        rows.append({
            "teacher_id": tp.id,
            "name": _name.get(tp.id, "Teacher #%d" % tp.id),
            "photo": bool(getattr(tp, "photo_b64", None)),
            "assigned": len(solo), "done": done, "pending": pending,
            "ontime": ontime, "delayed": delayed, "not_completed": not_completed, "rate": rate,
        })
    # ---- Collab aggregate row (koi individual name nahi) ----
    if collab_all:
        subs = [t for t in collab_all if t.submitted_at]
        done = len(subs)
        ontime = sum(1 for t in subs if t.on_time)
        delayed = sum(1 for t in subs if t.on_time is False)
        not_completed = sum(1 for t in collab_all if t.status == "not_completed")
        pending = sum(1 for t in collab_all if t.status == "assigned")
        rate = round(100 * ontime / done) if done else 0
        cnames = set()
        for t in collab_all:
            for tid in _collab_all_ids(t):
                if tid in _name:
                    cnames.add(_name[tid])
        rows.append({
            "teacher_id": 0, "name": "Collab", "is_collab": True,
            "collab_names": sorted(cnames), "photo": False,
            "assigned": len(collab_all), "done": done, "pending": pending,
            "ontime": ontime, "delayed": delayed, "not_completed": not_completed, "rate": rate,
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
@router.get("/admin/video-channels", dependencies=[Depends(_admin_section_guard)])
def vt_list_channels(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    rows = db.query(VideoChannel).order_by(VideoChannel.id.asc()).all()
    return {"channels": [{"id": c.id, "name": c.name, "active": bool(c.active)} for c in rows]}


@router.post("/admin/video-channels", dependencies=[Depends(_admin_section_guard)])
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
@router.get("/admin/video-types", dependencies=[Depends(_admin_section_guard)])
def vt_list_types(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_types(db)
    rows = db.query(VideoType).order_by(VideoType.sort.asc(), VideoType.id.asc()).all()
    return {"types": [{"id": c.id, "name": c.name, "active": bool(c.active),
                       "streaming_scope": getattr(c, "streaming_scope", "both") or "both"} for c in rows]}


@router.post("/admin/video-types", dependencies=[Depends(_admin_section_guard)])
def vt_add_type(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_types(db)
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
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "name": c.name, "streaming_scope": scope}


@router.post("/admin/video-types/{type_id}", dependencies=[Depends(_admin_section_guard)])
def vt_update_type(type_id: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), _=Depends(get_admin)):
    c = db.query(VideoType).filter(VideoType.id == type_id).first()
    if not c:
        raise HTTPException(404, "Type not found")
    if payload.get("streaming_scope") is not None:
        sc = (payload.get("streaming_scope") or "both").strip().lower()
        c.streaming_scope = sc if sc in ("both", "live", "recorded") else "both"
    if payload.get("active") is not None:
        c.active = bool(payload.get("active"))
    db.commit()
    return {"ok": True, "id": c.id, "streaming_scope": getattr(c, "streaming_scope", "both")}


@router.get("/teacher/video-channels")
def vt_teacher_channels(db: Session = Depends(get_db), _=Depends(get_teacher)):
    _seed_channels(db)
    rows = (db.query(VideoChannel).filter(VideoChannel.active == True)
            .order_by(VideoChannel.id.asc()).all())
    return {"channels": [{"id": c.id, "name": c.name} for c in rows]}


@router.get("/teacher/video-types")
def vt_teacher_types(db: Session = Depends(get_db), _=Depends(get_teacher)):
    _seed_types(db)
    rows = (db.query(VideoType).filter(VideoType.active == True)
            .order_by(VideoType.sort.asc(), VideoType.id.asc()).all())
    return {"types": [{"id": c.id, "name": c.name,
                       "streaming_scope": getattr(c, "streaming_scope", "both") or "both"} for c in rows]}


# =============================================================
# ADMIN — ASSIGN / LIST / STATS / REVIEW / PROPOSALS / REPORT
# =============================================================
@router.post("/admin/video-tasks", dependencies=[Depends(_admin_section_guard)])
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
    # collab teachers (primary ke alawa additional) — inko bhi wahi task dikhega
    import json as _json_c
    _collab = []
    for x in (payload.get("collab_teacher_ids") or []):
        try:
            xi = int(x)
            if xi and xi != tid and xi not in _collab:
                _collab.append(xi)
        except Exception:
            pass
    try:
        t = VideoTask(
            teacher_id=tid, title=title,
            channel_id=ch.id if ch else None,
            channel_name=ch.name if ch else "",
            subject=(payload.get("subject") or "").strip(),
            video_type=(payload.get("video_type") or "").strip(),
            thumbnail_b64=_checked_b64(payload),
            thumbnail_link=(payload.get("thumbnail_link") or "").strip(),
            reference=(payload.get("reference") or "").strip(),
            remarks=(payload.get("remarks") or "").strip(),
            streaming=(payload.get("streaming") or "").strip(),
            deadline=dl, status="assigned", proposed_by="admin", proposal_ok="approved",
        )
        if _collab:
            try: t.collab_teacher_ids = _json_c.dumps(_collab)
            except Exception: pass   # column abhi models me na ho to bhi crash na ho
        db.add(t)
        _hist_add(t, "assigned", "Deadline: %s" % dl.strftime("%d %b %Y, %I:%M %p"))
        # primary + collab sabko notify
        _notify_ids = [tid] + _collab
        _collab_note = (" (collab with %d more)" % len(_collab)) if _collab else ""
        for _ntid in _notify_ids:
            _ntp = _teacher_profile(db, _ntid)
            if _ntp and _ntp.user_id:
                _vt_notify(db, _ntp.user_id, "🎬 New Video Task Assigned",
                           f'You have been assigned a new video task: "{title}"'
                           + (f' for {ch.name}' if ch else '')
                           + _collab_note
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


@router.post("/admin/video-tasks/{task_id}/mark-old", dependencies=[Depends(_admin_section_guard)])
def vt_mark_old(task_id: int, payload: dict = Body(default={}),
                db: Session = Depends(get_db), _=Depends(get_admin)):
    """Project ko OLD/NEW mark karo. Old = pre-portal/purana content -> is month ke
    performance me count NAHI hoga. New (default) -> count hoga."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    t.is_old = bool((payload or {}).get("is_old", True))
    db.commit()
    try:
        import perf_engine as _pe; _pe.bust_board_cache()
    except Exception:
        pass
    return {"ok": True, "id": t.id, "is_old": bool(t.is_old)}


@router.post("/admin/video-tasks/{task_id}/verify-complete", dependencies=[Depends(_admin_section_guard)])
def vt_verify_complete(task_id: int, payload: dict = Body(...),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek hi action se poore collab task ko verify: 'Task Completed' -> saare collab
    teachers verified + status Approved + SAB teachers ko notification (sirf submit karne
    wale ko nahi). 'Task Not Completed' -> reopen (reshoot) + sab ko notify."""
    import json as _j
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    completed = bool(payload.get("completed", True))
    remarks = (payload.get("remarks") or "").strip()
    allids = _collab_all_ids(t)
    # teacher profile id -> user id (notification bhejne ke liye)
    def _uid(pid):
        _tp = db.query(TeacherProfile).filter(TeacherProfile.id == pid).first()
        return _tp.user_id if _tp else None
    if completed:
        ncmap = _collab_ncmap(t)
        # 'not completed' mark kiye teachers ko verify NAHI karte — baaki sab verified.
        _ok_ids = [i for i in allids if not ncmap.get(str(i))]
        _nc_ids = [i for i in allids if ncmap.get(str(i))]
        vmap = {str(i): True for i in _ok_ids}
        t.collab_verified = _j.dumps(vmap)
        t.status = "approved"
        t.reviewed = True
        if remarks:
            t.review_remarks = remarks
        _hist_add(t, "approved", "Task verified by production manager — %d completed, %d not completed"
                  % (len(_ok_ids), len(_nc_ids)))
        for pid in _ok_ids:
            uid = _uid(pid)
            if uid:
                _vt_notify(db, uid, "\u2705 Task Verified & Completed",
                           'Your collaborative task "%s" has been verified by the production manager. '
                           'Task complete — great teamwork!' % t.title)
        for pid in _nc_ids:
            uid = _uid(pid)
            if uid:
                _vt_notify(db, uid, "Your part was not completed",
                           'On the collaborative task "%s", your part was marked NOT completed by the '
                           'production manager. The video was approved for the other teachers, but this '
                           'counts as not completed for you and affects your payout.' % t.title,
                           ntype="warning")
    else:
        reshoot = bool(payload.get("reshoot", False))
        t.collab_verified = "{}"
        t.reviewed = True
        if reshoot:
            # Admin ne RESHOOT diya — task dobara khulta hai, teacher resubmit kar sakta hai.
            t.status = "reshoot"
            t.review_remarks = remarks or "Not completed — reshoot allowed by production manager"
            _hist_add(t, "reshoot", "Not completed — reshoot allowed"
                      + (" — " + remarks if remarks else ""))
            for pid in allids:
                uid = _uid(pid)
                if uid:
                    _vt_notify(db, uid, "\U0001f501 Reshoot Allowed",
                               'Your collaborative task "%s" was not completed, but the production manager '
                               'has allowed a reshoot%s. Please record and submit again.'
                               % (t.title, (" (" + remarks + ")") if remarks else ""))
        else:
            # NO reshoot — task LOCK. Teacher tab tak resubmit nahi kar sakta jab tak admin
            # reshoot na de. Warning + not-completed-on-time +1 + payout DELAY (on_time=False).
            t.status = "not_completed"
            t.on_time = False
            t.review_remarks = remarks or "Marked NOT completed (locked) by production manager"
            _hist_add(t, "rejected", "Task NOT completed — LOCKED (no reshoot)"
                      + (" — " + remarks if remarks else ""))
            for pid in allids:
                uid = _uid(pid)
                if uid:
                    _vt_notify(db, uid, "Task Not Completed — Locked",
                               'Your collaborative task "%s" was marked NOT COMPLETED by the production manager%s. '
                               'This counts as not completed on time and affects your payout (delayed). '
                               'You cannot resubmit until the production manager allows a reshoot.'
                               % (t.title, (" (" + remarks + ")") if remarks else ""),
                               ntype="warning")
    db.commit()
    return {"ok": True, "completed": completed, "status": t.status,
            "collab_teachers": [{"id": i, "name": _teacher_name(db, i),
                                 "verified": bool(_collab_vmap(t).get(str(i)))} for i in allids]}


@router.post("/admin/video-tasks/{task_id}/verify-teacher", dependencies=[Depends(_admin_section_guard)])
def vt_verify_teacher(task_id: int, payload: dict = Body(...),
                      db: Session = Depends(get_db), _=Depends(get_admin)):
    """Production manager collab task me har teacher ko alag verify karta hai.
    Sab verify -> task 'Approved'. Jo teacher task me hi nahi uska verify -> error
    (Task not completed by them)."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    tid = int(payload.get("teacher_id") or 0)
    verified = payload.get("verified", True)
    allids = _collab_all_ids(t)
    if tid not in allids:
        raise HTTPException(400, "This teacher is not part of this task.")
    import json as _j
    vmap = _collab_vmap(t)
    if verified:
        vmap[str(tid)] = True
    else:
        vmap.pop(str(tid), None)
    t.collab_verified = _j.dumps(vmap)
    all_ok = bool(allids) and all(vmap.get(str(i)) for i in allids)
    _hist_add(t, "verify", "%s %s by production manager" % (
        _teacher_name(db, tid), "verified" if verified else "verification removed"))
    if all_ok:
        _hist_add(t, "approved", "All collab teachers verified — Approved")
    db.commit()
    return {"ok": True, "all_verified": all_ok,
            "collab_teachers": [{"id": i, "name": _teacher_name(db, i),
                                 "verified": bool(vmap.get(str(i)))} for i in allids]}


@router.post("/admin/video-tasks/{task_id}/edit-collab", dependencies=[Depends(_admin_section_guard)])
def vt_edit_collab(task_id: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), _=Depends(get_admin)):
    """Add or remove collaborating teachers on an existing task (Admin).
    The primary teacher cannot be removed. Only edits the collaborator list and cleans
    per-teacher verify / not-completed flags — payout logic is untouched."""
    import json as _j
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    primary = t.teacher_id
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
    old_ids = _collab_extra_ids(t)
    added = [i for i in new_ids if i not in old_ids]
    removed = [i for i in old_ids if i not in new_ids]
    t.collab_teacher_ids = _j.dumps(new_ids)
    vmap = _collab_vmap(t); ncmap = _collab_ncmap(t)
    for rid in removed:
        vmap.pop(str(rid), None); ncmap.pop(str(rid), None)
    t.collab_verified = _j.dumps(vmap)
    t.collab_not_completed = _j.dumps(ncmap)
    for i in added:
        _hist_add(t, "collab_added", "%s added to collaboration by admin" % _teacher_name(db, i))
        _tp = db.query(TeacherProfile).filter(TeacherProfile.id == i).first()
        if _tp and _tp.user_id:
            _vt_notify(db, _tp.user_id, "Added to a collaboration",
                       'You have been added to the collaborative task "%s".' % (t.title or "a task"))
    for i in removed:
        _hist_add(t, "collab_removed", "%s removed from collaboration by admin" % _teacher_name(db, i))
        _tp = db.query(TeacherProfile).filter(TeacherProfile.id == i).first()
        if _tp and _tp.user_id:
            _vt_notify(db, _tp.user_id, "Removed from a collaboration",
                       'You are no longer part of the collaborative task "%s".' % (t.title or "a task"))
    db.commit()
    allids = _collab_all_ids(t)
    return {"ok": True, "added": len(added), "removed": len(removed),
            "collab_teachers": [{"id": i, "name": _teacher_name(db, i),
                                 "verified": bool(vmap.get(str(i))),
                                 "primary": (i == primary)} for i in allids]}


@router.get("/admin/collab-teachers", dependencies=[Depends(_admin_section_guard)])
def vt_collab_teachers(db: Session = Depends(get_db), _=Depends(get_admin)):
    """All active teachers (id + name) for the collaboration add/remove picker."""
    out = []
    for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
        out.append({"id": tp.id, "name": (tp.user.name if tp.user else ("Teacher #%s" % tp.id))})
    out.sort(key=lambda x: x["name"].lower())
    return {"teachers": out}


@router.post("/admin/video-tasks/{task_id}/mark-collab-teacher", dependencies=[Depends(_admin_section_guard)])
def vt_mark_collab_teacher(task_id: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), _=Depends(get_admin)):
    """Collab task me EK teacher ka state set karo: 'not_completed' / 'verified' / 'pending'.
    'not_completed' -> us teacher ne apna kaam nahi kiya; verify-all me wo verified nahi hoga
    aur uske payout par asar padega (baaki teachers verified ho jaate hain)."""
    import json as _j
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    tid = int(payload.get("teacher_id") or 0)
    state = (payload.get("state") or "").strip()
    allids = _collab_all_ids(t)
    if tid not in allids:
        raise HTTPException(400, "This teacher is not part of this task.")
    vmap = _collab_vmap(t)
    ncmap = _collab_ncmap(t)
    if state == "not_completed":
        ncmap[str(tid)] = True
        vmap.pop(str(tid), None)
        _hist_add(t, "verify", "%s marked NOT COMPLETED by production manager" % _teacher_name(db, tid))
        _tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
        if _tp and _tp.user_id:
            _vt_notify(db, _tp.user_id, "Your part was not completed",
                       'On the collaborative task "%s", your part was marked NOT completed by the '
                       'production manager. This affects your payout.' % t.title, ntype="warning")
    elif state == "verified":
        vmap[str(tid)] = True
        ncmap.pop(str(tid), None)
        _hist_add(t, "verify", "%s verified by production manager" % _teacher_name(db, tid))
    else:  # pending -> reset
        vmap.pop(str(tid), None)
        ncmap.pop(str(tid), None)
        _hist_add(t, "verify", "%s reset to pending by production manager" % _teacher_name(db, tid))
    t.collab_verified = _j.dumps(vmap)
    t.collab_not_completed = _j.dumps(ncmap)
    db.commit()
    return {"ok": True,
            "collab_teachers": [{"id": i, "name": _teacher_name(db, i),
                                 "verified": bool(vmap.get(str(i))),
                                 "not_completed": bool(ncmap.get(str(i)))} for i in allids]}


@router.get("/admin/video-tasks", dependencies=[Depends(_admin_section_guard)])
def vt_admin_list(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  video_type: str = "",
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                                   VideoTask.cancelled.isnot(True))
    if teacher_id:
        q = q.filter(VideoTask.teacher_id == teacher_id)
    if status:
        q = q.filter(VideoTask.status == status)
    if channel_id:
        q = q.filter(VideoTask.channel_id == channel_id)
    if video_type:
        q = q.filter(VideoTask.video_type == video_type)
    tasks = q.order_by(VideoTask.created_at.desc()).all()
    props = (db.query(VideoTask).filter(VideoTask.proposal_ok == "pending",
                                        VideoTask.cancelled.isnot(True))
             .order_by(VideoTask.created_at.desc()).all())
    urgent = (db.query(VideoTask).filter(VideoTask.kind == "urgent",
                                         VideoTask.cancelled.isnot(True))
              .order_by(VideoTask.created_at.desc()).all())
    _tnm = _all_teacher_names(db)   # ek query — per-task N+1 khatam (fast)
    return {"tasks": [_task_out(db, t, tname_map=_tnm) for t in tasks],
            "proposals": [_task_out(db, t, tname_map=_tnm) for t in props],
            "urgent": [_task_out(db, t, tname_map=_tnm) for t in urgent]}


@router.get("/admin/video-tasks/badge", dependencies=[Depends(_admin_section_guard)])
def vt_admin_badge(db: Session = Depends(get_db), _=Depends(get_admin)):
    """v115: halka sidebar badge count — Task Manager page khole bina bhi naya
    proposal ya review-pending submission ka indicator dikhe (dashboard load +
    notification poll pe refresh hota hai)."""
    checking = (db.query(VideoTask)
                .filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                        VideoTask.status == "submitted",
                        VideoTask.reviewed.isnot(True),
                        VideoTask.cancelled.isnot(True))
                .count())
    proposals = db.query(VideoTask).filter(VideoTask.proposal_ok == "pending",
                                           VideoTask.cancelled.isnot(True)).count()
    urgent = (db.query(VideoTask)
              .filter(VideoTask.kind == "urgent",
                      VideoTask.status != "uploaded",
                      VideoTask.cancelled.isnot(True))
              .count())
    return {"checking": checking, "proposals": proposals,
            "urgent": urgent,
            "count": checking + proposals}


@router.get("/admin/video-tasks/stats", dependencies=[Depends(_admin_section_guard)])
def vt_admin_stats(db: Session = Depends(get_db), _=Depends(get_admin)):
    _seed_channels(db)
    _vt_sweep(db)
    tasks = (db.query(VideoTask)
             .filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                     VideoTask.cancelled.isnot(True)).all())
    now = _now_ist()
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


@router.post("/admin/video-tasks/{task_id}/review", dependencies=[Depends(_admin_section_guard)])
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

    if action in ("rejected", "reshoot"):
        final = bool(payload.get("final") or payload.get("no_resubmit"))
        ndl = _parse_deadline(payload.get("new_deadline"))
        if not final and not ndl:
            raise HTTPException(400, "Set a new deadline, or choose Final reject (no re-submission)")
        t.status = action            # "reshoot" / "rejected" — filter me track hota hai
        t.reject_count = (t.reject_count or 0) + 1
        t.reviewed = True
        t.review_remarks = remarks
        t.submitted_link = ""
        t.submitted_at = None
        t.on_time = None
        t.warned_24h = False
        t.warned_overdue = False
        t.no_resubmit = final
        _word = "Reshoot" if action == "reshoot" else "Rejected"
        if final:
            # final: no re-submission — only remarks are shown to the creator
            _hist_add(t, action, ("%s — final, no re-submission" % _word)
                      + (f": {remarks}" if remarks else ""))
            if uid:
                _vt_notify(db, uid, f"Video Task {_word} — no re-submission",
                           f'Your submission for "{t.title}" was {_word.lower()}.'
                           + (f' Remarks: {remarks}' if remarks else '')
                           + ' No re-submission is required.',
                           link=str(t.id))
        else:
            t.deadline = ndl
            _hist_add(t, action, ("%s — sent back for re-submission" % _word)
                      + (f": {remarks}" if remarks else "")
                      + " — new deadline: " + ndl.strftime("%d %b %Y, %I:%M %p"))
            if uid:
                _vt_notify(db, uid, f"Video Task Sent Back — {_word}",
                           f'Your submission for "{t.title}" needs a {_word.lower()}'
                           + (f': {remarks}' if remarks else '.')
                           + f' New deadline: {ndl.strftime("%d %b %Y, %I:%M %p")}. '
                           f'Please submit again from My Tasks.',
                           link=str(t.id))
    else:
        t.status = action
        t.reviewed = True
        t.review_remarks = remarks
        # Urgent video: on-time = teacher ki di hui deadline tak UPLOAD hua ya nahi
        if action == "uploaded" and (getattr(t, "kind", "") or "") == "urgent" and t.deadline:
            t.on_time = (_now_ist() <= t.deadline)
        label = {"approved": "Approved", "editing_soon": "Editing Soon",
                 "editing_done": "Editing Done", "uploaded": "Uploaded"}[action]
        _hist_add(t, action, remarks)
        if uid:
            _vt_notify(db, uid, f"✅ Video Task Update — {label}",
                       f'Your video "{t.title}" status is now: {label}'
                       + (f'. Remarks: {remarks}' if remarks else '.'))
    db.commit()
    return {"ok": True, "status": t.status}


@router.post("/admin/video-tasks/{task_id}/approve-proposal", dependencies=[Depends(_admin_section_guard)])
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
            t.thumbnail_b64 = __import__("r2_storage").normalize(b64, "thumbnails", "image/jpeg")
        if payload.get("thumbnail_link"):
            t.thumbnail_link = payload["thumbnail_link"].strip()
        if payload.get("reference"):
            t.reference = payload["reference"].strip()
        if payload.get("remarks"):
            t.remarks = payload["remarks"].strip()
        if payload.get("video_type") is not None:
            t.video_type = (payload.get("video_type") or "").strip()
        if (payload.get("subject") or "").strip():
            t.subject = payload["subject"].strip()   # admin ne approval par subject chuna to override, warna proposal ka subject rahe
        # collab carry-forward: agar payload me teachers aaye to set karo, warna proposal
        # par jo the wahi rahein (admin ko dobara select na karna pade).
        import json as _json_ap
        _ap_collab = []
        for x in (payload.get("collab_teacher_ids") or []):
            try:
                xi = int(x)
            except Exception:
                continue
            if xi and xi != t.teacher_id and xi not in _ap_collab:
                _ap_collab.append(xi)
        if _ap_collab:
            try: t.collab_teacher_ids = _json_ap.dumps(_ap_collab)
            except Exception: pass
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
        # collab teachers ko bhi batao — unke My Tasks me ye video aa jaayega
        try:
            _ap_ids = _json_ap.loads(t.collab_teacher_ids) if getattr(t, "collab_teacher_ids", "") else []
        except Exception:
            _ap_ids = []
        for _cid in _ap_ids:
            _ctp = _teacher_profile(db, _cid)
            if _ctp and _ctp.user_id and _ctp.user_id != (tp.user_id if tp else None):
                _vt_notify(db, _ctp.user_id, "🎬 Collab Video Task",
                           f'You are a collaborator on the approved video "{t.title}". '
                           f'Deadline: {dl.strftime("%d %b %Y, %I:%M %p")}. Check My Tasks.')
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(400, f"Could not approve the proposal: {e}")
    return {"ok": True, "id": t.id}


@router.post("/admin/video-tasks/{task_id}/reject-proposal", dependencies=[Depends(_admin_section_guard)])
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


@router.post("/admin/video-tasks/{task_id}/notify-students", dependencies=[Depends(_admin_section_guard)])
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


@router.post("/admin/video-tasks/{task_id}/edit", dependencies=[Depends(_admin_section_guard)])
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
    if payload.get("streaming") is not None:
        st2 = (payload.get("streaming") or "").strip()
        if st2 != (getattr(t, "streaming", "") or ""):
            changes.append("streaming")
            t.streaming = st2
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
    if (getattr(t, "kind", "") or "") in ("one_shot", "rapid_revision", "project"):
        if payload.get("weekly_quota") is not None:
            try:
                wq = max(0, min(50, int(payload.get("weekly_quota") or 0)))
            except Exception:
                wq = 0
            if wq != (getattr(t, "weekly_quota", 0) or 0):
                changes.append("weekly target → %d videos/week" % wq if wq else "weekly target hataya")
                t.weekly_quota = wq
        if payload.get("weekly_day") is not None:
            wd = (payload.get("weekly_day") or "").strip().lower()
            if wd and wd not in WEEK_DAYS:
                raise HTTPException(400, "Weekly day invalid — monday..sunday")
            if wd != (getattr(t, "weekly_day", "") or ""):
                changes.append("weekly deadline day → " + (wd.title() if wd else "—"))
                t.weekly_day = wd
        # v92: chapters bhi edit — admin checklist se select kare kaunse chapters
        # task me rahen (TMA hatana ho ya koi wapas add karna ho). Link wali row
        # remove ho to wo bhi allow (admin ka conscious choice) — history me note.
        sel = payload.get("chapters")
        if sel is not None:
            if not isinstance(sel, list):
                raise HTTPException(400, "chapters must be a list of titles")
            keep, seen_k = [], set()
            for x in sel:
                s2 = re.sub(r"\s+", " ", str(x or "")).strip()[:300]
                if s2 and s2.lower() not in seen_k:
                    seen_k.add(s2.lower())
                    keep.append(s2)
            if not keep:
                raise HTTPException(400, "At least one chapter must stay selected")
            rows = (db.query(VideoTaskChapter)
                    .filter(VideoTaskChapter.task_id == t.id).all())
            removed = 0
            for crow in rows:
                if (crow.title or "").strip().lower() not in seen_k:
                    db.delete(crow)
                    removed += 1
            existing = {(crow.title or "").strip().lower() for crow in rows
                        if (crow.title or "").strip().lower() in seen_k}
            sort = max([getattr(crow, "sort", 0) or 0 for crow in rows] + [-1]) + 1
            added = 0
            for s2 in keep:
                if s2.lower() not in existing:
                    db.add(VideoTaskChapter(task_id=t.id, title=s2, sort=sort))
                    sort += 1
                    added += 1
            if removed or added:
                changes.append("chapters (%d added, %d removed)" % (added, removed))
    for fld, col in (("reference", "reference"), ("reference_video", "reference_video"), ("remarks", "remarks")):
        if payload.get(fld) is not None:
            v = (payload.get(fld) or "").strip()
            if v != (getattr(t, col) or ""):
                changes.append(fld)
                setattr(t, col, v)
    try:
        b64 = _checked_b64(payload)
        if b64:
            t.thumbnail_b64 = __import__("r2_storage").normalize(b64, "thumbnails", "image/jpeg")
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


def _purge_task_children(db, task_id):
    """Delete every child row that references a video_task via FK, so deleting the task
    does not hit a foreign-key constraint (MySQL error 1451). Safe if a table/row is absent."""
    try:
        db.query(VideoTaskChapter).filter(VideoTaskChapter.task_id == task_id).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        db.query(VideoTaskComment).filter(VideoTaskComment.task_id == task_id).delete(synchronize_session=False)
    except Exception:
        pass
    try:
        from models import VideoViewSnapshot as _VVS
        db.query(_VVS).filter(_VVS.task_id == task_id).delete(synchronize_session=False)
    except Exception:
        pass
    # Production management tables also FK to video_tasks.id
    try:
        from models import (EditingSession as _ES, GraphicsTask as _GT, TaskReview as _TR,
                             ProductionEvent as _PE, TaskAttachment as _TA)
        for _M in (_ES, _GT, _TR, _PE, _TA):
            try:
                db.query(_M).filter(_M.task_id == task_id).delete(synchronize_session=False)
            except Exception:
                pass
    except Exception:
        pass


@router.delete("/admin/video-tasks/{task_id}", dependencies=[Depends(_admin_section_guard)])
def vt_delete(task_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """v97: assigned video task delete — chapter rows bhi saath clean. Permanent action.
    Teacher ko notification jati hai taaki My Tasks se gayab hone pe confusion na ho."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    title = t.title or "Video task"
    _purge_task_children(db, t.id)
    tp = _teacher_profile(db, t.teacher_id)
    db.delete(t)
    if tp and tp.user_id:
        _vt_notify(db, tp.user_id, "🗑️ Video Task Removed",
                   f'Your video task "{title}" was removed by the admin. '
                   f'It no longer appears in My Tasks.')
    db.commit()
    return {"ok": True, "message": "Task deleted"}


@router.get("/admin/video-tasks/{task_id}/chapters", dependencies=[Depends(_admin_section_guard)])
def vt_task_chapter_options(task_id: int,
                            db: Session = Depends(get_db), _=Depends(get_admin)):
    """Edit modal ki chapter-checklist: subject ka poora syllabus master (PE/TMA tag
    ke saath) + current task chapters pre-checked. Custom titles (master me na hon)
    bhi dikhte hain — warna admin save karte hi wo silently remove ho jaate."""
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    rows = (db.query(VideoTaskChapter)
            .filter(VideoTaskChapter.task_id == t.id)
            .order_by(VideoTaskChapter.sort.asc(), VideoTaskChapter.id.asc()).all())
    current = [c.title for c in rows]
    curset = {x.strip().lower() for x in current}
    avail, source = [], ""
    base, cls = _display_base_cls(t.subject)
    if base:
        avail, source = _tagged_chapters_for(db, base, cls)
    items = [{"title": a["title"], "kind": a["kind"],
              "sel": a["title"].strip().lower() in curset} for a in avail]
    have = {a["title"].strip().lower() for a in avail}
    for cti in current:
        if cti.strip().lower() not in have:
            items.append({"title": cti, "kind": "", "sel": True})
    return {"items": items, "source": source, "total": len(current)}


def _subject_teachers(db, subject, class_level=""):
    """Subject (+optional class '10'/'12') ke ACTIVE teachers — auto-fetch."""
    from subjects_registry import squash
    sq = squash(subject)
    out = []
    if not sq:
        return out
    lv_want = class_level if class_level in ("10", "12") else ""
    for tp in db.query(TeacherProfile).all():
        _u = db.query(User).filter(User.id == tp.user_id).first()
        if _u is not None and _u.is_active is False:
            continue
        for nm, cl in _teacher_subject_list(db, tp):
            if squash(nm) != sq:
                continue
            lv = _class_level(cl)
            if lv_want and lv and lv != lv_want:
                continue
            out.append({"profile_id": tp.id,
                        "name": (_u.name if _u else "") or ("Teacher #%d" % tp.id),
                        "class": cl, "level": lv})
    return out


@router.get("/admin/video-tasks/subject-teachers", dependencies=[Depends(_admin_section_guard)])
def vt_subject_teachers(subject: str = "", class_level: str = "",
                        db: Session = Depends(get_db), _=Depends(get_admin)):
    """Project assign form — subject chunte hi teacher auto-fetch."""
    return {"teachers": _subject_teachers(db, subject, class_level)}


@router.post("/admin/video-tasks/project", dependencies=[Depends(_admin_section_guard)])
def vt_create_project(payload: dict = Body(...),
                      db: Session = Depends(get_db), _=Depends(get_admin)):
    """Multi-video PROJECT assign karo — weekly quota/day + FINAL deadline ke saath.
    Items: syllabus chapters se (connect=true) YA custom naam list (connect=false —
    timetable/chapters/parts se juda nahi). Teacher: manual ya subject se auto."""
    subject = (payload.get("subject") or "").strip()
    class_level = (payload.get("class_level") or "").strip()
    if class_level not in ("10", "12"):
        class_level = ""
    connect = bool(payload.get("connect"))
    final_dl = _parse_deadline(payload.get("deadline"))
    if not final_dl:
        raise HTTPException(400, "Final deadline is required")
    # teacher — manual dropdown ya subject se auto
    tp = None
    tid = int(payload.get("teacher_id") or 0)
    if tid:
        tp = _teacher_profile(db, tid)
        if not tp:
            raise HTTPException(404, "Teacher not found")
    elif subject:
        matches = _subject_teachers(db, subject, class_level)
        if not matches:
            raise HTTPException(400, "No active teacher found for this subject — "
                                     "please select one manually.")
        tp = _teacher_profile(db, matches[0]["profile_id"])
    if not tp:
        raise HTTPException(400, "Select a teacher, or choose a subject for auto-fetch.")
    display = _stable_subject_display(subject, class_level) if subject else ""
    title = (payload.get("title") or "").strip()
    if not title:
        title = "Project — %s" % display if display else ""
    if not title:
        raise HTTPException(400, "A subject or a project title is required")
    try:
        weekly_quota = max(0, min(50, int(payload.get("weekly_quota") or 0)))
    except Exception:
        weekly_quota = 0
    weekly_day = (payload.get("weekly_day") or "").strip().lower()
    if weekly_day and weekly_day not in WEEK_DAYS:
        raise HTTPException(400, "Invalid weekly day — use monday..sunday")
    # items — syllabus chapters (connect) ya custom list
    # chapter_scope: 'pe' = sirf Public Exam chapters (teacher ko TMA shoot nahi
    # karne), 'tma' = sirf TMA, '' / 'all' = PE+TMA dono
    chapter_scope = (payload.get("chapter_scope") or "").strip().lower()
    if chapter_scope not in ("pe", "tma"):
        chapter_scope = ""
    item_source, items = "custom", []
    if connect and subject:
        items, _src = _chapters_for(db, tp.id, subject, class_level, chapter_scope)
        item_source = "syllabus"
        if not items:
            raise HTTPException(400, "No chapters found for this scope in the syllabus "
                                     "manager — choose a different chapter scope or enter "
                                     "video names manually (Connect: No).")
    else:
        seen_it = set()
        for it in (payload.get("items") or []):
            s2 = re.sub(r"\s+", " ", str(it or "")).strip()
            if s2 and s2.lower() not in seen_it:
                seen_it.add(s2.lower())
                items.append(s2[:300])
            if len(items) >= 100:
                break
        if not items:
            raise HTTPException(400, "Add at least 1 video/item name "
                                     "(or turn on syllabus connect).")
    import json as _json_cp
    _collab_p = []
    for x in (payload.get("collab_teacher_ids") or []):
        try:
            xi = int(x)
            if xi and xi != tp.id and xi not in _collab_p:
                _collab_p.append(xi)
        except Exception:
            pass
    t = VideoTask(teacher_id=tp.id, title=title, kind="project", subject=display,
                  video_type="Project", status="assigned", proposed_by="admin",
                  proposal_ok="approved", deadline=final_dl,
                  remarks=(payload.get("remarks") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  weekly_quota=weekly_quota, weekly_day=weekly_day,
                  item_source=item_source)
    if _collab_p:
        try: t.collab_teacher_ids = _json_cp.dumps(_collab_p)
        except Exception: pass
    db.add(t)
    db.flush()
    if items:
        _sync_chapters(db, t, items)
    wk = []
    if weekly_quota:
        wk.append("%d videos/week" % weekly_quota)
    if weekly_day:
        wk.append("due every %s" % weekly_day.title())
    scope_lbl = {"pe": "PE chapters only", "tma": "TMA chapters only"}.get(
        chapter_scope, "PE + TMA chapters")
    items_lbl = ("%d video items (%s)" % (len(items), scope_lbl)) if item_source == "syllabus" \
        else "%d custom video items" % len(items)
    _hist_add(t, "assigned", "Project assigned — %s. %s. Final deadline: %s" % (
        ("Weekly: " + " · ".join(wk)) if wk else "No weekly target",
        items_lbl, final_dl.strftime("%d %b %Y, %I:%M %p")))
    if tp.user_id:
        _vt_notify(db, tp.user_id, "New Project — %s" % title,
                   'You have been assigned a new project: "%s" (%d videos). %s'
                   'Final deadline: %s. Paste each video\'s link in My Tasks as '
                   'you complete them.'
                   % (title, len(items),
                      ("Weekly target: " + " · ".join(wk) + ". " if wk else ""),
                      final_dl.strftime("%d %b %Y, %I:%M %p")))
    # Agar ye kisi teacher-proposal se approve hua hai -> us proposal ko yahin
    # positively close karo. Pehle frontend galti se reject-proposal call karta tha
    # jisse proposal card REJECTED dikhta tha aur teacher ko "Not Approved" notification
    # jaata tha. Ab project ban gaya, isliye original proposal row hata do (queue +
    # task list dono se) taaki koi leftover/rejected card na bache.
    try:
        prop_id = int(payload.get("proposal_id") or 0)
    except Exception:
        prop_id = 0
    if prop_id:
        prop = db.query(VideoTask).filter(VideoTask.id == prop_id,
                                          VideoTask.proposal_ok == "pending").first()
        if prop and prop.id != t.id:
            ptp = _teacher_profile(db, prop.teacher_id)
            if ptp and ptp.user_id and ptp.user_id != tp.user_id:
                _vt_notify(db, ptp.user_id, "Video Proposal Approved",
                           'Your proposal "%s" has been approved and assigned as a project.'
                           % (prop.title or "Video"))
            _purge_task_children(db, prop.id)
            db.delete(prop)
    db.commit()
    return {"ok": True, "id": t.id, "teacher": _teacher_name(db, tp.id),
            "total": len(items)}


@router.get("/admin/video-tasks/project-chapters", dependencies=[Depends(_admin_section_guard)])
def vt_admin_project_chapters(subject: str = "", class_level: str = "",
                              scope: str = "", teacher_id: int = 0,
                              db: Session = Depends(get_db), _=Depends(get_admin)):
    """Project assign form ka LIVE preview — syllabus connect on hone pe is
    subject/class/scope me kitne chapters video items banenge. count + sample."""
    subject = (subject or "").strip()
    if not subject:
        return {"count": 0, "titles": [], "source": "none"}
    if class_level not in ("10", "12"):
        class_level = ""
    tp = _teacher_profile(db, teacher_id) if teacher_id else None
    titles, src = _chapters_for(db, tp.id if tp else 0, subject, class_level, scope)
    return {"count": len(titles), "titles": titles[:8], "source": src}


def _special_payload(db, kind):
    tasks = (db.query(VideoTask).filter(VideoTask.kind == kind)
             .order_by(VideoTask.created_at.asc()).all())
    _tnm = _all_teacher_names(db)
    outs = [_special_out(db, t, tname_map=_tnm) for t in tasks]
    # v115: read-side guarantee — same teacher + same (normalized) subject ke
    # duplicate cards UI me kabhi na dikhen. DB self-heal _dedupe_special karta
    # hai; ye sirf display merge hai (kuch write nahi hota).
    def _nk(x):
        return re.sub(r"\s+", " ", (x or "")).strip().lower()
    seen, merged = {}, []
    for o in outs:
        key = (o.get("teacher_id"), _nk(o.get("subject") or o.get("title")))
        tgt = seen.get(key)
        if tgt is None:
            seen[key] = o
            merged.append(o)
            continue
        have = {_nk(c.get("title")): c for c in tgt.get("chapters", [])}
        for c in (o.get("chapters") or []):
            ck = _nk(c.get("title"))
            if ck not in have:
                tgt["chapters"].append(c)
                have[ck] = c
            elif (c.get("link") or "").strip() and not (have[ck].get("link") or "").strip():
                have[ck].update(c)
        tgt["done"] = sum(1 for c in tgt["chapters"] if (c.get("link") or "").strip())
        tgt["total"] = len(tgt["chapters"])
        tgt["pct"] = round(100 * tgt["done"] / tgt["total"]) if tgt["total"] else 0
        tgt["is_new"] = bool(tgt.get("is_new") or o.get("is_new"))
        if (o.get("last_link_at") or "") > (tgt.get("last_link_at") or ""):
            tgt["last_link_at"] = o["last_link_at"]
    outs = merged
    # NEW wale pehle, phir zyada progress wale
    outs.sort(key=lambda o: (not o["is_new"], -o["pct"], o["teacher"]))
    new_count = sum(1 for o in outs if o["is_new"])
    subjects = sorted({o["subject"] for o in outs if o["subject"]})
    return {"tasks": outs, "new_count": new_count, "subjects": subjects}


@router.get("/admin/video-tasks/special", dependencies=[Depends(_admin_section_guard)])
def vt_admin_special(kind: str = "one_shot",
                     db: Session = Depends(get_db), _=Depends(get_admin)):
    """One Shot / Rapid Revision tasks sabhi teachers ke — chapters + progress +
    NEW blink (last_link_at > admin_seen_at). kind=all pe dono ek saath."""
    if kind not in ("one_shot", "rapid_revision", "project", "all"):
        raise HTTPException(400, "Invalid kind")
    for tp in db.query(TeacherProfile).all():
        try:
            _ensure_special_teacher(db, tp)
        except Exception:
            db.rollback()
    if kind == "all":
        return {"one_shot": _special_payload(db, "one_shot"),
                "rapid_revision": _special_payload(db, "rapid_revision"),
                "project": _special_payload(db, "project")}
    return _special_payload(db, kind)


@router.post("/admin/video-tasks/{task_id}/seen", dependencies=[Depends(_admin_section_guard)])
def vt_admin_seen(task_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    t.admin_seen_at = _now_ist()
    db.commit()
    return {"ok": True}


@router.get("/admin/video-tasks/report.csv", dependencies=[Depends(_admin_section_guard)])
def vt_report_csv(teacher_id: int = 0, status: str = "", channel_id: int = 0,
                  video_type: str = "",
                  db: Session = Depends(get_db), _=Depends(get_admin)):
    import csv
    import io
    q = db.query(VideoTask).filter(VideoTask.proposal_ok != "pending", NOT_SPECIAL,
                                   VideoTask.cancelled.isnot(True))
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
    try:
        tasks = (db.query(VideoTask)
                 .filter(or_(VideoTask.teacher_id == tp.id,
                             VideoTask.collab_teacher_ids.like('%' + str(tp.id) + '%')),
                         VideoTask.cancelled.isnot(True),
                         or_(NOT_SPECIAL, VideoTask.kind == "urgent"))
                 .order_by(VideoTask.created_at.desc()).all())
        tasks = [t for t in tasks if tp.id in _collab_all_ids(t)]
    except Exception:
        # models me collab column abhi na ho to sirf primary teacher ke tasks
        tasks = (db.query(VideoTask)
                 .filter(VideoTask.teacher_id == tp.id,
                         VideoTask.cancelled.isnot(True),
                         or_(NOT_SPECIAL, VideoTask.kind == "urgent"))
                 .order_by(VideoTask.created_at.desc()).all())
    active = [t for t in tasks if t.status == "assigned" and t.proposal_ok != "pending"]
    active.sort(key=lambda t: t.deadline or datetime.max)
    rest = [t for t in tasks if t not in active]
    _tnm2 = _all_teacher_names(db)
    out = [_task_out(db, t, tname_map=_tnm2) for t in active + rest]
    nxt = active[0] if active else None
    # teacher ke apne stats: kitni upload hui, pending, on-time, delayed + is mahine type-wise
    now = _now_ist()
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
        "not_completed": sum(1 for t in real if t.status == "not_completed"),
        "month_types": month_type,
    }
    # special tasks (One Shot per subject + Rapid Revision) — chapters ke saath
    try:
        spts = (db.query(VideoTask)
                .filter(or_(VideoTask.teacher_id == tp.id,
                            VideoTask.collab_teacher_ids.like('%' + str(tp.id) + '%')),
                        VideoTask.cancelled.isnot(True),
                        VideoTask.kind.in_(["one_shot", "rapid_revision", "project"]))
                .order_by(VideoTask.kind.asc(), VideoTask.subject.asc()).all())
        spts = [t for t in spts if tp.id in _collab_all_ids(t)]
    except Exception:
        spts = (db.query(VideoTask)
                .filter(VideoTask.teacher_id == tp.id,
                        VideoTask.cancelled.isnot(True),
                        VideoTask.kind.in_(["one_shot", "rapid_revision", "project"]))
                .order_by(VideoTask.kind.asc(), VideoTask.subject.asc()).all())
    # legacy "All Subjects"/empty-subject card kabhi na dikhe — sirf subject-wise cards
    spts = [t for t in spts if (t.subject or "").strip().lower() not in ("", "all subjects")]
    special_all = [_special_out(db, t, tname_map=_tnm2) for t in spts]
    # Bulletproof display dedup: DB me duplicate ho (alag spelling/spacing/id) to bhi
    # teacher ko (kind + class-aware subject) ke hisaab se SIRF EK card dikhe —
    # sabse zyada progress (done chapters) wala. DB merge alag se _ensure_special_teacher me.
    _seen = {}
    for so in special_all:
        _b, _c = _display_base_cls(so.get("subject") or "")
        key = (so.get("kind"), _subj_ident(_b, _c))
        cur = _seen.get(key)
        if cur is None or (so.get("done", 0) > cur.get("done", 0)):
            _seen[key] = so
    special = sorted(_seen.values(),
                     key=lambda s: (s.get("kind") or "", s.get("subject") or ""))
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
    # Task ya Project propose + scope (complete chapter / N videos) -> readable remark
    ptype = (payload.get("propose_type") or "task").strip().lower()
    scope = (payload.get("project_scope") or "").strip().lower()
    vcount = str(payload.get("video_count") or "").strip()
    # teacher ne konse subject/class ke liye project maanga — admin approve modal me
    # auto pre-fill hoga (aur timetable se sahi chapters aayenge)
    psub = (payload.get("subject") or "").strip()
    pcls = (payload.get("class_level") or "").strip()
    if pcls not in ("10", "12"):
        pcls = ""
    subj_store = (("%s %s" % (psub, pcls)).strip() if pcls else psub) if psub else ""
    subj_label = (("%s · Class %s" % (psub, pcls)) if pcls else psub) if psub else ""
    if ptype == "project":
        if scope == "chapter":
            req = "Requested: PROJECT — complete chapter (from timetable)"
        elif scope == "videos":
            items = payload.get("video_items") or []
            items = [str(x).strip() for x in items if str(x or "").strip()]
            if items:
                req = ("Requested: PROJECT — set of %d videos:\n" % len(items)
                       + "\n".join("%d. %s" % (i + 1, v) for i, v in enumerate(items)))
            else:
                req = "Requested: PROJECT — %s videos" % (vcount or "4-5")
        else:
            req = "Requested: PROJECT"
        if subj_label:
            req += "\nSubject: " + subj_label
    else:
        req = "Requested: TASK — single video"
        if subj_label:
            req += "\nSubject: " + subj_label
    # collab (multiple teachers) — teacher doosre teachers ke saath propose kar sakta hai.
    # Proposal par hi store; approve hone par task inhi teachers ko dikhega (dobara set nahi karna padta).
    import json as _json_p
    _collab_p = []
    for x in (payload.get("collab_teacher_ids") or []):
        try:
            xi = int(x)
        except Exception:
            continue
        if xi and xi != tp.id and xi not in _collab_p:
            _tt = db.query(TeacherProfile).filter(TeacherProfile.id == xi).first()
            if _tt:
                _collab_p.append(xi)
    if _collab_p:
        req += "\nCollab with %d more teacher(s)" % len(_collab_p)
    t = VideoTask(teacher_id=tp.id, title=title,
                  channel_id=ch.id if ch else None,
                  channel_name=ch.name if ch else "",
                  video_type=(payload.get("video_type") or "").strip(),
                  thumbnail_b64=_checked_b64(payload),
                  thumbnail_link=(payload.get("thumbnail_link") or "").strip(),
                  streaming=(payload.get("streaming") or "").strip(),
                  reference=(payload.get("reference") or "").strip(),
                  remarks=req, subject=subj_store,
                  deadline=_parse_deadline(payload.get("expected_deadline")),
                  status="proposal", proposed_by="teacher", proposal_ok="pending")
    if _collab_p:
        try: t.collab_teacher_ids = _json_p.dumps(_collab_p)
        except Exception: pass
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


@router.post("/teacher/video-tasks/urgent")
def vt_urgent(payload: dict = Body(...), db: Session = Depends(get_db),
              current_user=Depends(get_teacher)):
    """Urgent video: teacher seedha title + channel + video link + upload deadline daalta hai.
    Koi approval nahi — turant submitted. Production manager ka flow same (approve/status/YT link).
    on-time = teacher ki di hui deadline tak UPLOAD hua ya nahi (upload pe compute hota hai)."""
    tp = _get_tp(current_user, db)
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "A video title is required")
    link = (payload.get("link") or payload.get("submitted_link") or "").strip()
    cid = payload.get("channel_id")
    ch = db.query(VideoChannel).filter(VideoChannel.id == int(cid)).first() if cid else None
    dl = _parse_deadline(payload.get("deadline"))
    now = _now_ist()
    t = VideoTask(teacher_id=tp.id, title=title, kind="urgent",
                  channel_id=ch.id if ch else None, channel_name=ch.name if ch else "",
                  video_type=(payload.get("video_type") or "").strip(),
                  streaming=(payload.get("streaming") or "").strip(),
                  deadline=dl,
                  submitted_link=link, submitted_at=(now if link else None),
                  on_time=None,
                  status=("submitted" if link else "assigned"),
                  proposed_by="teacher", proposal_ok="approved", reviewed=False)
    db.add(t)
    _hist_add(t, "urgent", "Urgent video submitted by teacher")
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "\U0001F6A8 Urgent Video Submitted",
                   f'{(uname.name if uname else "A teacher")} submitted an URGENT video: "{title}". '
                   f'Review it in Task Manager \u2192 Urgent Videos.')
    db.commit()
    return {"ok": True, "id": t.id}


@router.post("/teacher/video-tasks/{task_id}/submit")
def vt_submit(task_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
              current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t or tp.id not in _collab_all_ids(t):
        raise HTTPException(404, "Task not found")
    if (getattr(t, "kind", "normal") or "normal") != "normal":
        raise HTTPException(400, "For One Shot / Rapid Revision tasks, paste the link next to each chapter")
    if t.status not in ("assigned", "reshoot", "rejected"):
        raise HTTPException(400, "This task is not open for submission")
    link = (payload.get("link") or "").strip()
    if not link:
        raise HTTPException(400, "Please paste the drive link of your video")
    now = _now_ist()
    t.submitted_link = link
    t.submitted_at = now
    t.submitted_by = tp.id
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
        _vt_notify(db, tp.user_id, "Late Submission Noted",
                   f'Your video "{t.title}" was submitted after the deadline. '
                   f'Repeated delays may affect your payout.')
    uname = db.query(User).filter(User.id == tp.user_id).first()
    admins = db.query(User).filter(User.role == "admin", User.is_active == True).all()
    for a in admins:
        _vt_notify(db, a.id, "📥 Video Submitted for Checking",
                   f'{(uname.name if uname else "A teacher")} submitted the video '
                   f'"{t.title}" ({"on time" if t.on_time else "delayed"}). '
                   f'Please review it in Task Manager.')
    # Production bridge: if this task is tracked by the production system, advance
    # its lifecycle so it appears in PM Review (never breaks the legacy flow).
    try:
        if getattr(t, "lifecycle", "") in ("creator_assigned", "creator_working",
                                            "changes_required", "reshoot_required"):
            import production_core as _pc
            _pc.set_state(db, t, "pm_review", actor=current_user, event="teacher_submitted",
                          meta={"link": link, "note": "Video submitted" + ("" if t.on_time else " (delayed)")})
            _pc.notify_pms(db, "Video Submitted for Review",
                           f'{(uname.name if uname else "A teacher")} submitted "{t.title}" — ready for PM review.',
                           "production", link=str(t.id))
    except Exception:
        pass
    db.commit()
    return {"ok": True, "on_time": t.on_time}


@router.post("/teacher/video-tasks/{task_id}/chapter-link")
def vt_chapter_link(task_id: int, payload: dict = Body(...), db: Session = Depends(get_db),
                    current_user=Depends(get_teacher)):
    """One Shot / Rapid Revision special task — chapter/subject row pe video link.
    Approval NAHI chahiye; progress auto. 100% pe task complete + admin ko NEW blink."""
    tp = _get_tp(current_user, db)
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t or tp.id not in _collab_all_ids(t) or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision", "project"):
        raise HTTPException(404, "Special task not found")
    cid = int(payload.get("chapter_id") or 0)
    row = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.id == cid,
                   VideoTaskChapter.task_id == t.id).first())
    if not row:
        raise HTTPException(404, "Chapter not found in this task")
    link = (payload.get("link") or "").strip()
    now = _now_ist()
    had_link = bool((row.link or "").strip())
    # v91: blank link = REMOVE karna allowed hai (galat/test link hatane ke liye);
    # existing link pe naya link = UPDATE bhi allowed hai.
    if not link and not had_link:
        raise HTTPException(400, "Please paste the video link")
    removing = bool(not link and had_link)
    first_time = not had_link
    if removing:
        row.link = ""
        row.submitted_at = None
        row.edit_status = ""
    else:
        row.link = link
        row.submitted_at = now
        if first_time and _ch_status(row) == "editing_soon":
            row.edit_status = "editing_soon"   # nayi recording — editing karwani hai
    row.changed_at = now                       # admin ko changed-link blink
    t.last_link_at = now
    chs = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.task_id == t.id).all())
    done = sum(1 for c in chs if (c.link or "").strip())
    total = len(chs)
    just_completed = bool(total and done == total and t.status == "assigned" and not removing)
    # v91: link remove karne pe agar task submitted ho chuka tha to wapas open ho jaye
    if removing and t.status != "assigned":
        t.status = "assigned"
        t.submitted_at = None
        t.on_time = None
        _hist_add(t, "progress", '"%s" link removed — task reopened (%d/%d)' % (row.title, done, total))
    elif removing:
        _hist_add(t, "progress", '"%s" link removed (%d/%d)' % (row.title, done, total))
    elif first_time:
        _hist_add(t, "progress", '"%s" link added (%d/%d)' % (row.title, done, total))
    else:
        _hist_add(t, "progress", '"%s" link updated (%d/%d)' % (row.title, done, total))
    if just_completed:
        t.status = "submitted"
        t.submitted_at = now
        t.on_time = bool(t.deadline and now <= t.deadline)
        unit = {"one_shot": "chapters", "rapid_revision": "chapters",
                "project": "videos"}.get(t.kind, "items")
        _hist_add(t, "submitted", "All %d %s linked — %s" % (
            total, unit, "on time" if t.on_time else "delayed"))
    if just_completed:
        uname = db.query(User).filter(User.id == tp.user_id).first()
        if t.kind == "one_shot":
            label = "One Shot — %s" % t.subject
        elif t.kind == "project":
            label = t.title
        else:
            label = "Rapid Revision — %s" % t.subject
        unit = {"one_shot": "chapters", "rapid_revision": "chapters",
                "project": "videos"}.get(t.kind, "items")
        for a in db.query(User).filter(User.role == "admin", User.is_active == True).all():
            _vt_notify(db, a.id, "%s Complete" % label,
                       '%s completed all %d %s of "%s" (%s). View it in the Task Manager.'
                       % ((uname.name if uname else "A teacher"), total, unit, label,
                          "on time" if t.on_time else "delayed"))
    db.commit()
    return {"ok": True, "done": done, "total": total,
            "completed": bool(total and done == total)}


@router.post("/admin/video-tasks/chapter-status", dependencies=[Depends(_admin_section_guard)])
def vt_admin_chapter_status(payload: dict = Body(...), db: Session = Depends(get_db),
                            _=Depends(get_admin)):
    """Production team — kisi bhi special task (One Shot / Rapid Revision /
    Project) ke chapter/video ka EDIT STATUS set karo:
    editing_soon (editing karwani hai) -> editing_done (edited rakhi hai) ->
    uploaded (upload ho gayi). Sirf link wale chapters pe."""
    cid = int(payload.get("chapter_id") or 0)
    status = (payload.get("status") or "").strip()
    if status not in CHAPTER_EDIT_STATUSES:
        raise HTTPException(400, "Invalid status — use editing_soon / editing_done / uploaded")
    row = (db.query(VideoTaskChapter)
           .filter(VideoTaskChapter.id == cid).first())
    if not row:
        raise HTTPException(404, "Chapter not found")
    if not (row.link or "").strip():
        raise HTTPException(400, "Video link is not submitted yet — status can be set only after that")
    t = db.query(VideoTask).filter(VideoTask.id == row.task_id).first()
    if not t or (getattr(t, "kind", "") or "") not in ("one_shot", "rapid_revision", "project"):
        raise HTTPException(404, "Special task not found")
    old = _ch_status(row)
    if old == status:
        return {"ok": True, "chapter_id": cid, "status": status, "changed": False}
    row.edit_status = status
    lbl = {"editing_soon": "To Edit", "editing_done": "Edited",
           "uploaded": "Uploaded"}[status]
    old_lbl = {"editing_soon": "To Edit", "editing_done": "Edited",
               "uploaded": "Uploaded"}.get(old, "—")
    _hist_add(t, "progress", '"%s" production status: %s → %s' % (row.title, old_lbl, lbl))
    db.commit()
    return {"ok": True, "chapter_id": cid, "status": status, "changed": True}


# =============================================================
# REAL-TIME YOUTUBE VIEWS — link post + fetch + stats + graphs
# =============================================================
import urllib.request as _urlreq
import urllib.parse as _urlparse

_YT_ID_RX = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/))([A-Za-z0-9_-]{11})")


def _yt_extract_id(url):
    """YouTube URL (watch / youtu.be / shorts / embed / live) se 11-char video id."""
    u = (url or "").strip()
    if not u:
        return ""
    m = _YT_ID_RX.search(u)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", u):
        return u
    try:
        q = _urlparse.urlparse(u)
        vid = _urlparse.parse_qs(q.query).get("v", [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            return vid
    except Exception:
        pass
    return ""


def _yt_get_key(db):
    from models import AppSetting
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "youtube_api_key").first()
        return (row.value or "").strip() if row else ""
    except Exception:
        return ""


def _yt_fetch_views(video_ids, key):
    """YouTube Data API v3 videos.list(part=statistics) -> {id: views}. Batched 50.
    Poora guarded: key na ho / network fail -> {} (kabhi crash nahi)."""
    out = {}
    ids = [i for i in (video_ids or []) if i]
    if not ids or not key:
        return out
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        url = ("https://www.googleapis.com/youtube/v3/videos?part=statistics&id="
               + ",".join(chunk) + "&key=" + _urlparse.quote(key))
        try:
            req = _urlreq.Request(url, headers={"Accept": "application/json"})
            with _urlreq.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode("utf-8"))
            for item in (data.get("items") or []):
                vid = item.get("id")
                vc = ((item.get("statistics") or {}).get("viewCount"))
                if vid is not None and vc is not None:
                    try:
                        out[vid] = int(vc)
                    except Exception:
                        pass
        except Exception:
            continue
    return out


def _vt_parse_dt(s):
    if not s:
        return None
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f)
        except Exception:
            pass
    return None


def _views_at(db, task_id, before_dt):
    """Given time se pehle (ya us waqt) ka latest snapshot views. None agar koi nahi."""
    from models import VideoViewSnapshot
    if not before_dt:
        return None
    r = (db.query(VideoViewSnapshot)
         .filter(VideoViewSnapshot.task_id == task_id,
                 VideoViewSnapshot.captured_at <= before_dt)
         .order_by(VideoViewSnapshot.captured_at.desc()).first())
    return int(r.views) if r else None


def _vt_period_bounds(range_key, frm, to):
    """(start, end) datetimes. range_key: today | 7d | month | custom | all."""
    now = datetime.utcnow()
    rk = (range_key or "").strip().lower()
    if rk == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), None
    if rk in ("7d", "week", "last7"):
        return now - timedelta(days=7), None
    if rk in ("month", "30d", "last30"):
        return now - timedelta(days=30), None
    if rk == "custom":
        return _vt_parse_dt(frm), _vt_parse_dt(to)
    return None, None   # all-time


def _vt_task_period_views(db, t, period_start, period_end):
    """Is video ke period me views: (end ya current) - (period_start pe baseline).
    Baseline na mile (tracking period ke andar shuru hui) to 0 -> saare views period ke."""
    cur = int(t.yt_views or 0)
    if period_end:
        e = _views_at(db, t.id, period_end)
        if e is not None:
            cur = e
    if period_start:
        b = _views_at(db, t.id, period_start)
        base = b if b is not None else 0
        return max(0, cur - base)
    return cur


def _vt_category(t):
    """Video ka type category (short/live/one_shot/recorded/long) + label — breakdown ke liye."""
    kind = (t.kind or "normal")
    if kind in ("one_shot", "rapid_revision", "project"):
        return ("one_shot", "One Shot / Revision")
    vt = (t.video_type or "").lower()
    if "short" in vt:
        return ("short", "Shorts")
    if "live" in vt or (getattr(t, "streaming", "") or "").lower() == "live":
        return ("live", "YouTube Live")
    if "record" in vt:
        return ("recorded", "Recorded")
    return ("long", "Long Video")


def _vt_views_stats(db, teacher_id=None, period_start=None, period_end=None):
    """Totals + per-video (+thumb) + per-teacher + highest + all-teacher leaderboard + by-type.
    period_start/end diye ho to views = us period me GAINED (snapshot delta).
    Collab video -> team-specific bucket ("Collab: A + B"): SAME team merge, ALAG team alag.
    teacher_id diya ho to collab-member wali videos bhi us teacher ke scope me aati hain."""
    vids_all = [t for t in db.query(VideoTask).all() if (t.youtube_url or "")]
    tp_ids = set()
    for t in vids_all:
        if t.teacher_id:
            tp_ids.add(t.teacher_id)
        for cid in _collab_extra_ids(t):
            tp_ids.add(cid)
    tp_ids = list(tp_ids)
    tmap = {}
    if tp_ids:
        for tp in db.query(TeacherProfile).filter(TeacherProfile.id.in_(tp_ids)).all():
            u = db.query(User).filter(User.id == tp.user_id).first()
            tmap[tp.id] = (u.name if u else ("Teacher #%d" % tp.id))

    def _cnames(t):
        return sorted({tmap[cid] for cid in _collab_all_ids(t) if cid in tmap}) if _collab_extra_ids(t) else []

    def _vname(t):
        # collab video -> team-specific label. Same collab team wali videos ek bar me merge,
        # alag team wali alag bar. Non-collab -> primary teacher ka naam.
        if _collab_extra_ids(t):
            nms = _cnames(t)
            return ("Collab: " + " + ".join(nms)) if nms else "Collab"
        return tmap.get(t.teacher_id, "\u2014")

    def _vv(t):
        return _vt_task_period_views(db, t, period_start, period_end)

    # all-teacher leaderboard (comparison bar/pie) — collab team-wise buckets
    lb, lb_collab = {}, {}
    for t in vids_all:
        nm = _vname(t)
        lb[nm] = lb.get(nm, 0) + _vv(t)
        if nm.startswith("Collab"):
            lb_collab[nm] = _cnames(t)
    leaderboard = sorted(({"name": k, "views": v, "is_collab": k.startswith("Collab"),
                           "collab_names": lb_collab.get(k, [])}
                          for k, v in lb.items()),
                         key=lambda x: -x["views"])

    # scoped: teacher ki apni + collab-member wali videos (ya admin = sab)
    if teacher_id:
        from sqlalchemy import or_ as _orSc
        scoped_tasks = db.query(VideoTask).filter(
            _orSc(VideoTask.teacher_id == teacher_id,
                  VideoTask.collab_teacher_ids.like("%" + str(teacher_id) + "%"))).all()
        scoped_tasks = [t for t in scoped_tasks
                        if (t.teacher_id == teacher_id or teacher_id in _collab_all_ids(t))]
    else:
        scoped_tasks = db.query(VideoTask).all()
    vids = [t for t in scoped_tasks if (t.youtube_url or "")]
    pending = len([t for t in scoped_tasks
                   if t.status in ("assigned", "submitted") and not (t.youtube_url or "")])
    per_video, per_teacher, total_views, highest = [], {}, 0, None
    pt_collab, by_type = {}, {}
    for t in vids:
        v = _vv(t)
        total_views += v
        nm = _vname(t)
        per_teacher[nm] = per_teacher.get(nm, 0) + v
        if nm.startswith("Collab"):
            pt_collab[nm] = _cnames(t)
        catk, catl = _vt_category(t)
        bt = by_type.setdefault(catk, {"key": catk, "label": catl, "views": 0, "count": 0})
        bt["views"] += v
        bt["count"] += 1
        item = {"id": t.id, "title": t.title or "", "teacher": nm, "views": v,
                "is_collab": bool(_collab_extra_ids(t)), "collab_names": _cnames(t),
                "url": t.youtube_url or "",
                "thumb": (t.thumbnail_b64 or t.thumbnail_link or ""),
                "vtype": catk, "vtype_label": catl,
                "subject": t.subject or "", "kind": (t.kind or "normal"),
                "status": t.status or "", "primary": tmap.get(t.teacher_id, "\u2014"),
                "at": t.yt_views_at.strftime("%d %b, %I:%M %p") if t.yt_views_at else ""}
        per_video.append(item)
        if highest is None or v > highest["views"]:
            highest = item
    per_video.sort(key=lambda x: -x["views"])
    by_teacher = sorted(({"name": k, "views": v, "is_collab": k.startswith("Collab"),
                          "collab_names": pt_collab.get(k, [])}
                         for k, v in per_teacher.items()),
                        key=lambda x: -x["views"])
    by_type_list = sorted(by_type.values(), key=lambda x: -x["views"])
    # backward-compat: sabhi collab teams ke naamon ka union
    _all_collab = sorted({n for names in list(lb_collab.values()) + list(pt_collab.values()) for n in names})
    return {"uploaded": len(vids), "pending": pending, "total_views": total_views,
            "highest": highest, "per_video": per_video,
            "by_teacher": by_teacher, "leaderboard": leaderboard,
            "by_type": by_type_list, "collab_names": _all_collab}


def _vt_video_series(db, task_id, dt_from=None, dt_to=None):
    from models import VideoViewSnapshot
    q = db.query(VideoViewSnapshot).filter(VideoViewSnapshot.task_id == task_id)
    if dt_from:
        q = q.filter(VideoViewSnapshot.captured_at >= dt_from)
    if dt_to:
        q = q.filter(VideoViewSnapshot.captured_at <= dt_to)
    rows = q.order_by(VideoViewSnapshot.captured_at.asc()).all()
    return [{"at": r.captured_at.strftime("%Y-%m-%d %H:%M"), "views": int(r.views or 0)}
            for r in rows]


@router.get("/admin/settings/youtube-key", dependencies=[Depends(_admin_section_guard)])
def vt_get_yt_key(db: Session = Depends(get_db), _=Depends(get_admin)):
    k = _yt_get_key(db)
    masked = ("\u2022" * max(0, len(k) - 4) + k[-4:]) if k else ""
    return {"set": bool(k), "masked": masked}


@router.post("/admin/settings/youtube-key", dependencies=[Depends(_admin_section_guard)])
def vt_set_yt_key(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AppSetting
    key = (payload.get("key") or "").strip()
    row = db.query(AppSetting).filter(AppSetting.key == "youtube_api_key").first()
    if not row:
        db.add(AppSetting(key="youtube_api_key", value=key))
    else:
        row.value = key
    db.commit()
    return {"ok": True, "set": bool(key)}


@router.post("/admin/video-tasks/{task_id}/youtube-link", dependencies=[Depends(_admin_section_guard)])
def vt_post_youtube_link(task_id: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import VideoViewSnapshot
    t = db.query(VideoTask).filter(VideoTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    url = (payload.get("youtube_url") or "").strip()
    if not url:
        t.youtube_url = ""; t.yt_video_id = ""; t.yt_views = None; t.yt_views_at = None
        db.commit()
        return {"ok": True, "cleared": True}
    vid = _yt_extract_id(url)
    if not vid:
        raise HTTPException(400, "Could not read a YouTube video id from that link")
    t.youtube_url = url
    t.yt_video_id = vid
    if t.status != "uploaded":
        t.status = "uploaded"
        if (getattr(t, "kind", "") or "") == "urgent" and t.deadline:
            t.on_time = (_now_ist() <= t.deadline)
        _hist_add(t, "uploaded", "YouTube link posted")
    got = _yt_fetch_views([vid], _yt_get_key(db))
    if vid in got:
        t.yt_views = got[vid]; t.yt_views_at = datetime.utcnow()
        db.add(VideoViewSnapshot(task_id=t.id, views=got[vid]))
    db.commit()
    return {"ok": True, "video_id": vid, "views": t.yt_views}


@router.post("/admin/video-tasks/refresh-views", dependencies=[Depends(_admin_section_guard)])
def vt_refresh_views(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import VideoViewSnapshot
    key = _yt_get_key(db)
    if not key:
        raise HTTPException(400, "Add a YouTube API key first (in Task Manager settings).")
    tasks = db.query(VideoTask).filter(VideoTask.yt_video_id != "",
                                       VideoTask.yt_video_id != None).all()
    idmap = {}
    for t in tasks:
        idmap.setdefault(t.yt_video_id, []).append(t)
    got = _yt_fetch_views(list(idmap.keys()), key)
    now = datetime.utcnow(); n = 0
    for vid, views in got.items():
        for t in idmap.get(vid, []):
            t.yt_views = views; t.yt_views_at = now
            db.add(VideoViewSnapshot(task_id=t.id, views=views)); n += 1
    db.commit()
    return {"ok": True, "updated": n, "fetched": len(got), "total": len(idmap)}


@router.post("/teacher/video-tasks/refresh-views")
def vt_teacher_refresh_views(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher apni videos ke live views khud refresh kar sake (admin ki stored key se).
    Har refresh ek snapshot add karta hai -> timeline graph banta hai."""
    from models import VideoViewSnapshot
    tp = _get_tp(current_user, db)
    key = _yt_get_key(db)
    if not key:
        raise HTTPException(400, "YouTube API key abhi admin ne set nahi ki hai.")
    tasks = db.query(VideoTask).filter(VideoTask.teacher_id == tp.id,
                                       VideoTask.yt_video_id != "",
                                       VideoTask.yt_video_id != None).all()
    idmap = {}
    for t in tasks:
        idmap.setdefault(t.yt_video_id, []).append(t)
    got = _yt_fetch_views(list(idmap.keys()), key)
    now = datetime.utcnow(); n = 0
    for vid, views in got.items():
        for t in idmap.get(vid, []):
            t.yt_views = views; t.yt_views_at = now
            db.add(VideoViewSnapshot(task_id=t.id, views=views)); n += 1
    db.commit()
    return {"ok": True, "updated": n, "total": len(idmap)}


@router.get("/admin/video-views", dependencies=[Depends(_admin_section_guard)])
def vt_admin_views(video_id: int = 0, range: str = "", frm: str = "", to: str = "",
                   db: Session = Depends(get_db), _=Depends(get_admin)):
    ps, pe = _vt_period_bounds(range, frm, to)
    st = _vt_views_stats(db, period_start=ps, period_end=pe)
    st["youtube_key_set"] = bool(_yt_get_key(db))
    st["range"] = range or "all"
    if video_id:
        st["series"] = _vt_video_series(db, video_id, _vt_parse_dt(frm), _vt_parse_dt(to))
    return st


@router.get("/teacher/video-views")
def vt_teacher_views(video_id: int = 0, range: str = "", frm: str = "", to: str = "",
                     db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = _get_tp(current_user, db)
    ps, pe = _vt_period_bounds(range, frm, to)
    st = _vt_views_stats(db, teacher_id=tp.id, period_start=ps, period_end=pe)
    st["me"] = (db.query(User).filter(User.id == tp.user_id).first().name
                if db.query(User).filter(User.id == tp.user_id).first() else "")
    st["range"] = range or "all"
    if video_id:
        st["series"] = _vt_video_series(db, video_id, _vt_parse_dt(frm), _vt_parse_dt(to))
    return st


# =============================================================
# TEACHER MONTHLY TARGETS vs VIDEO ASSIGNMENTS (production manager view)
# =============================================================
def _vt_targets_for(db, tp, dt0, dt1):
    """Ek teacher ke targets (pay config) + is mahine ke video assignments
    (assigned/done/pending) + per-subject. KOI AMOUNT nahi — sirf targets/counts."""
    from teacher_routes import get_pay_config
    from models import User as _U
    _DEF = {"tests": "Weekly Tests", "videos": "Videos (One Shot/Revision)",
            "live": "YouTube Live", "shorts": "Shorts"}
    u = db.query(_U).filter(_U.id == tp.user_id).first()
    try:
        cfg = get_pay_config(db, tp.id)
    except Exception:
        cfg = None
    labels = (getattr(cfg, "target_labels", None) or {}) if cfg else {}

    def _lab(k):
        return (str(labels.get(k) or "").strip() or _DEF[k])

    tgt = {k: int(getattr(cfg, k + "_target", 0) or 0) for k in ("tests", "videos", "live", "shorts")} \
        if cfg else {"tests": 0, "videos": 0, "live": 0, "shorts": 0}

    from sqlalchemy import or_ as _orVT
    _cand = db.query(VideoTask).filter(
        _orVT(VideoTask.teacher_id == tp.id,
              VideoTask.collab_teacher_ids.like("%" + str(tp.id) + "%")),
        VideoTask.created_at >= dt0, VideoTask.created_at < dt1,
        VideoTask.proposal_ok != "pending",
        VideoTask.status != "rejected").all()
    # collab: primary YA collab-member dono ke liye task count (Vicky: sabhi collab done)
    tasks = [t for t in _cand if (t.teacher_id == tp.id or tp.id in _collab_all_ids(t))]

    cats = {"videos": {"assigned": 0, "done": 0, "verify": 0},
            "shorts": {"assigned": 0, "done": 0, "verify": 0},
            "live":   {"assigned": 0, "done": 0, "verify": 0}}
    bysub = {}
    for t in tasks:
        kind = (t.kind or "normal")
        sub = t.subject or "General"
        if kind in ("one_shot", "rapid_revision", "project"):
            continue   # projects "Project" component me count hote hain — videos me nahi
            chs = db.query(VideoTaskChapter).filter(VideoTaskChapter.task_id == t.id).all()
            if not chs:
                cats["videos"]["assigned"] += 1
                bysub.setdefault(sub, {"assigned": 0, "done": 0})
                bysub[sub]["assigned"] += 1
                continue
            for ch in chs:
                cats["videos"]["assigned"] += 1
                has_link = bool((ch.link or "").strip()) or bool(ch.submitted_at)
                vint = (getattr(ch, "vintage", "") or "")
                done = has_link and vint == "new"      # sirf NEW verified video count hoti hai
                if done:
                    cats["videos"]["done"] += 1
                elif has_link and vint == "":
                    cats["videos"]["verify"] += 1       # link hai par admin ne verify nahi kiya
                bysub.setdefault(sub, {"assigned": 0, "done": 0})
                bysub[sub]["assigned"] += 1
                if done:
                    bysub[sub]["done"] += 1
            continue
        vt = (t.video_type or "").lower()
        if "short" in vt:
            cat = "shorts"
        elif "live" in vt or (getattr(t, "streaming", "") or "") == "live":
            cat = "live"
        else:
            cat = "videos"
        cats[cat]["assigned"] += 1
        has_link = bool(t.submitted_at) or t.status == "uploaded"
        done = has_link            # normal videos: submit = done (New/Old check One Shot/Revision pe)
        if done:
            cats[cat]["done"] += 1
        if cat == "videos":
            bysub.setdefault(sub, {"assigned": 0, "done": 0})
            bysub[sub]["assigned"] += 1
            if done:
                bysub[sub]["done"] += 1

    rows = []
    for k in ("videos", "shorts", "live", "tests"):
        c = cats.get(k, {"assigned": 0, "done": 0, "verify": 0})
        rows.append({"key": k, "label": _lab(k), "target": tgt[k],
                     "assigned": c["assigned"], "done": c["done"],
                     "verify": c.get("verify", 0),
                     "pending": max(0, c["assigned"] - c["done"])})
    for c in (getattr(cfg, "custom_targets", None) or [] if cfg else []):
        if isinstance(c, dict) and str(c.get("name") or "").strip():
            rows.append({"key": "custom", "label": str(c.get("name"))[:60],
                         "target": int(c.get("count") or 0),
                         "assigned": 0, "done": 0, "pending": 0, "custom": True})

    subs = getattr(tp, "subjects", None) or []
    return {
        "teacher_id": tp.id, "name": (u.name if u else "Teacher #%d" % tp.id),
        "subjects": subs, "multi": len(subs) > 1, "rows": rows, "has_tasks": bool(tasks),
        "by_subject": [{"subject": s, "assigned": v["assigned"], "done": v["done"],
                        "pending": max(0, v["assigned"] - v["done"]),
                        "target": tgt["videos"]}
                       for s, v in sorted(bysub.items())],
    }


@router.get("/admin/teacher-targets", dependencies=[Depends(_admin_section_guard)])
def vt_teacher_targets(month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Sabhi teachers ke monthly targets + assignments (production manager view). Koi amount nahi."""
    from teacher_routes import _month_range
    start, end = _month_range(month)
    dt0 = datetime(start.year, start.month, start.day)
    dt1 = datetime(end.year, end.month, end.day)
    out = []
    for tp in db.query(TeacherProfile).all():
        row = _vt_targets_for(db, tp, dt0, dt1)
        if any(r["target"] > 0 for r in row["rows"]) or row["has_tasks"]:
            out.append(row)
    out.sort(key=lambda x: x["name"].lower())
    return {"month": "%04d-%02d" % (start.year, start.month), "teachers": out}


@router.get("/teacher/my-targets")
def vt_my_targets(month: str = "", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher apne monthly targets + progress dekhe (My Tasks me). Koi amount nahi."""
    from teacher_routes import _month_range
    tp = _get_tp(current_user, db)
    start, end = _month_range(month)
    dt0 = datetime(start.year, start.month, start.day)
    dt1 = datetime(end.year, end.month, end.day)
    row = _vt_targets_for(db, tp, dt0, dt1)
    return {"month": "%04d-%02d" % (start.year, start.month), "me": row}


# =============================================================
# NEW / OLD video verification (target integrity)
# =============================================================
@router.post("/admin/video-chapters/{cid}/vintage", dependencies=[Depends(_admin_section_guard)])
def vt_set_chapter_vintage(cid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Admin ek chapter ki video ko NEW ya OLD mark kare. Sirf NEW target me count hoti hai."""
    v = (payload or {}).get("vintage", "")
    if v not in ("new", "old", ""):
        raise HTTPException(status_code=400, detail="vintage new/old hona chahiye")
    ch = db.query(VideoTaskChapter).filter(VideoTaskChapter.id == cid).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Chapter not found")
    ch.vintage = v
    db.commit()
    return {"ok": True, "id": cid, "vintage": v}


@router.post("/admin/video-tasks/{tid}/vintage", dependencies=[Depends(_admin_section_guard)])
def vt_set_task_vintage(tid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    v = (payload or {}).get("vintage", "")
    if v not in ("new", "old", ""):
        raise HTTPException(status_code=400, detail="vintage new/old hona chahiye")
    t = db.query(VideoTask).filter(VideoTask.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    t.vintage = v
    db.commit()
    return {"ok": True, "id": tid, "vintage": v}
