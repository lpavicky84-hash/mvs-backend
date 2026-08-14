"""
translation_engine.py — subject-aware Hindi translation quality layer for MVS Class Manager.

Pure logic (no network). grading.py calls Gemini; this module wraps that with:
  - protected-token extraction/restore (LaTeX, formulae, numbers, units, scientific names)
  - subject/chapter glossary lookup + prompt injection + locked-term enforcement
  - a validation engine (structure, numbers, formulae, ordinals, "was it translated")
  - a confidence score (0-100)
  - legacy content scan + classification (verified / repair / retranslate)

Everything is defensive: bad input never raises, it just returns a lower confidence.
"""
import re
import json

# ----------------------------------------------------------------- ordinals
_ORD_RE = re.compile(r'(\d)[ \t\u00A0]+(?=(?:वीं|वाँ|वां|वें|वीँ))')


def fix_ordinals(s):
    """Join a number to a following Hindi ordinal suffix: '12 वीं' -> '12वीं'."""
    if not s or not isinstance(s, str):
        return s
    return _ORD_RE.sub(r'\1', s)


# ----------------------------------------------------------------- protected tokens
# Order matters: most specific first.
_TOKEN_PATTERNS = [
    re.compile(r'\$[^$]{1,400}\$'),                       # inline LaTeX  $...$
    re.compile(r'\\\([^)]{1,400}\\\)'),                   # \( ... \)
    re.compile(r'\\ce\{[^}]{1,200}\}'),                  # \ce{...} chemistry
    re.compile(r'\\[a-zA-Z]+(?:\{[^}]{0,120}\})?'),      # \frac{}{}, \sqrt{}, \alpha ...
    re.compile(r'\b[A-Z][a-z]?\d+(?:[A-Z][a-z]?\d*)+'),  # chemical formula H2O, H2SO4, KMnO4
    re.compile(r'\d+(?:\.\d+)?\s?(?:m/s2|m/s|km/h|kg|mol|cm|mm|km|nm|Hz|N|J|W|V|A|Ω|°C|°|%)'),  # number+unit
]


def protect_tokens(text):
    """Replace protected fragments with placeholders. Returns (masked_text, tokens)."""
    if not text or not isinstance(text, str):
        return text, {}
    tokens = {}
    idx = [0]

    def _sub(m):
        key = "\u27e6T%d\u27e7" % idx[0]           # ⟦T0⟧ — unlikely to be altered by the model
        tokens[key] = m.group(0)
        idx[0] += 1
        return key

    out = text
    for pat in _TOKEN_PATTERNS:
        out = pat.sub(_sub, out)
    return out, tokens


def restore_tokens(text, tokens):
    """Put protected fragments back. Tolerant of minor spacing the model may add."""
    if not text or not tokens:
        return text
    out = text
    for key, val in tokens.items():
        if key in out:
            out = out.replace(key, val)
        else:
            # model may have inserted a space inside the placeholder brackets
            loose = re.sub(r'\s+', r'\\s*', re.escape(key))
            out = re.sub(loose, lambda _m, v=val: v, out)
    return out


# ----------------------------------------------------------------- glossary
def glossary_rows(db, subject, chapter=""):
    """Fetch glossary rows relevant to (subject, chapter), most specific first.
    Returns a list of plain dicts. Safe if the table/db is unavailable."""
    try:
        from models import TranslationGlossary as G
    except Exception:
        return []
    if db is None:
        return []
    try:
        subj = (subject or "").strip()
        rows = db.query(G).filter(
            (G.subject == "") | (G.subject == subj)
        ).all()
    except Exception:
        return []
    ch = (chapter or "").strip().lower()
    out = []
    for r in rows:
        rc = (r.chapter or "").strip().lower()
        # chapter row only applies to its chapter; blank chapter = whole subject/global
        if rc and rc != ch:
            continue
        scope = 2 if (r.chapter or "").strip() else (1 if (r.subject or "").strip() else 0)
        out.append({
            "english": (r.english_term or "").strip(),
            "hindi": (r.preferred_hindi or "").strip(),
            "dnt": bool(r.do_not_translate),
            "locked": bool(r.locked),
            "priority": (r.priority or 0) + scope * 10,
        })
    out.sort(key=lambda x: x["priority"], reverse=True)
    return out


def glossary_prompt(rows):
    """Render glossary rows into a prompt block. Empty string when no rows."""
    if not rows:
        return ""
    lines = []
    for r in rows[:80]:
        if not r["english"]:
            continue
        if r["dnt"]:
            lines.append("- '%s': keep in English (do not translate)" % r["english"])
        elif r["hindi"]:
            tag = " [MUST USE]" if r["locked"] else ""
            lines.append("- '%s' = '%s'%s" % (r["english"], r["hindi"], tag))
    if not lines:
        return ""
    return ("APPROVED GLOSSARY (use these exact Hindi terms; [MUST USE] terms are mandatory):\n"
            + "\n".join(lines) + "\n")


def enforce_glossary(hindi, english, rows):
    """After translation, if a LOCKED English term still appears verbatim in the Hindi
    (i.e. the model left it in English), swap it for the approved Hindi. Conservative:
    only touches locked terms with a preferred Hindi, whole-word, case-insensitive."""
    if not hindi or not rows:
        return hindi
    out = hindi
    for r in rows:
        if not r["locked"] or not r["hindi"] or not r["english"] or r["dnt"]:
            continue
        try:
            pat = re.compile(r'\b' + re.escape(r["english"]) + r'\b', re.IGNORECASE)
            out = pat.sub(r["hindi"], out)
        except Exception:
            continue
    return out


# ----------------------------------------------------------------- validation
_DEV_RE = re.compile(r'[\u0900-\u097F]')                 # any Devanagari char
_NUM_RE = re.compile(r'\d+(?:\.\d+)?')
_LATEX_RE = re.compile(r'\$[^$]+\$|\\[a-zA-Z]+|\\ce\{[^}]+\}')
_ORD_SPLIT_RE = re.compile(r'\d[ \t\u00A0]+(?:वीं|वाँ|वां|वें)')


def _multiset(seq):
    d = {}
    for x in seq:
        d[x] = d.get(x, 0) + 1
    return d


def validate(english, hindi, is_math=False):
    """Compare a source English string with its Hindi translation.
    Returns (issues:list[str], confidence:int 0-100)."""
    english = english or ""
    hindi = hindi or ""
    issues = []

    if not hindi.strip():
        return (["empty translation"], 0)

    # 1. was it actually translated? (has Devanagari, unless the source is pure formula/number)
    has_words = bool(re.search(r'[A-Za-z]{2,}', english))
    if has_words and not _DEV_RE.search(hindi):
        issues.append("not translated (no Hindi text)")
    if has_words and hindi.strip() == english.strip():
        issues.append("identical to English (untranslated)")

    # 2. numbers preserved
    en_nums = _multiset(_NUM_RE.findall(english))
    hi_nums = _multiset(_NUM_RE.findall(hindi))
    for n, c in en_nums.items():
        if hi_nums.get(n, 0) < c:
            issues.append("number missing/changed: %s" % n)
            break

    # 3. LaTeX / formula tokens preserved
    en_tex = _multiset(_LATEX_RE.findall(english))
    hi_tex = _multiset(_LATEX_RE.findall(hindi))
    for t, c in en_tex.items():
        if hi_tex.get(t, 0) < c:
            issues.append("formula/LaTeX altered or missing")
            break

    # 4. ordinal rendering (number split from its Hindi suffix)
    if _ORD_SPLIT_RE.search(hindi):
        issues.append("ordinal split (e.g. '12 वीं' should be '12वीं')")

    # 5. bracket/paren balance drifting a lot
    if abs(english.count("(") - hindi.count("(")) > 1:
        issues.append("parenthesis count changed")

    # confidence
    conf = 100
    weights = {
        "empty translation": 100, "not translated (no Hindi text)": 70,
        "identical to English (untranslated)": 60, "ordinal split": 15,
        "formula/LaTeX altered or missing": 40, "parenthesis count changed": 10,
    }
    for iss in issues:
        base = next((w for k, w in weights.items() if iss.startswith(k)), 25)
        conf -= base
    return (issues, max(0, min(100, conf)))


def validate_pack(english, hindi, answer_en="", answer_hi="", options_en=None, options_hi=None):
    """Validate a whole question pack. Returns {issues, confidence, fields}."""
    options_en = options_en or []
    options_hi = options_hi or []
    all_issues = []
    scores = []

    iss, sc = validate(english, hindi)
    if iss:
        all_issues += ["Q: " + i for i in iss]
    scores.append(sc)

    if (answer_en or "").strip():
        iss, sc = validate(answer_en, answer_hi)
        if iss:
            all_issues += ["Answer: " + i for i in iss]
        scores.append(sc)

    if options_en:
        if len(options_hi) != len(options_en):
            all_issues.append("Options: count mismatch (%d vs %d)" % (len(options_en), len(options_hi)))
            scores.append(30)
        for i, oe in enumerate(options_en):
            oh = options_hi[i] if i < len(options_hi) else ""
            iss, sc = validate(oe, oh)
            if iss:
                all_issues += ["Option %s: %s" % (chr(65 + i), x) for x in iss]
            scores.append(sc)

    conf = int(sum(scores) / len(scores)) if scores else 0
    return {"issues": all_issues, "confidence": conf}


# ----------------------------------------------------------------- legacy scan
def classify(confidence, issues):
    """verified | repair | retranslate  (spec §60 legacy pipeline)."""
    hard = any(("not translated" in i or "identical" in i or "empty" in i or "count mismatch" in i)
               for i in issues)
    if hard or confidence < 55:
        return "retranslate"
    fixable = all(("ordinal" in i or "parenthesis" in i) for i in issues) if issues else True
    if issues and fixable:
        return "repair"
    if confidence >= 85 and not issues:
        return "verified"
    return "repair" if confidence >= 55 else "retranslate"


def repair(hindi):
    """Deterministic safe fixes that never change meaning (currently: ordinal join)."""
    return fix_ordinals(hindi or "")


def scan_pair(english, hindi, answer_en="", answer_hi="", options_en=None, options_hi=None):
    """Validate + classify a stored English/Hindi pair for the legacy scanner."""
    v = validate_pack(english, hindi, answer_en, answer_hi, options_en, options_hi)
    cls = classify(v["confidence"], v["issues"])
    return {"confidence": v["confidence"], "issues": v["issues"], "classification": cls}
