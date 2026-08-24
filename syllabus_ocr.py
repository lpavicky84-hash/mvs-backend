"""
Clean chapter/module names for Hindi (and other Devanagari) syllabus PDFs whose
text layer is garbled by a broken font ToUnicode map.

pdfplumber gives us the RELIABLE structure (module boundaries, lesson order,
marks, L-/T- split) but garbled NAMES. Rendering the page and running OCR gives
CLEAN names (but unreliable digits). So we keep pdfplumber's structure and only
swap in OCR's clean names, matched BY ORDER (never by the OCR digit, which is not
trustworthy). Everything here degrades gracefully: if the OCR toolchain is not
available the caller simply keeps the garbled-but-correct structure.
"""
import os
import re
import subprocess
import tempfile
import glob
import urllib.request

_DEVA = "\u0900-\u097F"
_HIN_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/hin.traineddata"


def _has(cmd):
    from shutil import which
    return which(cmd) is not None


def _tessdata_dir():
    """Return a dir that holds hin.traineddata, downloading it once if needed.
    Returns None if Hindi data cannot be made available."""
    # already installed?
    try:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                               text=True, timeout=20).stderr
        if re.search(r"^hin$", langs or "", re.M):
            return ""  # default tessdata already has hin
    except Exception:
        pass
    # cache dir
    cache = os.path.join(tempfile.gettempdir(), "mvs_tessdata")
    os.makedirs(cache, exist_ok=True)
    dst = os.path.join(cache, "hin.traineddata")
    if os.path.exists(dst) and os.path.getsize(dst) > 100000:
        return cache
    try:
        with urllib.request.urlopen(_HIN_URL, timeout=30) as r:
            data = r.read()
        if len(data) > 100000:
            with open(dst, "wb") as f:
                f.write(data)
            return cache
    except Exception:
        pass
    return None


def _have_fitz():
    try:
        import fitz  # noqa: F401
        return True
    except Exception:
        return False


def ocr_available():
    if not _has("tesseract"):
        return False
    return _has("pdftoppm") or _have_fitz()


def _render_pages(data, tmp):
    """Render each PDF page to a PNG path list. Prefers poppler (pdftoppm);
    falls back to PyMuPDF (fitz) so it still works when poppler is absent."""
    if _has("pdftoppm"):
        pdf_path = os.path.join(tmp, "in.pdf")
        with open(pdf_path, "wb") as f:
            f.write(data)
        try:
            subprocess.run(["pdftoppm", "-r", "300", "-png", pdf_path,
                            os.path.join(tmp, "pg")], timeout=120, check=True,
                           capture_output=True)
            imgs = sorted(glob.glob(os.path.join(tmp, "pg*.png")))
            if imgs:
                return imgs
        except Exception:
            pass
    if _have_fitz():
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            out = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(300 / 72.0, 300 / 72.0))
                p = os.path.join(tmp, "pg%03d.png" % i)
                pix.save(p)
                out.append(p)
            return out
        except Exception:
            pass
    return []


def ocr_pdf_text(data: bytes):
    """Render each page at 300 DPI and OCR in Hindi. Returns the full text, or
    '' if the toolchain / language data is not available."""
    if not ocr_available():
        return ""
    tdir = _tessdata_dir()
    if tdir is None:
        return ""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return ""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        imgs = _render_pages(data, tmp)
        if not imgs:
            return ""
        cfg = "--psm 6"
        if tdir:
            cfg += " --tessdata-dir " + tdir
        for img in imgs:
            try:
                out.append(pytesseract.image_to_string(Image.open(img),
                                                        lang="hin", config=cfg))
            except Exception:
                pass
    return "\n".join(out)


def _strip_junk(ln):
    """Drop OCR table-border / stray-glyph noise, keep readable text."""
    ln = re.sub(r"[\u0964\u0965]", " ", ln)          # dandas
    ln = re.sub(r"[|_>\u00ab\u00bb\[\]{}=()\"'*<]+", " ", ln)
    ln = re.sub(r"\s+", " ", ln).strip()
    return ln


def _deva_ratio(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "\u0900" <= c <= "\u097F") / len(letters)


_TAIL_RE = re.compile(r"\s*(?:काव्य\s*खंड|गद्य\s*खंड|लेखन\s*कौशल)?\s*[-\u2013\u2014]?\s*"
                      r"[\d\u0966-\u096F]{1,3}\s*अंक.*$")


def _clean_title(t):
    """Tidy a single OCR title: drop trailing 'section - N अंक', leading orphan
    matras/halants (OCR artefacts like '्््ि'), and non-Devanagari garble tokens."""
    t = _TAIL_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" .\u2013\u2014:-")
    t = re.sub(r"^[\u093E-\u094D\u0900-\u0903]+\s+", "", t)
    keep = [w for w in t.split(" ") if re.search(r"[\u0900-\u097F]", w)]
    return re.sub(r"\s+", " ", " ".join(keep)).strip(" .\u2013\u2014:-")


_LESSON_RE = re.compile(r"पाठ[\s\u0964\u0965]*[\d\u0966-\u096F]*[\s\u0964\u0965]*[-\u2013\u2014:]\s*(.+)")
_GRAM_RE = re.compile(r"(?:^|\s)[\d\u0966-\u096F]{1,2}\s*[.)]\s*([\u0900-\u097F].*)")
_APATHIT_RE = re.compile(r"(अपठित\s*काव्यांश|अपठित\s*गद्यांश)")


def clean_streams_from_ocr(text):
    """Return three ORDERED streams of clean names, read from the DETAIL part of a
    Hindi bifurcation PDF (the top weightage summary is skipped):
      book    -> titles from 'पाठ N - title' rows (the real book chapters)
      unseen  -> 'अपठित काव्यांश' / 'अपठित गद्यांश' rows, in order
      grammar -> 'N. topic' rows under व्याकरण, in order
    Each is matched to pdfplumber's structure BY ORDER, never by the OCR digit."""
    lines = [_strip_junk(l) for l in (text or "").splitlines()]
    book, unseen, grammar, modules = [], [], [], []
    started = False
    in_grammar = False

    def _add_mod(nm):
        nm = re.sub(r"\s+", " ", nm).strip()
        if nm and (not modules or modules[-1] != nm):
            modules.append(nm)

    for ln in lines:
        if not ln:
            continue
        if not started:
            if _LESSON_RE.search(ln):
                started = True
            else:
                continue
        # ordered module (section) names, taken from detail rows
        msec = re.search(r"(काव्य\s*खंड|गद्य\s*खंड|लेखन\s*कौशल|अपठित\s*काव्यांश|"
                         r"अपठित\s*गद्यांश|व्याकरण)", ln)
        if msec:
            _add_mod(msec.group(1))
        ma = _APATHIT_RE.search(ln)
        if ma and "पाठ" not in ln:
            unseen.append(re.sub(r"\s+", " ", ma.group(1)))
            continue
        ml = _LESSON_RE.search(ln)
        if ml:
            t = _clean_title(ml.group(1))
            if t and _deva_ratio(t) >= 0.5:
                book.append(t)
            continue
        if re.search(r"व्याकरण|5ाकरण|सर्वनाम|विशेषण|विलोम|कारक|उपसर्ग|प्रत्यय|"
                     r"मुहावरे|समास|संधि|विराम\s*चिह्न|वाक्य\s*परिवर्तन|शब्द-?भंडार", ln):
            in_grammar = True
        if in_grammar:
            mg = _GRAM_RE.search(ln)
            if mg:
                t = _clean_title(mg.group(1))
                if t and _deva_ratio(t) >= 0.5:
                    grammar.append(t)
                continue
        if book and not in_grammar and not re.search(r"[\d\u0966-\u096F]", ln) \
                and 4 <= len(ln) <= 22 and _deva_ratio(ln) >= 0.85 \
                and ln not in ("कौशल", "खंड", "पाठ", "अंक", "कुल", "विषय", "भाग") \
                and not re.search(r"खंड|कौशल|अंक|कुल", ln):
            frag = re.sub(r"^[\u093E-\u094D\u0900-\u0903]+\s*", "", ln).strip()
            if frag:
                book[-1] = (book[-1] + " " + frag).strip()
    return {"book": book, "unseen": unseen, "grammar": grammar, "modules": modules}


def apply_ocr_names(modules_struct, ocr_text):
    """Given pdfplumber's module structure (garbled names but correct shape) and
    OCR text, swap in clean names BY ORDER — but only per-stream when the OCR count
    exactly matches the structure count (so a noisy stream is left untouched rather
    than mis-assigned). Returns the number of names replaced."""
    s = clean_streams_from_ocr(ocr_text)
    changed = 0
    book_lessons = [l for m in modules_struct for l in m["lessons"]
                    if not str(l.get("no", "")).startswith("T-")]
    if s["book"] and len(s["book"]) == len(book_lessons):
        for les, t in zip(book_lessons, s["book"]):
            les["title"] = t; changed += 1
    unseen_mods = [m for m in modules_struct
                   if len(m["lessons"]) == 1 and str(m["lessons"][0].get("no", "")).startswith("T-")]
    if s["unseen"] and len(s["unseen"]) == len(unseen_mods):
        for m, nm in zip(unseen_mods, s["unseen"]):
            m["lessons"][0]["title"] = nm; changed += 1
    gram_mods = [m for m in modules_struct if len(m["lessons"]) > 1
                 and all(str(l.get("no", "")).startswith("T-") for l in m["lessons"])]
    for gm in gram_mods:
        if s["grammar"] and len(s["grammar"]) == len(gm["lessons"]):
            for l, t in zip(gm["lessons"], s["grammar"]):
                l["title"] = t; changed += 1
    # module (section) names — by TYPE, so a partly-noisy OCR still names most of
    # them: unseen modules mirror their (clean) lesson title; a grammar module is
    # व्याकरण; the remaining book modules take the ordered book-section names.
    book_sec = [nm for nm in s["modules"] if "अपठित" not in nm and "व्याकरण" not in nm]
    bi = 0
    for m in modules_struct:
        les = m["lessons"]
        is_unseen = len(les) == 1 and str(les[0].get("no", "")).startswith("T-")
        is_gram = len(les) > 1 and all(str(l.get("no", "")).startswith("T-") for l in les)
        if is_unseen and les[0].get("title") and _deva_ratio(les[0]["title"]) >= 0.8:
            m["module"] = les[0]["title"]; changed += 1
        elif is_gram:
            m["module"] = "व्याकरण"; changed += 1
        elif bi < len(book_sec):
            m["module"] = book_sec[bi]; bi += 1; changed += 1
    return changed
