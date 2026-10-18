"""
Project-based editor compensation — rate engine (Phase 3).

ISOLATED finance service. Money is stored/handled as INTEGER MINOR UNITS (paise for INR);
no binary float ever touches a persisted total. This module only does rate-plan management
and rate MATCHING (pure logic). It does NOT touch the existing operational/submission flow —
that integration is a later phase. SALARIED editors never reach this module.

Owner's 8-row source rate card is imported as a DRAFT template (editable), NEVER auto-published,
and NEVER auto-"nearest slab". Ambiguous/uncovered durations return explicit AMBIGUOUS / PENDING,
so Admin resolves boundaries before publishing (spec §5/§6).
"""

# Pricing strategies (spec §6)
STRAT_BAND = "FIXED_BY_DURATION_BAND"      # flat fee for a duration band + type
STRAT_PER_DELIVERABLE = "FIXED_PER_DELIVERABLE"   # flat fee per deliverable (urgent/custom scope)
STRAT_PER_MINUTE = "PER_FINISHED_MINUTE"   # optional: amount_minor is per finished minute
STRAT_CUSTOM = "ADMIN_CUSTOM_QUOTE"        # one-off admin quote
PRICING_STRATEGIES = (STRAT_BAND, STRAT_PER_DELIVERABLE, STRAT_PER_MINUTE, STRAT_CUSTOM)

DEFAULT_CURRENCY = "INR"


def rupees_to_minor(rupees):
    """6000 (rupees) -> 600000 (paise). Accepts int/str; rejects junk."""
    try:
        v = float(str(rupees).replace(",", "").strip())
    except Exception:
        raise ValueError("Invalid amount")
    if v < 0:
        raise ValueError("Amount cannot be negative")
    return int(round(v * 100))


def minor_to_display(minor, currency=DEFAULT_CURRENCY):
    """600000 paise -> '₹6,000.00' style. Pure display; never used as a stored value."""
    minor = int(minor or 0)
    whole, freq = divmod(abs(minor), 100)
    # Indian grouping
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        import re as _re
        head = _re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    sym = "₹" if currency == "INR" else (currency + " ")
    sign = "-" if minor < 0 else ""
    return "%s%s%s.%02d" % (sign, sym, s, freq)


def parse_duration_to_seconds(text):
    """Accept 'mm:ss' or 'hh:mm:ss' (or a plain integer of seconds). Returns a positive int.
    Rejects negative, NaN, Infinity, booleans, decimals and junk (spec §7)."""
    if isinstance(text, bool):
        raise ValueError("Invalid duration")
    if isinstance(text, int):
        if text < 0:
            raise ValueError("Duration cannot be negative")
        return int(text)
    s = str(text or "").strip()
    if not s:
        raise ValueError("Duration is required")
    if ":" not in s:
        if not s.isdigit():
            raise ValueError("Invalid duration")
        return int(s)
    parts = s.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError("Invalid duration — use mm:ss or hh:mm:ss")
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        mm, ss = nums
        hh = 0
    else:
        hh, mm, ss = nums
    if mm >= 60 or ss >= 60:
        raise ValueError("Minutes/seconds must be below 60")
    total = hh * 3600 + mm * 60 + ss
    if total < 0:
        raise ValueError("Duration cannot be negative")
    return total


# ---- Owner's source rate card (image) imported EXACTLY as a DRAFT. Amounts in paise. ----
# Duration bounds are the plain reading of the labels; the 15:00 and 20:00 OVERLAPS are left
# intentionally so the preview flags them and Admin resolves boundaries before publishing.
def _band(lo, hi):
    return (lo, hi)


SOURCE_RATE_DRAFT = [
    # label, video_type, editing_type, min_s, max_s, rupees, days, urgent
    ("R1", "Update", "Full Editing",                 10 * 60, 15 * 60, 6000,  "4 Days",  False),
    ("R2", "Update", "Full Editing",                 20 * 60, 20 * 60, 9000,  "6 Day",   False),
    ("R3", "Teaser", "Full Editing",                  2 * 60,  2 * 60, 4500,  "3-4 Day", False),
    ("R4", "Update", "Normal Editing with Intro.",   10 * 60, 15 * 60, 4500,  "3 Day",   False),
    ("R5", "Update", "Full Editing",                  5 * 60,  7 * 60, 3000,  "2 Day",   False),
    ("R6", "Interview", "Full Editing",              30 * 60, 30 * 60, 12000, "7-8 Day", False),
    ("R7", "Interview", "Full Editing",              10 * 60, 15 * 60, 6000,  "4 Day",   False),
    ("R8", "Update", "Full Editing",                 15 * 60, 20 * 60, 7500,  "5 Day",   False),
]
SOURCE_FOOTER = "With Working Day, Extra Hours, Sunday"


def draft_rule_dicts():
    """The 8 source rows as rule dicts ready to insert (amount in paise)."""
    out = []
    for i, (label, vt, et, lo, hi, rupees, days, urgent) in enumerate(SOURCE_RATE_DRAFT):
        out.append({
            "order_index": i,
            "label": label,
            "video_type": vt,
            "editing_type": et,
            "duration_min_seconds": lo,
            "duration_max_seconds": hi,
            "pricing_strategy": STRAT_BAND,
            "amount_minor": rupees_to_minor(rupees),
            "is_urgent": bool(urgent),
            "days_required": days,
            "active": True,
        })
    return out


def _norm(s):
    return (str(s or "")).strip().lower()


def match_rate(rules, duration_seconds, video_type, editing_type, urgent=False):
    """Pure matching. rules = list of objects/dicts with the rate-rule fields.
    Returns: {status, amount_minor, rule_id, rule_label, candidates:[labels], reason}.
      status: 'matched' | 'ambiguous' | 'pending'
    NO nearest-slab, NO silent rounding, NO highest-price pick (spec §5)."""
    dur = int(duration_seconds or 0)
    vt, et = _norm(video_type), _norm(editing_type)
    want_urgent = bool(urgent)

    def g(r, k):
        return r.get(k) if isinstance(r, dict) else getattr(r, k, None)

    cands = []
    for r in (rules or []):
        if not g(r, "active"):
            continue
        if bool(g(r, "is_urgent")) != want_urgent:
            continue
        if _norm(g(r, "video_type")) != vt:
            continue
        if _norm(g(r, "editing_type")) != et:
            continue
        strat = g(r, "pricing_strategy") or STRAT_BAND
        if strat in (STRAT_BAND, STRAT_PER_MINUTE):
            lo = g(r, "duration_min_seconds")
            hi = g(r, "duration_max_seconds")
            if lo is not None and dur < int(lo):
                continue
            if hi is not None and dur > int(hi):
                continue
        # FIXED_PER_DELIVERABLE / ADMIN_CUSTOM_QUOTE: not matched by duration here
        elif strat in (STRAT_PER_DELIVERABLE, STRAT_CUSTOM):
            continue
        cands.append(r)

    if not cands:
        return {"status": "pending", "amount_minor": None, "rule_id": None,
                "rule_label": None, "candidates": [],
                "reason": "No rule matches this duration/type — Admin must add or confirm a rule (RATE_PENDING)."}
    if len(cands) > 1:
        return {"status": "ambiguous", "amount_minor": None, "rule_id": None,
                "rule_label": None,
                "candidates": [g(c, "label") or ("#%s" % g(c, "id")) for c in cands],
                "reason": "More than one rule matches — Admin must resolve the boundary/priority before this can price."}
    r = cands[0]
    strat = g(r, "pricing_strategy") or STRAT_BAND
    if strat == STRAT_PER_MINUTE:
        minutes = (dur + 59) // 60   # explicit ceil-to-minute rule (documented)
        amount = int(g(r, "amount_minor") or 0) * minutes
    else:
        amount = int(g(r, "amount_minor") or 0)
    return {"status": "matched", "amount_minor": amount,
            "rule_id": g(r, "id"), "rule_label": g(r, "label"),
            "candidates": [g(r, "label")], "reason": "Matched"}


def detect_overlaps(rules):
    """Find pairs of ACTIVE band rules (same type+editing+urgency) whose duration ranges overlap.
    Returned to Admin as warnings — publishing is allowed only after Admin acknowledges/resolves."""
    def g(r, k):
        return r.get(k) if isinstance(r, dict) else getattr(r, k, None)
    band = [r for r in (rules or []) if g(r, "active") and (g(r, "pricing_strategy") or STRAT_BAND) in (STRAT_BAND, STRAT_PER_MINUTE)]
    warns = []
    for i in range(len(band)):
        for j in range(i + 1, len(band)):
            a, b = band[i], band[j]
            if _norm(g(a, "video_type")) != _norm(g(b, "video_type")):
                continue
            if _norm(g(a, "editing_type")) != _norm(g(b, "editing_type")):
                continue
            if bool(g(a, "is_urgent")) != bool(g(b, "is_urgent")):
                continue
            alo, ahi = int(g(a, "duration_min_seconds") or 0), int(g(a, "duration_max_seconds") or 0)
            blo, bhi = int(g(b, "duration_min_seconds") or 0), int(g(b, "duration_max_seconds") or 0)
            if alo <= bhi and blo <= ahi:   # inclusive overlap
                warns.append({"a": g(a, "label"), "b": g(b, "label"),
                              "overlap_from": max(alo, blo), "overlap_to": min(ahi, bhi)})
    return warns


# ===== Phase 4: submission billing — engagement, submission, estimate =====
def active_agreement_with_rules(db, staff_id):
    from models import EditorCompensationAgreement as _AG, EditorRateRule as _RR
    ag = (db.query(_AG).filter(_AG.staff_id == int(staff_id), _AG.state == "active")
          .order_by(_AG.version.desc()).first())
    if not ag:
        return None, []
    rules = db.query(_RR).filter(_RR.agreement_id == ag.id).all()
    return ag, rules


def ensure_engagement(db, sp, source_kind, source_id, is_urgent=False, deadline=None,
                      project_title="", source_title=""):
    """Idempotent: one engagement per (staff, source). Locks the ACTIVE agreement version once."""
    from models import EditorEngagement as _EE
    eng = (db.query(_EE).filter(_EE.staff_id == sp.id, _EE.source_kind == source_kind,
                                _EE.source_id == int(source_id)).first())
    ag, _ = active_agreement_with_rules(db, sp.id)
    if not eng:
        eng = _EE(staff_id=sp.id, source_kind=source_kind, source_id=int(source_id),
                  agreement_id=(ag.id if ag else None),
                  agreement_version=(ag.version if ag else None),
                  is_urgent=bool(is_urgent), deadline_at=deadline,
                  project_title=(project_title or "")[:200], source_title=(source_title or "")[:200])
        db.add(eng); db.flush()
    else:
        if eng.agreement_id is None and ag:   # lock at first available active agreement
            eng.agreement_id = ag.id
            eng.agreement_version = ag.version
        eng.is_urgent = bool(is_urgent)
        if deadline:
            eng.deadline_at = deadline
    return eng


def on_editor_submit(db, sp, source_kind, source_id, edited_link, billing,
                     is_urgent=False, deadline=None, project_title="", source_title=""):
    """Record a PROJECT_BASED editor's submission billing + (re)compute the private ESTIMATE.
    urgency is SERVER-owned (from task/chapter), never the editor's payload. One earning per
    engagement — resubmission updates it, never a second full fee. Returns the estimate dict."""
    from models import (EditorWorkSubmission as _WS, EditorEarning as _ER,
                        EditorCompensationAgreement as _AG, EditorRateRule as _RR)
    eng = ensure_engagement(db, sp, source_kind, source_id, is_urgent, deadline, project_title, source_title)
    billing = billing or {}
    dur = None
    try:
        if billing.get("duration") not in (None, ""):
            dur = parse_duration_to_seconds(billing.get("duration"))
    except Exception:
        dur = None
    vt = (str(billing.get("video_type") or "")).strip()
    et = (str(billing.get("editing_type") or "")).strip()
    rev = db.query(_WS).filter(_WS.engagement_id == eng.id).count() + 1
    db.add(_WS(engagement_id=eng.id, revision=rev, edited_link=(edited_link or "")[:1000],
               final_output_seconds=dur, video_type=vt[:60], editing_type=et[:80], is_urgent=bool(is_urgent)))
    ag, rules = None, []
    if eng.agreement_id:
        ag = db.query(_AG).filter(_AG.id == eng.agreement_id).first()
        rules = db.query(_RR).filter(_RR.agreement_id == eng.agreement_id).all()
    if dur is None or not vt or not et:
        res = {"status": "pending", "amount_minor": None, "rule_id": None, "rule_label": None,
               "candidates": [], "reason": "Billing details incomplete — fill duration, video type and editing type."}
    elif not rules:
        res = {"status": "pending", "amount_minor": None, "rule_id": None, "rule_label": None,
               "candidates": [], "reason": "No active rate plan yet — Admin confirmation pending."}
    else:
        res = match_rate(rules, dur, vt, et, bool(is_urgent))
    er = db.query(_ER).filter(_ER.engagement_id == eng.id).first()
    if not er:
        er = _ER(engagement_id=eng.id, staff_id=sp.id, currency=(ag.currency if ag else DEFAULT_CURRENCY))
        db.add(er)
    er.amount_minor = res.get("amount_minor")
    er.match_status = res.get("status")
    er.matched_rule_id = res.get("rule_id")
    er.matched_rule_label = (res.get("rule_label") or "")[:40]
    er.final_output_seconds = dur
    er.video_type = vt[:60]
    er.editing_type = et[:80]
    er.financial_state = "estimate"   # Phase 5 moves this to pending_verification/approved
    er.note = (res.get("reason") or "")[:300]
    db.flush()
    cur = (ag.currency if ag else DEFAULT_CURRENCY)
    return {"status": res.get("status"), "amount_minor": res.get("amount_minor"),
            "amount_display": (minor_to_display(res.get("amount_minor"), cur) if res.get("amount_minor") is not None else None),
            "rule_label": res.get("rule_label"), "candidates": res.get("candidates") or [],
            "reason": res.get("reason"), "is_urgent": bool(is_urgent), "currency": cur}


def editor_earnings_list(db, staff_id):
    """This editor's OWN engagements + estimates (private). Newest first."""
    from models import EditorEngagement as _EE, EditorEarning as _ER
    engs = (db.query(_EE).filter(_EE.staff_id == int(staff_id))
            .order_by(_EE.created_at.desc()).all())
    ers = {e.engagement_id: e for e in db.query(_ER).filter(_ER.staff_id == int(staff_id)).all()}
    out = []
    for eng in engs:
        er = ers.get(eng.id)
        cur = (er.currency if er else DEFAULT_CURRENCY)
        out.append({
            "engagement_id": eng.id,
            "source_kind": eng.source_kind,
            "project_title": eng.project_title or "",
            "source_title": eng.source_title or "",
            "is_urgent": bool(eng.is_urgent),
            "match_status": (er.match_status if er else "pending"),
            "financial_state": (er.financial_state if er else "estimate"),
            "amount_minor": (er.amount_minor if er else None),
            "amount_display": (minor_to_display(er.amount_minor, cur) if (er and er.amount_minor is not None) else None),
            "video_type": (er.video_type if er else ""),
            "editing_type": (er.editing_type if er else ""),
            "final_output_seconds": (er.final_output_seconds if er else None),
            "rule_label": (er.matched_rule_label if er else ""),
            "note": (er.note if er else ""),
            "created_at": eng.created_at.isoformat() if eng.created_at else None,
        })
    return out


# ===== Phase 5: QC technical acceptance + Admin financial approval =====
def _recompute_eligible(er):
    """Payable (eligible) only when BOTH technical QC ok AND Admin-approved AND an amount exists."""
    from datetime import datetime as _dt
    if (er.technical_ok and er.financial_state == "approved"
            and er.amount_minor is not None and er.eligible_at is None):
        er.eligible_at = _dt.utcnow()


def mark_technical_ok(db, source_kind, source_id):
    """QC approve -> earning.technical_ok=True (estimate -> pending_verification). Idempotent;
    only touches an EXISTING project-based earning. Returns the earning or None."""
    from models import EditorEngagement as _EE, EditorEarning as _ER
    eng = (db.query(_EE).filter(_EE.source_kind == source_kind,
                                _EE.source_id == int(source_id)).first())
    if not eng:
        return None
    er = db.query(_ER).filter(_ER.engagement_id == eng.id).first()
    if not er:
        return None
    er.technical_ok = True
    if er.financial_state == "estimate":
        er.financial_state = "pending_verification"
    _recompute_eligible(er)
    db.flush()
    return er


def admin_decide_earning(db, earning_id, decision, amount_minor=None, note="", actor_id=None):
    """Admin financial decision: approve | hold | reject. amount_minor set karo (ambiguous/custom
    resolve). Payable tabhi jab technical_ok AND approved AND amount present."""
    from models import EditorEarning as _ER
    er = db.query(_ER).filter(_ER.id == int(earning_id)).first()
    if not er:
        return None
    decision = (str(decision or "")).strip().lower()
    if amount_minor is not None:
        amt = int(amount_minor)
        if amt < 0:
            raise ValueError("Amount cannot be negative")
        er.amount_minor = amt
        er.match_status = "matched"   # Admin ne resolve kar diya
    if decision == "approve":
        if er.amount_minor is None:
            raise ValueError("Set an amount before approving (rate is pending/ambiguous).")
        er.financial_state = "approved"
    elif decision == "hold":
        er.financial_state = "on_hold"
        er.eligible_at = None
    elif decision == "reject":
        er.financial_state = "rejected"
        er.eligible_at = None
    else:
        raise ValueError("Invalid decision")
    if note:
        er.note = (str(note))[:300]
    _recompute_eligible(er)
    db.flush()
    return er


def _earning_row(db, er, eng=None, name=None):
    cur = er.currency or DEFAULT_CURRENCY
    return {"id": er.id, "staff_id": er.staff_id, "editor_name": name or "",
            "engagement_id": er.engagement_id,
            "source_kind": (eng.source_kind if eng else ""),
            "source_id": (eng.source_id if eng else None),
            "project_title": (eng.project_title if eng else ""),
            "source_title": (eng.source_title if eng else ""),
            "is_urgent": bool(eng.is_urgent) if eng else False,
            "match_status": er.match_status, "financial_state": er.financial_state,
            "technical_ok": bool(er.technical_ok),
            "amount_minor": er.amount_minor,
            "amount_display": (minor_to_display(er.amount_minor, cur) if er.amount_minor is not None else None),
            "video_type": er.video_type or "", "editing_type": er.editing_type or "",
            "final_output_seconds": er.final_output_seconds,
            "rule_label": er.matched_rule_label or "",
            "eligible_at": er.eligible_at.isoformat() if er.eligible_at else None,
            "note": er.note or "",
            "updated_at": er.updated_at.isoformat() if er.updated_at else None}


def admin_earnings_queue(db, status=None, staff_id=None, limit=300):
    """Admin finance review list across project editors (newest updated first)."""
    from models import EditorEarning as _ER, EditorEngagement as _EE, ProductionStaffProfile as _SP, User as _U
    q = db.query(_ER)
    if staff_id:
        q = q.filter(_ER.staff_id == int(staff_id))
    if status:
        q = q.filter(_ER.financial_state == status)
    ers = q.order_by(_ER.updated_at.desc()).limit(int(limit)).all()
    eng_ids = list({e.engagement_id for e in ers})
    engs = {e.id: e for e in db.query(_EE).filter(_EE.id.in_(eng_ids)).all()} if eng_ids else {}
    staff_ids = list({e.staff_id for e in ers})
    sps = {s.id: s for s in db.query(_SP).filter(_SP.id.in_(staff_ids)).all()} if staff_ids else {}
    uids = [s.user_id for s in sps.values() if s.user_id]
    users = {u.id: u for u in db.query(_U).filter(_U.id.in_(uids)).all()} if uids else {}
    out = []
    for er in ers:
        sp = sps.get(er.staff_id)
        nm = (users.get(sp.user_id).name if (sp and sp.user_id and users.get(sp.user_id)) else "")
        out.append(_earning_row(db, er, engs.get(er.engagement_id), nm))
    return out


# ===== Phase 5b: Monthly accounting + payout statement (spec §14/§15) =====
# Money stays in paise. A finalized statement is a FROZEN snapshot and is never repriced.
# eligible_at (a stable recorded event) decides the accounting month, in IST.

_IST_OFFSET_MIN = 330  # +05:30


def _period_bounds_utc(period):
    """period 'YYYY-MM' -> (start_utc, end_utc) half-open, for eligible_at (stored UTC) that
    falls in that IST calendar month."""
    from datetime import datetime as _dt, timedelta as _td
    y, m = int(period[:4]), int(period[5:7])
    ist = _td(minutes=_IST_OFFSET_MIN)
    start_ist = _dt(y, m, 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end_ist = _dt(ny, nm, 1)
    return (start_ist - ist, end_ist - ist)


def current_ist_period():
    from datetime import datetime as _dt, timedelta as _td
    now_ist = _dt.utcnow() + _td(minutes=_IST_OFFSET_MIN)
    return "%04d-%02d" % (now_ist.year, now_ist.month)


def _valid_period(period):
    import re as _re
    return bool(_re.match(r"^\d{4}-\d{2}$", str(period or "")) and 1 <= int(str(period)[5:7]) <= 12)


def _approved_eligible_earnings(db, staff_id, period):
    """Approved earnings whose eligible_at falls in the IST month. Ordered oldest-first.
    Returns list of (earning, engagement)."""
    from models import EditorEarning as _ER, EditorEngagement as _EE
    s_utc, e_utc = _period_bounds_utc(period)
    ers = (db.query(_ER)
           .filter(_ER.staff_id == int(staff_id),
                   _ER.financial_state == "approved",
                   _ER.eligible_at.isnot(None),
                   _ER.eligible_at >= s_utc, _ER.eligible_at < e_utc)
           .order_by(_ER.eligible_at.asc()).all())
    eng_ids = list({er.engagement_id for er in ers})
    engs = {e.id: e for e in db.query(_EE).filter(_EE.id.in_(eng_ids)).all()} if eng_ids else {}
    return [(er, engs.get(er.engagement_id)) for er in ers]


def _nonpayable_items(db, staff_id, period):
    """Submitted-but-not-yet-payable items for the same month (by submission/created month, IST),
    shown SEPARATELY from the statement total (spec §14: never silently inside the total)."""
    from models import EditorEarning as _ER, EditorEngagement as _EE
    s_utc, e_utc = _period_bounds_utc(period)
    ers = (db.query(_ER)
           .filter(_ER.staff_id == int(staff_id),
                   _ER.financial_state != "approved",
                   _ER.financial_state != "rejected",
                   _ER.created_at >= s_utc, _ER.created_at < e_utc)
           .order_by(_ER.created_at.asc()).all())
    eng_ids = list({er.engagement_id for er in ers})
    engs = {e.id: e for e in db.query(_EE).filter(_EE.id.in_(eng_ids)).all()} if eng_ids else {}
    out = []
    for er in ers:
        eng = engs.get(er.engagement_id)
        out.append({"earning_id": er.id, "title": (eng.source_title if eng else "") or (eng.project_title if eng else ""),
                    "financial_state": er.financial_state, "match_status": er.match_status,
                    "amount_display": (minor_to_display(er.amount_minor, er.currency or DEFAULT_CURRENCY) if er.amount_minor is not None else None)})
    return out


def _line_from_earning(er, eng):
    """Build the snapshot dict for one approved earning (used both for live draft + frozen line)."""
    return {"earning_id": er.id, "engagement_id": er.engagement_id,
            "source_kind": (eng.source_kind if eng else ""), "source_id": (eng.source_id if eng else None),
            "project_title": (eng.project_title if eng else ""), "source_title": (eng.source_title if eng else ""),
            "video_type": er.video_type or "", "editing_type": er.editing_type or "",
            "final_output_seconds": er.final_output_seconds,
            "is_urgent": bool(eng.is_urgent) if eng else False,
            "rule_label": er.matched_rule_label or "", "amount_minor": int(er.amount_minor or 0),
            "eligible_at": er.eligible_at, "submitted_at": er.created_at, "note": er.note or ""}


def _staff_currency(db, staff_id):
    ag, _ = active_agreement_with_rules(db, staff_id)
    return (ag.currency if ag else DEFAULT_CURRENCY)


def get_or_create_draft(db, staff_id, period, currency=None):
    """Idempotent: one draft statement per (staff, period, currency). Recomputes draft totals from
    the current approved ledger. Never touches a finalized statement."""
    from models import EditorPayoutStatement as _ST
    if not _valid_period(period):
        raise ValueError("Invalid period (expected YYYY-MM)")
    cur = currency or _staff_currency(db, staff_id)
    st = (db.query(_ST).filter(_ST.staff_id == int(staff_id), _ST.period == period,
                               _ST.currency == cur).first())
    if not st:
        st = _ST(staff_id=int(staff_id), period=period, currency=cur, state="draft")
        db.add(st); db.flush()
    if st.state == "draft":
        _recompute_draft_totals(db, st)
    return st


def _recompute_draft_totals(db, st):
    """For a DRAFT only: recompute standard/urgent/total from current approved earnings."""
    rows = _approved_eligible_earnings(db, st.staff_id, st.period)
    std = sum(int(er.amount_minor or 0) for er, eng in rows if not (eng and eng.is_urgent))
    urg = sum(int(er.amount_minor or 0) for er, eng in rows if (eng and eng.is_urgent))
    st.standard_minor = std
    st.urgent_minor = urg
    st.total_minor = std + urg + int(st.adjustments_minor or 0)
    db.flush()


def set_statement_adjustment(db, statement_id, amount_minor, note=""):
    """Admin authorized adjustment (+/-) on a DRAFT statement only."""
    from models import EditorPayoutStatement as _ST
    st = db.query(_ST).filter(_ST.id == int(statement_id)).first()
    if not st:
        return None
    if st.state != "draft":
        raise ValueError("Adjustments can only be set on a draft statement")
    st.adjustments_minor = int(amount_minor or 0)
    st.adjustment_note = (str(note or ""))[:300]
    _recompute_draft_totals(db, st)
    return st


def finalize_statement(db, statement_id, actor_id=None):
    """Freeze the statement: snapshot approved lines, lock totals, assign statement_no + issue_date.
    Idempotent — a finalized statement is returned unchanged. Line amounts never change after this."""
    from datetime import datetime as _dt
    from models import EditorPayoutStatement as _ST, EditorStatementLine as _SL
    st = db.query(_ST).filter(_ST.id == int(statement_id)).first()
    if not st:
        return None
    if st.state != "draft":
        return st  # already finalized/paid — never re-freeze
    rows = _approved_eligible_earnings(db, st.staff_id, st.period)
    # clear any stale lines (defensive; a draft should have none) then snapshot
    db.query(_SL).filter(_SL.statement_id == st.id).delete()
    std = urg = 0
    for er, eng in rows:
        d = _line_from_earning(er, eng)
        db.add(_SL(statement_id=st.id, earning_id=d["earning_id"], engagement_id=d["engagement_id"],
                   source_kind=d["source_kind"], source_id=d["source_id"],
                   project_title=d["project_title"], source_title=d["source_title"],
                   video_type=d["video_type"], editing_type=d["editing_type"],
                   final_output_seconds=d["final_output_seconds"], is_urgent=d["is_urgent"],
                   rule_label=d["rule_label"], amount_minor=d["amount_minor"],
                   eligible_at=d["eligible_at"], submitted_at=d["submitted_at"], note=d["note"][:300]))
        if d["is_urgent"]:
            urg += d["amount_minor"]
        else:
            std += d["amount_minor"]
    st.standard_minor = std
    st.urgent_minor = urg
    st.total_minor = std + urg + int(st.adjustments_minor or 0)
    st.state = "finalized"
    st.finalized_at = _dt.utcnow()
    st.finalized_by = actor_id
    st.issue_date = _dt.utcnow()
    if not st.statement_no:
        st.statement_no = "MVS/PES/%s/E%d-%d" % (st.period, int(st.staff_id), st.id)
    db.flush()
    return st


def record_statement_payment(db, statement_id, amount_minor, method="", reference="", actor_id=None, paid_on=None, note=""):
    """Record a payment allocation against a FINALIZED statement. Recording != transferring money.
    Recomputes paid_minor + payment state. Never changes line amounts."""
    from datetime import datetime as _dt
    from models import EditorPayoutStatement as _ST, EditorStatementPayment as _PM
    st = db.query(_ST).filter(_ST.id == int(statement_id)).first()
    if not st:
        return None
    if st.state == "draft":
        raise ValueError("Finalize the statement before recording a payment")
    amt = int(amount_minor or 0)
    if amt <= 0:
        raise ValueError("Payment amount must be positive")
    pm = _PM(statement_id=st.id, amount_minor=amt, method=(str(method or ""))[:40],
             reference=(str(reference or ""))[:80], actor_id=actor_id,
             paid_on=(paid_on or _dt.utcnow()), note=(str(note or ""))[:300])
    db.add(pm)
    db.flush()
    _recompute_payment_state(db, st)
    return pm


def _recompute_payment_state(db, st):
    from models import EditorStatementPayment as _PM
    total_paid = sum(int(p.amount_minor or 0) for p in
                     db.query(_PM).filter(_PM.statement_id == st.id).all())
    st.paid_minor = total_paid
    if st.state in ("draft",):
        pass
    elif total_paid <= 0:
        st.state = "finalized"
    elif total_paid < int(st.total_minor or 0):
        st.state = "partially_paid"
    else:
        st.state = "paid"   # total_paid >= total (overpayment kept visible via outstanding<0)
    db.flush()


def confirm_statement_receipt(db, payment_id, staff_id):
    """Editor confirms they received a recorded payment (own statement only)."""
    from datetime import datetime as _dt
    from models import EditorStatementPayment as _PM, EditorPayoutStatement as _ST
    pm = db.query(_PM).filter(_PM.id == int(payment_id)).first()
    if not pm:
        return None
    st = db.query(_ST).filter(_ST.id == pm.statement_id).first()
    if not st or int(st.staff_id) != int(staff_id):
        return None  # ownership guard
    pm.receipt_confirmed = True
    pm.receipt_confirmed_at = _dt.utcnow()
    db.flush()
    return pm


def _payment_row(pm, mask=True):
    ref = pm.reference or ""
    if mask and len(ref) > 4:
        ref = ("••••" + ref[-4:])
    return {"id": pm.id, "amount_minor": pm.amount_minor,
            "amount_display": minor_to_display(pm.amount_minor),
            "method": pm.method or "", "reference": ref,
            "paid_on": pm.paid_on.isoformat() if pm.paid_on else None,
            "receipt_confirmed": bool(pm.receipt_confirmed),
            "receipt_confirmed_at": pm.receipt_confirmed_at.isoformat() if pm.receipt_confirmed_at else None,
            "note": pm.note or ""}


def statement_detail(db, statement_id, include_nonpayable=True):
    """Full statement: lines (frozen if finalized, live if draft) + payments + outstanding.
    Amounts included — caller must enforce admin/own-editor authorization."""
    from models import (EditorPayoutStatement as _ST, EditorStatementLine as _SL,
                        EditorStatementPayment as _PM, ProductionStaffProfile as _SP, User as _U)
    st = db.query(_ST).filter(_ST.id == int(statement_id)).first()
    if not st:
        return None
    cur = st.currency or DEFAULT_CURRENCY
    sp = db.query(_SP).filter(_SP.id == st.staff_id).first()
    nm = ""
    if sp and sp.user_id:
        u = db.query(_U).filter(_U.id == sp.user_id).first()
        nm = (u.name if u else "")
    is_draft = (st.state == "draft")
    lines = []
    if is_draft:
        for er, eng in _approved_eligible_earnings(db, st.staff_id, st.period):
            d = _line_from_earning(er, eng)
            lines.append(d)
    else:
        for sl in db.query(_SL).filter(_SL.statement_id == st.id).order_by(_SL.id.asc()).all():
            lines.append({"earning_id": sl.earning_id, "engagement_id": sl.engagement_id,
                          "source_kind": sl.source_kind, "source_id": sl.source_id,
                          "project_title": sl.project_title, "source_title": sl.source_title,
                          "video_type": sl.video_type, "editing_type": sl.editing_type,
                          "final_output_seconds": sl.final_output_seconds, "is_urgent": bool(sl.is_urgent),
                          "rule_label": sl.rule_label, "amount_minor": int(sl.amount_minor or 0),
                          "eligible_at": sl.eligible_at, "submitted_at": sl.submitted_at, "note": sl.note or ""})
    # live draft totals reflect the current ledger
    if is_draft:
        std = sum(l["amount_minor"] for l in lines if not l["is_urgent"])
        urg = sum(l["amount_minor"] for l in lines if l["is_urgent"])
        total = std + urg + int(st.adjustments_minor or 0)
    else:
        std, urg, total = int(st.standard_minor or 0), int(st.urgent_minor or 0), int(st.total_minor or 0)
    payments = [_payment_row(p) for p in
                db.query(_PM).filter(_PM.statement_id == st.id).order_by(_PM.id.asc()).all()]
    paid = sum(int(p["amount_minor"] or 0) for p in payments)
    outstanding = total - paid

    def _disp(mn):
        return minor_to_display(mn, cur)

    out_lines = []
    for l in lines:
        out_lines.append({**{k: l[k] for k in ("earning_id", "engagement_id", "source_kind", "source_id",
                                               "project_title", "source_title", "video_type", "editing_type",
                                               "final_output_seconds", "is_urgent", "rule_label", "note")},
                          "amount_minor": l["amount_minor"], "amount_display": _disp(l["amount_minor"]),
                          "eligible_at": l["eligible_at"].isoformat() if l["eligible_at"] else None,
                          "submitted_at": l["submitted_at"].isoformat() if l["submitted_at"] else None})
    res = {"id": st.id, "staff_id": st.staff_id, "editor_name": nm, "period": st.period,
           "currency": cur, "state": st.state, "is_draft": is_draft,
           "statement_no": st.statement_no or "", "line_count": len(out_lines),
           "lines": out_lines,
           "standard_minor": std, "standard_display": _disp(std),
           "urgent_minor": urg, "urgent_display": _disp(urg),
           "adjustments_minor": int(st.adjustments_minor or 0), "adjustments_display": _disp(int(st.adjustments_minor or 0)),
           "adjustment_note": st.adjustment_note or "",
           "total_minor": total, "total_display": _disp(total),
           "paid_minor": paid, "paid_display": _disp(paid),
           "outstanding_minor": outstanding, "outstanding_display": _disp(outstanding),
           "payments": payments,
           "issue_date": st.issue_date.isoformat() if st.issue_date else None,
           "finalized_at": st.finalized_at.isoformat() if st.finalized_at else None,
           "note": st.note or ""}
    if include_nonpayable:
        res["nonpayable"] = _nonpayable_items(db, st.staff_id, st.period)
    return res


def editor_statements_list(db, staff_id):
    """This editor's own statements (newest period first), summary rows."""
    from models import EditorPayoutStatement as _ST
    sts = (db.query(_ST).filter(_ST.staff_id == int(staff_id))
           .order_by(_ST.period.desc(), _ST.id.desc()).all())
    out = []
    for st in sts:
        cur = st.currency or DEFAULT_CURRENCY
        total = int(st.total_minor or 0)
        paid = int(st.paid_minor or 0)
        out.append({"id": st.id, "period": st.period, "currency": cur, "state": st.state,
                    "statement_no": st.statement_no or "",
                    "total_display": minor_to_display(total, cur),
                    "paid_display": minor_to_display(paid, cur),
                    "outstanding_display": minor_to_display(total - paid, cur),
                    "issue_date": st.issue_date.isoformat() if st.issue_date else None})
    return out


def admin_statements_overview(db, period):
    """For the Admin monthly screen: every project-based editor with any approved-eligible earning
    OR an existing statement in `period`, plus that editor's statement summary."""
    from models import (EditorPayoutStatement as _ST, EditorEarning as _ER,
                        ProductionStaffProfile as _SP, User as _U)
    import production_core as _pc
    if not _valid_period(period):
        raise ValueError("Invalid period")
    s_utc, e_utc = _period_bounds_utc(period)
    staff_ids = set()
    # editors with approved-eligible earnings this month
    for (sid,) in (db.query(_ER.staff_id)
                   .filter(_ER.financial_state == "approved", _ER.eligible_at.isnot(None),
                           _ER.eligible_at >= s_utc, _ER.eligible_at < e_utc).distinct().all()):
        staff_ids.add(sid)
    # editors with an existing statement this month
    existing = {st.staff_id: st for st in db.query(_ST).filter(_ST.period == period).all()}
    for sid in existing:
        staff_ids.add(sid)
    sps = {s.id: s for s in db.query(_SP).filter(_SP.id.in_(list(staff_ids))).all()} if staff_ids else {}
    uids = [s.user_id for s in sps.values() if s.user_id]
    users = {u.id: u for u in db.query(_U).filter(_U.id.in_(uids)).all()} if uids else {}
    out = []
    for sid in staff_ids:
        sp = sps.get(sid)
        if not sp or not is_project_based_editor_sp(sp):
            continue
        nm = (users.get(sp.user_id).name if (sp and sp.user_id and users.get(sp.user_id)) else "")
        rows = _approved_eligible_earnings(db, sid, period)
        appr_total = sum(int(er.amount_minor or 0) for er, eng in rows)
        cur = _staff_currency(db, sid)
        st = existing.get(sid)
        out.append({"staff_id": sid, "editor_name": nm, "currency": cur,
                    "approved_count": len(rows),
                    "approved_total_display": minor_to_display(appr_total, cur),
                    "statement_id": (st.id if st else None),
                    "state": (st.state if st else None),
                    "statement_no": (st.statement_no if st else ""),
                    "total_display": (minor_to_display(int(st.total_minor or 0), cur) if st else None),
                    "paid_display": (minor_to_display(int(st.paid_minor or 0), cur) if st else None),
                    "outstanding_display": (minor_to_display(int(st.total_minor or 0) - int(st.paid_minor or 0), cur) if st else None)})
    out.sort(key=lambda r: (r["state"] or "", r["editor_name"] or ""))
    return out


def is_project_based_editor_sp(sp):
    """Local guard so this module doesn't hard-depend on production_core at import time."""
    try:
        import production_core as _pc
        return _pc.is_project_based_editor(sp)
    except Exception:
        return (getattr(sp, "compensation_type", "SALARIED") or "SALARIED").strip().upper() == "PROJECT_BASED"


# ===== Phase 6: data-retention / deletion safety (spec §22, audit A12) =====
# Finance tables that must NEVER be purged by generic user-reference cleanup.
FINANCE_TABLES = (
    "editor_comp_agreements", "editor_rate_rules", "editor_engagements",
    "editor_work_submissions", "editor_earnings", "editor_payout_statements",
    "editor_statement_lines", "editor_statement_payments",
)


def has_financial_history(db, staff_id):
    """True if this editor profile has ANY compensation record (agreement/engagement/earning/
    statement/payment). Such identities must be archived, never destructively deleted."""
    from models import (EditorCompensationAgreement as _AG, EditorEngagement as _EE,
                        EditorEarning as _ER, EditorPayoutStatement as _ST)
    sid = int(staff_id)
    for M, col in ((_AG, _AG.staff_id), (_EE, _EE.staff_id), (_ER, _ER.staff_id), (_ST, _ST.staff_id)):
        if db.query(M).filter(col == sid).first():
            return True
    return False


def financial_history_summary(db, staff_id):
    """Counts for admin messaging on an archive-instead-of-delete decision."""
    from models import (EditorCompensationAgreement as _AG, EditorEngagement as _EE,
                        EditorEarning as _ER, EditorPayoutStatement as _ST)
    sid = int(staff_id)
    return {"agreements": db.query(_AG).filter(_AG.staff_id == sid).count(),
            "engagements": db.query(_EE).filter(_EE.staff_id == sid).count(),
            "earnings": db.query(_ER).filter(_ER.staff_id == sid).count(),
            "statements": db.query(_ST).filter(_ST.staff_id == sid).count()}
