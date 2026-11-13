"""MVS Services — shared helpers: practical-subject detection, service visibility
(class/session/batch/practical targeting), date-range filters, and Excel export."""
import io
import json as _json
from datetime import datetime, timedelta


# ----------------------------------------------------------------- practical subjects
def practical_subjects_for(class_level, subject_names):
    """Student ke subjects me se practical-wale subjects + unke practical marks nikalo.
    syllabus_data se has_practical/practical_max read karta hai."""
    try:
        import syllabus_data as SD
    except Exception:
        return []
    cl = str(class_level or "").strip()
    cands = list(SD.SUBJECTS.get(cl, []))
    if not cands:  # class unknown -> dono classes dekho
        cands = list(SD.SUBJECTS.get("10", [])) + list(SD.SUBJECTS.get("12", []))
    by_name = {}
    for s in cands:
        by_name[str(s.get("name", "")).strip().lower()] = s
    out = []
    seen = set()
    for nm in (subject_names or []):
        key = str(nm).strip().lower()
        s = by_name.get(key)
        if not s:
            continue
        marks = s.get("marks", {}) or {}
        if marks.get("has_practical") and key not in seen:
            seen.add(key)
            out.append({"name": s.get("name", nm), "code": s.get("code", ""),
                        "practical_max": int(marks.get("practical_max", 0) or 0)})
    return out


def student_has_practical(profile):
    subs = getattr(profile, "subjects", None) or []
    if not isinstance(subs, list):
        subs = []
    return len(practical_subjects_for(getattr(profile, "class_level", ""), subs)) > 0


# ----------------------------------------------------------------- batch names
def _student_batch_names(profile, db):
    names = set()
    bn = getattr(profile, "batch_name", None)
    if bn:
        names.add(str(bn))
    try:
        bv = getattr(profile, "batch", None)
        if bv is not None:
            names.add(str(getattr(bv, "value", bv)))
    except Exception:
        pass
    ids = []
    for a in ("batch_id", "active_batch_id"):
        v = getattr(profile, a, None)
        if v:
            ids.append(v)
    try:
        from models import StudentBatch
        for sb in db.query(StudentBatch).filter(StudentBatch.student_id == profile.id).all():
            if getattr(sb, "batch_id", None):
                ids.append(sb.batch_id)
    except Exception:
        pass
    if ids:
        try:
            from models import Batch
            for b in db.query(Batch).filter(Batch.id.in_(set(ids))).all():
                if b.name:
                    names.add(b.name)
        except Exception:
            pass
    return names


# ----------------------------------------------------------------- visibility
def service_visible_to(svc, profile, db):
    """Kya ye service is student ko dikhni chahiye? (active + class + session + batch + practical)."""
    if not getattr(svc, "active", False):
        return False
    cl = str(getattr(profile, "class_level", "") or "").strip()
    tcl = (svc.target_class or "").strip()
    if tcl:
        if not cl or tcl != cl:
            return False
    ts = (svc.target_session or "").strip().lower()
    if ts:
        ss = str(getattr(profile, "exam_session", "") or "").strip().lower()
        if ss != ts:
            return False
    try:
        tb = _json.loads(svc.target_batches or "[]")
    except Exception:
        tb = []
    if tb:
        names_l = set(n.strip().lower() for n in _student_batch_names(profile, db))
        if not any(str(b).strip().lower() in names_l for b in tb):
            return False
    if getattr(svc, "require_practical", False) or getattr(svc, "kind", "") == "practical":
        if not student_has_practical(profile):
            return False
    return True


# ----------------------------------------------------------------- date range (IST)
def _ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def _parse_day(s):
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except Exception:
            continue
    return None


def utc_bounds(rng, frm=None, to=None):
    """Return (start_utc, end_utc) for today|week|month|custom. DB timestamps UTC hain (func.now)."""
    ist = _ist_now()
    ist_today = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    off = timedelta(hours=5, minutes=30)
    rng = (rng or "").strip().lower()
    if rng == "today":
        return (ist_today - off, None)
    if rng in ("week", "weekly"):
        return (ist_today - timedelta(days=6) - off, None)
    if rng in ("month", "monthly"):
        return (ist_today - timedelta(days=29) - off, None)
    if rng == "custom":
        f = _parse_day(frm)
        t = _parse_day(to)
        start = (f - off) if f else None
        end = ((t + timedelta(days=1)) - off) if t else None
        return (start, end)
    return (None, None)  # all-time


def interaction_rows(db, service_id=None, response=None, start=None, end=None):
    """Interaction rows (filter: service, response yes/no, date range)."""
    from models import MVSServiceInteraction as MI
    q = db.query(MI)
    if service_id:
        q = q.filter(MI.service_id == service_id)
    if response == "yes":
        q = q.filter(MI.response == "yes")
    elif response == "no":
        q = q.filter(MI.response == "no")
    elif response == "clicked":
        q = q.filter(MI.clicked_at.isnot(None))
    # date filter uses responded_at for yes/no, clicked_at for clicks
    tcol = MI.responded_at if response in ("yes", "no") else MI.clicked_at
    if start:
        q = q.filter(tcol >= start)
    if end:
        q = q.filter(tcol < end)
    q = q.order_by(MI.responded_at.desc().nullslast() if hasattr(MI.responded_at, "desc") else MI.id.desc())
    try:
        return q.all()
    except Exception:
        return q.order_by(MI.id.desc()).all()


def export_interactions_xlsx(db, rows, title="MVS Service"):
    """Interactions ko premium Excel me (name, phone, class, response, amount, screenshot?, time)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Services"
    heads = ["#", "Name", "Phone", "Class", "Response", "Amount", "Screenshot?", "Time"]
    fill = PatternFill("solid", fgColor="0F1B33")
    for c, h in enumerate(heads, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    for i, w in enumerate([6, 26, 16, 12, 12, 12, 13, 22], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    for i, r in enumerate(rows, 1):
        when = r.responded_at or r.clicked_at
        ws.append([i, r.student_name or "", r.student_phone or "", r.student_class or "",
                   (r.response or "clicked").upper(),
                   (r.amount if r.amount else ""),
                   ("YES" if r.screenshot else ""),
                   (when.strftime("%d %b %Y, %I:%M %p") if when else "")])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()
