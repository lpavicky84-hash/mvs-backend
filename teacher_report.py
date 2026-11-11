"""Premium Teacher Progress Report — weekly/monthly, PDF for admin & teachers.

Har teacher ka progress ek jagah:
  • Active: kitne din active rahe, kitne ghante, kitne din absent (working days me)
  • Classes: kitni classes li, kitne ghante padhaya
  • Class reports: time par vs 1 ghante se zyada delay par submit
  • Doubts: solve kiye (period me), 15 ghante se zyada delay par solve kiye, sabse delayed doubt
  • Video tasks: assign hue (naam ke saath), shorts, upload ho gaye, pending, delayed

collect_all() -> per-teacher metrics list (JSON-safe, UI bhi reuse kar sakta hai).
build_report_pdf() -> premium PDF bytes (matplotlib comparison charts ke saath).

Ye module raw models se compute karta hai (perf_engine/payout se independent) taaki
jo metric user ne maanga wahi exact aaye aur test karna aasan ho.
"""
from datetime import datetime, timedelta, date
import io

# -- tunable thresholds (report me clearly likhe jaate hain) --
REPORT_DELAY_MIN = 60        # class report 60 min se zyada late -> "delayed"
DOUBT_DELAY_HOURS = 15       # doubt 15 ghante se zyada me solve -> "delayed"
CLASS_NOMINAL_MIN = 90       # ek class ~90 min (duration missing ho to fallback)


def _period_bounds(period, ref=None):
    """(start_date, today, now_dt, label, days_in_period) — IST."""
    from teacher_routes import _ist_now
    now = ref or _ist_now()
    today = now.date()
    if period == "week":
        start = today - timedelta(days=today.weekday())      # is hafte ka Monday
        label = "Weekly · %s to %s" % (start.strftime("%d %b"), today.strftime("%d %b %Y"))
    else:
        period = "month"
        start = today.replace(day=1)
        label = "Monthly · %s" % today.strftime("%B %Y")
    return start, today, now, label, period


def _working_days(start, end):
    """Mon-Sat ko working maano (Sunday chhodke) — active/absent ke liye."""
    n = 0
    d = start
    while d <= end:
        if d.weekday() != 6:   # 6 = Sunday
            n += 1
        d += timedelta(days=1)
    return n


def collect_teacher_metrics(db, tp_id, tp_name, period="month", ref=None, _cfg=None):
    """Ek teacher ka poora metric dict. _cfg aur ref bahar se de sakte ho (batch speed)."""
    from models import (TimetableEntry, Doubt, VideoTask, TeacherAttendance)
    from teacher_routes import (_duration_of, _delay_of, _att_hours, _is_present)
    start, today, now, label, period = _period_bounds(period, ref)
    dt0 = datetime.combine(start, datetime.min.time())

    out = {"teacher_id": tp_id, "name": tp_name or ("Teacher #%d" % tp_id),
           "period": period, "period_label": label}

    # ---------- ACTIVE (attendance) ----------
    att = (db.query(TeacherAttendance)
           .filter(TeacherAttendance.teacher_id == tp_id,
                   TeacherAttendance.att_date >= start,
                   TeacherAttendance.att_date <= today).all())
    active_days = sum(1 for a in att if _is_present(a))
    active_hours = round(sum((_att_hours(a) or 0) for a in att), 1)
    wd = _working_days(start, today)
    out["active_days"] = active_days
    out["active_hours"] = active_hours
    out["working_days"] = wd
    out["absent_days"] = max(0, wd - active_days)
    # absent dates list (working days jinme attendance nahi)
    present_dates = set(a.att_date for a in att if _is_present(a))
    absent_list = []
    d = start
    while d <= today:
        if d.weekday() != 6 and d not in present_dates:
            absent_list.append(str(d))
        d += timedelta(days=1)
    out["absent_dates"] = absent_list[:40]

    # ---------- CLASSES + REPORTS ----------
    cls = (db.query(TimetableEntry)
           .filter(TimetableEntry.teacher_id == tp_id,
                   TimetableEntry.entry_type == "chapter",
                   TimetableEntry.completed == True,            # noqa: E712
                   TimetableEntry.entry_date >= start,
                   TimetableEntry.entry_date <= today))
    if hasattr(TimetableEntry, "archive_gen_id"):
        cls = cls.filter(TimetableEntry.archive_gen_id.is_(None))
    # SAME-TIME MULTI-BATCH: mirror (sister-batch) classes ko hours/count me mat gino -> ek
    # physical class ek hi baar. Mirror entries har batch ke student-view me fir bhi complete dikhti.
    if hasattr(TimetableEntry, "mirror_of_id"):
        cls = cls.filter(TimetableEntry.mirror_of_id.is_(None))
    cls = cls.all()
    classes_taken = len(cls)
    teach_min = 0
    on_time = delayed = unknown = 0
    delayed_reports = []
    for e in cls:
        dur = _duration_of(e) or CLASS_NOMINAL_MIN
        teach_min += dur
        dl = _delay_of(e)   # minutes class started late (None = unknown)
        if dl is None:
            unknown += 1
        elif dl > REPORT_DELAY_MIN:
            delayed += 1
            delayed_reports.append({
                "subject": e.subject or "", "chapter": e.chapter or "",
                "date": str(e.entry_date) if e.entry_date else "",
                "delay_min": int(dl)})
        else:
            on_time += 1
    out["classes_taken"] = classes_taken
    out["teach_hours"] = round(teach_min / 60.0, 1)
    out["reports_on_time"] = on_time
    out["reports_delayed"] = delayed
    out["reports_unknown"] = unknown
    delayed_reports.sort(key=lambda x: -x["delay_min"])
    out["delayed_reports"] = delayed_reports[:12]

    # ---------- DOUBTS ----------
    dts = (db.query(Doubt)
           .filter(Doubt.teacher_id == tp_id, Doubt.status == "resolved",
                   Doubt.resolved_at.isnot(None), Doubt.resolved_at >= dt0).all())
    solved = len(dts)
    d15 = 0
    most = None
    most_h = -1.0
    for d_ in dts:
        if d_.created_at and d_.resolved_at:
            h = (d_.resolved_at - d_.created_at).total_seconds() / 3600.0
            if h > DOUBT_DELAY_HOURS:
                d15 += 1
            if h > most_h:
                most_h = h
                most = d_
    out["doubts_solved"] = solved
    out["doubts_delayed_15h"] = d15
    out["most_delayed_doubt"] = ({
        "subject": most.subject or "", "topic": (most.topic or most.question or "")[:80],
        "hours": round(most_h, 1)} if most and most_h >= 0 else None)

    # ---------- VIDEO TASKS ----------
    try:
        from video_tasks import VT_COMPLETED_STATUSES, VT_OPEN_STATUSES
    except Exception:
        VT_COMPLETED_STATUSES = {"approved", "editing_soon", "editing_done", "uploaded"}
        VT_OPEN_STATUSES = {"assigned", "reshoot", "rejected"}
    vq = (db.query(VideoTask)
          .filter(VideoTask.teacher_id == tp_id,
                  VideoTask.created_at >= dt0,
                  VideoTask.cancelled.isnot(True)).all())
    assigned = shorts = uploaded = pending = delayed_v = 0
    task_names = []
    for t in vq:
        if (getattr(t, "proposal_ok", "") or "") == "pending":
            continue
        assigned += 1
        vt = (getattr(t, "video_type", "") or "").lower()
        is_short = ("short" in vt)
        if is_short:
            shorts += 1
        st = (t.status or "") or ""
        if st in VT_COMPLETED_STATUSES:
            uploaded += 1
        elif st in VT_OPEN_STATUSES:
            pending += 1
        # delayed: deadline ke baad submit, ya abhi tak open par deadline nikal gayi
        if t.submitted_at and t.deadline and t.submitted_at > t.deadline:
            delayed_v += 1
        elif st in VT_OPEN_STATUSES and t.deadline and t.deadline < now:
            delayed_v += 1
        if len(task_names) < 14:
            task_names.append({"title": (t.title or "Untitled")[:60],
                               "type": "Short" if is_short else "Video",
                               "status": st or "assigned"})
    out["videos_assigned"] = assigned
    out["videos_shorts"] = shorts
    out["videos_uploaded"] = uploaded
    out["videos_pending"] = pending
    out["videos_delayed"] = delayed_v
    out["video_tasks"] = task_names
    return out


def collect_all(db, teacher_ids=None, period="month"):
    """teacher_ids None -> saare teachers. Returns (period_label, [metrics...])."""
    from models import TeacherProfile, User
    from teacher_routes import _ist_now
    ref = _ist_now()
    q = db.query(TeacherProfile.id, TeacherProfile.user_id, User.name).join(
        User, User.id == TeacherProfile.user_id)
    rows = q.all()
    name_by_id = {tid: (nm or ("Teacher #%d" % tid)) for tid, _uid, nm in rows}
    if teacher_ids:
        want = set(int(x) for x in teacher_ids)
        ids = [tid for tid in name_by_id if tid in want]
    else:
        ids = list(name_by_id.keys())
    data = [collect_teacher_metrics(db, tid, name_by_id.get(tid), period, ref=ref)
            for tid in ids]
    # sabse active/productive upar
    data.sort(key=lambda m: (-(m["classes_taken"] + m["doubts_solved"]
                               + m["videos_uploaded"]), m["name"]))
    label = data[0]["period_label"] if data else _period_bounds(period, ref)[3]
    return label, data


# ==================================================================== PDF BUILD
# MVS premium palette
_GOLD = (184, 148, 31)
_GOLD_D = (138, 109, 16)
_INK = (26, 26, 30)
_MUT = (120, 120, 128)
_GREEN = (5, 150, 105)
_RED = (200, 40, 40)
_AMBER = (200, 120, 10)
_SOFT = (250, 248, 240)
_LINE = (228, 222, 205)


def _latin(s):
    """FPDF core fonts latin-1 only — baaki chars safe karo."""
    if s is None:
        return ""
    s = str(s)
    try:
        s.encode("latin-1")
        return s
    except Exception:
        return s.encode("latin-1", "replace").decode("latin-1")


def _logo_path():
    """MVS logo (logo.png) — repo root ya fonts/ me. Na mile to None (text-only fallback)."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("logo.png", "logo.jpg", "logo.jpeg"):
        for c in (os.path.join(here, name), os.path.join(here, "fonts", name),
                  os.path.join(os.getcwd(), name), name):
            if os.path.exists(c):
                return c
    return None


def _circle_img(raw_bytes, size=240):
    """Photo ko circular PNG bana do (premium avatar ke liye)."""
    from PIL import Image, ImageDraw
    import io as _io
    im = Image.open(_io.BytesIO(raw_bytes)).convert("RGBA")
    # center-crop to square
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    buf = _io.BytesIO()
    out.save(buf, "PNG")
    buf.seek(0)
    return buf


def _decode_photo(pb):
    """photo_b64 (data-uri ya raw b64) -> circular PNG BytesIO, ya None."""
    import base64
    try:
        s = pb
        if isinstance(s, str) and s[:5] == "data:" and "," in s[:64]:
            s = s.split(",", 1)[1]
        raw = base64.b64decode(s)
        return _circle_img(raw)
    except Exception:
        return None


def _load_photos(db, ids):
    from models import TeacherProfile
    out = {}
    if not ids:
        return out
    try:
        for tid, pb in db.query(TeacherProfile.id, TeacherProfile.photo_b64).filter(
                TeacherProfile.id.in_(list(ids))):
            if pb:
                buf = _decode_photo(pb)
                if buf is not None:
                    out[tid] = buf
    except Exception:
        pass
    return out


def _fig_png(fig, w=1200, dpi=150):
    import io as _io
    buf = _io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    import matplotlib.pyplot as plt
    plt.close(fig)
    buf.seek(0)
    return buf


def _comparison_chart(data):
    """Teachers comparison — 4 horizontal bar charts (classes, hours, doubts, videos)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    top = data[:12]
    names = [m["name"].split(" ")[0] if len(m["name"]) > 12 else m["name"] for m in top]
    gold = "#B8941F"
    panels = [("classes_taken", "Classes Taken", "#B8941F"),
              ("teach_hours", "Teaching Hours", "#8a6d10"),
              ("doubts_solved", "Doubts Solved", "#2563eb"),
              ("videos_uploaded", "Videos Uploaded", "#059669")]
    fig, axes = plt.subplots(2, 2, figsize=(10, max(3.2, 0.5 * len(top) + 1.6)))
    axes = axes.flatten()
    for ax, (key, title, color) in zip(axes, panels):
        vals = [m.get(key, 0) for m in top]
        y = range(len(top))
        ax.barh(list(y), vals, color=color, height=0.62, zorder=3)
        ax.set_yticks(list(y))
        ax.set_yticklabels(names, fontsize=8)
        ax.invert_yaxis()
        ax.set_title(title, fontsize=10, fontweight="bold", color="#1a1a1e", loc="left", pad=6)
        ax.grid(axis="x", color="#eee", zorder=0)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(length=0)
        mx = max(vals) if vals and max(vals) > 0 else 1
        for i, v in enumerate(vals):
            ax.text(v + mx * 0.02, i, str(v), va="center", fontsize=8,
                    fontweight="bold", color="#444")
        ax.set_xlim(0, mx * 1.15)
    fig.suptitle("Teacher Comparison", fontsize=13, fontweight="bold",
                 color="#8a6d10", x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return _fig_png(fig)


def _ontime_chart(data):
    """Class-report punctuality — stacked on-time vs delayed per teacher (%)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    top = [m for m in data if (m["reports_on_time"] + m["reports_delayed"]) > 0][:12]
    if not top:
        return None
    names = [m["name"].split(" ")[0] if len(m["name"]) > 12 else m["name"] for m in top]
    ot = [m["reports_on_time"] for m in top]
    dl = [m["reports_delayed"] for m in top]
    fig, ax = plt.subplots(figsize=(10, max(2.4, 0.42 * len(top) + 1.2)))
    y = range(len(top))
    ax.barh(list(y), ot, color="#059669", height=0.6, label="On time", zorder=3)
    ax.barh(list(y), dl, left=ot, color="#dc2626", height=0.6, label="Delayed (>1h)", zorder=3)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=8)
    ax.invert_yaxis()
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.grid(axis="x", color="#eee", zorder=0)
    ax.legend(loc="lower right", fontsize=8, frameon=False)
    ax.set_title("Class Report Punctuality (on-time vs delayed)", fontsize=12,
                 fontweight="bold", color="#8a6d10", loc="left", pad=8)
    fig.tight_layout()
    return _fig_png(fig)


class _RPDF:
    """Chhota FPDF wrapper — premium header/footer + helpers."""
    def __init__(self, period_label):
        from fpdf import FPDF
        self.pdf = FPDF(unit="mm", format="A4")
        self.pdf.set_auto_page_break(True, margin=16)
        self.period_label = period_label
        self.pdf.set_title("MVS Teacher Progress Report")
        self.logo = _logo_path()

    def header_band(self, subtitle=""):
        p = self.pdf
        p.set_fill_color(*_GOLD_D)
        p.rect(0, 0, 210, 26, "F")
        p.set_fill_color(*_GOLD)
        p.rect(0, 24, 210, 2, "F")
        tx = 12
        if self.logo:
            try:
                p.image(self.logo, x=10, y=3.5, w=19, h=19)
                tx = 33
            except Exception:
                tx = 12
        p.set_xy(tx, 6)
        p.set_text_color(255, 255, 255)
        p.set_font("Helvetica", "B", 16)
        p.cell(150, 7, _latin("MVS Foundation"), 0, 2, "L")
        p.set_x(tx)
        p.set_font("Helvetica", "", 10)
        p.cell(150, 5, _latin("Teacher Progress Report  -  " + self.period_label), 0, 0, "L")
        if subtitle:
            p.set_xy(12, 6)
            p.set_font("Helvetica", "B", 11)
            p.cell(186, 14, _latin(subtitle), 0, 0, "R")
        p.set_text_color(*_INK)
        p.set_y(32)

    def cover_page(self, label, data):
        """Premium front page — big MVS logo + title + period + teachers + date."""
        p = self.pdf
        p.add_page()
        # deep navy background (MVS brand)
        p.set_fill_color(13, 22, 48)
        p.rect(0, 0, 210, 297, "F")
        # gold frame
        p.set_draw_color(*_GOLD)
        p.set_line_width(0.8)
        p.rect(10, 10, 190, 277)
        p.set_line_width(0.2)
        # big logo centered
        if self.logo:
            try:
                p.image(self.logo, x=75, y=46, w=60, h=60)
            except Exception:
                pass
        else:
            p.set_xy(0, 66)
            p.set_text_color(*_GOLD)
            p.set_font("Helvetica", "B", 40)
            p.cell(210, 20, "MVS", 0, 0, "C")
        # brand name
        p.set_xy(0, 112)
        p.set_text_color(255, 255, 255)
        p.set_font("Helvetica", "B", 26)
        p.cell(210, 12, _latin("MVS FOUNDATION"), 0, 2, "C")
        p.set_text_color(*_GOLD)
        p.set_font("Helvetica", "", 11)
        p.cell(210, 7, _latin("N I O S   C O A C H I N G"), 0, 2, "C")
        # gold divider
        p.set_fill_color(*_GOLD)
        p.rect(80, 140, 50, 0.8, "F")
        # title
        p.set_xy(0, 150)
        p.set_text_color(255, 255, 255)
        p.set_font("Helvetica", "B", 22)
        p.cell(210, 12, _latin("Teacher Progress Report"), 0, 2, "C")
        p.set_text_color(210, 215, 230)
        p.set_font("Helvetica", "", 13)
        p.cell(210, 8, _latin(label), 0, 2, "C")
        # who
        if len(data) == 1:
            who = data[0]["name"]
        else:
            who = "%d Teachers" % len(data)
        p.ln(6)
        p.set_text_color(*_GOLD)
        p.set_font("Helvetica", "B", 15)
        p.cell(210, 9, _latin(who), 0, 2, "C")
        # footer: generated date
        p.set_xy(0, 262)
        p.set_text_color(170, 178, 200)
        p.set_font("Helvetica", "", 9.5)
        p.cell(210, 6, _latin("Generated on " + _now_str()), 0, 2, "C")
        p.cell(210, 6, _latin("Confidential - for internal review"), 0, 0, "C")
        p.set_text_color(*_INK)


def _stat_card(pdf, x, y, w, h, value, label, color=_INK, sub=""):
    pdf.set_fill_color(*_SOFT)
    pdf.set_draw_color(*_LINE)
    pdf.rect(x, y, w, h, "DF")
    pdf.set_xy(x, y + 2.5)
    pdf.set_text_color(*color)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(w, 8, _latin(str(value)), 0, 2, "C")
    pdf.set_text_color(*_MUT)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_xy(x, y + h - 6.5)
    pdf.cell(w, 3.5, _latin(label.upper()), 0, 0, "C")


def _section_title(pdf, txt):
    pdf.ln(2)
    pdf.set_text_color(*_GOLD_D)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, _latin(txt), 0, 1, "L")
    y = pdf.get_y()
    pdf.set_draw_color(*_LINE)
    pdf.line(12, y, 198, y)
    pdf.ln(2)
    pdf.set_text_color(*_INK)


def _teacher_page(RP, m, photo=None):
    pdf = RP.pdf
    pdf.add_page()
    RP.header_band(subtitle="")
    ny = pdf.get_y()
    tx = 12
    if photo is not None:
        try:
            # subtle gold ring behind the avatar
            pdf.set_fill_color(*_GOLD)
            pdf.ellipse(11.2, ny - 0.3, 21.6, 21.6, "F")
            pdf.image(photo, x=12, y=ny, w=20, h=20)
            tx = 37
        except Exception:
            tx = 12
    pdf.set_xy(tx, ny + 2)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 9, _latin(m["name"]), 0, 2, "L")
    pdf.set_x(tx)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_MUT)
    pdf.cell(0, 5, _latin(m["period_label"]), 0, 1, "L")
    if photo is not None and pdf.get_y() < ny + 22:
        pdf.set_y(ny + 22)
    pdf.ln(2)

    # ---- ACTIVITY cards row ----
    _section_title(pdf, "Activity & Teaching")
    y = pdf.get_y()
    cards = [
        (m["active_days"], "Active Days", _GREEN),
        (m["active_hours"], "Active Hours", _INK),
        (m["absent_days"], "Absent (working)", _RED if m["absent_days"] else _MUT),
        (m["classes_taken"], "Classes Taken", _INK),
        (m["teach_hours"], "Teaching Hours", _GOLD_D),
    ]
    w = 35.2
    for i, (v, l, c) in enumerate(cards):
        _stat_card(pdf, 12 + i * (w + 1), y, w, 17, v, l, c)
    pdf.set_y(y + 20)

    # ---- CLASS REPORTS ----
    _section_title(pdf, "Class Report Punctuality")
    y = pdf.get_y()
    cards = [(m["reports_on_time"], "On Time", _GREEN),
             (m["reports_delayed"], "Delayed >1h", _RED),
             (m["reports_unknown"], "No Timing", _MUT)]
    w = 59.3
    for i, (v, l, c) in enumerate(cards):
        _stat_card(pdf, 12 + i * (w + 1), y, w, 16, v, l, c)
    pdf.set_y(y + 19)
    if m["delayed_reports"]:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*_RED)
        pdf.cell(0, 5, _latin("Delayed reports:"), 0, 1)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_INK)
        for r in m["delayed_reports"][:6]:
            pdf.cell(0, 4.6, _latin("  - %s %s (%s)  %d min late" % (
                r["subject"], r["chapter"], r["date"], r["delay_min"])), 0, 1)
    pdf.ln(1)

    # ---- DOUBTS ----
    _section_title(pdf, "Doubts Solved")
    y = pdf.get_y()
    cards = [(m["doubts_solved"], "Solved", _GREEN),
             (m["doubts_delayed_15h"], "Delayed >15h", _AMBER),
             (m["most_delayed_doubt"]["hours"] if m["most_delayed_doubt"] else "-",
              "Most Delayed (h)", _RED)]
    w = 59.3
    for i, (v, l, c) in enumerate(cards):
        _stat_card(pdf, 12 + i * (w + 1), y, w, 16, v, l, c)
    pdf.set_y(y + 19)
    if m["most_delayed_doubt"]:
        md = m["most_delayed_doubt"]
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_MUT)
        pdf.cell(0, 4.6, _latin("Most delayed: %s - %s (%.1f h)" % (
            md["subject"], md["topic"], md["hours"])), 0, 1)
    pdf.ln(1)

    # ---- VIDEO TASKS ----
    _section_title(pdf, "Video Tasks")
    y = pdf.get_y()
    cards = [(m["videos_assigned"], "Assigned", _INK),
             (m["videos_shorts"], "Shorts", _GOLD_D),
             (m["videos_uploaded"], "Uploaded", _GREEN),
             (m["videos_pending"], "Pending", _AMBER),
             (m["videos_delayed"], "Delayed", _RED)]
    w = 35.2
    for i, (v, l, c) in enumerate(cards):
        _stat_card(pdf, 12 + i * (w + 1), y, w, 16, v, l, c)
    pdf.set_y(y + 19)
    if m["video_tasks"]:
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*_INK)
        for t in m["video_tasks"][:8]:
            pdf.cell(0, 4.6, _latin("  - [%s] %s  (%s)" % (
                t["type"], t["title"], t["status"])), 0, 1)


def build_report_pdf(db, teacher_ids=None, period="month"):
    """Premium PDF bytes. teacher_ids None -> saare teachers."""
    label, data = collect_all(db, teacher_ids, period)
    RP = _RPDF(label)
    pdf = RP.pdf

    # ---- PREMIUM COVER PAGE ----
    if data:
        try:
            RP.cover_page(label, data)
        except Exception:
            pass
    photos = _load_photos(db, [m["teacher_id"] for m in data])

    # ---- OVERVIEW PAGE ----
    pdf.add_page()
    RP.header_band(subtitle="%d teacher%s" % (len(data), "" if len(data) == 1 else "s"))
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 9, _latin("Team Overview" if len(data) != 1 else data[0]["name"]), 0, 1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*_MUT)
    pdf.cell(0, 5, _latin(label + "   -   generated " + _now_str()), 0, 1)
    pdf.ln(1)

    if not data:
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 10, _latin("No teacher data for this period."), 0, 1)
        return bytes(pdf.output())

    # team totals
    tot = {k: sum(m[k] for m in data) for k in
           ("classes_taken", "teach_hours", "doubts_solved", "videos_uploaded",
            "videos_pending", "reports_delayed")}
    _section_title(pdf, "At a Glance")
    y = pdf.get_y()
    cards = [(round(tot["teach_hours"], 1), "Teaching Hours", _GOLD_D),
             (tot["classes_taken"], "Classes", _INK),
             (tot["doubts_solved"], "Doubts Solved", _GREEN),
             (tot["videos_uploaded"], "Videos Up", _GREEN),
             (tot["videos_pending"], "Videos Pending", _AMBER),
             (tot["reports_delayed"], "Late Reports", _RED)]
    w = 29.3
    for i, (v, l, c) in enumerate(cards):
        _stat_card(pdf, 12 + i * (w + 1), y, w, 16, v, l, c)
    pdf.set_y(y + 20)

    # comparison chart (only when >1 teacher)
    if len(data) > 1:
        try:
            img = _comparison_chart(data)
            pdf.image(img, x=12, w=186)
            pdf.ln(2)
        except Exception:
            pass
        try:
            oc = _ontime_chart(data)
            if oc is not None:
                if pdf.get_y() > 210:
                    pdf.add_page(); RP.header_band()
                pdf.image(oc, x=12, w=186)
        except Exception:
            pass

    # per-teacher detail pages
    for m in data:
        _teacher_page(RP, m, photos.get(m["teacher_id"]))

    return bytes(pdf.output())


def _now_str():
    from teacher_routes import _ist_now
    return _ist_now().strftime("%d %b %Y, %I:%M %p")

