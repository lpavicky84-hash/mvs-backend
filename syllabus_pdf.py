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
    r"^\s*(?:module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2})\b[\.\):]?\s*|\d{1,2}\s*[\.\)]\s*)", re.I)

# A cell that begins a new module: "Module I", "Module-2", "1." or "1)"
MODULE_START_RE = re.compile(
    r"^\s*(?:module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2})\b|\d{1,2}\s*[\.\)]\s*\S)", re.I)

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


MARKER_RE = re.compile(
    r"\b(?:Lesson|Chapter|Ch|L)\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212\.:]?\s*(\d{1,2})\b", re.I)


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
    c = re.sub(r"[\-\u2013\u2014\s]+$", "", c)
    c = re.sub(r"^[\)\]]\s*", "", c)
    return _clean(c)


def _lessons(cell):
    """
    Pull [(no, title)] out of one table cell.

    Three layouts are supported:
      bracketed   L-3 (Laws of Motion)   /  Lesson-1 (Atoms and Molecules)
      bare        Lesson-21 d-Block and f-Block Elements
      language    3 : Gillu              (one lesson per line)
    """
    raw = "" if cell is None else str(cell)
    raw = re.sub(r"\(cid\s*:\s*\d+\)", "", raw)
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw)
    txt = _clean(raw)
    if not txt or txt in {"-", "_", "--", "\u2013", "\u2014"}:
        return []

    out = []
    marks = list(MARKER_RE.finditer(txt))
    for i, m in enumerate(marks):
        chunk = txt[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(txt)]
        title = _title_from_chunk(chunk)
        if len(title) >= 3:
            out.append((int(m.group(1)), title))

    # language subjects: a numbered line starts a lesson, the lines after it
    # continue the same title until the next number
    if not out:
        cur_no, cur_title = None, []
        def _flush():
            if cur_no is not None:
                t = COUNT_MARK_RE.sub("", " ".join(cur_title)).strip(" .:\u0964-\u2013\u2014")
                if t:
                    out.append((cur_no, _clean(t)))
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
                c = _clean(cell)
                if re.search(r"no\.?\s*of\s*lessons", c, re.I):
                    m = re.search(r"no\.?\s*of\s*lessons\s*\)?\s*[:=\-\u2013]?\s*(\d{1,3})", c, re.I)
                    if m:
                        hdr[key] = int(m.group(1))
            joined = " ".join(_clean(x) for x in (name, tma_cell, pe_cell))
            has_lesson = bool(_lessons(tma_cell) or _lessons(pe_cell))
            if not has_lesson and HEADER_CELL_RE.search(joined):
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
            nm = _module_name(name)
            if not nm or not re.search(r"[A-Za-z]{3}|[\u0900-\u097F]{2}", nm):
                nm = nm or "Module"
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
            if pe_x is None and re.fullmatch(r"Public", t, re.I):
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
            # a leftover heading fragment is not worth reporting to the admin
            if not HEADER_CELL_RE.search(name) and len(name) > 2:
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
        if not m:
            # कुल पाठ : 25
            m = re.search(r"\u0915\u0941\s*\u0932\s*\u092a\u093e\u0920\s*[=:\-\u2013]?\s*(\d{1,3})", joined)
        if m:
            expected["total"] = int(m.group(1))
    if "tma" not in expected or "pe" not in expected:
        # the column counts print as  पाठ - 9   and   पाठ – 16  with a dash.
        # the grand total uses a colon (कुल पाठ : 25) so a dash only match keeps
        # the two apart even when the font mangles कुल into "कु ल".
        dev = [int(x) for x in re.findall(
            r"\u092a\u093e\u0920\s*[\-\u2010\u2011\u2012\u2013\u2014\u2212]\s*(\d{1,3})", joined)]
        if len(dev) >= 2:
            expected["tma"] = dev[0]
            expected["pe"] = dev[1]
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
