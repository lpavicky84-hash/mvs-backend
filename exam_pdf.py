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


def _font_path_bold():
    """Bold static instance (wght=700) of the same Devanagari font. Optional -
    if not deployed, the caller falls back to registering the regular file as
    'bold' so nothing breaks."""
    here = os.path.dirname(os.path.abspath(__file__))
    name = "NotoSansDevanagari-Bold-Static.ttf"
    for c in [os.path.join(here, "fonts", name), os.path.join(here, name),
              os.path.join(os.getcwd(), "fonts", name), os.path.join(os.getcwd(), name),
              "fonts/%s" % name, name]:
        if os.path.exists(c):
            return c
    return None


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
            "\u0928\u094b\u091f:", "\u0905\u0924:",
            "दिए गए आंकड़े:", "दिए गए आँकड़े:", "अवधारणा की जाँच:",
            "मान रखने पर:", "गणना कीजिए:"]


_RUNON_RE = re.compile(
    r"[a-z0-9\)\]\.:\u00b2\u00b3][A-Z]"                    # wordEndCapital merge, no space
    r"|\b(?:N|J|V|A|W|Hz|Pa|C)[A-Z][a-z]"                  # unit+CapitalWord merge
    r"|:[a-zA-Z]\s*="                                       # ":x =" (heading glued to formula)
    r"|[a-zA-Z0-9\)\]\u00b2\u00b3][\u0900-\u097F]"          # latin/digit glued to Devanagari (kgलगाया)
    r"|\u0964\s*\S"                                          # danda । with more text after it
)
_MATH_GLUE_RE = re.compile(r"\$[^$]*\$(?=[A-Z\u0900-\u097F])")  # $...$Substitute / $...$दिए
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
    # a $math$ block with a capitalised/Devanagari word glued right onto its end
    # ("$...t^2$Substitute", "$...m/s$दिए") - must check the RAW line since
    # stripping the math also hides the glue point
    if _MATH_GLUE_RE.search(line):
        return True
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
            # $math$ glued straight onto a Capitalised / Devanagari word
            # ("$...t^2$Substitute", "$...$दिए") -> break after the math
            nxt = parts[i + 1] if i + 1 < len(parts) else ""
            if re.match(r"[A-Z\u0900-\u097F]", nxt):
                out.append("\n")
            continue
        # protect multi-word headings so camelCase split does not break them
        ph = {}
        for idx, h in enumerate(_HEADINGS + _HEAD_HI):
            if h in seg:
                tok = "\x00H%d\x00" % idx
                ph[tok] = h
                seg = seg.replace(h, "\n" + tok + "\n")
        seg = re.sub(r"\s*(Step\s+\d+\s*:)", r"\n\1", seg)
        seg = re.sub(r"\s*(\u091a\u0930\u0923\s*\d+\s*:)", r"\n\1", seg)   # चरण N:
        seg = re.sub(r"\s*(Final Answer\s*:)", r"\nFinal Answer: ", seg)
        seg = re.sub(r"\s*(\u0905\u0902\u0924\u093f\u092e \u0909\u0924\u094d\u0924\u0930\s*:)",
                     r"\n\1 ", seg)
        seg = seg.replace("\u0964", "\u0964\n")                            # break after danda ।
        seg = re.sub(r"([a-z0-9\)\]\.:\u00b2\u00b3])([A-Z])", r"\1\n\2", seg)
        seg = re.sub(r"\b(N|J|V|A|W|Hz|Pa|C)([A-Z][a-z])", r"\1\n\2", seg)
        seg = re.sub(r"([a-zA-Z0-9\)\]\u00b2\u00b3])([\u0900-\u097F])", r"\1\n\2", seg)  # kgलगाया
        seg = re.sub(r":([a-zA-Z]\s*=)", r":\n\1", seg)
        seg = re.sub(r":([\u0900-\u097F])", r":\n\1", seg)                 # कीजिए:पिंड
        seg = re.sub(r"(\))([a-z]\s*=)", r"\1\n\2", seg)                   # (4)s = ... chained eq
        seg = re.sub(r"(?<!\d)\s+(\d+\.)\s+(?=[A-Z\u0900-\u097F])", r"\n\1 ", seg)
        for tok, h in ph.items():
            seg = seg.replace(tok, h)
        # heading-colon glued straight to a $math$ block -> equation on its own line
        if i + 1 < len(parts) and seg.rstrip().endswith(":"):
            seg = seg + "\n"
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
                and not re.search(r"[A-Za-z]{4,}|[\u0900-\u097F]{3,}", c.split("=")[0]):
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
            in_data = lc.startswith(("given", "data", "to find", "required", "list",
                                     "\u0926\u093f\u092f\u093e \u0917\u092f\u093e",       # दिया गया
                                     "\u0926\u093f\u090f \u0917\u090f \u0906"))           # दिए गए आंकड़े
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


def _star(pdf, cx, cy, r, color):
    """Small 4-point sparkle star."""
    pdf.set_fill_color(*color)
    k = r * 0.28
    pdf.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
                 (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)],
                style="F")


def _draw_best_of_luck(pdf, LM, EPW, is_hi, teacher_name):
    """A friendly flat-design teacher wishing 'Best of Luck', drawn entirely with
    vector primitives (crisp at any zoom). Fills the empty space at the end of
    the paper. All coordinates relative so it survives layout changes."""
    CARD_H = 56
    bottom = pdf.h - 18                       # auto-page-break limit
    if pdf.get_y() > bottom - (CARD_H + 6):
        # not enough room - fresh page, centred vertically in the content area
        pdf.add_page()
        y0 = pdf.t_margin + max(0, ((bottom - pdf.t_margin) - CARD_H) / 2)
    else:
        # centre the card in the leftover space of the last page
        y0 = pdf.get_y() + max(3, ((bottom - pdf.get_y()) - CARD_H) / 2)
    x0 = LM
    pdf.set_y(y0)
    W = EPW
    SKIN = (255, 214, 178)
    HAIR = (56, 40, 30)
    # card
    pdf.set_fill_color(248, 250, 253)
    pdf.set_draw_color(*BORDER)
    pdf.set_line_width(0.4)
    pdf.rect(x0, y0, W, CARD_H, style="DF", round_corners=True, corner_radius=4)
    # thin amber top accent inside the card
    pdf.set_fill_color(*AMBER)
    pdf.rect(x0 + 10, y0, W - 20, 1.1, style="F", round_corners=True, corner_radius=0.5)

    # ---- teacher figure (left)
    tx = x0 + 24            # horizontal centre of the figure
    ty = y0 + 12            # top of head
    # raised arm (waving) behind the body
    pdf.set_fill_color(*NAVY2)
    pdf.polygon([(tx + 6.5, ty + 21), (tx + 15, ty + 8.5), (tx + 17.6, ty + 11),
                 (tx + 10, ty + 24)], style="F")
    pdf.set_fill_color(*SKIN)
    pdf.ellipse(tx + 13.6, ty + 6.2, 4.6, 4.6, style="F")        # waving hand
    # hair cap + head
    pdf.set_fill_color(*HAIR)
    pdf.ellipse(tx - 5.6, ty - 0.8, 11.2, 10.6, style="F")
    pdf.set_fill_color(*SKIN)
    pdf.ellipse(tx - 5, ty + 1.2, 10, 10, style="F")             # face
    pdf.set_fill_color(*HAIR)
    pdf.rect(tx - 5.6, ty + 0.6, 11.2, 3.4, style="F", round_corners=True, corner_radius=1.5)
    # glasses
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.55)
    pdf.ellipse(tx - 4.2, ty + 4.4, 3.4, 3.2, style="D")
    pdf.ellipse(tx + 0.8, ty + 4.4, 3.4, 3.2, style="D")
    pdf.line(tx - 0.8, ty + 6, tx + 0.8, ty + 6)
    # smile
    pdf.set_line_width(0.5)
    pdf.arc(tx - 1.6, ty + 7.2, 3.2, 20, 160, b=2.4, style="D")
    # body: blazer
    pdf.set_fill_color(*NAVY)
    pdf.polygon([(tx - 8.5, ty + 26), (tx - 5.5, ty + 12.4), (tx + 5.5, ty + 12.4),
                 (tx + 8.5, ty + 26)], style="F")
    # shirt + tie
    pdf.set_fill_color(255, 255, 255)
    pdf.polygon([(tx - 2.6, ty + 12.4), (tx + 2.6, ty + 12.4), (tx, ty + 18.5)], style="F")
    pdf.set_fill_color(*AMBER)
    pdf.polygon([(tx - 1, ty + 13.2), (tx + 1, ty + 13.2), (tx + 0.6, ty + 19.5),
                 (tx, ty + 21), (tx - 0.6, ty + 19.5)], style="F")
    # book in the other hand
    pdf.set_fill_color(*GREEN)
    pdf.rect(tx - 13.5, ty + 18, 7.6, 5.4, style="F", round_corners=True, corner_radius=0.8)
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(tx - 12.7, ty + 19, 6, 0.9, style="F")
    pdf.rect(tx - 12.7, ty + 20.6, 6, 0.9, style="F")

    # ---- speech bubble (right)
    msg = "\u0936\u0941\u092d\u0915\u093e\u092e\u0928\u093e\u090f\u0901!" if is_hi else "Best of Luck!"
    sub = ("\u0916\u0942\u092c \u0905\u091a\u094d\u091b\u0947 \u0938\u0947 \u0932\u093f\u0916\u0928\u093e!"
           if is_hi else "Do your best, champions!")
    bx = x0 + 48
    bw = W - 48 - 10
    by = y0 + 10
    bh = 26
    pdf.set_fill_color(255, 255, 255)
    pdf.set_draw_color(*GREEN)
    pdf.set_line_width(0.5)
    pdf.rect(bx, by, bw, bh, style="DF", round_corners=True, corner_radius=3.5)
    # bubble tail pointing at the teacher
    pdf.set_fill_color(255, 255, 255)
    pdf.polygon([(bx + 0.4, by + 13), (bx - 5.5, by + 17.5), (bx + 0.4, by + 19)], style="F")
    pdf.set_draw_color(*GREEN)
    pdf.line(bx + 0.4, by + 13, bx - 5.5, by + 17.5)
    pdf.line(bx - 5.5, by + 17.5, bx + 0.4, by + 19)
    # bubble text
    pdf.set_xy(bx + 4, by + 4)
    pdf.set_font("Noto", "B", 17)
    pdf.set_text_color(*GREEN)
    pdf.cell(bw - 8, 9.5, msg, align="C")
    pdf.set_xy(bx + 4, by + 15)
    pdf.set_font("Noto", size=10.5)
    pdf.set_text_color(*NAVY2)
    pdf.cell(bw - 8, 6.5, sub, align="C")
    # teacher signature line under the bubble
    if (teacher_name or "").strip():
        pdf.set_xy(bx, by + bh + 3.5)
        pdf.set_font("Noto", "B", 9)
        pdf.set_text_color(*GREY)
        pdf.cell(bw, 5, "\u2014 %s" % teacher_name, align="R")
    # sparkles
    _star(pdf, bx + bw - 5, by - 3.2, 2.4, AMBER)
    _star(pdf, bx + 7, by - 2.4, 1.7, AMBER)
    _star(pdf, bx + bw + 2.5, by + bh - 3, 1.9, AMBER)
    pdf.set_xy(LM, y0 + CARD_H + 4)
    pdf.set_text_color(20, 22, 28)
    pdf.set_line_width(0.3)


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
            self.set_font("Noto", "B", 8)
            self.set_text_color(*GREY)
            self.cell(0, 6, "MVS Foundation  \u00b7  %s" % (ex.teacher_name or ""), align="L")
            self.set_y(-12)
            self.cell(0, 6, "Page %d" % self.page_no(), align="R")

    pdf = PDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("Noto", "", FONT)
    pdf.add_font("Noto", "B", _font_path_bold() or FONT)
    pdf.add_page()
    pdf.set_text_shaping(True)
    EPW = pdf.epw
    LM = pdf.l_margin

    # ---- header band: navy + circular logo + bold title + highlighted info chips
    BAND_H = 42
    CHIPBG = (48, 78, 126)               # lighter navy chip fill
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, pdf.w, BAND_H, style="F")
    pdf.set_fill_color(*AMBER)
    pdf.rect(0, BAND_H, pdf.w, 1.5, style="F")
    text_x = LM
    logo = _logo_path()
    if logo:
        try:
            D = 30                       # logo diameter (mm)
            ly = (BAND_H - D) / 2
            # white ring behind the circular logo so it pops on the navy band
            pdf.set_fill_color(255, 255, 255)
            pdf.ellipse(LM - 1.2, ly - 1.2, D + 2.4, D + 2.4, style="F")
            pdf.image(logo, x=LM, y=ly, w=D, h=D)
            text_x = LM + D + 8
        except Exception:
            text_x = LM
    # title (bold, large)
    pdf.set_xy(text_x, 6.5)
    pdf.set_font("Noto", "B", 21)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(pdf.w - pdf.r_margin - text_x, 10.5, _clean(ex.title or "Test"), new_x="LMARGIN", new_y="NEXT")
    # info chips row: subject / medium / total marks as highlighted rounded pills
    chips = [c for c in [ex.subject or "", L["medium"], "%s: %s" % (L["total"], ex.total_marks)] if c.strip()]
    cy = 20.5
    cx = text_x
    pdf.set_font("Noto", "B", 10)
    for chip in chips:
        cw = pdf.get_string_width(chip) + 9
        pdf.set_fill_color(*CHIPBG)
        pdf.rect(cx, cy, cw, 8.5, style="F", round_corners=True, corner_radius=4.1)
        pdf.set_xy(cx, cy + 0.5)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(cw, 7.5, chip, align="C")
        cx += cw + 3.5
    # answer-key tag: amber badge
    tag = L["qpaper"]
    pdf.set_font("Noto", "B", 8.5)
    tw = pdf.get_string_width(tag) + 9
    ty = 31.5
    pdf.set_fill_color(*AMBER)
    pdf.rect(text_x, ty, tw, 7.5, style="F", round_corners=True, corner_radius=2.2)
    pdf.set_xy(text_x, ty + 0.5)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(tw, 6.5, tag, align="C")
    pdf.set_xy(LM, BAND_H + 8)

    for q in questions:
        qtext = (q.question_text_hi if (is_hi and q.question_text_hi) else q.question_text) or ""
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()

        y0 = pdf.get_y()
        # question badge
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Noto", "B", 12.5)
        badge = "%s%d" % (L["q"], q.q_no)
        bw = pdf.get_string_width(badge) + 10
        pdf.rect(LM, y0, bw, 9.5, style="F", round_corners=True, corner_radius=2.5)
        pdf.set_xy(LM, y0 + 0.7)
        pdf.cell(bw, 8.1, badge, align="C")
        # marks pill
        pill = "%d %s" % (q.max_marks, L["marks"])
        pdf.set_font("Noto", "B", 9)
        pw = pdf.get_string_width(pill) + 9
        pdf.set_fill_color(*LIGHT)
        pdf.set_draw_color(*BORDER)
        pdf.rect(pdf.w - pdf.r_margin - pw, y0, pw, 9.5, style="DF", round_corners=True, corner_radius=2.5)
        pdf.set_xy(pdf.w - pdf.r_margin - pw, y0 + 0.7)
        pdf.set_text_color(*GREY)
        pdf.cell(pw, 8.1, pill, align="C")
        pdf.set_xy(LM, y0 + 13)

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
                    pdf.set_text_color(28, 32, 40)
                    pdf.set_x(LM)
                    pdf.multi_cell(EPW, 7, "   %s)   %s" % (chr(65 + idx), _clean(str(op))),
                                   new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.8)
            pdf.set_text_color(0, 0, 0)
        else:
            ans = (q.model_answer_hi if (is_hi and q.model_answer_hi) else q.model_answer) or ""
            if ans.strip():
                pdf.ln(2.5)
                yy = pdf.get_y()
                pdf.set_fill_color(*GREEN)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Noto", "B", 9.5)
                lw = pdf.get_string_width(L["answer"]) + 10
                pdf.rect(LM, yy, lw, 7.5, style="F", round_corners=True, corner_radius=2)
                pdf.set_xy(LM, yy + 0.5)
                pdf.cell(lw, 6.5, L["answer"], align="C")
                pdf.set_xy(LM, yy + 11)
                for kind, c, raw in _blocks(ans):
                    _render_block(pdf, kind, c, LM, EPW, is_q=False, raw=raw)
            _img(pdf, q.model_answer_image)

        pdf.ln(3)
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.3)
        pdf.line(LM, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4)

    # friendly send-off illustration at the end of the paper
    try:
        _draw_best_of_luck(pdf, LM, EPW, is_hi, ex.teacher_name or "")
    except Exception:
        pass

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
    pdf.set_font("Noto", size=11)
    num_w, den_w = pdf.get_string_width(num), pdf.get_string_width(den)
    frac_w = max(num_w, den_w) + 5.5
    pdf.set_font("Noto", size=13)
    pre_w = pdf.get_string_width(pre) if pre.strip() else 0
    post_w = pdf.get_string_width(post) if post.strip() else 0
    total_w = pre_w + frac_w + post_w
    x0 = LM + max(0, (EPW - total_w) / 2)
    y0 = pdf.get_y() + 1.5
    pdf.set_text_color(*color)
    if pre.strip():
        pdf.set_xy(x0, y0 + 3.6)
        pdf.set_font("Noto", size=13)
        pdf.cell(pre_w, 6.5, pre, align="L")
    fx = x0 + pre_w
    pdf.set_font("Noto", size=11)
    pdf.set_xy(fx, y0)
    pdf.cell(frac_w, 5.5, num, align="C")
    pdf.set_draw_color(*color)
    pdf.set_line_width(0.4)
    pdf.line(fx + 1.5, y0 + 6.3, fx + frac_w - 1.5, y0 + 6.3)
    pdf.set_xy(fx, y0 + 6.8)
    pdf.cell(frac_w, 5.5, den, align="C")
    if post.strip():
        pdf.set_xy(fx + frac_w, y0 + 3.6)
        pdf.set_font("Noto", size=13)
        pdf.cell(post_w, 6.5, post, align="L")
    pdf.set_xy(LM, y0 + 13.8)
    pdf.set_text_color(20, 22, 28)


_MAJOR_HEAD_RE = re.compile(
    r"^(statement|given data|given|solution|to find|required|concept check|"
    r"the smart strategy"
    r"|\u0915\u0925\u0928"                                   # कथन
    r"|\u0926\u093f\u092f\u093e \u0917\u092f\u093e"          # दिया गया
    r"|\u0926\u093f\u090f \u0917\u090f"                      # दिए गए ...
    r"|\u0939\u0932"                                          # हल
    r"|\u0905\u0935\u0927\u093e\u0930\u0923\u093e"            # अवधारणा ...
    r")\s*[:\u0903]|^(step\s*\d+|\u091a\u0930\u0923\s*\d+)\s*:",
    re.IGNORECASE)


def _render_block(pdf, kind, c, LM, EPW, is_q, raw=None):
    if kind == "head":
        acc = NAVY if is_q else NAVY2
        if _MAJOR_HEAD_RE.match(c.strip()):
            # major section heading: larger navy type - clean, no side bar
            pdf.ln(2.6)
            pdf.set_x(LM)
            pdf.set_font("Noto", "B", 13)
            pdf.set_text_color(*acc)
            pdf.multi_cell(EPW, 7.4, c, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.6)
        else:
            # minor connector line ("According to...", "Substitute the values:"):
            # coloured text only - no bar, so the left edge stays clean
            pdf.ln(1.4)
            pdf.set_x(LM)
            pdf.set_font("Noto", size=11.8)
            pdf.set_text_color(*acc)
            pdf.multi_cell(EPW, 7, c, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 22, 28)
    elif kind == "final":
        pdf.ln(2.2)
        yy = pdf.get_y()
        pdf.set_font("Noto", "B", 11.5)
        lines = pdf.multi_cell(EPW - 10, 7, c, dry_run=True, output="LINES")
        bh = 7 * max(1, len(lines)) + 5
        pdf.set_fill_color(*GREENBG)
        pdf.set_draw_color(*GREEN)
        pdf.set_line_width(0.45)
        pdf.rect(LM, yy, EPW, bh, style="DF", round_corners=True, corner_radius=2.5)
        pdf.set_xy(LM + 5, yy + 2.5)
        pdf.set_text_color(*GREEN)
        pdf.multi_cell(EPW - 10, 7, c, new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(LM, yy + bh + 2)
        pdf.set_text_color(20, 22, 28)
    elif kind == "eq":
        # clean, no background fill - a real stacked fraction when \frac is present,
        # otherwise plain centered equation text
        frac = _split_frac(raw)
        color = NAVY if is_q else NAVY2
        if frac:
            pdf.ln(0.8)
            _render_fraction(pdf, frac, LM, EPW, color)
        else:
            pdf.ln(1.6)
            pdf.set_font("Noto", size=13.5)
            pdf.set_text_color(*color)
            pdf.set_x(LM)
            pdf.cell(EPW, 8.5, c, align="C")
            pdf.ln(10)
            pdf.set_text_color(20, 22, 28)
    elif kind == "bullet":
        pdf.set_x(LM + 4)
        pdf.set_font("Noto", size=11.5)
        pdf.set_text_color(*(NAVY if is_q else GREEN))
        pdf.cell(5, 6.8, "\u2022")
        pdf.set_text_color(28, 32, 40)
        pdf.multi_cell(EPW - 9, 6.8, c, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)
    else:
        pdf.set_x(LM)
        pdf.set_font("Noto", size=11.5)
        pdf.set_text_color(22, 26, 34)
        pdf.multi_cell(EPW, 6.8, c, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)


# ====================================================================== marks stamp
def _fmt_num(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else ("%.1f" % f)
    except Exception:
        return str(v)


def _stamp_image(data, mime, label_big, label_small):
    """Draw a green marks badge on the top-right corner of a photo answer sheet."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font
    im = _Img.open(_io.BytesIO(data)).convert("RGB")
    W, H = im.size
    d = _Draw.Draw(im)
    try:
        f_big = _Font.truetype(_font_path_bold() or _font_path(), max(22, W // 16))
        f_sm = _Font.truetype(_font_path(), max(13, W // 38))
    except Exception:
        f_big = _Font.load_default()
        f_sm = f_big
    bb = d.textbbox((0, 0), label_big, font=f_big)
    sb = d.textbbox((0, 0), label_small, font=f_sm)
    tw = max(bb[2] - bb[0], sb[2] - sb[0])
    th = (bb[3] - bb[1]) + (sb[3] - sb[1])
    pad = max(10, W // 60)
    gap = max(4, W // 200)
    bw, bh = tw + pad * 2, th + gap + pad * 2
    m = max(10, W // 50)
    x1, y1 = W - m - bw, m
    d.rounded_rectangle([x1, y1, x1 + bw, y1 + bh], radius=max(8, W // 90),
                        fill=(22, 122, 74), outline=(255, 255, 255),
                        width=max(2, W // 400))
    cx = x1 + bw / 2
    d.text((cx - (bb[2] - bb[0]) / 2, y1 + pad - bb[1]), label_big,
           font=f_big, fill=(255, 255, 255))
    d.text((cx - (sb[2] - sb[0]) / 2, y1 + pad + (bb[3] - bb[1]) + gap - sb[1]),
           label_small, font=f_sm, fill=(214, 240, 226))
    out = _io.BytesIO()
    if "png" in (mime or ""):
        im.save(out, format="PNG")
        return out.getvalue(), "image/png"
    im.save(out, format="JPEG", quality=92)
    return out.getvalue(), "image/jpeg"


def _stamp_pdf(data, label_big, label_small):
    """Merge a green marks badge onto the first page of a PDF answer sheet."""
    import io as _io
    from pypdf import PdfReader, PdfWriter
    from fpdf import FPDF
    reader = PdfReader(_io.BytesIO(data))
    p0 = reader.pages[0]
    w_mm = float(p0.mediabox.width) * 25.4 / 72.0
    h_mm = float(p0.mediabox.height) * 25.4 / 72.0
    ov = FPDF(unit="mm", format=(w_mm, h_mm))
    ov.set_auto_page_break(False)
    ov.add_page()
    ov.add_font("Noto", "", _font_path())
    ov.add_font("Noto", "B", _font_path_bold() or _font_path())
    ov.set_font("Noto", "B", 15)
    bw = max(ov.get_string_width(label_big), 0) + 12
    ov.set_font("Noto", size=7.5)
    bw = max(bw, ov.get_string_width(label_small) + 12)
    bh = 10.2 + 5.2
    x1, y1 = w_mm - 8 - bw, 8
    ov.set_fill_color(22, 122, 74)
    ov.set_draw_color(255, 255, 255)
    ov.set_line_width(0.7)
    ov.rect(x1, y1, bw, bh, style="DF", round_corners=True, corner_radius=2.5)
    ov.set_xy(x1, y1 + 1.6)
    ov.set_font("Noto", "B", 15)
    ov.set_text_color(255, 255, 255)
    ov.cell(bw, 8, label_big, align="C")
    ov.set_xy(x1, y1 + 9.6)
    ov.set_font("Noto", size=7.5)
    ov.set_text_color(214, 240, 226)
    ov.cell(bw, 4.6, label_small, align="C")
    ov_reader = PdfReader(_io.BytesIO(bytes(ov.output())))
    p0.merge_page(ov_reader.pages[0])
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    out = _io.BytesIO()
    writer.write(out)
    return out.getvalue(), "application/pdf"


def stamp_marks_on_answer(data, mime, obtained, total, verdict=None):
    """Stamp 'MARKS X/Y' on a graded answer sheet (photo or PDF). Never raises -
    on any failure the original file is returned untouched so downloads keep
    working exactly as before."""
    try:
        label_big = "%s / %s" % (_fmt_num(obtained), _fmt_num(total))
        label_small = ("MARKS  \u00b7  " + str(verdict).upper()) if verdict else "MARKS  \u00b7  CHECKED"
        m = (mime or "").lower()
        if "pdf" in m:
            return _stamp_pdf(data, label_big, label_small)
        return _stamp_image(data, m, label_big, label_small)
    except Exception:
        return data, (mime or "application/octet-stream")
