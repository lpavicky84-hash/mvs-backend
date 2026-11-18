"""
Web Push sender (VAPID) — ab EVERY notification ke saath phone push jaata hai.

Architecture: ek persistent background worker + queue. notify() sirf enqueue karta
hai (micro-seconds, request block nahi hoti). Worker short window me saare jobs
drain karke payload-wise COALESCE karta hai — isliye 9798-student broadcast bhi
1 DB query + N sends ban jaata hai (thread/session explosion nahi).

VAPID keys env me na ho to sab gracefully no-op.
  VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT
"""
import os
import json
import time
import hashlib
import threading
import re as _re_px

# Push notification title/body ko bhi emoji-free rakho (★ ₹ Hindi preserve).
_PX_EMOJI_RE = _re_px.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F0FF\U00002600-\U00002604"
    "\U00002607-\U000026FF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0000200D\U000020E3\U00002139]",
    _re_px.UNICODE)


def _px_clean(s):
    if not s:
        return s
    try:
        t = _PX_EMOJI_RE.sub("", str(s))
        t = _re_px.sub(r"[ \t]{2,}", " ", t)
        return t.strip()
    except Exception:
        return s

try:
    import queue as _queue
except Exception:
    _queue = None

_PUB = (os.getenv("VAPID_PUBLIC_KEY") or "").strip()
_PRIV = (os.getenv("VAPID_PRIVATE_KEY") or "").strip()
_SUBJ = (os.getenv("VAPID_SUBJECT") or "mailto:support@mvsfoundation.in").strip()

_Q = _queue.Queue(maxsize=200000) if _queue else None
_worker_lock = threading.Lock()
_worker_started = False

# ===== DEDUP: ek hi notification (same user + same title/body/url) ko baar-baar push na karo.
# Bug tha: task-sync/retry ki wajah se purana notification phone par repeat ho raha tha.
# Window ke andar same payload dobara aaye to us user ke liye skip. =====
_RECENT = {}
_RECENT_LOCK = threading.Lock()
_RECENT_TTL = 12 * 3600          # 12 ghante: same content dobara push nahi
_RECENT_MAX = 120000


def _dedup_filter(ids, pj):
    """Return sirf un user_ids ko jinhe ye EXACT payload pichle _RECENT_TTL me nahi gaya."""
    now = time.time()
    h = hashlib.md5(pj.encode("utf-8")).hexdigest()
    out = []
    try:
        with _RECENT_LOCK:
            if len(_RECENT) > _RECENT_MAX:
                for k, t in list(_RECENT.items()):
                    if now - t > _RECENT_TTL:
                        _RECENT.pop(k, None)
            for uid in ids:
                key = str(uid) + "|" + h
                if now - _RECENT.get(key, 0) > _RECENT_TTL:
                    _RECENT[key] = now
                    out.append(uid)
    except Exception:
        return ids
    return out


def enabled():
    return bool(_PUB and _PRIV and _Q is not None)


def public_key():
    return _PUB


def _ids(user_ids):
    seq = user_ids if isinstance(user_ids, (list, tuple, set)) else [user_ids]
    out = []
    for u in seq:
        try:
            if u:
                out.append(int(u))
        except Exception:
            pass
    return out


# ===== Role -> portal path mapping (har notification SAHI app me khule) =====
# Bug tha: har push ka url "/" (root) jaata tha -> root student Class Manager login hai,
# isliye teacher/editor/etc ke notification click karte hi STUDENT app khul jaata tha. Ab
# recipient ke role se portal nikaalke payload me `portal` (+ scoped url) bhejte hain, aur
# service worker us portal ki hi window focus/open karta hai.
_ROLE_PORTAL = {
    "admin": "admin", "teacher": "teacher", "student": "student",
    "production_manager": "production", "editor": "editor",
    "youtuber": "youtuber", "graphics": "graphics",
}


def portal_for_role(role):
    r = getattr(role, "value", role)
    return _ROLE_PORTAL.get(str(r or "").strip(), "")


def portal_for_user(db, user_id):
    """Recipient user ka portal (teacher/student/editor/...) uske role se. Fail -> ''."""
    try:
        from models import User
        row = db.query(User.role).filter(User.id == int(user_id)).first()
        return portal_for_role(row[0]) if row else ""
    except Exception:
        return ""


def deeplink_url(portal, notif_type, ref=None):
    """Notification tap -> SAHI section ka URL. IMPORTANT: student portal sirf ROOT '/' par serve
    hoti hai (koi '/student' route nahi -> warna 404 'Not Found' aata tha). Baaki sab portals ke
    apne path hain (/teacher, /admin, /editor, ...). Query ?nt=<type>&nr=<ref> frontend deep-link ke liye."""
    p = (portal or "").strip()
    base = "" if p == "student" else p          # student -> root; baaki -> /<portal>
    path = ("/" + base) if base else "/"
    if p and notif_type:
        q = "nt=" + str(notif_type)
        if ref not in (None, ""):
            q += "&nr=" + str(ref)
        path = path + ("&" if "?" in path else "?") + q
    return path


def enqueue(user_ids, title, body, url="/", tag=None, portal=""):
    """Notification ke saath phone push queue me daalo (best-effort, non-blocking)."""
    if not enabled():
        return
    ids = _ids(user_ids)
    if not ids:
        return
    payload = {"title": (_px_clean(str(title or "MVS Foundation")) or "MVS Foundation")[:160],
               "body": (_px_clean(str(body or "")) or "")[:480], "url": url or "/"}
    if portal:
        payload["portal"] = str(portal)[:20]
    if tag:
        payload["tag"] = str(tag)[:80]
    pj = json.dumps(payload, sort_keys=True)
    ids = _dedup_filter(ids, pj)          # same notification dobara push na ho
    if not ids:
        return
    try:
        _Q.put_nowait((ids, pj))
        _ensure_worker()
    except Exception:
        pass


# backward-compatible helpers
def push_many(user_ids, title, body, url="/", tag=None):
    enqueue(user_ids, title, body, url=url, tag=tag)


def push_one(user_id, title, body, url="/", tag=None):
    if user_id:
        enqueue([user_id], title, body, url=url, tag=tag)


def _ensure_worker():
    global _worker_started
    if _worker_started or not enabled():
        return
    with _worker_lock:
        if _worker_started:
            return
        try:
            threading.Thread(target=_worker_loop, daemon=True).start()
            _worker_started = True
        except Exception:
            pass


def _worker_loop():
    while True:
        try:
            first = _Q.get()
            batch = [first]
            deadline = time.time() + 0.5     # chhoti window me coalesce
            while len(batch) < 20000:
                rem = deadline - time.time()
                if rem <= 0:
                    break
                try:
                    batch.append(_Q.get(timeout=rem))
                except Exception:
                    break
            _process(batch)
        except Exception:
            time.sleep(1)


def _role_portal(role):
    """UserRole -> jis app (portal) pe notification khulna chahiye."""
    r = (getattr(role, "value", None) or str(role or "")).strip().lower()
    if r == "production_manager":
        return "production"
    if r in ("teacher", "student", "admin", "editor", "graphics", "youtuber"):
        return r
    return ""


def _payload_with_portal(pj, portal):
    """Payload me recipient ka portal daalo aur url ko us app ke path par set karo. Isse SW ko
    pata chal jaata hai ki kaunsa app kholna hai -> teacher ka notification student app pe nahi
    khulega. Original deep-link (task id) ?n= me rakho (app chahe to use kare)."""
    if not portal:
        return pj
    try:
        import urllib.parse as _up
        d = json.loads(pj)
        link = d.get("url") or ""
        base = "/" + portal
        if link and str(link) not in ("/", ""):
            d["url"] = base + "?n=" + _up.quote(str(link), safe="")
        else:
            d["url"] = base
        d["portal"] = portal
        return json.dumps(d, sort_keys=True)
    except Exception:
        return pj


def _process(batch):
    # payload-wise user_ids coalesce
    groups = {}
    for ids, pj in batch:
        s = groups.get(pj)
        if s is None:
            s = set(); groups[pj] = s
        s.update(ids)
    try:
        from database import SessionLocal
        from push_models import PushSubscription
    except Exception:
        return
    db = SessionLocal()
    try:
        for pj, uidset in groups.items():
            uids = list(uidset)
            # user_id -> portal (role-based): notification ko SAHI app pe kholne ke liye.
            role_map = {}
            try:
                from models import User
                for i in range(0, len(uids), 900):
                    for uid, role in db.query(User.id, User.role).filter(
                            User.id.in_(uids[i:i + 900])):
                        role_map[uid] = _role_portal(role)
            except Exception:
                try: db.rollback()
                except Exception: pass
            # subscriptions ko PORTAL ke hisaab se group karo (har portal ka alag payload+url).
            by_portal = {}
            for i in range(0, len(uids), 900):
                chunk = uids[i:i + 900]
                try:
                    for s in db.query(PushSubscription).filter(
                            PushSubscription.user_id.in_(chunk)).all():
                        p = role_map.get(s.user_id, "")
                        by_portal.setdefault(p, []).append((s.id, s.endpoint, s.p256dh, s.auth))
                except Exception:
                    try: db.rollback()
                    except Exception: pass
            for p, rows in by_portal.items():
                if not rows:
                    continue
                dead = _send_group(rows, _payload_with_portal(pj, p))
                for i in range(0, len(dead), 900):
                    try:
                        db.query(PushSubscription).filter(
                            PushSubscription.id.in_(dead[i:i + 900])).delete(synchronize_session=False)
                        db.commit()
                    except Exception:
                        try: db.rollback()
                        except Exception: pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _send_group(rows, payload_json):
    """rows = [(id, endpoint, p256dh, auth), ...] (plain tuples, thread-safe). Returns dead ids."""
    dead = []

    def _run(r):
        return (r[0], _send_one(r[1], r[2], r[3], payload_json))

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=24) as ex:
            for sid, res in ex.map(_run, rows):
                if res == "dead":
                    dead.append(sid)
    except Exception:
        for r in rows:
            if _send_one(r[1], r[2], r[3], payload_json) == "dead":
                dead.append(r[0])
    return dead


def _send_one(endpoint, p256dh, auth, payload_json):
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=payload_json,
            vapid_private_key=_PRIV,
            vapid_claims={"sub": _SUBJ},
            ttl=86400,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return "dead"
        return False
    except Exception:
        return False
