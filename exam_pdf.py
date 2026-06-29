"""
PDF generation for exam question+answer papers (English medium + Hindi medium).
Uses fpdf2 + uharfbuzz text-shaping with a bundled Noto Sans Devanagari font so
Hindi (Devanagari) renders correctly. Requires:  pip install fpdf2 uharfbuzz
and the font file at fonts/NotoSansDevanagari-Regular.ttf
"""
import os, re, io, base64

def _font_path():
    """Find the bundled Devanagari font whether it sits in a fonts/ folder or in
    the repo root (so it works no matter where it was uploaded)."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "fonts", "NotoSansDevanagari-Regular.ttf"),
        os.path.join(here, "NotoSansDevanagari-Regular.ttf"),
        os.path.join(os.getcwd(), "fonts", "NotoSansDevanagari-Regular.ttf"),
        os.path.join(os.getcwd(), "NotoSansDevanagari-Regular.ttf"),
        "fonts/NotoSansDevanagari-Regular.ttf",
        "NotoSansDevanagari-Regular.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

_TEX_MAP = [
    (r"\\times", "\u00d7"), (r"\\cdot", "\u00b7"), (r"\\div", "\u00f7"),
    (r"\\pm", "\u00b1"), (r"\\mp", "\u2213"), (r"\\circ", "\u00b0"), (r"\\degree", "\u00b0"),
    (r"\\alpha", "\u03b1"), (r"\\beta", "\u03b2"), (r"\\gamma", "\u03b3"),
    (r"\\theta", "\u03b8"), (r"\\phi", "\u03c6"), (r"\\pi", "\u03c0"),
    (r"\\Delta", "\u0394"), (r"\\delta", "\u03b4"), (r"\\lambda", "\u03bb"),
    (r"\\mu", "\u03bc"), (r"\\omega", "\u03c9"), (r"\\Omega", "\u03a9"),
    (r"\\infty", "\u221e"), (r"\\rightarrow", "\u2192"), (r"\\to", "\u2192"),
    (r"\\leftarrow", "\u2190"), (r"\\geq", "\u2265"), (r"\\leq", "\u2264"),
    (r"\\neq", "\u2260"), (r"\\approx", "\u2248"), (r"\\sum", "\u03a3"),
]


def _clean(text):
    """Turn LaTeX/chemistry source into readable plain text for the PDF."""
    t = text or ""
    t = re.sub(r"\\ce\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\(text|mathrm|mathbf|bf|textbf)\{([^{}]*)\}", r"\2", t)
    t = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", "\u221a(\\1)", t)
    for pat, rep in _TEX_MAP:
        t = re.sub(pat, rep, t)
    t = re.sub(r"\^\{([^{}]*)\}", r"^\1", t)
    t = re.sub(r"_\{([^{}]*)\}", r"_\1", t)
    t = t.replace("$", "")
    t = t.replace("\\\\", "\n")
    t = re.sub(r"\\[,;: ]", " ", t)
    return t.strip()


def _img(pdf, b64str):
    if not b64str:
        return
    try:
        raw = b64str
        if raw.startswith("data:") and "," in raw:
            raw = raw.split(",", 1)[1]
        data = base64.b64decode(raw)
        pdf.image(io.BytesIO(data), w=min(90, pdf.epw))
        pdf.ln(2)
    except Exception:
        pass


def build_exam_pdf(ex, questions, medium="english"):
    """Return PDF bytes for the exam in the given medium ('english' or 'hindi')."""
    from fpdf import FPDF
    is_hi = (medium == "hindi")
    L = {
        "q":       ("\u092a\u094d\u0930\u0936\u094d\u0928 " if is_hi else "Q"),
        "marks":   ("\u0905\u0902\u0915" if is_hi else "marks"),
        "answer":  ("\u0909\u0924\u094d\u0924\u0930:" if is_hi else "Answer:"),
        "correct": ("\u2713 \u0938\u0939\u0940 \u0909\u0924\u094d\u0924\u0930" if is_hi else "(correct)"),
        "medium":  ("\u0939\u093f\u0902\u0926\u0940 \u092e\u093e\u0927\u094d\u092f\u092e" if is_hi else "English medium"),
        "total":   ("\u0915\u0941\u0932" if is_hi else "Total"),
    }
    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()
    pdf.add_font("Noto", "", _font_path())
    pdf.set_text_shaping(True)

    def MC(h, txt):
        pdf.multi_cell(0, h, txt, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Noto", size=16)
    MC(9, _clean(ex.title or "Test"))
    pdf.set_font("Noto", size=10)
    pdf.set_text_color(90, 90, 90)
    meta = "%s   |   %s   |   %s: %s %s   |   %s" % (
        ex.subject or "", L["medium"], L["total"], ex.total_marks, L["marks"], ex.teacher_name or "")
    MC(6, meta)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    pdf.set_draw_color(210, 210, 210)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)

    for q in questions:
        qtext = (q.question_text_hi if (is_hi and q.question_text_hi) else q.question_text) or ""
        pdf.set_font("Noto", size=12)
        MC(7, "%s%d.   (%d %s)" % (L["q"], q.q_no, q.max_marks, L["marks"]))
        pdf.set_font("Noto", size=11)
        MC(7, _clean(qtext))
        _img(pdf, q.image_b64)

        if (ex.test_type or "") == "mcq":
            opts = (q.options_hi if (is_hi and q.options_hi) else q.options) or []
            for idx, op in enumerate(opts):
                is_corr = q.correct_option and str(op).strip() == str(q.correct_option).strip()
                if is_corr:
                    pdf.set_text_color(30, 110, 50)
                MC(7, "      %s)   %s%s" % (
                    chr(65 + idx), _clean(str(op)), ("    " + L["correct"]) if is_corr else ""))
                pdf.set_text_color(0, 0, 0)
        else:
            ans = (q.model_answer_hi if (is_hi and q.model_answer_hi) else q.model_answer) or ""
            if ans.strip():
                pdf.set_font("Noto", size=10)
                pdf.set_text_color(30, 90, 40)
                MC(6, L["answer"] + " " + _clean(ans))
                pdf.set_text_color(0, 0, 0)
            _img(pdf, q.model_answer_image)
        pdf.ln(4)

    return bytes(pdf.output())
