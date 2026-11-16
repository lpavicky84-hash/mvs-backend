"""
Web Push subscription API — koi bhi logged-in user (student/teacher/admin) apna
browser/phone subscribe kar sakta hai. Additive, isolated router.
"""
from fastapi import APIRouter, Depends, Body
from sqlalchemy.orm import Session

from database import get_db
from security import get_current_user
import push_service

router = APIRouter(prefix="/api/push", tags=["Push"])


@router.get("/vapid-public-key")
def vapid_public_key():
    return {"enabled": push_service.enabled(), "key": push_service.public_key()}


@router.post("/subscribe")
def push_subscribe(payload: dict = Body(...),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    from push_models import PushSubscription
    sub = payload.get("subscription") or payload
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return {"ok": False, "detail": "Invalid subscription"}
    ua = (payload.get("ua") or "")[:255]
    # same endpoint pe purani entry hata do (dedupe) — fir is user ke liye add
    try:
        db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete(
            synchronize_session=False)
    except Exception:
        pass
    db.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth, ua=ua))
    db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
def push_unsubscribe(payload: dict = Body(default={}),
                     db: Session = Depends(get_db), user=Depends(get_current_user)):
    from push_models import PushSubscription
    endpoint = ((payload or {}).get("endpoint") or "").strip()
    q = db.query(PushSubscription).filter(PushSubscription.user_id == user.id)
    if endpoint:
        q = q.filter(PushSubscription.endpoint == endpoint)
    try:
        q.delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    return {"ok": True}


@router.post("/test")
def push_test(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Khud ko ek test push bhej ke dekho ki phone par aata hai ya nahi."""
    push_service.push_one(user.id, "MVS Foundation",
                          "Test notification — push working! You will now get alerts on your phone.",
                          url="/")
    return {"ok": True, "enabled": push_service.enabled()}
