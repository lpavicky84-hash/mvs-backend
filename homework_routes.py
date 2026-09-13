"""
Homework Checker API — student sends notes/homework to their SUBJECT TEACHER for
checking. Mirrors the Material Checker (teacher<->admin) flow, but student<->teacher.

Flow: student submit -> teacher view/download (auto under_review + student notified)
      -> teacher "Changes Required" (student resubmits) OR "Checking Done" (congrats).
Chat: WhatsApp-style; student can reply only if the teacher has allowed chat.
Additive only — no existing table/route touched.
"""
import time as _time
from datetime import datetime as _dt
from fastapi import (APIRouter, Depends, HTTPException, Form, UploadFile, File)
from sqlalchemy.orm import Session

from database import get_db
from security import get_student, get_teacher

router = APIRouter(tags=["HomeworkChecker"])

_PENDING = ("submitted", "resubmitted")
_HW_TYPING = {}   # {submission_id: {"student": ts, "teacher": ts}}


# ----------------------------- helpers -----------------------------
def _R2():
    return __import__("r2_storage")


def _hsafe(name):
    return (str(name or "file")).replace(chr(34), "").replace("\n", " ")[:180]


def _sp(db, user):
    from student_routes import get_student_profile
    return get_student_profile(user, db)


def _tp(db, user):
    from teacher_routes import get_teacher_profile
    return get_teacher_profile(user, db)


def _sub_or_404(db, hid):
    from homework_models import HomeworkSubmission
    m = db.query(HomeworkSubmission).filter(HomeworkSubmission.id == hid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Homework not found.")
    return m


def _student_owns(db, hid, sp):
    m = _sub_or_404(db, hid)
    if m.student_id != sp.id:
        raise HTTPException(status_code=404, detail="Homework not found.")
    return m


def _teacher_owns(db, hid, tp):
    m = _sub_or_404(db, hid)
    if m.teacher_id != tp.id:
        raise HTTPException(status_code=404, detail="Homework not found.")
    return m


def _notify(db, user_id, title, msg, ntype="homework"):
    try:
        from admin_routes import notify
        if user_id:
            notify(db, user_id, title, msg, ntype)
            db.commit()   # notify ADD karta hai, khud commit nahi — warna notification lost ho jaata tha
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _student_user_id(db, m):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == m.student_id).first()
    return (sp.user_id if sp else None), (sp.user.name if (sp and sp.user) else "Student"), sp


def _teacher_user_id(db, m):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == m.teacher_id).first()
    return (tp.user_id if tp else None), (tp.user.name if (tp and tp.user) else "Teacher"), tp


def _hw_set_typing(hid, role):
    try:
        d = _HW_TYPING.setdefault(int(hid), {})
        d[role] = _time.time()
        if len(_HW_TYPING) > 800:
            cut = _time.time() - 30
            for k in list(_HW_TYPING.keys()):
                vals = _HW_TYPING.get(k) or {}
                if not vals or all((v or 0) < cut for v in vals.values()):
                    _HW_TYPING.pop(k, None)
    except Exception:
        pass


def _hw_is_typing(hid, role):
    try:
        d = _HW_TYPING.get(int(hid)) or {}
        return (_time.time() - (d.get(role) or 0)) < 6
    except Exception:
        return False


def _presence_for(db, uid):
    from models import UserSession
    if not uid:
        return (False, None)
    try:
        s = db.query(UserSession).filter(UserSession.user_id == uid).order_by(UserSession.last_seen.desc()).first()
        last = s.last_seen if s else None
    except Exception:
        last = None
    online = bool(last and (_dt.now() - last).total_seconds() < 50)
    return (online, (last.strftime("%d %b, %I:%M %p") if last else None))


def _chat_status(db, m, my_role):
    if my_role == "teacher":
        uid, name, sp = _student_user_id(db, m)
        other_role, other_tid = "student", None
    else:
        uid, name, tp = _teacher_user_id(db, m)
        other_role, other_tid = "teacher", (m.teacher_id or None)
    online, last = _presence_for(db, uid)
    return {"other_name": name, "other_role": other_role, "other_tid": other_tid,
            "other_online": online, "other_last_seen": last,
            "other_typing": _hw_is_typing(m.id, other_role)}


def _sub_dict(db, m, teacher_name=None, student_name=None, unread=0):
    return {"id": m.id, "subject": m.subject, "title": m.title,
            "description": m.description or "", "status": m.status,
            "current_version": m.current_version or 1,
            "chat_allowed": bool(m.chat_allowed),
            "teacher_id": m.teacher_id, "teacher_name": teacher_name,
            "student_id": m.student_id, "student_name": student_name,
            "unread": unread,
            "created_at": str(m.created_at or "")[:19]}


def _versions(db, hid):
    from homework_models import HomeworkVersion
    vs = db.query(HomeworkVersion).filter(HomeworkVersion.submission_id == hid) \
        .order_by(HomeworkVersion.version_no.desc()).all()
    return [{"id": v.id, "version_no": v.version_no, "filename": v.filename,
             "file_size": v.file_size or 0, "remarks": v.remarks or "",
             "created_at": str(v.created_at or "")[:19]} for v in vs]


def _att_dict(a):
    return {"id": a.id, "filename": a.filename or "file", "mime": a.mime or "",
            "is_image": (a.mime or "").startswith("image/")}


def _msg_dict(db, msg):
    from homework_models import HomeworkAttachment
    atts = db.query(HomeworkAttachment).filter(HomeworkAttachment.message_id == msg.id).all()
    return {"id": msg.id, "sender_role": msg.sender_role or "", "message": msg.message or "",
            "at": str(msg.created_at or "")[:19],
            "read_by_student": bool(msg.read_by_student),
            "read_by_teacher": bool(msg.read_by_teacher),
            "attachments": [_att_dict(a) for a in atts]}


async def _store_version(db, m, upload, remarks=None):
    from homework_models import HomeworkVersion
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    ct = getattr(upload, "content_type", None) or "application/pdf"
    R2 = _R2()
    ref = R2.store_file_value(R2.new_key("homework", upload.filename or "file"), raw, ct)
    vno = (m.current_version or 0) + 1 if db.query(HomeworkVersion).filter(HomeworkVersion.submission_id == m.id).count() else 1
    v = HomeworkVersion(submission_id=m.id, version_no=vno, file_url=ref,
                        filename=(upload.filename or "homework.pdf"), file_size=len(raw),
                        mime=ct, remarks=remarks)
    db.add(v)
    m.current_version = vno
    return v


async def _post_msg(db, m, sender_role, sender_uid, message, files):
    from homework_models import HomeworkMessage, HomeworkAttachment
    msg = HomeworkMessage(submission_id=m.id, sender_user_id=sender_uid,
                          sender_role=sender_role, message=(message or "").strip() or None,
                          read_by_student=(sender_role == "student"),
                          read_by_teacher=(sender_role == "teacher"))
    db.add(msg)
    db.flush()
    R2 = _R2()
    for f in (files or []):
        if not f:
            continue
        raw = await f.read()
        if not raw:
            continue
        ct = f.content_type or "application/octet-stream"
        ref = R2.store_file_value(R2.new_key("hw-chat", f.filename or "file"), raw, ct)
        db.add(HomeworkAttachment(submission_id=m.id, message_id=msg.id,
                                  kind=("image" if ct.startswith("image/") else "file"),
                                  url=ref, filename=f.filename or "file", mime=ct,
                                  uploader_user_id=sender_uid))
    db.commit()
    return msg


def _auto_under_review(db, m):
    """Teacher ne submitted/resubmitted homework VIEW/DOWNLOAD kiya -> under_review + student notify."""
    try:
        if (m.status or "") in _PENDING:
            m.status = "under_review"
            db.commit()
            uid, _n, _sp = _student_user_id(db, m)
            _notify(db, uid, "\U0001F440 Homework Under Review",
                    'Your teacher opened your "%s" homework. It is now under review.' % m.title)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# ============================ STUDENT =============================
@router.get("/api/student/hw/teachers")
def student_hw_teachers(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Student ke subjects + har subject ka teacher (auto-fill ke liye)."""
    from student_routes import _teacher_for_subject
    sp = _sp(db, current_user)
    out, seen = [], set()
    for s in (sp.subjects or []):
        tp = _teacher_for_subject(db, s)
        if s in seen:
            continue
        seen.add(s)
        out.append({"subject": s,
                    "teacher_id": (tp.id if tp else None),
                    "teacher_name": (tp.user.name if (tp and tp.user) else None),
                    "has_teacher": bool(tp and tp.user)})
    return {"subjects": out}


@router.post("/api/student/homework")
async def student_create_homework(subject: str = Form(...), title: str = Form(...),
                                  description: str = Form(""), file: UploadFile = File(...),
                                  db: Session = Depends(get_db), current_user=Depends(get_student)):
    from homework_models import HomeworkSubmission
    from student_routes import _teacher_for_subject
    sp = _sp(db, current_user)
    tp = _teacher_for_subject(db, subject)
    if not tp:
        raise HTTPException(status_code=400, detail="No teacher is assigned for this subject. Please contact the admin.")
    m = HomeworkSubmission(student_id=sp.id, teacher_id=tp.id, subject=subject,
                           title=(title or "Homework").strip()[:200],
                           description=(description or "").strip() or None,
                           status="submitted", current_version=0, chat_allowed=False)
    db.add(m)
    db.flush()
    await _store_version(db, m, file)
    db.commit()
    _notify(db, tp.user_id, "\U0001F4DD New Homework to Check",
            '%s sent a %s homework "%s" for checking.' % (current_user.name, subject, m.title), "homework")
    return {"ok": True, "id": m.id}


@router.get("/api/student/homework")
def student_list_homework(db: Session = Depends(get_db), current_user=Depends(get_student)):
    from homework_models import HomeworkSubmission, HomeworkMessage
    sp = _sp(db, current_user)
    rows = db.query(HomeworkSubmission).filter(HomeworkSubmission.student_id == sp.id) \
        .order_by(HomeworkSubmission.id.desc()).all()
    from models import TeacherProfile
    tmap = {t.id: (t.user.name if t.user else "Teacher")
            for t in db.query(TeacherProfile).filter(TeacherProfile.id.in_([r.teacher_id for r in rows] or [0])).all()}
    out = []
    for m in rows:
        unread = db.query(HomeworkMessage).filter(HomeworkMessage.submission_id == m.id,
                                                  HomeworkMessage.sender_role == "teacher",
                                                  HomeworkMessage.read_by_student == False).count()
        out.append(_sub_dict(db, m, teacher_name=tmap.get(m.teacher_id), unread=unread))
    return {"homework": out}


@router.get("/api/student/homework/{hid}")
def student_get_homework(hid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = _sp(db, current_user)
    m = _student_owns(db, hid, sp)
    _uid, tname, _tp = _teacher_user_id(db, m)
    d = _sub_dict(db, m, teacher_name=tname)
    d["versions"] = _versions(db, m.id)
    return {"submission": d}


@router.post("/api/student/homework/{hid}/resubmit")
async def student_resubmit(hid: int, file: UploadFile = File(...),
                           db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = _sp(db, current_user)
    m = _student_owns(db, hid, sp)
    if m.status not in ("changes_required", "under_review", "checking_done"):
        # normally resubmit tab jab changes maange gaye ho; phir bhi allow (safe)
        pass
    await _store_version(db, m, file)
    m.status = "resubmitted"
    db.commit()
    _uid, _n, _tp = _teacher_user_id(db, m)
    _notify(db, _uid, "\U0001F504 Homework Resubmitted",
            '%s resubmitted "%s" (v%d).' % (current_user.name, m.title, m.current_version))
    return {"ok": True}


@router.get("/api/student/homework-versions/{vid}/download")
def student_dl_version(vid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    return _serve_version(db, vid, "student", current_user, inline=False)


@router.get("/api/student/homework-versions/{vid}/view")
def student_view_version(vid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    return _serve_version(db, vid, "student", current_user, inline=True)


def _serve_version(db, vid, role, user, inline):
    from homework_models import HomeworkVersion
    v = db.query(HomeworkVersion).filter(HomeworkVersion.id == vid).first()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found.")
    m = _sub_or_404(db, v.submission_id)
    if role == "student":
        sp = _sp(db, user)
        if m.student_id != sp.id:
            raise HTTPException(status_code=404, detail="Not found.")
    else:
        tp = _tp(db, user)
        if m.teacher_id != tp.id:
            raise HTTPException(status_code=404, detail="Not found.")
        _auto_under_review(db, m)   # teacher ne dekha/download kiya
    return _R2().proxy_response(v.file_url, v.mime or "application/pdf",
                               _hsafe(v.filename or "homework.pdf"), (not inline), sniff=True)


# -------- student chat --------
@router.get("/api/student/homework/{hid}/messages")
def student_msgs(hid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    from homework_models import HomeworkMessage
    sp = _sp(db, current_user)
    m = _student_owns(db, hid, sp)
    msgs = db.query(HomeworkMessage).filter(HomeworkMessage.submission_id == hid) \
        .order_by(HomeworkMessage.created_at).all()
    changed = False
    for x in msgs:
        if x.sender_role == "teacher" and not x.read_by_student:
            x.read_by_student = True
            changed = True
    if changed:
        db.commit()
    st = _chat_status(db, m, "student")
    st["chat_allowed"] = bool(m.chat_allowed)
    return {"messages": [_msg_dict(db, x) for x in msgs], "status": st}


@router.post("/api/student/homework/{hid}/typing")
def student_typing(hid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = _sp(db, current_user)
    _student_owns(db, hid, sp)
    _hw_set_typing(hid, "student")
    return {"ok": True}


@router.post("/api/student/homework/{hid}/messages")
async def student_send_msg(hid: int, message: str = Form(""),
                           files: list[UploadFile] = File(default=[]),
                           db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = _sp(db, current_user)
    m = _student_owns(db, hid, sp)
    if not m.chat_allowed:
        raise HTTPException(status_code=403, detail="Your teacher hasn't enabled replies yet. Please wait.")
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_msg(db, m, "student", current_user.id, message, files)
    _uid, _n, _tp = _teacher_user_id(db, m)
    _notify(db, _uid, "\U0001F4AC Homework reply from student",
            '%s replied on "%s".' % (current_user.name, m.title))
    return {"ok": True, "message": _msg_dict(db, msg)}


@router.get("/api/student/homework-attachments/{aid}/view")
def student_att_view(aid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    return _serve_att(db, aid, "student", current_user, inline=True)


@router.get("/api/student/homework-attachments/{aid}/download")
def student_att_dl(aid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    return _serve_att(db, aid, "student", current_user, inline=False)


def _serve_att(db, aid, role, user, inline):
    from homework_models import HomeworkAttachment
    a = db.query(HomeworkAttachment).filter(HomeworkAttachment.id == aid).first()
    if not a:
        raise HTTPException(status_code=404, detail="Not found.")
    m = _sub_or_404(db, a.submission_id)
    if role == "student":
        sp = _sp(db, user)
        if m.student_id != sp.id:
            raise HTTPException(status_code=404, detail="Not found.")
    else:
        tp = _tp(db, user)
        if m.teacher_id != tp.id:
            raise HTTPException(status_code=404, detail="Not found.")
    return _R2().proxy_response(a.url, a.mime or "application/octet-stream",
                               _hsafe(a.filename or "file"), (not inline), sniff=True)


# ============================ TEACHER =============================
@router.get("/api/teacher/homework")
def teacher_list_homework(status: str = "", student_id: int = 0,
                          db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from homework_models import HomeworkSubmission, HomeworkMessage
    tp = _tp(db, current_user)
    q = db.query(HomeworkSubmission).filter(HomeworkSubmission.teacher_id == tp.id)
    rows = q.order_by(HomeworkSubmission.id.desc()).all()
    from models import StudentProfile
    smap = {s.id: (s.user.name if s.user else "Student")
            for s in db.query(StudentProfile).filter(StudentProfile.id.in_([r.student_id for r in rows] or [0])).all()}
    counts = {"total": len(rows), "pending": 0, "under_review": 0, "changes_required": 0, "checking_done": 0}
    for m in rows:
        if m.status in _PENDING:
            counts["pending"] += 1
        elif m.status == "under_review":
            counts["under_review"] += 1
        elif m.status == "changes_required":
            counts["changes_required"] += 1
        elif m.status == "checking_done":
            counts["checking_done"] += 1
    out = []
    for m in rows:
        if student_id and m.student_id != student_id:
            continue
        if status:
            if status == "pending" and m.status not in _PENDING:
                continue
            if status != "pending" and m.status != status:
                continue
        unread = db.query(HomeworkMessage).filter(HomeworkMessage.submission_id == m.id,
                                                  HomeworkMessage.sender_role == "student",
                                                  HomeworkMessage.read_by_teacher == False).count()
        out.append(_sub_dict(db, m, student_name=smap.get(m.student_id), unread=unread))
    return {"homework": out, "counts": counts}


@router.get("/api/teacher/homework/{hid}")
def teacher_get_homework(hid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = _tp(db, current_user)
    m = _teacher_owns(db, hid, tp)
    _uid, sname, _sp = _student_user_id(db, m)
    d = _sub_dict(db, m, student_name=sname)
    d["versions"] = _versions(db, m.id)
    return {"submission": d}


@router.get("/api/teacher/homework-versions/{vid}/download")
def teacher_dl_version(vid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return _serve_version(db, vid, "teacher", current_user, inline=False)


@router.get("/api/teacher/homework-versions/{vid}/view")
def teacher_view_version(vid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return _serve_version(db, vid, "teacher", current_user, inline=True)


@router.post("/api/teacher/homework/{hid}/toggle-chat")
def teacher_toggle_chat(hid: int, payload: dict = None, db: Session = Depends(get_db),
                        current_user=Depends(get_teacher)):
    tp = _tp(db, current_user)
    m = _teacher_owns(db, hid, tp)
    allow = bool((payload or {}).get("allow"))
    m.chat_allowed = allow
    db.commit()
    if allow:
        uid, _n, _sp = _student_user_id(db, m)
        _notify(db, uid, "\U0001F4AC Teacher enabled chat",
                'You can now reply to your teacher on "%s".' % m.title)
    return {"ok": True, "chat_allowed": m.chat_allowed}


@router.post("/api/teacher/homework/{hid}/review")
async def teacher_review(hid: int, payload: dict = None, db: Session = Depends(get_db),
                         current_user=Depends(get_teacher)):
    tp = _tp(db, current_user)
    m = _teacher_owns(db, hid, tp)
    decision = str((payload or {}).get("decision") or "").strip()
    remarks = str((payload or {}).get("remarks") or "").strip()
    if decision not in ("changes_required", "checking_done", "under_review"):
        raise HTTPException(status_code=400, detail="Invalid decision.")
    m.status = decision
    # remark -> current version pe bhi save
    if remarks:
        from homework_models import HomeworkVersion
        cur = db.query(HomeworkVersion).filter(HomeworkVersion.submission_id == m.id) \
            .order_by(HomeworkVersion.version_no.desc()).first()
        if cur:
            cur.remarks = ((cur.remarks + "\n") if cur.remarks else "") + remarks
    db.commit()
    uid, _n, _sp = _student_user_id(db, m)
    if decision == "checking_done":
        # chat me message: teacher ne remark diya to wahi, warna auto-congrats
        auto = remarks or ("\U0001F389 Checking done \u2014 sab theek hai, great work!")
        try:
            await _post_msg(db, m, "teacher", current_user.id, auto, [])
        except Exception:
            pass
        _notify(db, uid, "\U0001F389 Homework Checked!",
                'Congratulations! Your "%s" homework has been checked. %s' % (m.title, ("Remark: " + remarks) if remarks else "Great work!"))
    elif decision == "changes_required":
        m2 = 'Changes needed on "%s". %s' % (m.title, ("Remark: " + remarks) if remarks else "Please check the chat and resubmit.")
        if remarks:
            try:
                await _post_msg(db, m, "teacher", current_user.id, remarks, [])
            except Exception:
                pass
        _notify(db, uid, "\u270F\uFE0F Changes Required on your Homework", m2)
    return {"ok": True, "status": m.status, "chat_allowed": m.chat_allowed}


# -------- teacher chat --------
@router.get("/api/teacher/homework/{hid}/messages")
def teacher_msgs(hid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from homework_models import HomeworkMessage
    tp = _tp(db, current_user)
    m = _teacher_owns(db, hid, tp)
    msgs = db.query(HomeworkMessage).filter(HomeworkMessage.submission_id == hid) \
        .order_by(HomeworkMessage.created_at).all()
    changed = False
    for x in msgs:
        if x.sender_role == "student" and not x.read_by_teacher:
            x.read_by_teacher = True
            changed = True
    if changed:
        db.commit()
    st = _chat_status(db, m, "teacher")
    st["chat_allowed"] = bool(m.chat_allowed)
    return {"messages": [_msg_dict(db, x) for x in msgs], "status": st}


@router.post("/api/teacher/homework/{hid}/typing")
def teacher_typing(hid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = _tp(db, current_user)
    _teacher_owns(db, hid, tp)
    _hw_set_typing(hid, "teacher")
    return {"ok": True}


@router.post("/api/teacher/homework/{hid}/messages")
async def teacher_send_msg(hid: int, message: str = Form(""),
                           files: list[UploadFile] = File(default=[]),
                           db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = _tp(db, current_user)
    m = _teacher_owns(db, hid, tp)
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_msg(db, m, "teacher", current_user.id, message, files)
    uid, _n, _sp = _student_user_id(db, m)
    _notify(db, uid, "\U0001F4AC Message from your teacher",
            'New message on your "%s" homework.' % m.title)
    return {"ok": True, "message": _msg_dict(db, msg)}


@router.get("/api/teacher/homework-attachments/{aid}/view")
def teacher_att_view(aid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return _serve_att(db, aid, "teacher", current_user, inline=True)


@router.get("/api/teacher/homework-attachments/{aid}/download")
def teacher_att_dl(aid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return _serve_att(db, aid, "teacher", current_user, inline=False)


@router.get("/api/teacher/homework-student/{sid}")
def teacher_student_profile(sid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Student ka bio + kitne homework bheje (teacher tracker se naam pe click)."""
    from homework_models import HomeworkSubmission
    from models import StudentProfile
    tp = _tp(db, current_user)
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student not found.")
    rows = db.query(HomeworkSubmission).filter(HomeworkSubmission.teacher_id == tp.id,
                                               HomeworkSubmission.student_id == sid) \
        .order_by(HomeworkSubmission.id.desc()).all()
    by = {"total": len(rows), "pending": 0, "under_review": 0, "changes_required": 0, "checking_done": 0}
    for m in rows:
        if m.status in _PENDING:
            by["pending"] += 1
        elif m.status in by:
            by[m.status] += 1
    return {"student": {"id": sp.id, "name": (sp.user.name if sp.user else "Student"),
                        "phone": sp.phone, "class_name": sp.class_name,
                        "class_level": sp.class_level, "batch_name": sp.batch_name},
            "counts": by,
            "homework": [_sub_dict(db, m) for m in rows]}
