"""
MVS Foundation CRM - NIOS Syllabus Tracker router.

Mount in main.py:
    import syllabus_routes
    app.include_router(syllabus_routes.router)

Depends on the same contract as every other router in this repo:
    from database import get_db
    from security import get_admin, get_student
    router functions take db: Session = Depends(get_db) + user = Depends(...)

Tables are created lazily by _ensure_syllabus(db), exactly like
_ensure_geofence in teacher_routes.py. Keep that call at the top of every
endpoint that touches syllabus tables. Removing it breaks a fresh deploy.
"""

import os
import json
import math
import hmac
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body, Request
from sqlalchemy.orm import Session
from sqlalchemy import text as _text

from database import get_db
from security import get_admin, get_student
from models import StudentProfile, AvailableSubject, AppSetting

import syllabus_data as SD

router = APIRouter(prefix="/api/syllabus", tags=["Syllabus Tracker"])

CHAPTER_API_KEY = os.environ.get("CHAPTER_API_KEY", "")

DEFAULTS = {
    "syl_high_target": "75",
    "syl_safety_buffer": "20",
    "syl_sessions": json.dumps(SD.EXAM_SESSIONS),
}

_SYL_READY = False


# ---------------------------------------------------------------------------
# Lazy migration
# ---------------------------------------------------------------------------

def _ensure_syllabus(db):
    """Creates syllabus tables and student columns on first use. Idempotent."""
    global _SYL_READY
    if _SYL_READY:
        return
    stmts = [
        """CREATE TABLE IF NOT EXISTS syllabus_overrides (
             id INTEGER PRIMARY KEY AUTO_INCREMENT,
             class_level VARCHAR(5), code VARCHAR(20),
             payload LONGTEXT, updated_at DATETIME NULL)""",
        """CREATE TABLE IF NOT EXISTS syllabus_hidden (
             id INTEGER PRIMARY KEY AUTO_INCREMENT,
             class_level VARCHAR(5), code VARCHAR(20))""",
        """CREATE TABLE IF NOT EXISTS chapter_plans (
             id INTEGER PRIMARY KEY AUTO_INCREMENT,
             student_id INTEGER, subject_code VARCHAR(20),
             selected LONGTEXT, done LONGTEXT,
             tma_assumed FLOAT NULL, practical_assumed FLOAT NULL,
             updated_at DATETIME NULL)""",
        "CREATE TABLE IF NOT EXISTS app_settings (`key` VARCHAR(50) PRIMARY KEY, value TEXT NULL)",
    ]
    for s in stmts:
        try:
            db.execute(_text(s)); db.commit()
        except Exception:
            db.rollback()
            # SQLite fallback (local testing) - AUTO_INCREMENT / LONGTEXT differ
            try:
                alt = (s.replace("INTEGER PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
                        .replace("LONGTEXT", "TEXT").replace("`key`", "key"))
                db.execute(_text(alt)); db.commit()
            except Exception:
                db.rollback()
    for col in ["exam_session VARCHAR(30) NULL", "study_target VARCHAR(10) NULL"]:
        try:
            db.execute(_text("ALTER TABLE student_profiles ADD COLUMN %s" % col)); db.commit()
        except Exception:
            db.rollback()
    for idx in ["CREATE INDEX ix_chapter_plans_student ON chapter_plans (student_id)"]:
        try:
            db.execute(_text(idx)); db.commit()
        except Exception:
            db.rollback()
    _SYL_READY = True


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _setting(db, key, default=""):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row and row.value is not None:
        return row.value
    return DEFAULTS.get(key, default)


def _set_setting(db, key, value):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = str(value)
    else:
        db.add(AppSetting(key=key, value=str(value)))


def _cfg(db):
    return {
        "high_target": float(_setting(db, "syl_high_target", "75") or 75),
        "buffer_pct": float(_setting(db, "syl_safety_buffer", "20") or 20),
    }


# ---------------------------------------------------------------------------
# Syllabus access (seed data + admin overrides + validation gate)
# ---------------------------------------------------------------------------

def _overrides(db, class_level):
    _ensure_syllabus(db)
    rows = db.execute(_text(
        "SELECT code, payload FROM syllabus_overrides WHERE class_level=:c"),
        {"c": str(class_level)}).fetchall()
    out = {}
    for r in rows:
        try:
            out[r[0]] = json.loads(r[1])
        except Exception:
            pass
    return out


def _hidden(db, class_level):
    _ensure_syllabus(db)
    rows = db.execute(_text(
        "SELECT code FROM syllabus_hidden WHERE class_level=:c"),
        {"c": str(class_level)}).fetchall()
    return {r[0] for r in rows}


def subject_list(db, class_level, include_hidden=False):
    cl = str(class_level)
    base = {s["code"]: json.loads(json.dumps(s)) for s in SD.SUBJECTS.get(cl, [])}
    base.update(_overrides(db, cl))
    hid = _hidden(db, cl)
    out = []
    for code in sorted(base.keys()):
        s = dict(base[code])
        if not include_hidden and code in hid:
            continue
        s["hidden"] = code in hid
        status, issues = SD.validate_subject(s)
        s["status"] = status
        s["issues"] = issues
        out.append(s)
    return out


def get_subject(db, class_level, code):
    for s in subject_list(db, class_level, include_hidden=True):
        if s["code"] == str(code):
            return s
    return None


def subject_code_for_name(db, class_level, name):
    """Timetable stores subject NAMES. Map to a syllabus code."""
    n = (name or "").strip().lower()
    if not n:
        return None
    av = db.query(AvailableSubject).filter(
        AvailableSubject.class_level == str(class_level)).all()
    for a in av:
        if (a.name or "").strip().lower() == n and a.code:
            return str(a.code).strip()
    for s in SD.SUBJECTS.get(str(class_level), []):
        if s["name"].strip().lower() == n:
            return s["code"]
    return None


def class_level_from_name(class_name):
    """'Class 12' / '12A' / '12' -> '12'."""
    s = str(class_name or "")
    return "10" if "10" in s else ("12" if "12" in s else "")


# ---------------------------------------------------------------------------
# Calculation engine (identical maths to the standalone tracker)
# ---------------------------------------------------------------------------

def compute(subject, selected, tma_assumed=None, practical_assumed=None,
            high_target=75.0, buffer_pct=20.0):
    m = subject["marks"]
    rows = SD.flatten(subject)
    pe_rows = [r for r in rows if r["kind"] == "PE"]
    paper = float(m.get("paper_marks") or (m["theory_max"] / 0.8 if m["theory_max"] else 0))
    scale = (m["theory_max"] / paper) if paper else 0

    total_paper = round(sum(r["marks"] for r in pe_rows), 2)
    sel = set(selected or [])
    covered_paper = round(sum(r["marks"] for r in pe_rows if r["no"] in sel), 2)
    covered_theory = round(covered_paper * scale, 2)

    tma = float(m["tma_max"]) if tma_assumed is None or tma_assumed < 0 else float(tma_assumed)
    pr_default = round(m["practical_max"] * 0.8, 2)
    pr = pr_default if practical_assumed is None or practical_assumed < 0 else float(practical_assumed)
    pr = min(pr, m["practical_max"])
    tma = min(tma, m["tma_max"])
    buf = 1 + (buffer_pct / 100.0)

    if m.get("combined_pass"):
        need_theory = max(m["combined_pass"] - pr, 0)
        pass_rule = "Theory and Practical together must reach %s" % m["combined_pass"]
    else:
        need_theory = m["theory_pass"]
        pass_rule = "Theory must reach %s out of %s" % (m["theory_pass"], m["theory_max"])

    need_theory_final = max(need_theory, max(m["aggregate_pass"] - tma - pr, 0))
    pass_paper = min(round(((need_theory_final / scale) if scale else 0) * buf, 1), total_paper)
    high_paper = min(round(((max(high_target - tma - pr, 0) / scale) if scale else 0) * buf, 1), total_paper)

    remaining = sorted([r for r in pe_rows if r["no"] not in sel],
                       key=lambda r: (-r["marks"], r["no"]))

    def pick_until(target_paper):
        need = target_paper - covered_paper
        chosen, acc = [], 0.0
        for r in remaining:
            if need - acc <= 0.01:
                break
            chosen.append(r); acc += r["marks"]
        return chosen

    has_marks = total_paper > 0
    return {
        "paper_marks": paper, "scale": round(scale, 4),
        "total_pe_marks": total_paper, "covered_paper": covered_paper,
        "covered_theory": covered_theory,
        "tma_assumed": tma, "practical_assumed": pr,
        "projected_total": round(covered_theory + tma + pr, 1),
        "pass_rule": pass_rule,
        "pass_paper_needed": pass_paper,
        "pass_reached": has_marks and covered_paper + 0.01 >= pass_paper,
        "high_target": high_target,
        "high_paper_needed": high_paper,
        "high_reached": has_marks and covered_paper + 0.01 >= high_paper,
        "pass_gap_chapters": pick_until(pass_paper),
        "high_gap_chapters": pick_until(high_paper),
        "selected_count": len([r for r in pe_rows if r["no"] in sel]),
        "pe_count": len(pe_rows), "buffer_pct": buffer_pct,
    }


def _plan_row(db, student_id, code):
    r = db.execute(_text(
        "SELECT selected, done, tma_assumed, practical_assumed FROM chapter_plans "
        "WHERE student_id=:s AND subject_code=:c"), {"s": student_id, "c": str(code)}).fetchone()
    if not r:
        return [], [], -1.0, -1.0
    try:
        sel = json.loads(r[0] or "[]")
    except Exception:
        sel = []
    try:
        done = json.loads(r[1] or "[]")
    except Exception:
        done = []
    return sel, done, (r[2] if r[2] is not None else -1.0), (r[3] if r[3] is not None else -1.0)


def _student_profile(db, user):
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student profile not found.")
    return sp


def _student_codes(db, sp):
    """
    Subject codes for this student, mapped from their subject names.
    Returns (class_level, codes, unmapped_names).

    unmapped_names are subjects the student is enrolled in that have no entry
    in the syllabus master. They are reported, never silently dropped, so a
    missing subject shows up instead of quietly disappearing.
    """
    cl = str(sp.class_level or class_level_from_name(sp.class_name) or "12")
    names = sp.subjects if isinstance(sp.subjects, list) else []
    codes, unmapped = [], []
    known = {s["code"] for s in subject_list(db, cl, include_hidden=True)}
    for n in names:
        c = subject_code_for_name(db, cl, n)
        if c and c in known:
            if c not in codes:
                codes.append(c)
        else:
            unmapped.append(str(n))
    return cl, codes, unmapped


def _days_left(db, session_id):
    try:
        sessions = json.loads(_setting(db, "syl_sessions", "[]"))
    except Exception:
        sessions = SD.EXAM_SESSIONS
    for s in sessions:
        if s.get("id") == session_id:
            if s.get("date"):
                try:
                    return (date.fromisoformat(s["date"]) - date.today()).days, s
                except Exception:
                    return None, s
            return None, s
    return None, None


# ---------------------------------------------------------------------------
# STUDENT ENDPOINTS
# ---------------------------------------------------------------------------

@router.get("/me")
def syl_me(db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    cfg = _cfg(db)
    dl, sess = _days_left(db, getattr(sp, "exam_session", "") or "")
    try:
        sessions = json.loads(_setting(db, "syl_sessions", "[]"))
    except Exception:
        sessions = SD.EXAM_SESSIONS
    return {
        "name": user.name, "class_level": cl, "subject_codes": codes,
        "unmapped_subjects": unmapped,
        "exam_session": getattr(sp, "exam_session", "") or "",
        "target": getattr(sp, "study_target", "") or "pass",
        "days_left": dl, "session": sess, "sessions": sessions,
        "high_target": cfg["high_target"], "buffer_pct": cfg["buffer_pct"],
    }


@router.post("/profile")
def syl_profile(payload: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    db.execute(_text("UPDATE student_profiles SET exam_session=:e, study_target=:t WHERE id=:i"),
               {"e": (payload.get("exam_session") or "")[:30],
                "t": (payload.get("target") or "pass")[:10], "i": sp.id})
    db.commit()
    return {"ok": True}


@router.get("/overview")
def syl_overview(db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    cfg = _cfg(db)
    out = []
    for code in codes:
        subj = get_subject(db, cl, code)
        if not subj:
            continue
        sel, done, tma, pr = _plan_row(db, sp.id, code)
        ready = subj.get("status") == "ready"
        calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"]) if ready else None
        out.append({"code": subj["code"], "name": subj["name"],
                    "status": subj.get("status", "pending"),
                    "selected": len(sel) if ready else 0,
                    "done": len(done) if ready else 0, "calc": calc})
    for n in unmapped:
        out.append({"code": "", "name": n, "status": "pending",
                    "selected": 0, "done": 0, "calc": None})
    dl, sess = _days_left(db, getattr(sp, "exam_session", "") or "")
    return {"subjects": out, "days_left": dl, "session": sess,
            "unmapped_subjects": unmapped,
            "target": getattr(sp, "study_target", "") or "pass"}


@router.get("/subject/{code}")
def syl_subject(code: str, db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    if str(code) not in codes:
        raise HTTPException(status_code=403, detail="This subject is not in your enrolment.")
    subj = get_subject(db, cl, code)
    if not subj:
        raise HTTPException(status_code=404, detail="Subject not found.")
    if subj.get("status") != "ready":
        return {"subject": {"code": subj["code"], "name": subj["name"], "status": subj["status"],
                            "marks": subj["marks"]},
                "modules": [], "chapters": [], "selected": [], "done": [], "calc": None,
                "message": "Syllabus for this subject is under verification. It will be available shortly."}
    sel, done, tma, pr = _plan_row(db, sp.id, code)
    cfg = _cfg(db)
    return {
        "subject": {"code": subj["code"], "name": subj["name"], "status": subj["status"],
                    "marks": subj["marks"]},
        "modules": subj.get("modules", []),
        "chapters": SD.flatten(subj),
        "selected": sel, "done": done,
        "calc": compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"]),
    }


@router.post("/plan")
def syl_plan(payload: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    code = str(payload.get("subject_code") or "")
    if code not in codes:
        raise HTTPException(status_code=403, detail="This subject is not in your enrolment.")
    subj = get_subject(db, cl, code)
    if not subj or subj.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Syllabus for this subject is not verified yet.")

    valid = {r["no"] for r in SD.flatten(subj) if r["kind"] == "PE"}
    sel = [x for x in (payload.get("selected") or []) if x in valid]
    done = [x for x in (payload.get("done") or []) if x in sel]
    tma = float(payload.get("tma_assumed", -1) or -1)
    pr = float(payload.get("practical_assumed", -1) or -1)

    exists = db.execute(_text(
        "SELECT id FROM chapter_plans WHERE student_id=:s AND subject_code=:c"),
        {"s": sp.id, "c": code}).fetchone()
    args = {"s": sp.id, "c": code, "sel": json.dumps(sel), "dn": json.dumps(done),
            "t": tma, "p": pr, "u": datetime.utcnow()}
    if exists:
        db.execute(_text("UPDATE chapter_plans SET selected=:sel, done=:dn, tma_assumed=:t, "
                         "practical_assumed=:p, updated_at=:u WHERE student_id=:s AND subject_code=:c"), args)
    else:
        db.execute(_text("INSERT INTO chapter_plans (student_id, subject_code, selected, done, "
                         "tma_assumed, practical_assumed, updated_at) "
                         "VALUES (:s, :c, :sel, :dn, :t, :p, :u)"), args)
    db.commit()
    cfg = _cfg(db)
    return {"ok": True, "calc": compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"])}


@router.get("/strategy")
def syl_strategy(db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    cfg = _cfg(db)
    target = getattr(sp, "study_target", "") or "pass"
    dl, sess = _days_left(db, getattr(sp, "exam_session", "") or "")
    dl = max(dl if dl is not None else 120, 7)
    weeks = max(math.ceil(dl / 7), 1)
    months = max(math.ceil(dl / 30), 1)

    queue = []
    for code in codes:
        subj = get_subject(db, cl, code)
        if not subj or subj.get("status") != "ready":
            continue
        sel, done, tma, pr = _plan_row(db, sp.id, code)
        calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"])
        pool = calc["high_gap_chapters"] if target == "high" else calc["pass_gap_chapters"]
        merged = {r["no"]: r for r in SD.flatten(subj)
                  if r["kind"] == "PE" and r["no"] in set(sel) and r["no"] not in set(done)}
        for r in pool:
            merged.setdefault(r["no"], r)
        for r in merged.values():
            queue.append({"subject": subj["name"], "code": subj["code"], **r})

    queue.sort(key=lambda r: (-r["marks"], r["subject"], r["no"]))

    def bucket(n, label):
        b = [[] for _ in range(n)]
        for i, item in enumerate(queue):
            b[i % n].append(item)
        return [{"label": "%s %d" % (label, i + 1), "items": x,
                 "marks": round(sum(y["marks"] for y in x), 1)}
                for i, x in enumerate(b) if x]

    return {"days_left": dl, "weeks": weeks, "months": months, "target": target,
            "total_pending": len(queue),
            "weekly": bucket(weeks, "Week"), "monthly": bucket(months, "Month")}


# ---------------------------------------------------------------------------
# ADMIN ENDPOINTS
# ---------------------------------------------------------------------------

@router.get("/admin/subjects")
def syl_admin_subjects(class_level: str = "12", db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    out = []
    for s in subject_list(db, class_level, include_hidden=True):
        rows = SD.flatten(s)
        out.append({
            "code": s["code"], "name": s["name"], "status": s.get("status", "pending"),
            "hidden": s.get("hidden", False),
            "pe": len([r for r in rows if r["kind"] == "PE"]),
            "tma": len([r for r in rows if r["kind"] == "TMA"]),
            "issues": s.get("issues", []), "expected": s.get("expected", {}),
            "marks": s["marks"], "template": s.get("template", ""),
        })
    return {"items": out}


@router.get("/admin/subject")
def syl_admin_subject(class_level: str, code: str,
                      db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    s = get_subject(db, class_level, code)
    if not s:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return {"subject": s, "template_values": SD.TPL}


@router.post("/admin/subject")
def syl_admin_save(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    cl = str(payload.get("class_level") or "")
    code = str(payload.get("code") or "")
    p = payload.get("payload") or {}
    if not cl or not code or not p.get("name"):
        raise HTTPException(status_code=400, detail="Class, subject code and name are required.")
    p.setdefault("code", code)
    p.setdefault("modules", [])
    p.setdefault("expected", {})
    if "marks" not in p:
        raise HTTPException(status_code=400, detail="Marks structure is required.")
    status, issues = SD.validate_subject(p)
    p["status"] = status
    exists = db.execute(_text("SELECT id FROM syllabus_overrides WHERE class_level=:c AND code=:k"),
                        {"c": cl, "k": code}).fetchone()
    args = {"c": cl, "k": code, "p": json.dumps(p), "u": datetime.utcnow()}
    if exists:
        db.execute(_text("UPDATE syllabus_overrides SET payload=:p, updated_at=:u "
                         "WHERE class_level=:c AND code=:k"), args)
    else:
        db.execute(_text("INSERT INTO syllabus_overrides (class_level, code, payload, updated_at) "
                         "VALUES (:c, :k, :p, :u)"), args)
    db.commit()
    return {"ok": True, "status": status, "issues": issues}


@router.post("/admin/subject-visibility")
def syl_admin_visibility(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    cl, code = str(payload.get("class_level")), str(payload.get("code"))
    if payload.get("hidden"):
        ex = db.execute(_text("SELECT id FROM syllabus_hidden WHERE class_level=:c AND code=:k"),
                        {"c": cl, "k": code}).fetchone()
        if not ex:
            db.execute(_text("INSERT INTO syllabus_hidden (class_level, code) VALUES (:c, :k)"),
                       {"c": cl, "k": code})
    else:
        db.execute(_text("DELETE FROM syllabus_hidden WHERE class_level=:c AND code=:k"),
                   {"c": cl, "k": code})
    db.commit()
    return {"ok": True}


@router.post("/admin/reset-subject")
def syl_admin_reset(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    db.execute(_text("DELETE FROM syllabus_overrides WHERE class_level=:c AND code=:k"),
               {"c": str(payload.get("class_level")), "k": str(payload.get("code"))})
    db.commit()
    return {"ok": True}


@router.post("/admin/parse-pdf")
async def syl_admin_parse_pdf(file: UploadFile = File(...), _=Depends(get_admin)):
    """Read a NIOS syllabus PDF and return the chapter block for the editor."""
    import syllabus_pdf
    name = (file.filename or "").lower()
    if not name.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File is too large. Maximum size is 20 MB.")
    try:
        res = syllabus_pdf.parse_syllabus_pdf(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not read this PDF. " + str(exc)[:180])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not read this PDF."))
    res["filename"] = file.filename
    return res


@router.get("/admin/settings")
def syl_admin_settings(db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    return {"values": {k: _setting(db, k) for k in DEFAULTS}}


@router.post("/admin/settings")
def syl_admin_set_settings(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    for k, v in (payload.get("values") or {}).items():
        if k in DEFAULTS:
            _set_setting(db, k, v)
    db.commit()
    return {"ok": True}


@router.get("/admin/progress")
def syl_admin_progress(class_level: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Who is using the tracker and where they stand."""
    _ensure_syllabus(db)
    cfg = _cfg(db)
    rows = db.execute(_text(
        "SELECT student_id, subject_code, selected, done, tma_assumed, practical_assumed, updated_at "
        "FROM chapter_plans ORDER BY updated_at DESC")).fetchall()
    by_student = {}
    for r in rows:
        by_student.setdefault(r[0], []).append(r)
    out = []
    for sid, plans in list(by_student.items())[:300]:
        sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
        if not sp:
            continue
        cl = str(sp.class_level or "12")
        if class_level and cl != str(class_level):
            continue
        subs = []
        for p in plans:
            subj = get_subject(db, cl, p[1])
            if not subj or subj.get("status") != "ready":
                continue
            try:
                sel = json.loads(p[2] or "[]")
            except Exception:
                sel = []
            c = compute(subj, sel, p[4] if p[4] is not None else -1,
                        p[5] if p[5] is not None else -1, cfg["high_target"], cfg["buffer_pct"])
            subs.append({"code": subj["code"], "name": subj["name"],
                         "covered": c["covered_paper"], "total": c["total_pe_marks"],
                         "pass": c["pass_reached"], "high": c["high_reached"]})
        out.append({"student_id": sid, "name": sp.user.name if sp.user else "",
                    "phone": sp.phone, "class_level": cl,
                    "exam_session": getattr(sp, "exam_session", "") or "",
                    "target": getattr(sp, "study_target", "") or "pass",
                    "subjects": subs})
    return {"items": out}


# ---------------------------------------------------------------------------
# CHAPTER MASTER - used by the timetable and by any external service
# ---------------------------------------------------------------------------

def _chapter_key_ok(request: Request):
    key = request.headers.get("x-mvs-chapter-key", "")
    return bool(CHAPTER_API_KEY) and hmac.compare_digest(key, CHAPTER_API_KEY)


@router.get("/chapters/master")
def chapters_master(request: Request, class_level: str, code: str = "",
                    db: Session = Depends(get_db)):
    if not _chapter_key_ok(request):
        raise HTTPException(status_code=401, detail="Chapter API key required.")
    _ensure_syllabus(db)
    out = []
    for s in subject_list(db, class_level, include_hidden=True):
        if code and s["code"] != str(code):
            continue
        if s.get("status") != "ready":
            if code:
                raise HTTPException(status_code=409,
                                    detail="Syllabus not verified. " + "; ".join(s.get("issues", [])))
            continue
        out.append({"code": s["code"], "name": s["name"], "chapters": SD.chapter_master(s)})
    return {"class_level": class_level, "subjects": out, "count": len(out)}


@router.post("/chapters/resolve")
def chapters_resolve(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    if not _chapter_key_ok(request):
        raise HTTPException(status_code=401, detail="Chapter API key required.")
    _ensure_syllabus(db)
    cl = str(payload.get("class_level") or "")
    code = str(payload.get("subject_code") or "")
    if not code and payload.get("subject_name"):
        code = subject_code_for_name(db, cl, payload["subject_name"]) or ""
    subj = get_subject(db, cl, code) if code else None
    if not subj:
        raise HTTPException(status_code=404, detail="Subject not found in the syllabus master.")
    if subj.get("status") != "ready":
        raise HTTPException(status_code=409,
                            detail="Syllabus not verified. " + "; ".join(subj.get("issues", [])))
    results = [SD.resolve_chapter(subj, n) for n in (payload.get("names") or [])]
    return {"subject": {"code": subj["code"], "name": subj["name"]}, "results": results,
            "summary": {a: len([r for r in results if r["action"] == a])
                        for a in ("accept", "review", "reject")}}


# ---------------------------------------------------------------------------
# Timetable helper - imported by admin_routes.py and teacher_routes.py
# ---------------------------------------------------------------------------

def annotate_timetable_rows(db, class_name, rows):
    """
    Tag every parsed timetable row with a chapter match result.

    Adds to each row:
        match_action   accept | review | reject | no_master
        match_no       canonical lesson number when accepted
        match_title    canonical chapter title when accepted
        match_kind     PE or TMA
        match_note     reason shown to the admin

    Nothing is blocked here. The admin sees the tags in the preview screen and
    decides. This is what stops grammar sub-topics and revision slots from
    silently becoming chapters.
    """
    try:
        _ensure_syllabus(db)
    except Exception:
        return rows
    cl = class_level_from_name(class_name)
    cache = {}
    for r in rows:
        try:
            name = (r.get("subject") or "").strip()
            if name not in cache:
                c = subject_code_for_name(db, cl, name) if cl else None
                cache[name] = get_subject(db, cl, c) if c else None
            subj = cache[name]
            if not subj or subj.get("status") != "ready":
                r["match_action"] = "no_master"
                r["match_note"] = ("Verified syllabus not loaded for this subject, "
                                   "chapter name will be saved as typed.")
                continue
            res = SD.resolve_chapter(subj, r.get("chapter") or "")
            r["match_action"] = res["action"]
            r["match_note"] = res["reason"]
            if res["chapter"]:
                r["match_no"] = res["chapter"]["no"]
                r["match_title"] = res["chapter"]["title"]
                r["match_kind"] = res["chapter"]["kind"]
                r["match_score"] = round(res["score"], 3)
            r["match_candidates"] = [c["no"] + " " + c["title"] for c in res.get("candidates", [])]
        except Exception:
            r["match_action"] = "no_master"
            r["match_note"] = "Chapter check could not run."
    return rows
