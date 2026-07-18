"""
PDF TIMETABLE PARSER — bulletproof edition
==========================================
Teacher kaise bhi PDF de, ye parser khud samajh leta hai:

DATES   : 2026-06-02 | 02-06-2026 | 2/6/2026 | 02.06.26 | 2 June 2026 |
          June 2, 2026 | 2nd Jun 26 | 02 JUNE 2026 ... (sab)
TIMES   : 20:00:00 | 09:00 | 9:00 AM | 9AM | 9.30 pm | 5:30PM | 17:00 hrs |
          9:00 - 10:30 AM (range -> start) | 9 to 10 am ... (sab)
DAYS    : PDF me jo bhi likha ho — DAY HAMESHA DATE SE CALCULATE hota hai
          (galat/missing day apne aap sahi ho jata hai)
COLUMNS : kisi bhi order me hon (S.No | Day | Date | Topic | Time ...) —
          har row me date/time/day cell khud pehchana jata hai
TABLES  : table na mile to text lines se bhi parse hota hai
"""
import io
import re
from datetime import datetime

try:
    import pdfplumber
except Exception:
    pdfplumber = None

SUBJECT_PATTERNS = [
    ('Physics', 'PHYSICS'), ('Chemistry', 'CHEMISTRY'), ('Mathematics', 'MATHEMATIC'),
    ('Biology', 'BIOLOGY'), ('English', 'ENGLISH'), ('Home Science', 'HOME SCIENCE'),
    ('Data Entry Operations', 'DATA ENTRY'), ('Hindi', 'HINDI'), ('Computer Science', 'COMPUTER'),
    ('Psychology', 'PSYCHOLOG'), ('Painting', 'PAINTING'), ('Physical Education', 'PHYSICAL EDUCATION'),
    ('Accountancy', 'ACCOUNT'), ('Economics', 'ECONOMIC'), ('Business Studies', 'BUSINESS'),
    ('Sociology', 'SOCIOLOG'), ('Political Science', 'POLITICAL'), ('Geography', 'GEOGRAPH'),
    ('History', 'HISTORY'), ('Sanskrit', 'SANSKRIT'), ('Science and Technology', 'SCIENCE AND TECH'),
    ('Social Science', 'SOCIAL SCIENCE'),
]
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DAY_WORDS = {d.lower(): d for d in DAYS}
DAY_WORDS.update({d[:3].lower(): d for d in DAYS})
DAY_WORDS.update({'tues': 'Tuesday', 'thur': 'Thursday', 'thurs': 'Thursday', 'weds': 'Wednesday'})

EVENT_KW = ['doubt', 'dpp', 'test series', 'mission 75', 'mission75', 'pyq', 'mock test',
            'subjective test', 'answer writing', 'target 75', 'note making',
            'strategy just before', 'revision', 'holiday', 'break']
PART_RE = re.compile(r'\(\s*(?:part|p)\s*[-–]?\s*(\d+)\s*\)|\bpart\s*[-–]?\s*(\d+)\b', re.I)
CHAP_RE = re.compile(r'(chapter|chap|ch|lesson|l)\s*[-.\s]?\s*\d+', re.I)
ORD_RE = re.compile(r'(\d)(st|nd|rd|th)\b', re.I)


# ------------------------------------------------------------------ helpers
def detect_subject(text):
    """Jo subject-name text me SABSE PEHLE aata hai wahi jeeta hai.
    (Title hamesha upar hota hai — 'Social Science' ke topics me 'Economics'
    aa jaye to bhi title wala hi chunega.) Header 400 chars ko priority."""
    up = (text or '').upper()

    def earliest(chunk):
        best, best_i = None, 10**9
        for name, pat in SUBJECT_PATTERNS:
            i = chunk.find(pat)
            if i != -1 and i < best_i:
                best, best_i = name, i
        return best

    return earliest(up[:400]) or earliest(up)


def _mk_date(y, mo, d):
    if y < 100:
        y += 2000
    try:
        return datetime(y, mo, d)
    except Exception:
        return None


def parse_date(s):
    """Har format ki date -> datetime (ya None)."""
    if not s:
        return None
    s = ORD_RE.sub(r'\1', str(s))                       # 2nd -> 2
    s = re.sub(r'[,]', ' ', s)
    s = re.sub(r'(\d)\s*([-/.])\s*', r'\1\2', s)
    s = re.sub(r'([-/.])\s*(\d)', r'\1\2', s)
    s = re.sub(r'\s{2,}', ' ', s).strip()
    if not s:
        return None
    # YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    m = re.search(r'\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b', s)
    if m:
        return _mk_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # DD-MM-YYYY / DD/MM/YY / DD.MM.YYYY
    m = re.search(r'\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b', s)
    if m:
        return _mk_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # 2 June 2026 / 02 Jun 26
    m = re.search(r'\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{2,4})\b', s)
    if m:
        mo = MONTHS.get(m.group(2)[:3].lower())
        if mo:
            return _mk_date(int(m.group(3)), mo, int(m.group(1)))
    # June 2 2026 / Jun 2, 26
    m = re.search(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\s+(\d{2,4})\b', s)
    if m:
        mo = MONTHS.get(m.group(1)[:3].lower())
        if mo:
            return _mk_date(int(m.group(3)), mo, int(m.group(2)))
    # 2 June (bina saal) -> current/agla saal assume
    m = re.search(r'\b(\d{1,2})\s+([A-Za-z]{3,9})\b', s)
    if m:
        mo = MONTHS.get(m.group(2)[:3].lower())
        if mo:
            today = datetime.now()
            dt = _mk_date(today.year, mo, int(m.group(1)))
            if dt and (dt - today).days < -90:          # bahut peeche -> agla saal
                dt = _mk_date(today.year + 1, mo, int(m.group(1)))
            return dt
    return None


def parse_time(s):
    """Har format ka time -> 'h:mm am/pm' (ya '')."""
    if not s:
        return ''
    t = str(s).strip().lower()
    t = t.replace('hrs', '').replace('hr', '').replace('o\'clock', '')
    t = re.sub(r'\s+', ' ', t)
    # range: "9:00 - 10:30 am" / "9 to 10 am" -> start (meridian end se le lo)
    mr = re.split(r'\s*(?:-|–|to)\s*', t)
    tail_ap = ''
    if len(mr) > 1:
        ap = re.search(r'\b(am|pm|a\.m\.|p\.m\.)\b', mr[-1])
        if ap:
            tail_ap = ap.group(1).replace('.', '')
        t = mr[0].strip()
    t = t.replace('.', ':') if re.match(r'^\d{1,2}\.\d{2}', t) else t
    m = re.match(r'^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?$',
                 t.replace(' ', ''))
    if not m:
        m = re.match(r'^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(am|pm)?\b', t)
    if not m:
        return str(s).strip()
    h = int(m.group(1)); mi = m.group(2) or '00'
    ap = (m.group(3) or tail_ap or '').replace('.', '')
    if ap in ('am', 'pm'):
        if ap == 'pm' and h < 12:
            h += 12
        if ap == 'am' and h == 12:
            h = 0
    out_ap = 'am' if h < 12 else 'pm'
    h12 = h % 12 or 12
    return f"{h12}:{mi} {out_ap}"


def looks_like_time(s):
    t = str(s or '').strip().lower()
    if not t or len(t) > 22:
        return False
    return bool(re.search(r'\d{1,2}[:.]\d{2}', t) or re.search(r'\b\d{1,2}\s*(am|pm)\b', t))


def looks_like_day(s):
    t = re.sub(r'[^a-z]', '', str(s or '').lower())
    return t in DAY_WORDS


def is_event(t):
    tl = (t or '').lower()
    return any(k in tl for k in EVENT_KW)


def _clean(t):
    t = PART_RE.sub('', t or '')
    t = re.sub(r'\s{2,}', ' ', t).strip(' -–+:|,')
    return t


def _norm(t):
    return re.sub(r'[^a-z0-9]', '', (t or '').lower())


# ------------------------------------------------------------------ row engine
def classify_row(cells):
    """Kisi bhi column-order wali row -> (date_dt, time_str, topic).
       Day PDF se NAHI liya jata — hamesha date se nikalta hai."""
    cells = [re.sub(r'\s+', ' ', str(c).replace('\n', ' ')).strip()
             for c in cells if c is not None]
    cells = [c for c in cells if c != '']
    if not cells:
        return None
    date_dt, date_i = None, -1
    for i, c in enumerate(cells):
        d = parse_date(c)
        if d:
            date_dt, date_i = d, i
            break
    if not date_dt:
        return None
    rest = [c for i, c in enumerate(cells) if i != date_i]
    time_s, time_i = '', -1
    for i, c in enumerate(rest):
        if looks_like_time(c) and not parse_date(c):
            time_s, time_i = parse_time(c), i
            break
    rest = [c for i, c in enumerate(rest) if i != time_i]
    rest = [c for c in rest if not looks_like_day(c)]           # day cells drop
    rest = [c for c in rest if not re.fullmatch(r'\d{1,3}\.?', c)]   # S.No drop
    topic = max(rest, key=len).strip() if rest else ''
    if not topic:
        return None
    return date_dt, time_s, topic


def _emit(out, subject, date_dt, time_s, topic, state):
    day = DAYS[date_dt.weekday()]                     # day HAMESHA date se
    date = date_dt.strftime('%Y-%m-%d')
    # Merged time-cell fix: PDFs me time cell aksar kai rows me merge hota hai —
    # sirf pehli row me time hota hai, baaki khaali. Jis row me time nahi mila
    # wo pichhli row ka time inherit karti hai (subject-scope; naya time aate hi
    # update — isliye 10:00 wale block ke baad 9:30 wala block sahi rehta hai).
    if time_s:
        state['time'] = time_s
    else:
        time_s = state.get('time', '')
    if is_event(topic):
        out.append({'subject': subject, 'date': date, 'day': day, 'time': time_s,
                    'type': 'event', 'chapter': _clean(topic) or topic, 'part': None})
        return
    pm = PART_RE.search(topic)
    if pm:
        pnum = int(pm.group(1) or pm.group(2))
        name = _clean(topic)
        if pnum == 1 or CHAP_RE.search(topic):
            state['chapter'] = name
        chapter = state['chapter'] or name
        part = f'Part {pnum}' + (f' - {name}' if name and _norm(name) != _norm(chapter)
                                 and _norm(name) not in _norm(chapter) else '')
    else:
        name = _clean(topic)
        # naam current chapter ke andar hi hai -> usi chapter ki extra class
        if state['chapter'] and _norm(name) and _norm(name) in _norm(state['chapter']):
            chapter, part = state['chapter'], None
        else:
            chapter, part = name, None
            state['chapter'] = chapter
    out.append({'subject': subject, 'date': date, 'day': day, 'time': time_s,
                'type': 'chapter', 'chapter': chapter, 'part': part})


HEADER_WORDS = ('date', 'day', 'topic', 'topics', 'time', 'subject', 's.no', 'sno', 'sr')


def _is_header(cells):
    joined = ' '.join(str(c or '').lower() for c in cells)
    return sum(1 for w in HEADER_WORDS if w in joined) >= 2


# ------------------------------------------------------------------ main
def parse_pdf(file_bytes, force_subject=None):
    """Returns list of dicts: subject, date(YYYY-MM-DD), day, time, type, chapter, part."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber not installed")
    out = []
    cur_subject = force_subject
    state = {'chapter': ''}
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            if not force_subject:
                sub = detect_subject(text)
                if sub and sub != cur_subject:
                    cur_subject = sub
                    state = {'chapter': ''}
            subject = cur_subject
            if not subject:
                continue
            tables = page.extract_tables() or []
            got = 0
            for table in tables:
                for row in table:
                    if not row or _is_header(row):
                        continue
                    r = classify_row(row)
                    if r:
                        _emit(out, subject, r[0], r[1], r[2], state)
                        got += 1
            if got == 0:                                   # fallback: text lines
                for line in text.split('\n'):
                    if _is_header([line]):
                        continue
                    d = parse_date(line)
                    if not d:
                        continue
                    r = classify_row(re.split(r'\s{2,}|\t', line) if re.search(r'\s{2,}|\t', line)
                                     else [line])
                    if r:
                        _emit(out, subject, r[0], r[1], r[2], state)
                    else:
                        # date line ke saath jo bacha use topic maan lo
                        rest = line
                        for pat in (r'\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b',
                                    r'\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b'):
                            rest = re.sub(pat, ' ', rest)
                        tm = ''
                        mt = re.search(r'\d{1,2}[:.]\d{2}(?::\d{2})?\s*(?:am|pm)?', rest, re.I)
                        if mt:
                            tm = parse_time(mt.group(0))
                            rest = rest.replace(mt.group(0), ' ')
                        for w in list(DAY_WORDS.keys()):
                            rest = re.sub(r'\b' + w + r'\b', ' ', rest, flags=re.I)
                        rest = re.sub(r'\s{2,}', ' ', rest).strip(' -|')
                        if rest:
                            _emit(out, subject, d, tm, rest, state)
    return out
