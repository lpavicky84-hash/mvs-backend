"""
Premium question+answer PDF generator for MVS Foundation (English + Hindi medium).
Uses fpdf2 + uharfbuzz shaping with a bundled Noto Sans Devanagari font.
Turns run-on AI answers into cleanly structured blocks (headings, bullets,
steps, centered equations, highlighted final answer) with a premium layout.
"""
import os, re, io, base64


import tempfile as _tempfile

# Cache of variable-font -> frozen static instance (per (src, weight)).
_STATIC_CACHE = {}
# All the Devanagari Noto filenames we might find in a deploy (static first, then variable).
_NOTO_NAMES = [
    "NotoSansDevanagari-Static.ttf", "NotoSansDevanagari-Regular.ttf",
    "NotoSansDevanagari.ttf", "NotoSansDevanagari-VariableFont_wdth,wght.ttf",
    "NotoSansDevanagari[wdth,wght].ttf",
]


def _noto_candidates(name):
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(here, "fonts", name), os.path.join(here, name),
            os.path.join(os.getcwd(), "fonts", name), os.path.join(os.getcwd(), name),
            "fonts/%s" % name, name]


def _find_existing(name):
    for c in _noto_candidates(name):
        if os.path.exists(c):
            return c
    return None


def _raw_noto_src():
    """First Noto Devanagari file that actually exists on disk (static or variable)."""
    for name in _NOTO_NAMES:
        p = _find_existing(name)
        if p:
            return p
    return None


def _instance_static(src, wght=400, tag="reg"):
    """A VARIABLE font's Devanagari conjuncts/matras get corrupted by fpdf2's glyph
    subsetter (e.g. 'प्रतिष्ठा' -> 'प्रतष्ठिा'), even though HarfBuzz shapes them
    correctly. Freezing the variable font to a STATIC instance embeds reliably. This
    does that at runtime and caches the result in a temp file, so a deploy only needs
    the ordinary variable Noto file (no binary static upload required). Needs fonttools;
    if unavailable, returns src unchanged (no worse than before)."""
    if not src:
        return src
    key = (src, wght)
    cached = _STATIC_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    try:
        from fontTools.ttLib import TTFont
        from fontTools.varLib.instancer import instantiateVariableFont
        f = TTFont(src)
        if "fvar" not in f:                 # already a static font -> use as-is
            _STATIC_CACHE[key] = src
            return src
        axes = {}
        for a in f["fvar"].axes:
            axes[a.axisTag] = wght if a.axisTag == "wght" else a.defaultValue
        instantiateVariableFont(f, axes, inplace=True)
        out = os.path.join(_tempfile.gettempdir(), "MVSNotoDeva-%s.ttf" % tag)
        f.save(out)
        _STATIC_CACHE[key] = out
        return out
    except Exception:
        return src


def _enable_shaping(pdf, font_path):
    """Turn ON Devanagari text shaping AND verify it works. Correct Hindi in the PDF
    (conjuncts like क्त, matra reordering like प्रतिष्ठा) requires HarfBuzz shaping via
    uharfbuzz. If shaping is provably dead we RAISE a clear error rather than silently
    emitting broken, unreadable Devanagari (spec §39)."""
    pdf.set_text_shaping(True)          # fpdf2 itself raises here if uharfbuzz is missing
    broken = False
    try:
        import uharfbuzz as hb
        with open(font_path, "rb") as _f:
            _blob = hb.Blob(_f.read())
        _font = hb.Font(hb.Face(_blob))
        _buf = hb.Buffer()
        _buf.add_str("\u0915\u094d\u0924")     # क + ् + त  ->  क्त must collapse to < 3 glyphs
        _buf.guess_segment_properties()
        hb.shape(_font, _buf)
        broken = len(_buf.glyph_infos) >= 3
    except Exception:
        broken = False                  # can't verify -> trust set_text_shaping, don't block
    if broken:
        raise RuntimeError(
            "Hindi shaping is not working on this server (the क्त conjunct did not form). "
            "Deploy the FULL exam_pdf.py, keep 'uharfbuzz' + 'fonttools' in requirements.txt, "
            "and do a clean rebuild.")


def _font_path():
    """Regular Devanagari font path. Prefers an explicit static file; otherwise takes
    whatever Noto file exists and freezes a variable one to static at runtime so
    conjuncts embed correctly."""
    static = _find_existing("NotoSansDevanagari-Static.ttf")
    if static:
        return static
    src = _raw_noto_src()
    if src:
        return _instance_static(src, 400, "reg")
    return "NotoSansDevanagari-Static.ttf"


_HAS_DVS = False  # v120: DejaVu Sans (math glyphs) available ya nahi


def _dvs_path(bold=False):
    """DejaVu Sans - isme ∫, Σ, π, ², ≤ jaise math glyphs hote hain (Noto
    Devanagari me nahi hote). Equation blocks isi se render hote hain jab
    available ho; na ho to purana Noto+ASCII fallback chalta rehta hai."""
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    here = os.path.dirname(os.path.abspath(__file__))
    for p in [os.path.join(here, "fonts", name), os.path.join(here, name),
              os.path.join(os.getcwd(), "fonts", name), os.path.join(os.getcwd(), name),
              "fonts/%s" % name, name]:
        if os.path.exists(p):
            return p
    return None


def _register_dvs(pdf):
    """DejaVu ko 'DVS' family ke roop me register karo; success flag module-global."""
    global _HAS_DVS
    p = _dvs_path()
    _HAS_DVS = bool(p)
    if _HAS_DVS:
        pdf.add_font("DVS", "", p)
        pdf.add_font("DVS", "B", _dvs_path(bold=True) or p)


def _dvs_ok_for(*texts):
    """Equation text me Devanagari nahi + DVS registered -> math font use karo."""
    if not _HAS_DVS:
        return False
    return not any(re.search(r"[\u0900-\u097F]", t or "") for t in texts)

def _para_font(pdf, raw, c, size, bold):
    """Bullet/para fallback ke liye: English/math text DejaVu se (math glyphs
    milte hain), Devanagari ya font na ho to Noto+_fx fallback. Text lautata hai."""
    if _dvs_ok_for(raw or c):
        try:
            pdf.set_font("DVS", "B" if bold else "", size)
            return _clean(_strip_rich(raw or ""), fx=False)
        except Exception:
            pass
    _style_font(pdf, "", size, base_bold=bold)
    return c



def _font_path_bold():
    """Bold Devanagari font. Prefers an explicit bold-static file; otherwise freezes a
    bold (wght=700) static instance from the variable font at runtime. Returns None only
    if no Noto file exists at all (caller then registers the regular file as bold)."""
    boldstatic = _find_existing("NotoSansDevanagari-Bold-Static.ttf")
    if boldstatic:
        return boldstatic
    src = _raw_noto_src()
    if src:
        out = _instance_static(src, 700, "bold")
        if out and out != src:
            return out
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
    # v120: calculus / science operators (word-functions stay ASCII, symbols unicode)
    (r"\\iint(?![a-zA-Z])", "\u222c"), (r"\\iiint(?![a-zA-Z])", "\u222d"),
    (r"\\oint(?![a-zA-Z])", "\u222e"), (r"\\int(?![a-zA-Z])", "\u222b"),
    (r"\\prod(?![a-zA-Z])", "\u220f"), (r"\\partial(?![a-zA-Z])", "\u2202"),
    (r"\\nabla(?![a-zA-Z])", "\u2207"),
    (r"\\log(?![a-zA-Z])", "log"), (r"\\ln(?![a-zA-Z])", "ln"),
    (r"\\sin(?![a-zA-Z])", "sin"), (r"\\cos(?![a-zA-Z])", "cos"),
    (r"\\tan(?![a-zA-Z])", "tan"), (r"\\cosec(?![a-zA-Z])", "cosec"),
    (r"\\sec(?![a-zA-Z])", "sec"), (r"\\cot(?![a-zA-Z])", "cot"),
    (r"\\lim(?![a-zA-Z])", "lim"),
    (r"\\therefore(?![a-zA-Z])", "\u2234"), (r"\\because(?![a-zA-Z])", "\u2235"),
    (r"\\in(?![a-zA-Z])", "\u2208"), (r"\\cup(?![a-zA-Z])", "\u222a"),
    (r"\\cap(?![a-zA-Z])", "\u2229"), (r"\\subseteq(?![a-zA-Z])", "\u2286"),
    (r"\\subset(?![a-zA-Z])", "\u2282"),
    (r"\\ldots|\\cdots|\\dots", "\u2026"), (r"\\prime(?![a-zA-Z])", "\u2032"),
    (r"\\epsilon(?![a-zA-Z])", "\u03b5"), (r"\\varepsilon(?![a-zA-Z])", "\u03b5"),
    (r"\\Sigma", "\u03a3"), (r"\\Gamma", "\u0393"), (r"\\Phi", "\u03a6"),
    (r"\\Lambda", "\u039b"),
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


# --------------------------------------------- font coverage safety (IMPORTANT)
# Noto Sans Devanagari me Latin + Devanagari to hai, lekin math/Greek glyphs
# NAHI hain: ^2 (superscripts), pi/theta/lambda jaise Greek, sqrt, infinity,
# <=, >=, +/-, arrows, ticks. fpdf2 missing glyph ko CHUPCHAP drop kar deta
# hai - "v^2 - u^2 = 2as" paper me "v - u = 2as" ban jaata (GALAT formula!).
# Isliye render se pehle inhe ASCII me badal dete hain. Map tabhi badlo jab
# fonts/ me aisa font rakha jaye jinme ye glyphs hon (tab _fx ko skip karna).
_MISSING_FIX = {
    # superscripts / subscripts -> caret/underscore notation (unambiguous)
    "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4",
    "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
    "⁺": "^+", "⁻": "^-", "ⁿ": "^n",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
    "₅": "_5", "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9",
    # vulgar fractions
    "½": "1/2", "¼": "1/4", "¾": "3/4",
    # Greek letters (physics/maths symbols) -> spelled out
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "θ": "theta", "λ": "lambda", "μ": "mu", "π": "pi",
    "ρ": "rho", "σ": "sigma", "τ": "tau", "φ": "phi",
    "ω": "omega", "Δ": "Delta", "Σ": "Sigma", "Ω": "Omega",
    # operators / relations / arrows
    "√": "sqrt", "∞": "infinity", "≈": "~=", "≠": "!=",
    "≤": "<=", "≥": ">=", "±": "+/-", "∓": "-/+",
    # v120 safety (Noto fallback path me glyph na mile to readable ASCII)
    "∫": "integral", "∬": "integral", "∭": "integral",
    "∮": "integral", "∏": "product", "∂": "d", "∇": "del",
    "∴": "therefore", "∵": "because", "∈": "in",
    "∪": "union", "∩": "intersection", "⊂": "subset",
    "⊆": "subseteq", "…": "...", "′": "'",
    "⃗": "", "̂": "", "î": "i", "ĵ": "j",
    "→": "->", "←": "<-", "⇒": "=>",
    # check marks -> simple text marker
    "✓": "[OK]", "✔": "[OK]",
}
_FIX_TRANS = str.maketrans(_MISSING_FIX)


def _fx(t):
    """Font me na hone wale chars ko ASCII equivalent se badalta hai.
    _clean() ke andar call hota hai, isliye saara question/answer text cover."""
    return (t or "").translate(_FIX_TRANS)


# ------------------------------------------------- portal ka light rich markup
# Portal me teacher **bold**, __underline__, *italic* likhta hai aur alternative
# question ke liye akeli "OR" line. Ye markers plain text me save hote hain,
# isliye PDF ko bhi wahi samajhna padta hai. Markers _clean() se PEHLE nikaale
# jaate hain, warna "__x__" ko _supsub subscript samajh leta hai.
_RICH_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S)
_OR_LINE_RE = re.compile(u"^\\s*(?:\\*\\*)?\\s*(OR|or|Or|oR|\u092f\u093e)\\s*(?:\\*\\*)?\\s*$")


def _strip_rich(t):
    """Markers hata ke plain text - classification aur width measure ke liye."""
    prev = None
    out = t or ""
    while prev != out:
        prev = out
        out = _RICH_RE.sub(lambda m: m.group(1) or m.group(2) or m.group(3) or "", out)
    return out


def _rich_runs(t):
    """[(text, style)] deta hai; style fpdf ka 'B' / 'I' / 'U' / combo hota hai."""
    runs, pos = [], 0
    for m in _RICH_RE.finditer(t or ""):
        if m.start() > pos:
            runs.append((t[pos:m.start()], ""))
        if m.group(1) is not None:
            inner, st = m.group(1), "B"
        elif m.group(2) is not None:
            inner, st = m.group(2), "U"
        else:
            inner, st = m.group(3), "I"
        for sub_txt, sub_st in _rich_runs(inner):
            merged = "".join(sorted(set(st + sub_st)))
            runs.append((sub_txt, merged))
        pos = m.end()
    if pos < len(t or ""):
        runs.append((t[pos:], ""))
    return [(x, y) for x, y in runs if x]


def _is_or_line(ln):
    return bool(_OR_LINE_RE.match((ln or "").strip()))


def _or_token(ln):
    """Hindi paper me separator "\u092f\u093e" hota hai, English me "OR"."""
    m = _OR_LINE_RE.match((ln or "").strip())
    if m and m.group(1) == u"\u092f\u093e":
        return u"\u092f\u093e"
    return "OR"


def _style_font(pdf, style, size, base_bold=False):
    """fpdf style string set karta hai; italic file na ho to bhi crash nahi hota."""
    st = "".join(sorted(set(style + ("B" if base_bold else ""))))
    try:
        pdf.set_font("Noto", st, size)
    except Exception:
        try:
            pdf.set_font("Noto", st.replace("I", ""), size)
        except Exception:
            pdf.set_font("Noto", "B" if "B" in st else "", size)


def _write_rich(pdf, raw, LM, EPW, size, line_h, base_bold=False, color=(22, 26, 34)):
    """Mixed bold/italic/underline text ko wrap karke likhta hai.
    Koi marker na ho to False lautata hai taaki caller purana multi_cell use kare."""
    runs = _rich_runs(raw or "")
    if not any(st for _, st in runs):
        return False
    # write() hamesha left margin pe wrap karta hai - bullet jaisi indented
    # lines ke liye margin ko temporarily shift karte hain
    old_lm, old_rm = pdf.l_margin, pdf.r_margin
    pdf.l_margin = LM
    pdf.r_margin = pdf.w - LM - EPW
    pdf.set_x(LM)
    pdf.set_text_color(*color)
    try:
        for txt, st in runs:
            piece = _clean(txt)
            if not piece:
                continue
            _style_font(pdf, st, size, base_bold)
            pdf.write(line_h, piece)
        pdf.ln(line_h)
    finally:
        pdf.l_margin, pdf.r_margin = old_lm, old_rm
    _style_font(pdf, "", size, base_bold)
    pdf.set_text_color(20, 22, 28)
    return True



# ------------------------------------------------------ v116 math heal (JS mirror)
# Portal ke mvs_portal_connected.html ka _mathHeal isi ka JS mirror hai — dono ko
# saath me badalna. Render-time repair: OCR/typed galtiyan (toota frac, cm3, 2ex,
# bina-brace sqrt, unbalanced braces) PDF banne se PEHLE theek ho jaati hain,
# isliye purana stored content bhi premium dikhta hai (DB touch nahi hota).
_FRAC_WORDY_RE = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")


def _frac_wordy(zone):
    def rep(m):
        def w(x):
            t = x.strip()
            if re.search(r"[A-Za-z\u0900-\u097f]{2,}\s+[A-Za-z\u0900-\u097f]{2,}", t) \
                    and not t.startswith("\\text"):
                return "\\text{%s}" % t
            return x
        return "\\frac{%s}{%s}" % (w(m.group(1)), w(m.group(2)))
    return _FRAC_WORDY_RE.sub(rep, zone)


def _heal_math_zone(z):
    s = z
    # broken frac: opening brace gayab — "=A}/{B" -> \frac{A}{B}
    s = re.sub(r"([^\s={}][^{}=\n]{0,80}?)\}\s*/\s*\{([^{}\n]+)\}",
               lambda m: "\\frac{%s}{%s}" % (m.group(1).strip(), m.group(2).strip()), s)
    # sqrt bina braces
    s = re.sub(r"\\sqrt\s+([A-Za-z0-9.]+)", r"\\sqrt{\1}", s)
    s = re.sub(r"\\sqrt([0-9A-Za-z])(?![A-Za-z{])", r"\\sqrt{\1}", s)
    # 2ex -> 2e^{x}
    s = re.sub(r"(?<![A-Za-z\\])e([a-z])(?![A-Za-z])", r"e^{\1}", s)
    # units: cm3 / cm_3 / 192cm_2 -> \text{cm}^{3}
    s = re.sub(r"\\text\{(cm|mm|km|dm|m|kg|g|mg|ml|mol)\}\s*_\s*\{?([23])\}?",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"(?<=[0-9)])\s?(cm|mm|km|dm|kg|mg|ml|mol)\s*_\s*\{?([23])\}?",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"\b(cm|mm|km|dm|kg|mg|ml|mol)\s*_\s*\{?([23])\}?",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"(?<=[0-9)])\s?(cm|mm|km|dm|kg|mg|ml|mol)([23])(?![0-9A-Za-z])",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"\b(cm|mm|km|dm|kg|mg|ml|mol)([23])(?![0-9A-Za-z])",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"(?<=[0-9)])\s?m\s*_\s*\{?([23])\}?", r"\\text{m}^{\1}", s)
    s = re.sub(r"(?<=[0-9)])\s?m([23])(?![0-9A-Za-z])", r" \\text{m}^{\1}", s)
    s = re.sub(r"(?<=[0-9)])\s?(cm|mm|km|dm|kg|mg|ml|mol)\^\{?([23])\}?",
               r"\\text{\1}^{\2}", s)
    s = re.sub(r"\b(cm|mm|km|dm|kg|mg|ml|mol)\^\{?([23])\}?",
               r"\\text{\1}^{\2}", s)
    # common typed-LaTeX repairs (portal _texFix ke saath parity)
    s = re.sub(r"\\text\s*([^\s{][^}]*)", r"\\text{\1}", s)
    s = re.sub(r"\\(mathrm|mathbf|mathit)\s*([^\s{])", r"\\mathrm{\2}", s)
    s = s.replace("^{}", "").replace("_{}", "")
    s = re.sub(r"[_^]\s*(?=\s|$)", "", s)
    s = re.sub(r"\\cir(?!c|[a-zA-Z])", r"^\\circ", s)
    s = re.sub(r"\\left\s*(?=[^([{|$])", "", s)
    s = re.sub(r"\\right\s*(?=[^)\]}|$])", "", s)
    s = _frac_wordy(s)
    opens, closes = s.count("{"), s.count("}")
    if opens > closes:
        s += "}" * (opens - closes)
    return s


def _heal_plain_line(ln):
    if re.search(r"\\begin\{|\\end\{|\\hline", ln):
        return ln
    s = ln
    has_eq = "=" in s
    has_hi = bool(re.search(r"[\u0900-\u097f]", s))
    # broken frac bina $ ke: "=A}/{B" -> $\frac{A}{B}$ (wordy args \text)
    if has_eq:
        def _bf(m):
            def w(x):
                t = x.strip()
                return ("\\text{%s}" % t) if re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", t) else t
            return "%s $\\frac{%s}{%s}$" % (m.group(1), w(m.group(2)), w(m.group(3)))
        s = re.sub(r"(=)\s*([^{}=\n]{2,80}?)\}\s*/\s*\{([^{}\n]{1,80}?)\}", _bf, s)
    # ascii sqrt(2) -> $\sqrt{2}$
    s = re.sub(r"\bsqrt\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", r"$\\sqrt{\1}$", s)
    # units: cm3 / cm_3 / 192cm2 -> $\text{cm}^{3}$
    s = re.sub(r"(?<=[0-9)])\s?(cm|mm|km|dm|kg|mg|ml|mol)\s*_\s*\{?([23])\}?",
               r" $\\text{\1}^{\2}$", s)
    s = re.sub(r"\b(cm|mm|km|dm|kg|mg|ml|mol)\s*_\s*\{?([23])\}?",
               r" $\\text{\1}^{\2}$", s)
    s = re.sub(r"(?<=[0-9)])\s?(cm|mm|km|dm|kg|mg|ml|mol)([23])(?![0-9A-Za-z])",
               r" $\\text{\1}^{\2}$", s)
    s = re.sub(r"\b(cm|mm|km|dm|kg|mg|ml|mol)([23])(?![0-9A-Za-z])",
               r" $\\text{\1}^{\2}$", s)
    s = re.sub(r"(?<=[0-9)])\s?m\s*_\s*\{?([23])\}?", r" $\\text{m}^{\1}$", s)
    s = re.sub(r"(?<=[0-9)])\s?m([23])(?![0-9A-Za-z])", r" $\\text{m}^{\1}$", s)
    # equation line me variable ka square/cube: "= a3" -> $a^{3}$ (Hindi lines skip)
    if has_eq and not has_hi:
        s = re.sub(r"(?<![A-Za-z\\])([a-z])([23])(?=[\s,.=):;}]|$)",
                   r"$\1^{\2}$", s)
    return s


_ZONE_RE = re.compile(r"(\$\$[^$\n]*\$\$|\$[^$\n]+\$|\\\([^()\n]*\\\)|\\\[[^\]\n]*\\\])")
_ZONE_FULL = re.compile(r"^(?:\$\$[^$\n]*\$\$|\$[^$\n]+\$|\\\([^()\n]*\\\)|\\\[[^\]\n]*\\\])$")


def _math_heal(t):
    """v116: OCR/typed math galtiyan repair — _blocks se PEHLE, taaki purana
    stored content bhi PDF me theek render ho. Idempotent: achha LaTeX untouched."""
    out = []
    for ln in (t or "").split("\n"):
        if not ln.strip():
            out.append(ln)
            continue
        parts = _ZONE_RE.split(ln)
        for i, p in enumerate(parts):
            if not p:
                continue
            if _ZONE_FULL.match(p):
                if p.startswith("$$"):
                    parts[i] = "$$" + _heal_math_zone(p[2:-2]) + "$$"
                elif p.startswith("\\("):
                    parts[i] = "\\(" + _heal_math_zone(p[2:-2]) + "\\)"
                elif p.startswith("\\["):
                    parts[i] = "\\[" + _heal_math_zone(p[2:-2]) + "\\]"
                else:
                    parts[i] = "$" + _heal_math_zone(p[1:-1]) + "$"
            else:
                parts[i] = _heal_plain_line(p)
        out.append("".join(parts))
    return "\n".join(out)


_FRAC_NEST_RE = re.compile(r"\\[dt]?frac\{((?:[^{}]|\{[^{}]*\})*)\}\{((?:[^{}]|\{[^{}]*\})*)\}")
_MAT_ENV_RE = re.compile(r"\\begin\{(bmatrix|pmatrix|matrix|vmatrix)\}([\s\S]*?)\\end\{\1\}")


def _mat_flat(m):
    """Matrix/determinant ko ek line me: [a, b; c, d] (vmatrix -> |a, b; c, d|)."""
    rows = [r.strip() for r in m.group(2).split(r"\\") if r.strip()]
    body = "; ".join(", ".join(c.strip() for c in r.split("&")) for r in rows)
    op, cl = {"bmatrix": ("[", "]"), "pmatrix": ("(", ")"),
              "matrix": ("[", "]"), "vmatrix": ("|", "|")}[m.group(1)]
    return op + " " + body + " " + cl


def _clean(text, fx=True):
    t = text or ""
    t = re.sub(r"\\ce\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\boxed\{((?:[^{}]|\{[^{}]*\})*)\}", r"\1", t)
    t = re.sub(r"\\(text|mathrm|mathbf|bf|textbf|textit|mathit)\{([^{}]*)\}", r"\2", t)
    t = re.sub(r"\\binom\{((?:[^{}]|\{[^{}]*\})*)\}\{((?:[^{}]|\{[^{}]*\})*)\}", r"C(\1, \2)", t)
    t = _MAT_ENV_RE.sub(_mat_flat, t)
    # \\left( \\right) jaise stretchy delimiters -> plain bracket ('.' wala delimiter gayab)
    t = re.sub(r"\\(left|right)\s*(\\?[|(){}\[\].])",
               lambda m: "" if m.group(2) in (".", "\\.") else m.group(2), t)
    t = re.sub(r"\\(left|right)(?![a-zA-Z])", "", t)
    # nested frac bhi handle — stable hone tak (innermost pehle)
    for _ in range(4):
        nt = _FRAC_NEST_RE.sub(r"(\1)/(\2)", t)
        if nt == t:
            break
        t = nt
    t = re.sub(r"\\sqrt\{([^{}]*)\}", "\u221a(\\1)", t)
    t = re.sub(r"\\hat\{i\}", "\u00ee", t)
    t = re.sub(r"\\hat\{j\}", "\u0135", t)
    t = re.sub(r"\\hat\{([^{}])\}", r"\1" + "\u0302", t)
    t = re.sub(r"\\vec\{([^{}]*)\}", r"\1" + "\u20d7", t)
    for pat, rep in _TEX_MAP:
        t = re.sub(pat, rep, t)
    t = _supsub(t)
    t = t.replace("\\\\", "\n").replace("$", "")
    t = re.sub(r"\\[,;:! ]", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    # atomic ordinal safety: keep "12वीं" together (old content may store "12 वीं",
    # which would break across a line in the PDF). Only joins genuine ordinal suffixes.
    t = re.sub(r"(\d)[ \t\u00A0]+(?=(?:वीं|वाँ|वां|वें|वीँ))", r"\1", t)
    return (_fx(t) if fx else t).strip()


# ------------------------------------------------------ structure the run-on text
_HEADINGS = [
    "Statement:", "Given Data:", "Given:", "Data:", "Solution:", "Required:",
    "To Find:", "Formula:", "Formula used:", "Substitute the values:",
    "Rearranging the formula to find acceleration:", "Rearranging:",
    "Concept Check:", "Note:", "Therefore:", "Hence:", "Conclusion:",
    "Answer:", "Thus:", "Substitute:",
    "Given,", "Let,", "Then,", "Substituting,", "Using the formula,",
    "We get:", "we get:", "we get,", "or,", "Rewrite the integrand:",
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
    # v120: display math ($$..$$) ke saath prose glued ho to bhi split chahiye -
    # textbook layout me equation ko apni centered line milti hai
    if "$$" in line:
        prose = re.sub(r"\$\$[^$]*\$\$", " ", line)
        if re.search(r"[A-Za-z0-9\u0900-\u097F]", prose):
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
    parts = re.split(r"(\$\$[^$]*\$\$|\$[^$]*\$)", line)
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:
            if seg.startswith("$$"):
                # v120: display math hamesha apni line pe - dono taraf break
                if out and not "".join(out[-1:]).endswith("\n"):
                    out.append("\n")
                out.append(seg)
                out.append("\n")
                continue
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


def _both_txt(en, hi):
    """Bilingual mode: English ke neeche Hindi — dono medium ek saath (A+अ)."""
    en = (en or "").strip()
    hi = (hi or "").strip()
    if hi and hi != en:
        return (en + "\n" + hi) if en else hi
    return en


def _both_opts(en_list, hi_list):
    """Options bhi bilingual: 'A) text / हिंदी' — length mismatch pe sirf English."""
    en_list = list(en_list or [])
    hi_list = list(hi_list or [])
    if not hi_list or len(hi_list) != len(en_list):
        return en_list
    return [((e or "") + (" / " + h if h and h != e else "")) for e, h in zip(en_list, hi_list)]


def _blocks(text):
    text = _math_heal(text)  # v116: OCR/typed math repair (render-time, DB untouched)
    blocks = []
    for ln in _presplit(text):
        if _is_or_line(ln):
            blocks.append(("oralt", _or_token(ln), ln))
            continue
        low = _strip_rich(ln).lower()
        c = _clean(_strip_rich(ln))
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
        elif (low.startswith("$$") and low.rstrip().endswith("$$")) or \
                (low.startswith("\\[") and low.rstrip().endswith("\\]")):
            # v120: display-math line - lambi ho to bhi centered equation block
            blocks.append(("eq", c, ln))
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
        # R2 migration ke baad figure ek URL ho sakta hai (base64 nahi) — usse fetch karo
        if isinstance(raw, str) and raw.startswith("http"):
            try:
                import urllib.request
                _ir = urllib.request.Request(raw, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; "
                                                          "Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                                                          "Chrome/122 Safari/537.36"})
                with urllib.request.urlopen(_ir, timeout=15) as _r:
                    data = _r.read()
            except Exception:
                return  # fetch fail -> figure skip (PDF crash nahi hoga)
        else:
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


def validate_pdf(data, want_devanagari=False):
    """Lightweight post-generation sanity check (spec §6/§39). Returns (ok, issues).
    Never raises. Confirms the bytes are a real, non-empty PDF with at least one page,
    and (optionally) that Devanagari text made it into the file."""
    issues = []
    try:
        if not data or len(data) < 800:
            return False, ["empty or truncated PDF"]
        head = bytes(data[:5])
        if head[:4] != b"%PDF":
            issues.append("missing %PDF header")
        if b"%%EOF" not in bytes(data[-1024:]):
            issues.append("missing %%EOF trailer")
        blob = bytes(data)
        if b"/Type /Page" not in blob and b"/Type/Page" not in blob:
            issues.append("no page objects found")
        if want_devanagari:
            # a correctly embedded/shaped Devanagari PDF embeds the Noto font subset
            if b"Noto" not in blob and b"Devanagari" not in blob:
                issues.append("Devanagari font not embedded")
    except Exception as e:
        return False, ["validation error: %s" % e]
    return (len(issues) == 0), issues


def build_exam_pdf(ex, questions, medium="english"):
    from fpdf import FPDF
    is_hi = (medium == "hindi")
    is_both = (medium == "both")
    L = {
        "q":       ("\u092a\u094d\u0930. " if is_hi else "Q"),
        "marks":   ("\u0905\u0902\u0915" if is_hi else "marks"),
        "answer":  ("\u0909\u0924\u094d\u0924\u0930" if is_hi else "ANSWER"),
        "correct": ("\u0938\u0939\u0940 \u0909\u0924\u094d\u0924\u0930" if is_hi else "Correct"),
        "medium":  ("Bilingual (A + \u0905)" if is_both else ("\u0939\u093f\u0902\u0926\u0940 \u092e\u093e\u0927\u094d\u092f\u092e" if is_hi else "English Medium")),
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
    _register_dvs(pdf)
    # Devanagari font me alag italic file nahi hoti - regular/bold hi register
    # kar dete hain taaki *italic* markup pe PDF crash na kare
    try:
        pdf.add_font("Noto", "I", FONT)
        pdf.add_font("Noto", "BI", _font_path_bold() or FONT)
    except Exception:
        pass
    pdf.add_page()
    _enable_shaping(pdf, FONT)
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
    chips = [c for c in [ex.subject or "", L["medium"], ("%s: %s" % (L["total"], ex.total_marks)) if ex.total_marks is not None else ""] if c and c.strip()]
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
        qtext = (_both_txt(q.question_text, q.question_text_hi) if is_both else (q.question_text_hi if (is_hi and q.question_text_hi) else q.question_text)) or ""
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
        # marks pill (DPP jaise mark-less papers me skip)
        if getattr(q, "max_marks", None) is not None:
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

        # question body - "OR" wale question me har part ka apna figure:
        # Part A -> uska diagram -> OR -> Part B -> uska diagram
        qblocks = _blocks(qtext)
        alt_img = getattr(q, "alt_image_b64", None)
        has_or = any(k == "oralt" for k, _, _ in qblocks)
        drawn_first = False
        for kind, c, raw in qblocks:
            if kind == "oralt" and has_or and alt_img and not drawn_first:
                _img(pdf, q.image_b64)
                drawn_first = True
            _render_block(pdf, kind, c, LM, EPW, is_q=True, raw=raw)
        if drawn_first:
            _img(pdf, alt_img)
        else:
            _img(pdf, q.image_b64)
            if alt_img:
                _img(pdf, alt_img)

        if (ex.test_type or "") == "mcq":
            opts = (_both_opts(q.options, q.options_hi) if is_both else (q.options_hi if (is_hi and q.options_hi) else q.options)) or []
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
                    pdf.multi_cell(EPW, 7, "   %s)   %s      %s" % (chr(65 + idx), _clean(_strip_rich(str(op))), L["correct"]),
                                   new_x="LMARGIN", new_y="NEXT", fill=True, border=1)
                else:
                    pdf.set_text_color(28, 32, 40)
                    pdf.set_x(LM)
                    pdf.multi_cell(EPW, 7, "   %s)   %s" % (chr(65 + idx), _clean(_strip_rich(str(op)))),
                                   new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.8)
            pdf.set_text_color(0, 0, 0)
        else:
            ans = (_both_txt(q.model_answer, q.model_answer_hi) if is_both else (q.model_answer_hi if (is_hi and q.model_answer_hi) else q.model_answer)) or ""
            if ans.strip():
                pdf.ln(2.5)
                yy = pdf.get_y()
                if yy + 16 > pdf.h - 18:
                    pdf.add_page()
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

    _out = bytes(pdf.output())
    _ok, _iss = validate_pdf(_out, want_devanagari=(medium in ("hindi", "both")))
    if not _ok:
        raise RuntimeError("PDF validation failed: " + "; ".join(_iss))
    return _out


def _split_frac(raw, fx=True):
    """Agar raw (pre-clean) text me THEEK EK \\[dt]frac{num}{den} hai to cleaned
    (prefix, numerator, denominator, suffix) return karo taaki use asli stacked
    fraction ki tarah draw kiya ja sake. Zero ya ek se zyada frac (ya nested frac
    andar) ho to None - tab poori line _clean se flat (a)/(b) me render hoti hai,
    jo zyada consistent lagti hai."""
    if not raw:
        return None
    ms = list(_FRAC_NEST_RE.finditer(raw))
    if len(ms) != 1:
        return None
    m = ms[0]
    if "\\frac" in m.group(1) or "\\frac" in m.group(2):
        return None
    pre, post = raw[:m.start()], raw[m.end():]
    return (_clean(pre, fx=fx), _clean(m.group(1), fx=fx),
            _clean(m.group(2), fx=fx), _clean(post, fx=fx))


def _render_fraction(pdf, frac, LM, EPW, color, font="Noto"):
    """Draw prefix, a numerator/line/denominator stack, then suffix - a real
    vertical fraction like a textbook, instead of flattened '(a)/(b)' text."""
    # the stack is drawn with absolute coordinates in several pieces, so it must
    # never straddle a page break - move to a fresh page up-front if it won't fit
    if pdf.get_y() + 17 > pdf.h - 18:
        pdf.add_page()
    pre, num, den, post = frac
    # v120: bahut wide fraction ho to font shrink karke margin ke andar rakho
    sh = 1.0
    for _try in range(5):
        pdf.set_font(font, size=13 * sh)
        _pw = pdf.get_string_width(pre) if pre.strip() else 0
        _qw = pdf.get_string_width(post) if post.strip() else 0
        pdf.set_font(font, size=11 * sh)
        _sw = max(pdf.get_string_width(num), pdf.get_string_width(den)) + 5.5
        if _pw + _sw + _qw <= EPW - 4:
            break
        sh *= 0.88
    pdf.set_font(font, size=11 * sh)
    num_w, den_w = pdf.get_string_width(num), pdf.get_string_width(den)
    frac_w = max(num_w, den_w) + 5.5
    pdf.set_font(font, size=13 * sh)
    pre_w = pdf.get_string_width(pre) if pre.strip() else 0
    post_w = pdf.get_string_width(post) if post.strip() else 0
    total_w = pre_w + frac_w + post_w
    x0 = LM + max(0, (EPW - total_w) / 2)
    y0 = pdf.get_y() + 1.5
    pdf.set_text_color(*color)
    if pre.strip():
        pdf.set_xy(x0, y0 + 3.6)
        pdf.set_font(font, size=13 * sh)
        pdf.cell(pre_w, 6.5, pre, align="L")
    fx = x0 + pre_w
    pdf.set_font(font, size=11 * sh)
    pdf.set_xy(fx, y0)
    pdf.cell(frac_w, 5.5, num, align="C")
    pdf.set_draw_color(*color)
    pdf.set_line_width(0.4)
    pdf.line(fx + 1.5, y0 + 6.3, fx + frac_w - 1.5, y0 + 6.3)
    pdf.set_xy(fx, y0 + 6.8)
    pdf.cell(frac_w, 5.5, den, align="C")
    if post.strip():
        pdf.set_xy(fx + frac_w, y0 + 3.6)
        pdf.set_font(font, size=13 * sh)
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


def _render_block(pdf, kind, c, LM, EPW, is_q, raw=None, scale=1.0):
    def _S(v):
        return round(v * scale, 1)
    if kind == "oralt":
        # Alternative question separator - hamesha page ke beech me, bold
        pdf.ln(2.2)
        pdf.set_x(LM)
        _style_font(pdf, "B", _S(12))
        pdf.set_text_color(*NAVY)
        pdf.cell(EPW, 7.5, (c or "OR"), align="C")
        pdf.ln(9.5)
        pdf.set_text_color(20, 22, 28)
        return
    if kind == "head":
        acc = NAVY if is_q else NAVY2
        if _MAJOR_HEAD_RE.match(c.strip()):
            # major section heading: larger navy type - clean, no side bar
            pdf.ln(2.6)
            pdf.set_x(LM)
            pdf.set_font("Noto", "B", _S(13))
            pdf.set_text_color(*acc)
            pdf.multi_cell(EPW, _S(7.4), c, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.6)
        else:
            # minor connector line ("According to...", "Substitute the values:"):
            # coloured text only - no bar, so the left edge stays clean
            pdf.ln(1.4)
            pdf.set_x(LM)
            pdf.set_font("Noto", size=_S(11.8))
            pdf.set_text_color(*acc)
            pdf.multi_cell(EPW, _S(7), c, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(20, 22, 28)
    elif kind == "final":
        pdf.ln(2.2)
        yy = pdf.get_y()
        pdf.set_font("Noto", "B", _S(11.5))
        lines = pdf.multi_cell(EPW - 10, _S(7), c, dry_run=True, output="LINES")
        bh = _S(7) * max(1, len(lines)) + 5
        # box + text use absolute coords - jump to a fresh page if it won't fit
        if yy + bh + 3 > pdf.h - 18:
            pdf.add_page()
            yy = pdf.get_y()
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
        # v120: DejaVu (math glyphs) se render jab possible ho - ∫, ², log, brackets
        use_dvs = _dvs_ok_for(raw or c)
        fnt = "DVS" if use_dvs else "Noto"
        frac = _split_frac(raw, fx=not use_dvs)
        color = NAVY if is_q else NAVY2
        if frac:
            pdf.ln(0.8)
            _render_fraction(pdf, frac, LM, EPW, color, font=fnt)
        else:
            txt = _clean(_strip_rich(raw or ""), fx=not use_dvs) if use_dvs else c
            pdf.ln(1.6)
            try:
                pdf.set_font(fnt, size=_S(13.5))
            except Exception:
                fnt, use_dvs = "Noto", False
                pdf.set_font(fnt, size=_S(13.5))
                txt = c
            pdf.set_text_color(*color)
            pdf.set_x(LM)
            if pdf.get_string_width(txt) <= EPW - 4:
                pdf.cell(EPW, _S(8.5), txt, align="C")
                pdf.ln(10)
            else:
                # v120: lambi equation margin me wrap (centered)
                pdf.multi_cell(EPW, _S(7.5), txt, align="C",
                               new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3)
            pdf.set_text_color(20, 22, 28)
    elif kind == "bullet":
        pdf.set_x(LM + 4)
        _style_font(pdf, "", _S(11.5), base_bold=is_q)
        pdf.set_text_color(*(NAVY if is_q else GREEN))
        pdf.cell(5, _S(6.8), "\u2022")
        pdf.set_text_color(28, 32, 40)
        if not _write_rich(pdf, raw, LM + 9, EPW - 9, _S(11.5), _S(6.8),
                           base_bold=is_q, color=(28, 32, 40)):
            ctxt = _para_font(pdf, raw, c, _S(11.5), is_q)
            pdf.multi_cell(EPW - 9, _S(6.8), ctxt, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)
    else:
        # Question text bold rehta hai (exam paper style), answer normal weight
        if not _write_rich(pdf, raw, LM, EPW, _S(11.5), _S(6.8),
                           base_bold=is_q, color=(22, 26, 34)):
            pdf.set_x(LM)
            ctxt = _para_font(pdf, raw, c, _S(11.5), is_q)
            pdf.set_text_color(22, 26, 34)
            pdf.multi_cell(EPW, _S(6.8), ctxt, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)


# ====================================================================== marks stamp
def _fmt_num(v):
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else ("%.1f" % f)
    except Exception:
        return str(v)


def _stamp_image(data, mime, label_big, label_small, per_q=None):
    """Draw a green marks badge on the top-right corner of a photo answer sheet,
    plus a per-question marks panel below it (photo sheets have no searchable
    text positions, so the breakdown is listed under the total)."""
    import io as _io
    from PIL import Image as _Img, ImageDraw as _Draw, ImageFont as _Font
    im = _Img.open(_io.BytesIO(data)).convert("RGB")
    W, H = im.size
    d = _Draw.Draw(im)
    try:
        f_big = _Font.truetype(_font_path_bold() or _font_path(), max(22, W // 16))
        f_sm = _Font.truetype(_font_path(), max(13, W // 38))
        f_q = _Font.truetype(_font_path_bold() or _font_path(), max(14, W // 34))
    except Exception:
        f_big = _Font.load_default()
        f_sm = f_q = f_big
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
    # ---- per-question breakdown panel (looks like the checker's margin notes)
    rows = []
    for r in (per_q or [])[:14]:
        rows.append("Q%s:  %s / %s" % (r.get("q_no"), _fmt_num(r.get("marks", 0)),
                                       _fmt_num(r.get("max", 0))))
    if len(per_q or []) > 14:
        rows.append("...")
    if rows:
        qpad = max(8, W // 80)
        line_h = 0
        qw = 0
        for t in rows:
            qb = d.textbbox((0, 0), t, font=f_q)
            qw = max(qw, qb[2] - qb[0])
            line_h = max(line_h, qb[3] - qb[1])
        line_h = int(line_h * 1.45)
        pw, ph = qw + qpad * 2, line_h * len(rows) + qpad * 2
        px, py = W - m - pw, y1 + bh + max(6, W // 120)
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=max(6, W // 110),
                            fill=(255, 255, 255), outline=(200, 32, 40),
                            width=max(2, W // 500))
        yy = py + qpad
        for t in rows:
            d.text((px + qpad, yy), t, font=f_q, fill=(200, 32, 40))
            yy += line_h
    out = _io.BytesIO()
    if "png" in (mime or ""):
        im.save(out, format="PNG")
        return out.getvalue(), "image/png"
    im.save(out, format="JPEG", quality=92)
    return out.getvalue(), "image/jpeg"


_QLINE_RE = re.compile(
    r"^\s*(?:question|que|q|\u092a\u094d\u0930\u0936\u094d\u0928|\u092a\u094d\u0930)\s*\.?\s*(\d{1,2})\b",
    re.IGNORECASE)


def _find_question_positions(data):
    """Locate 'Q1.', 'Question 2', 'प्र. 3' line starts in a PDF answer sheet
    using pdfplumber -> {q_no: (page_index, top_in_points)}. Best-effort."""
    import io as _io
    positions = {}
    try:
        import pdfplumber
        with pdfplumber.open(_io.BytesIO(data)) as pp:
            for pi, page in enumerate(pp.pages):
                try:
                    words = page.extract_words() or []
                except Exception:
                    continue
                lines = {}
                for w in words:
                    lines.setdefault(round(w["top"] / 3.0), []).append(w)
                for key in sorted(lines):
                    ws = sorted(lines[key], key=lambda w: w["x0"])
                    text = " ".join(w["text"] for w in ws)
                    mm_ = _QLINE_RE.match(text)
                    if mm_:
                        qn = int(mm_.group(1))
                        if qn not in positions:
                            positions[qn] = (pi, float(ws[0]["top"]))
    except Exception:
        return {}
    return positions


def _stamp_pdf(data, label_big, label_small, per_q=None):
    """Overlay marks onto a PDF answer sheet: a green total badge on page 1 plus
    a red per-question chip in the right margin beside each detected question -
    the way a checker writes marks next to every answer."""
    import io as _io
    from pypdf import PdfReader, PdfWriter
    from fpdf import FPDF
    reader = PdfReader(_io.BytesIO(data))
    qpos = _find_question_positions(data) if per_q else {}

    ov = FPDF(unit="mm")
    ov.set_auto_page_break(False)
    ov.add_font("Noto", "", _font_path())
    ov.add_font("Noto", "B", _font_path_bold() or _font_path())
    _register_dvs(ov)
    RED = (200, 32, 40)
    for pi, p in enumerate(reader.pages):
        w_mm = float(p.mediabox.width) * 25.4 / 72.0
        h_mm = float(p.mediabox.height) * 25.4 / 72.0
        ov.add_page(format=(w_mm, h_mm))
        if pi == 0:
            # total badge (top-right)
            ov.set_font("Noto", "B", 15)
            bw = ov.get_string_width(label_big) + 12
            ov.set_font("Noto", size=7.5)
            bw = max(bw, ov.get_string_width(label_small) + 12)
            bh = 15.4
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
        # per-question chips beside each detected question line
        for r in (per_q or []):
            hit = qpos.get(int(r.get("q_no", -1)))
            if not hit or hit[0] != pi:
                continue
            txt = "%s / %s" % (_fmt_num(r.get("marks", 0)), _fmt_num(r.get("max", 0)))
            ov.set_font("Noto", "B", 10.5)
            tick_w = 4.2
            cw = ov.get_string_width(txt) + 7 + tick_w
            ch = 7.6
            cy = hit[1] * 25.4 / 72.0 - 1.4
            cy = max(2, min(cy, h_mm - ch - 2))
            cx = w_mm - cw - 4
            ov.set_fill_color(255, 255, 255)
            ov.set_draw_color(*RED)
            ov.set_line_width(0.5)
            ov.rect(cx, cy, cw, ch, style="DF", round_corners=True, corner_radius=1.8)
            # vector tick (font has no check-mark glyph)
            ov.set_line_width(0.75)
            tx0, ty0 = cx + 2.2, cy + ch / 2 + 0.4
            ov.line(tx0, ty0, tx0 + 1.1, ty0 + 1.4)
            ov.line(tx0 + 1.1, ty0 + 1.4, tx0 + 3.0, ty0 - 1.9)
            ov.set_xy(cx + tick_w, cy + 0.5)
            ov.set_text_color(*RED)
            ov.cell(cw - tick_w - 1, 6.6, txt, align="C")
    ov_reader = PdfReader(_io.BytesIO(bytes(ov.output())))
    writer = PdfWriter()
    for pi, p in enumerate(reader.pages):
        try:
            p.merge_page(ov_reader.pages[pi])
        except Exception:
            pass
        writer.add_page(p)
    out = _io.BytesIO()
    writer.write(out)
    return out.getvalue(), "application/pdf"


def stamp_marks_on_answer(data, mime, obtained, total, verdict=None, per_q=None):
    """Stamp total + per-question marks on a graded answer sheet (photo or PDF).
    Never raises - on any failure the original file is returned untouched so
    downloads keep working exactly as before."""
    try:
        label_big = "%s / %s" % (_fmt_num(obtained), _fmt_num(total))
        label_small = ("MARKS  \u00b7  " + _fx(str(verdict)).upper()) if verdict else "MARKS  \u00b7  CHECKED"
        m = (mime or "").lower()
        if "pdf" in m:
            return _stamp_pdf(data, label_big, label_small, per_q=per_q)
        return _stamp_image(data, m, label_big, label_small, per_q=per_q)
    except Exception:
        return data, (mime or "application/octet-stream")



# ============================================================ DPP premium layout
DPPGOLD = (143, 117, 17)       # image-4 jaisa olive-gold band
DPPGOLD_D = (112, 91, 13)      # darker separator line
DPPCHIP = (172, 143, 34)       # band ke andar lighter-gold chip
DPPCREAM = (250, 246, 232)     # instructions cream box
DPPTEXT = (30, 28, 22)


def _teacher_photo_circle(b64):
    """Teacher photo -> circular-cropped PNG temp file (PDF band ke liye).
    Kisi bhi step pe fail ho to None — caller logo/text pe fallback kare.
    R2 migration ke baad photo ek URL ho sakta hai (base64 nahi) -> usse fetch karo."""
    import base64 as _b
    import io as _io
    import tempfile as _tf
    from PIL import Image as _Im, ImageOps as _IO, ImageDraw as _ID
    if not b64:
        return None
    raw = None
    if isinstance(b64, str) and b64.startswith("http"):
        # R2 / http URL -> fetch bytes. Cloudflare bot-check se bachne ko browser User-Agent.
        import urllib.request as _u
        _req = _u.Request(b64, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                      "Chrome/122 Safari/537.36"})
        with _u.urlopen(_req, timeout=15) as _r:
            raw = _r.read()
    else:
        s = b64
        if isinstance(s, str) and s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        raw = _b.b64decode(s)
    im = _Im.open(_io.BytesIO(raw)).convert("RGB")
    im = _IO.exif_transpose(im)
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    im = im.resize((320, 320), _Im.LANCZOS)
    mask = _Im.new("L", (320, 320), 0)
    _ID.Draw(mask).ellipse((0, 0, 320, 320), fill=255)
    out = _Im.new("RGBA", (320, 320), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    tf = _tf.NamedTemporaryFile(delete=False, suffix=".png")
    tf.close()
    out.save(tf.name)
    return tf.name


def build_dpp_pdf(ex, questions, medium="english", kind="q"):
    """Professional DPP paper — gold-band layout (question paper / solutions).
    kind="q" -> sirf questions; kind="s" -> har question ke neeche model answer."""
    from fpdf import FPDF
    from datetime import datetime
    is_hi = (medium == "hindi")
    is_both = (medium == "both")
    L = {
        "subtitle": ("DAILY PRACTICE PAPER"),
        "qp":      ("प्रश्न पत्र" if is_hi else "QUESTION PAPER"),
        "sol":     ("उत्तर पत्र" if is_hi else "SOLUTIONS"),
        "question":("प्रश्न" if is_hi else "Question"),
        "qshort":  ("प्र." if is_hi else "Q"),
        "questions":("प्रश्न" if is_hi else "QUESTIONS"),
        "marks":   ("अंक" if is_hi else "MARKS"),
        "max":     ("पूर्णांक" if is_hi else "MAX MARKS"),
        "model":   ("\u0909\u0924\u094d\u0924\u0930" if is_hi else ("\u0909\u0924\u094d\u0924\u0930 / ANSWER" if is_both else "ANSWER")),
        "gi":      ("सामान्य निर्देश" if is_hi else "GENERAL INSTRUCTIONS"),
        "setby":   ("SET BY"),
        "faculty": ("Faculty"),
        "class_l": ("कक्षा" if is_hi else "CLASS"),
        "subject_l": ("विषय" if is_hi else "SUBJECT"),
        "medium_l": ("माध्यम" if is_hi else "MEDIUM"),
        "date_l":  ("तिथि" if is_hi else "DATE"),
        "instr_list": ([
            "सभी प्रश्न अनिवार्य हैं।",
            "उत्तर अपनी कॉपी में साफ़-साफ़ लिखें।",
            "हल करने के बाद अपनी answer PDF पोर्टल पर अपलोड करके Submit दबाएँ।",
            "Model answers submit करने के बाद खुलेंगे।",
        ] if is_hi else [
            "All questions are compulsory.",
            "Solve the paper neatly in your notebook.",
            "After solving, upload your answer PDF on the portal and press Submit.",
            "Model answers will unlock after you submit.",
        ]),
        "instr":   (("निर्देश: सभी प्रश्न अनिवार्य हैं। उत्तर अपनी कॉपी में साफ़-साफ़ लिखें। "
                     "हल करने के बाद अपनी answer PDF पोर्टल पर अपने teacher को submit करें।")
                    if is_hi else
                    ("Instructions: All questions are compulsory. Solve neatly in your notebook. "
                     "After solving, upload your answer PDF on the portal and press Submit.")),
        "medium":  ("Bilingual (A + अ)" if is_both else ("हिंदी माध्यम" if is_hi else "English Medium")),
    }
    FONT = _font_path()
    qs = list(questions or [])
    total_marks = None
    try:
        ms = [q.max_marks for q in qs if getattr(q, "max_marks", None) is not None]
        if ms and len(ms) == len(qs):
            total_marks = sum(ms)
    except Exception:
        total_marks = None

    class PDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Noto", "B", 8)
            self.set_text_color(*GREY)
            self.cell(0, 6, "MVS Foundation  ·  %s" % (getattr(ex, "teacher_name", "") or ""), align="L")
            self.set_y(-12)
            self.cell(0, 6, "Page %d" % self.page_no(), align="R")

    pdf = PDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_font("Noto", "", FONT)
    pdf.add_font("Noto", "B", _font_path_bold() or FONT)
    _register_dvs(pdf)
    try:
        pdf.add_font("Noto", "I", FONT)
        pdf.add_font("Noto", "BI", _font_path_bold() or FONT)
    except Exception:
        pass
    pdf.add_page()
    _enable_shaping(pdf, FONT)
    EPW = pdf.epw
    LM = pdf.l_margin

    # ============================================================
    # HEADER (image-267 style): white top — MVS logo + brand | SET BY + teacher
    # ============================================================
    HDR_H = 24
    _tname = _clean(getattr(ex, "teacher_name", "") or "")
    _subj  = _clean(getattr(ex, "subject", "") or "")
    # --- left: logo + brand name
    logo = _logo_path()
    lx, ly, ld = LM, 4.5, 15.5
    if logo:
        try:
            pdf.image(logo, x=lx, y=ly, w=ld, h=ld)
        except Exception:
            logo = None
    if not logo:
        # fallback: gold rounded box with MVS
        pdf.set_fill_color(*DPPGOLD)
        pdf.rect(lx, ly, ld, ld, style="F", round_corners=True, corner_radius=3)
        pdf.set_xy(lx, ly + 4.4)
        pdf.set_font("Noto", "B", 9.5)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(ld, 6, "MVS", align="C")
    bx = lx + ld + 5
    pdf.set_xy(bx, 5.2)
    pdf.set_font("Noto", "B", 14.5)
    pdf.set_text_color(31, 41, 55)
    pdf.cell(0, 7, "MVS Foundation")
    pdf.set_xy(bx, 12.6)
    pdf.set_font("Noto", "B", 7)
    pdf.set_text_color(120, 113, 92)
    pdf.cell(0, 4, "MANISH VERMA CLASSES")
    # --- right: SET BY + teacher name + subject Faculty + photo circle
    pd_ = 15.5                                  # photo diameter
    px = pdf.w - pdf.r_margin - pd_
    photo = None
    _tb64 = getattr(ex, "teacher_photo_b64", None)
    if _tb64:
        try:
            photo = _teacher_photo_circle(_tb64)
        except Exception:
            photo = None
    if photo:
        try:
            pdf.set_fill_color(255, 255, 255)
            pdf.ellipse(px - 1.1, ly - 1.1, pd_ + 2.2, pd_ + 2.2, style="F")
            pdf.set_draw_color(*DPPGOLD)
            pdf.set_line_width(0.5)
            pdf.ellipse(px - 1.1, ly - 1.1, pd_ + 2.2, pd_ + 2.2, style="D")
            pdf.image(photo, x=px, y=ly, w=pd_, h=pd_)
        except Exception:
            pass
    else:
        # initials circle (gold)
        pdf.set_fill_color(*DPPGOLD)
        pdf.ellipse(px, ly, pd_, pd_, style="F")
        ini = "".join(w[0] for w in (_tname or "T").split()[:2]).upper()
        pdf.set_xy(px, ly + 4.6)
        pdf.set_font("Noto", "B", 8.5)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(pd_, 6, ini, align="C")
    tx = LM + 40                                # text block: right-aligned, ends photo se pehle
    tw2 = px - 4 - tx
    pdf.set_xy(tx, 4.2)
    pdf.set_font("Noto", "B", 6)
    pdf.set_text_color(156, 147, 120)
    pdf.cell(tw2, 3.6, L["setby"], align="R")
    pdf.set_xy(tx, 8.2)
    pdf.set_font("Noto", "B", 10.5)
    pdf.set_text_color(31, 41, 55)
    pdf.cell(tw2, 5, _tname or "Teacher", align="R")
    pdf.set_xy(tx, 13.8)
    pdf.set_font("Noto", "B", 7)
    pdf.set_text_color(120, 113, 92)
    pdf.cell(tw2, 4, ((_subj + " ") if _subj else "") + L["faculty"], align="R")
    # thin rule under header
    pdf.set_draw_color(226, 220, 200)
    pdf.set_line_width(0.3)
    pdf.line(LM, HDR_H - 1.5, pdf.w - pdf.r_margin, HDR_H - 1.5)

    # ============================================================
    # GOLD BAND: bada title + DPP subtitle + chapter | stat boxes right
    # ============================================================
    BAND_Y, BAND_H = HDR_H + 2.5, 27
    pdf.set_fill_color(*DPPGOLD)
    pdf.rect(0, BAND_Y, pdf.w, BAND_H, style="F")
    pdf.set_fill_color(*DPPGOLD_D)
    pdf.rect(0, BAND_Y + BAND_H, pdf.w, 1.4, style="F")
    # stat boxes (right): QUESTIONS + MAX MARKS (agar marks hain)
    stats = [(str(len(qs)), L["questions"])]
    if total_marks is not None:
        stats.append((str(total_marks), L["max"]))
    sbw, sbh, sgap = 25, 17.5, 4
    sx = pdf.w - pdf.r_margin - (sbw * len(stats) + sgap * (len(stats) - 1))
    sy = BAND_Y + (BAND_H - sbh) / 2
    for val, lab in stats:
        pdf.set_fill_color(255, 255, 255)
        pdf.rect(sx, sy, sbw, sbh, style="F", round_corners=True, corner_radius=2.2)
        pdf.set_xy(sx, sy + 2.2)
        pdf.set_font("Noto", "B", 12.5)
        pdf.set_text_color(*DPPGOLD_D)
        pdf.cell(sbw, 7, val, align="C")
        pdf.set_xy(sx, sy + 10.6)
        pdf.set_font("Noto", "B", 5.8)
        pdf.set_text_color(120, 110, 80)
        pdf.cell(sbw, 3.6, lab, align="C")
        sx += sbw + sgap
    # title + subtitle (chapter dedup ke saath)
    _title = _clean(getattr(ex, "title", "") or "DPP")
    _ch_raw = _clean(getattr(ex, "chapter", "") or "")
    _pt_raw = _clean(getattr(ex, "part", "") or "")
    _chp = " · ".join(c for c in [_ch_raw, _pt_raw] if c)
    _sub_line = L["subtitle"] + (("   ·   " + _chp) if _chp else "")
    text_w = (pdf.w - pdf.r_margin - (sbw * len(stats) + sgap * (len(stats) - 1)) - 8) - LM
    pdf.set_xy(LM, BAND_Y + 5)
    pdf.set_font("Noto", "B", 15 if len(_title) > 60 else 17)
    pdf.set_text_color(255, 255, 255)
    pdf.multi_cell(text_w, 7.4, _title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(LM)
    pdf.set_font("Noto", "B", 7.6)
    pdf.set_text_color(255, 238, 196)
    pdf.multi_cell(text_w, 4.4, _sub_line, new_x="LMARGIN", new_y="NEXT")

    # ============================================================
    # INFO ROW: CLASS / SUBJECT / MEDIUM / DATE + QUESTION PAPER·SOLUTIONS badge
    # ============================================================
    iy = BAND_Y + BAND_H + 6
    _cls = _clean(getattr(ex, "class_name", "") or "") or "—"
    _med_lbl = ("हिंदी" if is_hi else "English")
    info = [(L["class_l"], _cls), (L["subject_l"], _subj or "—"),
            (L["medium_l"], _med_lbl), (L["date_l"], datetime.now().strftime("%d %b %Y"))]
    tag = L["sol"] if kind == "s" else L["qp"]
    pdf.set_font("Noto", "B", 8.5)
    tagw = pdf.get_string_width(tag) + 12
    ib_gap = 4
    ibw = (EPW - tagw - ib_gap * len(info)) / len(info)
    ix = LM
    for lab, val in info:
        pdf.set_fill_color(255, 255, 255)
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(ix, iy, ibw, 14.5, style="DF", round_corners=True, corner_radius=2.2)
        pdf.set_xy(ix + 3.5, iy + 2.2)
        pdf.set_font("Noto", "B", 6.2)
        pdf.set_text_color(*GREY)
        pdf.cell(ibw - 7, 3.6, lab)
        pdf.set_xy(ix + 3.5, iy + 6.8)
        pdf.set_font("Noto", "B", 9.5)
        pdf.set_text_color(*DPPTEXT)
        pdf.cell(ibw - 7, 5.5, val)
        ix += ibw + ib_gap
    # question paper / solutions badge (solid)
    pdf.set_fill_color(*(GREEN if kind == "s" else DPPGOLD))
    pdf.rect(ix, iy, tagw, 14.5, style="F", round_corners=True, corner_radius=2.2)
    pdf.set_xy(ix, iy + 4.6)
    pdf.set_font("Noto", "B", 8.5)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(tagw, 6, tag, align="C")

    # ============================================================
    # GENERAL INSTRUCTIONS (automatic numbered list) — cream box, gold left bar
    # ============================================================
    gy = iy + 20
    items = L["instr_list"]
    gih = 8 + len(items) * 5.4
    pdf.set_fill_color(*DPPCREAM)
    pdf.set_draw_color(232, 220, 180)
    pdf.set_line_width(0.3)
    pdf.rect(LM, gy, EPW, gih, style="DF", round_corners=True, corner_radius=2)
    pdf.set_fill_color(*DPPGOLD)
    pdf.rect(LM, gy, 2.2, gih, style="F")
    pdf.set_xy(LM + 6, gy + 2.6)
    pdf.set_font("Noto", "B", 8.5)
    pdf.set_text_color(*DPPGOLD_D)
    pdf.cell(0, 5, L["gi"])
    yy = gy + 8.6
    pdf.set_font("Noto", size=9)
    pdf.set_text_color(96, 80, 20)
    for i, it in enumerate(items, 1):
        pdf.set_xy(LM + 6, yy)
        pdf.cell(EPW - 12, 5, "%d.  %s" % (i, it))
        yy += 5.4
    pdf.set_xy(LM, gy + gih + 6)

    # ---------- questions
    for q in qs:
        qtext = (_both_txt(getattr(q, "question_text", ""), getattr(q, "question_text_hi", "")) if is_both else getattr(q, "question_text_hi", None) if (is_hi and getattr(q, "question_text_hi", None))
                 else getattr(q, "question_text", "")) or ""
        if pdf.get_y() > pdf.h - 55:
            pdf.add_page()
        y0 = pdf.get_y()
        # test-jaisa gold "Q1" badge + marks pill (right)
        badge = "%s%s" % (L["qshort"], getattr(q, "q_no", ""))
        pdf.set_fill_color(*DPPGOLD)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Noto", "B", 11.5)
        bw2 = pdf.get_string_width(badge) + 10
        pdf.rect(LM, y0, bw2, 9, style="F", round_corners=True, corner_radius=2.5)
        pdf.set_xy(LM, y0 + 0.6)
        pdf.cell(bw2, 7.8, badge, align="C")
        if getattr(q, "max_marks", None) is not None:
            pill = "%d %s" % (q.max_marks, L["marks"])
            pdf.set_font("Noto", "B", 8.5)
            pw = pdf.get_string_width(pill) + 9
            pdf.set_fill_color(*LIGHT)
            pdf.set_draw_color(*BORDER)
            pdf.rect(pdf.w - pdf.r_margin - pw, y0, pw, 9, style="DF", round_corners=True, corner_radius=2.5)
            pdf.set_xy(pdf.w - pdf.r_margin - pw, y0 + 0.6)
            pdf.set_text_color(*GREY)
            pdf.cell(pw, 7.8, pill, align="C")
        pdf.set_xy(LM, y0 + 12.5)
        pdf.set_text_color(*DPPTEXT)

        qblocks = _blocks(qtext)
        alt_img = getattr(q, "alt_image_b64", None)
        has_or = any(k == "oralt" for k, _, _ in qblocks)
        drawn_first = False
        for kb, c, raw in qblocks:
            if kb == "oralt" and has_or and alt_img and not drawn_first:
                _img(pdf, getattr(q, "image_b64", None))
                drawn_first = True
            _render_block(pdf, kb, c, LM, EPW, is_q=True, raw=raw, scale=0.88)
        if drawn_first:
            _img(pdf, alt_img)
        else:
            _img(pdf, getattr(q, "image_b64", None))
            if alt_img:
                _img(pdf, alt_img)

        # options (A)-(D) agar hain
        opts = (_both_opts(getattr(q, "options", None), getattr(q, "options_hi", None)) if is_both else getattr(q, "options_hi", None) if (is_hi and getattr(q, "options_hi", None))
                else getattr(q, "options", None)) or []
        if opts:
            pdf.ln(1.5)
            for idx, op in enumerate(opts):
                pdf.set_font("Noto", size=10)
                pdf.set_text_color(28, 32, 40)
                pdf.set_x(LM)
                pdf.multi_cell(EPW, 6.6, "   (%s)   %s" % (chr(65 + idx), _clean(_strip_rich(str(op)))),
                               new_x="LMARGIN", new_y="NEXT")
                pdf.ln(0.8)

        # solutions paper: model answer
        if kind == "s":
            ans = (_both_txt(getattr(q, "model_answer", ""), getattr(q, "model_answer_hi", "")) if is_both else getattr(q, "model_answer_hi", None) if (is_hi and getattr(q, "model_answer_hi", None))
                   else getattr(q, "model_answer", "")) or ""
            if ans.strip() or getattr(q, "model_answer_image", None):
                pdf.ln(2.5)
                yy = pdf.get_y()
                if yy + 16 > pdf.h - 18:
                    pdf.add_page()
                    yy = pdf.get_y()
                pdf.set_fill_color(*GREEN)
                pdf.set_text_color(255, 255, 255)
                pdf.set_font("Noto", "B", 9.5)
                lw = pdf.get_string_width(L["model"]) + 10
                pdf.rect(LM, yy, lw, 7.5, style="F", round_corners=True, corner_radius=2)
                pdf.set_xy(LM, yy + 0.5)
                pdf.cell(lw, 6.5, L["model"], align="C")
                pdf.set_xy(LM, yy + 11)
                for kb, c, raw in _blocks(ans):
                    _render_block(pdf, kb, c, LM, EPW, is_q=False, raw=raw, scale=0.9)
                _img(pdf, getattr(q, "model_answer_image", None))

        # separator rule
        pdf.ln(3)
        pdf.set_draw_color(*BORDER)
        pdf.set_line_width(0.3)
        pdf.line(LM, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(4.5)

    _out = bytes(pdf.output())
    _ok, _iss = validate_pdf(_out, want_devanagari=(medium in ("hindi", "both")))
    if not _ok:
        raise RuntimeError("PDF validation failed: " + "; ".join(_iss))
    return _out
