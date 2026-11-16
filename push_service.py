"""
Web Push sender (VAPID). Phone/browser par notification bhejta hai — Chrome, Edge,
Firefox, aur installed-PWA Safari (iOS 16.4+). Agar VAPID keys env me set nahi hain
to sab kuch gracefully no-op (kuch nahi tootta).

Env vars (Railway):
  VAPID_PUBLIC_KEY   — base64url public key (frontend ko bhi jaata hai)
  VAPID_PRIVATE_KEY  — base64url raw private key
  VAPID_SUBJECT      — "mailto:you@domain" (optional, default mailto:support@mvsfoundation.in)

Sending background thread me hota hai taaki bade broadcasts request ko block na karein.
"""
import os
import json
import threading

_PUB = (os.getenv("VAPID_PUBLIC_KEY") or "").strip()
_PRIV = (os.getenv("VAPID_PRIVATE_KEY") or "").strip()
_SUBJ = (os.getenv("VAPID_SUBJECT") or "mailto:support@mvsfoundation.in").strip()


def enabled():
    return bool(_PUB and _PRIV)


def public_key():
    return _PUB


def _clean(s, n=4000):
    s = "" if s is None else str(s)
    return s[:n]


def _send_one(endpoint, p256dh, auth, payload_json):
    """Returns True (ok), 'dead' (gone -> prune), or False (transient)."""
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


def _worker(user_ids, payload):
    if not enabled():
        return
    try:
        from database import SessionLocal
        from push_models import PushSubscription
    except Exception:
        return
    payload_json = json.dumps(payload)
    db = SessionLocal()
    try:
        subs = (db.query(PushSubscription)
                .filter(PushSubscription.user_id.in_(list(user_ids))).all())
        if not subs:
            return
        dead = []
        try:
            from concurrent.futures import ThreadPoolExecutor
            jobs = [(s.id, s.endpoint, s.p256dh, s.auth) for s in subs]

            def _run(job):
                sid, ep, pk, au = job
                r = _send_one(ep, pk, au, payload_json)
                return (sid, r)

            with ThreadPoolExecutor(max_workers=16) as ex:
                for sid, r in ex.map(_run, jobs):
                    if r == "dead":
                        dead.append(sid)
        except Exception:
            # fallback: sequential
            for s in subs:
                r = _send_one(s.endpoint, s.p256dh, s.auth, payload_json)
                if r == "dead":
                    dead.append(s.id)
        if dead:
            try:
                db.query(PushSubscription).filter(
                    PushSubscription.id.in_(dead)).delete(synchronize_session=False)
                db.commit()
            except Exception:
                db.rollback()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def push_many(user_ids, title, body, url="/", tag=None):
    """Best-effort phone push to many users — non-blocking (background thread)."""
    try:
        ids = list({int(u) for u in (user_ids or []) if u})
    except Exception:
        ids = [u for u in (user_ids or []) if u]
    if not enabled() or not ids:
        return
    payload = {"title": _clean(title, 160) or "MVS Foundation",
               "body": _clean(body, 480), "url": url or "/"}
    if tag:
        payload["tag"] = str(tag)[:80]
    try:
        threading.Thread(target=_worker, args=(ids, payload), daemon=True).start()
    except Exception:
        pass


def push_one(user_id, title, body, url="/", tag=None):
    if user_id:
        push_many([user_id], title, body, url=url, tag=tag)
