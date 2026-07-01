"""
Premium question+answer PDF generator for MVS Foundation (English + Hindi medium).
Uses fpdf2 + uharfbuzz shaping with a bundled Noto Sans Devanagari font.
Turns run-on AI answers into cleanly structured blocks (headings, bullets,
steps, centered equations, highlighted final answer) with a premium layout.
"""
import os, re, io, base64


def _font_path():
    """NotoSansDevanagari-Regular.ttf (the original file) is a VARIABLE font
    (wght/wdth axes). fpdf2's glyph-subset embedding can corrupt complex Devanagari
    conjuncts from a variable font (e.g. 'ज्ञ' rendering as a stray dot) even though
    HarfBuzz shapes it correctly. NotoSansDevanagari-Static.ttf is the same font
    frozen to its default instance (fontTools varLib.instancer) - a plain static
    TrueType font that embeds reliably. Prefer it; fall back to the variable file
    (and finally the bare filename) so deploys stay backward compatible."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    for name in ["NotoSansDevanagari-Static.ttf", "NotoSansDevanagari-Regular.ttf"]:
        candidates += [
            os.path.join(here, "fonts", name), os.path.join(here, name),
            os.path.join(os.getcwd(), "fonts", name), os.path.join(os.getcwd(), name),
            "fonts/%s" % name, name,
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "NotoSansDevanagari-Static.ttf"


def _logo_path():
    """Optional premium header logo. Drop a file named logo.png (or logo.jpg) in the
    repo root or fonts/ folder and it will appear in the PDF header automatically -
    no code change needed. If absent, the header stays text-only (current look)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ["logo.png", "logo.jpg", "logo.jpeg"]:
        for c in [os.path.join(here, name), os.path.join(here, "fonts", name),
                  os.path.join(os.getcwd(), name), name]:
            if os.path.exists(c):
                return c
    return None


# ---------------------------------------------------------------- LaTeX cleanup
_TEX_MAP = [
    (r"\\times", "\u00d7"), (r"\\cdot", "\u00b7"), (r"\\div", "\u00f7"),
    (r"\\pm", "\u00b1"), (r"\\mp", "\u2213"), (r"\\circ", "\u00b0"), (r"\\degree", "\u00b0"),
    (r"\\alpha", "\u03b1"), (r"\\beta", "\u03b2"), (r"\\gamma", "\u03b3"),
    (r"\\theta", "\u03b8"), (r"\\phi", "\u03c6"), (r"\\pi", "\u03c0"),
    (r"\\Delta", "\u0394"), (r"\\delta", "\u03b4"), (r"\\lambda", "\u03bb"),
    (r"\\mu", "\u03bc"), (r"\\omega", "\u03c9"), (r"\\Omega", "\u03a9"),
    (r"\\rho", "\u03c1"), (r"\\sigma", "\u03c3"), (r"\\tau", "\u03c4"),
    (r"\\infty", "\u221e"), (r"\\rightarrow", "\u2192"), (r"\\to", "\u2192"),
    (r"\\Rightarrow", "\u21d2"), (r"\\leftarrow", "\u2190"),
    (r"\\geq", "\u2265"), (r"\\leq", "\u2264"), (r"\\neq", "\u2260"),
    (r"\\approx", "\u2248"), (r"\\sum", "\u03a3"), (r"\\sqrt", "\u221a"),
]
_SUP = {"0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3", "4": "\u2074",
        "5": "\u2075", "6": "\u2076", "7": "\u2077", "8": "\u2078", "9": "\u2079",
        "+": "\u207a", "-": "\u207b", "n": "\u207f"}
_SUB = {"0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083", "4": "\u2084",
        "5": "\u2085", "6": "\u2086", "7": "\u2087", "8": "\u2088", "9": "\u2089"}


def _supsub(t):
    def sup(m):
        s = m.group(1)
        return "".join(_SUP.get(c, "^" + c) for c in s) if all(c in _SUP for c in s) else "^" + s
    def sub(m):
        s = m.group(1)
        return "".join(_SUB.get(c, "_" + c) for c in s) if all(c in _SUB for c in s) else "_" + s
    t = re.sub(r"\^\{([^{}]*)\}", sup, t)
    t = re.sub(r"\^([0-9n+\-])", lambda m: _SUP.get(m.group(1), "^" + m.group(1)), t)
    t = re.sub(r"_\{([^{}]*)\}", sub, t)
    t = re.sub(r"_([0-9])", lambda m: _SUB.get(m.group(1), "_" + m.group(1)), t)
    return t


def _clean(text):
    t = text or ""
    t = re.sub(r"\\ce\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\(text|mathrm|mathbf|bf|textbf|textit|mathit)\{([^{}]*)\}", r"\2", t)
    t = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", "\u221a(\\1)", t)
    for pat, rep in _TEX_MAP:
        t = re.sub(pat, rep, t)
    t = _supsub(t)
    t = t.replace("\\\\", "\n").replace("$", "")
    t = re.sub(r"\\[,;:! ]", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


# ------------------------------------------------------ structure the run-on text
_HEADINGS = [
    "Statement:", "Given Data:", "Given:", "Data:", "Solution:", "Required:",
    "To Find:", "Formula:", "Formula used:", "Substitute the values:",
    "Rearranging the formula to find acceleration:", "Rearranging:",
    "Concept Check:", "Note:", "Therefore:", "Hence:", "Conclusion:",
    "The Smart Strategy (Law of Conservation of Energy):", "The Smart Strategy:",
    "According to Newton's Second Law of Motion:",
]
_HEAD_HI = ["\u0915\u0925\u0928:", "\u0926\u093f\u092f\u093e \u0917\u092f\u093e:",
            "\u0939\u0932:", "\u0938\u0942\u0924\u094d\u0930:",
            "\u0905\u0902\u0924\u093f\u092e \u0909\u0924\u094d\u0924\u0930:",
            "\u0928\u094b\u091f:", "\u0905\u0924:"]


_RUNON_RE = re.compile(
    r"[a-z0-9\)\]\.:\u00b2\u00b3][A-Z]"                    # wordEndCapital merge, no space
    r"|\b(?:N|J|V|A|W|Hz|Pa|C)[A-Z][a-z]"                  # unit+CapitalWord merge
    r"|:[a-zA-Z]\s*="                                       # ":x =" (heading glued to formula)
)
_HEAD_MIDLINE_RE = re.compile(
    "|".join(re.escape(h) for h in (_HEADINGS + _HEAD_HI))
)


def _strip_math(line):
    return re.sub(r"\$[^$]*\$", "", line)


def _looks_runon(line):
    """True if a line still looks like several sentences/headings glued together
    without proper separation - i.e. it needs the heuristic splitter below. A line
    that already came in on its own (from well-structured AI/teacher input) will
    not match this and is left exactly as written."""
    probe = _strip_math(line)
    if _RUNON_RE.search(probe):
        return True
    # a heading marker appearing anywhere OTHER than the very start of the line
    # means it's still stuck to the previous sentence
    m = _HEAD_MIDLINE_RE.search(probe)
    if m and m.start() > 0:
        return True
    return False


def _heuristic_split(line):
    """Best-effort splitter for a still-run-on line (legacy data, or AI output that
    didn't fully follow the line-break instructions). Not applied to lines that
    already look clean, so it can no longer mangle properly formatted text."""
    parts = re.split(r"(\$[^$]*\$)", line)
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            out.append(seg)
            continue
        # protect multi-word headings so camelCase split does not break them
        ph = {}
        for idx, h in enumerate(_HEADINGS + _HEAD_HI):
            if h in seg:
                tok = "\x00H%d\x00" % idx
                ph[tok] = h
                seg = seg.replace(h, "\n" + tok + "\n")
        seg = re.sub(r"\s*(Step\s+\d+\s*:)", r"\n\1", seg)
        seg = re.sub(r"\s*(Final Answer\s*:)", r"\nFinal Answer: ", seg)
        seg = re.sub(r"\s*(\u0905\u0902\u0924\u093f\u092e \u0909\u0924\u094d\u0924\u0930\s*:)",
                     r"\n\1 ", seg)
        seg = re.sub(r"([a-z0-9\)\]\.:\u00b2\u00b3])([A-Z])", r"\1\n\2", seg)
        seg = re.sub(r"\b(N|J|V|A|W|Hz|Pa|C)([A-Z][a-z])", r"\1\n\2", seg)
        seg = re.sub(r":([a-zA-Z]\s*=)", r":\n\1", seg)
        seg = re.sub(r"(?<!\d)\s+(\d+\.)\s+(?=[A-Z\u0900-\u097F])", r"\n\1 ", seg)
        for tok, h in ph.items():
            seg = seg.replace(tok, h)
        out.append(seg)
    merged = re.sub(r"\n{2,}", "\n", "".join(out))
    return [ln.strip() for ln in merged.split("\n") if ln.strip()]


def _presplit(text):
    """Split source text into display lines. Real newlines from the source (typed,
    pasted, or AI-generated with the structured-line-break instruction) are trusted
    as-is - the exact line breaks the teacher copied from ChatGPT/Word etc. are
    preserved 1:1. Only a line that still looks glued-together falls back to the
    regex heuristic splitter."""
    raw_lines = re.split(r"\r?\n", text or "")
    out = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        if _looks_runon(line):
            out.extend(_heuristic_split(line))
        else:
            out.append(line)
    return out


def _blocks(text):
    blocks = []
    for ln in _presplit(text):
        low = ln.lower()
        c = _clean(ln)
        if not c:
            continue
        if low.startswith("final answer") or low.startswith(
                "\u0905\u0902\u0924\u093f\u092e \u0909\u0924\u094d\u0924\u0930"):
            blocks.append(("final", c, ln))
        elif re.match(r"^step\s+\d+\s*:", low) or ln.rstrip().endswith(":") \
                or any(low.startswith(h.lower()) for h in _HEADINGS + _HEAD_HI):
            blocks.append(("head", c, ln))
        elif re.match(r"^[\-\u2022\u25e6]\s+", ln):
            blocks.append(("bullet", re.sub(r"^[\-\u2022\u25e6]\s+", "", c), ln))
        elif "=" in c and len(c) < 46 and not c.rstrip().endswith(":") \
                and not re.search(r"[A-Za-z\u0900-\u097F]{4,}", c.split("=")[0]):
            blocks.append(("eq", c, ln))
        else:
            blocks.append(("para", c, ln))
    # absorb the value lines that follow "Final Answer:" into the highlighted box
    merged = []
    i = 0
    while i < len(blocks):
        k, c, raw = blocks[i]
        if k == "final":
            parts = [c]
            j = i + 1
            while j < len(blocks) and blocks[j][0] in ("para", "eq", "bullet"):
                parts.append(blocks[j][1])
                j += 1
            merged.append(("final", "   \u00b7   ".join(parts), raw))
            i = j
        else:
            merged.append((k, c, raw))
            i += 1
    # items listed under a "Given Data / Required / To Find" heading become bullets
    out = []
    in_data = False
    for k, c, raw in merged:
        if k == "head":
            lc = c.lower()
            in_data = lc.startswith(("given", "data", "to find", "required", "list"))
            out.append((k, c, raw))
        elif in_data and k == "para":
            out.append(("bullet", c, raw))
        else:
            if k in ("eq", "final"):
                in_data = False
            out.append((k, c, raw))
    return out


# -------------------------------------------------------------------- image embed
def _img(pdf, b64str):
    if not b64str:
        return
    try:
        raw = b64str
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        data = base64.b64decode(raw)
        pdf.ln(1)
        x = pdf.get_x()
        pdf.image(io.BytesIO(data), w=min(85, pdf.epw * 0.55))
        pdf.set_x(x)
        pdf.ln(2)
    except Exception:
        pass


# ---------------------------------------------------------------------- palette
NAVY = (17, 40, 74)
NAVY2 = (32, 66, 116)
GREEN = (22, 122, 74)
GREENBG = (232, 248, 240)
AMBER = (183, 121, 8)
GREY = (110, 116, 128)
LIGHT = (243, 245, 249)
BORDER = (223, 227, 235)
EQBG = (244, 246, 251)


def build_exam_pdf(ex, questions, medium="english"):
    from fpdf import FPDF
    is_hi = (medium == "hindi")
    L = {
        "q":       ("\u092a\u094d\u0930. " if is_hi else "Q"),
        "marks":   ("\u0905\u0902\u0915" if is_hi else "marks"),
        "answer":  ("\u0909\u0924\u094d\u0924\u0930" if is_hi else "ANSWER"),
        "correct": ("\u2713 \u0938\u0939\u0940" if is_hi else "Correct"),
        "medium":  ("\u0939\u093f\u0902\u0926\u0940 \u092e\u093e\u0927\u094d\u092f\u092e" if is_hi else "English Medium"),
        "total":   ("\u0915\u0941\u0932 \u0905\u0902\u0915" if is_hi else "Total Marks"),
        "qpaper":  ("\u092a\u094d\u0930\u0936\u094d\u0928 \u092a\u0924\u094d\u0930 (\u0909\u0924\u094d\u0924\u0930 \u0938\u0939\u093f\u0924)" if is_hi else "QUESTION PAPER WITH ANSWER KEY"),
    }
    FONT = _font_path()

    class PDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Noto", size=8)
            self.set_text_color(*GREY)
            self.cell(0, 6, "MVS Foundation  \u00b7  %s" % (ex.teacher_name or ""), align="L")
            self.set_y(-12)
            self.cell(0, 6, "Page %d" % self.page_no(), align="R")

    pdf = PDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("Noto", "", FONT)
    pdf.add_page()
    pdf.set_text_shaping(True)
    EPW = pdf.epw
    LM = pdf.l_margin

    # ---- header band (premium: navy band + amber accent strip, optional logo)
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, 33, style="F")
    pdf.set_fill_color(*AMBER)
    pdf.rect(0, 33, pdf.w, 1.5, style="F")
    text_x = LM
    logo = _logo_path()
    if logo:
        try:
            logo_h = 19
            pdf.image(logo, x=LM, y=7, h=logo_h)
            # measure width so the title text starts cleanly after the logo
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(logo) as _im:
                    lw, lh = _im.size
                text_x = LM + (logo_h * lw / lh) + 6
            except Exception:
                text_x = LM + 24
        except Exception:
            text_x = LM
    pdf.set_xy(text_x, 7)
    pdf.set_font("Noto", size=17)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(pdf.w - pdf.r_margin - text_x, 9, _clean(ex.title or "Test"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(text_x)
    pdf.set_font("Noto", size=9)
    pdf.set_text_color(200, 210, 228)
    pdf.cell(pdf.w - pdf.r_margin - text_x, 5.5, "%s    \u00b7    %s    \u00b7    %s: %s" % (
        ex.subject or "", L["medium"], L["total"], ex.total_marks),
        new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(text_x)
    pdf.set_font("Noto", size=7.5)
    pdf.set_text_color(*AMBER)
    pdf.cell(pdf.w - pdf.r_margin - text_x, 5, L["qpaper"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(LM, 40)

    for q in questions:
        qtext = (q.question_text_hi if (is_hi and q.question_text_hi) else q.question_text) or ""
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()

        y0 = pdf.get_y()
        # question badge
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Noto", size=11)
        badge = "%s%d" % (L["q"], q.q_no)
        bw = pdf.get_string_width(badge) + 8
        pdf.rect(LM, y0, bw, 8, style="F", round_corners=True, corner_radius=2)
        pdf.set_xy(LM, y0 + 0.6)
        pdf.cell(bw, 6.8, badge, align="C")
        # marks pill
        pill = "%d %s" % (q.max_marks, L["marks"])
        pdf.set_font("Noto", size=8.5)
        pw = pdf.get_string_width(pill) + 8
        pdf.set_fill_color(*LIGHT)
        pdf.set_draw_color(*BORDER)
        pdf.rect(pdf.w - pdf.r_margin - pw, y0, pw, 8, style="DF", round_corners=True, corner_radius=2)
        pdf.set_xy(pdf.w - pdf.r_margin - pw, y0 + 0.6)
        pdf.set_text_color(*GREY)
        pdf.cell(pw, 6.8, pill, align="C")
        pdf.set_xy(LM, y0 + 11)

        # question body
        for kind, c, raw in _blocks(qtext):
            _render_block(pdf, kind, c, LM, EPW, is_q=True, raw=raw)
        _img(pdf, q.image_b64)

        if (ex.test_type or "") == "mcq":
            opts = (q.options_hi if (is_hi and q.options_hi) else q.options) or []
            pdf.ln(1.5)
            for idx, op in enumerate(opts):
                is_corr = q.correct_option and str(op).strip() == str(q.correct_option).strip()
                pdf.set_font("Noto", size=10.5)
                yy = pdf.get_y()
                if is_corr:
                    pdf.set_fill_color(*GREENBG)
                    pdf.set_draw_color(*GREEN)
                    pdf.set_text_color(*GREEN)
                    pdf.set_x(LM)
                    pdf.multi_cell(EPW, 7, "   %s)   %s      %s" % (chr(65 + idx), _clean(str(op)), L["correct"]),
                                   new_x="LMARGIN", new_y="NEXT", fill=True, border=1)
                else:
                    pdf.set_text_color(38, 42, 50)
                    pdf.set_x(LM)
                    pdf.multi_cell(EPW, 7, "   %s)   %s" % (chr(65 + idx), _clean(str(op))),
                                   new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.8)
            pdf.set_text_color(0, 0, 0)
        else:
            ans = (q.model_answer_hi if (is_hi and q.model_answer_hi) else q.model_answer) or ""
            if ans.strip():
                pdf.ln(2)
                yy = pdf.get_y()
                pdf.set_fill_color(*GREEN)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Noto", size=8.5)
                lw = pdf.get_string_width(L["answer"]) + 8
                pdf.rect(LM, yy, lw, 6.5, style="F", round_corners=True, corner_radius=1.5)
                pdf.set_xy(LM, yy + 0.4)
                pdf.cell(lw, 5.7, L["answer"], align="C")
                pdf.set_xy(LM, yy + 9)
                for kind, c, raw in _blocks(ans):
                    _render_block(pdf, kind, c, LM, EPW, is_q=False, raw=raw)
            _img(pdf, q.model_answer_image)

        pdf.ln(3)
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.3)
        pdf.line(LM, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    return bytes(pdf.output())


_FRAC_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")


def _split_frac(raw):
    """If raw (pre-clean) text contains a \\frac{num}{den}, return the cleaned
    (prefix, numerator, denominator, suffix) so it can be drawn as a real stacked
    fraction. Returns None if there's no \\frac to render."""
    if not raw:
        return None
    m = _FRAC_RE.search(raw)
    if not m:
        return None
    pre, post = raw[:m.start()], raw[m.end():]
    return _clean(pre), _clean(m.group(1)), _clean(m.group(2)), _clean(post)


def _render_fraction(pdf, frac, LM, EPW, color):
    """Draw prefix, a numerator/line/denominator stack, then suffix - a real
    vertical fraction like a textbook, instead of flattened '(a)/(b)' text."""
    pre, num, den, post = frac
    pdf.set_font("Noto", size=10)
    num_w, den_w = pdf.get_string_width(num), pdf.get_string_width(den)
    frac_w = max(num_w, den_w) + 5
    pdf.set_font("Noto", size=11.5)
    pre_w = pdf.get_string_width(pre) if pre.strip() else 0
    post_w = pdf.get_string_width(post) if post.strip() else 0
    total_w = pre_w + frac_w + post_w
    x0 = LM + max(0, (EPW - total_w) / 2)
    y0 = pdf.get_y() + 1.5
    pdf.set_text_color(*color)
    if pre.strip():
        pdf.set_xy(x0, y0 + 3.3)
        pdf.set_font("Noto", size=11.5)
        pdf.cell(pre_w, 6, pre, align="L")
    fx = x0 + pre_w
    pdf.set_font("Noto", size=10)
    pdf.set_xy(fx, y0)
    pdf.cell(frac_w, 5, num, align="C")
    pdf.set_draw_color(*color)
    pdf.set_line_width(0.35)
    pdf.line(fx + 1.5, y0 + 5.7, fx + frac_w - 1.5, y0 + 5.7)
    pdf.set_xy(fx, y0 + 6.1)
    pdf.cell(frac_w, 5, den, align="C")
    if post.strip():
        pdf.set_xy(fx + frac_w, y0 + 3.3)
        pdf.set_font("Noto", size=11.5)
        pdf.cell(post_w, 6, post, align="L")
    pdf.set_xy(LM, y0 + 12.3)
    pdf.set_text_color(20, 22, 28)


def _render_block(pdf, kind, c, LM, EPW, is_q, raw=None):
    if kind == "head":
        # premium heading: bigger type + a solid colour accent bar (poor-man's bold,
        # since only the Regular weight of the Devanagari font is bundled)
        pdf.ln(2)
        yy = pdf.get_y()
        acc = NAVY if is_q else NAVY2
        pdf.set_font("Noto", size=11.5)
        lines = pdf.multi_cell(EPW - 6, 6.6, c, dry_run=True, output="LINES")
        bh = 6.6 * max(1, len(lines))
        pdf.set_fill_color(*acc)
        pdf.rect(LM, yy + 0.6, 1.4, bh - 1.2, style="F")
        pdf.set_xy(LM + 5, yy)
        pdf.set_text_color(*acc)
        pdf.multi_cell(EPW - 6, 6.6, c, new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(LM)
        pdf.set_text_color(20, 22, 28)
    elif kind == "final":
        pdf.ln(1.6)
        yy = pdf.get_y()
        pdf.set_font("Noto", size=10.5)
        lines = pdf.multi_cell(EPW - 8, 6.2, c, dry_run=True, output="LINES")
        bh = 6.2 * max(1, len(lines)) + 4
        pdf.set_fill_color(*GREENBG)
        pdf.set_draw_color(*GREEN)
        pdf.set_line_width(0.4)
        pdf.rect(LM, yy, EPW, bh, style="DF", round_corners=True, corner_radius=2)
        pdf.set_xy(LM + 4, yy + 2)
        pdf.set_text_color(*GREEN)
        pdf.multi_cell(EPW - 8, 6.2, c, new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(LM, yy + bh + 1.5)
        pdf.set_text_color(20, 22, 28)
    elif kind == "eq":
        # clean, no background fill - a real stacked fraction when \frac is present,
        # otherwise plain centered equation text
        frac = _split_frac(raw)
        color = NAVY if is_q else NAVY2
        if frac:
            pdf.ln(0.5)
            _render_fraction(pdf, frac, LM, EPW, color)
        else:
            pdf.ln(1.2)
            pdf.set_font("Noto", size=12)
            pdf.set_text_color(*color)
            pdf.set_x(LM)
            pdf.cell(EPW, 7.5, c, align="C")
            pdf.ln(8.5)
            pdf.set_text_color(20, 22, 28)
    elif kind == "bullet":
        pdf.set_x(LM + 3)
        pdf.set_font("Noto", size=10.5)
        pdf.set_text_color(*(NAVY if is_q else GREEN))
        pdf.cell(4.5, 6, "\u2022")
        pdf.set_text_color(38, 42, 50)
        pdf.multi_cell(EPW - 7.5, 6, c, new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_x(LM)
        pdf.set_font("Noto", size=10.5)
        pdf.set_text_color(30, 34, 42)
        pdf.multi_cell(EPW, 6, c, new_x="LMARGIN", new_y="NEXT")
