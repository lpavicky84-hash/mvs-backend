"""
MVS Foundation - NIOS Study Tracker
Syllabus / Marks / Weightage master data.

MARKS MODEL (Stream-1, TMA applicable)
--------------------------------------
Every NIOS question paper is set for `paper_marks`.
For Stream-1 learners the theory paper is scaled down to 80% and the
remaining 20% is given as Internal Assessment (TMA).

    theory_max = 0.8 * paper_marks
    tma_max    = 0.2 * (theory_max + tma_max)

Passing criteria are taken from NIOS Notification 34/2021 (Annexure).

Sr. Secondary
  Without practical      Th 80  Pr  -   IA 20   Pass: Th 26,  Agg 33
  With practical (Sc.)   Th 64  Pr 20   IA 16   Pass: Th 21, Pr 07, Agg 33
  Painting (332)         Th 24  Pr 70   IA 06   Pass: Th 08, Pr 23, Agg 33
  Data Entry Op (336)    Th 32  Pr 60   IA 08   Pass: Th 11, Pr 20, Agg 33
  Computer Science (330) Th 48  Pr 40   IA 12   Pass: Th 16, Pr 13, Agg 33
  Phy Edu & Yoga (373)   Th 56  Pr 30   IA 14   Pass: Th 19, Pr 10, Agg 33

Secondary
  Without practical      Th 80  Pr  -   IA 20   Pass: Th 26,  Agg 33
  Maths/Sc&Tech/Home Sc  Th 68  Pr 15   IA 17   Pass: Th+Pr 27 together, Agg 33
  Painting (225)         Th 24  Pr 70   IA 06   Pass: Th+Pr 31 together, Agg 33
  Data Entry Op (229)    Th 32  Pr 60   IA 08   Pass: Th+Pr 30 together, Agg 33
  Music (242 / 243)      Th 32  Pr 60   IA 08   Pass: Th+Pr 30 together, Agg 33

CHAPTER MODEL
-------------
Each subject holds modules. Each module has `weightage` (marks of the
question paper) and a list of lessons. Every lesson is tagged either
"PE"  -> asked in the Public Examination
"TMA" -> Tutor Marked Assignment only, never asked in the exam

Module weightage is distributed equally across that module's PE lessons
unless a lesson carries an explicit `marks` value.
"""

# ---------------------------------------------------------------------------
# Marks templates
# ---------------------------------------------------------------------------

TPL = {
    # --------------------------- Sr. Secondary ---------------------------
    "SR_THEORY": {
        "theory_max": 80, "practical_max": 0, "tma_max": 20,
        "theory_pass": 26, "practical_pass": 0, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 100, "has_practical": False,
    },
    "SR_PRACTICAL": {
        "theory_max": 64, "practical_max": 20, "tma_max": 16,
        "theory_pass": 21, "practical_pass": 7, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 80, "has_practical": True,
    },
    "SR_PAINTING": {
        "theory_max": 24, "practical_max": 70, "tma_max": 6,
        "theory_pass": 8, "practical_pass": 23, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 30, "has_practical": True,
    },
    "SR_DEO": {
        "theory_max": 32, "practical_max": 60, "tma_max": 8,
        "theory_pass": 11, "practical_pass": 20, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 40, "has_practical": True,
    },
    "SR_COMPSC": {
        "theory_max": 48, "practical_max": 40, "tma_max": 12,
        "theory_pass": 16, "practical_pass": 13, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 60, "has_practical": True,
    },
    "SR_PHYEDU": {
        "theory_max": 56, "practical_max": 30, "tma_max": 14,
        "theory_pass": 19, "practical_pass": 10, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 70, "has_practical": True,
    },
    # ----------------------------- Secondary -----------------------------
    "SEC_THEORY": {
        "theory_max": 80, "practical_max": 0, "tma_max": 20,
        "theory_pass": 26, "practical_pass": 0, "combined_pass": 0,
        "aggregate_pass": 33, "paper_marks": 100, "has_practical": False,
    },
    "SEC_PRACTICAL": {
        "theory_max": 68, "practical_max": 15, "tma_max": 17,
        "theory_pass": 0, "practical_pass": 0, "combined_pass": 27,
        "aggregate_pass": 33, "paper_marks": 85, "has_practical": True,
    },
    "SEC_PAINTING": {
        "theory_max": 24, "practical_max": 70, "tma_max": 6,
        "theory_pass": 0, "practical_pass": 0, "combined_pass": 31,
        "aggregate_pass": 33, "paper_marks": 30, "has_practical": True,
    },
    "SEC_DEO": {
        "theory_max": 32, "practical_max": 60, "tma_max": 8,
        "theory_pass": 0, "practical_pass": 0, "combined_pass": 30,
        "aggregate_pass": 33, "paper_marks": 40, "has_practical": True,
    },
    "SEC_MUSIC": {
        "theory_max": 32, "practical_max": 60, "tma_max": 8,
        "theory_pass": 0, "practical_pass": 0, "combined_pass": 30,
        "aggregate_pass": 33, "paper_marks": 40, "has_practical": True,
    },
}


def _m(name, weightage, lessons):
    """Build a module dict. lessons = list of (lesson_no, title, kind)."""
    return {
        "module": name,
        "weightage": weightage,
        "lessons": [
            {"no": n, "title": t, "kind": k}
            for (n, t, k) in lessons
        ],
    }


# ---------------------------------------------------------------------------
# Full syllabus data (verified subjects)
# ---------------------------------------------------------------------------

PHYSICS_312 = [
    _m("Motion, Force and Energy", 14, [
        ("L-1", "Units, Dimensions and Vectors", "TMA"),
        ("L-2", "Motion in a Straight Line", "TMA"),
        ("L-3", "Laws of Motion", "PE"),
        ("L-4", "Motion in a Plane", "TMA"),
        ("L-5", "Gravitation", "TMA"),
        ("L-6", "Work, Energy and Power", "PE"),
        ("L-7", "Motion of a Rigid Body", "TMA"),
    ]),
    _m("Mechanics of Solids and Fluids", 6, [
        ("L-8", "Elastic Properties of Solids", "TMA"),
        ("L-9", "Properties of Fluids", "PE"),
    ]),
    _m("Thermal Physics", 6, [
        ("L-10", "Kinetic Theory of Gases", "TMA"),
        ("L-11", "Thermodynamics", "PE"),
        ("L-12", "Heat Transfer and Solar Energy", "TMA"),
    ]),
    _m("Oscillations and Waves", 6, [
        ("L-13", "Simple Harmonic Motion", "TMA"),
        ("L-14", "Wave Phenomena", "PE"),
    ]),
    _m("Electricity and Magnetism", 16, [
        ("L-15", "Electric Charge and Electric Field", "PE"),
        ("L-16", "Electric Potential and Capacitors", "PE"),
        ("L-17", "Electric Current", "PE"),
        ("L-18", "Magnetism and Magnetic Effect of Electric Current", "PE"),
        ("L-19", "Electromagnetic Induction and Alternating Current", "PE"),
    ]),
    _m("Optics and Optical Instruments", 14, [
        ("L-20", "Reflection and Refraction of Light", "TMA"),
        ("L-21", "Dispersion and Scattering of Light", "PE"),
        ("L-22", "Wave Phenomena and Light", "PE"),
        ("L-23", "Optical Instruments", "PE"),
    ]),
    _m("Atoms and Nuclei", 8, [
        ("L-24", "Structure of Atom", "PE"),
        ("L-25", "Dual Nature of Radiation and Matter", "PE"),
        ("L-26", "Nuclei and Radioactivity", "PE"),
        ("L-27", "Nuclear Fission and Fusion", "PE"),
    ]),
    _m("Semiconductors and their Applications", 10, [
        ("L-28", "Semiconductors and Semiconducting Devices", "PE"),
        ("L-29", "Applications of Semiconductor Devices", "PE"),
        ("L-30", "Communication Systems", "TMA"),
    ]),
]

# Counts printed on the NIOS Physics 312 syllabus PDF itself.
# The validator below refuses to publish the subject unless the loaded
# chapters reconcile with these numbers exactly.
PHYSICS_312_EXPECTED = {"total": 30, "tma": 12, "pe": 18}


# ---------------------------------------------------------------------------
# Subject registry
# ---------------------------------------------------------------------------
# status: "ready"   -> chapters loaded, student can use the tracker
#         "pending" -> marks structure ready, chapters to be loaded from admin

def _s(code, name, template, status="pending", modules=None, stream="both", expected=None):
    return {
        "code": code,
        "name": name,
        "template": template,
        "marks": dict(TPL[template]),
        "status": status,
        "modules": modules or [],
        "expected": expected or {},
    }


SUBJECTS = {
    "10": [
        _s("201", "Hindi", "SEC_THEORY"),
        _s("202", "English", "SEC_THEORY"),
        _s("206", "Urdu", "SEC_THEORY"),
        _s("209", "Sanskrit", "SEC_THEORY"),
        _s("211", "Mathematics", "SEC_PRACTICAL"),
        _s("212", "Science and Technology", "SEC_PRACTICAL"),
        _s("213", "Social Science", "SEC_THEORY"),
        _s("214", "Economics", "SEC_THEORY"),
        _s("215", "Business Studies", "SEC_THEORY"),
        _s("216", "Home Science", "SEC_PRACTICAL"),
        _s("222", "Psychology", "SEC_THEORY"),
        _s("223", "Indian Culture and Heritage", "SEC_THEORY"),
        _s("224", "Accountancy", "SEC_THEORY"),
        _s("225", "Painting", "SEC_PAINTING"),
        _s("229", "Data Entry Operations", "SEC_DEO"),
        _s("235", "Arabic", "SEC_THEORY"),
        _s("236", "Persian", "SEC_THEORY"),
        _s("238", "Sindhi", "SEC_THEORY"),
        _s("242", "Hindustani Music", "SEC_MUSIC"),
        _s("243", "Carnatic Music", "SEC_MUSIC"),
        _s("245", "Veda Adhyayan", "SEC_THEORY"),
        _s("246", "Sanskrit Vyakarana", "SEC_THEORY"),
        _s("247", "Bharatiya Darshan", "SEC_THEORY"),
    ],
    "12": [
        _s("301", "Hindi", "SR_THEORY"),
        _s("302", "English", "SR_THEORY"),
        _s("303", "Bengali", "SR_THEORY"),
        _s("304", "Tamil", "SR_THEORY"),
        _s("305", "Odia", "SR_THEORY"),
        _s("306", "Urdu", "SR_THEORY"),
        _s("309", "Sanskrit", "SR_THEORY"),
        _s("310", "Punjabi", "SR_THEORY"),
        _s("311", "Mathematics", "SR_THEORY"),
        _s("312", "Physics", "SR_PRACTICAL", status="ready", modules=PHYSICS_312,
           expected=PHYSICS_312_EXPECTED),
        _s("313", "Chemistry", "SR_PRACTICAL"),
        _s("314", "Biology", "SR_PRACTICAL"),
        _s("315", "History", "SR_THEORY"),
        _s("316", "Geography", "SR_PRACTICAL"),
        _s("317", "Political Science", "SR_THEORY"),
        _s("318", "Economics", "SR_THEORY"),
        _s("319", "Business Studies", "SR_THEORY"),
        _s("320", "Accountancy", "SR_THEORY"),
        _s("321", "Home Science", "SR_PRACTICAL"),
        _s("328", "Psychology", "SR_THEORY"),
        _s("330", "Computer Science", "SR_COMPSC"),
        _s("331", "Sociology", "SR_THEORY"),
        _s("332", "Painting", "SR_PAINTING"),
        _s("333", "Environmental Science", "SR_PRACTICAL"),
        _s("335", "Mass Communication", "SR_PRACTICAL"),
        _s("336", "Data Entry Operations", "SR_DEO"),
        _s("338", "Introduction to Law", "SR_THEORY"),
        _s("339", "Library and Information Science", "SR_PRACTICAL"),
        _s("373", "Physical Education and Yoga", "SR_PHYEDU"),
    ],
}


# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------

def chapter_weightage(modules):
    """Return {lesson_no: marks} for PE lessons, module weightage split equally."""
    out = {}
    for mod in modules:
        pe = [l for l in mod["lessons"] if l["kind"] == "PE"]
        if not pe:
            continue
        explicit = sum(l.get("marks", 0) or 0 for l in pe)
        remaining = max(float(mod["weightage"]) - explicit, 0.0)
        auto = [l for l in pe if not l.get("marks")]
        shares = {}
        if auto:
            base = round(remaining / len(auto), 2)
            shares = {l["no"]: base for l in auto}
            # push the rounding remainder onto the first chapter so the module
            # marks add up exactly, otherwise totals drift (14/3 -> 14.01)
            drift = round(remaining - base * len(auto), 2)
            if drift:
                shares[auto[0]["no"]] = round(base + drift, 2)
        for l in pe:
            out[l["no"]] = float(l.get("marks") or shares.get(l["no"], 0))
    return out


def flatten(subject):
    """Return list of PE lesson dicts with marks + module name."""
    w = chapter_weightage(subject.get("modules", []))
    rows = []
    for mod in subject.get("modules", []):
        for l in mod["lessons"]:
            rows.append({
                "no": l["no"],
                "title": l["title"],
                "kind": l["kind"],
                "module": mod["module"],
                "module_weightage": mod["weightage"],
                "marks": w.get(l["no"], 0.0) if l["kind"] == "PE" else 0.0,
            })
    return rows


def find_subject(class_level, code):
    for s in SUBJECTS.get(str(class_level), []):
        if s["code"] == str(code):
            return s
    return None


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
# A subject is only published to students when every check below passes.
# This exists because a wrong PE / TMA tag is the single most damaging bug in
# this product: a student skips a chapter that is actually in the exam.
# Guessing is not allowed. Data that does not reconcile is held back.

def validate_subject(subject):
    """Return (status, issues). status is ready / needs_review / pending."""
    issues = []
    modules = subject.get("modules") or []
    if not modules or not any(m.get("lessons") for m in modules):
        return "pending", []

    rows = flatten(subject)
    pe = [r for r in rows if r["kind"] == "PE"]
    tma = [r for r in rows if r["kind"] == "TMA"]
    exp = subject.get("expected") or {}

    # 1. counts printed on the syllabus PDF must match exactly
    if exp.get("total") and exp["total"] != len(rows):
        issues.append("Syllabus PDF says %d lessons in total but %d are loaded."
                      % (exp["total"], len(rows)))
    if exp.get("tma") is not None and exp["tma"] != len(tma):
        issues.append("Syllabus PDF says %d TMA lessons but %d are tagged TMA."
                      % (exp["tma"], len(tma)))
    if exp.get("pe") is not None and exp["pe"] != len(pe):
        issues.append("Syllabus PDF says %d Public Examination lessons but %d are tagged PE."
                      % (exp["pe"], len(pe)))
    if not exp:
        issues.append("Expected lesson counts are not set. Import the syllabus PDF, "
                      "or enter the totals printed on it, so the chapter tags can be verified.")

    # 2. module weightage must add up to the question paper total
    paper = float(subject.get("marks", {}).get("paper_marks") or 0)
    wsum = round(sum(float(m.get("weightage") or 0) for m in modules), 2)
    if paper and abs(wsum - paper) > 0.51:
        issues.append("Module weightage adds up to %s but the question paper is %s marks."
                      % (wsum, paper))

    # 3. a module that carries marks must have at least one PE chapter
    for m in modules:
        pe_l = [l for l in m["lessons"] if l["kind"] == "PE"]
        w = float(m.get("weightage") or 0)
        if w > 0 and not pe_l:
            issues.append("Module '%s' carries %s marks but has no Public Examination chapter."
                          % (m["module"], m["weightage"]))
        # 3b. hand entered chapter marks must fit inside the module weightage
        fixed = sum(float(l.get("marks") or 0) for l in pe_l)
        if fixed > w + 0.01:
            issues.append("Module '%s': the marks set by hand add up to %s, which is more than the "
                          "module weightage of %s." % (m["module"], round(fixed, 2), round(w, 2)))
        elif fixed > 0 and len([l for l in pe_l if not l.get("marks")]) == 0 and abs(fixed - w) > 0.01:
            issues.append("Module '%s': every chapter has a fixed mark and they add up to %s, "
                          "but the module weightage is %s." % (m["module"], round(fixed, 2), round(w, 2)))

    # 4. no duplicate or missing lesson numbers
    nums = [r["no"] for r in rows]
    dup = sorted({n for n in nums if nums.count(n) > 1})
    if dup:
        issues.append("Duplicate lesson numbers: " + ", ".join(dup))

    # 5. every chapter needs a real title
    for r in rows:
        if len(r["title"].strip()) < 3:
            issues.append("Chapter %s has no proper title." % r["no"])

    if not pe:
        issues.append("No Public Examination chapter found. Students would have nothing to study.")

    return ("needs_review" if issues else "ready"), issues


# ---------------------------------------------------------------------------
# Canonical chapter master
# ---------------------------------------------------------------------------
# The timetable, class reports and any other module must resolve a free text
# chapter name against this master instead of creating new rows on their own.
# That is what stops grammar sub-topics and random text from becoming chapters.

import re as _re
import difflib as _difflib

_STOP = {"the", "of", "and", "a", "an", "in", "to", "on", "for", "its", "with",
         "chapter", "lesson", "unit", "topic", "part", "ch", "l", "no"}

_NOT_A_CHAPTER = {
    "revision", "revison", "doubt", "doubts", "test", "tests", "practice",
    "pyq", "dpp", "assignment", "tma", "holiday", "break", "orientation",
    "introduction", "intro", "syllabus", "discussion", "extra", "extra class",
    "backlog", "misc", "miscellaneous", "general", "na", "n/a", "tbd",
}


def _tokens(s):
    s = _re.sub(r"\b(l|lesson|ch|chapter)\s*[-\u2013]?\s*\d+\b", " ", str(s or "").lower())
    s = _re.sub(r"[^a-z0-9\s]", " ", s)
    return [t for t in s.split() if t and t not in _STOP and len(t) > 1]


def _lesson_no(s):
    m = _re.search(r"\b(?:l|lesson|ch|chapter)\s*[-\u2013]?\s*(\d{1,2})\b", str(s or ""), _re.I)
    return "L-%d" % int(m.group(1)) if m else None


def chapter_master(subject):
    """Canonical chapter list for one subject, PE chapters first."""
    rows = flatten(subject)
    return [{
        "no": r["no"], "title": r["title"], "kind": r["kind"],
        "module": r["module"], "marks": r["marks"],
        "key": "%s::%s" % (subject["code"], r["no"]),
    } for r in rows]


def resolve_chapter(subject, text, accept=0.62, review=0.42):
    """
    Match a free text chapter name against the subject's canonical master.

    Returns dict with:
        action    accept | review | reject
        chapter   matched canonical chapter, or None
        score     0..1
        candidates top 3 alternatives
        reason    why it was rejected
    """
    raw = str(text or "").strip()
    master = chapter_master(subject)
    blank = {"action": "reject", "chapter": None, "score": 0.0,
             "candidates": [], "reason": "", "input": raw}

    if not raw:
        blank["reason"] = "Empty chapter name."
        return blank
    if not master:
        blank["reason"] = "This subject has no verified chapter list yet."
        return blank

    low = _re.sub(r"[^a-z0-9 ]", " ", raw.lower()).strip()
    low = _re.sub(r"\s+", " ", low)
    if low in _NOT_A_CHAPTER:
        blank["reason"] = "This is an activity, not a syllabus chapter."
        return blank

    # exact lesson number wins outright
    ln = _lesson_no(raw)
    if ln:
        for c in master:
            if c["no"] == ln:
                return {"action": "accept", "chapter": c, "score": 1.0,
                        "candidates": [], "reason": "Matched by lesson number.", "input": raw}

    toks = _tokens(raw)
    if not toks:
        blank["reason"] = "No usable words in this chapter name."
        return blank

    scored = []
    for c in master:
        ct = _tokens(c["title"])
        if not ct:
            continue
        ratio = _difflib.SequenceMatcher(None, " ".join(toks), " ".join(ct)).ratio()
        overlap = len(set(toks) & set(ct)) / max(len(set(toks) | set(ct)), 1)
        scored.append((round(0.55 * ratio + 0.45 * overlap, 4), c))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        blank["reason"] = "No chapter to compare against."
        return blank

    top, cand = scored[0]
    others = [{"no": c["no"], "title": c["title"], "kind": c["kind"], "score": s}
              for s, c in scored[1:4]]

    # a clear winner needs to beat the runner up, otherwise a human should look
    gap = top - (scored[1][0] if len(scored) > 1 else 0)
    if top >= accept and gap >= 0.06:
        return {"action": "accept", "chapter": cand, "score": top,
                "candidates": others, "reason": "", "input": raw}
    if top >= review:
        return {"action": "review", "chapter": cand, "score": top,
                "candidates": others,
                "reason": "Close to more than one chapter. Please confirm the correct one.",
                "input": raw}
    return {"action": "reject", "chapter": None, "score": top, "candidates": others,
            "reason": "Does not match any chapter in the verified syllabus.", "input": raw}


# NIOS runs the practical examination a few weeks before the theory papers.
# theory_date is the first day of the theory schedule, practical_date is the
# first day of the practical schedule. Both are editable from Admin settings.
# NIOS does not publish the exact date sheet months in advance, so every date
# here is an expectation, not a confirmation. The labels below are what the
# learner sees, the dates are only used to count the days.
EXAM_SESSIONS = [
    # tma: True  -> Stream 1, the learner gets an assignment share of the paper
    # tma: False -> no assignment, the whole written paper counts as theory
    {"id": "oct2026", "label": "October 2026 Public Examination",
     "theory_date": "2026-10-05", "practical_date": "2026-09-16", "tma": True,
     "theory_label": "First week of October 2026",
     "practical_label": "Around 16 September 2026"},
    {"id": "apr2027", "label": "April 2027 Public Examination",
     "theory_date": "2027-04-02", "practical_date": "2027-03-15", "tma": True,
     "theory_label": "First week of April 2027",
     "practical_label": "Around 15 March 2027"},
    {"id": "stream2", "label": "Stream 2 Examination",
     "theory_date": "2026-10-05", "practical_date": "2026-09-16", "tma": False,
     "theory_label": "First week of October 2026",
     "practical_label": "Around 16 September 2026"},
    {"id": "ondemand", "label": "On Demand Examination",
     "theory_date": "", "practical_date": "", "ask_date": True, "tma": False,
     "theory_label": "", "practical_label": "About three weeks before your paper"},
]
