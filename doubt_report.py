"""Admin 'control room' doubt report — per-teacher resolution performance over a
date range (today / week / month / custom), with on-time vs late + office-hours-adjusted
average. Premium PDF for sharing in the teachers' group. Teacher+admin data only."""
import io
import doubt_metrics as _dm


def report_data(db, start, end, label="", only_teachers=None):
    from models import Doubt, DoubtStatus, TeacherProfile, User
    sla_now = _dm.sla_hours(db)
    oh = _dm.office_cfg(db)
    q = db.query(Doubt.teacher_id, Doubt.subject, Doubt.created_at,
                 Doubt.resolved_at, Doubt.sla_snapshot)\
        .filter(Doubt.status == DoubtStatus.resolved, Doubt.resolved_at.isnot(None))
    if start:
        q = q.filter(Doubt.resolved_at >= start)
    if end:
        q = q.filter(Doubt.resolved_at < end)
    rows = q.all()
    tids = {r[0] for r in rows if r[0]}
    tmap = {}
    if tids:
        for tid, nm in db.query(TeacherProfile.id, User.name)\
                .join(User, User.id == TeacherProfile.user_id)\
                .filter(TeacherProfile.id.in_(tids)).all():
            tmap[tid] = nm or ""
    # optional: sirf chune gaye teachers (name set, case-insensitive)
    sel = None
    if only_teachers:
        sel = set(str(x).strip().lower() for x in only_teachers if str(x).strip())
        if sel:
            rows = [r for r in rows if (tmap.get(r[0], "Unassigned") or "Unassigned").strip().lower() in sel]
    per = {}
    persub = {}
    tot = {"resolved": 0, "on_time": 0, "late": 0, "office_sum": 0, "max_late": 0}

    def _canon_sub(s):
        s = (s or "").strip() or "General"
        try:
            import subject_registry as _SR
            return _SR.canon_display(s) or s
        except Exception:
            return s

    for tid, subj, cat, rat, snap in rows:
        m = _dm.metrics_for(cat, rat, snap, sla_now, oh)
        if not m:
            continue
        key = tid or 0
        t = per.setdefault(key, {"teacher": tmap.get(tid, "Unassigned") or "Unassigned",
                                 "resolved": 0, "on_time": 0, "late": 0,
                                 "office_sum": 0, "max_late": 0})
        sub = _canon_sub(subj)
        sg = persub.setdefault(sub, {"subject": sub, "resolved": 0, "on_time": 0,
                                     "late": 0, "office_sum": 0, "max_late": 0})
        for bucket in (t, sg):
            bucket["resolved"] += 1
            if m["late"]:
                bucket["late"] += 1
                bucket["max_late"] = max(bucket["max_late"], m["late_minutes"])
            else:
                bucket["on_time"] += 1
            bucket["office_sum"] += m["office_minutes"]
        tot["resolved"] += 1
        if m["late"]:
            tot["late"] += 1
            tot["max_late"] = max(tot["max_late"], m["late_minutes"])
        else:
            tot["on_time"] += 1
        tot["office_sum"] += m["office_minutes"]
    teachers = []
    for t in per.values():
        avg = round(t["office_sum"] / t["resolved"]) if t["resolved"] else 0
        teachers.append({
            "teacher": t["teacher"], "resolved": t["resolved"],
            "on_time": t["on_time"], "late": t["late"],
            "avg_minutes": avg, "avg_label": _dm.fmt_dur(avg),
            "max_late_label": (_dm.fmt_dur(t["max_late"]) if t["max_late"] else ""),
            "on_time_pct": (round(t["on_time"] * 100 / t["resolved"]) if t["resolved"] else 0),
        })
    teachers.sort(key=lambda x: (-x["resolved"], x["teacher"]))
    subjects = []
    for sg in persub.values():
        avg = round(sg["office_sum"] / sg["resolved"]) if sg["resolved"] else 0
        subjects.append({
            "subject": sg["subject"], "resolved": sg["resolved"],
            "on_time": sg["on_time"], "late": sg["late"],
            "avg_minutes": avg, "avg_label": _dm.fmt_dur(avg),
            "max_late_label": (_dm.fmt_dur(sg["max_late"]) if sg["max_late"] else ""),
            "on_time_pct": (round(sg["on_time"] * 100 / sg["resolved"]) if sg["resolved"] else 0),
        })
    subjects.sort(key=lambda x: (-x["late"], -x["resolved"], x["subject"]))
    avg_all = round(tot["office_sum"] / tot["resolved"]) if tot["resolved"] else 0
    return {
        "label": label,
        "teachers": teachers,
        "subjects": subjects,
        "totals": {
            "resolved": tot["resolved"], "on_time": tot["on_time"], "late": tot["late"],
            "avg_minutes": avg_all, "avg_label": _dm.fmt_dur(avg_all),
            "max_late_label": (_dm.fmt_dur(tot["max_late"]) if tot["max_late"] else ""),
            "on_time_pct": (round(tot["on_time"] * 100 / tot["resolved"]) if tot["resolved"] else 0),
        },
        "sla_hours": sla_now,
        "office_hours": {"enabled": oh["enabled"], "start": oh["start"], "end": oh["end"]},
    }


def report_pdf(db, data, period_label=""):
    """Premium A4 PDF — MVS branding, reuses scholarship palette."""
    from fpdf import FPDF
    import scholarship as SCH
    NAVY, GOLD, GOLD_D, INK = SCH._NAVY, SCH._GOLD, SCH._GOLD_D, SCH._INK
    L = SCH._latin
    MUT = (120, 120, 128)
    GREEN = (31, 122, 82)
    RED = (192, 57, 43)
    pdf = FPDF("P", "mm", "A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    W = 210

    # ---- header band ----
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, W, 34, "F")
    try:
        lp = SCH._logo_path()
        if lp:
            pdf.image(lp, 12, 7, 20, 20)
    except Exception:
        pass
    pdf.set_xy(36, 9)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 8, L("MVS Foundation"), ln=1)
    pdf.set_xy(36, 18)
    pdf.set_text_color(*GOLD)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, L("Doubt Resolution Report"), ln=1)
    pdf.set_xy(36, 25)
    pdf.set_text_color(210, 210, 215)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, L(period_label or data.get("label", "")), ln=1)

    oh = data.get("office_hours", {})
    pdf.set_y(40)
    pdf.set_text_color(*MUT)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.cell(0, 5, L("SLA: %sh  -  Office hours: %02d:00-%02d:00 %s  -  Average = office-hours time only"
                     % (data.get("sla_hours", 15), oh.get("start", 9), oh.get("end", 20),
                        "(on)" if oh.get("enabled", True) else "(off)")), ln=1)

    # ---- totals strip ----
    t = data.get("totals", {})
    pdf.ln(2)
    y0 = pdf.get_y()
    cards = [("Resolved", str(t.get("resolved", 0)), NAVY),
             ("On time", str(t.get("on_time", 0)), GREEN),
             ("Late", str(t.get("late", 0)), RED),
             ("On-time %", "%d%%" % t.get("on_time_pct", 0), GOLD_D),
             ("Avg time", t.get("avg_label", "-") or "-", NAVY)]
    cw = (W - 24) / len(cards)
    x = 12
    for lbl, val, col in cards:
        pdf.set_fill_color(248, 245, 236)
        pdf.set_draw_color(225, 220, 205)
        pdf.rect(x, y0, cw - 3, 20, "DF")
        pdf.set_xy(x, y0 + 3)
        pdf.set_text_color(*col)
        pdf.set_font("Helvetica", "B", 15)
        pdf.cell(cw - 3, 8, L(val), align="C")
        pdf.set_xy(x, y0 + 12)
        pdf.set_text_color(*MUT)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.cell(cw - 3, 5, L(lbl.upper()), align="C")
        x += cw
    pdf.set_y(y0 + 26)

    # ---- per-teacher table ----
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, L("Teacher-wise performance"), ln=1)

    heads = [("Teacher", 78), ("Resolved", 24), ("On time", 22), ("Late", 18),
             ("On-time %", 24), ("Avg time", 24)]

    def _row_head():
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8.5)
        for h, w in heads:
            pdf.cell(w, 8, L(h), border=0, align=("L" if h == "Teacher" else "C"), fill=True)
        pdf.ln(8)

    _row_head()
    pdf.set_font("Helvetica", "", 8.5)
    alt = False
    for tr in data.get("teachers", []):
        if pdf.get_y() > 270:
            pdf.add_page()
            _row_head()
            pdf.set_font("Helvetica", "", 8.5)
        pdf.set_fill_color(248, 245, 236) if alt else pdf.set_fill_color(255, 255, 255)
        alt = not alt
        pdf.set_text_color(*INK)
        pdf.cell(78, 7, L((tr["teacher"] or "")[:44]), border="B", fill=True)
        pdf.cell(24, 7, str(tr["resolved"]), border="B", align="C", fill=True)
        pdf.set_text_color(*GREEN)
        pdf.cell(22, 7, str(tr["on_time"]), border="B", align="C", fill=True)
        pdf.set_text_color(*(RED if tr["late"] else MUT))
        pdf.cell(18, 7, str(tr["late"]), border="B", align="C", fill=True)
        pdf.set_text_color(*INK)
        pdf.cell(24, 7, "%d%%" % tr["on_time_pct"], border="B", align="C", fill=True)
        pdf.cell(24, 7, L(tr["avg_label"] or "-"), border="B", align="C", fill=True)
        pdf.ln(7)

    # ---- subject-wise table ----
    subs = data.get("subjects", [])
    if subs:
        pdf.ln(8)
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.set_text_color(*INK)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, L("Subject-wise performance"), ln=1)
        sheads = [("Subject", 78), ("Resolved", 24), ("On time", 22), ("Late", 18),
                  ("On-time %", 24), ("Avg time", 24)]

        def _shead():
            pdf.set_fill_color(*NAVY)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 8.5)
            for h, w in sheads:
                pdf.cell(w, 8, L(h), border=0, align=("L" if h == "Subject" else "C"), fill=True)
            pdf.ln(8)

        _shead()
        pdf.set_font("Helvetica", "", 8.5)
        alt2 = False
        for sg in subs:
            if pdf.get_y() > 270:
                pdf.add_page()
                _shead()
                pdf.set_font("Helvetica", "", 8.5)
            pdf.set_fill_color(248, 245, 236) if alt2 else pdf.set_fill_color(255, 255, 255)
            alt2 = not alt2
            pdf.set_text_color(*INK)
            pdf.cell(78, 7, L((sg["subject"] or "")[:44]), border="B", fill=True)
            pdf.cell(24, 7, str(sg["resolved"]), border="B", align="C", fill=True)
            pdf.set_text_color(*GREEN)
            pdf.cell(22, 7, str(sg["on_time"]), border="B", align="C", fill=True)
            pdf.set_text_color(*(RED if sg["late"] else MUT))
            pdf.cell(18, 7, str(sg["late"]), border="B", align="C", fill=True)
            pdf.set_text_color(*INK)
            pdf.cell(24, 7, "%d%%" % sg["on_time_pct"], border="B", align="C", fill=True)
            pdf.cell(24, 7, L(sg["avg_label"] or "-"), border="B", align="C", fill=True)
            pdf.ln(7)

    pdf.ln(6)
    pdf.set_text_color(*MUT)
    pdf.set_font("Helvetica", "I", 7.5)
    pdf.cell(0, 4, L("Average response time counts only office-hours minutes, so after-hours "
                     "doubts do not inflate it. Late = resolved after the SLA that was in force "
                     "at resolution time. Internal report - not shown to students."), ln=1)

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)
