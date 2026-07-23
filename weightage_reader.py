"""
MVS Foundation CRM - Weightage by Content reader.

Two ways to get module marks into the tracker when the syllabus PDF has no
Weightage by Content table:

  1. paste the table as text            -> parse_weightage_text()
  2. paste or upload a screenshot of it -> parse_weightage_image()

Both return the same shape:

    {"ok": True, "rows": [{"module": "...", "marks": 14.0}, ...],
     "total": 80.0, "source": "text" | "image", "warnings": [...]}

The image path needs the tesseract binary. If it is not installed the call
returns ok False with a clear message, and the text path still works, so the
feature degrades instead of breaking.
"""

import io
import re

try:
    import pytesseract
    from PIL import Image
    HAVE_OCR = True
except ImportError:
    HAVE_OCR = False


TOTAL_WORDS = re.compile(r"^\s*(total|कुल|कु ल|योग|sum)\b", re.I)
NOISE_WORDS = re.compile(
    r"^\s*(sl\.?|s\.?\s*no\.?|sr\.?|module|content|unit|chapter|marks|weightage|"
    r"क्र\.?|मॉड्यूल|अंक)\s*$", re.I)


def _clean(s):
    s = re.sub(r"\(cid\s*:\s*\d+\)", "", str(s or ""))
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_lead(name):
    """Drop a leading serial number or module label."""
    n = _clean(name)
    n = re.sub(r"^\s*\d{1,2}\s*[\.\)]\s*", "", n)
    n = re.sub(r"^\s*module\s*[\-\u2013]?\s*(?:[IVXLC]+|\d{1,2})\b[\.\):]?\s*", "", n, flags=re.I)
    return n.strip(" .:-\u2013")


def _rows_from_lines(lines):
    """Every line that ends with a number becomes one module row."""
    rows, warnings = [], []
    for raw in lines:
        line = _clean(raw)
        if not line or NOISE_WORDS.match(line):
            continue
        # marks sit at the end of the line, optionally after a separator
        m = re.search(r"[\|\t;,:\s]\s*(\d{1,3}(?:\.\d+)?)\s*$", line)
        if not m:
            m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)\s*$", line)
            if m and rows:
                # a bare number on its own line belongs to the previous name
                if rows[-1]["marks"] is None:
                    rows[-1]["marks"] = float(m.group(1))
                    continue
            if re.search(r"[A-Za-z\u0900-\u097F]{3}", line):
                rows.append({"module": _strip_lead(line), "marks": None})
            continue
        name = _strip_lead(line[: m.start()])
        marks = float(m.group(1))
        if TOTAL_WORDS.match(name) or not name:
            continue
        rows.append({"module": name, "marks": marks})

    out = [r for r in rows if r["marks"] is not None]
    dropped = len(rows) - len(out)
    if dropped:
        warnings.append("%d line(s) had no marks and were skipped." % dropped)
    return out, warnings


def _lines_from_words(data, tol=14):
    """
    Rebuild table rows from OCR word boxes.

    Reading a table with image_to_string often returns the name column and the
    marks column as two separate blocks, because the gap between them is wide.
    Grouping words by their y position puts each printed row back together.
    """
    words = []
    n = len(data.get("text", []))
    for i in range(n):
        t = _clean(data["text"][i])
        if not t:
            continue
        try:
            conf = float(data.get("conf", ["-1"] * n)[i])
        except (TypeError, ValueError):
            conf = -1
        if conf >= 0 and conf < 35:
            continue
        words.append((int(data["top"][i]), int(data["left"][i]), t))
    if not words:
        return []
    words.sort()
    lines, cur, cur_top = [], [], None
    for top, left, t in words:
        if cur_top is None or abs(top - cur_top) <= tol:
            cur.append((left, t))
            cur_top = top if cur_top is None else cur_top
        else:
            lines.append(cur); cur = [(left, t)]; cur_top = top
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:
        ln.sort()
        out.append(" ".join(t for _, t in ln))
    return out


def _pair_by_order(lines):
    """
    Fallback when a screenshot was read column by column: all the names come
    first, then all the numbers. If the two counts match, pair them in order.
    """
    names, nums = [], []
    for raw in lines:
        line = _clean(raw)
        if not line or NOISE_WORDS.match(line) or TOTAL_WORDS.match(line):
            continue
        if re.fullmatch(r"\d{1,3}(?:\.\d+)?", line):
            nums.append(float(line))
        elif re.search(r"[A-Za-z\u0900-\u097F]{3}", line):
            names.append(_strip_lead(line))
    if not names or not nums:
        return [], []
    warn = ["The screenshot was read column by column, so the names and the marks were "
            "paired in order. Please check each row carefully."]
    if len(names) != len(nums):
        k = min(len(names), len(nums))
        warn.append("%d module names and %d numbers were found, so only the first %d were paired."
                    % (len(names), len(nums), k))
        names, nums = names[:k], nums[:k]
    return [{"module": names[i], "marks": nums[i]} for i in range(len(names))], warn


def parse_weightage_text(text):
    """Read a Weightage by Content table that was pasted as text."""
    if not _clean(text):
        return {"ok": False, "error": "Nothing was pasted."}
    rows, warnings = _rows_from_lines(str(text).splitlines())
    if not rows:
        return {"ok": False,
                "error": "No module and marks pairs could be read. Each line should have the "
                         "module name followed by its marks, for example: Optics 14"}
    total = round(sum(r["marks"] for r in rows), 2)
    if total and total not in (30, 40, 60, 70, 80, 85, 100):
        warnings.append("The marks add up to %s. NIOS question papers are usually 30, 40, 60, "
                        "70, 80, 85 or 100 marks. Please check." % total)
    return {"ok": True, "rows": rows, "total": total, "source": "text", "warnings": warnings}


def parse_weightage_image(data, langs="eng+hin"):
    """Read a Weightage by Content table from a screenshot."""
    if not HAVE_OCR:
        return {"ok": False,
                "error": "Screenshot reading is not enabled on this server. Add tesseract-ocr to "
                         "the deployment and pytesseract to requirements.txt, or paste the table "
                         "as text instead."}
    try:
        img = Image.open(io.BytesIO(data))
    except Exception as exc:
        return {"ok": False, "error": "That file could not be opened as an image. " + str(exc)[:120]}

    try:
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # upscale small screenshots, OCR is much better above roughly 1000px wide
        if img.width < 1000:
            img = img.resize((img.width * 2, img.height * 2))
        data = None
        for lang in (langs, "eng"):
            try:
                data = pytesseract.image_to_data(img, lang=lang,
                                                 output_type=pytesseract.Output.DICT)
                if any(_clean(t) for t in data.get("text", [])):
                    break
            except Exception:
                data = None
    except Exception as exc:
        return {"ok": False, "error": "The screenshot could not be read. " + str(exc)[:140]}

    if not data or not any(_clean(t) for t in data.get("text", [])):
        return {"ok": False,
                "error": "No text was found in this screenshot. Try a larger and sharper crop of "
                         "just the Weightage by Content table."}

    lines = _lines_from_words(data)
    rows, warnings = _rows_from_lines(lines)
    if not rows:
        rows, warnings = _pair_by_order(lines)
    if not rows:
        return {"ok": False,
                "error": "Text was found but no module and marks pairs. Crop the screenshot to "
                         "just the table, or paste the table as text instead."}
    total = round(sum(r["marks"] for r in rows), 2)
    warnings.insert(0, "These numbers were read from a screenshot. Please check every module "
                       "before saving.")
    if total and total not in (30, 40, 60, 70, 80, 85, 100):
        warnings.append("The marks add up to %s, which is not a usual NIOS paper total." % total)
    return {"ok": True, "rows": rows, "total": total, "source": "image", "warnings": warnings}
