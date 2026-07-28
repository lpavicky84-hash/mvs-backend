# ---------------------------------------------------------------------------
# NIOS Canonical Subject Registry
#
# Identity rule (bulletproof):
#   * Same CLASS + same subject (case/spelling koi bhi)  -> EK HI subject
#     ("physics", "PHYSICS", " Physics " sab "Physics" hi hain)
#   * Different CLASS + same name -> ALAG subjects (English 202 Cl-10 vs 302 Cl-12)
#   * Same CLASS + same name + different NIOS codes -> ALAG subjects
#     (Data Entry Operations 336 vs 632 — dono Class 12)
#   * Code hi final truth hai; naam resolve hote hain official NIOS naam pe.
# ---------------------------------------------------------------------------
import re

try:
    from syllabus_data import SUBJECTS as _SYL_SUBJECTS
except Exception:
    _SYL_SUBJECTS = {}

# NIOS codes jo syllabus planner me nahi hain lekin identity ke liye jaane-jane chahiye
_EXTRA_CODES = {
    "12": [("632", "Data Entry Operations")],
    "10": [],
}

# ---------------------------------------------------------------------------
# official tables: cls -> [(code, name)]
# ---------------------------------------------------------------------------
def _build():
    off = {"10": [], "12": []}
    for cls, lst in (_SYL_SUBJECTS or {}).items():
        c = "10" if str(cls).strip() == "10" else ("12" if str(cls).strip() == "12" else None)
        if not c:
            continue
        for s in lst:
            try:
                off[c].append((str(s["code"]), s["name"]))
            except Exception:
                try:
                    off[c].append((str(s.code), s.name))
                except Exception:
                    pass
    for c, extra in _EXTRA_CODES.items():
        for code, nm in extra:
            if (code, nm) not in off[c]:
                off[c].append((code, nm))
    return off

_OFFICIAL = _build()

# squashed alias words (NIOS PDFs / class manager abbreviations)
_WORD_ALIAS = [
    (r"\bop\b", "operations"),
    (r"\bsci\b", "science"),
    (r"\btech\b", "technology"),
    (r"\bbus\b", "business"),
    (r"\bstuds?\b|\bstudy\b", "studies"),
    (r"\bacc\b", "accountancy"),
    (r"\beng\b", "english"),
    (r"\bchem\b", "chemistry"),
    (r"\bphy\b", "physics"),
    (r"\bmaths\b", "mathematics"),
    (r"\bbio\b", "biology"),
    (r"\beco\b", "economics"),
    (r"\bcomp\b", "computer"),
    (r"\bhin\b", "hindi"),
    (r"\bsst\b|\bsocial\b", "social science"),
]

def squash(s):
    """Case/space/punctuation-free compare key: 'HOME SCIENCE' == 'Home Science'."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())

def _expand_words(s):
    t = " " + re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()) + " "
    for pat, rep in _WORD_ALIAS:
        t = re.sub(pat, " " + rep + " ", t)
    return re.sub(r"\s+", " ", t).strip()

def _explicit_code(s):
    """Naam me likha NIOS code nikalo: '(632)', '[336]', '- 229', 'DEO 229' end me."""
    s = str(s or "")
    m = re.search(r"[\(\[]\s*(\d{3})\s*[\)\]]", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|[^0-9])(\d{3})\s*$", s)
    if m:
        return m.group(1)
    return ""

def _cls_digits(cls):
    m = re.search(r"\d+", str(cls or ""))
    return m.group(0) if m else ""

# indexes
_BY_CODE = {}
for _c, _lst in _OFFICIAL.items():
    for _code, _nm in _lst:
        _BY_CODE[_code] = (_nm, _c)

def _candidates(cls):
    if cls and cls in _OFFICIAL:
        return list(_OFFICIAL[cls])
    return _OFFICIAL["10"] + _OFFICIAL["12"]

def canon_subject(name, cls=None):
    """Kisi bhi raw subject naam ko resolve karo.
    Returns dict: {"name": official_name, "code": code|None, "ambiguous": bool}
    ya None agar NIOS list me nahi mila (custom subject — caller original rakhe).
    cls = '10'/'12'/Class string (optional; mila to usi class me dhundho)."""
    cls = _cls_digits(cls)
    if cls not in ("10", "12"):
        cls = None
    raw = str(name or "").strip()
    if not raw:
        return None
    # 1) explicit code = final truth
    code = _explicit_code(raw)
    if code and code in _BY_CODE:
        nm, c = _BY_CODE[code]
        return {"name": nm, "code": code, "ambiguous": False}
    cands = _candidates(cls)
    # 2) exact squash
    sq = squash(raw)
    if sq:
        hits = [(cd, nm) for cd, nm in cands if squash(nm) == sq]
        if hits:
            codes = {h[0] for h in hits}
            return {"name": hits[0][1], "code": (hits[0][0] if len(codes) == 1 else None),
                    "ambiguous": len(codes) > 1}
    # 3) alias-expand karke exact
    ex = squash(_expand_words(raw))
    if ex and ex != sq:
        hits = [(cd, nm) for cd, nm in cands if squash(nm) == ex]
        if hits:
            codes = {h[0] for h in hits}
            return {"name": hits[0][1], "code": (hits[0][0] if len(codes) == 1 else None),
                    "ambiguous": len(codes) > 1}
    # 3.5) trailing bracket hatao (koi bhi content: "(Practical)", "(Recorded)")
    t2 = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", raw).strip()
    if t2 and squash(t2) != sq:
        hits = [(cd, nm) for cd, nm in cands if squash(nm) == squash(t2)]
        if hits:
            codes = {h[0] for h in hits}
            return {"name": hits[0][1], "code": (hits[0][0] if len(codes) == 1 else None),
                    "ambiguous": len(codes) > 1}
    # 4) unique prefix: "SCIENCE" -> "Science and Technology" (us class me sirf ek)
    base = ex or sq
    if len(base) >= 4:
        pref = [(cd, nm) for cd, nm in cands if squash(nm).startswith(base)]
        if len({squash(nm) for _, nm in pref}) == 1 and pref:
            codes = {h[0] for h in pref}
            return {"name": pref[0][1], "code": (pref[0][0] if len(codes) == 1 else None),
                    "ambiguous": len(codes) > 1}
    return None

def canon_display(name, cls=None):
    """Display ke liye official naam. Multi-code same-name (336/632) me explicit
    code ho to 'Name (code)' — warna plain official naam. Unknown -> cleaned original."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    r = canon_subject(raw, cls)
    if not r:
        # cleaned original: bracket-code hatao, spaces theek karo
        t = re.sub(r"\s*[\(\[][^\)\]]*\d+[^\)\]]*[\)\]]\s*$", "", raw)
        t = re.sub(r"\s*[-–—_/]\s*\d{2,}\s*$", "", t)
        return re.sub(r"\s+", " ", t).strip()
    code = _explicit_code(raw)
    cls_d = _cls_digits(cls)
    if code:
        same_name = {c for c, nm in _candidates(cls_d) if squash(nm) == squash(r["name"])}
        if len(same_name) > 1:
            return "%s (%s)" % (r["name"], code)
    return r["name"]

def canon_key(name, cls=None):
    """Identity key matching ke liye: code known -> 'c336'; naam ambiguous ->
    'n12:dataentryoperations'; unknown -> 'u12:rawsquash'."""
    cls_d = _cls_digits(cls)
    r = canon_subject(name, cls_d)
    if r:
        if r.get("code"):
            return "c" + r["code"]
        return "n%s:%s" % (cls_d or "", squash(r["name"]))
    sq = squash(raw_clean(name))
    return "u%s:%s" % (cls_d or "", sq)

def raw_clean(name):
    t = re.sub(r"\s*[\(\[][^\)\]]*\d+[^\)\]]*[\)\]]\s*$", "", str(name or ""))
    t = re.sub(r"\s*[-–—_/]\s*\d{2,}\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()

def canon_norm(name, cls=None):
    """_subj_norm replacement: canonical official naam ka squash (scope matching)."""
    r = canon_subject(name, cls)
    return squash(r["name"]) if r else squash(raw_clean(name))

def canon_list(names, cls=None):
    """List of raw subjects -> canonical display names (dupes merge, order stable)."""
    out, seen = [], set()
    for n in names or []:
        d = canon_display(n, cls)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out
