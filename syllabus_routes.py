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
import re
import json
import math
import hmac
from datetime import datetime, date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body, Request
from sqlalchemy.orm import Session
from sqlalchemy import text as _text

from database import get_db
from security import get_admin, get_student, get_teacher
from models import StudentProfile, AvailableSubject, AppSetting

import syllabus_data as SD

router = APIRouter(prefix="/api/syllabus", tags=["Syllabus Tracker"])

CHAPTER_API_KEY = os.environ.get("CHAPTER_API_KEY", "")

DEFAULTS = {
    "syl_high_target": "75",
    "syl_top_target": "90",
    "syl_safety_buffer": "0",
    # the cushion is measured in chapters, not marks: one extra chapter, and a
    # second only if the first one is too small to be worth anything
    "syl_bonus_chapters": "2",
    "syl_bonus_min_marks": "6",
    "syl_ondemand_practical_gap": "20",
    "syl_sessions": json.dumps(SD.EXAM_SESSIONS),
}

_SYL_READY = False


# ---------------------------------------------------------------------------
# Lazy migration
# ---------------------------------------------------------------------------

_NIOS_REF_HINT = ("Enter a valid NIOS Reference No. (e.g. A0726320328 - 1 letter + 10 digits) "
                  "or Enrollment No. (e.g. 050108263013 - 12 digits).")
_NIOS_REF_RE = re.compile(r"^([A-Z][0-9]{10}|[0-9]{12})$")

def _valid_nios_ref(v):
    """Reference No. like A0726320328 (1 alphabet + 10 digits = 11 total) or
    Enrollment No. like 050108263013 (12 digits)."""
    return bool(_NIOS_REF_RE.match((v or "").strip().upper()))


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
        """CREATE TABLE IF NOT EXISTS predicted_results (
             id INTEGER PRIMARY KEY AUTO_INCREMENT,
             student_id INTEGER, target VARCHAR(10),
             subjects LONGTEXT, total FLOAT, max_marks FLOAT, percentage FLOAT,
             created_at DATETIME NULL, updated_at DATETIME NULL)""",
        """CREATE TABLE IF NOT EXISTS milestone_dates (
             id INTEGER PRIMARY KEY AUTO_INCREMENT,
             student_id INTEGER, subject_code VARCHAR(10), target VARCHAR(10),
             reached_at DATETIME)""",
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
    for col in ["exam_session VARCHAR(30) NULL", "study_target VARCHAR(10) NULL",
                "exam_date VARCHAR(20) NULL", "exam_stream VARCHAR(4) NULL",
                "goal VARCHAR(20) NULL", "goal_custom VARCHAR(120) NULL",
                "nios_ref VARCHAR(40) NULL"]:
        try:
            db.execute(_text("ALTER TABLE student_profiles ADD COLUMN %s" % col)); db.commit()
        except Exception:
            db.rollback()
    for col in ["option_choice TEXT NULL"]:
        try:
            db.execute(_text("ALTER TABLE chapter_plans ADD COLUMN %s" % col)); db.commit()
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
        "top_target": float(_setting(db, "syl_top_target", "90") or 90),
        "buffer_pct": float(_setting(db, "syl_safety_buffer", "0") or 0),
        "bonus_chapters": int(float(_setting(db, "syl_bonus_chapters", "2") or 2)),
        "bonus_min_marks": float(_setting(db, "syl_bonus_min_marks", "6") or 6),
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

def apply_stream(marks, stream):
    """
    Stream 2, 3 and 4 learners have no TMA (Notification 34/2021 Annexure).
    Their theory paper is not scaled down, so the full question paper counts
    and the pass mark is 33 percent of it.
    """
    m = dict(marks)
    if str(stream) not in ("2", "3", "4"):
        return m
    paper = float(m.get("paper_marks") or m.get("theory_max") or 0)
    pr = float(m.get("practical_max") or 0)
    m["theory_max"] = paper
    m["tma_max"] = 0
    m["practical_max"] = pr
    if m.get("combined_pass"):
        m["combined_pass"] = int(round((paper + pr) * 0.33))
        m["theory_pass"] = 0
        m["practical_pass"] = 0
    else:
        m["theory_pass"] = int(round(paper * 0.33))
        m["practical_pass"] = int(round(pr * 0.33)) if pr else 0
    m["aggregate_pass"] = 33
    m["stream"] = str(stream)
    return m


def compute(subject, selected, tma_assumed=None, practical_assumed=None,
            high_target=75.0, buffer_pct=0.0, bonus_chapters=2, bonus_min_marks=6.0,
            stream="1", top_target=90.0, choice=None):
    m = apply_stream(subject["marks"], stream)
    rows = SD.flatten(subject)
    pe_rows = [r for r in rows if r["kind"] == "PE"]
    paper = float(m.get("paper_marks") or (m["theory_max"] / 0.8 if m["theory_max"] else 0))
    scale = (m["theory_max"] / paper) if paper else 0

    sel = set(selected or [])

    # OR option pairs: one exam slot, two options. Plans see the pair as one
    # module (the chosen option, else the heaviest); the pair's marks count
    # once; the student's covered marks are their best option's worth.
    groups = SD._optional_groups(subject.get("modules", []))
    group_of, reps = {}, {}
    mod_order = {mod["module"]: i for i, mod in enumerate(subject.get("modules", []))}
    for g, ms in groups.items():
        pick = (choice or {}).get(g)
        rep = next((mod for mod in ms if mod["module"] == pick), None)
        if rep is None:
            rep = max(ms, key=lambda mod: (
                float(mod.get("weightage") or 0),
                len([l for l in mod["lessons"] if l["kind"] == "PE"]),
                -mod_order.get(mod["module"], 0)))
        reps[g] = rep["module"]
        for mod in ms:
            group_of[mod["module"]] = g
    pe_plan = [r for r in pe_rows
               if r["module"] not in group_of or reps.get(group_of[r["module"]]) == r["module"]]

    covered_group = 0.0
    for g, ms in groups.items():
        per_mod = {}
        for mod in ms:
            per_mod[mod["module"]] = round(sum(
                r["marks"] for r in pe_rows if r["module"] == mod["module"] and r["no"] in sel), 2)
        pick = (choice or {}).get(g)
        covered_group += per_mod.get(pick, max(per_mod.values()) if per_mod else 0.0)

    total_paper = round(sum(r["marks"] for r in pe_plan), 2)
    covered_paper = round(
        sum(r["marks"] for r in pe_plan if r["no"] in sel and r["module"] not in group_of)
        + covered_group, 2)
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

    need_theory_agg = max(m["aggregate_pass"] - tma - pr, 0)
    need_theory_final = max(need_theory, need_theory_agg)
    # bare requirement, before any margin
    pass_raw = round(((need_theory_final / scale) if scale else 0), 1)
    high_raw = round(((max(high_target - tma - pr, 0) / scale) if scale else 0), 1)
    top_raw = round(((max(top_target - tma - pr, 0) / scale) if scale else 0), 1)
    theory_raw = round(((need_theory / scale) if scale else 0), 1)
    # The requirement is exactly the NIOS rule. The cushion on top is measured
    # in whole chapters, not in marks, because a learner studies chapters:
    # one extra chapter, and a second one only if the first is too small to
    # give any real protection.
    # NIOS publishes weightage per MODULE, never per chapter. So suggestions
    # are whole modules, best value first: the module that carries the most
    # marks per chapter to study (high weightage, few chapters) ranks first.
    group_max_w = {g: max(float(mod.get("weightage") or 0) for mod in ms)
                   for g, ms in groups.items()}
    modules = []
    for r in pe_plan:
        if not modules or modules[-1]["module"] != r["module"]:
            w = float(r.get("module_weightage") or 0) or \
                group_max_w.get(group_of.get(r["module"]), 0.0)
            modules.append({"module": r["module"],
                            "weightage": w,
                            "rows": []})
        modules[-1]["rows"].append(r)
    for i, mo in enumerate(modules):
        mo["pe_count"] = len(mo["rows"])
        mo["marks"] = round(sum(r["marks"] for r in mo["rows"]), 2)
        mo["ratio"] = round(mo["weightage"] / mo["pe_count"], 3) if mo["pe_count"] else 0.0
        mo["order"] = i
    ranked_mods = sorted(modules, key=lambda mo: (-mo["ratio"], -mo["weightage"], mo["order"]))
    ranked = [r for mo in ranked_mods for r in mo["rows"]]
    max_bonus_ch = max(int(bonus_chapters or 0), 0)
    min_bonus = float(bonus_min_marks or 0)

    def with_bonus(req):
        """Return (total_target, bonus_marks, bonus_rows) for a requirement."""
        acc, idx = 0.0, len(ranked)
        for i, r in enumerate(ranked):
            if acc + 0.01 >= req:
                idx = i
                break
            acc += r["marks"]
        else:
            idx = len(ranked)
        rows_, bmarks = [], 0.0
        for r in ranked[idx:]:
            if len(rows_) >= max_bonus_ch:
                break
            rows_.append(r)
            bmarks = round(bmarks + r["marks"], 2)
            if bmarks + 0.01 >= min_bonus:
                break
        return (min(round(req + bmarks, 1), total_paper), round(bmarks, 1), rows_)

    pass_core = min(round(pass_raw, 1), total_paper)
    high_core = min(round(high_raw, 1), total_paper)
    # theory alone is compulsory: a learner can clear the aggregate and still fail
    theory_core = min(round(theory_raw, 1), total_paper)
    pass_paper, pass_bonus, pass_bonus_rows = with_bonus(pass_core)
    high_paper, high_bonus, high_bonus_rows = with_bonus(high_core)
    theory_paper, theory_bonus, _ = with_bonus(theory_core)
    top_core = min(round(top_raw, 1), total_paper)
    top_paper, top_bonus, top_bonus_rows = with_bonus(top_core)

    remaining = [r for mo in ranked_mods for r in mo["rows"] if r["no"] not in sel]

    def modules_until(target_paper):
        """Whole modules, best marks-per-chapter first, until the target is covered.
        The last module may not need to be finished completely: when a heavy
        multi-chapter module crosses the target with only a few chapters, the
        plan says "any k chapters" instead of the whole module."""
        need = target_paper - covered_paper
        chosen, acc = [], 0.0
        for mo in ranked_mods:
            if need - acc <= 0.01:
                break
            picked = len([r for r in mo["rows"] if r["no"] in sel])
            new_marks = round(sum(r["marks"] for r in mo["rows"] if r["no"] not in sel), 2)
            left = mo["pe_count"] - picked
            per_ch = (mo["marks"] / mo["pe_count"]) if mo["pe_count"] else 0.0
            missing = need - acc
            require = None
            if per_ch > 0 and left > 0 and missing > 0:
                k = int(missing / per_ch + 0.999999)
                if 0 < k < left:
                    require = {"type": "any", "k": k}    # any k chapters are enough
                else:
                    require = {"type": "full"}           # the whole module is compulsory
            chosen.append({
                "module": mo["module"], "weightage": mo["weightage"],
                "pe_count": mo["pe_count"], "ratio": mo["ratio"],
                "marks": mo["marks"], "new_marks": new_marks,
                "picked": picked, "require": require,
                "chapters": mo["rows"],
            })
            acc += new_marks
        return chosen, round(acc, 2)

    pass_plan_modules, pass_plan_marks = modules_until(pass_paper)
    high_plan_modules, high_plan_marks = modules_until(high_paper)
    top_plan_modules, top_plan_marks = modules_until(top_paper)

    def pick_until(target_paper):
        need = target_paper - covered_paper
        chosen, acc = [], 0.0
        for r in remaining:
            if need - acc <= 0.01:
                break
            chosen.append(r); acc += r["marks"]
        return chosen

    def bonus_after(core_paper, safe_paper):
        """Chapters sitting between the requirement and the cushion."""
        core_set = {r["no"] for r in pick_until(core_paper)}
        return [r for r in pick_until(safe_paper) if r["no"] not in core_set]

    has_marks = total_paper > 0
    return {
        "paper_marks": paper, "scale": round(scale, 4),
        "total_pe_marks": total_paper, "covered_paper": covered_paper,
        "covered_theory": covered_theory,
        "tma_assumed": tma, "practical_assumed": pr,
        "projected_total": round(covered_theory + tma + pr, 1),
        "pass_rule": pass_rule,
        "bonus_chapter_count": len(pass_bonus_rows),
        # the cushion is whatever sits above the NIOS requirement
        "bonus_marks": pass_bonus,
        "pass_bonus_marks": pass_bonus,
        "high_bonus_marks": high_bonus,
        "theory_bonus_marks": theory_bonus,
        "pass_core_needed": pass_core,
        "pass_core_reached": has_marks and covered_paper + 0.01 >= pass_core,
        "pass_bonus_chapters": pass_bonus_rows,
        "high_core_needed": high_core,
        "high_core_reached": has_marks and covered_paper + 0.01 >= high_core,
        "high_bonus_chapters": high_bonus_rows,
        "theory_core_needed": theory_core,
        "pass_paper_raw": pass_raw,
        "high_paper_raw": high_raw,
        "theory_paper_raw": theory_raw,
        "pass_paper_needed": pass_paper,
        "pass_reached": has_marks and covered_paper + 0.01 >= pass_paper,
        # theory only requirement, tracked separately because it is compulsory
        "theory_pass_mark": round(need_theory, 1),
        "theory_paper_needed": theory_paper,
        "theory_reached": has_marks and covered_theory + 0.01 >= need_theory,
        "theory_gap_theory": max(round(need_theory - covered_theory, 1), 0),
        "theory_gap_paper": max(round(theory_paper - covered_paper, 1), 0),
        "aggregate_reached": round(covered_theory + tma + pr, 1) + 0.01 >= m["aggregate_pass"],
        # NIOS checks three things separately. All of them are reported in the
        # units the marksheet uses, so nothing has to be converted by the reader.
        "theory_have": covered_theory,
        "theory_need": round(need_theory, 1),
        "theory_max": m["theory_max"],
        "practical_have": pr,
        "practical_need": float(m.get("practical_pass") or 0),
        "practical_max": m["practical_max"],
        "practical_reached": (not m.get("has_practical")) or pr + 0.01 >= float(m.get("practical_pass") or 0),
        "tma_have": tma,
        "tma_max": m["tma_max"],
        "aggregate_have": round(covered_theory + tma + pr, 1),
        "aggregate_need": m["aggregate_pass"],
        "has_practical": bool(m.get("has_practical")),
        "combined_pass": float(m.get("combined_pass") or 0),
        "high_target": high_target,
        "high_paper_needed": high_paper,
        "high_reached": has_marks and covered_paper + 0.01 >= high_paper,
        "top_target": top_target,
        "top_core_needed": top_core,
        "top_paper_needed": top_paper,
        "top_reached": has_marks and covered_paper + 0.01 >= top_paper,
        "top_bonus_marks": top_bonus,
        "top_bonus_chapters": top_bonus_rows,
        "top_core_reached": has_marks and covered_paper + 0.01 >= top_core,
        "pass_plan_modules": pass_plan_modules,
        "pass_plan_marks": pass_plan_marks,
        "high_plan_modules": high_plan_modules,
        "high_plan_marks": high_plan_marks,
        "top_plan_modules": top_plan_modules,
        "top_plan_marks": top_plan_marks,
        "pass_gap_chapters": pick_until(pass_paper),
        "high_gap_chapters": pick_until(high_paper),
        "top_gap_chapters": pick_until(top_paper),
        "selected_count": len([r for r in pe_plan if r["no"] in sel]),
        "pe_count": len(pe_plan), "buffer_pct": buffer_pct,
        "option_reps": reps,
        "stream": str(stream), "marks": m,
    }


def _plan_row(db, student_id, code):
    r = db.execute(_text(
        "SELECT selected, done, tma_assumed, practical_assumed, option_choice FROM chapter_plans "
        "WHERE student_id=:s AND subject_code=:c"), {"s": student_id, "c": str(code)}).fetchone()
    if not r:
        return [], [], -1.0, -1.0, {}
    try:
        sel = json.loads(r[0] or "[]")
    except Exception:
        sel = []
    try:
        done = json.loads(r[1] or "[]")
    except Exception:
        done = []
    try:
        choice = json.loads(r[4] or "{}") if len(r) > 4 else {}
    except Exception:
        choice = {}
    return (sel, done, (r[2] if r[2] is not None else -1.0),
            (r[3] if r[3] is not None else -1.0), choice if isinstance(choice, dict) else {})


def _stream_for(db, sp):
    """
    The stream is never asked from the learner. It follows the examination they
    picked: October and April carry a TMA, Stream 2 and On Demand do not.
    """
    sid = getattr(sp, "exam_session", "") or ""
    for x in _sessions(db):
        if x.get("id") == sid:
            return "1" if x.get("tma", True) else "2"
    return "1"


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
    subs = subject_list(db, cl, include_hidden=True)
    known = {s["code"] for s in subs}
    hidden = {s["code"] for s in subs if s.get("hidden")}
    for n in names:
        c = subject_code_for_name(db, cl, n)
        if c and c in known:
            # a subject hidden by the admin is removed from the tracker
            # entirely - not listed, not accessible, not reported as missing
            if c in hidden:
                continue
            if c not in codes:
                codes.append(c)
        else:
            unmapped.append(str(n))
    return cl, codes, unmapped


def _sessions(db):
    try:
        out = json.loads(_setting(db, "syl_sessions", "[]"))
        return out if out else SD.EXAM_SESSIONS
    except Exception:
        return SD.EXAM_SESSIONS


def _dleft(iso):
    if not iso:
        return None
    try:
        return (date.fromisoformat(str(iso)[:10]) - date.today()).days
    except Exception:
        return None


def _exam_dates(db, session_id, custom_date=""):
    """
    Return (info, session) where info carries both countdowns.

    NIOS runs practicals before the theory papers, so a learner needs to see
    both. For On Demand the learner picks the date, and the practical is
    assumed a few weeks before it unless the admin says otherwise.
    """
    sess = None
    for x in _sessions(db):
        if x.get("id") == session_id:
            sess = x
            break
    if not sess:
        return {"theory_date": "", "practical_date": "", "theory_days": None,
                "practical_days": None, "days_left": None, "custom": False}, None

    theory = sess.get("theory_date") or sess.get("date") or ""
    practical = sess.get("practical_date") or ""
    custom = False
    if sess.get("ask_date") or (not theory and custom_date):
        theory = str(custom_date or "")[:10]
        custom = True
        if theory and not practical:
            try:
                gap = int(float(_setting(db, "syl_ondemand_practical_gap", "20") or 20))
                practical = (date.fromisoformat(theory) - timedelta(days=gap)).isoformat()
            except Exception:
                practical = ""
    td, pd_ = _dleft(theory), _dleft(practical)
    return ({"theory_date": theory, "practical_date": practical,
             "theory_days": td, "practical_days": pd_,
             # what the learner reads. NIOS announces the date sheet late, so
             # these stay worded as expectations rather than fixed dates
             "theory_label": sess.get("theory_label", ""),
             "practical_label": sess.get("practical_label", ""),
             "theory_confirmed": bool(custom),
             "days_left": td, "custom": custom}, sess)


def _days_left(db, session_id, custom_date=""):
    info, sess = _exam_dates(db, session_id, custom_date)
    return info["theory_days"], sess


# ---------------------------------------------------------------------------
# STUDENT ENDPOINTS
# ---------------------------------------------------------------------------

def _auto_session(db, sp):
    """Portal/app students often never open the session dropdown. Pick the
    nearest upcoming exam session for them and persist it (stream follows),
    so days-left, plans and result cards always have a session behind them."""
    cur = getattr(sp, "exam_session", "") or ""
    if cur:
        return cur
    sess_list = _sessions(db)
    best = None
    for x in sess_list:
        td = _dleft(x.get("theory_date") or x.get("date") or "")
        if td is not None and td >= 0:
            if best is None or td < best[1]:
                best = (x, td)
    if best is None and sess_list:
        best = (sess_list[0], None)
    if not best:
        return ""
    x = best[0]
    st = "1" if x.get("tma", True) else "2"
    try:
        db.execute(_text("UPDATE student_profiles SET exam_session=:e, exam_stream=:s WHERE id=:i"),
                   {"e": x.get("id") or "", "s": st, "i": sp.id})
        db.commit()
        sp.exam_session, sp.exam_stream = x.get("id") or "", st
    except Exception:
        db.rollback()
    return getattr(sp, "exam_session", "") or ""


@router.get("/me")
def syl_me(db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    cfg = _cfg(db)
    had_session = bool(getattr(sp, "exam_session", "") or "")
    session_id = _auto_session(db, sp)
    info, sess = _exam_dates(db, session_id, getattr(sp, "exam_date", "") or "")
    return {
        "name": user.name, "class_level": cl, "subject_codes": codes,
        "unmapped_subjects": unmapped,
        "exam_session": session_id,
        "session_auto": bool(session_id) and not had_session,
        "exam_date": getattr(sp, "exam_date", "") or "",
        "stream": _stream_for(db, sp),
        "has_tma": _stream_for(db, sp) == "1",
        "target": getattr(sp, "study_target", "") or "",
        "goal": getattr(sp, "goal", "") or "",
        "goal_custom": getattr(sp, "goal_custom", "") or "",
        "nios_ref": getattr(sp, "nios_ref", "") or "",
        "source": getattr(sp, "source", "") or "mvs_app",
        "days_left": info["theory_days"], "exam": info,
        "session": sess, "sessions": _sessions(db),
        "high_target": cfg["high_target"], "top_target": cfg["top_target"],
        "buffer_pct": cfg["buffer_pct"],
        "bonus_chapters": cfg["bonus_chapters"], "bonus_min_marks": cfg["bonus_min_marks"],
    }


@router.post("/profile")
def syl_profile(payload: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    # partial-update safe: only the fields actually sent are changed, so a
    # date-only change can never wipe the session/target (and vice versa)
    sets, params = [], {"i": sp.id}
    if payload.get("exam_session") is not None:
        sess = str(payload.get("exam_session") or "")[:30]
        # the stream follows the chosen examination, it is never sent by the client
        st = "1"
        for x in _sessions(db):
            if x.get("id") == sess:
                st = "1" if x.get("tma", True) else "2"
                break
        sets += ["exam_session=:e", "exam_stream=:s"]
        params["e"], params["s"] = sess, st
    if payload.get("target") is not None:
        sets.append("study_target=:t")
        params["t"] = str(payload.get("target") or "")[:10]
    if payload.get("exam_date") is not None:
        sets.append("exam_date=:d")
        params["d"] = str(payload.get("exam_date") or "")[:20]
    if payload.get("goal") is not None:
        g = str(payload.get("goal") or "")[:20]
        if g not in ("", "jee", "neet", "other"):
            raise HTTPException(status_code=400, detail="Unknown goal.")
        sets.append("goal=:g")
        params["g"] = g
        if g != "other":
            # switching away from Other clears the handwritten goal
            sets.append("goal_custom=:gc")
            params["gc"] = ""
    if payload.get("goal_custom") is not None:
        sets.append("goal_custom=:gc")
        params["gc"] = str(payload.get("goal_custom") or "")[:120]
    if payload.get("nios_ref") is not None:
        ref = str(payload.get("nios_ref") or "").strip().upper()[:40]
        if ref and not _valid_nios_ref(ref):
            raise HTTPException(status_code=400, detail=_NIOS_REF_HINT)
        sets.append("nios_ref=:r")
        params["r"] = ref
    if sets:
        db.execute(_text("UPDATE student_profiles SET " + ", ".join(sets) + " WHERE id=:i"), params)
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
        sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
        ready = subj.get("status") == "ready"
        calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"], cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice) if ready else None
        out.append({"code": subj["code"], "name": subj["name"],
                    "status": subj.get("status", "pending"),
                    "selected": len(sel) if ready else 0,
                    "done": len(done) if ready else 0, "calc": calc})
    for n in unmapped:
        out.append({"code": "", "name": n, "status": "pending",
                    "selected": 0, "done": 0, "calc": None})
    info, sess = _exam_dates(db, getattr(sp, "exam_session", "") or "",
                             getattr(sp, "exam_date", "") or "")
    ms = _milestone_map(db, sp.id)
    for r in out:
        r["milestones"] = ms.get(r["code"], {})
    return {"subjects": out, "days_left": info["theory_days"], "exam": info, "session": sess,
            "unmapped_subjects": unmapped,
            "target": getattr(sp, "study_target", "") or ""}


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
    sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
    cfg = _cfg(db)
    return {
        "subject": {"code": subj["code"], "name": subj["name"], "status": subj["status"],
                    "marks": subj["marks"], "display_mode": subj.get("display_mode") or "modules"},
        "modules": subj.get("modules", []),
        "chapters": SD.flatten(subj),
        "selected": sel, "done": done,
        "option_choice": choice,
        "milestones": _milestone_map(db, sp.id).get(str(code), {}),
        "calc": compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"], cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice),
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
    _prev_sel, _prev_done, _t, _p, choice = _plan_row(db, sp.id, code)
    cfg = _cfg(db)
    calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"],
                   cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
    _sync_milestones(db, sp, code, calc)
    db.commit()
    return {"ok": True, "calc": calc, "option_choice": choice,
            "milestones": _milestone_map(db, sp.id).get(code, {})}


@router.post("/option-choice")
def syl_option_choice(payload: dict = Body(...), db: Session = Depends(get_db), user=Depends(get_student)):
    """NIOS optional module: one exam slot, two options ("which is your
    choice?"). The student studies only one - plans and marks follow it."""
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, _un = _student_codes(db, sp)
    code = str(payload.get("subject_code") or "")
    if code not in codes:
        raise HTTPException(status_code=403, detail="This subject is not in your enrolment.")
    subj = get_subject(db, cl, code)
    if not subj or subj.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Syllabus for this subject is not verified yet.")
    group = str(payload.get("group") or "").strip()
    module = str(payload.get("module") or "").strip()
    groups = SD._optional_groups(subj.get("modules", []))
    if group not in groups:
        raise HTTPException(status_code=400, detail="Unknown option group for this subject.")
    names = [m["module"] for m in groups[group]]
    if module not in names:
        raise HTTPException(status_code=400, detail="That module is not one of the options.")

    sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
    choice[group] = module
    exists = db.execute(_text(
        "SELECT id FROM chapter_plans WHERE student_id=:s AND subject_code=:c"),
        {"s": sp.id, "c": code}).fetchone()
    if exists:
        db.execute(_text("UPDATE chapter_plans SET option_choice=:oc, updated_at=:u "
                         "WHERE student_id=:s AND subject_code=:c"),
                   {"oc": json.dumps(choice), "u": datetime.utcnow(), "s": sp.id, "c": code})
    else:
        db.execute(_text("INSERT INTO chapter_plans (student_id, subject_code, selected, done, "
                         "tma_assumed, practical_assumed, option_choice, updated_at) "
                         "VALUES (:s, :c, '[]', '[]', -1, -1, :oc, :u)"),
                   {"s": sp.id, "c": code, "oc": json.dumps(choice), "u": datetime.utcnow()})
    db.commit()
    cfg = _cfg(db)
    calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"],
                   cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
    _sync_milestones(db, sp, code, calc)
    db.commit()
    return {"ok": True, "option_choice": choice, "calc": calc,
            "milestones": _milestone_map(db, sp.id).get(code, {})}


def _tier_hit(calc, tier):
    if tier == "pass":
        return bool(calc.get("pass_reached") and calc.get("theory_reached"))
    return bool(calc.get(tier + "_reached"))


def _milestone_map(db, student_id):
    """{subject_code: {target: 'YYYY-MM-DD'}} of every target ever reached
    (and still standing) by this student."""
    out = {}
    for r in db.execute(_text(
            "SELECT subject_code, target, reached_at FROM milestone_dates WHERE student_id=:s"),
            {"s": student_id}).fetchall():
        out.setdefault(r[0], {})[r[1]] = str(r[2])[:10]
    return out


def _sync_milestones(db, sp, code, calc):
    """
    Stamp the day a target is first reached; wipe the stamp when the target
    drops back below the line (clear-all really does send the student back to
    square one, dates included). Stored result cards that the student no
    longer qualifies for are erased the same way.
    """
    now = datetime.utcnow()
    for tier in ("pass", "high", "top"):
        ex = db.execute(_text(
            "SELECT id FROM milestone_dates WHERE student_id=:s AND subject_code=:c AND target=:t"),
            {"s": sp.id, "c": code, "t": tier}).fetchone()
        if _tier_hit(calc, tier):
            if not ex:
                db.execute(_text(
                    "INSERT INTO milestone_dates (student_id, subject_code, target, reached_at) "
                    "VALUES (:s,:c,:t,:u)"), {"s": sp.id, "c": code, "t": tier, "u": now})
        elif ex:
            db.execute(_text("DELETE FROM milestone_dates WHERE id=:i"), {"i": ex[0]})
    # a result card stays only while every subject still reaches its tier
    rows = _progress_rows(db, sp)
    for tier in ("pass", "high", "top"):
        card = db.execute(_text(
            "SELECT id FROM predicted_results WHERE student_id=:s AND target=:t"),
            {"s": sp.id, "t": tier}).fetchone()
        if card and not (rows and all(_tier_hit(r["calc"], tier) for r in rows)):
            db.execute(_text("DELETE FROM predicted_results WHERE id=:i"), {"i": card[0]})


# ---------------------------------------------------------------------------
# Predicted results
# ---------------------------------------------------------------------------
# A predicted result card is unlocked only when EVERY ready subject of the
# student reaches a target tier (pass / high / top). The server recomputes
# everything from the saved chapter plans - the client only asks.

def _tier_needed(calc, tier):
    if tier == "top":
        return float(calc.get("top_paper_needed") or 0)
    if tier == "high":
        return float(calc.get("high_paper_needed") or 0)
    return max(float(calc.get("pass_paper_needed") or 0),
               float(calc.get("theory_paper_needed") or 0))


def _progress_rows(db, sp):
    """Per-subject fresh calc for one student (ready subjects only)."""
    cl, codes, _un = _student_codes(db, sp)
    cfg = _cfg(db)
    out = []
    for code in codes:
        subj = get_subject(db, cl, code)
        if not subj or subj.get("status") != "ready":
            continue
        sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
        calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"],
                       cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
        out.append({"code": subj["code"], "name": subj["name"],
                    "selected": len(sel), "calc": calc})
    return out


def _prediction_card(db, sp, tier):
    """(complete, card). complete = every ready subject reached the tier."""
    rows = _progress_rows(db, sp)
    if not rows:
        return False, None
    subs, total = [], 0.0
    for r in rows:
        c = r["calc"]
        if not _tier_hit(c, tier):
            return False, None
        proj = min(100.0, round(float(c.get("projected_total") or 0), 1))
        mk = c.get("marks") or {}
        subs.append({
            "code": r["code"], "name": r["name"], "projected": proj,
            # marksheet bifurcation for the result card
            "theory": round(float(c.get("covered_theory") or 0), 1),
            "theory_max": mk.get("theory_max") or 0,
            "tma": round(float(c.get("tma_assumed") or 0), 1),
            "tma_max": mk.get("tma_max") or 0,
            "practical": round(float(c.get("practical_assumed") or 0), 1),
            "practical_max": mk.get("practical_max") or 0,
            # paper-level numbers for the theory calculation popup
            "paper": round(float(c.get("covered_paper") or 0), 1),
            "paper_max": float(c.get("paper_marks") or 0),
            "theory_pass": float(c.get("theory_pass_mark") or 0),
        })
        total += proj
    n = len(subs)
    return True, {"target": tier, "subjects": subs, "total": round(total, 1),
                  "max_marks": n * 100, "percentage": round(total / n, 1) if n else 0.0}


@router.post("/predicted")
def syl_predicted_record(payload: dict = Body(...), db: Session = Depends(get_db),
                         user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    tier = str((payload or {}).get("target") or "")
    if tier not in ("pass", "high", "top"):
        raise HTTPException(status_code=400, detail="Unknown target.")
    complete, card = _prediction_card(db, sp, tier)
    if not complete:
        raise HTTPException(status_code=409,
                            detail="Every subject must reach this target before the result card is made.")
    # NIOS reference/enrollment number — EVERY student (mvs app ya mvs portal,
    # dono) enters it manually, exactly once; the saved value is then reused on
    # every later card. Strict format check, warna card nahi banta.
    ref = getattr(sp, "nios_ref", "") or ""
    sent_ref = str((payload or {}).get("nios_ref") or "").strip().upper()[:40]
    if not ref and sent_ref:
        if not _valid_nios_ref(sent_ref):
            raise HTTPException(status_code=400, detail=_NIOS_REF_HINT)
        db.execute(_text("UPDATE student_profiles SET nios_ref=:r WHERE id=:i"),
                   {"r": sent_ref, "i": sp.id})
        db.commit()
        ref = sent_ref
    if not ref:
        return {"need_ref": True, "target": tier}
    now = datetime.utcnow()
    ex = db.execute(_text("SELECT id FROM predicted_results WHERE student_id=:s AND target=:t"),
                    {"s": sp.id, "t": tier}).fetchone()
    args = {"s": sp.id, "t": tier, "sub": json.dumps(card["subjects"]),
            "tot": card["total"], "mx": card["max_marks"], "pc": card["percentage"], "u": now}
    if ex:
        db.execute(_text("UPDATE predicted_results SET subjects=:sub, total=:tot, max_marks=:mx, "
                         "percentage=:pc, updated_at=:u WHERE student_id=:s AND target=:t"), args)
    else:
        db.execute(_text("INSERT INTO predicted_results (student_id, target, subjects, total, "
                         "max_marks, percentage, created_at, updated_at) "
                         "VALUES (:s,:t,:sub,:tot,:mx,:pc,:u,:u)"), args)
    db.commit()
    card["student"] = (sp.user.name if getattr(sp, "user", None) else "") or ""
    card["nios_ref"] = ref
    card["recorded"] = True
    return card


@router.get("/predicted/mine")
def syl_predicted_mine(db: Session = Depends(get_db), user=Depends(get_student)):
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    rows = db.execute(_text(
        "SELECT target, subjects, total, max_marks, percentage, updated_at "
        "FROM predicted_results WHERE student_id=:s ORDER BY id"), {"s": sp.id}).fetchall()
    ref = getattr(sp, "nios_ref", "") or ""
    return {"cards": [{"target": r[0], "subjects": json.loads(r[1] or "[]"), "total": r[2],
                       "max_marks": r[3], "percentage": r[4], "updated_at": str(r[5] or ""),
                       "nios_ref": ref}
                      for r in rows],
            "goal": getattr(sp, "goal", "") or "",
            "goal_custom": getattr(sp, "goal_custom", "") or ""}


@router.get("/admin/predicted")
def syl_admin_predicted(target: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    q = ("SELECT pr.student_id, pr.target, pr.subjects, pr.total, pr.max_marks, pr.percentage, "
         "pr.updated_at, sp.class_level, sp.batch_name, u.name, u.user_id, "
         "sp.goal, sp.goal_custom, sp.nios_ref "
         "FROM predicted_results pr "
         "JOIN student_profiles sp ON sp.id = pr.student_id "
         "JOIN users u ON u.id = sp.user_id")
    args = {}
    if target in ("pass", "high", "top"):
        q += " WHERE pr.target=:t"
        args["t"] = target
    out = [{"student_id": r[0], "target": r[1], "subjects": json.loads(r[2] or "[]"),
            "total": r[3], "max_marks": r[4], "percentage": r[5], "updated_at": str(r[6] or ""),
            "class_level": r[7], "batch": r[8], "name": r[9], "user_id": r[10],
            "goal": r[11] or "", "goal_custom": r[12] or "", "nios_ref": r[13] or ""}
           for r in db.execute(_text(q), args).fetchall()]
    out.sort(key=lambda x: (-(x["percentage"] or 0), (x["name"] or "").lower()))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return {"results": out}


@router.get("/teacher/predicted")
def syl_teacher_predicted(target: str = "", db: Session = Depends(get_db),
                          user=Depends(get_teacher)):
    """Same rank list as the admin sees, scoped to the teacher's own students
    (subject overlap, exactly like the teacher student search)."""
    _ensure_syllabus(db)
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
    tsubs = {str(x).strip().lower() for x in (tp.subjects or []) if str(x).strip()} if tp else set()
    q = ("SELECT pr.student_id, pr.target, pr.subjects, pr.total, pr.max_marks, pr.percentage, "
         "pr.updated_at, sp.class_level, sp.batch_name, sp.subjects, u.name, u.user_id, "
         "sp.goal, sp.goal_custom, sp.nios_ref "
         "FROM predicted_results pr "
         "JOIN student_profiles sp ON sp.id = pr.student_id "
         "JOIN users u ON u.id = sp.user_id")
    args = {}
    if target in ("pass", "high", "top"):
        q += " WHERE pr.target=:t"
        args["t"] = target
    out = []
    for r in db.execute(_text(q), args).fetchall():
        try:
            ssubs = {str(x).strip().lower() for x in json.loads(r[9] or "[]")}
        except Exception:
            ssubs = set()
        if tsubs and not (tsubs & ssubs):
            continue
        out.append({"student_id": r[0], "target": r[1], "subjects": json.loads(r[2] or "[]"),
                    "total": r[3], "max_marks": r[4], "percentage": r[5], "updated_at": str(r[6] or ""),
                    "class_level": r[7], "batch": r[8],
                    "name": r[10], "user_id": r[11],
                    "goal": r[12] or "", "goal_custom": r[13] or "", "nios_ref": r[14] or ""})
    out.sort(key=lambda x: (-(x["percentage"] or 0), (x["name"] or "").lower()))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return {"results": out}


# ---------------------------------------------------------------------------
# Student ranks - who has covered how much of the syllabus
# ---------------------------------------------------------------------------

def _rank_rows(db):
    """
    Every student ranked by average syllabus coverage across their ready
    subjects (descending), milestone stamps as the tie-breaker. This one list
    feeds the student's own rank card, the teacher view and the admin report.
    """
    from models import StudentProfile, User
    cfg = _cfg(db)
    rows = []
    profiles = db.query(StudentProfile).all()
    users = {u.id: u for u in db.query(User).all()}
    for sp in profiles:
        cl, codes, _un = _student_codes(db, sp)
        tot, n, covered_sum, paper_sum = 0.0, 0, 0.0, 0.0
        for code in codes:
            subj = get_subject(db, cl, code)
            if not subj or subj.get("status") != "ready":
                continue
            sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
            c = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"],
                        cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
            paper = float(c.get("paper_marks") or 0)
            covered = float(c.get("covered_paper") or 0)
            if paper > 0:
                tot += min(100.0, covered / paper * 100.0)
                n += 1
                covered_sum += covered
                paper_sum += paper
        if not n:
            continue
        ms = db.execute(_text(
            "SELECT target, reached_at FROM milestone_dates WHERE student_id=:s ORDER BY reached_at"),
            {"s": sp.id}).fetchall()
        u = users.get(sp.user_id)
        rows.append({
            "student_id": sp.id, "name": (u.name if u else "") or "",
            "user_id": (u.user_id if u else "") or "",
            "batch": getattr(sp, "batch_name", None) or "",
            "class_level": cl,
            "coverage": round(tot / n, 1),
            "covered_marks": round(covered_sum, 1), "paper_marks": round(paper_sum, 1),
            "milestones": len(ms),
            "first_milestone": str(ms[0][1])[:10] if ms else "",
            "pass_done": any(m[0] == "pass" for m in ms),
            "high_done": any(m[0] == "high" for m in ms),
            "top_done": any(m[0] == "top" for m in ms),
            "goal": getattr(sp, "goal", "") or "",
            "goal_custom": getattr(sp, "goal_custom", "") or "",
            "nios_ref": getattr(sp, "nios_ref", "") or "",
        })
    rows.sort(key=lambda r: (-r["coverage"], -r["milestones"], r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


@router.get("/ranks")
def syl_my_rank(db: Session = Depends(get_db), user=Depends(get_student)):
    """The logged-in student's own rank card + the top five, for their
    progress page."""
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    rows = _rank_rows(db)
    mine = next((r for r in rows if r["student_id"] == sp.id), None)
    return {"total": len(rows), "me": mine,
            "top5": [{k: r[k] for k in ("rank", "name", "coverage", "milestones",
                                        "pass_done", "high_done", "top_done")}
                     for r in rows[:5]]}


@router.get("/teacher/ranks")
def syl_teacher_ranks(db: Session = Depends(get_db), user=Depends(get_teacher)):
    """The full rank list, scoped to the teacher's own students."""
    _ensure_syllabus(db)
    from models import TeacherProfile, StudentProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
    tsubs = {str(x).strip().lower() for x in (tp.subjects or []) if str(x).strip()} if tp else set()
    mine_ids = set()
    if tsubs:
        for s in db.query(StudentProfile).all():
            ss = {str(x).strip().lower() for x in (s.subjects or []) if str(x).strip()}
            if tsubs & ss:
                mine_ids.add(s.id)
    rows = [r for r in _rank_rows(db) if not mine_ids or r["student_id"] in mine_ids]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"results": rows}


@router.get("/admin/ranks")
def syl_admin_ranks(db: Session = Depends(get_db), _=Depends(get_admin)):
    _ensure_syllabus(db)
    return {"results": _rank_rows(db)}


@router.get("/strategy")
def syl_strategy(db: Session = Depends(get_db), user=Depends(get_student)):
    """
    Weekly and monthly plan.

    Only chapters needed for the chosen target are planned, never the whole
    syllabus. Every bucket is grouped by subject with its own marks total, and
    the response says whether the target is already covered.
    """
    _ensure_syllabus(db)
    sp = _student_profile(db, user)
    cl, codes, unmapped = _student_codes(db, sp)
    cfg = _cfg(db)
    target = getattr(sp, "study_target", "") or ""
    if target not in ("pass", "high", "top"):
        raise HTTPException(status_code=409,
                            detail="Please choose your target first, then build the plan.")

    info, sess = _exam_dates(db, getattr(sp, "exam_session", "") or "",
                             getattr(sp, "exam_date", "") or "")
    theory_days = info["theory_days"]
    if theory_days is None:
        raise HTTPException(status_code=409,
                            detail="Please choose your exam session first, then build the plan.")
    if theory_days < 1:
        raise HTTPException(status_code=409, detail="That exam date has already passed.")

    weeks = max(math.ceil(theory_days / 7), 1)
    months = max(math.ceil(theory_days / 30), 1)

    queue, per_subject = [], []
    for code in codes:
        subj = get_subject(db, cl, code)
        if not subj or subj.get("status") != "ready":
            continue
        sel, done, tma, pr, choice = _plan_row(db, sp.id, code)
        calc = compute(subj, sel, tma, pr, cfg["high_target"], cfg["buffer_pct"], cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), cfg["top_target"], choice=choice)
        if target == "top":
            need = calc["top_paper_needed"]
            plan_mods = calc["top_plan_modules"]
        elif target == "high":
            need = calc["high_paper_needed"]
            plan_mods = calc["high_plan_modules"]
        else:
            need = max(calc["pass_paper_needed"], calc["theory_paper_needed"])
            plan_mods = calc["pass_plan_modules"]

        # chapters already chosen but not finished, plus the modules the plan says
        pending = [r for r in SD.flatten(subj)
                   if r["kind"] == "PE" and r["no"] in set(sel) and r["no"] not in set(done)]
        merged = {r["no"]: r for r in pending}
        plan_out = []
        for mo in plan_mods:
            chs = []
            for r in mo["chapters"]:
                row = dict(r)
                row["done"] = r["no"] in set(done)
                row["picked"] = r["no"] in set(sel)
                chs.append(row)
                merged.setdefault(r["no"], r)
            plan_out.append({
                "module": mo["module"], "weightage": mo["weightage"],
                "pe_count": mo["pe_count"], "ratio": mo["ratio"],
                "marks": mo["marks"], "new_marks": mo["new_marks"],
                "require": mo.get("require"),
                "done": len([r for r in mo["chapters"] if r["no"] in set(done)]),
                "chapters": chs,
            })
        rows = list(merged.values())
        ratio_of = {r["no"]: mo["ratio"] for mo in plan_mods for r in mo["chapters"]}
        for r in rows:
            queue.append({"subject": subj["name"], "code": subj["code"],
                          "ratio": ratio_of.get(r["no"], 0.0), **r})
        # what the result looks like once the suggested modules are finished
        new_marks = round(sum(mo["new_marks"] for mo in plan_mods), 1)
        projected_after = round(min(calc["covered_paper"] + new_marks, calc["total_pe_marks"])
                                * calc["scale"] + calc["tma_assumed"] + calc["practical_assumed"], 1)
        per_subject.append({
            "code": subj["code"], "name": subj["name"],
            "covered": calc["covered_paper"], "needed": need,
            "total": calc["total_pe_marks"],
            "pending_chapters": len(rows),
            "chapters_left": len(rows),
            "core_needed": calc["top_core_needed"] if target == "top" else (
                calc["high_core_needed"] if target == "high" else max(
                    calc["pass_core_needed"], calc["theory_core_needed"])),
            "core_reached": calc["top_paper_needed"] <= calc["covered_paper"] if target == "top" else (
                calc["high_core_reached"] if target == "high" else (
                    calc["pass_core_reached"] and calc["theory_reached"])),
            "pending_marks": round(sum(r["marks"] for r in rows), 1),
            "done": list(done),
            "reached": calc["top_reached"] if target == "top" else (
                calc["high_reached"] if target == "high" else (
                    calc["pass_reached"] and calc["theory_reached"])),
            "theory_reached": calc["theory_reached"],
            "plan_modules": plan_out,
            "plan_marks": new_marks,
            "projected_after": projected_after,
            "projected_now": calc["projected_total"],
            "display_mode": subj.get("display_mode") or "modules",
        })

    queue.sort(key=lambda r: (-r.get("ratio", 0.0), -r["marks"], r["subject"], r["no"]))

    exam_day = None
    try:
        exam_day = date.fromisoformat(info["theory_date"]) if info.get("theory_date") else None
    except Exception:
        exam_day = None

    def bucket(n, label, span_days):
        buckets = [[] for _ in range(n)]
        for i, item in enumerate(queue):
            buckets[i % n].append(item)
        out = []
        start = date.today()
        for i, b in enumerate(buckets):
            if not b:
                continue
            b_from = start + timedelta(days=i * span_days)
            b_to = start + timedelta(days=(i + 1) * span_days - 1)
            if exam_day and b_from > exam_day:
                break                      # nothing is planned after the exam
            if exam_day and b_to > exam_day:
                b_to = exam_day            # the plan stops at the exam
            by_sub = {}
            for it in b:
                by_sub.setdefault(it["subject"], {"subject": it["subject"], "code": it["code"],
                                                  "items": [], "marks": 0.0})
                by_sub[it["subject"]]["items"].append(it)
                by_sub[it["subject"]]["marks"] = round(
                    by_sub[it["subject"]]["marks"] + it["marks"], 1)
            out.append({
                "label": "%s %d" % (label, i + 1),
                "from": b_from.isoformat(),
                "to": b_to.isoformat(),
                "subjects": sorted(by_sub.values(), key=lambda x: -x["marks"]),
                "chapters": len(b),
                "marks": round(sum(x["marks"] for x in b), 1),
            })
        return out

    return {
        "days_left": theory_days, "exam": info, "weeks": weeks, "months": months,
        "target": target, "high_target": cfg["high_target"], "top_target": cfg["top_target"],
        "bonus_chapters": cfg["bonus_chapters"], "bonus_min_marks": cfg["bonus_min_marks"],
        "total_pending": len(queue),
        "pending_marks": round(sum(r["marks"] for r in queue), 1),
        "per_subject": per_subject,
        "all_reached": all(x["reached"] for x in per_subject) if per_subject else False,
        "weekly": bucket(weeks, "Week", 7),
        "monthly": bucket(months, "Month", 30),
    }


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


@router.post("/admin/parse-weightage")
async def syl_admin_parse_weightage(request: Request,
                                    file: UploadFile = File(None),
                                    _=Depends(get_admin)):
    """
    Read a Weightage by Content table that the admin pasted, either as text in
    the request body or as a screenshot upload.
    """
    import weightage_reader
    if file is not None:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(data) > 12 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image is too large. Maximum size is 12 MB.")
        res = weightage_reader.parse_weightage_image(data)
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        res = weightage_reader.parse_weightage_text(body.get("text") or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Could not read the table."))
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
        "SELECT student_id, subject_code, selected, done, tma_assumed, practical_assumed, updated_at, "
        "option_choice FROM chapter_plans ORDER BY updated_at DESC")).fetchall()
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
            try:
                choice = json.loads(p[7] or "{}") if len(p) > 7 else {}
            except Exception:
                choice = {}
            c = compute(subj, sel, p[4] if p[4] is not None else -1,
                        p[5] if p[5] is not None else -1, cfg["high_target"], cfg["buffer_pct"], cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
            try:
                done_list = json.loads(p[3] or "[]")
            except Exception:
                done_list = []
            subs.append({"code": subj["code"], "name": subj["name"],
                         "selected": len(sel), "done": len(done_list),
                         "covered": c["covered_paper"], "total": c["total_pe_marks"],
                         "pass": c["pass_reached"], "high": c["high_reached"]})
        last_up = max((str(p[6] or "") for p in plans), default="")
        info, _sess = _exam_dates(db, getattr(sp, "exam_session", "") or "",
                                  getattr(sp, "exam_date", "") or "")
        out.append({"student_id": sid, "name": sp.user.name if sp.user else "",
                    "phone": sp.phone, "class_level": cl,
                    "exam_session": getattr(sp, "exam_session", "") or "",
                    "exam_date": getattr(sp, "exam_date", "") or "",
                    "days_left": info["theory_days"],
                    "target": getattr(sp, "study_target", "") or "pass",
                    "last_update": last_up,
                    "subjects": subs})
    out.sort(key=lambda x: x["last_update"], reverse=True)
    return {"items": out}


@router.get("/admin/progress/{student_id}")
def syl_admin_progress_detail(student_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """One student's full tracker report - profile, per-subject plan and coverage."""
    _ensure_syllabus(db)
    sp = db.query(StudentProfile).filter(StudentProfile.id == student_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student not found")
    cfg = _cfg(db)
    cl = str(sp.class_level or class_level_from_name(sp.class_name) or "12")
    plans = db.execute(_text(
        "SELECT subject_code, selected, done, tma_assumed, practical_assumed, updated_at, option_choice "
        "FROM chapter_plans WHERE student_id=:i"), {"i": student_id}).fetchall()
    info, _sess = _exam_dates(db, getattr(sp, "exam_session", "") or "",
                              getattr(sp, "exam_date", "") or "")
    subs = []
    for p in plans:
        subj = get_subject(db, cl, p[0])
        if not subj:
            continue
        try:
            sel = json.loads(p[1] or "[]")
        except Exception:
            sel = []
        try:
            done = json.loads(p[2] or "[]")
        except Exception:
            done = []
        try:
            choice = json.loads(p[6] or "{}") if len(p) > 6 else {}
        except Exception:
            choice = {}
        entry = {"code": subj["code"], "name": subj["name"],
                 "status": subj.get("status", "pending"),
                 "selected": len(sel), "done": len(done),
                 "updated_at": str(p[5] or "")}
        if subj.get("status") == "ready":
            c = compute(subj, sel, p[3] if p[3] is not None else -1,
                        p[4] if p[4] is not None else -1, cfg["high_target"], cfg["buffer_pct"],
                        cfg["bonus_chapters"], cfg["bonus_min_marks"], _stream_for(db, sp), choice=choice)
            entry.update({"covered": c["covered_paper"], "total": c["total_pe_marks"],
                          "pass": c["pass_reached"], "high": c["high_reached"],
                          "projected": c["projected_total"]})
            by_no = {r["no"]: r for r in SD.flatten(subj)}
            entry["done_chapters"] = [
                {"no": n, "title": by_no[n]["title"], "marks": by_no[n]["marks"]}
                for n in done if n in by_no]
            entry["todo_chapters"] = [
                {"no": n, "title": by_no[n]["title"], "marks": by_no[n]["marks"]}
                for n in sel if n in by_no and n not in done]
        subs.append(entry)
    return {"student": {"id": sp.id, "name": sp.user.name if sp.user else "",
                        "phone": sp.phone, "class_level": cl,
                        "class_name": sp.class_name or "",
                        "subjects_enrolled": sp.subjects if isinstance(sp.subjects, list) else [],
                        "exam_session": getattr(sp, "exam_session", "") or "",
                        "exam_date": getattr(sp, "exam_date", "") or "",
                        "days_left": info["theory_days"],
                        "target": getattr(sp, "study_target", "") or "",
                        "high_target": cfg["high_target"]},
            "subjects": subs}


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
