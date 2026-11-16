"""
MVS Services Chat API — student <-> admin, per purchased service.

Design goals (learned from the homework-chat slowness audit):
  * Incremental polling: GET ...?since=<id> returns ONLY newer messages, so a
    long thread is never re-sent or re-rendered on every poll.
  * Attachments served separately (view/download) -> load once, cache client-side.
  * Server never blocks on notify (best-effort).

Auto-enable: when a student's purchase is recorded (response == "yes"), a thread
is ensured + enabled. No admin approval needed. Admin can disable a specific
student's thread (then that student can't send, but history stays).
Additive only — no existing table/route touched.
"""
import time as _time
from datetime import datetime as _dt
from fastapi import (APIRouter, Depends, HTTPException, Form, UploadFile, File)
from sqlalchemy.orm import Session

from database import get_db
from security import get_student, get_admin

router = APIRouter(tags=["ServicesChat"])

# in-memory typing signals: {thread_id: {"student": ts, "admin": ts}}
_SC_TYPING = {}
_ONLINE_WINDOW = 55   # seconds


# ------------------------------- helpers -------------------------------
def _R2():
    return __import__("r2_storage")


def _safe(name):
    return (str(name or "file")).replace(chr(34), "").replace("\n", " ")[:180]


def _sp(db, user):
    from student_routes import get_student_profile
    return get_student_profile(user, db)


def _thread_or_404(db, tid):
    from service_chat_models import ServiceChatThread
    t = db.query(ServiceChatThread).filter(ServiceChatThread.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Chat not found.")
    return t


def _ensure_thread(db, sid, prof, user, create=True):
    """Get (or create) the (service, student) thread. create=False -> may return None."""
    from service_chat_models import ServiceChatThread
    from models import MVSService
    t = db.query(ServiceChatThread).filter(
        ServiceChatThread.service_id == sid,
        ServiceChatThread.student_id == prof.id).first()
    if not t and create:
        svc = db.query(MVSService).filter(MVSService.id == sid).first()
        t = ServiceChatThread(
            service_id=sid, student_id=prof.id,
            student_user_id=getattr(user, "id", None),
            student_name=(getattr(user, "name", "") or getattr(prof, "name", "") or "Student"),
            student_phone=(getattr(prof, "phone", "") or ""),
            student_class=(getattr(prof, "class_name", "") or getattr(prof, "class_level", "") or ""),
            service_title=(svc.title if svc else ""), enabled=True)
        db.add(t)
        db.flush()
    elif t:
        # keep denormalized fields fresh (name/user can change)
        if getattr(user, "id", None):
            t.student_user_id = user.id
        if getattr(user, "name", ""):
            t.student_name = user.name
    return t


def ensure_thread_on_purchase(db, sid, prof, user):
    """Called from the purchase endpoint when response == 'yes'. Enables chat."""
    try:
        t = _ensure_thread(db, sid, prof, user, create=True)
        if t and not t.enabled:
            # a purchase re-enables (admin-disabled thread reopens on a fresh buy)
            t.enabled = True
        return t
    except Exception:
        return None


def _set_typing(tid, role):
    try:
        _SC_TYPING.setdefault(int(tid), {})[role] = _time.time()
    except Exception:
        pass


def _is_typing(tid, role):
    try:
        return (_time.time() - _SC_TYPING.get(int(tid), {}).get(role, 0)) < 5
    except Exception:
        return False


def _any_admin_online(db):
    """True if any admin reported active recently (student sees 'MVS Support online')."""
    from models import UserSession, User, UserRole
    try:
        rows = db.query(UserSession).filter(UserSession.role == "admin").order_by(
            UserSession.last_seen.desc()).limit(8).all()
        now = _dt.now()
        for s in rows:
            seen = s.last_active or s.last_seen
            if seen and (now - seen).total_seconds() < _ONLINE_WINDOW and not s.ended_at:
                return True
    except Exception:
        pass
    return False


def _student_online(db, uid):
    from models import UserSession
    if not uid:
        return (False, None)
    try:
        s = db.query(UserSession).filter(UserSession.user_id == uid).order_by(
            UserSession.last_seen.desc()).first()
        seen = (s.last_active or s.last_seen) if s else None
    except Exception:
        seen = None
    online = bool(seen and (_dt.now() - seen).total_seconds() < _ONLINE_WINDOW)
    return (online, (seen.strftime("%d %b, %I:%M %p") if seen else None))


def _att_dict(a):
    return {"id": a.id, "filename": a.filename or "file", "mime": a.mime or "",
            "kind": a.kind or "file", "is_image": (a.mime or "").startswith("image/")}


def _msg_dict(db, m, atts_by_msg):
    return {"id": m.id, "sender_role": m.sender_role or "", "sender_name": m.sender_name or "",
            "message": m.message or "", "at": _ist(m.created_at),
            "attachments": [_att_dict(a) for a in atts_by_msg.get(m.id, [])]}


def _ist(dt):
    try:
        from models import ist_iso
        return ist_iso(dt)
    except Exception:
        return str(dt or "")[:19]


def _collect_msgs(db, tid, since=0):
    """Return (messages[], max_id) for thread, id > since, oldest-first."""
    from service_chat_models import ServiceChatMsg, ServiceChatAttachment
    q = db.query(ServiceChatMsg).filter(ServiceChatMsg.thread_id == tid)
    if since:
        q = q.filter(ServiceChatMsg.id > since)
    msgs = q.order_by(ServiceChatMsg.id.asc()).all()
    if not msgs:
        return [], since
    ids = [m.id for m in msgs if m.has_att]
    atts_by_msg = {}
    if ids:
        for a in db.query(ServiceChatAttachment).filter(
                ServiceChatAttachment.message_id.in_(ids)).all():
            atts_by_msg.setdefault(a.message_id, []).append(a)
    out = [_msg_dict(db, m, atts_by_msg) for m in msgs]
    return out, max(m.id for m in msgs)


async def _post_msg(db, t, sender_role, sender_name, message, files):
    from service_chat_models import ServiceChatMsg, ServiceChatAttachment
    text = (message or "").strip() or None
    msg = ServiceChatMsg(thread_id=t.id, sender_role=sender_role,
                         sender_name=(sender_name or "")[:160], message=text)
    db.add(msg)
    db.flush()
    R2 = _R2()
    n_att = 0
    preview = text or ""
    for f in (files or []):
        if not f:
            continue
        raw = await f.read()
        if not raw:
            continue
        ct = getattr(f, "content_type", None) or "application/octet-stream"
        ref = R2.store_file_value(R2.new_key("svc-chat", f.filename or "file"), raw, ct)
        db.add(ServiceChatAttachment(
            thread_id=t.id, message_id=msg.id,
            kind=("image" if ct.startswith("image/") else "file"),
            url=ref, filename=(f.filename or "file"), mime=ct, size=len(raw)))
        n_att += 1
        if not preview:
            preview = ("Photo" if ct.startswith("image/") else "File")
    if n_att:
        msg.has_att = True
    # thread rollups (admin-list preview + sort)
    t.last_msg_at = _dt.utcnow()
    t.last_msg_text = (preview or "")[:240]
    t.last_sender = sender_role
    if sender_role == "admin":
        t.admin_last_read_id = msg.id   # admin's own message is read by admin
    else:
        t.student_last_read_id = msg.id
    db.commit()
    return msg


def _serve_att(db, aid, guard_thread_ids=None):
    from service_chat_models import ServiceChatAttachment
    a = db.query(ServiceChatAttachment).filter(ServiceChatAttachment.id == aid).first()
    if not a:
        raise HTTPException(status_code=404, detail="Not found.")
    if guard_thread_ids is not None and a.thread_id not in guard_thread_ids:
        raise HTTPException(status_code=404, detail="Not found.")
    return a


def _att_response(a, inline):
    return _R2().proxy_response(a.url, a.mime or "application/octet-stream",
                               _safe(a.filename or "file"), (not inline), sniff=True)


# ============================= STUDENT =============================
def _student_status(db, t):
    typing = _is_typing(t.id, "admin")
    online = _any_admin_online(db)
    return {"other_name": "MVS Support", "other_online": online, "other_typing": typing,
            "enabled": bool(t.enabled)}


@router.get("/api/student/service-chat/{sid}")
def student_service_chat(sid: int, since: int = 0,
                         db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Get the chat for a purchased service. since>0 -> only newer messages (poll)."""
    prof = _sp(db, current_user)
    if not prof:
        raise HTTPException(status_code=404, detail="Not found.")
    t = _ensure_thread(db, sid, prof, current_user, create=False)
    if not t:
        return {"exists": False, "enabled": False, "messages": [], "last_id": 0}
    msgs, maxid = _collect_msgs(db, t.id, since=since)
    # mark admin messages as read + presence
    if maxid and maxid > (t.student_last_read_id or 0):
        t.student_last_read_id = maxid
    t.student_last_seen = _dt.utcnow()
    db.commit()
    return {"exists": True, "enabled": bool(t.enabled), "thread_id": t.id,
            "title": t.service_title or "", "messages": msgs,
            "last_id": maxid, "status": _student_status(db, t)}


@router.post("/api/student/service-chat/{sid}/typing")
def student_service_typing(sid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    prof = _sp(db, current_user)
    t = _ensure_thread(db, sid, prof, current_user, create=False) if prof else None
    if t:
        _set_typing(t.id, "student")
    return {"ok": True}


@router.post("/api/student/service-chat/{sid}/messages")
async def student_service_send(sid: int, message: str = Form(""),
                               files: list[UploadFile] = File(default=[]),
                               db: Session = Depends(get_db), current_user=Depends(get_student)):
    prof = _sp(db, current_user)
    if not prof:
        raise HTTPException(status_code=404, detail="Not found.")
    t = _ensure_thread(db, sid, prof, current_user, create=False)
    if not t:
        raise HTTPException(status_code=403, detail="Chat is available after you purchase this service.")
    if not t.enabled:
        raise HTTPException(status_code=403, detail="This chat has been closed by the admin.")
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_msg(db, t, "student", (current_user.name or "Student"), message, files)
    # notify admins (best-effort, non-blocking to the response path)
    try:
        from admin_routes import notify_section_admins
        notify_section_admins(db, "services", "New Services Chat message",
                              '%s sent a message about "%s".' % (current_user.name, t.service_title or "a service"),
                              "service_chat")
        db.commit()
    except Exception:
        try: db.rollback()
        except Exception: pass
    one, _m = _collect_msgs(db, t.id, since=msg.id - 1)
    return {"ok": True, "message": (one[-1] if one else None), "last_id": msg.id}


@router.get("/api/student/service-chat-attachments/{aid}/view")
def student_att_view(aid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    prof = _sp(db, current_user)
    from service_chat_models import ServiceChatThread
    tids = [x.id for x in db.query(ServiceChatThread.id).filter(
        ServiceChatThread.student_id == (prof.id if prof else -1)).all()]
    return _att_response(_serve_att(db, aid, guard_thread_ids=set(tids)), inline=True)


@router.get("/api/student/service-chat-attachments/{aid}/download")
def student_att_dl(aid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    prof = _sp(db, current_user)
    from service_chat_models import ServiceChatThread
    tids = [x.id for x in db.query(ServiceChatThread.id).filter(
        ServiceChatThread.student_id == (prof.id if prof else -1)).all()]
    return _att_response(_serve_att(db, aid, guard_thread_ids=set(tids)), inline=False)


# ============================== ADMIN ==============================
@router.get("/api/admin/service-chats")
def admin_service_chats(db: Session = Depends(get_db), _=Depends(get_admin)):
    """WhatsApp-style list: every thread with a last message, recent first."""
    from service_chat_models import ServiceChatThread, ServiceChatMsg
    threads = db.query(ServiceChatThread).order_by(
        ServiceChatThread.last_msg_at.desc().nullslast(),
        ServiceChatThread.id.desc()).all()
    out = []
    for t in threads:
        unread = db.query(ServiceChatMsg).filter(
            ServiceChatMsg.thread_id == t.id,
            ServiceChatMsg.sender_role == "student",
            ServiceChatMsg.id > (t.admin_last_read_id or 0)).count()
        online, _last = _student_online(db, t.student_user_id)
        out.append({
            "thread_id": t.id, "service_id": t.service_id,
            "student_id": t.student_id, "student_name": t.student_name or "Student",
            "student_phone": t.student_phone or "", "student_class": t.student_class or "",
            "service_title": t.service_title or "", "enabled": bool(t.enabled),
            "last_text": t.last_msg_text or "", "last_sender": t.last_sender or "",
            "last_at": _ist(t.last_msg_at), "unread": unread, "online": online})
    total_unread = sum(x["unread"] for x in out)
    return {"chats": out, "total_unread": total_unread}


def _admin_status(db, t):
    online, last = _student_online(db, t.student_user_id)
    return {"other_name": t.student_name or "Student", "other_online": online,
            "other_last_seen": last, "other_typing": _is_typing(t.id, "student"),
            "enabled": bool(t.enabled)}


@router.get("/api/admin/service-chat/{tid}")
def admin_service_chat(tid: int, since: int = 0,
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    t = _thread_or_404(db, tid)
    msgs, maxid = _collect_msgs(db, t.id, since=since)
    if maxid and maxid > (t.admin_last_read_id or 0):
        t.admin_last_read_id = maxid
        db.commit()
    return {"thread_id": t.id, "enabled": bool(t.enabled),
            "student_name": t.student_name or "Student",
            "student_phone": t.student_phone or "", "student_class": t.student_class or "",
            "service_title": t.service_title or "", "messages": msgs,
            "last_id": maxid, "status": _admin_status(db, t)}


@router.post("/api/admin/service-chat/{tid}/typing")
def admin_service_typing(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    _set_typing(tid, "admin")
    return {"ok": True}


@router.post("/api/admin/service-chat/{tid}/messages")
async def admin_service_send(tid: int, message: str = Form(""),
                             files: list[UploadFile] = File(default=[]),
                             db: Session = Depends(get_db), current_user=Depends(get_admin)):
    t = _thread_or_404(db, tid)
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_msg(db, t, "admin", (getattr(current_user, "name", "") or "MVS Support"), message, files)
    # notify the student
    try:
        from admin_routes import notify
        if t.student_user_id:
            notify(db, t.student_user_id, "New message from MVS Support",
                   'You have a reply about "%s".' % (t.service_title or "your service"),
                   "service_chat")
            db.commit()
    except Exception:
        try: db.rollback()
        except Exception: pass
    one, _m = _collect_msgs(db, t.id, since=msg.id - 1)
    return {"ok": True, "message": (one[-1] if one else None), "last_id": msg.id}


@router.post("/api/admin/service-chat/{tid}/toggle")
def admin_service_toggle(tid: int, payload: dict = None,
                         db: Session = Depends(get_db), _=Depends(get_admin)):
    t = _thread_or_404(db, tid)
    want = None
    if isinstance(payload, dict) and "enabled" in payload:
        want = bool(payload.get("enabled"))
    t.enabled = (not t.enabled) if want is None else want
    db.commit()
    return {"ok": True, "enabled": bool(t.enabled)}


@router.get("/api/admin/service-chat-attachments/{aid}/view")
def admin_att_view(aid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    return _att_response(_serve_att(db, aid), inline=True)


@router.get("/api/admin/service-chat-attachments/{aid}/download")
def admin_att_dl(aid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    return _att_response(_serve_att(db, aid), inline=False)
