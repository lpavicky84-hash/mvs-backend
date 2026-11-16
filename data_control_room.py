"""
MVS Data Control Room — merged export of ALL MVS Services interactions.

One place to pull every paid (and "not yet") response across every service:
name, phone, class, WHICH service, amount, and date — filtered by
Today / Weekly / Monthly / Custom range and by response (Yes / No / All),
downloadable as Excel, PDF or Image (PNG). Additive only.
"""
import io
from datetime import datetime, timedelta

_IST = timedelta(hours=5, minutes=30)


def _ist_str(dt):
    if not dt:
        return ""
    try:
        return (dt + _IST).strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return ""


def _ist_date(dt):
    if not dt:
        return ""
    try:
        return (dt + _IST).strftime("%d %b %Y")
    except Exception:
        return ""


def merged_rows(db, response="all", start=None, end=None, service_ids=None):
    """Merged interaction rows across services. response: yes | no | all (feedback given)."""
    from models import MVSServiceInteraction as MI, MVSService
    resp = (response or "all").strip().lower()
    q = db.query(MI)
    if resp == "yes":
        q = q.filter(MI.response == "yes")
    elif resp == "no":
        q = q.filter(MI.response == "no")
    else:  # all = anyone who answered yes or no
        q = q.filter(MI.response.in_(["yes", "no"]))
    if service_ids:
        q = q.filter(MI.service_id.in_(list(service_ids)))
    if start:
        q = q.filter(MI.responded_at >= start)
    if end:
        q = q.filter(MI.responded_at < end)
    rows = q.order_by(MI.responded_at.desc(), MI.id.desc()).all()
    # service title map
    sids = {r.service_id for r in rows}
    titles = {}
    if sids:
        for s in db.query(MVSService).filter(MVSService.id.in_(list(sids))).all():
            titles[s.id] = s.title or ("Service #%s" % s.id)
    out = []
    for r in rows:
        out.append({
            "name": r.student_name or "",
            "phone": r.student_phone or "",
            "class": r.student_class or "",
            "service": titles.get(r.service_id, "Service #%s" % r.service_id),
            "response": (r.response or "").upper(),
            "amount": (float(r.amount) if r.amount else 0.0),
            "has_amount": bool(r.amount),
            "time": _ist_str(r.responded_at),
            "date": _ist_date(r.responded_at),
            "_dt": r.responded_at,
        })
    return out


def summarize(rows):
    yes = [r for r in rows if r["response"] == "YES"]
    no = [r for r in rows if r["response"] == "NO"]
    revenue = sum(r["amount"] for r in yes)
    return {"total": len(rows), "yes": len(yes), "no": len(no), "revenue": revenue}


def range_label(rng, frm=None, to=None):
    rng = (rng or "").strip().lower()
    return {
        "today": "Today", "week": "Last 7 days", "weekly": "Last 7 days",
        "month": "Last 30 days", "monthly": "Last 30 days",
    }.get(rng, ("%s to %s" % (frm or "…", to or "…")) if rng == "custom" else "All time")


# ------------------------------- EXCEL -------------------------------
def build_xlsx(rows, meta):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = Workbook()
    ws = wb.active
    ws.title = "Data Control Room"
    navy = PatternFill("solid", fgColor="0F1B33")
    gold = PatternFill("solid", fgColor="B8941F")
    thin = Side(style="thin", color="E3DAC4")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:H1")
    t = ws.cell(1, 1, "MVS Foundation — Data Control Room")
    t.font = Font(bold=True, size=15, color="FFFFFF")
    t.fill = navy
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 26
    ws.merge_cells("A2:H2")
    sub = ws.cell(2, 1, "%s  ·  %s  ·  %d records  ·  Revenue: Rs %s" % (
        meta.get("response_label", "All"), meta.get("range_label", "All time"),
        meta.get("total", 0), _money(meta.get("revenue", 0))))
    sub.font = Font(size=10, color="5A5340")
    sub.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 18

    heads = ["#", "Name", "Phone", "Class", "Service (paid for)", "Response", "Amount (Rs)", "Date & Time"]
    for c, h in enumerate(heads, 1):
        cell = ws.cell(4, c, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = gold
        cell.alignment = Alignment(horizontal="center")
        cell.border = border
    for i, w in enumerate([5, 24, 16, 9, 30, 11, 13, 22], 1):
        ws.column_dimensions[chr(64 + i)].width = w
    rr = 5
    for i, r in enumerate(rows, 1):
        vals = [i, r["name"], r["phone"], r["class"], r["service"], r["response"],
                (r["amount"] if r["has_amount"] else ""), r["time"]]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(rr, c, v)
            cell.border = border
            if c in (1, 4, 6, 7):
                cell.alignment = Alignment(horizontal="center")
            if r["response"] == "YES" and c == 6:
                cell.font = Font(bold=True, color="1F7A52")
            elif r["response"] == "NO" and c == 6:
                cell.font = Font(color="C0392B")
        rr += 1
    # totals
    tc = ws.cell(rr, 5, "TOTAL REVENUE")
    tc.font = Font(bold=True)
    tc.alignment = Alignment(horizontal="right")
    vc = ws.cell(rr, 7, meta.get("revenue", 0))
    vc.font = Font(bold=True, color="1F7A52")
    vc.alignment = Alignment(horizontal="center")
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()


def _money(n):
    try:
        n = float(n)
        s = ("%d" % n) if n == int(n) else ("%.2f" % n)
    except Exception:
        return str(n)
    # Indian grouping
    neg = s.startswith("-")
    s = s.lstrip("-")
    if "." in s:
        ip, dp = s.split(".", 1)
        dp = "." + dp
    else:
        ip, dp = s, ""
    if len(ip) > 3:
        head = ip[:-3]
        tail = ip[-3:]
        import re as _re
        head = _re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        ip = head + "," + tail
    return ("-" if neg else "") + ip + dp


# ------------------------------- PDF -------------------------------
def build_pdf(rows, meta):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                    Spacer)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=12 * mm, rightMargin=12 * mm, title="MVS Data Control Room")
    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Title"], fontSize=17, textColor=colors.HexColor("#0F1B33"),
                       spaceAfter=2, alignment=TA_CENTER)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9.5, textColor=colors.HexColor("#6B6355"),
                         alignment=TA_CENTER, spaceAfter=10)
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=8.3, leading=10.5)
    cellc = ParagraphStyle("cc", parent=cell, alignment=TA_CENTER)

    story = [Paragraph("MVS Foundation — Data Control Room", h),
             Paragraph("%s &nbsp;·&nbsp; %s &nbsp;·&nbsp; %d records &nbsp;·&nbsp; Revenue: Rs %s" % (
                 meta.get("response_label", "All"), meta.get("range_label", "All time"),
                 meta.get("total", 0), _money(meta.get("revenue", 0))), sub)]

    head = ["#", "Name", "Phone", "Class", "Service", "Resp", "Amount", "Date"]
    data = [head]
    for i, r in enumerate(rows, 1):
        data.append([str(i), Paragraph(_esc(r["name"]), cell), r["phone"], r["class"],
                     Paragraph(_esc(r["service"]), cell), r["response"],
                     (_money(r["amount"]) if r["has_amount"] else "—"),
                     Paragraph(_esc(r["date"]), cellc)])
    col_w = [9 * mm, 34 * mm, 24 * mm, 12 * mm, 44 * mm, 13 * mm, 20 * mm, 30 * mm]
    tbl = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#B8941F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("FONTSIZE", (0, 1), (-1, -1), 8.3),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (3, 0), (3, -1), "CENTER"),
        ("ALIGN", (5, 0), (6, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E3DAC4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FBF7EC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for ri, r in enumerate(rows, 1):
        if r["response"] == "YES":
            style.append(("TEXTCOLOR", (5, ri), (5, ri), colors.HexColor("#1F7A52")))
        else:
            style.append(("TEXTCOLOR", (5, ri), (5, ri), colors.HexColor("#C0392B")))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    if not rows:
        story.append(Spacer(1, 10 * mm))
        story.append(Paragraph("No records for this filter.", sub))
    doc.build(story)
    bio.seek(0)
    return bio.getvalue()


def _esc(s):
    return (str(s or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------- IMAGE (PNG) -------------------------------
def build_png(rows, meta):
    from PIL import Image, ImageDraw, ImageFont

    def _font(sz, bold=False):
        cands = (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold
                 else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
        cands += ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
        for c in cands:
            try:
                return ImageFont.truetype(c, sz)
            except Exception:
                continue
        return ImageFont.load_default()

    W = 1180
    pad = 34
    header_h = 118
    row_h = 46
    head_row_h = 44
    n = len(rows)
    H = header_h + head_row_h + max(n, 1) * row_h + 70
    img = Image.new("RGB", (W, H), "#F6F2E8")
    d = ImageDraw.Draw(img)
    f_title = _font(30, True)
    f_sub = _font(16)
    f_h = _font(15, True)
    f_c = _font(15)
    f_cb = _font(15, True)

    # header band
    d.rectangle([0, 0, W, header_h], fill="#0F1B33")
    d.rectangle([0, header_h, W, header_h + 6], fill="#B8941F")
    d.text((pad, 30), "MVS Foundation", font=f_title, fill="#FFFFFF")
    d.text((pad, 72), "Data Control Room", font=_font(19, True), fill="#E8C15A")
    meta_line = "%s  ·  %s  ·  %d records  ·  Revenue: Rs %s" % (
        meta.get("response_label", "All"), meta.get("range_label", "All time"),
        meta.get("total", 0), _money(meta.get("revenue", 0)))
    tw = d.textlength(meta_line, font=f_sub)
    d.text((W - pad - tw, 84), meta_line, font=f_sub, fill="#C9BFA6")

    # columns: #, Name, Phone, Class, Service, Resp, Amount, Date (last col = remaining width)
    cols = [("#", 44), ("Name", 208), ("Phone", 142), ("Class", 72),
            ("Service", 262), ("Resp", 80), ("Amount", 108), ("Date", 0)]
    xs = []
    x = pad
    for i, (_, w) in enumerate(cols):
        xs.append(x)
        x += (w if w else (W - pad - x))
    y = header_h + 14
    # header row
    d.rectangle([pad, y, W - pad, y + head_row_h], fill="#B8941F")
    for i, (name, _) in enumerate(cols):
        d.text((xs[i] + 8, y + 12), name, font=f_h, fill="#FFFFFF")
    y += head_row_h

    def _clip(txt, font, maxw):
        txt = str(txt or "")
        if d.textlength(txt, font=font) <= maxw:
            return txt
        while txt and d.textlength(txt + "…", font=font) > maxw:
            txt = txt[:-1]
        return txt + "…"

    for idx, r in enumerate(rows, 1):
        if idx % 2 == 0:
            d.rectangle([pad, y, W - pad, y + row_h], fill="#FBF7EC")
        colw = [c[1] for c in cols]
        vals = [str(idx), r["name"], r["phone"], r["class"], r["service"],
                r["response"], (_money(r["amount"]) if r["has_amount"] else "—"), r["date"]]
        for i, v in enumerate(vals):
            maxw = (colw[i] if colw[i] else (W - pad - xs[i])) - 14
            fill = "#14213D"
            font = f_c
            if i == 5:
                fill = "#1F7A52" if r["response"] == "YES" else "#C0392B"
                font = f_cb
            elif i == 1:
                font = f_cb
            d.text((xs[i] + 8, y + 13), _clip(v, font, maxw), font=font, fill=fill)
        d.line([pad, y + row_h, W - pad, y + row_h], fill="#ECE3CE", width=1)
        y += row_h
    if not rows:
        d.text((pad + 10, y + 14), "No records for this filter.", font=f_c, fill="#6B6355")
        y += row_h
    # footer
    d.text((pad, y + 16), "Generated by MVS Class Manager", font=_font(13), fill="#9A8F78")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio.getvalue()
