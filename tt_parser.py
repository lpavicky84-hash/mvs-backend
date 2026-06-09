"""PDF timetable parser — extracts subject-wise chapter/part/date/day/time flow."""
import re, io
from datetime import datetime

try:
    import pdfplumber
except Exception:
    pdfplumber = None

SUBJECT_PATTERNS = [
    ('Physics','PHYSICS'),('Chemistry','CHEMISTRY'),('Mathematics','MATHEMATIC'),
    ('Biology','BIOLOGY'),('English','ENGLISH'),('Home Science','HOME SCIENCE'),
    ('Data Entry Operations','DATA ENTRY'),('Hindi','HINDI'),('Computer Science','COMPUTER'),
    ('Psychology','PSYCHOLOG'),('Painting','PAINTING'),('Physical Education','PHYSICAL EDUCATION'),
    ('Accountancy','ACCOUNT'),('Economics','ECONOMIC'),('Business Studies','BUSINESS'),
    ('Sociology','SOCIOLOG'),('Political Science','POLITICAL'),('Geography','GEOGRAPH'),
    ('History','HISTORY'),('Sanskrit','SANSKRIT'),
]
MONTHS = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
EVENT_KW = ['doubt','dpp','test series','mission 75','mission75','pyq','mock test','subjective test',
            'answer writing','target 75','note making','strategy just before','revision']
PART_RE = re.compile(r'\(\s*(?:part|p)\s*[-–]?\s*(\d+)\s*\)', re.I)
CHAP_RE = re.compile(r'(chapter|ch|lesson)\s*[-\s]?\s*\d+', re.I)

def detect_subject(text):
    up = (text or '').upper()
    for name, pat in SUBJECT_PATTERNS:
        if pat in up:
            return name
    return None

def parse_date(s):
    if not s: return None
    s = str(s).strip()
    s = re.sub(r'(\d)\s*-\s*', r'\1-', s); s = re.sub(r'-\s*(\d)', r'-\1', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    m = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try: return datetime(y, mo, d).strftime('%Y-%m-%d')
        except: return None
    m = re.match(r'^(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})$', s)
    if m:
        d = int(m.group(1)); mo = MONTHS.get(m.group(2)[:3].lower()); y = int(m.group(3))
        if mo:
            try: return datetime(y, mo, d).strftime('%Y-%m-%d')
            except: return None
    return None

def is_event(t):
    tl = t.lower()
    return any(k in tl for k in EVENT_KW)

def _clean(t):
    t = PART_RE.sub('', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' -–+:')
    return t

def parse_pdf(file_bytes, force_subject=None):
    """Returns list of dicts: subject, date(YYYY-MM-DD), day, time, type, chapter, part."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed")
    out = []
    cur_subject = force_subject; cur_chapter = ''
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            if not force_subject:
                sub = detect_subject(page.extract_text() or '')
                if sub and sub != cur_subject:
                    cur_subject = sub; cur_chapter = ''
            for table in (page.extract_tables() or []):
                for row in table:
                    if not row or len(row) < 3: continue
                    date_s = (row[0] or '').strip()
                    day = (row[1] or '').strip() if len(row) > 1 else ''
                    topic = (row[2] or '').strip() if len(row) > 2 else ''
                    time = (row[3] or '').strip() if len(row) > 3 else ''
                    if not topic or topic.lower() == 'topics' or date_s.lower() == 'date':
                        continue
                    date = parse_date(date_s)
                    if not date or not cur_subject:
                        continue
                    time = re.sub(r':\s*(am|pm)', r' \1', time, flags=re.I).replace('::', ':').strip()
                    if is_event(topic):
                        out.append({'subject': cur_subject, 'date': date, 'day': day, 'time': time,
                                    'type': 'event', 'chapter': topic, 'part': None})
                    else:
                        pm = PART_RE.search(topic)
                        if pm:
                            pnum = int(pm.group(1)); name = _clean(topic)
                            if pnum == 1 or CHAP_RE.search(topic):
                                cur_chapter = name
                            chapter = cur_chapter or name
                            part = f'Part {pnum}' + (f' - {name}' if name and name != chapter else '')
                        else:
                            chapter = _clean(topic); part = None; cur_chapter = chapter
                        out.append({'subject': cur_subject, 'date': date, 'day': day, 'time': time,
                                    'type': 'chapter', 'chapter': chapter, 'part': part})
    return out
