"""
MVS Foundation CRM - NIOS syllabus PDF parser
NIOS syllabus PDF parser.

Reads a NIOS "Syllabus" PDF (the one linked from the Syllabus and Sample
Question Paper page) and pulls out two things:

  1. "Bifurcation of Syllabus" table
        MODULE (No. & name) | TMA (40%) | Public Examination (60%)
     Every lesson is written as  L-3 (Laws of Motion)

  2. "Weightage by Content" table
        Sl. | Module | Marks

The two tables are then merged so that every module carries its exam
weightage, and each lesson is tagged PE or TMA.

Works on text based PDFs. Scanned image PDFs are rejected with a clear
message because there is no text layer to read.
"""

import io
import re
import difflib

try:
    import pdfplumber
    HAVE_PDFPLUMBER = True
except ImportError:
    HAVE_PDFPLUMBER = False


LESSON_RE = re.compile(
    r"L\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212]?\s*(\d{1,2})\s*"
    r"[\(\[]\s*(.+?)\s*[\)\]]",
    re.S,
)
LESSON_LOOSE_RE = re.compile(
    r"(?:L|Lesson)\s*[\-\u2013\u2014]?\s*(\d{1,2})\s*[\.\:\)]?\s*([A-Za-z][^\n]{3,80})"
)
LEAD_NUM_RE = re.compile(r"^\s*\d{1,2}\s*[\.\)]\s*")
MODULE_HINT = re.compile(r"module", re.I)
TMA_HINT = re.compile(r"\bTMA\b", re.I)
PE_HINT = re.compile(r"public\s+exam", re.I)
MARKS_HINT = re.compile(r"^\s*marks?\s*$", re.I)
WEIGHT_HINT = re.compile(r"weightage\s+by\s+content", re.I)
TOTAL_RE = re.compile(r"^\s*total\s*$", re.I)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(s):
    """Join wrapped lines and squeeze whitespace."""
    if s is None:
        return ""
    s = str(s).replace("\u00ad", "")
    # NIOS Devanagari PDFs often carry a broken font encoding that drops NULs
    # and other control bytes where a matra should be. Strip them so the text
    # is at least usable, and warn separately.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _norm(s):
    """Normalise a module name for matching."""
    s = _clean(s).lower()
    s = LEAD_NUM_RE.sub("", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(and|of|the|module|no|name)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _module_name(s):
    return LEAD_NUM_RE.sub("", _clean(s)).strip()


COUNT_MARK_RE = re.compile(r"\(\s*(\d{1,3})\s*(?:\u092a\u093e\u0920|lessons?|\u0932\u0947\u0938\u0928)\s*\)", re.I)
COLON_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[:\.\u0964]\s*(.+?)\s*$")
SKIP_LINE_RE = re.compile(
    r"^\s*[\(\)\-\u2013\u2014_\s]*$"
    r"|no\.?\s*of\s*lessons"
    r"|\u092a\u093e\u0920\u094b\u0902"
    r"|^\s*\(\s*\d{1,3}\s*(?:\u092a\u093e\u0920|lessons?)\s*\)\s*$",
    re.I)


def _stated_count(cell):
    """The '(3 lessons)' style number printed at the bottom of a cell."""
    m = COUNT_MARK_RE.search(_clean(cell))
    return int(m.group(1)) if m else None


def _lessons(cell):
    """
    Pull [(no, title)] out of one table cell.

    Two layouts are supported:
      science style   L-3 (Laws of Motion)
      language style  3 : Gillu          (one lesson per line)
    """
    raw = "" if cell is None else str(cell)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
    txt = _clean(raw)
    if not txt or txt in {"-", "_", "--", "\u2013", "\u2014"}:
        return []

    out = []
    for no, title in LESSON_RE.findall(txt):
        title = _clean(title)
        title = re.sub(r"\s*\.\s*$", "", title)
        if title:
            out.append((int(no), title))

    # language subjects: every line is "<number> : <title>"
    if not out:
        for line in raw.split("\n"):
            line = _clean(line)
            if not line or SKIP_LINE_RE.search(line):
                continue
            m = COLON_LINE_RE.match(line)
            if not m:
                continue
            title = COUNT_MARK_RE.sub("", m.group(2)).strip(" .:\u0964-")
            if title:
                out.append((int(m.group(1)), title))

    if not out:
        for no, title in LESSON_LOOSE_RE.findall(txt):
            t = _clean(title)
            if t and not SKIP_LINE_RE.search(t):
                out.append((int(no), t))

    seen, uniq = set(), []
    for no, title in out:
        if no in seen:
            continue
        seen.add(no)
        uniq.append((no, title))
    return uniq


def _to_float(s):
    m = re.search(r"\d+(?:\.\d+)?", _clean(s))
    return float(m.group()) if m else None


# ---------------------------------------------------------------------------
# table finders
# ---------------------------------------------------------------------------

def _find_bifurcation(tables):
    """Return list of (module_name, tma_cell, pe_cell) from the bifurcation table."""
    best = None
    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        flat = " | ".join(_clean(c) for row in tbl[:4] for c in row if c)
        if not (TMA_HINT.search(flat) and PE_HINT.search(flat)):
            continue
        # locate the column indexes from the header rows
        tma_col = pe_col = mod_col = None
        for row in tbl[:4]:
            for i, c in enumerate(row):
                t = _clean(c)
                if tma_col is None and TMA_HINT.search(t):
                    tma_col = i
                if pe_col is None and PE_HINT.search(t):
                    pe_col = i
                if mod_col is None and MODULE_HINT.search(t):
                    mod_col = i
        if tma_col is None or pe_col is None:
            continue
        if mod_col is None:
            mod_col = 0
        rows, hdr = [], {}
        for row in tbl:
            if max(mod_col, tma_col, pe_col) >= len(row):
                continue
            name = _clean(row[mod_col])
            tma_cell, pe_cell = row[tma_col], row[pe_col]
            # header cell "( No. of lessons ) 8" carries the column total
            for key, cell in (("tma", tma_cell), ("pe", pe_cell)):
                c = _clean(cell)
                if re.search(r"no\.?\s*of\s*lessons", c, re.I):
                    m = re.search(r"no\.?\s*of\s*lessons\s*\)?\s*[:=\-]?\s*(\d{1,3})", c, re.I)
                    if m:
                        hdr[key] = int(m.group(1))
            if not name or MODULE_HINT.search(name) or TOTAL_RE.match(name):
                continue
            # banner rows like "Total No. of Lessons=22" span the table
            if re.search(r"total\s+no\.?\s*of\s*lessons", name, re.I):
                continue
            if not _clean(tma_cell) and not _clean(pe_cell):
                continue
            # a module name has letters in any script, not just Latin
            if not re.search(r"[A-Za-z]{3}|[\u0900-\u097F]{2}", name):
                continue
            rows.append((_module_name(name), tma_cell, pe_cell))
        if rows and (best is None or len(rows) > len(best)):
            best = (rows, hdr)
    return best or ([], {})


def _find_weightage(tables, page_texts):
    """Return list of (module_name, marks) from the Weightage by Content table."""
    best = None
    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        header = " | ".join(_clean(c) for c in (tbl[0] or []) if c)
        has_marks = any(MARKS_HINT.match(_clean(c or "")) for c in (tbl[0] or []))
        if not (has_marks or re.search(r"marks", header, re.I)):
            continue
        if not re.search(r"module|content|unit|chapter", header, re.I):
            continue
        mod_col, mark_col = None, None
        for i, c in enumerate(tbl[0] or []):
            t = _clean(c)
            if mod_col is None and re.search(r"module|content|unit|chapter", t, re.I):
                mod_col = i
            if MARKS_HINT.match(t) or re.fullmatch(r"marks?\b.*", t, re.I):
                mark_col = i
        if mod_col is None or mark_col is None:
            continue
        rows = []
        for row in tbl[1:]:
            if max(mod_col, mark_col) >= len(row):
                continue
            name = _clean(row[mod_col])
            marks = _to_float(row[mark_col])
            if marks is None:
                continue
            if TOTAL_RE.match(name) or not name:
                continue
            rows.append((_module_name(name), marks))
        if rows and (best is None or len(rows) > len(best)):
            best = rows
    if best:
        return best

    # text fallback: read the lines right after "Weightage by Content"
    for txt in page_texts:
        m = WEIGHT_HINT.search(txt or "")
        if not m:
            continue
        chunk = txt[m.end(): m.end() + 1400]
        rows = []
        for line in chunk.split("\n"):
            line = _clean(line)
            mm = re.match(r"^\d{1,2}[\.\)]\s+(.+?)\s+(\d{1,3})$", line)
            if mm and not TOTAL_RE.match(mm.group(1)):
                rows.append((_module_name(mm.group(1)), float(mm.group(2))))
        if rows:
            return rows
    return []


def _match_weightage(modules, weights):
    """Attach marks to modules. Fuzzy match by name, positional fallback."""
    notes = []
    if not weights:
        notes.append("Weightage by Content table not found. All module marks set to 0, please fill them manually.")
        return [0.0] * len(modules), notes

    used = set()
    result = [None] * len(modules)
    for i, mod in enumerate(modules):
        target = _norm(mod)
        best_j, best_score = None, 0.0
        for j, (wname, _) in enumerate(weights):
            if j in used:
                continue
            score = difflib.SequenceMatcher(None, target, _norm(wname)).ratio()
            if score > best_score:
                best_score, best_j = score, j
        if best_j is not None and best_score >= 0.55:
            result[i] = weights[best_j][1]
            used.add(best_j)

    missing = [i for i, v in enumerate(result) if v is None]
    if missing and len(modules) == len(weights):
        for i in missing:
            if i not in used:
                result[i] = weights[i][1]
        notes.append("Some module names did not match exactly, they were matched by order instead. Please check the marks column.")
    for i, v in enumerate(result):
        if v is None:
            result[i] = 0.0
            notes.append("No weightage found for module: " + modules[i])
    return result, notes


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def parse_syllabus_pdf(data: bytes):
    """
    Parse a NIOS syllabus PDF.

    Returns dict:
        ok            bool
        error         str (when ok is False)
        modules       [{module, weightage, lessons:[{no,title,kind}]}]
        text          the chapter block in the admin editor format
        paper_marks   sum of module weightage
        stats         {total, pe, tma}
        warnings      [str]
    """
    if not HAVE_PDFPLUMBER:
        return {"ok": False, "error": "PDF reader is not installed on the server. Add pdfplumber to requirements.txt and redeploy."}

    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        return {"ok": False, "error": "This file could not be opened as a PDF. " + str(exc)[:150]}

    tables, page_texts = [], []
    try:
        for page in pdf.pages[:40]:
            page_texts.append(page.extract_text() or "")
            for t in (page.extract_tables() or []):
                tables.append(t)
            # some NIOS PDFs draw tables without ruling lines
            try:
                for t in (page.extract_tables({"vertical_strategy": "text",
                                               "horizontal_strategy": "text"}) or []):
                    tables.append(t)
            except Exception:
                pass
    finally:
        pdf.close()

    joined = "\n".join(page_texts).strip()
    if len(joined) < 60:
        return {"ok": False, "error": "No readable text found. This looks like a scanned PDF. Please download the text version from the NIOS website."}

    rows, hdr_counts = _find_bifurcation(tables)
    if not rows:
        return {"ok": False,
                "error": "Bifurcation of Syllabus table could not be located in this PDF. Make sure you uploaded the syllabus PDF and not the sample question paper."}

    module_names = [r[0] for r in rows]
    weights, warnings = _match_weightage(module_names, _find_weightage(tables, page_texts))

    modules, seen_no = [], set()
    dup, empty_mods, count_mismatch = [], [], []
    for (name, tma_cell, pe_cell), w in zip(rows, weights):
        lessons = []
        t_l = _lessons(tma_cell)
        p_l = _lessons(pe_cell)
        for no, title in t_l:
            lessons.append({"no": no, "title": title, "kind": "TMA"})
        for no, title in p_l:
            lessons.append({"no": no, "title": title, "kind": "PE"})
        # the PDF prints "(3 lessons)" at the bottom of each cell - use it
        for label, cell, got in (("TMA", tma_cell, len(t_l)), ("Public Examination", pe_cell, len(p_l))):
            want = _stated_count(cell)
            if want is not None and want != got:
                count_mismatch.append("%s, %s column: PDF says %d lessons but %d were read."
                                      % (name, label, want, got))
        if not lessons:
            empty_mods.append(name)
            continue
        lessons.sort(key=lambda x: x["no"])
        for l in lessons:
            if l["no"] in seen_no:
                dup.append("L-%d" % l["no"])
            seen_no.add(l["no"])
        modules.append({
            "module": name,
            "weightage": float(w),
            "lessons": [{"no": "L-%d" % l["no"], "title": l["title"], "kind": l["kind"]} for l in lessons],
        })

    all_lessons = [l for m in modules for l in m["lessons"]]
    pe = len([l for l in all_lessons if l["kind"] == "PE"])
    tma = len([l for l in all_lessons if l["kind"] == "TMA"])

    if dup:
        warnings.append("These lesson numbers appeared more than once: " + ", ".join(sorted(set(dup))))
    warnings.extend(count_mismatch)
    if empty_mods:
        warnings.append("These modules have no separate lessons in the PDF and were skipped: "
                        + ", ".join(empty_mods))
    if re.search(r"[\u0900-\u097F]", joined) and re.search(r"[\x00-\x08\x0e-\x1f]", "".join(page_texts)):
        warnings.append("This PDF uses a broken Devanagari font encoding, so some Hindi titles may be "
                        "missing matras. Please read through the chapter list and correct the spellings.")
    if not all_lessons:
        return {"ok": False, "error": "The bifurcation table was found but no lessons could be read from it. Please add the chapters manually."}

    totals, splits = [], []
    for m in re.finditer(r"(total\s+)?no\.?\s*of\s*lessons\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212:=]?\s*(\d{1,3})",
                         joined, re.I):
        (totals if m.group(1) else splits).append(int(m.group(2)))
    if totals and totals[0] != len(all_lessons):
        warnings.append("The PDF says %d lessons in total but %d were read. Please check the chapter list below."
                        % (totals[0], len(all_lessons)))
    if len(splits) >= 2:
        if splits[0] != tma:
            warnings.append("The PDF lists %d TMA lessons but %d were read." % (splits[0], tma))
        if splits[1] != pe:
            warnings.append("The PDF lists %d Public Examination lessons but %d were read." % (splits[1], pe))

    paper = round(sum(m["weightage"] for m in modules), 2)
    if paper and paper not in (30, 40, 60, 70, 80, 85, 100):
        warnings.append("Module marks add up to %s. NIOS question papers are usually 30, 40, 60, 70, 80, 85 or 100 marks. Please verify." % paper)

    expected = {}
    if totals:
        expected["total"] = totals[0]
    if len(splits) >= 2:
        expected["tma"] = splits[0]
        expected["pe"] = splits[1]
    # header cells of the bifurcation table are the most reliable source
    if hdr_counts.get("tma") is not None:
        expected["tma"] = hdr_counts["tma"]
    if hdr_counts.get("pe") is not None:
        expected["pe"] = hdr_counts["pe"]
    if "total" not in expected:
        m = re.search(r"total\s+no\.?\s*of\s*lessons\s*[=:\-\u2013]?\s*(\d{1,3})", joined, re.I)
        if m:
            expected["total"] = int(m.group(1))
    if not expected:
        warnings.append("The lesson count line could not be read from this PDF. "
                        "Enter the totals printed on it manually so the chapter tags can be verified.")

    text = "\n\n".join(
        "# %s | %s\n%s" % (
            m["module"],
            int(m["weightage"]) if float(m["weightage"]).is_integer() else m["weightage"],
            "\n".join("%s | %s | %s" % (l["no"], l["title"], l["kind"]) for l in m["lessons"]),
        )
        for m in modules
    )

    return {
        "ok": True,
        "modules": modules,
        "text": text,
        "paper_marks": paper,
        "expected": expected,
        "stats": {"total": len(all_lessons), "pe": pe, "tma": tma, "modules": len(modules)},
        "warnings": warnings,
    }


def suggest_marks(paper_marks, has_practical, practical_max=0):
    """Build a marks structure from the detected paper total."""
    p = float(paper_marks or 0)
    theory = round(p * 0.8)
    total_100 = 100
    tma = round(theory / 0.8 * 0.2) if p else 0
    pr = float(practical_max or 0)
    if has_practical and not pr:
        pr = max(total_100 - theory - tma, 0)
    if not has_practical:
        pr = 0
        tma = total_100 - theory
    return {
        "theory_max": theory,
        "practical_max": pr,
        "tma_max": max(total_100 - theory - pr, 0),
        "paper_marks": p,
        "theory_pass": round(theory * 0.33) if not has_practical else round(theory * 0.33),
        "practical_pass": round(pr * 0.33) if has_practical else 0,
        "combined_pass": 0,
        "aggregate_pass": 33,
        "has_practical": bool(has_practical),
    }
