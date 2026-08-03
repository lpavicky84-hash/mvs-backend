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
    r"(?:Lesson|Chapter|Ch|L)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]?\s*(\d{1,2})\s*\.?\s*"
    r"[\(\[]\s*(.+?)\s*[\)\]]",
    re.S | re.I,
)
LESSON_LOOSE_RE = re.compile(
    r"(?:L|Lesson|Ch|Chapter|\u092a\u093e\u0920)\s*[\-\u2013\u2014]?\s*(\d{1,2})\s*[\.\:\)\u2013]?\s*"
    r"([A-Za-z\u0900-\u097F][^\n]{2,90})", re.I
)
LEAD_NUM_RE = re.compile(
    r"^\s*(?:module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2}\s*[AB]?)\b[\.\):\-\u2013]?\s*"
    r"|\d{1,2}\s*[AB]?\s*[\.\)\-\u2013:]\s*"
    r"|[IVXLC]+\s*[AB]?\s*[\.\):\-\u2013]\s*)", re.I)

# A cell that begins a new module: "Module I", "Module-2", "1.", "1)", "6A.",
# "8A- Water ...", or a bare roman numeral row: "I : CONCEPT OF LAW", "VII B- ..."
MODULE_START_RE = re.compile(
    r"^\s*(?:module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2}\s*[AB]?)\b"
    r"|\d{1,2}\s*[AB]?\s*[\.\)\-\u2013:]\s*\S"
    r"|[IVXLC]+\s*[AB]?\s*[\.\):\-\u2013](?:\s*\S|\s*$))", re.I)

# A row that is part of the table heading, never a module
HEADER_CELL_RE = re.compile(
    r"no\.?\s*&\s*name|TMA\s*\(|public\s+exam|no\.?\s*of\s*lessons"
    r"|^\s*module\s*$|total\s+no\.?\s*of|^\s*(?:I|II|III)\s*$"
    r"|\u092a\u093e\u0920\u094b\u0902\s*\u0915\u0940", re.I)
MODULE_HINT = re.compile(r"module", re.I)
# NIOS language syllabus PDFs carry no English header at all, so match the
# Devanagari wording and the 40% / 60% split that every one of them prints.
TMA_HINT = re.compile(
    r"\bTMA\b|\u091f\u0940\u090f\u092e\u090f|\u092e\u0942\u0932\u094d\u092f\u093e\u0902\u0915\u0928"
    r"|\u0905\u0902\u0915\u093f\u0924|40\s*%", re.I)
PE_HINT = re.compile(
    r"public\s+exam|\u0938\u093e\u0930\u094d\u0935\u091c\u0928\u093f\u0915"
    r"|\u092a\u0930\u0940\u0915\u094d\u0937\u093e|60\s*%", re.I)
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
    s = re.sub(r"\(cid\s*:\s*\d+\)", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    # a word broken across lines at a hyphen joins back: "pre- historic" -> "pre-historic"
    s = re.sub(r"(\w)-\s+([a-z])", r"\1-\2", s)
    return s.strip()


def _norm(s):
    """Normalise a module name for matching."""
    s = _clean(s).lower()
    s = LEAD_NUM_RE.sub("", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(and|of|the|module|no|name)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _module_name(s):
    """Strip leading numbering/module prefixes, repeatedly ('1.Module-II: X' -> 'X')."""
    s = _clean(s)
    while True:
        t = LEAD_NUM_RE.sub("", s).strip()
        if t == s:
            break
        s = t
    # "(04 Lessons)" / "(8lessons)" printed as part of the module name
    s = COUNT_MARK_RE.sub("", s)
    # option prefix glued to the front: "(A) Management of Libraries"
    s = re.sub(r"^\s*\([ABab]\)\s*", "", s)
    # "OPTIONAL MODULE" heading glued to the last core module's name
    s = re.sub(r"\s+optional\s+module\s*$", "", s, flags=re.I)
    # dangling connector between the two options: "... Tour Operation Business OR"
    s = re.sub(r"(?:\s+OR)+\s*$", "", s, flags=re.I)
    # punctuation left behind by the numbering strip: "- Hospitality Management"
    s = re.sub(r"^[\-\u2013\u2014:\.\s]+", "", s)
    # frequent NIOS print typo: "2oth Century" for "20th Century"
    s = re.sub(r"\b2oth\b", "20th", s, flags=re.I)
    return s


# "(12 Marks)" printed inside a module name (NIOS bifurcation sheets do this)
MARKS_NAME_RE = re.compile(r"\(\s*(\d{1,3})\s*marks?\s*\)", re.I)


def _split_name_marks(name):
    """-> (clean_name, marks or None) for names like 'Module-II Ecology (26 Marks)'."""
    m = MARKS_NAME_RE.search(name or "")
    marks = float(m.group(1)) if m else None
    clean = MARKS_NAME_RE.sub("", name or "")
    return _module_name(clean), marks


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


MARKER_RE = re.compile(
    r"\b(?:Lesson|Chapter|Ch|L)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]?\s*"
    r"(\d{1,2})(?:([AB])(?=[\.\s\-\u2013:]|$)|\s+([AB])\s*\.)?", re.I)

# "L- I-Meaning of Law": some sheets print small lesson numbers as roman numerals
_ROMAN_ONE = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
              "VIII": 8, "IX": 9, "X": 10}
ROMAN_MARKER_RE = re.compile(
    r"\bL\s*[\-\u2013]\s*([IVX]{1,4})\s*[\-\u2013]\s*(?=[A-Za-z])")


def _title_from_chunk(chunk):
    """A lesson title is either wrapped in brackets or runs to the next marker."""
    c = _clean(chunk)
    c = re.sub(r"^[\-\u2013\u2014:\.\s]+", "", c)
    if c.startswith("(") or c.startswith("["):
        m = re.match(r"[\(\[]\s*(.+?)\s*[\)\]]", c, re.S)
        if m:
            return _clean(m.group(1))
        return _clean(c.lstrip("(["))
    c = COUNT_MARK_RE.sub("", c)
    # table furniture that lands at the end of a lesson title: the "OR" between
    # the two options, the "OPTIONAL MODULE" heading split across the columns
    c = re.sub(r"(?:\s+(?:OR|OPTIONAL))+\s*$", "", c)
    c = re.sub(r"\s+MODULE\s*$", "", c)
    # page furniture glued to a title: "... Indian Cultural Heritage Page 1 of 1"
    c = re.sub(r"\s*Page\s+\d+\s+of\s+\d+\s*$", "", c, flags=re.I)
    c = re.sub(r"[\-\u2013\u2014\s]+$", "", c)
    c = re.sub(r"^[\)\]\|]\s*", "", c)
    return _clean(c)


def _lessons(cell):
    """
    Pull [(no, letter, title)] out of one table cell.

    Three layouts are supported:
      bracketed   L-3 (Laws of Motion)   /  Lesson-1 (Atoms and Molecules)
      bare        Lesson-21 d-Block and f-Block Elements
      language    3 : Gillu              (one lesson per line)

    Optional-module variants keep their letter: L-29A / L-30 B. -> (29, "A"/"B", ..).
    """
    raw = "" if cell is None else str(cell)
    raw = re.sub(r"\(cid\s*:\s*\d+\)", "", raw)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
    # "L- I-Meaning of Law": roman numeral used as a small lesson number
    raw = ROMAN_MARKER_RE.sub(lambda m: "L-%d " % _ROMAN_ONE[m.group(1)], raw)
    txt = _clean(raw)
    if not txt or txt in {"-", "_", "--", "\u2013", "\u2014"}:
        return []

    out = []
    marks = list(MARKER_RE.finditer(txt))
    for i, m in enumerate(marks):
        chunk = txt[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(txt)]
        title = _title_from_chunk(chunk)
        if len(title) >= 3:
            letter = (m.group(2) or m.group(3) or "").upper()
            out.append((int(m.group(1)), letter, title))

    # language subjects: a numbered line starts a lesson, the lines after it
    # continue the same title until the next number
    if not out:
        cur_no, cur_title = None, []
        def _flush():
            if cur_no is not None:
                t = COUNT_MARK_RE.sub("", " ".join(cur_title)).strip(" .:\u0964-\u2013\u2014")
                if t:
                    out.append((cur_no, "", _clean(t)))
        for line in raw.split("\n"):
            line = _clean(line)
            if not line or SKIP_LINE_RE.search(line):
                continue
            m = COLON_LINE_RE.match(line)
            if m:
                _flush()
                cur_no, cur_title = int(m.group(1)), [m.group(2)]
            elif cur_no is not None:
                cur_title.append(line)
        _flush()

    seen, uniq = set(), []
    for no, letter, title in out:
        if (no, letter) in seen:
            continue
        seen.add((no, letter))
        uniq.append((no, letter, title))
    return uniq


def _to_float(s):
    m = re.search(r"\d+(?:\.\d+)?", _clean(s))
    return float(m.group()) if m else None


# ---------------------------------------------------------------------------
# table finders
# ---------------------------------------------------------------------------

def _find_bifurcation(tables):
    """
    Return (rows, header_counts) where rows = [(module_name, tma_text, pe_text)].

    NIOS lays this table out in three different ways:

      A. one table row per module, each cell holding every lesson of that module
      B. one table row per printed LINE, so a module spans many rows and the
         continuation rows have an empty module cell
      C. module names that wrap over two or three lines

    The grouper below handles all three. A row starts a new module when its
    module cell carries an explicit marker (Module I, Module-2, 1., 2)).
    If no row in the table carries such a marker, every non empty module cell
    is treated as a new module, which is the layout A behaviour.
    """
    def _body(tbl, mod_col, tma_col, pe_col):
        """Rows of one table reduced to (module, tma cell, pe cell)."""
        hdr, body = {}, []
        for row in tbl:
            if max(mod_col, tma_col, pe_col) >= len(row):
                continue
            name = _clean(row[mod_col])
            tma_cell, pe_cell = row[tma_col] or "", row[pe_col] or ""
            for key, cell in (("tma", tma_cell), ("pe", pe_cell)):
                cnt, rest = _hdr_count(cell)
                if cnt is not None:
                    hdr[key] = cnt
                    if key == "tma":
                        tma_cell = rest
                    else:
                        pe_cell = rest
            joined = " ".join(_clean(x) for x in (name, tma_cell, pe_cell))
            has_lesson = bool(_lessons(tma_cell) or _lessons(pe_cell))
            if not has_lesson and (HEADER_CELL_RE.search(joined) or TITLE_JUNK_RE.search(joined)):
                continue
            if not (name or _clean(tma_cell) or _clean(pe_cell)):
                continue
            body.append((name, tma_cell, pe_cell))
        return body, hdr

    def _group(body):
        """Join continuation rows into whole modules."""
        numbered = any(MODULE_START_RE.match(n) for n, _, _ in body)
        groups = []
        for name, tma_cell, pe_cell in body:
            starts = MODULE_START_RE.match(name) if numbered else bool(name)
            if starts or not groups:
                groups.append([name, [tma_cell], [pe_cell]])
            else:
                if name:
                    groups[-1][0] = (groups[-1][0] + " " + name).strip()
                groups[-1][1].append(tma_cell)
                groups[-1][2].append(pe_cell)
        rows = []
        for name, tmas, pes in groups:
            # keep the raw numbering here ("6A.") - the caller cleans the name
            # and reads the option letter off it
            nm = name if re.search(r"[A-Za-z]{3}|[\u0900-\u097F]{2}", name or "") else (name or "Module")
            rows.append((nm, "\n".join(x for x in tmas if _clean(x)),
                         "\n".join(x for x in pes if _clean(x))))
        return rows

    candidates = []
    for rank, pg, tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        flat = " | ".join(_clean(c) for row in tbl[:8] for c in row if c)
        if not (TMA_HINT.search(flat) and PE_HINT.search(flat)):
            continue

        tma_col = pe_col = mod_col = None
        for row in tbl[:8]:
            for i, c in enumerate(row):
                t = _clean(c)
                if tma_col is None and TMA_HINT.search(t):
                    tma_col = i
                if pe_col is None and PE_HINT.search(t):
                    pe_col = i
                if mod_col is None and re.search(r"module", t, re.I) and not MODULE_START_RE.match(t):
                    mod_col = i
        if tma_col is None or pe_col is None or tma_col == pe_col:
            continue
        if mod_col is None:
            mod_col = 0

        body, hdr = _body(tbl, mod_col, tma_col, pe_col)
        if not body:
            continue
        rows = _group(body)
        score = sum(len(_lessons(a)) + len(_lessons(b)) for _, a, b in rows)
        junk = sum(1 for n, _, _ in rows if MARKER_RE.search(n) or len(n) < 3)
        if score:
            candidates.append({"score": score, "junk": junk, "rank": rank, "page": pg,
                               "ncols": len(tbl[0]), "cols": (mod_col, tma_col, pe_col),
                               "body": body, "hdr": hdr, "nrows": len(rows)})

    if not candidates:
        return [], {}
    candidates.sort(key=lambda c: (-c["score"], c["junk"], c["rank"], c["nrows"]))
    best = candidates[0]
    mod_col, tma_col, pe_col = best["cols"]

    # A long syllabus table runs over several pages. The continuation pages
    # carry no header, so they never become candidates on their own. Pick up
    # any table of the same shape on a later page and append its rows.
    body = list(best["body"])
    for rank, pg, tbl in tables:
        if rank != best["rank"] or pg <= best["page"] or not tbl:
            continue
        if len(tbl[0]) != best["ncols"]:
            continue
        flat = " | ".join(_clean(c) for row in tbl[:4] for c in row if c)
        if TMA_HINT.search(flat) and PE_HINT.search(flat):
            continue  # a fresh header means a different table, not a continuation
        more, _ = _body(tbl, mod_col, tma_col, pe_col)
        if more and any(_lessons(a) or _lessons(b) for _, a, b in more):
            body.extend(more)

    return _group(body), best["hdr"]


def _find_weightage(tables, page_texts):
    """Return list of (module_name, marks) from the Weightage by Content table."""
    best = None
    for _rank, _pg, tbl in tables:
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

def _bucket_line(ws, b_mod, b_pe, slack=80):
    """
    Split one visual row into (module, tma, pe) word lists. The cut is made
    at the widest horizontal gap near each header-derived boundary, so a
    wrapped TMA title that runs long does not bleed into the PE cell.
    """
    def cut(words, bound):
        best = None
        for i, (a, b) in enumerate(zip(words, words[1:])):
            # never split right before a bracket: "(14 Marks)" or "(A.D. 300)"
            # is part of the name or title on the left, not a new column
            if str(b["text"]).startswith("("):
                continue
            gap = b["x0"] - a["x1"]
            mid = (a["x1"] + b["x0"]) / 2.0
            if gap > 6 and abs(mid - bound) <= slack and (best is None or gap > best[0]):
                best = (gap, i + 1)
        if best is not None:
            return words[:best[1]], words[best[1]:]
        i = 0
        while i < len(words) and words[i]["x0"] < bound:
            i += 1
        return words[:i], words[i:]

    mod_ws, rest = cut(ws, b_mod)
    tma_ws, pe_ws = cut(rest, b_pe)
    return mod_ws, tma_ws, pe_ws


# a header cell that only carries the column count: "(No. of lessons- 11)",
# "06 lessons", "1 2 lessons" (digits split by the font), "(14lessons)"
HDR_COUNT_FULL_RE = re.compile(
    r"^\s*\(?\s*(?:(?:no\.?\s*of\s*)?lessons?\s*[\-\u2013:=]?\s*\(?\s*(\d{1,3})\s*\)?"
    r"|(\d{1,2}(?:\s\d)?)\s*lessons?)\s*\)?\s*$", re.I)
HDR_COUNT_LEAD_RE = re.compile(
    r"^\s*\(?\s*(\d{1,2}(?:\s\d)?)\s*lessons?\s*\)?\s*")


def _hdr_count(cell):
    """-> (count or None, cell with the count text removed)."""
    c = _clean(cell)
    m = HDR_COUNT_FULL_RE.match(c)
    if m:
        return int((m.group(1) or m.group(2)).replace(" ", "")), ""
    m = re.search(r"no\.?\s*of\s*lessons?\s*\)?\s*[:=\-\u2013]?\s*(\d{1,3})", c, re.I)
    if m:
        return int(m.group(1)), c
    m = HDR_COUNT_LEAD_RE.match(c)
    if m and re.match(r"\s*(?:\(cid|$|\n|L|Lesson)", c[m.end():], re.I):
        return int(m.group(1).replace(" ", "")), c[m.end():].strip()
    return None, cell


# lines that are table furniture, not content: the "OR" between options, the
# "OPTIONAL MODULE" heading split across columns, a stray "Module Name" header
JUNK_CELL_RE = re.compile(r"^\s*(?:or|optional|module|module\s+name)\s*$", re.I)
TITLE_JUNK_RE = re.compile(
    r"bifurcation\s*of\s*syllabus|^\s*subject\s*:|course\s+level|^\s*code\s*:"
    r"|^\s*total\s+lessons?\b|^\s*module\s+name\b", re.I)
# a lesson marker that drifted into the module column: "Lesson-26"
MOD_MARKER_ONLY_RE = re.compile(
    r"^\s*(?:L|Lesson)\s*[\-\u2013]?\s*\d{1,2}\s*[AB]?\s*[\.:]?\s*$", re.I)
# a lesson number whose "L-" prefix drifted into the other column: "7: Title"
NUM_TITLE_RE = re.compile(r"^\s*(\d{1,2})\s*[:.]\s*\S")
BARE_L_RE = re.compile(r"^\s*L\s*[\-\u2013]?\s*$", re.I)
BARE_MARKER_RE = re.compile(r"^\s*(?:L|Lesson)\s*[\-\u2013]?\s*\d{1,2}\s*[AB]?\s*[\.:]?\s*$", re.I)
# "7A. L- 29:" style option tags printed inside the lesson cells
CELL_TAG_RE = re.compile(r"(?<![\w\-\u2013])\d{1,2}\s*[AB]\s*\.", re.I)


def _merge_drift(tma, pe, tma_x0=None, pe_x0=None, tma_cx=None, pe_cx=None, b_pe=None):
    """
    Repair column drift inside one visual row. A marker split from its lesson
    rejoins it. Ownership rules:
      - a bare "L-" belongs with the side that has the lesson NUMBER ("7: T.")
      - a numbered marker ("L-8.") belongs to the column whose other lesson
        markers sit at the same x position (headers lie - cells are the truth)
    """
    if not (tma and pe):
        return tma, pe

    def _owner(mx0):
        """Which column a marker at x0 really belongs to."""
        if mx0 is None:
            return "tma"
        if tma_cx is not None and pe_cx is not None:
            return "tma" if abs(mx0 - tma_cx) <= abs(mx0 - pe_cx) else "pe"
        if b_pe is not None:
            return "tma" if mx0 < b_pe else "pe"
        return "tma"

    def _trail_l(cell):
        """'of Representation L-' -> ('of Representation', True): a marker prefix
        glued to the end of a wrapped title."""
        m = re.match(r"^(.*?)\s*(?:L|Lesson)\s*[\-\u2013]?\s*$", cell or "", re.I)
        return (m.group(1), True) if m else (cell, False)

    th, t_lost = _trail_l(tma)
    ph, p_lost = _trail_l(pe)
    if t_lost and NUM_TITLE_RE.match(pe):
        return (th, "") if _owner(pe_x0) == "tma" else (th, "L-" + pe.strip())
    if p_lost and NUM_TITLE_RE.match(tma):
        return ("L-" + tma.strip(), ph) if _owner(tma_x0) == "tma" else ("", ph)
    if BARE_MARKER_RE.match(tma) and not MARKER_RE.search(pe):
        merged = (tma + " " + pe).strip()
        return (merged, "") if _owner(tma_x0) == "tma" else ("", merged)
    if BARE_MARKER_RE.match(pe) and not MARKER_RE.search(tma):
        merged = (pe + " " + tma).strip()
        return (merged, "") if _owner(pe_x0) == "tma" else ("", merged)
    return tma, pe


def _position_rows(pdf_pages):
    """
    Read the bifurcation table by word x-position instead of ruling lines.

    NIOS prints these sheets with partial or broken ruling, so pdfplumber's
    table extraction often shifts cells between columns from row to row.
    Word positions do not have that problem: locate the MODULE / TMA / Public
    Examination header words, split the column boundaries at the midpoints
    between them, then bucket every word below the header by its x position.

    Handles both known layouts:
      module rows : module name in col 1, lesson lists in the TMA/PE columns
      lesson rows : one lesson per row, the TMA/PE column carries the word
                    "TMA" or "Public Examination" as the category tag

    Returns (rows, hdr_counts) in the same shape as _find_bifurcation.
    """
    groups = []          # [name, [tma chunks], [pe chunks]]
    hdr = {}
    flat_mode = False
    last_flat = None     # chunk list of the last per-lesson row (for wrapped titles)
    b_mod = b_pe = None  # column boundaries (midpoints between header x0s)
    mod_y = []           # (group idx, page idx, y) where each module name starts
    assign = []          # (page idx, y, group idx, slot 1|2, chunk) per lesson line

    for page_i, page in enumerate(pdf_pages[:40]):
        try:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        except Exception:
            continue
        if not words:
            continue

        # glued-font sheets (no space glyphs at all) come out as mega-words.
        # Re-extract with a tight tolerance to split them back into words.
        MARK_X_RE = re.compile(r"^(?:L|Lesson)\s*[\-\u2013]?\s*\d{1,2}", re.I)
        mw0 = sorted(w["x1"] - w["x0"] for w in words
                     if MARK_X_RE.match((w.get("text") or "").strip()))
        glued = len(mw0) >= 4 and mw0[len(mw0) // 2] > 80
        if glued:
            try:
                fine = page.extract_words(x_tolerance=2, use_text_flow=False,
                                          keep_blank_chars=False) or []
                if fine:
                    words = fine
            except Exception:
                pass

        # the exam column is headed "Public Examination" in older PDFs and
        # "Term End Examination (60%)" in newer ones - accept both. Some fonts
        # glue the words together ("PublicExamination(60%)"), so fall back to a
        # looser match when the strict one finds nothing.
        PE_HEAD_RE = re.compile(r"(?:Public|Term|Examination|PE)\b", re.I)
        PE_HEAD_LOOSE_RE = re.compile(r"(?:Public|Term|Examination)\w*", re.I)
        mod_x = tma_x = pe_x = None
        tma_top = pe_top = None
        for w in words:
            t = (w.get("text") or "").strip()
            if mod_x is None and re.match(r"(?:MODULE|Lesson)\b", t, re.I):
                mod_x = w["x0"]
            if tma_x is None and re.match(r"TMA\b", t, re.I):
                tma_x, tma_top = w["x0"], w["top"]
            if pe_x is None and PE_HEAD_RE.match(t):
                pe_x, pe_top = w["x0"], w["top"]
        if pe_x is None:
            for w in words:
                t = (w.get("text") or "").strip()
                if PE_HEAD_LOOSE_RE.match(t) and (tma_top is None or abs(w["top"] - tma_top) <= 42):
                    pe_x, pe_top = w["x0"], w["top"]
                    break
        # a "Term"/"Examination" hit far below the TMA header is a lesson
        # title word, not the column header - reject it
        if tma_top is not None and pe_top is not None and abs(tma_top - pe_top) > 42:
            pe_x = None
        header_top = None
        if tma_x is not None and pe_x is not None and pe_x > tma_x:
            mx = mod_x if mod_x is not None and mod_x < tma_x else 0.0
            b_mod = (mx + tma_x) / 2.0
            b_pe = (tma_x + pe_x) / 2.0
            header_top = min((w["top"] for w in words
                              if re.match(r"(?:TMA|Public|Term|Examination)\b",
                                          (w.get("text") or "").strip(), re.I)),
                             default=None)
        if b_mod is None or b_pe is None:
            continue

        # where each column's lesson markers really sit: headers are centered
        # and lie, so learn the two clusters from numbered markers themselves
        mws = sorted((w["x0"], w["x1"] - w["x0"]) for w in words
                     if MARK_X_RE.match((w.get("text") or "").strip()))
        xs = [x for x, _ in mws]
        tma_cx = pe_cx = None
        if len(xs) >= 4:
            gi, gap = max(((i, xs[i + 1] - xs[i]) for i in range(len(xs) - 1)),
                          key=lambda t: t[1])
            left, right = xs[:gi + 1], xs[gi + 1:]
            if gap > 30 and len(left) >= 2 and len(right) >= 2:
                tma_cx = left[len(left) // 2]
                pe_cx = right[len(right) // 2]
                # A glued font puts PE markers far left of the centred header
                # word, so the header midpoint cuts the PE column itself. With
                # spaced text the long TMA titles need the header midpoint.
                cand = (tma_cx + pe_cx) / 2.0
                if glued and left[-1] + 60 < cand < b_pe:
                    b_pe = cand

        lines = []  # words on one visual row; tops on that row can jitter a few px
        for w in sorted(words, key=lambda x: (x["top"], x["x0"])):
            if header_top is not None and w["top"] < header_top - 2:
                continue
            if lines and w["top"] <= lines[-1][1] + 3:
                lines[-1][1] = max(lines[-1][1], w["top"])
                lines[-1][2].append(w)
            else:
                lines.append([w["top"], w["top"], [w]])

        for _top0, _top1, ws in lines:
            ws = sorted(ws, key=lambda x: x["x0"])
            mod_ws, tma_ws, pe_ws = _bucket_line(ws, b_mod, b_pe)
            mod = _clean(" ".join(w["text"] for w in mod_ws))
            tma = _clean(" ".join(w["text"] for w in tma_ws))
            pe = _clean(" ".join(w["text"] for w in pe_ws))
            # table furniture lines: "OR", "OPTIONAL MODULE", "Module Name"
            or_line = False
            if JUNK_CELL_RE.match(mod or ""):
                or_line = bool(re.match(r"(?i)^\s*or\s*$", mod or ""))
                mod = ""
            if JUNK_CELL_RE.match(tma or ""):
                or_line = or_line or bool(re.match(r"(?i)^\s*or\s*$", tma or ""))
                tma = ""
            if JUNK_CELL_RE.match(pe or ""):
                or_line = or_line or bool(re.match(r"(?i)^\s*or\s*$", pe or ""))
                pe = ""
            # the connector between two option modules matters: remember it
            if or_line and groups:
                groups[-1][0] = (groups[-1][0] + " OR").strip()
            # a lesson marker that drifted into the module column ("Lesson-26")
            # belongs with the lessons, not with the module name
            if mod and MOD_MARKER_ONLY_RE.match(mod):
                tma = (mod + " " + tma).strip()
                mod = ""
            if not (mod or tma or pe):
                continue
            joined = " ".join(x for x in (mod, tma, pe) if x)

            for k2, cell in (("tma", tma), ("pe", pe)):
                cnt, rest = _hdr_count(cell)
                if cnt is not None:
                    hdr[k2] = cnt
                    if k2 == "tma":
                        tma = rest
                    else:
                        pe = rest
            if not (mod or tma or pe):
                continue
            # sheet title / column header junk, never a module
            if TITLE_JUNK_RE.search(joined) and not MARKER_RE.search(tma or "") \
                    and not MARKER_RE.search(pe or ""):
                continue
            # option tags printed inside the lesson cells: "7A. L- 29: ..."
            tma = CELL_TAG_RE.sub("", tma).strip()
            pe = CELL_TAG_RE.sub("", pe).strip()
            # repair column drift (bare "L-" / bare markers / loose titles);
            # the marker's own x position against the column clusters decides
            tma_x0 = tma_ws[0]["x0"] if tma_ws else None
            pe_x0 = pe_ws[0]["x0"] if pe_ws else None
            tma, pe = _merge_drift(tma, pe, tma_x0, pe_x0, tma_cx, pe_cx, b_pe)

            tma_cat = bool(re.match(r"TMA\b", tma, re.I)) if tma else False
            pe_cat = bool(PE_HEAD_RE.match(pe)) if pe else False
            has_markers = bool(MARKER_RE.search(tma) or MARKER_RE.search(pe))

            # per-lesson category layout: "1. Basics of" + category word
            mnum = re.match(r"^\s*(\d{1,2})\s*[\.\)]\s*(\S.*)$", mod)
            if mnum and (tma_cat or pe_cat) and not has_markers:
                flat_mode = True
                no = int(mnum.group(1))
                title = _clean(mnum.group(2))
                if not groups or groups[-1][0] != "__flat__":
                    groups.append(["__flat__", [], []])
                idx = 1 if tma_cat else 2
                groups[-1][idx].append("L-%d %s" % (no, title))
                last_flat = groups[-1][idx]
                continue
            if flat_mode and mod and not tma and not pe and last_flat is not None:
                last_flat[-1] = last_flat[-1] + " " + mod   # wrapped lesson title
                continue

            if HEADER_CELL_RE.search(joined) and not has_markers:
                continue

            if MODULE_START_RE.match(mod):
                groups.append([mod, [], []])
                mod_y.append((len(groups) - 1, page_i, _top0))
                last_flat = None
            elif mod and not groups:
                # a sheet title is not a module; wait for the real first module
                if TITLE_JUNK_RE.search(mod):
                    continue
                groups.append([mod, [], []])
                mod_y.append((len(groups) - 1, page_i, _top0))
                last_flat = None
            elif mod and groups:
                groups[-1][0] = (groups[-1][0] + " " + mod).strip()   # wrapped module name
            if not groups and (tma or pe):
                # lessons that sit above the first module row (their module name
                # row was eaten by the header) - park them, prepend to the first
                # real module when the table is done
                groups.append(["__pending__", [], []])
            if groups:
                if tma and not tma_cat:
                    groups[-1][1].append(tma)
                    assign.append((page_i, _top0, len(groups) - 1, 1, tma))
                if pe and not pe_cat:
                    groups[-1][2].append(pe)
                    assign.append((page_i, _top0, len(groups) - 1, 2, pe))

    _rehome_by_blocks(groups, mod_y, assign, pdf_pages, b_mod)

    rows = []
    pending = None
    for name, tmas, pes in groups:
        nm = "All Lessons" if name == "__flat__" else name
        t_txt, p_txt = "\n".join(tmas), "\n".join(pes)
        if name == "__pending__" or (not _clean(name) and (t_txt or p_txt)):
            pending = (t_txt, p_txt)          # prepend to the first real module
            continue
        if pending:
            t_txt = "\n".join(x for x in (pending[0], t_txt) if x)
            p_txt = "\n".join(x for x in (pending[1], p_txt) if x)
            pending = None
        if _clean(t_txt) or _clean(p_txt):
            rows.append((nm, t_txt, p_txt))
    if pending and rows:                      # never found a real module: keep the lessons
        t_txt = "\n".join(x for x in (pending[0], rows[-1][1]) if x)
        p_txt = "\n".join(x for x in (pending[1], rows[-1][2]) if x)
        rows[-1] = (rows[-1][0], t_txt, p_txt)
    return rows, hdr


def _module_block_bounds(page, b_mod):
    """
    Y coordinates of the horizontal rulings that cross the module column.
    On fully cell-ruled sheets (e.g. Sociology 331) every module gets one
    tall bordered cell; the borders of those cells are the only trustworthy
    module boundaries, because the module NAME is printed in the vertical
    middle of its cell while its first lesson lines sit above the name.
    """
    ys = []
    try:
        for rc in page.rects:
            h = rc["bottom"] - rc["top"]
            wdt = rc["x1"] - rc["x0"]
            if h < 2.5 and wdt > 20 and rc["x0"] <= b_mod + 4:
                ys.append((rc["top"] + rc["bottom"]) / 2.0)
    except Exception:
        pass
    try:
        for ln in page.lines:
            if abs(ln["top"] - ln["bottom"]) < 2 and \
                    ln["x1"] - ln["x0"] > 20 and ln["x0"] <= b_mod + 4:
                ys.append((ln["top"] + ln["bottom"]) / 2.0)
    except Exception:
        pass
    ys = sorted(ys)
    out = []
    for y in ys:
        if not out or y - out[-1] > 3:
            out.append(y)
    return out


def _rehome_by_blocks(groups, mod_y, assign, pdf_pages, b_mod):
    """
    Fix lessons that were appended to the wrong module because the module
    name is centred inside a tall bordered cell: lesson lines ABOVE the name
    line (same cell) went to the previous module. The cell rulings decide:
    every lesson line belongs to the bordered block that contains it.

    Safe no-op on sheets ruled per lesson row (each lesson line is then its
    own block, blocks without a module name keep their current owner) and on
    unruled sheets (no boundaries found at all).
    """
    if b_mod is None or not mod_y or not assign:
        return
    import bisect as _bs
    for pi in {a[0] for a in assign}:
        try:
            page = pdf_pages[pi]
        except Exception:
            continue
        bounds = _module_block_bounds(page, b_mod)
        if len(bounds) < 2:
            continue
        def _blk(y, bounds=bounds):
            return _bs.bisect_right(bounds, y) - 1
        owners = {}
        ok = True
        for gi, p2, y in mod_y:
            if p2 != pi:
                continue
            b = _blk(y)
            if b < 0 or b >= len(bounds) - 1 or b in owners:
                ok = False          # two names in one cell: do not trust the grid
                break
            owners[b] = gi
        if not ok or not owners:
            continue
        touched = set()
        for ai, (p2, y, gi, slot, chunk) in enumerate(assign):
            if p2 != pi:
                continue
            tgt = owners.get(_blk(y))
            if tgt is not None and tgt != gi and chunk in groups[gi][slot]:
                groups[gi][slot].remove(chunk)
                groups[tgt][slot].append(chunk)
                touched.add((gi, slot))
                touched.add((tgt, slot))
                assign[ai] = (p2, y, tgt, slot, chunk)
        if not touched:
            continue
        # keep every touched slot in reading order after the moves
        y_of = {}
        for p2, y, gi, slot, chunk in assign:
            if p2 == pi:
                y_of.setdefault((gi, slot, chunk), y)
        for gi, slot in touched:
            groups[gi][slot].sort(key=lambda c: y_of.get((gi, slot, c), 0))


def _lesson_total(rows):
    return sum(len(_lessons(a)) + len(_lessons(b)) for _, a, b in rows)


MOD_LETTER_RE = re.compile(
    r"^\s*(?:module\s*[\-\u2013]?\s*)?([IVXLC]+|\d{1,2})\s*\(?\s*([AB])\s*[\)\.\-\u2013:]", re.I)
LESSON_NO_RE = re.compile(r"^L-(\d{1,2})([AB]?)$")


def _link_or_options(modules):
    """
    NIOS optional modules come as a pair for one exam slot - the student sits
    only one of them. Printed as 6A/6B, VII A/VII B, 5 (a)/5 (b), or the same
    number twice with an "or" between. Link each pair with an optional_group,
    route lettered lessons to their own option (the PDF often misplaces one),
    give same-numbered lessons a letter so both options can coexist, and tidy
    the option names. Marks stay on the option that carried them; the sibling
    borrows through the group at read time.
    """
    by_num = {}
    for i, m in enumerate(modules):
        ml = m.get("_ml")
        if ml:
            by_num.setdefault(ml[0], []).append(i)
    for num, idxs in by_num.items():
        letters = {modules[i]["_ml"][1] for i in idxs}
        if not ({"A", "B"} <= letters):
            continue
        grp = "option " + num
        pair = [modules[i] for i in idxs]
        pool = [l for m in pair for l in m["lessons"]]
        lettered = any(str(l["no"]).endswith(("A", "B")) for l in pool)
        if lettered:
            # pool and re-route: a lesson printed under the wrong option goes home
            for m in pair:
                want = m["_ml"][1]
                if want == "A":
                    m["lessons"] = [dict(l) for l in pool if not str(l["no"]).endswith("B")]
                else:
                    m["lessons"] = [dict(l) for l in pool if str(l["no"]).endswith("B")]
            # a letter can be glued to the title and lost ("L-35AQuest"):
            # when the sibling carries the same number lettered, this one is
            # the matching letter of its own option
            sib = {}
            for m in pair:
                for l in m["lessons"]:
                    nm = LESSON_NO_RE.match(str(l["no"]))
                    if nm and nm.group(2):
                        sib.setdefault(nm.group(1), set()).add(nm.group(2))
            for m in pair:
                want = m["_ml"][1]
                other = "B" if want == "A" else "A"
                for l in m["lessons"]:
                    nm = LESSON_NO_RE.match(str(l["no"]))
                    if nm and not nm.group(2) and other in sib.get(nm.group(1), set()):
                        l["no"] = "L-%s%s" % (nm.group(1), want)
                        # the glued letter leaked into the title: "AQuest",
                        # "A Water Conservation" - take it back
                        t = re.sub(r"^%s(?:\s+|\s*[\-\u2013:\.]\s*|(?=[A-Z]))" % want,
                                   "", str(l["title"]))
                        if t:
                            l["title"] = t
        else:
            # neither option letters its lessons. When both reuse the same
            # numbers (L-20 in 6A and in 6B), letter each side so the lessons
            # stay distinct; otherwise keep the printed numbering as is.
            own = {id(m): [str(l["no"]) for l in m["lessons"]] for m in pair}
            flat = [n for nos in own.values() for n in nos]
            if len(flat) != len(set(flat)):
                for m in pair:
                    suffix = m["_ml"][1]
                    m["lessons"] = [dict(l, no=str(l["no"]) + suffix) for l in m["lessons"]]
        for m in pair:
            want = m["_ml"][1]
            m["optional_group"] = grp
            m["module"] = re.sub(r"\s+OR\s*$", "", m["module"], flags=re.I).strip() \
                          + " (Option %s)" % want
            m["lessons"].sort(key=lambda l: (int(LESSON_NO_RE.match(l["no"]).group(1))
                                             if LESSON_NO_RE.match(l["no"]) else 999, l["no"]))
        ws = [m["weightage"] for m in pair]
        if sum(ws) and any(w == 0 for w in ws):
            top = max(ws)
            for m in pair:
                m["weightage"] = top if m["_ml"][1] == "A" else 0.0
    for m in modules:
        m.pop("_ml", None)
    # an option that ended up with no lessons at all only confuses the editor
    return [m for m in modules if m["lessons"] or not m.get("optional_group")]


def _groups_of(modules):
    """{group_key: [modules]} for OR option pairs ('6A/6B - one exam slot')."""
    groups = {}
    for m in modules:
        g = m.get("optional_group")
        if g:
            groups.setdefault(g, []).append(m)
    return groups


def _effective_counts(modules):
    """
    (total, tma, pe) lesson counts the way NIOS states them on the PDF:
    every OR option pair is counted once, as the bigger of the two options.
    """
    groups = _groups_of(modules)
    grouped = {id(m) for ms in groups.values() for m in ms}

    def _k(m, kind):
        return len([l for l in m["lessons"] if l["kind"] == kind])

    tot = tma = pe = 0
    for m in modules:
        if id(m) in grouped:
            continue
        tot += len(m["lessons"])
        tma += _k(m, "TMA")
        pe += _k(m, "PE")
    for ms in groups.values():
        tot += max(len(m["lessons"]) for m in ms)
        tma += max(_k(m, "TMA") for m in ms)
        pe += max(_k(m, "PE") for m in ms)
    return tot, tma, pe


def _words_fallback(pdf_pages):
    """
    Last resort for PDFs with no ruled table.

    Finds the x position of the TMA and Public Examination headers, then
    assigns every word on the page to the module / TMA / PE column by its own
    x position. Works on layouts that pdfplumber cannot see as a table.
    """
    out_rows = []
    for page in pdf_pages[:20]:
        try:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
        except Exception:
            continue
        if not words:
            continue
        tma_x = pe_x = None
        for w in words:
            t = (w.get("text") or "").strip()
            if tma_x is None and re.fullmatch(r"TMA", t, re.I):
                tma_x = w["x0"]
            if pe_x is None and re.fullmatch(r"Public|Term|Examination|PE", t, re.I):
                pe_x = w["x0"]
        if tma_x is None or pe_x is None or pe_x <= tma_x:
            continue

        lines = {}
        for w in words:
            key = round(w["top"] / 4.0)
            lines.setdefault(key, []).append(w)

        cur = None
        for key in sorted(lines):
            ws = sorted(lines[key], key=lambda x: x["x0"])
            mod_txt = " ".join(w["text"] for w in ws if w["x1"] <= tma_x + 4)
            tma_txt = " ".join(w["text"] for w in ws if tma_x - 4 < w["x0"] < pe_x - 4)
            pe_txt = " ".join(w["text"] for w in ws if w["x0"] >= pe_x - 4)
            mod_txt = _clean(mod_txt)
            if mod_txt and not MODULE_HINT.search(mod_txt) and \
               re.search(r"[A-Za-z]{3}|[\u0900-\u097F]{2}", mod_txt) and \
               not re.search(r"total\s+no\.?\s*of|no\.?\s*of\s*lessons", mod_txt, re.I):
                cur = [_module_name(mod_txt), [], []]
                out_rows.append(cur)
            if cur is not None:
                if _clean(tma_txt):
                    cur[1].append(tma_txt)
                if _clean(pe_txt):
                    cur[2].append(pe_txt)
        if out_rows:
            break
    return [(n, "\n".join(a), "\n".join(b)) for n, a, b in out_rows if a or b]


# ---------------------------------------------------------------------------
# v102: post-parse reconciliation — the table extraction of some NIOS sheets
# drops lessons (marker lands in the module-name cell, a heading wraps over
# the lesson, columns glue together). These helpers rebuild the damage from
# the plain page text and make the expected-count reading just as tolerant.
# ---------------------------------------------------------------------------

# a title that ends like this was cut off mid-sentence by the cell split
UNFINISHED_RE = re.compile(
    r"(?:\b(?:and|&|of|the|for|in|to|on|with|its|their|from|by|a|an|or)\s*$|[,\-\u2013:/(]$)", re.I)

LESSON_ANY_RE = re.compile(
    r"\b(?:L|Lesson)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]\s*\d{1,2}(?![\d])", re.I)


def _lesson_marker_re(n, letter=""):
    # 'L-4' must not match inside 'L-42' or the option tags 'L-4A'
    if letter:
        return re.compile(
            r"\b(?:L|Lesson)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]\s*%d\s*%s(?![\dAB])" % (n, letter), re.I)
    return re.compile(
        r"\b(?:L|Lesson)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]\s*%d(?![\dAB])" % n, re.I)


def _find_lesson_in_text(lines, n, mod_name="", letter=""):
    """
    Locate lesson n in the raw page lines and rebuild its title, joining
    wrapped continuation lines ("Chemical Reaction" + "and Equations") and
    cutting the other column's lesson off the same line. Leading words that
    really belong to a wrapped module name ("Things" in "Moving Things") are
    dropped from continuations.
    """
    pat = _lesson_marker_re(n, letter)
    mod_words = set(_norm(mod_name).split())
    for i, line in enumerate(lines):
        m = pat.search(line)
        if not m:
            continue
        tail = line[m.end():]
        nm = LESSON_ANY_RE.search(tail)
        if nm:
            tail = tail[:nm.start()]
        title = tail.strip()
        joined_cont = False
        for j in range(i + 1, min(i + 3, len(lines))):
            cont = lines[j].strip()
            if not cont:
                break
            nm2 = LESSON_ANY_RE.search(cont)
            if nm2:
                cont = cont[:nm2.start()].strip()
            if not cont or len(cont) > 55:
                break
            if MODULE_START_RE.match(cont) or HEADER_CELL_RE.search(cont):
                break
            words = cont.split()
            while words and words[0].strip(".,:;-()").lower() in mod_words:
                words = words[1:]
            cont = " ".join(words).strip()
            if not cont:
                break
            if UNFINISHED_RE.search(title) or re.match(
                    r"^(?:and|&|of|the|for|in|to|on|with)\b", cont, re.I) or cont[0].islower():
                title = (title + " " + cont).strip()
                joined_cont = True
                if not UNFINISHED_RE.search(title):
                    break
            else:
                break
        t = _title_from_chunk(title)
        if len(t) >= 3:
            return i, t, joined_cont
    return None


def _module_by_context(lines, idx, modules):
    """The module heading printed nearest above a lesson tells its home -
    headings are often glued to lesson text, so cut at the first marker."""
    names = [(m, _norm(m["module"])) for m in modules]
    best, best_score = None, 0.0
    for j in range(idx, max(-1, idx - 16), -1):
        line = lines[j].strip()
        if not line:
            continue
        if not (MODULE_START_RE.match(line) or re.search(r"module", line, re.I)):
            continue
        cut = LESSON_ANY_RE.search(line)
        head = line[:cut.start()] if cut else line
        nl = _norm(head)
        if not nl and cut and j + 1 < len(lines):
            # the module name wrapped onto the next line ("Module- IV" / "Energy ...")
            nxt = lines[j + 1].strip()
            c2 = LESSON_ANY_RE.search(nxt)
            frag = nxt[:c2.start()] if c2 else nxt
            nl = _norm(head + " " + " ".join(frag.split()[:4]))
        if not nl:
            continue
        for m, nm in names:
            if not nm:
                continue
            score = difflib.SequenceMatcher(None, nl, nm).ratio()
            if nm in nl or nl in nm:
                score = max(score, 0.8)
            if score > best_score:
                best, best_score = m, score
        if best_score >= 0.45:
            return best
    return best if best_score >= 0.45 else None


def _lsort_key(l):
    mm = LESSON_NO_RE.match(str(l["no"]))
    return (int(mm.group(1)), mm.group(2)) if mm else (999, "")


LESSON_IN_NAME_RE = re.compile(
    r"\b(?:L|Lesson)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]\s*\d{1,2}\s*[AB]?(?![\d])", re.I)


def _fix_module_names(modules):
    """
    A lesson marker that landed inside the module-name cell leaves names like
    'Moving L.9 Things'; a nameless fragment row ('4', '18') is really the
    tail of the previous module - merge it back.
    """
    out = []
    for m in modules:
        name = LESSON_IN_NAME_RE.sub(" ", m["module"])
        name = re.sub(r"\s+", " ", name).strip(" -\u2013:.")
        m["module"] = name
        if not re.search(r"[A-Za-z\u0900-\u097F]", name):
            if out:
                prev = out[-1]
                if not prev["weightage"] and m["weightage"]:
                    prev["weightage"] = m["weightage"]
                prev["lessons"].extend(m["lessons"])
                prev["lessons"].sort(key=_lsort_key)
                continue
            m["module"] = "Module"
        out.append(m)
    return out


def _rename_from_text(modules, joined):
    """
    Some sheets list units under the module and the cell read glues them all
    into the name ('Home Science Unit -1 ... Unit -2'). The clean name sits on
    a 'Module-k <name>' line in the page text - match it by name overlap or by
    the lessons printed right after it.
    """
    lines = joined.split("\n")
    head_re = re.compile(r"^\s*module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2})\b[\.\):\-\u2013]?\s*(.+)", re.I)
    heads = []
    for i, line in enumerate(lines):
        hm = head_re.match(line.strip())
        if not hm:
            continue
        cand = hm.group(1).strip()
        cut = LESSON_ANY_RE.search(cand)
        if cut:
            cand = cand[:cut.start()].strip()
        cand = cand.strip(" -\u2013:.")
        if 3 <= len(cand) <= 60 and _norm(cand):
            heads.append((i, cand))
    if not heads:
        return
    for m in modules:
        name = m["module"]
        if len(name) <= 45 or not re.search(r"\bunit\s*[\-\u2013]?\s*\d", name, re.I):
            continue
        nn = _norm(name)
        nums = []
        for l in m["lessons"]:
            mm = LESSON_NO_RE.match(str(l["no"]))
            if mm:
                nums.append(int(mm.group(1)))
        best, best_score = None, 0
        for idx, cand in heads:
            nc = _norm(cand)
            score = 0
            if " ".join(nc.split()[:2]) and " ".join(nc.split()[:2]) in nn:
                score += 100
            if nums:
                window = "\n".join(lines[idx:idx + 20])
                score += sum(1 for n in nums if _lesson_marker_re(n).search(window))
            if score > best_score:
                best, best_score = cand, score
        need = 100 if not nums else max(2, (len(nums) + 1) // 2)
        if best and best_score >= need:
            m["module"] = best


def _suffix_polluted(nt, t, others):
    """The words a repair adds must not already live inside another lesson's
    title - in glued two-column lines the neighbour column's wrapped title
    bleeds in without any marker to cut at ('Status and Role' + 'Association
    and Institution' from L-5's title)."""
    nw, tw = nt.lower().split(), t.lower().split()
    k = 0
    while k < min(len(nw), len(tw)) and nw[k] == tw[k]:
        k += 1
    grams = [" ".join(nw[g:g + 2]) for g in range(k, len(nw) - 1)]
    if not grams:
        return False
    for o in others:
        ol = o.lower()
        if any(g in ol for g in grams):
            return True
    return False


def _repair_titles(modules, joined):
    """
    Re-read titles the cell split truncated. Two shapes:
      cut mid-sentence   'Cooperation, Competition and'
      cut but looks whole 'Indian Social' (the page text carries the full title)
    The page-text title wins when it keeps the cell title as a prefix and adds
    words; single glued words ('HumanWants') never replace a readable title.
    """
    lines = joined.split("\n")
    all_titles = [str(l["title"] or "") for m in modules for l in m["lessons"]]
    for m in modules:
        for l in m["lessons"]:
            t = str(l["title"] or "").strip()
            mm = LESSON_NO_RE.match(str(l["no"]))
            if not mm or not t:
                continue
            strict = bool(UNFINISHED_RE.search(t) or len(t) >= 86)
            hit = _find_lesson_in_text(lines, int(mm.group(1)), m["module"], mm.group(2) or "")
            if not hit:
                continue
            nt = hit[1]
            if not (len(t) < len(nt) <= 130):
                continue
            if not nt.lower().startswith(t[:12].lower()):
                continue
            if strict:
                l["title"] = nt
            elif not hit[2] and len(nt.split()) > len(t.split()) and not _suffix_polluted(
                    nt, t, [o for o in all_titles if o != t]):
                # same-line titles only: a wrapped continuation from a glued
                # two-column line can smuggle the neighbour column's words in
                l["title"] = nt


def _reconcile_missing(modules, joined, expected):
    """
    Every lesson number between the smallest and the largest must exist once.
    Recover the ones the table extraction dropped, put them in the module the
    page context points to, and tag TMA/PE so the counts match what the PDF
    states in its header.
    """
    seen = {}
    for m in modules:
        for l in m["lessons"]:
            mm = LESSON_NO_RE.match(str(l["no"]))
            if mm:
                seen.setdefault(int(mm.group(1)), m)
    if not seen:
        return []
    lo, hi = min(seen), max(seen)
    tot = expected.get("total")
    if tot and hi < tot and tot - hi <= 8:
        hi = tot
    missing = [n for n in range(lo, hi + 1) if n not in seen]
    if not missing or len(missing) > 10:
        return []
    lines = joined.split("\n")
    tma_want, pe_want = expected.get("tma"), expected.get("pe")
    tma_got = sum(1 for m in modules for l in m["lessons"] if l["kind"] == "TMA")
    pe_got = sum(1 for m in modules for l in m["lessons"] if l["kind"] == "PE")
    recovered = []
    for n in missing:
        probe = _find_lesson_in_text(lines, n)
        if not probe:
            continue
        idx = probe[0]
        mod = _module_by_context(lines, idx, modules) or seen.get(n - 1) or seen.get(n + 1)
        if mod is None:
            continue
        hit = _find_lesson_in_text(lines, n, mod["module"])
        if not hit:
            continue
        title = hit[1]
        if tma_want is not None and tma_got < tma_want:
            kind = "TMA"
        elif pe_want is not None and pe_got < pe_want:
            kind = "PE"
        else:
            kind = "TMA" if tma_want is None else "PE"
        if kind == "TMA":
            tma_got += 1
        else:
            pe_got += 1
        mod["lessons"].append({"no": "L-%d" % n, "title": title, "kind": kind})
        mod["lessons"].sort(key=_lsort_key)
        seen[n] = mod
        recovered.append(n)
    return recovered


def _compute_expected(joined, hdr_counts):
    """
    Read the lesson totals the PDF states (they verify the chapter tags).
    NIOS prints these in many styles:
      'Total no. of Lessons - 32'          grand total
      'Total Lessons (32)'                 grand total
      'Lessons- 24'                        glued grand total (no-space fonts)
      'Total No. of Lesson (12)' / '(16)'  one per column (213 style)
      '(No. of lessons 13) (No. of lessons 19)'  column counts
    """
    totals, splits = [], []
    for m in re.finditer(r"(total\s*)?no\.?\s*of\s*lessons?\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212:=]?\s*(\d{1,3})",
                         joined, re.I):
        (totals if m.group(1) else splits).append(int(m.group(2)))
    if not totals:
        m = re.search(r"total\s+lessons?\s*[\(\:\-]?\s*(\d{1,3})", joined, re.I)
        if m:
            totals.append(int(m.group(1)))
    expected = {}
    if totals:
        expected["total"] = totals[0]
    if len(splits) >= 2:
        expected["tma"] = splits[0]
        expected["pe"] = splits[1]
    if hdr_counts.get("tma") is not None:
        expected["tma"] = hdr_counts["tma"]
    if hdr_counts.get("pe") is not None:
        expected["pe"] = hdr_counts["pe"]
    if "total" not in expected:
        m = re.search(r"total\s*no\.?\s*of\s*lessons?\s*[=:\-\u2013]?\s*(\d{1,3})", joined, re.I)
        if not m:
            m = re.search(r"\u0915\u0941\s*\u0932\s*\u092a\u093e\u0920\s*[=:\-\u2013]?\s*(\d{1,3})", joined)
        if m:
            expected["total"] = int(m.group(1))
    if "tma" not in expected or "pe" not in expected:
        dev = [int(x) for x in re.findall(
            r"\u092a\u093e\u0920\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212]\s*(\d{1,3})", joined)]
        if len(dev) >= 2:
            expected.setdefault("tma", dev[0])
            expected.setdefault("pe", dev[1])
    # v102 rule A: 'Total No. of Lesson (12)' + '(16)' are the two columns.
    # Text extraction does not always keep the visual column order, so assign
    # each count by the heading nearest above it (TMA/40% vs Public/60%).
    col_ms = list(re.finditer(
        r"total\s+no\.?\s*of\s*lessons?\s*\(\s*(\d{1,3})\s*\)", joined, re.I))
    if len(col_ms) >= 2:
        tma_c, pe_c = None, None
        for cm in col_ms:
            ctx = joined[max(0, cm.start() - 260):cm.start()].lower()
            tma_at = max(ctx.rfind("tma"), ctx.rfind("40%"))
            pe_at = max(ctx.rfind("public"), ctx.rfind("term end"), ctx.rfind("termend"), ctx.rfind("60%"))
            val = int(cm.group(1))
            if tma_at >= 0 and tma_at > pe_at:
                tma_c = val if tma_c is None else tma_c
            elif pe_at >= 0:
                pe_c = val if pe_c is None else pe_c
        if tma_c is None and pe_c is None:
            tma_c, pe_c = int(col_ms[0].group(1)), int(col_ms[1].group(1))
        elif tma_c is None:
            tma_c = next((int(cm.group(1)) for cm in col_ms if int(cm.group(1)) != pe_c), None)
        elif pe_c is None:
            pe_c = next((int(cm.group(1)) for cm in col_ms if int(cm.group(1)) != tma_c), None)
        vals = [int(cm.group(1)) for cm in col_ms]
        if tma_c is not None:
            expected["tma"] = tma_c
        if pe_c is not None:
            expected["pe"] = pe_c
        if expected.get("total") in (None, tma_c, pe_c) and tma_c is not None and pe_c is not None:
            expected["total"] = tma_c + pe_c
    # v102 rule B: glued grand total 'Lessons- 24' (no-space fonts)
    g = [int(x) for x in re.findall(r"\blessons?\s*[\-\u2013]\s*(\d{1,3})", joined, re.I)]
    g = [x for x in g if 5 <= x <= 60 and x != expected.get("tma") and x != expected.get("pe")]
    if g:
        cand = max(g)
        cur = expected.get("total")
        if cur is None or (cand > cur and cand >= cur + (expected.get("tma") or 0)):
            expected["total"] = cand
    # v102 rule C: backfill a missing column count from the other two
    if expected.get("total") and expected.get("tma") and not expected.get("pe"):
        pe = expected["total"] - expected["tma"]
        if pe > 0:
            expected["pe"] = pe
    if expected.get("total") and expected.get("pe") and not expected.get("tma"):
        tma = expected["total"] - expected["pe"]
        if tma > 0:
            expected["tma"] = tma
    return expected


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
    warn_fallback = False
    try:
        for pageno, page in enumerate(pdf.pages[:40]):
            page_texts.append(page.extract_text() or "")
            for t in (page.extract_tables() or []):
                tables.append((0, pageno, t))
            # NIOS PDFs come in every flavour: ruled tables, partially ruled,
            # and plain text columns. Try each strategy and keep everything.
            for rank, opts in enumerate((
                    {"vertical_strategy": "lines", "horizontal_strategy": "text"},
                    {"vertical_strategy": "text", "horizontal_strategy": "lines"},
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}), start=1):
                try:
                    for t in (page.extract_tables(opts) or []):
                        tables.append((rank, pageno, t))
                except Exception:
                    pass
    finally:
        pdf.close()

    joined = "\n".join(page_texts).strip()
    if len(joined) < 60:
        return {"ok": False, "error": "No readable text found. This looks like a scanned PDF. Please download the text version from the NIOS website."}

    rows, hdr_counts = _find_bifurcation(tables)

    # NIOS sheets are often ruled so badly that table extraction shifts cells
    # between columns. Read the same page by word x-position too and keep
    # whichever method finds more lessons.
    pos_rows, pos_hdr = [], {}
    try:
        pdf2 = pdfplumber.open(io.BytesIO(data))
        try:
            pos_rows, pos_hdr = _position_rows(pdf2.pages)
        finally:
            pdf2.close()
    except Exception:
        pos_rows, pos_hdr = [], {}
    if _lesson_total(pos_rows) >= _lesson_total(rows):
        rows, hdr_counts = pos_rows, pos_hdr
    elif not hdr_counts:
        hdr_counts = pos_hdr

    if not rows or not any(_lessons(a) or _lessons(b) for _, a, b in rows):
        try:
            pdf2 = pdfplumber.open(io.BytesIO(data))
            try:
                fb = _words_fallback(pdf2.pages)
            finally:
                pdf2.close()
        except Exception:
            fb = []
        if fb and any(_lessons(a) or _lessons(b) for _, a, b in fb):
            rows = fb
            warn_fallback = True
        elif not rows:
            return {"ok": False,
                    "error": "Bifurcation of Syllabus table could not be located in this PDF. "
                             "Make sure you uploaded the syllabus PDF and not the sample question paper."}

    clean_names, embedded = [], []
    for r in rows:
        cn, em = _split_name_marks(r[0])
        clean_names.append(cn)
        embedded.append(em)
    wrows = _find_weightage(tables, page_texts)
    weights, warnings = _match_weightage(clean_names, wrows)
    if not wrows and any(e is not None for e in embedded):
        # module names themselves carry "(NN Marks)" - that IS the weightage
        weights = [e if e is not None else 0.0 for e in embedded]
        warnings = [w for w in warnings if not w.startswith("Weightage by Content table not found")]
        if any(e is None for e in embedded):
            warnings.append("Some modules have no marks printed in their name - please fill those manually.")
    else:
        # weightage table wins, embedded marks only fill the gaps
        weights = [w if w else (e or 0.0) for w, e in zip(weights, embedded)]

    modules, seen_no = [], set()
    dup, empty_mods, count_mismatch = [], [], []
    for (raw_name, tma_cell, pe_cell), w, cname in zip(rows, weights, clean_names):
        name = cname or raw_name
        mletter = MOD_LETTER_RE.match(raw_name or "")
        # option tags inside lesson cells ("7A. L- 29:") and OR connector lines
        tma_cell = CELL_TAG_RE.sub("", str(tma_cell or ""))
        pe_cell = CELL_TAG_RE.sub("", str(pe_cell or ""))
        tma_cell = "\n".join(l for l in tma_cell.split("\n") if not JUNK_CELL_RE.match(_clean(l)))
        pe_cell = "\n".join(l for l in pe_cell.split("\n") if not JUNK_CELL_RE.match(_clean(l)))
        lessons = []
        t_l = _lessons(tma_cell)
        p_l = _lessons(pe_cell)
        for no, letter, title in t_l:
            lessons.append({"no": no, "letter": letter, "title": title, "kind": "TMA"})
        for no, letter, title in p_l:
            lessons.append({"no": no, "letter": letter, "title": title, "kind": "PE"})
        # the PDF prints "(3 lessons)" at the bottom of each cell - use it
        for label, cell, got in (("TMA", tma_cell, len(t_l)), ("Public Examination", pe_cell, len(p_l))):
            want = _stated_count(cell)
            if want is not None and got < want:
                count_mismatch.append("%s, %s column: PDF says %d lessons but %d were read."
                                      % (name, label, want, got))
        if not lessons:
            # a leftover heading fragment is not worth reporting to the admin
            if not HEADER_CELL_RE.search(name) and len(name) > 2:
                empty_mods.append(name)
            continue
        lessons.sort(key=lambda x: (x["no"], x["letter"]))
        modules.append({
            "module": name,
            "weightage": float(w),
            "lessons": [{"no": "L-%d%s" % (l["no"], l["letter"]), "title": l["title"], "kind": l["kind"]}
                        for l in lessons],
            "_ml": (mletter.group(1), mletter.group(2).upper()) if mletter else None,
            "_raw": raw_name,
        })

    # some sheets print the option pair as the same module number twice with an
    # "or" between ("6. Analysis ... or" then "6. Application ...")
    LEAD_DIGIT_RE = re.compile(r"^\s*(?:module\s*[\-\u2013]?\s*)?(\d{1,2})\s*[\.\)\-\u2013:]", re.I)
    prev = None
    for m in modules:
        if m.get("_ml"):
            prev = None
            continue
        d = LEAD_DIGIT_RE.match(m.get("_raw") or "")
        if d and prev is not None:
            pd = LEAD_DIGIT_RE.match(prev.get("_raw") or "")
            joins = re.search(r"\bor\s*$", _clean(prev.get("_raw") or ""), re.I) or \
                    re.match(r"(?i)\s*or\b", _clean(m.get("_raw") or ""))
            if pd and pd.group(1) == d.group(1) and joins:
                prev["_ml"] = (d.group(1), "A")
                m["_ml"] = (d.group(1), "B")
                prev = None
                continue
        prev = m

    modules = _link_or_options(modules)
    for m in modules:
        m.pop("_raw", None)

    # drop lessons read twice (two extraction paths can see the same row) -
    # only after the option pairs were linked: both options may print the same
    # numbers and the linker letters them, so they are no longer duplicates
    for m in modules:
        kept = []
        for l in m["lessons"]:
            key = str(l["no"])
            if key in seen_no:
                dup.append(key)
                continue
            seen_no.add(key)
            kept.append(l)
        m["lessons"] = kept
    modules = [m for m in modules if m["lessons"] or m.get("optional_group")]

    # v102: repair what the table layout damaged before the counts are checked
    modules = _fix_module_names(modules)
    _rename_from_text(modules, joined)
    _repair_titles(modules, joined)
    pre_expected = _compute_expected(joined, hdr_counts)
    recovered = _reconcile_missing(modules, joined, pre_expected)
    if recovered:
        warnings.append("The table layout had dropped %d lesson(s); recovered from the page text: %s."
                        % (len(recovered), ", ".join("L-%d" % n for n in recovered)))

    all_lessons = [l for m in modules for l in m["lessons"]]
    pe = len([l for l in all_lessons if l["kind"] == "PE"])
    tma = len([l for l in all_lessons if l["kind"] == "TMA"])

    if dup:
        warnings.append("These lesson numbers appeared more than once: " + ", ".join(sorted(set(dup))))
    warnings.extend(count_mismatch)
    if empty_mods:
        warnings.append("These modules have no separate lessons in the PDF and were skipped: "
                        + ", ".join(empty_mods))
    if re.search(r"[\u0900-\u097F]", joined) and (
            re.search(r"[\x00-\x08\x0e-\x1f]", "".join(page_texts))
            or "(cid:" in "".join(page_texts)):
        warnings.append("This PDF uses a broken Devanagari font encoding, so some Hindi titles may be "
                        "missing matras. Please read through the chapter list and correct the spellings.")
    if warn_fallback:
        warnings.insert(0, "This PDF had no readable table, so the columns were detected by position. "
                           "Please check the chapter list carefully before saving.")
    if not all_lessons:
        return {"ok": False,
                "error": "The bifurcation table was found but no lessons could be read from it. "
                         "The lesson format in this PDF is not recognised. Send the PDF to support so it can be added."}

    expected = _compute_expected(joined, hdr_counts)
    # an OR pair counts once in the NIOS totals (the student sits one option)
    eff_total, eff_tma, eff_pe = _effective_counts(modules)
    # only an under-read is a real problem: extra entries are the OR variants,
    # which the admin can see and trim in the chapter list
    if expected.get("total") and eff_total < expected["total"]:
        warnings.append("The PDF says %d lessons in total but %d were read. Please check the chapter list below."
                        % (expected["total"], eff_total))
    if expected.get("tma") and eff_tma < expected["tma"]:
        warnings.append("The PDF lists %d TMA lessons but %d were read." % (expected["tma"], eff_tma))
    if expected.get("pe") and eff_pe < expected["pe"]:
        warnings.append("The PDF lists %d Public Examination lessons but %d were read." % (expected["pe"], eff_pe))

    paper = round(sum(m["weightage"] for m in modules), 2)
    if paper and paper not in (30, 40, 60, 70, 80, 85, 100):
        warnings.append("Module marks add up to %s. NIOS question papers are usually 30, 40, 60, 70, 80, 85 or 100 marks. Please verify." % paper)

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
        # an OR option pair is ONE exam slot (the student sits only one side),
        # so the headline counts show the pair once, exactly the way NIOS
        # prints the totals on the syllabus PDF
        "stats": {"total": eff_total, "pe": eff_pe, "tma": eff_tma,
                  "modules": len(modules) - sum(
                      len(ms) - 1 for ms in _groups_of(modules).values())},
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
