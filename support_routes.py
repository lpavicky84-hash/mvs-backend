# ===========================================================================
# support_routes.py — Complaints & Resolution + Feedback & Ratings API
# Modular + additive. Reuses get_student/get_admin auth, the Notification model,
# r2_storage, and the admin section-guard. No prefix; per-endpoint guards.
# ===========================================================================
from fastapi import (APIRouter, Depends, HTTPException, Body, UploadFile,
                     File, Form, Request)
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from database import get_db
from security import get_student, get_admin
from models import User, UserRole, StudentProfile, Notification, ist_now
import support_models as SM

router = APIRouter(tags=["Support"])

_MAX_IMG = 8 * 1024 * 1024        # 8 MB per image
_MAX_VOICE = 12 * 1024 * 1024     # 12 MB per voice clip
_MAX_IMAGES = 10                  # per message, prevents flooding
_IMG_MIME = ("image/",)
_VOICE_MIME = ("audio/", "video/webm")


def _safe_name(name, fallback="file"):
    """Strip path separators and control chars from a client-supplied filename —
    it is only metadata (storage key is server-generated), but it is echoed back
    in Content-Disposition, so keep it clean."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if 32 <= ord(ch) < 127 and ch not in '"\r\n')
    name = name.strip() or fallback
    return name[:120]


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------
def admin_guard(request: Request, current_user=Depends(get_admin)):
    """Section-aware admin gate (reuses admin_routes' map). Full admins pass;
    restricted sub-admins need the section the path maps to."""
    try:
        from admin_routes import (ADMIN_SECTION_MAP, ADMIN_SECTION_ALIASES,
                                  admin_allowed_sections)
    except Exception:
        return current_user
    allowed = admin_allowed_sections(current_user)
    path = request.url.path
    if allowed is not None and path.startswith("/api/admin/"):
        parts = path.split("/")
        first = parts[3] if len(parts) > 3 else ""
        sec = ADMIN_SECTION_MAP.get(first)
        if sec is not None:
            acceptable = ADMIN_SECTION_ALIASES.get(sec, {sec})
            if allowed.isdisjoint(acceptable):
                raise HTTPException(status_code=403,
                                    detail="You do not have access to this section.")
    return current_user


def _sp(db, me):
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == me.id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student profile not found.")
    return sp


def _notify(db, user_id, title, message, ntype, link=None):
    if not user_id:
        return
    db.add(Notification(user_id=user_id, title=title, message=message,
                        notif_type=ntype, link=link))


def _admin_user_ids(db):
    return [u.id for u in db.query(User.id).filter(User.role == UserRole.admin).all()]


def _log(db, complaint_id, actor_id, actor_role, action, detail=None):
    try:
        db.add(SM.ComplaintEvent(complaint_id=complaint_id, actor_user_id=actor_id,
                                 actor_role=actor_role, action=action, detail=detail))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------
async def _save_attachments(db, complaint_id, message_id, uploader_id, images, voice):
    r2 = __import__("r2_storage")
    out = []
    for f in (images or [])[:_MAX_IMAGES]:
        if not f:
            continue
        ct = f.content_type or ""
        if not ct.startswith(_IMG_MIME):
            continue
        raw = await f.read()
        if not raw or len(raw) > _MAX_IMG:
            continue
        fn = _safe_name(f.filename, "image")
        ref = r2.store_file_value(r2.new_key("cmp-img", fn), raw, ct)
        a = SM.ComplaintAttachment(complaint_id=complaint_id, message_id=message_id,
                                   kind="image", url=ref, filename=fn,
                                   mime=ct, size=len(raw), uploader_user_id=uploader_id)
        db.add(a)
        out.append(a)
    if voice:
        ct = voice.content_type or "audio/webm"
        raw = await voice.read()
        if raw and len(raw) <= _MAX_VOICE and (ct.startswith("audio/") or ct in _VOICE_MIME):
            fn = _safe_name(voice.filename, "voice.webm")
            ref = r2.store_file_value(r2.new_key("cmp-voice", fn), raw, ct)
            db.add(SM.ComplaintAttachment(complaint_id=complaint_id, message_id=message_id,
                                          kind="voice", url=ref, filename=fn,
                                          mime=ct, size=len(raw), uploader_user_id=uploader_id))
    return out


def _att_dict(a):
    return {"id": a.id, "kind": a.kind or "image", "filename": a.filename or "",
            "mime": a.mime or "", "size": a.size or 0, "duration": a.duration or 0,
            "message_id": a.message_id}


def _serve_attachment(db, aid, is_admin, me, download):
    a = db.query(SM.ComplaintAttachment).filter(SM.ComplaintAttachment.id == aid).first()
    if not a:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    if not is_admin:
        c = db.query(SM.Complaint).filter(SM.Complaint.id == a.complaint_id).first()
        sp = _sp(db, me)
        if not c or c.student_id != sp.id:
            raise HTTPException(status_code=403, detail="Not your attachment.")
    r2 = __import__("r2_storage")
    return r2.file_response(a.url, a.mime or "application/octet-stream",
                            a.filename or "file", download)


# ---------------------------------------------------------------------------
# serializers
# ---------------------------------------------------------------------------
def _cat_name(db, cid, _cache={}):
    if not cid:
        return ""
    c = db.query(SM.ComplaintCategory).filter(SM.ComplaintCategory.id == cid).first()
    return c.name if c else ""


def _counts(db, complaint_id):
    imgs = db.query(func.count(SM.ComplaintAttachment.id)).filter(
        SM.ComplaintAttachment.complaint_id == complaint_id,
        SM.ComplaintAttachment.kind == "image").scalar() or 0
    voices = db.query(func.count(SM.ComplaintAttachment.id)).filter(
        SM.ComplaintAttachment.complaint_id == complaint_id,
        SM.ComplaintAttachment.kind == "voice").scalar() or 0
    return int(imgs), int(voices)


def _serialize_list(db, rows, with_student=False):
    """Batched serializer for complaint LISTS — one query each for attachment
    counts, students, users and categories (no per-row N+1)."""
    if not rows:
        return []
    ids = [c.id for c in rows]
    cnt = {}
    for cid, kind, n in db.query(
            SM.ComplaintAttachment.complaint_id, SM.ComplaintAttachment.kind,
            func.count(SM.ComplaintAttachment.id)).filter(
            SM.ComplaintAttachment.complaint_id.in_(ids)).group_by(
            SM.ComplaintAttachment.complaint_id, SM.ComplaintAttachment.kind).all():
        cnt.setdefault(cid, {})[kind] = int(n)
    cats = {c.id: c.name for c in db.query(SM.ComplaintCategory).all()}
    smap, umap = {}, {}
    if with_student:
        sids = list({c.student_id for c in rows if c.student_id})
        sps = db.query(StudentProfile).filter(StudentProfile.id.in_(sids)).all() if sids else []
        smap = {sp.id: sp for sp in sps}
        uids = {sp.user_id for sp in sps} | {c.resolved_by for c in rows if c.resolved_by}
        uids = [x for x in uids if x]
        umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    out = []
    for c in rows:
        cc = cnt.get(c.id, {})
        d = {"id": c.id, "complaint_number": c.complaint_number, "title": c.title,
             "description": c.description or "", "status": c.status,
             "priority": c.priority or "normal", "source": c.source or "portal",
             "category_id": c.category_id, "category": cats.get(c.category_id, ""),
             "created_at": str(c.created_at or "")[:19], "updated_at": str(c.updated_at or "")[:19],
             "resolved_at": str(c.resolved_at or "")[:19] or None,
             "assigned_to": c.assigned_to, "resolved_by": c.resolved_by,
             "read_by_student": bool(c.read_by_student), "read_by_admin": bool(c.read_by_admin),
             "image_count": cc.get("image", 0), "voice_count": cc.get("voice", 0)}
        if with_student:
            sp = smap.get(c.student_id)
            u = umap.get(sp.user_id) if sp else None
            d["student"] = {"id": sp.id if sp else None, "name": (u.name if u else "Student"),
                            "student_id": (u.user_id if u else ""),
                            "batch": (sp.batch_name if sp else "") or ""}
            if c.resolved_by:
                ru = umap.get(c.resolved_by)
                d["resolver_name"] = ru.name if ru else ""
        out.append(d)
    return out


def _complaint_dict(db, c, with_student=False, with_thread=False):
    d = {"id": c.id, "complaint_number": c.complaint_number, "title": c.title,
         "description": c.description or "", "status": c.status,
         "priority": c.priority or "normal", "source": c.source or "portal",
         "category_id": c.category_id, "category": _cat_name(db, c.category_id),
         "created_at": str(c.created_at or "")[:19], "updated_at": str(c.updated_at or "")[:19],
         "resolved_at": str(c.resolved_at or "")[:19] or None,
         "assigned_to": c.assigned_to, "resolved_by": c.resolved_by,
         "read_by_student": bool(c.read_by_student), "read_by_admin": bool(c.read_by_admin)}
    imgs, voices = _counts(db, c.id)
    d["image_count"], d["voice_count"] = imgs, voices
    if with_student:
        sp = db.query(StudentProfile).filter(StudentProfile.id == c.student_id).first()
        u = db.query(User).filter(User.id == sp.user_id).first() if sp else None
        d["student"] = {"id": sp.id if sp else None,
                        "name": (u.name if u else "Student"),
                        "student_id": (u.user_id if u else ""),
                        "batch": (sp.batch_name if sp else "") or ""}
        if c.resolved_by:
            ru = db.query(User).filter(User.id == c.resolved_by).first()
            d["resolver_name"] = ru.name if ru else ""
    if with_thread:
        msgs = db.query(SM.ComplaintMessage).filter(
            SM.ComplaintMessage.complaint_id == c.id).order_by(SM.ComplaintMessage.created_at).all()
        atts = db.query(SM.ComplaintAttachment).filter(
            SM.ComplaintAttachment.complaint_id == c.id).all()
        by_msg = {}
        for a in atts:
            by_msg.setdefault(a.message_id, []).append(_att_dict(a))
        d["messages"] = [{"id": m.id, "sender_role": m.sender_role or "",
                          "message": m.message or "", "at": str(m.created_at or "")[:19],
                          "attachments": by_msg.get(m.id, [])} for m in msgs]
    return d


# ===========================================================================
# STUDENT
# ===========================================================================
@router.get("/api/student/complaint-categories")
def student_categories(db: Session = Depends(get_db), me=Depends(get_student)):
    cats = db.query(SM.ComplaintCategory).filter(
        SM.ComplaintCategory.status == "active").order_by(
        SM.ComplaintCategory.display_order, SM.ComplaintCategory.id).all()
    return {"categories": [{"id": c.id, "name": c.name, "icon": c.icon or ""} for c in cats]}


@router.post("/api/student/complaints")
async def student_create_complaint(
        title: str = Form(...), description: str = Form(""), category_id: str = Form(""),
        images: list[UploadFile] = File(default=[]), voice: UploadFile = File(default=None),
        db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Please add a short title.")
    cid = int(category_id) if str(category_id).strip().isdigit() else None
    if cid is not None:
        ok = db.query(SM.ComplaintCategory.id).filter(
            SM.ComplaintCategory.id == cid,
            SM.ComplaintCategory.status == "active").first()
        if not ok:
            cid = None
    c = SM.Complaint(student_id=sp.id, category_id=cid, title=title[:200],
                     description=(description or "").strip() or None, status="open",
                     priority="normal", source="portal", read_by_student=True,
                     read_by_admin=False)
    db.add(c)
    db.flush()
    c.complaint_number = SM.gen_complaint_number(db, c.created_at or ist_now())
    msg = SM.ComplaintMessage(complaint_id=c.id, sender_user_id=me.id,
                              sender_role="student", message=(description or "").strip() or None)
    db.add(msg)
    db.flush()
    await _save_attachments(db, c.id, msg.id, me.id, images, voice)
    _log(db, c.id, me.id, "student", "created", title[:120])
    link = "/support/complaint/%d" % c.id
    for aid in _admin_user_ids(db):
        _notify(db, aid, "New complaint", "%s: %s" % (c.complaint_number, title[:80]),
                "complaint_new", link)
    db.commit()
    return {"ok": True, "id": c.id, "complaint_number": c.complaint_number, "status": c.status}


@router.get("/api/student/complaints")
def student_list_complaints(db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    rows = db.query(SM.Complaint).filter(SM.Complaint.student_id == sp.id).order_by(
        SM.Complaint.updated_at.desc()).all()
    return {"complaints": _serialize_list(db, rows),
            "unread": sum(1 for c in rows if not c.read_by_student)}


@router.get("/api/student/complaints/{cid}")
def student_complaint_detail(cid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c or c.student_id != sp.id:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    if not c.read_by_student:
        c.read_by_student = True
        db.commit()
    return {"complaint": _complaint_dict(db, c, with_thread=True)}


@router.post("/api/student/complaints/{cid}/messages")
async def student_reply(cid: int, message: str = Form(""),
                        images: list[UploadFile] = File(default=[]),
                        voice: UploadFile = File(default=None),
                        db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c or c.student_id != sp.id:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    if not (message or "").strip() and not images and not voice:
        raise HTTPException(status_code=400, detail="Empty message.")
    m = SM.ComplaintMessage(complaint_id=c.id, sender_user_id=me.id,
                            sender_role="student", message=(message or "").strip() or None)
    db.add(m)
    db.flush()
    await _save_attachments(db, c.id, m.id, me.id, images, voice)
    if c.status == "resolved":
        c.status = "reopened"
        c.reopened_at = ist_now()
        _log(db, c.id, me.id, "student", "reopened")
    if c.status == "waiting_student":
        c.status = "in_progress"
    c.read_by_admin = False
    c.updated_at = ist_now()
    link = "/support/complaint/%d" % c.id
    for aid in _admin_user_ids(db):
        _notify(db, aid, "Complaint update", "%s: student replied" % c.complaint_number,
                "complaint_reply", link)
    db.commit()
    return {"ok": True}


@router.get("/api/student/complaint-attachments/{aid}/view")
def student_view_att(aid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    return _serve_attachment(db, aid, False, me, False)


@router.get("/api/student/complaint-attachments/{aid}/download")
def student_dl_att(aid: int, db: Session = Depends(get_db), me=Depends(get_student)):
    return _serve_attachment(db, aid, False, me, True)


# ---- feedback ----
@router.post("/api/student/feedback")
def student_feedback(payload: dict = Body(...), db: Session = Depends(get_db),
                     me=Depends(get_student)):
    sp = _sp(db, me)
    try:
        rating = int(payload.get("rating"))
    except Exception:
        raise HTTPException(status_code=400, detail="Please choose a star rating.")
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be 1–5.")
    fb = SM.Feedback(student_id=sp.id, rating=rating,
                     review=(str(payload.get("review") or "").strip() or None),
                     status="active", read_by_admin=False)
    db.add(fb)
    for aid in _admin_user_ids(db):
        _notify(db, aid, "New feedback", "%d★ from a student" % rating, "feedback_new",
                "/support/feedback")
    db.commit()
    return {"ok": True, "id": fb.id}


@router.get("/api/student/feedback")
def student_my_feedback(db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    rows = db.query(SM.Feedback).filter(SM.Feedback.student_id == sp.id,
                                        SM.Feedback.status == "active").order_by(
        SM.Feedback.created_at.desc()).all()
    return {"feedback": [{"id": f.id, "rating": f.rating, "review": f.review or "",
                          "created_at": str(f.created_at or "")[:19]} for f in rows]}


# ===========================================================================
# ADMIN — complaints
# ===========================================================================
def _apply_date_range(q, col, rng, frm, to):
    from datetime import timedelta
    today = ist_now().date()
    if rng == "today":
        start, end = today, today
    elif rng == "yesterday":
        start = end = today - timedelta(days=1)
    elif rng == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif rng == "month":
        start, end = today.replace(day=1), today
    elif rng == "last_month":
        first = today.replace(day=1)
        end = first - timedelta(days=1)
        start = end.replace(day=1)
    elif rng == "custom" and frm and to:
        try:
            from datetime import date
            start = date.fromisoformat(frm[:10]); end = date.fromisoformat(to[:10])
        except Exception:
            return q
    else:
        return q
    from datetime import datetime, time
    return q.filter(col >= datetime.combine(start, time.min),
                    col <= datetime.combine(end, time.max))


@router.get("/api/admin/complaints")
def admin_list_complaints(status: str = "", category_id: int = 0, priority: str = "",
                          resolver: int = 0, source: str = "", q: str = "",
                          date_range: str = "", date_from: str = "", date_to: str = "",
                          page: int = 1, page_size: int = 20,
                          db: Session = Depends(get_db), _=Depends(admin_guard)):
    query = db.query(SM.Complaint)
    if status and status != "all":
        if status == "pending":
            query = query.filter(SM.Complaint.status.in_(SM.OPEN_STATUSES))
        else:
            query = query.filter(SM.Complaint.status == status)
    if category_id:
        query = query.filter(SM.Complaint.category_id == category_id)
    if priority and priority != "all":
        query = query.filter(SM.Complaint.priority == priority)
    if resolver:
        query = query.filter(SM.Complaint.resolved_by == resolver)
    if source and source != "all":
        query = query.filter(SM.Complaint.source == source)
    query = _apply_date_range(query, SM.Complaint.created_at, date_range, date_from, date_to)
    if q and q.strip():
        term = "%%%s%%" % q.strip()
        sub = db.query(StudentProfile.id).join(
            User, User.id == StudentProfile.user_id).filter(
            or_(User.name.ilike(term), User.user_id.ilike(term)))
        query = query.filter(or_(SM.Complaint.complaint_number.ilike(term),
                                 SM.Complaint.title.ilike(term),
                                 SM.Complaint.description.ilike(term),
                                 SM.Complaint.student_id.in_(sub)))
    total = query.count()
    page = max(1, page); page_size = min(max(1, page_size), 50)
    rows = query.order_by(SM.Complaint.updated_at.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    return {"complaints": _serialize_list(db, rows, with_student=True),
            "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total}


@router.get("/api/admin/complaints/{cid}")
def admin_complaint_detail(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    if not c.read_by_admin:
        c.read_by_admin = True
        db.commit()
    return {"complaint": _complaint_dict(db, c, with_student=True, with_thread=True)}


@router.post("/api/admin/complaints/{cid}/messages")
async def admin_reply(cid: int, message: str = Form(""),
                      images: list[UploadFile] = File(default=[]),
                      voice: UploadFile = File(default=None),
                      db: Session = Depends(get_db), me=Depends(admin_guard)):
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    if not (message or "").strip() and not images and not voice:
        raise HTTPException(status_code=400, detail="Empty reply.")
    m = SM.ComplaintMessage(complaint_id=c.id, sender_user_id=me.id,
                            sender_role="admin", message=(message or "").strip() or None)
    db.add(m)
    db.flush()
    await _save_attachments(db, c.id, m.id, me.id, images, voice)
    if c.status in ("open", "reopened"):
        c.status = "in_progress"
    c.read_by_student = False
    c.updated_at = ist_now()
    _log(db, c.id, me.id, "admin", "replied", (message or "")[:120])
    sp = db.query(StudentProfile).filter(StudentProfile.id == c.student_id).first()
    if sp:
        _notify(db, sp.user_id, "Reply on your complaint",
                "%s — support replied" % c.complaint_number, "complaint_reply",
                "/support/complaint/%d" % c.id)
    db.commit()
    return {"ok": True, "status": c.status}


@router.post("/api/admin/complaints/{cid}/action")
def admin_complaint_action(cid: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), me=Depends(admin_guard)):
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    action = str((payload or {}).get("action") or "").strip()
    sp = db.query(StudentProfile).filter(StudentProfile.id == c.student_id).first()
    if action == "resolve":
        c.status = "resolved"
        now = ist_now()
        if not c.first_resolved_at:
            c.first_resolved_at = now
        c.resolved_at = now
        c.resolved_by = me.id
        c.read_by_student = False
        _log(db, c.id, me.id, "admin", "resolved")
        if sp:
            _notify(db, sp.user_id, "Complaint resolved",
                    "%s has been resolved" % c.complaint_number, "complaint_resolved",
                    "/support/complaint/%d" % c.id)
    elif action == "reopen":
        c.status = "reopened"
        c.reopened_at = ist_now()
        _log(db, c.id, me.id, "admin", "reopened")
    elif action == "status":
        st = str(payload.get("status") or "")
        if st in SM.COMPLAINT_STATUSES:
            c.status = st
            _log(db, c.id, me.id, "admin", "status", st)
    elif action == "priority":
        pr = str(payload.get("priority") or "")
        if pr in SM.COMPLAINT_PRIORITIES:
            c.priority = pr
            _log(db, c.id, me.id, "admin", "priority", pr)
    elif action == "assign":
        c.assigned_to = int(payload.get("assigned_to") or 0) or None
        if c.status == "open":
            c.status = "assigned"
        _log(db, c.id, me.id, "admin", "assigned", str(c.assigned_to))
    else:
        raise HTTPException(status_code=400, detail="Unknown action.")
    c.updated_at = ist_now()
    db.commit()
    return {"ok": True, "status": c.status, "priority": c.priority}


@router.get("/api/admin/complaints/{cid}/whatsapp")
def admin_complaint_whatsapp(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    """Graceful WhatsApp: returns a wa.me click-to-send link if configured and the
    student has a phone. No unknown send API is called."""
    c = db.query(SM.Complaint).filter(SM.Complaint.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Complaint not found.")
    sp = db.query(StudentProfile).filter(StudentProfile.id == c.student_id).first()
    configured = False
    try:
        import whatsapp as W
        configured = bool(W.is_configured())
    except Exception:
        configured = False
    phone = (sp.phone if sp else "") or ""
    if not phone:
        return {"available": False, "reason": "no_phone"}
    import urllib.parse
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    last = db.query(SM.ComplaintMessage).filter(
        SM.ComplaintMessage.complaint_id == c.id,
        SM.ComplaintMessage.sender_role == "admin").order_by(
        SM.ComplaintMessage.created_at.desc()).first()
    body = "Regarding your complaint %s: %s" % (
        c.complaint_number, (last.message if last and last.message else "It has been resolved."))
    url = "https://wa.me/%s?text=%s" % (digits, urllib.parse.quote(body))
    return {"available": True, "configured": configured, "url": url}


# ---- analytics ----
@router.get("/api/admin/complaint-analytics")
def admin_complaint_analytics(rng: str = "30", db: Session = Depends(get_db),
                              _=Depends(admin_guard)):
    from datetime import timedelta, datetime, time
    C = SM.Complaint
    today = ist_now().date()
    pending = db.query(func.count(C.id)).filter(C.status.in_(SM.OPEN_STATUSES)).scalar() or 0
    total = db.query(func.count(C.id)).scalar() or 0
    resolved_total = db.query(func.count(C.id)).filter(C.status == "resolved").scalar() or 0
    reopened = db.query(func.count(C.id)).filter(C.status == "reopened").scalar() or 0
    critical = db.query(func.count(C.id)).filter(
        C.priority.in_(["critical", "emergency"]), C.status.in_(SM.OPEN_STATUSES)).scalar() or 0

    def _day_bounds(d):
        return datetime.combine(d, time.min), datetime.combine(d, time.max)
    ts, te = _day_bounds(today)
    created_today = db.query(func.count(C.id)).filter(C.created_at >= ts, C.created_at <= te).scalar() or 0
    resolved_today = db.query(func.count(C.id)).filter(
        C.resolved_at >= ts, C.resolved_at <= te).scalar() or 0
    m_start = datetime.combine(today.replace(day=1), time.min)
    created_month = db.query(func.count(C.id)).filter(C.created_at >= m_start).scalar() or 0
    resolved_month = db.query(func.count(C.id)).filter(C.resolved_at >= m_start).scalar() or 0

    resolution_rate = round(resolved_total * 100.0 / total, 1) if total else 0.0

    # avg resolution time (seconds) over resolved complaints
    rows = db.query(C.created_at, C.resolved_at).filter(
        C.resolved_at.isnot(None), C.created_at.isnot(None)).all()
    secs = [(r.resolved_at - r.created_at).total_seconds() for r in rows
            if r.resolved_at and r.created_at and r.resolved_at >= r.created_at]
    avg_secs = int(sum(secs) / len(secs)) if secs else 0

    # top resolver
    top = db.query(C.resolved_by, func.count(C.id).label("n")).filter(
        C.resolved_by.isnot(None)).group_by(C.resolved_by).order_by(func.count(C.id).desc()).first()
    top_resolver = None
    if top and top[0]:
        u = db.query(User).filter(User.id == top[0]).first()
        top_resolver = {"id": top[0], "name": (u.name if u else ""), "count": int(top[1])}

    # trend (received vs resolved) by day
    try:
        days = int(rng)
    except Exception:
        days = 30
    days = min(max(days, 7), 120)
    win_start = datetime.combine(today - timedelta(days=days - 1), time.min)
    rec_map = {str(d): int(n) for d, n in db.query(
        func.date(C.created_at), func.count(C.id)).filter(
        C.created_at >= win_start).group_by(func.date(C.created_at)).all()}
    res_map = {str(d): int(n) for d, n in db.query(
        func.date(C.resolved_at), func.count(C.id)).filter(
        C.resolved_at >= win_start).group_by(func.date(C.resolved_at)).all()}
    trend = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        trend.append({"date": key, "received": rec_map.get(key, 0),
                      "resolved": res_map.get(key, 0)})

    # by category
    cats = {c.id: c.name for c in db.query(SM.ComplaintCategory).all()}
    by_cat_rows = db.query(C.category_id, func.count(C.id)).group_by(C.category_id).all()
    by_category = [{"category": cats.get(cid, "Uncategorised"), "count": int(n)}
                   for cid, n in by_cat_rows]

    return {"pending": int(pending), "total": int(total), "resolved": int(resolved_total),
            "reopened": int(reopened), "critical": int(critical),
            "created_today": int(created_today), "resolved_today": int(resolved_today),
            "created_month": int(created_month), "resolved_month": int(resolved_month),
            "resolution_rate": resolution_rate, "avg_resolution_secs": avg_secs,
            "top_resolver": top_resolver, "trend": trend, "by_category": by_category}


@router.get("/api/admin/students/{sid}/support")
def admin_student_support(sid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student not found.")
    u = db.query(User).filter(User.id == sp.user_id).first()
    C = SM.Complaint
    comps = db.query(C).filter(C.student_id == sid).order_by(C.created_at.desc()).all()
    total = len(comps)
    open_n = sum(1 for c in comps if c.status in SM.OPEN_STATUSES)
    resolved_n = sum(1 for c in comps if c.status == "resolved")
    crit = sum(1 for c in comps if c.priority in ("critical", "emergency"))
    secs = [(c.resolved_at - c.created_at).total_seconds() for c in comps
            if c.resolved_at and c.created_at and c.resolved_at >= c.created_at]
    avg = int(sum(secs) / len(secs)) if secs else 0
    fbs = db.query(SM.Feedback).filter(SM.Feedback.student_id == sid,
                                       SM.Feedback.status == "active").order_by(
        SM.Feedback.created_at.desc()).all()
    return {"student": {"id": sp.id, "name": (u.name if u else "Student"),
                        "student_id": (u.user_id if u else ""), "phone": sp.phone or "",
                        "batch": sp.batch_name or "", "email": sp.email or "",
                        "verified": bool(sp.is_verified)},
            "summary": {"total": total, "open": open_n, "resolved": resolved_n,
                        "critical": crit, "avg_resolution_secs": avg},
            "complaints": _serialize_list(db, comps),
            "feedback": [{"id": f.id, "rating": f.rating, "review": f.review or "",
                          "created_at": str(f.created_at or "")[:19]} for f in fbs]}


@router.get("/api/admin/complaint-resolvers")
def admin_resolvers(db: Session = Depends(get_db), _=Depends(admin_guard)):
    ids = [r[0] for r in db.query(SM.Complaint.resolved_by).filter(
        SM.Complaint.resolved_by.isnot(None)).distinct().all()]
    admins = db.query(User).filter(User.role == UserRole.admin).all()
    seen = {u.id for u in admins}
    out = [{"id": u.id, "name": u.name} for u in admins]
    for i in ids:
        if i not in seen:
            u = db.query(User).filter(User.id == i).first()
            if u:
                out.append({"id": u.id, "name": u.name})
    return {"resolvers": out}


# ---- admin categories ----
@router.get("/api/admin/complaint-categories")
def admin_list_categories(db: Session = Depends(get_db), _=Depends(admin_guard)):
    cats = db.query(SM.ComplaintCategory).order_by(
        SM.ComplaintCategory.display_order, SM.ComplaintCategory.id).all()
    counts = dict(db.query(SM.Complaint.category_id, func.count(SM.Complaint.id)).group_by(
        SM.Complaint.category_id).all())
    return {"categories": [{"id": c.id, "name": c.name, "icon": c.icon or "",
                            "status": c.status, "display_order": c.display_order or 0,
                            "count": int(counts.get(c.id, 0))} for c in cats]}


@router.post("/api/admin/complaint-categories")
def admin_add_category(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(admin_guard)):
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required.")
    order = (db.query(func.count(SM.ComplaintCategory.id)).scalar() or 0) + 1
    c = SM.ComplaintCategory(name=name, icon=str(payload.get("icon") or "").strip() or None,
                             display_order=order, status="active")
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id}


@router.patch("/api/admin/complaint-categories/{cid}")
def admin_edit_category(cid: int, payload: dict = Body(...), db: Session = Depends(get_db),
                        _=Depends(admin_guard)):
    c = db.query(SM.ComplaintCategory).filter(SM.ComplaintCategory.id == cid).first()
    if not c:
        raise HTTPException(status_code=404, detail="Category not found.")
    if "name" in payload and str(payload["name"]).strip():
        c.name = str(payload["name"]).strip()
    if "status" in payload and str(payload["status"]) in ("active", "inactive"):
        c.status = str(payload["status"])
    if "display_order" in payload:
        try:
            c.display_order = int(payload["display_order"])
        except Exception:
            pass
    db.commit()
    return {"ok": True}


@router.get("/api/admin/complaint-attachments/{aid}/view")
def admin_view_att(aid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    return _serve_attachment(db, aid, True, None, False)


@router.get("/api/admin/complaint-attachments/{aid}/download")
def admin_dl_att(aid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    return _serve_attachment(db, aid, True, None, True)


# ===========================================================================
# ADMIN — feedback
# ===========================================================================
@router.get("/api/admin/feedback")
def admin_feedback(rating: int = 0, page: int = 1, page_size: int = 20,
                   db: Session = Depends(get_db), _=Depends(admin_guard)):
    q = db.query(SM.Feedback).filter(SM.Feedback.status == "active")
    if rating:
        q = q.filter(SM.Feedback.rating == rating)
    total = q.count()
    page = max(1, page); page_size = min(max(1, page_size), 50)
    rows = q.order_by(SM.Feedback.created_at.desc()).offset(
        (page - 1) * page_size).limit(page_size).all()
    out = []
    for f in rows:
        sp = db.query(StudentProfile).filter(StudentProfile.id == f.student_id).first()
        u = db.query(User).filter(User.id == sp.user_id).first() if sp else None
        out.append({"id": f.id, "rating": f.rating, "review": f.review or "",
                    "created_at": str(f.created_at or "")[:19], "new": not f.read_by_admin,
                    "student": {"id": sp.id if sp else None,
                                "name": (u.name if u else "Student"),
                                "student_id": (u.user_id if u else "")}})
    return {"feedback": out, "total": total, "page": page, "page_size": page_size,
            "has_more": page * page_size < total}


@router.post("/api/admin/feedback/{fid}/read")
def admin_feedback_read(fid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    f = db.query(SM.Feedback).filter(SM.Feedback.id == fid).first()
    if f and not f.read_by_admin:
        f.read_by_admin = True
        db.commit()
    return {"ok": True}


@router.post("/api/admin/feedback/{fid}/delete")
def admin_delete_feedback(fid: int, db: Session = Depends(get_db), me=Depends(admin_guard)):
    f = db.query(SM.Feedback).filter(SM.Feedback.id == fid).first()
    if not f:
        raise HTTPException(status_code=404, detail="Feedback not found.")
    f.status = "deleted"          # soft delete — preserves audit
    f.deleted_at = ist_now()
    f.deleted_by = me.id
    db.commit()
    return {"ok": True}


@router.get("/api/admin/feedback-analytics")
def admin_feedback_analytics(db: Session = Depends(get_db), _=Depends(admin_guard)):
    F = SM.Feedback
    active = db.query(F).filter(F.status == "active")
    total = active.count()
    if not total:
        return {"total": 0, "average": 0.0, "distribution": [], "new": 0}
    avg = round((db.query(func.sum(F.rating)).filter(F.status == "active").scalar() or 0) / total, 1)
    dist = []
    for star in (5, 4, 3, 2, 1):
        n = db.query(func.count(F.id)).filter(F.status == "active", F.rating == star).scalar() or 0
        dist.append({"star": star, "count": int(n), "pct": round(n * 100.0 / total, 1)})
    new = db.query(func.count(F.id)).filter(F.status == "active", F.read_by_admin == False).scalar() or 0  # noqa: E712
    return {"total": int(total), "average": avg, "distribution": dist, "new": int(new)}


# ---- unread badges (student + admin) ----
@router.get("/api/student/support-badges")
def student_badges(db: Session = Depends(get_db), me=Depends(get_student)):
    sp = _sp(db, me)
    unread = db.query(func.count(SM.Complaint.id)).filter(
        SM.Complaint.student_id == sp.id, SM.Complaint.read_by_student == False).scalar() or 0  # noqa: E712
    return {"complaints": int(unread)}


@router.get("/api/admin/support-badges")
def admin_badges(db: Session = Depends(get_db), _=Depends(admin_guard)):
    comp = db.query(func.count(SM.Complaint.id)).filter(
        SM.Complaint.read_by_admin == False).scalar() or 0  # noqa: E712
    fb = db.query(func.count(SM.Feedback.id)).filter(
        SM.Feedback.status == "active", SM.Feedback.read_by_admin == False).scalar() or 0  # noqa: E712
    return {"complaints": int(comp), "feedback": int(fb)}
