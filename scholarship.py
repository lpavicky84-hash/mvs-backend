"""MVS Scholarship Result Portal — helpers, seed, Excel (template/export/parse), premium PDF.

Flow:
  - Admin test ka result_type = "scholarship" set karta hai.
  - Selected students (name + batch) ki list (SEED se auto-fill ya Excel) + unke phone (admin).
  - Declare -> students public link par phone se result check karte hain.
  - Match: SIRF phone se. name/class sirf backend data (ScholarshipCheck log) ke liye.
  - Public ko sirf name + batch dikhte hain (koi phone/personal data nahi).
"""
import io
import json

# image 1 ki 20 selected students — admin ek click me auto-fill kar sake (phone baad me daale)
SEED_RESULTS = [
    ("Sumit Kumar", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Alina", "Lakshya (Commerce) – NIOS Class 12th April 2027"),
    ("Vanshika", "Lakshya (Arts) – NIOS Class 12th April 2027"),
    ("Harshita", "UDAAN – NIOS Class 10th April 2027"),
    ("Kartik Chaturvedi", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Nisha", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("shubham", "Lakshya (Arts) – NIOS Class 12th April 2027"),
    ("payal Singh", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Yash singh", "UDAAN – NIOS Class 10th April 2027"),
    ("Tanushka M", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("zainab", "Lakshya (Arts) – NIOS Class 12th April 2027"),
    ("Neeraj", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Devansh", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Abhijeet Gour", "Lakshya (Commerce) – NIOS Class 12th April 2027"),
    ("Akanksha", "UDAAN – NIOS Class 10th April 2027"),
    ("Kamil Badar", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Sandhya", "Lakshya (Commerce) – NIOS Class 12th April 2027"),
    ("Mahima", "Lakshya (Arts) – NIOS Class 12th April 2027"),
    ("Zameer", "Lakshya (Science) – NIOS Class 12th April 2027"),
    ("Rahul Thakur", "Lakshya (Science) – NIOS Class 12th April 2027"),
]


def norm_phone(p):
    """Phone ko match ke liye normalize: sirf digits, last 10 (country code/space ignore)."""
    d = "".join(ch for ch in str(p or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def public_list(db, exam_id):
    """Public-safe list: rank, name, batch (NO phone). Top rank upar."""
    from models import ScholarshipResult
    rows = (db.query(ScholarshipResult)
            .filter(ScholarshipResult.exam_id == exam_id)
            .order_by(ScholarshipResult.rank_no.asc(), ScholarshipResult.id.asc()).all())
    return [{"rank": i + 1, "name": r.name or "", "batch": r.batch or ""}
            for i, r in enumerate(rows)]


def match_phone(db, exam_id, phone):
    """phone -> matching ScholarshipResult ya None."""
    from models import ScholarshipResult
    np = norm_phone(phone)
    if not np:
        return None
    for r in (db.query(ScholarshipResult).filter(ScholarshipResult.exam_id == exam_id).all()):
        if norm_phone(r.phone) == np:
            return r
    return None


# ------------------------------------------------------------------ EXCEL
def results_template_xlsx():
    """Admin ke liye bulk-upload template: Name | Batch | Phone (+ example rows)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    heads = ["Name", "Batch", "Phone"]
    fill = PatternFill("solid", fgColor="0F1B33")
    for c, h in enumerate(heads, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True, color="FFFFFF", size=12)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 52
    ws.column_dimensions["C"].width = 18
    # example rows (admin ise replace kare)
    for name, batch in SEED_RESULTS[:2]:
        ws.append([name, batch, ""])
    ws.append(["", "", ""])
    note = ws.cell(ws.max_row + 2, 1,
                   "Note: Phone 10-digit. Name & Batch public dikhenge; Phone kabhi public nahi. "
                   "Upload par ye list replace ho jaayegi.")
    note.font = Font(italic=True, color="888888", size=9)
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()


def parse_results_xlsx(file_bytes):
    """Uploaded Excel/CSV -> [{name, batch, phone}]. Pehli header row skip (agar 'name' ho)."""
    from openpyxl import load_workbook
    rows = []
    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        first = True
        for r in ws.iter_rows(values_only=True):
            if r is None:
                continue
            vals = [("" if v is None else str(v).strip()) for v in r]
            while len(vals) < 3:
                vals.append("")
            name, batch, phone = vals[0], vals[1], vals[2]
            if first:
                first = False
                if name.lower() in ("name", "student", "student name"):
                    continue
            if not (name or batch or phone):
                continue
            rows.append({"name": name, "batch": batch, "phone": norm_phone(phone)})
    except Exception:
        # CSV fallback
        try:
            import csv
            txt = file_bytes.decode("utf-8", "replace")
            rd = csv.reader(io.StringIO(txt))
            first = True
            for vals in rd:
                while len(vals) < 3:
                    vals.append("")
                name, batch, phone = vals[0].strip(), vals[1].strip(), vals[2].strip()
                if first:
                    first = False
                    if name.lower() in ("name", "student", "student name"):
                        continue
                if not (name or batch or phone):
                    continue
                rows.append({"name": name, "batch": batch, "phone": norm_phone(phone)})
        except Exception:
            pass
    return rows


def checks_xlsx(db, exam):
    """Jo students ne result check kiya — admin ke liye full data (phone sahit)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from models import ScholarshipCheck
    wb = Workbook()
    ws = wb.active
    ws.title = "Result Checks"
    heads = ["#", "Name", "Phone", "Class", "Selected", "Allotted Batch", "Checked At"]
    fill = PatternFill("solid", fgColor="0F1B33")
    for c, h in enumerate(heads, 1):
        cell = ws.cell(1, c, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center")
    widths = [6, 24, 16, 14, 12, 46, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w
    rows = (db.query(ScholarshipCheck)
            .filter(ScholarshipCheck.exam_id == exam.id)
            .order_by(ScholarshipCheck.checked_at.desc()).all())
    for i, c in enumerate(rows, 1):
        ws.append([i, c.name or "", c.phone or "", c.student_class or "",
                   "YES" if c.matched else "NO", c.matched_batch or "",
                   c.checked_at.strftime("%d %b %Y, %I:%M %p") if c.checked_at else ""])
    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()


# ------------------------------------------------------------------ PREMIUM PDF
_NAVY = (13, 22, 48)
_GOLD = (184, 148, 31)
_GOLD_D = (138, 109, 16)
_INK = (26, 26, 30)
_MUT = (120, 120, 128)
_SOFT = (250, 248, 240)
_LINE = (228, 222, 205)


def _latin(s):
    if s is None:
        return ""
    s = str(s).replace("–", "-").replace("—", "-").replace("’", "'")
    try:
        s.encode("latin-1")
        return s
    except Exception:
        return s.encode("latin-1", "replace").decode("latin-1")


def _logo_path():
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("logo.png", "logo.jpg", "logo.jpeg"):
        for c in (os.path.join(here, name), os.path.join(os.getcwd(), name)):
            if os.path.exists(c):
                return c
    return None


def result_pdf(db, exam):
    """Premium scholarship result PDF — cover (MVS logo) + selected students table."""
    from fpdf import FPDF
    rows = public_list(db, exam.id)
    logo = _logo_path()
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_title("MVS Scholarship Result")

    # ---------- COVER ----------
    pdf.add_page()
    pdf.set_fill_color(*_NAVY)
    pdf.rect(0, 0, 210, 297, "F")
    pdf.set_draw_color(*_GOLD)
    pdf.set_line_width(0.8)
    pdf.rect(10, 10, 190, 277)
    pdf.set_line_width(0.2)
    if logo:
        try:
            pdf.image(logo, x=80, y=48, w=50, h=50)
        except Exception:
            pass
    pdf.set_xy(0, 104)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 25)
    pdf.cell(210, 12, _latin("MVS FOUNDATION"), 0, 2, "C")
    pdf.set_text_color(*_GOLD)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(210, 7, _latin("MANISH VERMA CLASSES"), 0, 2, "C")
    pdf.set_fill_color(*_GOLD)
    pdf.rect(80, 134, 50, 0.8, "F")
    pdf.set_xy(0, 144)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(210, 12, _latin("Scholarship Test Result"), 0, 2, "C")
    pdf.set_text_color(210, 215, 230)
    pdf.set_font("Helvetica", "", 13)
    pdf.cell(210, 8, _latin(exam.title or "MVS Scholarship Test"), 0, 2, "C")
    pdf.ln(5)
    pdf.set_text_color(*_GOLD)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(210, 9, _latin("%d Selected Students" % len(rows)), 0, 2, "C")
    pdf.set_xy(0, 262)
    pdf.set_text_color(170, 178, 200)
    pdf.set_font("Helvetica", "", 9.5)
    from datetime import datetime
    try:
        from teacher_routes import _ist_now
        now = _ist_now()
    except Exception:
        now = datetime.now()
    pdf.cell(210, 6, _latin("Generated on " + now.strftime("%d %b %Y, %I:%M %p")), 0, 2, "C")
    pdf.cell(210, 6, _latin("Confidential - MVS Foundation"), 0, 0, "C")

    # ---------- RESULTS TABLE ----------
    def header_band():
        pdf.set_fill_color(*_GOLD_D)
        pdf.rect(0, 0, 210, 24, "F")
        pdf.set_fill_color(*_GOLD)
        pdf.rect(0, 22, 210, 2, "F")
        tx = 12
        if logo:
            try:
                pdf.image(logo, x=10, y=3.5, w=17, h=17)
                tx = 31
            except Exception:
                tx = 12
        pdf.set_xy(tx, 5)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(150, 7, _latin("MVS Foundation"), 0, 2, "L")
        pdf.set_x(tx)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(150, 5, _latin("Scholarship Test Result - Selected Students"), 0, 0, "L")
        pdf.set_text_color(*_INK)
        pdf.set_y(30)

    def footer():
        pdf.set_y(-14)
        pdf.set_draw_color(*_LINE)
        pdf.line(12, pdf.get_y(), 198, pdf.get_y())
        pdf.set_y(-12)
        pdf.set_text_color(*_MUT)
        pdf.set_font("Helvetica", "", 8)
        pdf.cell(0, 6, _latin("MVS Foundation - Manish Verma Classes   |   " + (exam.title or "Scholarship Test")),
                 0, 0, "C")

    pdf.add_page()
    header_band()
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_INK)
    pdf.cell(0, 8, _latin("Selected Students & Allotted Batch"), 0, 1, "L")
    pdf.ln(1)
    # table header
    pdf.set_fill_color(*_NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(16, 9, "Rank", 1, 0, "C", True)
    pdf.cell(58, 9, "Name", 1, 0, "L", True)
    pdf.cell(112, 9, "Allotted Batch", 1, 1, "L", True)
    pdf.set_font("Helvetica", "", 9.5)
    for i, r in enumerate(rows):
        if pdf.get_y() > 272:
            footer()
            pdf.add_page()
            header_band()
            pdf.set_fill_color(*_NAVY)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(16, 9, "Rank", 1, 0, "C", True)
            pdf.cell(58, 9, "Name", 1, 0, "L", True)
            pdf.cell(112, 9, "Allotted Batch", 1, 1, "L", True)
            pdf.set_font("Helvetica", "", 9.5)
        top10 = (i < 10)
        pdf.set_fill_color(*( _SOFT if (i % 2 == 0) else (255, 255, 255)))
        pdf.set_text_color(*(_GOLD_D if top10 else _INK))
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(16, 8, _latin("#%d" % r["rank"]), 1, 0, "C", True)
        pdf.set_text_color(*_INK)
        pdf.set_font("Helvetica", "B" if top10 else "", 10)
        pdf.cell(58, 8, _latin((r["name"] or "")[:34]), 1, 0, "L", True)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(70, 70, 76)
        pdf.cell(112, 8, _latin((r["batch"] or "")[:70]), 1, 1, "L", True)
    footer()
    return bytes(pdf.output())


# ========================= PROMO BANNER (upload) + BATCH AUTO-FETCH =========================
def compress_banner(raw, max_w=1600):
    """Uploaded promo banner ko compress karke base64 data-URL me store karne layak banao.
    PIL se resize + JPEG q82. Fail ho to original ko hi base64 me daal do (best-effort)."""
    import base64
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        # transparency (PNG) ko white background par flatten karo
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
            im = bg
        else:
            im = im.convert("RGB")
        if im.width > max_w:
            h = int(im.height * (max_w / float(im.width)))
            im = im.resize((max_w, h), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=82, optimize=True)
        b = out.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")
    except Exception:
        try:
            # unknown/animated — keep original bytes, guess mime
            mime = "image/png" if raw[:4] == b"\x89PNG" else ("image/gif" if raw[:3] == b"GIF" else "image/jpeg")
            return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
        except Exception:
            return ""


def _meta_pick(html, keys):
    """HTML se meta tag content nikalo. keys = property/name values ki list (priority order)."""
    import re
    for k in keys:
        # property="og:image" content="..."  OR  content="..." property="og:image"  (dono order)
        for pat in (
            r'<meta[^>]+(?:property|name)\s*=\s*["\']%s["\'][^>]*content\s*=\s*["\']([^"\']+)["\']' % re.escape(k),
            r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]*(?:property|name)\s*=\s*["\']%s["\']' % re.escape(k),
        ):
            m = re.search(pat, html, re.I)
            if m and m.group(1).strip():
                return m.group(1).strip()
    return ""


def _abs_url(base, u):
    try:
        from urllib.parse import urljoin
        return urljoin(base, u)
    except Exception:
        return u


def _download_image(url, referer=None):
    """Thumbnail image download karke compressed base64 data-URL banao (hotlink-proof).
    Fail/non-image/too-big par "" (caller remote URL fallback use karega)."""
    import re
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": referer or url,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            ct = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read(6 * 1024 * 1024)  # cap 6MB
    except Exception:
        return ""
    if not data:
        return ""
    if "image" not in ct and not re.search(r'\.(jpe?g|png|webp|gif|avif)(\?|$)', url, re.I):
        return ""
    return compress_banner(data, max_w=760)


def fetch_batch_meta(url):
    """Admin ke paste kiye batch URL se thumbnail + title + price auto-fetch.
    Sirf http/https. Best-effort: OG/twitter meta + JSON-LD offers.price + ₹ regex fallback.
    Returns {title,image,price,old,desc,url}. Network/parse fail par jitna mila wahi."""
    import re
    import json as _json
    from urllib.parse import urlparse
    out = {"title": "", "image": "", "image_url": "", "price": "", "old": "", "desc": "", "url": url or ""}
    u = (url or "").strip()
    if not u:
        return out
    if not re.match(r'^https?://', u, re.I):
        u = "https://" + u
        out["url"] = u
    pr = urlparse(u)
    if pr.scheme not in ("http", "https") or not pr.netloc:
        return out
    try:
        import urllib.request
        req = urllib.request.Request(u, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read(1500000)  # cap ~1.5MB
            final_url = resp.geturl() or u
        enc = "utf-8"
        html = raw.decode(enc, "ignore")
    except Exception:
        return out
    # title
    out["title"] = (_meta_pick(html, ["og:title", "twitter:title"]) or "")
    if not out["title"]:
        m = re.search(r'<title[^>]*>([^<]{1,160})</title>', html, re.I)
        if m:
            out["title"] = m.group(1).strip()
    # description
    out["desc"] = (_meta_pick(html, ["og:description", "twitter:description", "description"]) or "")[:300]
    # image (thumbnail/banner)
    img = _meta_pick(html, ["og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"])
    if not img:
        m = re.search(r'<link[^>]+rel\s*=\s*["\']image_src["\'][^>]*href\s*=\s*["\']([^"\']+)["\']', html, re.I)
        if m:
            img = m.group(1).strip()
    if img:
        abs_img = _abs_url(final_url, img)
        out["image_url"] = abs_img
        # thumbnail ko DOWNLOAD karke base64 me store -> hotlink/CORS/referer block se bach jaao,
        # aur student page par pakka dikhe (image 3 jaisa). Fail ho to remote URL fallback.
        data_url = _download_image(abs_img, referer=final_url)
        out["image"] = data_url or abs_img

    def _num(s):
        try:
            return int(re.sub(r'[^0-9]', '', str(s)))
        except Exception:
            return 0

    # ---- PRICE (smart): JSON-LD/meta pehle, warna ₹ regex with promo-filter + discount-pair ----
    ld_prices = []
    try:
        for blk in re.findall(r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            try:
                data = _json.loads(blk.strip())
            except Exception:
                continue
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    off = node.get("offers")
                    if isinstance(off, dict):
                        stack.append(off)
                    elif isinstance(off, list):
                        stack.extend(off)
                    for pk in ("price", "lowPrice", "highPrice"):
                        if node.get(pk) and _num(node.get(pk)) >= 1:
                            ld_prices.append(_num(node.get(pk)))
                    stack.extend([v for v in node.values() if isinstance(v, (dict, list))])
                elif isinstance(node, list):
                    stack.extend(node)
    except Exception:
        pass
    mp = _meta_pick(html, ["product:price:amount", "og:price:amount"])
    mp_n = _num(mp) if mp else 0

    # ₹ amounts with nearby context — "off/discount/save/coupon/flat/cashback" wale promo drop
    reup = re.compile(r'(?:₹|&#8377;|&#x20b9;|Rs\.?\s?)\s*([0-9][0-9,]{1,8})', re.I)
    promo_re = re.compile(r'off|discount|save|saving|coupon|flat|cash\s?back|extra|upto|up\s*to|%', re.I)
    reg_nums = []
    for m in reup.finditer(html):
        n = _num(m.group(1))
        if n < 400 or n > 500000:
            continue
        a = max(0, m.start() - 22); b = min(len(html), m.end() + 14)
        if promo_re.search(html[a:b]):
            continue  # promo/discount amount — real price nahi
        reg_nums.append(n)

    price_n = 0; old_n = 0
    if ld_prices:
        # JSON-LD sabse reliable: sabse chhota = current price
        price_n = min(ld_prices)
        bigger = [x for x in (reg_nums + ld_prices) if x > price_n]
        if bigger:
            old_n = max(bigger)
    elif mp_n:
        price_n = mp_n
        bigger = [x for x in reg_nums if x > price_n]
        if bigger:
            old_n = max(bigger)
    elif reg_nums:
        uniq = sorted(set(reg_nums))
        # discount-pair: do amounts jisme bada/chhota <= 2.6x (real MRP vs discounted), warna sirf price
        best = None
        for hi in reversed(uniq):
            for lo in uniq:
                if lo < hi and hi / float(lo) <= 2.6:
                    best = (lo, hi); break
            if best:
                break
        if best:
            price_n, old_n = best
        else:
            price_n = min(uniq)

    if price_n:
        out["price"] = "₹" + "{:,}".format(price_n)
    if old_n and old_n > price_n:
        out["old"] = "₹" + "{:,}".format(old_n)
    out["title"] = (out["title"] or "")[:140]
    return out
