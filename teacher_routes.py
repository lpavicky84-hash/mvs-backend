import json
import re
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_teacher, get_current_user
import grading
from models import (
    User, TeacherProfile, ClassEntry, ClassStatus,
    RescheduleRequest, RescheduleStatus, DPP, Test, Doubt,
    DoubtStatus, Timetable, Notification, TestStatus,
    Exam, ExamQuestion, ExamAttempt, ExamResult
)
from schemas import (
    ClassEntryCreate, ClassEntryUpdate, ClassEntryOut,
    TimetableCreate, TimetableOut,
    RescheduleCreate, RescheduleOut,
    DPPCreate, DPPOut,
    TestCreate, TestPaperUpload, TestOut,
    DoubtResolve, DoubtOut,
    TeacherDashboard
)

router = APIRouter(prefix="/api/teacher", tags=["Teacher"])

def get_teacher_profile(user, db):
    profile = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Teacher profile nahi mila")
    return profile

def notify(db, user_id: int, title: str, message: str, notif_type: str):
    """Helper to create notification"""
    n = Notification(user_id=user_id, title=title, message=message, notif_type=notif_type)
    db.add(n)

# ===== DASHBOARD =====
@router.get("/dashboard", response_model=TeacherDashboard)
def teacher_dashboard(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    now = datetime.now()
    month_start = date(now.year, now.month, 1)
    week_start  = date.today() - timedelta(days=date.today().weekday())

    q = db.query(ClassEntry).filter(ClassEntry.teacher_id == tp.id)

    total_done      = q.filter(ClassEntry.status == ClassStatus.done).count()
    total_pending   = q.filter(ClassEntry.status == ClassStatus.pending).count()
    total_rescheduled = q.filter(ClassEntry.status == ClassStatus.rescheduled).count()
    monthly_done    = q.filter(ClassEntry.status == ClassStatus.done, ClassEntry.scheduled_date >= month_start).count()
    monthly_pending = q.filter(ClassEntry.status == ClassStatus.pending, ClassEntry.scheduled_date >= month_start).count()
    weekly_done     = q.filter(ClassEntry.status == ClassStatus.done, ClassEntry.scheduled_date >= week_start).count()

    # Reset monthly reschedule counter if new month
    if tp.reschedule_reset_month != now.month:
        tp.reschedule_count_this_month = 0
        tp.reschedule_reset_month = now.month
        db.commit()

    total_dpps  = db.query(DPP).filter(DPP.teacher_id == tp.id).count()
    total_tests = db.query(Test).filter(Test.teacher_id == tp.id).count()
    unresolved  = db.query(Doubt).filter(Doubt.teacher_id == tp.id, Doubt.status == DoubtStatus.pending).count()

    return TeacherDashboard(
        total_done=total_done, total_pending=total_pending,
        total_rescheduled=total_rescheduled, monthly_done=monthly_done,
        monthly_pending=monthly_pending, weekly_done=weekly_done,
        reschedule_this_month=tp.reschedule_count_this_month,
        total_dpps=total_dpps, total_tests=total_tests,
        unresolved_doubts=unresolved
    )

# ===== TIMETABLE =====
@router.post("/timetable", response_model=TimetableOut)
def add_timetable(req: TimetableCreate, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    entry = Timetable(teacher_id=tp.id, **req.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

@router.get("/timetable", response_model=List[TimetableOut])
def get_timetable(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return db.query(Timetable).filter(Timetable.teacher_id == tp.id, Timetable.is_active == True).all()

# ===== CLASSES =====
@router.post("/classes", response_model=ClassEntryOut)
def create_class(req: ClassEntryCreate, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    entry = ClassEntry(teacher_id=tp.id, **req.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry

@router.get("/classes", response_model=List[ClassEntryOut])
def get_classes(
    status: Optional[str] = None,
    subject: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    tp = get_teacher_profile(current_user, db)
    q = db.query(ClassEntry).filter(ClassEntry.teacher_id == tp.id)
    if status:
        q = q.filter(ClassEntry.status == status)
    if subject:
        q = q.filter(ClassEntry.subject == subject)
    return q.order_by(ClassEntry.scheduled_date, ClassEntry.scheduled_time).all()

@router.get("/classes/today", response_model=List[ClassEntryOut])
def get_today_classes(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return db.query(ClassEntry).filter(
        ClassEntry.teacher_id == tp.id,
        ClassEntry.scheduled_date == date.today()
    ).order_by(ClassEntry.scheduled_time).all()

@router.patch("/classes/{class_id}/upload", response_model=ClassEntryOut)
def upload_class_pdf(
    class_id: int,
    drive_link: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    """Teacher uploads PDF → status auto-Done"""
    tp = get_teacher_profile(current_user, db)
    entry = db.query(ClassEntry).filter(
        ClassEntry.id == class_id,
        ClassEntry.teacher_id == tp.id
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Class nahi mili")
    entry.drive_link = drive_link
    entry.status = ClassStatus.done
    db.commit()
    db.refresh(entry)
    return entry

# ===== RESCHEDULE =====
@router.post("/reschedule", response_model=RescheduleOut)
def request_reschedule(req: RescheduleCreate, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    now = datetime.now()

    # Reset if new month
    if tp.reschedule_reset_month != now.month:
        tp.reschedule_count_this_month = 0
        tp.reschedule_reset_month = now.month

    # NOTE: Monthly reschedule limit hata di gayi hai. Teacher jitni baar chahe
    # reschedule request bhej sakta hai — har request admin approval par hi
    # apply hoti hai. Count sirf tracking/reporting ke liye rakha gaya hai.

    class_entry = db.query(ClassEntry).filter(
        ClassEntry.id == req.class_entry_id,
        ClassEntry.teacher_id == tp.id
    ).first()
    if not class_entry:
        raise HTTPException(status_code=404, detail="Class nahi mili")

    # Check existing pending request
    existing = db.query(RescheduleRequest).filter(
        RescheduleRequest.class_entry_id == req.class_entry_id,
        RescheduleRequest.status == RescheduleStatus.pending
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Is class ke liye pehle se request pending hai")

    # Mark class as rescheduled (pending admin approval)
    class_entry.status = ClassStatus.rescheduled

    rs = RescheduleRequest(
        class_entry_id=req.class_entry_id,
        teacher_id=tp.id,
        original_date=class_entry.scheduled_date,
        original_time=class_entry.scheduled_time,
        new_date=req.new_date,
        new_time=req.new_time,
        reason=req.reason,
        status=RescheduleStatus.pending
    )
    db.add(rs)

    # Notify all admins
    admins = db.query(User).filter(User.role == "admin").all()
    for admin in admins:
        notify(db, admin.id,
               f"Reschedule Request — {current_user.name}",
               f"{class_entry.subject} ({class_entry.class_name}) ko {req.new_date} pe reschedule karna chahte hain. Reason: {req.reason}",
               "reschedule_request")

    db.commit()
    db.refresh(rs)
    return rs

@router.get("/reschedule", response_model=List[RescheduleOut])
def get_my_reschedules(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return db.query(RescheduleRequest).filter(RescheduleRequest.teacher_id == tp.id).all()

# ===== DPP =====
@router.post("/dpp", response_model=DPPOut)
def upload_dpp(req: DPPCreate, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    dpp = DPP(teacher_id=tp.id, **req.model_dump())
    db.add(dpp)
    db.commit()
    db.refresh(dpp)
    return dpp

@router.get("/dpp", response_model=List[DPPOut])
def get_dpps(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return db.query(DPP).filter(DPP.teacher_id == tp.id).all()

# ===== TESTS =====
@router.post("/tests", response_model=TestOut)
def create_test(req: TestCreate, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    test = Test(teacher_id=tp.id, **req.model_dump())
    db.add(test)
    db.commit()
    db.refresh(test)
    return test

@router.patch("/tests/{test_id}/upload-paper")
def upload_question_paper(
    test_id: int,
    drive_link: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    """Upload question paper — must be 15 min before test"""
    tp = get_teacher_profile(current_user, db)
    test = db.query(Test).filter(Test.id == test_id, Test.teacher_id == tp.id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Test nahi mila")
    test.question_paper_link = drive_link
    test.status = TestStatus.active
    db.commit()
    return {"message": "Question paper upload ho gaya! Students ko access mil gayi.", "drive_link": drive_link}

@router.get("/tests", response_model=List[TestOut])
def get_tests(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return db.query(Test).filter(Test.teacher_id == tp.id).all()

# ===== DOUBTS =====
@router.get("/doubts")
def get_doubts(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    tp = get_teacher_profile(current_user, db)
    q = db.query(Doubt).filter(Doubt.teacher_id == tp.id)
    if status:
        q = q.filter(Doubt.status == status)
    out = []
    for d in q.order_by(Doubt.created_at.desc()).all():
        sname = d.student.user.name if d.student and d.student.user else "Student"
        out.append({"id": d.id, "student_name": sname, "subject": d.subject, "topic": d.topic,
                    "question": d.question, "has_image": bool(d.image_b64),
                    "attach_mime": d.attach_mime, "attach_name": d.attach_name,
                    "has_voice": bool(d.audio_b64), "has_answer_voice": bool(d.answer_audio_b64),
                    "has_answer_file": bool(d.answer_attach_b64), "answer_attach_mime": d.answer_attach_mime,
                    "answer": d.answer, "status": d.status.value if hasattr(d.status, "value") else d.status,
                    "created_at": str(d.created_at)[:16]})
    return out

def _t_doubt_media(b64, mime, name):
    import base64
    from fastapi import Response
    if not b64:
        raise HTTPException(status_code=404, detail="Not found")
    safe = (name or "file").replace('"', "")
    return Response(content=base64.b64decode(b64),
                    media_type=mime or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{safe}"'})

def _t_own_doubt(did, db, current_user):
    tp = get_teacher_profile(current_user, db)
    d = db.query(Doubt).filter(Doubt.id == did, Doubt.teacher_id == tp.id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Doubt not found")
    return d

@router.get("/doubt/{did}/image")
def teacher_doubt_image(did: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    d = _t_own_doubt(did, db, current_user)
    return _t_doubt_media(d.image_b64, d.attach_mime or "image/jpeg", d.attach_name)

@router.get("/doubt/{did}/voice")
def teacher_doubt_voice(did: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    d = _t_own_doubt(did, db, current_user)
    return _t_doubt_media(d.audio_b64, "audio/webm", "voice.webm")

@router.get("/doubt/{did}/answer-voice")
def teacher_doubt_answer_voice(did: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    d = _t_own_doubt(did, db, current_user)
    return _t_doubt_media(d.answer_audio_b64, "audio/webm", "answer.webm")

@router.get("/doubt/{did}/answer-file")
def teacher_doubt_answer_file(did: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    d = _t_own_doubt(did, db, current_user)
    return _t_doubt_media(d.answer_attach_b64, d.answer_attach_mime, d.answer_attach_name)

@router.patch("/doubts/{doubt_id}/resolve")
def resolve_doubt(
    doubt_id: int,
    req: DoubtResolve,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    tp = get_teacher_profile(current_user, db)
    doubt = db.query(Doubt).filter(Doubt.id == doubt_id, Doubt.teacher_id == tp.id).first()
    if not doubt:
        raise HTTPException(status_code=404, detail="Doubt nahi mila")
    doubt.answer = req.answer
    doubt.answer_image_link = req.answer_image_link
    if req.answer_audio_b64:
        doubt.answer_audio_b64 = req.answer_audio_b64
    if req.answer_attach_b64:
        doubt.answer_attach_b64 = req.answer_attach_b64
        doubt.answer_attach_mime = req.answer_attach_mime or "application/octet-stream"
        doubt.answer_attach_name = (req.answer_attach_name or "attachment")[:250]
    doubt.status = DoubtStatus.resolved
    doubt.resolved_at = datetime.now()

    # Notify student
    student_user = db.query(User).filter(User.id == doubt.student.user_id).first()
    if student_user:
        notify(db, student_user.id,
               "Aapka Doubt Resolve Ho Gaya! ✅",
               f"{doubt.subject} — {doubt.topic}: {current_user.name} ne jawab de diya.",
               "doubt_resolved")
    db.commit()
    return {"message": "Doubt resolve kar diya! Student ko notification chali gayi."}

# ===== NOTIFICATIONS =====
@router.get("/notifications")
def get_notifications(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    notifs = db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).order_by(Notification.created_at.desc()).limit(20).all()
    return notifs

@router.patch("/notifications/{notif_id}/read")
def mark_read(notif_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    n = db.query(Notification).filter(Notification.id == notif_id, Notification.user_id == current_user.id).first()
    if n:
        n.is_read = True
        db.commit()
    return {"message": "Read mark ho gaya"}

# ===== TIMETABLE ENTRIES (chapter + parts + date + day) =====
def _serialize_tt(e):
    return {
        "id": e.id, "subject": e.subject, "class_name": e.class_name,
        "chapter": e.chapter, "part": e.part,
        "date": str(e.entry_date) if e.entry_date else None, "day": e.day,
        "time": getattr(e, "time_text", None), "type": getattr(e, "entry_type", None) or "chapter", "status": getattr(e, "status", None) or "approved",
        "completed": bool(getattr(e, "completed", False)),
        "topic_covered": getattr(e, "topic_covered", None),
        "homework": getattr(e, "homework", None),
        "dpp_given": bool(getattr(e, "dpp_given", False)),
        "remarks": getattr(e, "remarks", None),
        "start_time": getattr(e, "start_time", None),
        "end_time": getattr(e, "end_time", None),
        "completed_at": str(getattr(e, "completed_at", "")) if getattr(e, "completed_at", None) else None
    }

@router.post("/timetable-entry")
def add_tt_entry(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    edate = None
    d = (payload.get("entry_date") or "").strip()
    if d:
        try:
            edate = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            edate = None
    e = TimetableEntry(
        teacher_id=tp.id,
        subject=(payload.get("subject") or "").strip(),
        class_name=(payload.get("class_name") or "").strip(),
        chapter=(payload.get("chapter") or "").strip(),
        part=(payload.get("part") or "").strip() or None,
        entry_date=edate,
        day=(payload.get("day") or "").strip() or None,
        time_text=(payload.get("time") or "").strip() or None,
        entry_type=(payload.get("type") or "chapter").strip()
    )
    db.add(e); db.commit(); db.refresh(e)
    return _serialize_tt(e)

@router.get("/timetable-entries")
def list_tt_entries(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    es = db.query(TimetableEntry).filter(TimetableEntry.teacher_id == tp.id).order_by(
        TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.entry_date
    ).all()
    return [_serialize_tt(e) for e in es]

@router.delete("/timetable-entry/{entry_id}")
def delete_tt_entry(entry_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher apni timeline me dikhne wali koi bhi class delete kar sakta hai —
    apni uploaded ya admin-uploaded (subject match), same scope jaise my-timetable."""
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id).first()
    if not e or (e.subject not in (tp.subjects or []) and e.teacher_id != tp.id):
        raise HTTPException(status_code=404, detail="Entry not found")
    db.delete(e); db.commit()
    return {"message": "Class deleted"}

@router.delete("/timetable-entries/all")
def clear_tt_entries(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    db.query(TimetableEntry).filter(TimetableEntry.teacher_id == tp.id).delete()
    db.commit()
    return {"message": "Saari entries clear ho gayi"}

# ===== PDF TIMETABLE UPLOAD (auto-parse) =====
@router.post("/timetable-pdf")
async def upload_timetable_pdf(
    file: UploadFile = File(...),
    class_name: str = Form("Class 12"),
    subject: str = Form(""),
    replace: str = Form("false"),
    preview: str = Form("false"),
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    import tt_parser
    raw = await file.read()
    try:
        rows = tt_parser.parse_pdf(raw, force_subject=(subject.strip() or None))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF parse error: {e}")
    if not rows:
        raise HTTPException(status_code=400, detail="PDF se koi valid row nahi mili. Text-based PDF honi chahiye.")

    subjects_found = sorted(set(r["subject"] for r in rows))
    # preview mode: sirf parsed rows dikhao, DB me kuch save mat karo
    if preview.lower() == "true":
        return {"added": 0, "subjects": subjects_found, "preview": rows}
    # replace sirf SAME CLASS ki entries hatao — dusri class ka same-name subject alag timetable hai
    if replace.lower() == "true":
        db.query(TimetableEntry).filter(
            TimetableEntry.teacher_id == tp.id,
            TimetableEntry.subject.in_(subjects_found),
            TimetableEntry.class_name == class_name
        ).delete(synchronize_session=False)

    added = 0
    for r in rows:
        edate = None
        try:
            edate = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except Exception:
            pass
        db.add(TimetableEntry(
            teacher_id=tp.id, subject=r["subject"], class_name=class_name,
            chapter=r["chapter"], part=r["part"], entry_date=edate,
            day=r["day"] or None, time_text=r["time"] or None, entry_type=r["type"]
        ))
        added += 1
    db.commit()
    return {"added": added, "subjects": subjects_found}

# ===== TEACHER: TIMETABLE PDF COMMIT (preview edit ke baad final save) =====
@router.post("/timetable-pdf-commit")
def teacher_timetable_pdf_commit(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    rows = payload.get("rows") or []
    class_name = (payload.get("class_name") or "Class 12").strip()
    replace = str(payload.get("replace") or "false")
    clean = []
    for r in rows:
        sub = (r.get("subject") or "").strip()
        ch = (r.get("chapter") or "").strip()
        if not sub or not ch:
            continue
        clean.append({"subject": sub, "chapter": ch, "part": (r.get("part") or "").strip() or None,
                      "date": r.get("date") or "", "day": (r.get("day") or "").strip(),
                      "time": (r.get("time") or "").strip(), "type": r.get("type") or "chapter"})
    if not clean:
        raise HTTPException(status_code=400, detail="Koi valid row nahi bachi — kam se kam 1 chapter rakho.")
    subjects_found = sorted(set(r["subject"] for r in clean))
    if replace.lower() == "true":
        db.query(TimetableEntry).filter(
            TimetableEntry.teacher_id == tp.id,
            TimetableEntry.subject.in_(subjects_found),
            TimetableEntry.class_name == class_name
        ).delete(synchronize_session=False)
    added = 0
    for r in clean:
        edate = None
        try: edate = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except Exception: pass
        db.add(TimetableEntry(
            teacher_id=tp.id, subject=r["subject"], class_name=class_name,
            chapter=r["chapter"], part=r["part"], entry_date=edate,
            day=r["day"] or None, time_text=r["time"] or None, entry_type=r["type"]
        ))
        added += 1
    db.commit()
    return {"added": added, "subjects": subjects_found}

# ===== TEACHER: EDIT TIMETABLE ENTRY TOPIC/PART =====
@router.patch("/timetable-entry/{entry_id}")
def edit_tt_entry(entry_id: int, payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id).first()
    if not e or (e.subject not in (tp.subjects or []) and e.teacher_id != tp.id):
        raise HTTPException(status_code=404, detail="Entry nahi mili")
    if "part" in payload:
        e.part = (payload.get("part") or "").strip() or None
    if "time" in payload:
        e.time_text = (payload.get("time") or "").strip() or None
    if "chapter" in payload and (payload.get("chapter") or "").strip():
        e.chapter = (payload.get("chapter") or "").strip()
    if "type" in payload and (payload.get("type") or "").strip() in ("chapter", "event"):
        e.entry_type = (payload.get("type") or "").strip()
    if "entry_date" in payload:
        d = (payload.get("entry_date") or "").strip()
        if d:
            try:
                e.entry_date = datetime.strptime(d, "%Y-%m-%d").date()
                e.day = e.entry_date.strftime("%A")
            except Exception:
                raise HTTPException(status_code=400, detail="Date must be in YYYY-MM-DD format")
        else:
            e.entry_date = None
    db.commit()
    return _serialize_tt(e)

# ===== TEACHER: DELETE OWN SUBJECT TIMETABLE (one click, type-to-confirm on frontend) =====
@router.delete("/timetable-subject")
def teacher_delete_tt_subject(subject: str, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher apne kisi subject ka poora timetable ek click me delete kar sakta hai.
    Scope wahi hai jo my-timetable me dikhta hai: subject unke assigned subjects me
    hona chahiye (ya unki khud ki uploaded entries). Doosre subjects delete nahi hote."""
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    from sqlalchemy import or_
    subject = (subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")
    if subject not in (tp.subjects or []):
        owned = db.query(TimetableEntry).filter(
            TimetableEntry.subject == subject, TimetableEntry.teacher_id == tp.id).first()
        if not owned:
            raise HTTPException(status_code=403, detail="This subject is not assigned to you")
    n = db.query(TimetableEntry).filter(TimetableEntry.subject == subject).delete(synchronize_session=False)
    db.commit()
    return {"deleted": n, "message": f"{n} entries deleted for {subject}"}

# ===== TEACHER: SEND NOTIFICATION TO STUDENTS =====
@router.post("/notify")
def teacher_notify(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import User
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title aur message zaroori hain")
    students = db.query(User).filter(User.is_active == True, User.role == "student").all()
    sender = "👨‍🏫 " + current_user.name
    for s in students:
        notify(db, s.id, sender + ": " + title, message, "teacher_message")
    db.commit()
    return {"message": f"{len(students)} students ko bhej di!", "count": len(students)}

# ===== STUDY MATERIAL (PDF upload to DB) =====
@router.post("/material")
async def upload_material(
    file: UploadFile = File(...),
    subject: str = Form(...),
    class_name: str = Form("Class 12"),
    chapter: str = Form(""),
    material_type: str = Form("notes"),   # notes | dpp | test | other
    title: str = Form(""),
    category: str = Form(""),
    duration_min: int = Form(0),
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    import base64
    from models import Material
    tp = get_teacher_profile(current_user, db)
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File 20MB se badi hai. Chhoti PDF use karein.")
    b64 = base64.b64encode(raw).decode("ascii")
    m = Material(
        teacher_id=tp.id, teacher_name=current_user.name, subject=subject.strip(),
        class_name=class_name.strip(), chapter=chapter.strip(),
        material_type=material_type.strip(), title=(title.strip() or file.filename),
        category=(category.strip() or None),
        filename=file.filename, content_b64=b64,
        duration_min=(duration_min or None)
    )
    db.add(m); db.commit(); db.refresh(m)
    # Notify students who have this subject
    try:
        from models import StudentProfile
        label = {"notes": "Class Notes", "dpp": "DPP", "test": "Test"}.get(m.material_type, (m.category or "Material"))
        sps = db.query(StudentProfile).all()
        for sp in sps:
            if sp.subjects and subject.strip() in sp.subjects and sp.user:
                notify(db, sp.user.id, f"📚 New {label}: {subject.strip()}",
                       f"{current_user.name} ne {subject.strip()} ({chapter.strip() or 'General'}) ke liye {label} upload ki hai. Materials section mein dekho!",
                       "new_material")
        db.commit()
    except Exception:
        db.rollback()
    return {"id": m.id, "message": "Upload ho gaya!"}

@router.get("/materials")
def teacher_materials(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material
    tp = get_teacher_profile(current_user, db)
    ms = db.query(Material).filter(Material.teacher_id == tp.id,
                                   Material.material_type != "answer").order_by(Material.created_at.desc()).all()
    return [{"id": m.id, "subject": m.subject, "chapter": m.chapter, "type": m.material_type,
             "title": m.title, "filename": m.filename, "duration_min": m.duration_min,
             "date": str(m.created_at)[:10]} for m in ms]

@router.get("/chapter-status")
def chapter_status(subject: str, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """For a subject, list chapters from timetable + whether notes/dpp uploaded."""
    from models import TimetableEntry, Material
    chapters = [r[0] for r in db.query(TimetableEntry.chapter).filter(
        TimetableEntry.subject == subject,
        TimetableEntry.entry_type == "chapter").distinct().all() if r[0]]
    mats = db.query(Material).filter(Material.subject == subject).all()
    out = []
    for ch in chapters:
        notes = any(m.chapter == ch and m.material_type == "notes" for m in mats)
        dpp = any(m.chapter == ch and m.material_type == "dpp" for m in mats)
        out.append({"chapter": ch, "notes": notes, "dpp": dpp})
    return out

@router.get("/material/{mid}/download")
def teacher_download(mid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m: raise HTTPException(status_code=404, detail="Nahi mila")
    data = base64.b64decode(m.content_b64)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{m.filename or "file.pdf"}"'})

@router.delete("/material/{mid}")
def delete_material(mid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material
    tp = get_teacher_profile(current_user, db)
    m = db.query(Material).filter(Material.id == mid, Material.teacher_id == tp.id).first()
    if not m: raise HTTPException(status_code=404, detail="Nahi mila")
    db.delete(m); db.commit()
    return {"message": "Delete ho gaya"}

# ===== TEACHER PROFILE & SUBJECT SELECTION (class-wise) =====
@router.get("/profile")
def teacher_profile(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    sc = tp.subject_classes or []
    return {
        "name": current_user.name,
        "user_id": current_user.user_id,
        "gender": tp.gender,
        "subjects": tp.subjects or [],
        "subject_classes": sc,
        "needs_subjects": len(sc) == 0
    }

@router.get("/available-subjects")
def teacher_available_subjects(class_level: str, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import AvailableSubject
    subs = db.query(AvailableSubject).filter(
        AvailableSubject.class_level == class_level, AvailableSubject.is_active == True).all()
    return [{"name": s.name, "code": s.code} for s in subs]

@router.post("/set-subjects")
def teacher_set_subjects(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    selections = payload.get("selections", [])   # [{"subject":..,"class":"10"/"12"}]
    if not selections:
        raise HTTPException(status_code=400, detail="Kam se kam 1 subject select karein")
    tp.subject_classes = selections
    tp.subjects = sorted({s.get("subject") for s in selections if s.get("subject")})
    db.commit()
    return {"message": "Subjects save ho gaye!", "subjects": tp.subjects}

# ===== TEACHER: VIEW TIMETABLE (by their subjects, admin-uploaded) =====
@router.get("/my-timetable")
def my_timetable(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    if not subs:
        return []
    from sqlalchemy import or_
    es = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs),
        or_(TimetableEntry.status==None, TimetableEntry.status!='pending')).order_by(
        TimetableEntry.subject, TimetableEntry.entry_date).all()
    return [_serialize_tt(e) for e in es]

# ===== TEACHER: TODAY'S CLASSES with material status =====
@router.get("/today-classes")
def today_classes(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry, Material
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    if not subs:
        return []
    today = date.today()
    from sqlalchemy import or_
    es = db.query(TimetableEntry).filter(
        TimetableEntry.subject.in_(subs), TimetableEntry.entry_date == today,
        or_(TimetableEntry.status==None, TimetableEntry.status!='pending')).all()
    mats = db.query(Material).filter(Material.subject.in_(subs)).all()
    out = []
    for e in es:
        notes = any(m.chapter == e.chapter and m.subject == e.subject and m.material_type == "notes" for m in mats)
        dpp = any(m.chapter == e.chapter and m.subject == e.subject and m.material_type == "dpp" for m in mats)
        d = _serialize_tt(e); d["notes"] = notes; d["dpp"] = dpp
        out.append(d)
    out.sort(key=lambda x: x.get("time") or "")
    return out

# ===== TEACHER: REQUEST EXTRA CLASS (needs admin approval) =====
@router.post("/request-class")
def request_class(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry, User, UserRole, Notification
    tp = get_teacher_profile(current_user, db)
    subject = (payload.get("subject") or "").strip()
    if subject not in (tp.subjects or []):
        raise HTTPException(status_code=400, detail="Yeh aapka subject nahi hai")
    edate = None
    if payload.get("date"):
        try:
            from datetime import datetime as _dt
            edate = _dt.strptime(payload["date"], "%Y-%m-%d").date()
        except Exception:
            pass
    day = edate.strftime("%a") if edate else None
    e = TimetableEntry(
        teacher_id=tp.id, subject=subject, class_name=payload.get("class_name", "Class 12"),
        chapter=(payload.get("chapter") or payload.get("topic") or "Extra Class").strip(),
        part=((payload.get("topic") or "").strip() or None),
        entry_date=edate, day=day, time_text=(payload.get("time") or "").strip() or None,
        entry_type="chapter", status="pending"
    )
    db.add(e); db.flush()
    # notify admins
    for adm in db.query(User).filter(User.role == UserRole.admin).all():
        db.add(Notification(user_id=adm.id, title="New Extra Class Request",
                            message=f"{current_user.name} ne {subject} ki extra class request ki hai ({payload.get('date','')} {payload.get('time','')}). Approve karein.",
                            notif_type="class_request"))
    db.commit(); db.refresh(e)
    return {"id": e.id, "message": "Request admin ko bhej di! Approve hote hi timetable mein aa jayegi."}

# ===== TEACHER: SUBJECT-WISE STUDENT COUNTS =====
@router.get("/student-counts")
def teacher_student_counts(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import StudentProfile
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    sc = tp.subject_classes or []
    students = db.query(StudentProfile).all()
    out = []
    for s in subs:
        cnt = sum(1 for sp in students if sp.subjects and s in sp.subjects)
        cls = next((x.get("class") for x in sc if x.get("subject") == s), None)
        out.append({"subject": s, "class": cls, "count": cnt})
    out.sort(key=lambda x: -x["count"])
    return {"total": sum(o["count"] for o in out), "subjects": out}

# ===== TEACHER: VIEW SUBMISSIONS + GIVE MARKS =====
@router.get("/dpp-results")
def teacher_dpp_results(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Every DPP the teacher uploaded, with submission counts (submitted /
    checked / pending) so the DPP Result page can show progress at a glance."""
    from models import Material, MaterialView, StudentProfile
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    dpps = db.query(Material).filter(
        Material.material_type == "dpp",
        Material.subject.in_(subs)).order_by(Material.created_at.desc()).all() if subs else []
    ids = [m.id for m in dpps]
    answers = db.query(Material).filter(
        Material.material_type == "answer",
        Material.parent_id.in_(ids)).all() if ids else []
    # how many students should be doing each DPP (same subject)
    roster = {}
    for sp in db.query(StudentProfile).all():
        for s in (sp.subjects or []):
            roster[s] = roster.get(s, 0) + 1
    views, downloads = {}, {}
    if ids:
        for v in db.query(MaterialView).filter(MaterialView.material_id.in_(ids)).all():
            (downloads if v.action == "download" else views).setdefault(v.material_id, set()).add(v.student_id)
    out = []
    for m in dpps:
        mine = [a for a in answers if a.parent_id == m.id]
        checked = sum(1 for a in mine if (a.marks or "").strip())
        total_students = roster.get(m.subject, 0)
        out.append({
            "id": m.id, "subject": m.subject, "chapter": m.chapter, "part": m.part,
            "title": m.title, "filename": m.filename,
            "date": str(m.created_at)[:10] if m.created_at else "",
            "views": len(views.get(m.id, ())), "downloads": len(downloads.get(m.id, ())),
            "submitted": len(mine), "checked": checked, "pending": len(mine) - checked,
            "total_students": total_students,
        })
    totals = {
        "dpps": len(out),
        "submitted": sum(o["submitted"] for o in out),
        "checked": sum(o["checked"] for o in out),
        "pending": sum(o["pending"] for o in out),
    }
    return {"totals": totals, "rows": out}


@router.get("/submissions")
def teacher_submissions(parent_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material
    subs = db.query(Material).filter(Material.material_type == "answer",
                                     Material.parent_id == parent_id).order_by(Material.created_at.desc()).all()
    return [{"id": m.id, "student_name": m.student_name, "marks": m.marks,
             "date": str(m.created_at)[:16]} for m in subs]

@router.post("/submission/{sid}/marks")
def set_marks(sid: int, payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material, StudentProfile, Notification
    m = db.query(Material).filter(Material.id == sid, Material.material_type == "answer").first()
    if not m:
        raise HTTPException(status_code=404, detail="Submission nahi mili")
    m.marks = str(payload.get("marks", "")).strip()
    # notify student
    if m.student_id:
        sp = db.query(StudentProfile).filter(StudentProfile.id == m.student_id).first()
        if sp and sp.user:
            db.add(Notification(user_id=sp.user.id, title="DPP Checked!",
                                message=f"{current_user.name} ne aapki {m.subject} DPP check ki. Marks: {m.marks}",
                                notif_type="marks"))
    db.commit()
    return {"message": "Marks save ho gaye!"}

# ===== TEACHER: OWN PHOTO + MY STUDENTS LIST =====
@router.get("/my-photo")
def teacher_my_photo(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    import base64
    from fastapi import Response
    tp = get_teacher_profile(current_user, db)
    if not tp.photo_b64:
        raise HTTPException(status_code=404, detail="Photo nahi")
    return Response(content=base64.b64decode(tp.photo_b64), media_type="image/jpeg")

@router.get("/student/{sid}/photo")
def teacher_student_photo(sid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    import base64
    from fastapi import Response
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp or not sp.photo_b64:
        raise HTTPException(status_code=404, detail="Photo nahi")
    return Response(content=base64.b64decode(sp.photo_b64), media_type="image/jpeg")

def _subj_key(name):
    """Subject naam ko compare karne layak banata hai: extra space, case aur
    "(Class 12)" jaisa suffix hata deta hai. Excel upload se aksar "Physics "
    jaise trailing space aa jaate the aur student teacher ki list me hi
    nahi dikhta tha."""
    t = str(name or "")
    t = re.sub(r"\((?:class\s*)?\d+(?:th)?\)", " ", t, flags=re.I)
    t = re.sub(r"[^a-z0-9]+", " ", t.lower())
    return " ".join(t.split()).strip()


@router.get("/my-students-list")
def teacher_my_students_list(q: str = "", subject: str = "", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import StudentProfile
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    sub_keys = {_subj_key(x) for x in subs if _subj_key(x)}
    want = _subj_key(subject) if subject else ""
    ql = " ".join((q or "").split()).strip().lower()
    q_tokens = [t for t in ql.split(" ") if t]
    rows = db.query(StudentProfile).all()
    out = []
    for sp in rows:
        ssubs = sp.subjects or []
        matched = [x for x in ssubs if _subj_key(x) in sub_keys]
        if not matched:
            continue
        if want and want not in {_subj_key(x) for x in ssubs}:
            continue
        nm = (sp.user.name if sp.user else "") or ""
        if q_tokens:
            hay = " ".join([
                " ".join(nm.split()).lower(),
                (sp.phone or ""),
                (sp.user.user_id if sp.user else "") or "",
                (sp.batch_name or ""),
            ]).lower()
            # har shabd alag se match ho - "tanu sharma" "TANU  SHARMA" se bhi mile
            if not all(t in hay for t in q_tokens):
                continue
        out.append({"id": sp.id, "name": nm, "phone": sp.phone, "class": sp.class_level,
                    "user_id": (sp.user.user_id if sp.user else None),
                    "batch": sp.batch_name, "medium": sp.medium,
                    "email": sp.email,
                    "subjects": matched, "has_photo": bool(sp.photo_b64)})
    out.sort(key=lambda x: (x["name"] or "").lower())
    return {"total": len(out), "students": out}

# ===== TEACHER -> ADMIN MESSAGE =====
@router.post("/message-admin")
def teacher_message_admin(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required")
    admins = db.query(User).filter(User.role == "admin").all()
    sender = current_user.name
    for a in admins:
        notify(db, a.id, f"\u2709\ufe0f {sender}: {title}", message, "teacher_to_admin")
    db.commit()
    return {"message": "Message sent to the admin"}

# ===== TEACHER ACCOUNTABILITY: classes with status, mark-complete, compliance =====
def _class_status(e):
    """Upcoming | Live | Completed | Missed based on date/time + completed flag."""
    if getattr(e, "completed", False):
        return "Completed"
    today = date.today()
    if e.entry_date is None:
        return "Upcoming"
    if e.entry_date < today:
        return "Missed"
    if e.entry_date > today:
        return "Upcoming"
    return "Pending"  # today, not yet completed

@router.get("/my-classes")
def teacher_my_classes(scope: str = "all", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry
    from sqlalchemy import or_
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    if not subs:
        return []
    q = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs),
        or_(TimetableEntry.status == None, TimetableEntry.status != "pending"))
    if scope == "today":
        q = q.filter(TimetableEntry.entry_date == date.today())
    es = q.order_by(TimetableEntry.entry_date, TimetableEntry.time_text).all()
    out = []
    for e in es:
        d = _serialize_tt(e); d["live_status"] = _class_status(e)
        out.append(d)
    return out

@router.post("/class/{entry_id}/complete")
def teacher_complete_class(entry_id: int, payload: dict, background_tasks: BackgroundTasks = None,
                           db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry
    tp = get_teacher_profile(current_user, db)
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id).first()
    if not e or e.subject not in (tp.subjects or []):
        raise HTTPException(status_code=404, detail="Class not found")
    e.completed = True
    e.completed_at = datetime.now()
    e.topic_covered = (payload.get("topic_covered") or e.chapter or "").strip() or None
    e.start_time = (payload.get("start_time") or "").strip() or None
    e.end_time = (payload.get("end_time") or "").strip() or None
    e.homework = (payload.get("homework") or "").strip() or None
    e.dpp_given = bool(payload.get("dpp_given"))
    e.remarks = (payload.get("remarks") or "").strip() or None
    db.commit()
    _maybe_warn_late(db, tp, e)
    if background_tasks is not None:
        background_tasks.add_task(_notify_class_done, e.subject, e.chapter or "",
                                  e.part or "", current_user.name or "Your teacher",
                                  bool(e.dpp_given))
    return {"message": "Class marked as completed."}


_LATE_WARN_THRESHOLD = 2      # more than this many late starts in a month -> remind


def _maybe_warn_late(db, tp, entry):
    """If this teacher has now started more than _LATE_WARN_THRESHOLD classes late
    this month, remind them once. Repeat delays hurt the institute's reputation and
    unsettle students, so the reminder is sent automatically."""
    try:
        from models import TimetableEntry
        d = _delay_of(entry)
        if _delay_band(d) not in ("minor", "late"):
            return
        today = date.today()
        month_start = date(today.year, today.month, 1)
        rows = db.query(TimetableEntry).filter(
            TimetableEntry.teacher_id == tp.id,
            TimetableEntry.completed == True,
            TimetableEntry.entry_date >= month_start).all()
        late = sum(1 for r in rows if _delay_band(_delay_of(r)) in ("minor", "late"))
        if late <= _LATE_WARN_THRESHOLD or not tp.user:
            return
        title = "\u26a0\ufe0f Class Punctuality Reminder"
        # only remind once a month
        seen = db.query(Notification).filter(
            Notification.user_id == tp.user.id, Notification.title == title,
            Notification.created_at >= datetime(today.year, today.month, 1)).first()
        if seen:
            return
        msg = ("Is mahine aapki %d classes late shuru hui hain.\n\n"
               "Isse MVS Foundation ki reputation par asar padta hai aur bachche panic hote hain. "
               "Ye aapki monthly report par bhi impact karega.\n\n"
               "Please classes time par shuru karein." % late)
        notify(db, tp.user.id, title, msg, "warning")
        db.commit()
    except Exception:
        db.rollback()


def _notify_class_done(subject, chapter, part, teacher_name, dpp_given):
    """Tell the subject's students the class report is up, and guide them to the
    class notes / DPP / verification so they know exactly what to do next."""
    from database import SessionLocal
    from models import StudentProfile
    db = SessionLocal()
    try:
        topic = " \u00b7 ".join([x for x in (chapter, part) if x])
        msg = ("%s ne %s ki class complete kar di%s.\n"
               "\u2022 Class Notes: Materials \u2192 %s\n"
               "%s"
               "\u2022 Time Table par 'Mark Done' dabakar lecture verify karein (XP milega)."
               % (teacher_name, subject, (" (" + topic + ")") if topic else "", subject,
                  ("\u2022 DPP: DPP Submit page se download karke solve karein aur upload karein\n"
                   if dpp_given else "")))
        for sp in db.query(StudentProfile).all():
            if subject in (sp.subjects or []) and sp.user:
                notify(db, sp.user.id, "\U0001F4DA %s class complete" % subject, msg, "class")
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

@router.get("/compliance")
def teacher_compliance(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry, Material
    from sqlalchemy import or_
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    subj_count = max(1, len(subs))
    today = date.today()
    month_start = date(today.year, today.month, 1)
    classes = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs),
        or_(TimetableEntry.status == None, TimetableEntry.status != "pending"),
        TimetableEntry.entry_type == "chapter").all() if subs else []
    due = [c for c in classes if c.entry_date and c.entry_date <= today]
    completed = [c for c in due if getattr(c, "completed", False)]
    mats = db.query(Material).filter(Material.subject.in_(subs)).all() if subs else []
    dpp_count = sum(1 for m in mats if m.material_type == "dpp")
    notes_count = sum(1 for m in mats if m.material_type == "notes")
    test_count = sum(1 for m in mats if m.material_type == "test")
    # component scores (0..1)
    cc = (len(completed) / len(due)) if due else 1.0
    dpp_s = min(1.0, dpp_count / subj_count)
    mat_s = min(1.0, notes_count / subj_count)
    test_s = min(1.0, test_count / subj_count)
    score = round(cc * 40 + dpp_s * 25 + mat_s * 20 + test_s * 15)
    band = "green" if score >= 81 else ("yellow" if score >= 61 else "red")
    return {
        "score": score, "band": band,
        "breakdown": {
            "class_completion": {"weight": 40, "pct": round(cc * 100), "got": round(cc * 40)},
            "dpp_upload": {"weight": 25, "pct": round(dpp_s * 100), "got": round(dpp_s * 25)},
            "study_material": {"weight": 20, "pct": round(mat_s * 100), "got": round(mat_s * 20)},
            "test_creation": {"weight": 15, "pct": round(test_s * 100), "got": round(test_s * 15)},
        },
        "stats": {
            "classes_due": len(due), "classes_completed": len(completed),
            "dpp_count": dpp_count, "notes_count": notes_count, "test_count": test_count,
            "classes_today": sum(1 for c in classes if c.entry_date == today),
            "completed_today": sum(1 for c in completed if c.entry_date == today),
            "pending_today": sum(1 for c in classes if c.entry_date == today and not getattr(c, "completed", False)),
            "missed": sum(1 for c in due if not getattr(c, "completed", False)),
            "subject_count": len(subs),
        }
    }

# ===== TEACHER: DOUBT STATS (pending, resolved, avg response time) =====
@router.get("/doubt-stats")
def teacher_doubt_stats(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Doubt, DoubtStatus
    tp = get_teacher_profile(current_user, db)
    ds = db.query(Doubt).filter(Doubt.teacher_id == tp.id).all()
    pending = sum(1 for d in ds if (d.status.value if hasattr(d.status, "value") else d.status) == "pending")
    resolved_list = [d for d in ds if (d.status.value if hasattr(d.status, "value") else d.status) == "resolved" and d.resolved_at and d.created_at]
    resolved = sum(1 for d in ds if (d.status.value if hasattr(d.status, "value") else d.status) == "resolved")
    avg_min = None
    if resolved_list:
        total = sum((d.resolved_at - d.created_at).total_seconds() for d in resolved_list)
        avg_min = round(total / len(resolved_list) / 60)
    return {"pending": pending, "resolved": resolved, "total": len(ds), "avg_response_minutes": avg_min}

# ===== TEACHER: PERFORMANCE (aggregates + recent activity + monthly) =====
@router.get("/performance")
def teacher_performance(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import TimetableEntry, Material
    from sqlalchemy import or_
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    classes = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs),
        or_(TimetableEntry.status == None, TimetableEntry.status != "pending"),
        TimetableEntry.entry_type == "chapter").all() if subs else []
    completed = [c for c in classes if getattr(c, "completed", False)]
    mats = db.query(Material).filter(Material.subject.in_(subs)).all() if subs else []
    dpp_count = sum(1 for m in mats if m.material_type == "dpp")
    notes_count = sum(1 for m in mats if m.material_type in ("notes", "other"))
    test_count = sum(1 for m in mats if m.material_type == "test")
    # monthly completed (last 6 months)
    from collections import OrderedDict
    today = date.today()
    months = OrderedDict()
    for i in range(5, -1, -1):
        y = today.year; mo = today.month - i
        while mo <= 0:
            mo += 12; y -= 1
        months[f"{y}-{mo:02d}"] = 0
    for c in completed:
        if c.completed_at:
            key = f"{c.completed_at.year}-{c.completed_at.month:02d}"
            if key in months:
                months[key] += 1
    monthly = [{"month": k, "count": v} for k, v in months.items()]
    # recent activity (completions + uploads)
    acts = []
    for c in completed:
        if c.completed_at:
            acts.append({"type": "class", "text": f"Completed {c.subject} — {c.topic_covered or c.chapter or ''}", "at": c.completed_at})
    for m in mats:
        if m.created_at:
            acts.append({"type": m.material_type, "text": f"Uploaded {m.material_type.upper()}: {m.title or m.chapter or m.subject}", "at": m.created_at})
    acts.sort(key=lambda x: x["at"], reverse=True)
    recent = [{"type": a["type"], "text": a["text"], "at": str(a["at"])[:16]} for a in acts[:12]]
    return {
        "classes_assigned": len(classes), "classes_completed": len(completed),
        "dpp_uploaded": dpp_count, "materials_uploaded": notes_count, "tests_created": test_count,
        "monthly": monthly, "recent": recent
    }

# ===== TEACHER: MATERIAL ANALYTICS (views/downloads per material) =====
@router.get("/material-analytics")
def teacher_material_analytics(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material, MaterialView
    from sqlalchemy import func as _f
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    mats = db.query(Material).filter(Material.subject.in_(subs),
        Material.material_type.in_(["notes", "dpp", "test", "other"])).order_by(Material.created_at.desc()).all() if subs else []
    out = []
    for m in mats:
        viewed = db.query(_f.count(_f.distinct(MaterialView.student_id))).filter(MaterialView.material_id == m.id).scalar() or 0
        downloads = db.query(_f.count(MaterialView.id)).filter(MaterialView.material_id == m.id, MaterialView.action == "download").scalar() or 0
        out.append({
            "id": m.id, "type": m.material_type, "category": m.category,
            "title": m.title or m.chapter or m.subject, "subject": m.subject,
            "upload_date": str(m.created_at)[:10] if m.created_at else None,
            "students_viewed": viewed, "downloads": downloads,
            "approval_status": getattr(m, "approval_status", "approved") or "approved",
        })
    return out

# ===== TEACHER: STUDENT ENGAGEMENT =====
@router.get("/student-engagement")
def teacher_student_engagement(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import Material, MaterialView, StudentProfile
    from sqlalchemy import func as _f, or_
    tp = get_teacher_profile(current_user, db)
    subs = tp.subjects or []
    if not subs:
        return []
    # students who have any of the teacher's subjects
    students = []
    for sp in db.query(StudentProfile).all():
        if set(sp.subjects or []) & set(subs):
            students.append(sp)
    # teacher material ids
    mat_ids = [m.id for m in db.query(Material).filter(Material.subject.in_(subs)).all()]
    out = []
    for sp in students:
        answers = db.query(Material).filter(Material.material_type == "answer", Material.student_id == sp.id).all()
        pids = [a.parent_id for a in answers if a.parent_id]
        ptypes = {}
        if pids:
            for pm in db.query(Material).filter(Material.id.in_(pids)).all():
                ptypes[pm.id] = pm.material_type
        dpp_done = sum(1 for a in answers if ptypes.get(a.parent_id) == "dpp")
        test_done = sum(1 for a in answers if ptypes.get(a.parent_id) == "test")
        downloads = db.query(_f.count(MaterialView.id)).filter(
            MaterialView.student_id == sp.id, MaterialView.action == "download",
            MaterialView.material_id.in_(mat_ids) if mat_ids else False).scalar() or 0
        last_act = db.query(MaterialView).filter(MaterialView.student_id == sp.id).order_by(MaterialView.created_at.desc()).first()
        out.append({
            "name": (sp.user.name if sp.user else "Student"),
            "phone": sp.phone, "subjects": sp.subjects or [],
            "dpp_completed": dpp_done, "tests_completed": test_done,
            "material_downloads": downloads,
            "last_active": str(last_act.created_at)[:16] if last_act else None,
        })
    out.sort(key=lambda x: (x["material_downloads"] + x["dpp_completed"] + x["tests_completed"]), reverse=True)
    return out


# ===================== EXAM / TEST ENGINE (teacher) =====================

# ---- exam engine: lazy column migration + helpers (safe to call anywhere) ----
_EXAM_COLS_READY = False

def _ensure_exam_columns(db):
    """Add scheduled_at / attempted / skipped columns on first use (MySQL/Postgres/SQLite).
    Runs once per process; every ALTER is best-effort so existing databases upgrade themselves."""
    global _EXAM_COLS_READY
    if _EXAM_COLS_READY:
        return
    from sqlalchemy import text as _text
    stmts = [
        ("ALTER TABLE exams ADD COLUMN scheduled_at DATETIME NULL",
         "ALTER TABLE exams ADD COLUMN scheduled_at TIMESTAMP NULL"),
        ("ALTER TABLE exam_attempts ADD COLUMN attempted JSON NULL",
         "ALTER TABLE exam_attempts ADD COLUMN attempted TEXT NULL"),
        ("ALTER TABLE exam_attempts ADD COLUMN skipped JSON NULL",
         "ALTER TABLE exam_attempts ADD COLUMN skipped TEXT NULL"),
        ("ALTER TABLE exam_questions ADD COLUMN alt_image_b64 LONGTEXT NULL",
         "ALTER TABLE exam_questions ADD COLUMN alt_image_b64 TEXT NULL"),
    ]
    for group in stmts:
        for s in group:
            try:
                db.execute(_text(s))
                db.commit()
                break
            except Exception:
                db.rollback()
    _EXAM_COLS_READY = True


def _exam_parse_dt(v):
    """Parse an ISO-ish datetime from the portal; returns None on failure."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    return None

@router.post("/exam")
def create_exam(payload: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    qs = payload.get("questions") or []
    if not payload.get("title") or not qs:
        raise HTTPException(400, "Title and at least one question are required")
    ttype = payload.get("test_type", "subjective")
    total = sum(int(q.get("max_marks", 1) or 1) for q in qs)
    ex = Exam(teacher_id=tp.id, teacher_name=current_user.name,
              subject=payload.get("subject", ""), title=payload["title"],
              chapter=payload.get("chapter"), test_type=ttype,
              medium=payload.get("medium", "English"),
              total_marks=total, duration_min=int(payload.get("duration_min", 60) or 60),
              scheduled_at=_exam_parse_dt(payload.get("scheduled_at")))
    db.add(ex); db.flush()
    for i, q in enumerate(qs, start=1):
        co = q.get("correct_option")
        opts_hi = q.get("options_hi") if ttype == "mcq" else None
        db.add(ExamQuestion(exam_id=ex.id, q_no=i,
               question_text=q.get("question_text", ""),
               max_marks=int(q.get("max_marks", 1) or 1),
               model_answer=q.get("model_answer"),
               options=q.get("options") if ttype == "mcq" else None,
               correct_option=(str(co) if co not in (None, "") else None),
               image_b64=q.get("image_b64"),
               question_text_hi=(q.get("question_text_hi") or None),
               model_answer_hi=(q.get("model_answer_hi") or None),
               options_hi=(opts_hi if opts_hi else None),
               model_answer_image=q.get("model_answer_image"),
               alt_image_b64=q.get("alt_image_b64"),
               explanation=(q.get("explanation") or None),
               explanation_hi=(q.get("explanation_hi") or None)))
    db.commit()
    # Bilingual Hindi is now filled on-demand by the portal (free). Paid Gemini
    # auto-translation is disabled to avoid API costs. (Function kept for manual use.)
    # if (ex.medium or "").lower().startswith("bi") and background_tasks is not None:
    #     background_tasks.add_task(_bg_translate_exam, ex.id)
    return {"id": ex.id, "total_marks": total, "questions": len(qs),
            "test_type": ttype, "medium": ex.medium,
            "scheduled_at": ex.scheduled_at.isoformat() if getattr(ex, "scheduled_at", None) else None}


def _bg_translate_exam(exam_id):
    """Fill in any missing Hindi fields for a bilingual test using Gemini.
    Runs after the response so test creation stays fast. Only fills blanks."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        ex = db.query(Exam).filter(Exam.id == exam_id).first()
        if not ex:
            return
        qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).order_by(ExamQuestion.q_no).all()
        for q in qs:
            need_q = not (q.question_text_hi or "").strip()
            need_a = (ex.test_type != "mcq") and not (q.model_answer_hi or "").strip()
            need_o = (ex.test_type == "mcq") and not q.options_hi
            if not (need_q or need_a or need_o):
                continue
            tr = grading.translate_question_to_hindi(
                q.question_text or "", q.model_answer or "",
                (q.options or []) if ex.test_type == "mcq" else None, ex.subject or "")
            if not tr:
                continue
            if need_q and tr.get("question"):
                q.question_text_hi = tr["question"]
            if need_a and tr.get("answer"):
                q.model_answer_hi = tr["answer"]
            if need_o and tr.get("options") and len(tr["options"]) == len(q.options or []):
                q.options_hi = tr["options"]
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.get("/exam/{exam_id}/pdf")
def teacher_exam_pdf(exam_id: int, medium: str = "english",
                     db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Download the full question+answer paper as a PDF in English or Hindi medium."""
    _ensure_exam_columns(db)
    import exam_pdf
    from fastapi import Response
    tp = get_teacher_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).order_by(ExamQuestion.q_no).all()
    med = "hindi" if str(medium).lower().startswith("hi") else "english"
    try:
        data = exam_pdf.build_exam_pdf(ex, qs, med)
    except Exception as e:
        raise HTTPException(500, "Could not generate the PDF. The server needs fpdf2, "
                                 "uharfbuzz and the Devanagari font. (%s)" % e)
    safe = (ex.title or "test").replace('"', "").replace("/", "-").strip()[:60] or "test"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s-%s.pdf"' % (safe, med)})

@router.get("/exams")
def list_exams(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _ensure_exam_columns(db)
    from models import ExamView
    tp = get_teacher_profile(current_user, db)
    rows = db.query(Exam).filter(Exam.teacher_id == tp.id, Exam.is_active == True).order_by(Exam.created_at.desc()).all()
    ids = [e.id for e in rows]
    views, downloads = {}, {}
    if ids:
        for v in db.query(ExamView).filter(ExamView.exam_id.in_(ids)).all():
            (downloads if v.action == "download" else views).setdefault(v.exam_id, set()).add(v.student_id)
        # a student who attempted the test has obviously seen it - count them as a
        # viewer too, so tests taken before view-tracking existed still show up
        for a in db.query(ExamAttempt).filter(ExamAttempt.exam_id.in_(ids)).all():
            views.setdefault(a.exam_id, set()).add(a.student_id)
    out = []
    for e in rows:
        nq = db.query(ExamQuestion).filter(ExamQuestion.exam_id == e.id).count()
        na = db.query(ExamAttempt).filter(ExamAttempt.exam_id == e.id).count()
        ng = db.query(ExamAttempt).filter(ExamAttempt.exam_id == e.id, ExamAttempt.status == "graded").count()
        out.append({"id": e.id, "title": e.title, "subject": e.subject, "chapter": e.chapter,
                    "test_type": e.test_type, "total_marks": e.total_marks, "duration_min": e.duration_min,
                    "medium": e.medium, "questions": nq, "attempts": na, "graded": ng,
                    "views": len(views.get(e.id, ())), "downloads": len(downloads.get(e.id, ())),
                    "scheduled_at": e.scheduled_at.isoformat() if getattr(e, "scheduled_at", None) else None,
                    "created_at": e.created_at.isoformat() if e.created_at else None})
    return out


@router.get("/exam/{exam_id}/audience")
def exam_audience(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Which students opened / downloaded this test."""
    _ensure_exam_columns(db)
    from models import ExamView, StudentProfile
    tp = get_teacher_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    rows = db.query(ExamView).filter(ExamView.exam_id == exam_id).all()
    attempts = db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id).all()
    sids = list({r.student_id for r in rows} | {a.student_id for a in attempts})
    smap = {}
    if sids:
        for sp in db.query(StudentProfile).filter(StudentProfile.id.in_(sids)).all():
            smap[sp.id] = (sp.user.name if sp.user else ("Student #%d" % sp.id))
    viewers, downloaders, seen = [], [], set()
    for r in rows:
        entry = {"student_id": r.student_id, "name": smap.get(r.student_id, "Student"),
                 "at": str(r.created_at)[:16]}
        if r.action == "download":
            downloaders.append(entry)
        else:
            seen.add(r.student_id)
            viewers.append(entry)
    # attempted => viewed (covers tests taken before view-tracking existed)
    for a in attempts:
        if a.student_id in seen:
            continue
        seen.add(a.student_id)
        viewers.append({"student_id": a.student_id, "name": smap.get(a.student_id, "Student"),
                        "at": str(a.submitted_at)[:16] if getattr(a, "submitted_at", None) else ""})
    return {"material": {"id": ex.id, "title": ex.title, "type": "test",
                         "subject": ex.subject, "chapter": ex.chapter, "part": None},
            "viewers": viewers, "downloaders": downloaders}

@router.get("/exam/{exam_id}/attempts")
def exam_attempts(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import StudentProfile
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(404, "Exam not found")
    atts = db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id).order_by(ExamAttempt.submitted_at.desc()).all()
    out = []
    for a in atts:
        _atl = getattr(a, "attempted", None)
        _skl = getattr(a, "skipped", None)
        _sp = db.query(StudentProfile).filter(StudentProfile.id == a.student_id).first()
        _su = _sp.user if _sp else None
        out.append({"attempt_id": a.id, "student_id": a.student_id, "student_name": a.student_name,
            "status": a.status, "total_awarded": a.total_awarded, "verdict": a.verdict,
            "has_answer": bool(a.answer_image_b64),
            "feedback": a.overall_feedback,
            "results": [{"q_no": rr.q_no, "marks": rr.marks_awarded,
                         "max": rr.max_marks, "remark": rr.remark or ""}
                        for rr in db.query(ExamResult)
                                    .filter(ExamResult.attempt_id == a.id)
                                    .order_by(ExamResult.q_no).all()],
            "phone": (_sp.phone if _sp else None),
            "student_code": (_su.user_id if _su else None),
            "batch": (_sp.batch_name if _sp else None),
            "class_level": (_sp.class_level if _sp else None),
            "medium": (_sp.medium if _sp else None),
            "email": (_sp.email if _sp else None),
            "subjects": ((_sp.subjects or []) if _sp else []),
            "attempted_count": (len(_atl) if isinstance(_atl, list) else None),
            "skipped_count": (len(_skl) if isinstance(_skl, list) else None),
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None})
    qrows = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.q_no).all()
    questions = [{"q_no": q.q_no, "question_text": q.question_text, "max_marks": q.max_marks,
                  "model_answer": q.model_answer, "options": q.options,
                  "correct_option": q.correct_option, "image_b64": q.image_b64,
                  "question_text_hi": q.question_text_hi, "model_answer_hi": q.model_answer_hi,
                  "options_hi": q.options_hi, "model_answer_image": q.model_answer_image,
                  "alt_image_b64": getattr(q, "alt_image_b64", None),
                  "explanation": q.explanation, "explanation_hi": q.explanation_hi} for q in qrows]
    return {"exam": {"id": ex.id, "title": ex.title, "total_marks": ex.total_marks,
                     "test_type": ex.test_type, "subject": ex.subject, "chapter": ex.chapter,
                     "medium": ex.medium, "duration_min": ex.duration_min,
                     "scheduled_at": ex.scheduled_at.isoformat() if getattr(ex, "scheduled_at", None) else None},
            "questions": questions, "attempts": out}


def _exam_verdict_t(aw, tot):
    if not tot:
        return "Good"
    p = aw / tot * 100
    return "Excellent" if p >= 80 else ("Good" if p >= 50 else "Needs Improvement")

def _notify_exam_result_t(db, att, ex):
    """Notify the student that their test result is ready."""
    try:
        from models import StudentProfile
        sp = db.query(StudentProfile).filter(StudentProfile.id == att.student_id).first()
        if sp and sp.user_id:
            try:
                sc = "%g" % float(att.total_awarded)
            except Exception:
                sc = str(att.total_awarded)
            db.add(Notification(
                user_id=sp.user_id,
                title="Result ready: %s" % (ex.title or "Test"),
                message="Your test has been checked. You scored %s/%s. Tap to view your result and download your answer sheet." % (sc, ex.total_marks),
                notif_type="exam_result"))
    except Exception:
        pass


@router.post("/attempt/{attempt_id}/grade")
def grade_attempt_now(attempt_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    att = db.query(ExamAttempt).filter(ExamAttempt.id == attempt_id).first()
    if not att:
        raise HTTPException(404, "Attempt not found")
    ex = db.query(Exam).filter(Exam.id == att.exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(403, "Not your test")
    if att.status == "graded":
        return {"status": "graded", "message": "Already graded"}
    qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).order_by(ExamQuestion.q_no).all()
    teacher = ex.teacher_name or "your teacher"
    if ex.test_type == "mcq":
        results, total = grading.grade_mcq(qs, att.mcq_answers or {})
        feedback, verdict = "", _exam_verdict_t(total, ex.total_marks)
    else:
        results, total, feedback, verdict = grading.grade_subjective(qs, att.answer_image_b64 or "", "image/jpeg")
        if results is None:
            raise HTTPException(400, "AI grading failed: " + (feedback or "unknown error") + " -- you can use Grade Manually instead.")
        verdict = verdict or _exam_verdict_t(total, ex.total_marks)
    db.query(ExamResult).filter(ExamResult.attempt_id == att.id).delete()
    for r in results:
        db.add(ExamResult(attempt_id=att.id, q_no=r["q_no"], marks_awarded=r["marks"],
               max_marks=r["max"], remark=r.get("remark", "")))
    att.total_awarded = total
    att.status = "graded"
    att.graded_at = datetime.utcnow()
    att.verdict = verdict
    att.overall_feedback = feedback or ("Graded by teacher. \u2014 %s" % teacher)
    _notify_exam_result_t(db, att, ex)
    db.commit()
    return {"status": "graded", "total_awarded": total, "verdict": verdict}


# ===================== AI AUTO-MAGIC ENDPOINTS (Phase 2) =====================
@router.post("/ocr-question")
def ocr_question(payload: dict = Body(...), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    img = payload.get("image_b64") or ""
    if not img:
        raise HTTPException(400, "No image provided")
    res = grading.ocr_extract_question(img, payload.get("test_type", "subjective"),
                                       payload.get("mime_type", "image/jpeg"))
    if res is None:
        raise HTTPException(503, "AI could not read the image. Check GEMINI_API_KEY or try a clearer screenshot.")
    return res

@router.post("/format-text")
def format_text(payload: dict = Body(...), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    out = grading.format_text_latex(payload.get("text", ""))
    if out is None:
        raise HTTPException(503, "AI formatting is unavailable. Check GEMINI_API_KEY.")
    return {"text": out}

@router.post("/parse-exam-docx")
async def parse_exam_docx(file: UploadFile = File(...), test_type: str = Form("subjective"),
                          db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    import io
    try:
        from docx import Document
    except Exception:
        raise HTTPException(503, "Word parsing is not enabled on the server (add python-docx to requirements.txt).")
    data = await file.read()
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "Could not open the Word file. Please upload a valid .docx file.")
    full = "\n".join(p.text for p in doc.paragraphs if p.text and p.text.strip())
    # also pull text from tables
    for tb in doc.tables:
        for row in tb.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                full += "\n" + " | ".join(cells)
    if not full.strip():
        raise HTTPException(400, "The Word file appears to be empty.")
    qs = grading.local_structure_questions(full, test_type)
    if qs is None:
        raise HTTPException(400, grading.LAST_ERROR or "Could not read questions from the document.")
    return {"questions": qs, "count": len(qs)}


@router.post("/parse-exam-pdf")
async def parse_exam_pdf(file: UploadFile = File(...), test_type: str = Form("subjective"),
                         db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Extract questions + answers from an uploaded PDF question paper."""
    import io
    data = await file.read()
    full = ""
    # try PyMuPDF first, then pdfplumber as a fallback
    try:
        import fitz
        doc = fitz.open(stream=data, filetype="pdf")
        full = "\n".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                full = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        except Exception:
            raise HTTPException(503, "PDF parsing is not enabled on the server (add pymupdf to requirements.txt).")
    if not full.strip():
        raise HTTPException(400, "Could not read any text from this PDF. If it is a scanned image, please use a text PDF or the screenshot auto-fill.")
    qs = grading.local_structure_questions(full, test_type)
    if qs is None:
        raise HTTPException(400, grading.LAST_ERROR or "Could not read questions from the PDF.")
    return {"questions": qs, "count": len(qs), "note": grading.LAST_ERROR or None}


@router.get("/attempt/{attempt_id}/answer")
def attempt_answer_image(attempt_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _ensure_exam_columns(db)
    import base64          # ROOT CAUSE: ye import missing tha -> NameError ->
                           # unhandled 500 (CORS headers ke bina) -> portal par
                           # "Failed to fetch". Isi wajah se sheet kabhi nahi khulti thi.
    from fastapi import Response
    tp = get_teacher_profile(current_user, db)
    att = db.query(ExamAttempt).filter(ExamAttempt.id == attempt_id).first()
    if not att:
        raise HTTPException(404, "Attempt not found")
    ex = db.query(Exam).filter(Exam.id == att.exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(403, "Not your test")
    if (att.status or "") == "grading":
        att.status = "marking"   # teacher opened the sheet -> "being checked"
        db.commit()
    if not att.answer_image_b64:
        raise HTTPException(404, "No answer sheet uploaded")
    # Students upload a photo OR a PDF. Pehle hamesha image/jpeg bheja jaata tha
    # aur decode fail hone par unhandled 500 aata tha - browser use CORS ke bina
    # block kar deta tha, isliye portal par "Failed to fetch" dikhta tha.
    raw = att.answer_image_b64 or ""
    mime = "image/jpeg"
    if raw.startswith("data:") and "," in raw:
        header, raw = raw.split(",", 1)
        try:
            mime = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
        except Exception:
            mime = "image/jpeg"
    raw = "".join(raw.split())          # stray whitespace/newlines hatao
    raw += "=" * (-len(raw) % 4)        # padding theek karo
    try:
        data = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "The uploaded answer sheet could not be read. Ask the student to upload it again.")
    if not data:
        raise HTTPException(404, "No answer sheet uploaded")
    ext = "pdf" if "pdf" in mime else ("png" if "png" in mime else "jpg")
    safe = "".join(c for c in (att.student_name or "student") if c.isalnum() or c in " -_").strip() or "student"
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": 'inline; filename="answer-%s.%s"' % (safe, ext),
                             "Content-Length": str(len(data))})

@router.post("/attempt/{attempt_id}/grade-manual")
def grade_attempt_manual(attempt_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    att = db.query(ExamAttempt).filter(ExamAttempt.id == attempt_id).first()
    if not att:
        raise HTTPException(404, "Attempt not found")
    ex = db.query(Exam).filter(Exam.id == att.exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(403, "Not your test")
    qmap = {q.q_no: q for q in db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).all()}
    results = payload.get("results") or []
    db.query(ExamResult).filter(ExamResult.attempt_id == att.id).delete()
    total = 0.0
    for r in results:
        try:
            qn = int(r.get("q_no"))
        except Exception:
            continue
        mx = qmap[qn].max_marks if qn in qmap else int(r.get("max", 1) or 1)
        try:
            mk = float(r.get("marks", 0) or 0)
        except Exception:
            mk = 0.0
        mk = max(0.0, min(mk, float(mx)))
        total += mk
        db.add(ExamResult(attempt_id=att.id, q_no=qn, marks_awarded=mk, max_marks=mx, remark=r.get("remark", "")))
    att.total_awarded = total
    att.status = "graded"
    att.graded_at = datetime.utcnow()
    att.verdict = payload.get("verdict") or _exam_verdict_t(total, ex.total_marks)
    fb = payload.get("feedback") or ""
    att.overall_feedback = fb if fb else ("Checked by %s." % (ex.teacher_name or "your teacher"))
    _notify_exam_result_t(db, att, ex)
    db.commit()
    return {"status": "graded", "total_awarded": total, "verdict": att.verdict}


@router.get("/ai-status")
def ai_status(current_user=Depends(get_teacher)):
    return grading.ai_status()


# ============================================================
#  SMART LECTURE VERIFICATION — TEACHER SIDE
# ============================================================
from models import Lecture, LectureQuestion, StudentProfile

_LQ_TYPES = {"mcq", "image_mcq", "numerical", "fill_blank", "true_false"}


@router.get("/timetable-entries-lite")
def teacher_tt_entries_lite(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Timetable entries a lecture report can optionally be linked to. Matches by
    the teacher's own entries AND by their subjects, so linking works even when
    an entry was created without a teacher_id."""
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    from sqlalchemy import or_
    subs = tp.subjects or []
    q = db.query(TimetableEntry)
    conds = [TimetableEntry.teacher_id == tp.id]
    if subs:
        conds.append(TimetableEntry.subject.in_(subs))
    es = q.filter(or_(*conds)).order_by(TimetableEntry.entry_date.desc()).limit(120).all()
    # skip test/event rows - only teachable chapter parts make sense to verify
    out = []
    for e in es:
        et = (getattr(e, "entry_type", "") or "").lower()
        if et in ("test", "exam", "event"):
            continue
        out.append({"id": e.id, "subject": e.subject, "chapter": e.chapter, "part": e.part,
                    "date": str(e.entry_date) if e.entry_date else None,
                    "class_name": e.class_name})
    return out


@router.post("/lecture")
async def create_lecture(payload: dict = Body(...), background_tasks: BackgroundTasks = None,
                         db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Publish a lecture report with a mandatory verification question set."""
    tp = get_teacher_profile(current_user, db)
    subject = (payload.get("subject") or "").strip()
    if not subject:
        raise HTTPException(400, "Subject is required")
    qs = payload.get("questions") or []
    valid_qs = [q for q in qs if (q.get("question") or "").strip() and (q.get("qtype") in _LQ_TYPES)]
    if not valid_qs:
        raise HTTPException(400, "At least one valid verification question is required")

    lec = Lecture(
        teacher_id=tp.id, teacher_name=(current_user.name or ""),
        subject=subject, class_level=(payload.get("class_level") or None),
        chapter=(payload.get("chapter") or None), part=(payload.get("part") or None),
        title=(payload.get("title") or (subject + " Lecture")),
        timetable_entry_id=(payload.get("timetable_entry_id") or None),
        summary=(payload.get("summary") or None), homework=(payload.get("homework") or None),
        pdf_b64=(payload.get("pdf_b64") or None), pdf_filename=(payload.get("pdf_filename") or None),
        dpp_b64=(payload.get("dpp_b64") or None), dpp_filename=(payload.get("dpp_filename") or None),
        is_active=True,
    )
    from datetime import date as _date
    ld = payload.get("lecture_date")
    if ld:
        try:
            lec.lecture_date = _date.fromisoformat(ld)
        except Exception:
            pass
    db.add(lec); db.flush()

    # Mirror the uploads into Materials so students find them under Study Material
    # and view/download analytics work exactly like any other material.
    from models import Material
    def _mk(kind, b64, fname):
        if not b64:
            return
        raw = b64.split(",")[-1]
        db.add(Material(
            teacher_id=tp.id, teacher_name=(current_user.name or ""),
            subject=subject, class_name=(payload.get("class_level") or None),
            chapter=(payload.get("chapter") or None), part=(payload.get("part") or None),
            material_type=kind, title=(lec.title or subject),
            filename=(fname or ("%s.pdf" % kind)), content_b64=raw))
    _mk("notes", payload.get("pdf_b64"), payload.get("pdf_filename"))
    _mk("dpp", payload.get("dpp_b64"), payload.get("dpp_filename"))
    db.flush()

    for q in valid_qs:
        db.add(LectureQuestion(
            lecture_id=lec.id, qtype=q.get("qtype"),
            question=(q.get("question") or ""), question_hi=(q.get("question_hi") or None),
            image_b64=(q.get("image_b64") or None),
            options=(q.get("options") or None), options_hi=(q.get("options_hi") or None),
            option_images=(q.get("option_images") or None),
            correct=str(q.get("correct") if q.get("correct") is not None else ""),
            tolerance=(float(q["tolerance"]) if q.get("tolerance") not in (None, "") else None),
        ))
    db.commit(); db.refresh(lec)

    # notify students of this subject in the background (fast response)
    if background_tasks is not None:
        background_tasks.add_task(_notify_lecture_students, subject, lec.title, current_user.name)
    return {"id": lec.id, "message": "Lecture report published"}


def _notify_lecture_students(subject, title, teacher_name):
    from database import SessionLocal
    db = SessionLocal()
    try:
        studs = db.query(StudentProfile).all()
        for sp in studs:
            if subject in (sp.subjects or []) and sp.user:
                notify(db, sp.user.id, "\U0001F4DA New Lecture: %s" % subject,
                       "%s ne '%s' ka lecture report daala hai. Mark it done to verify." % (teacher_name, title),
                       "lecture")
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.get("/lectures")
def teacher_lectures(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import LectureVerification
    lecs = db.query(Lecture).filter(Lecture.teacher_id == tp.id).order_by(Lecture.created_at.desc()).all()
    out = []
    for l in lecs:
        nq = db.query(LectureQuestion).filter(LectureQuestion.lecture_id == l.id).count()
        verified = db.query(LectureVerification).filter(
            LectureVerification.lecture_id == l.id, LectureVerification.status == "verified").count()
        attempted = db.query(LectureVerification).filter(LectureVerification.lecture_id == l.id).count()
        out.append({"id": l.id, "title": l.title, "subject": l.subject, "chapter": l.chapter,
                    "date": str(l.lecture_date) if l.lecture_date else str(l.created_at)[:10],
                    "questions": nq, "verified": verified, "attempted": attempted,
                    "has_pdf": bool(l.pdf_b64), "has_dpp": bool(l.dpp_b64), "is_active": l.is_active})
    return out


@router.delete("/lecture/{lecture_id}")
def delete_lecture(lecture_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    lec = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.teacher_id == tp.id).first()
    if not lec:
        raise HTTPException(404, "Lecture not found")
    lec.is_active = False
    db.commit()
    return {"message": "Lecture removed"}


# ===================================================================== CLASS REPORTS
# Delay tracking + teaching-hours analytics, computed from the timetable's
# scheduled slot (time_text) vs what the teacher actually reported (start_time).

def _parse_hhmm(s):
    """'6:30 pm' / '18:30' / '6.30pm' -> minutes since midnight, or None."""
    if not s:
        return None
    m = re.search(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm)?", str(s), re.I)
    if not m:
        m2 = re.search(r"(\d{1,2})\s*(am|pm)", str(s), re.I)
        if not m2:
            return None
        h = int(m2.group(1)) % 12
        if m2.group(2).lower() == "pm":
            h += 12
        return h * 60
    h, mi = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return h * 60 + mi


def _slot_start(time_text):
    """The scheduled slot may be a range ('6:30 pm - 7:30 pm'); take its start."""
    if not time_text:
        return None
    first = re.split(r"[-\u2013\u2014to]+", str(time_text))[0]
    return _parse_hhmm(first)


def _delay_of(e):
    """Minutes the class started late (negative = early). None when unknown."""
    sched = _slot_start(getattr(e, "time_text", None))
    actual = _parse_hhmm(getattr(e, "start_time", None))
    if sched is None or actual is None:
        return None
    return actual - sched


def _duration_of(e):
    a = _parse_hhmm(getattr(e, "start_time", None))
    b = _parse_hhmm(getattr(e, "end_time", None))
    if a is None or b is None:
        return 0
    d = b - a
    if d < 0:
        d += 24 * 60          # crossed midnight
    return d if 0 < d <= 6 * 60 else 0


def _delay_band(d):
    if d is None:
        return "unknown"
    if d <= 5:
        return "ontime"       # up to 5 min = on time
    if d <= 15:
        return "minor"
    return "late"


def _report_rows(db, subjects, teacher_map=None):
    from models import TimetableEntry
    q = db.query(TimetableEntry).filter(TimetableEntry.completed == True,
                                        TimetableEntry.entry_type == "chapter")
    if subjects is not None:
        if not subjects:
            return []
        q = q.filter(TimetableEntry.subject.in_(subjects))
    es = q.order_by(TimetableEntry.entry_date.desc()).limit(300).all()
    rows = []
    for e in es:
        d = _delay_of(e)
        rows.append({
            "id": e.id, "subject": e.subject, "chapter": e.chapter, "part": e.part,
            "date": str(e.entry_date) if e.entry_date else None,
            "scheduled": e.time_text, "start_time": e.start_time, "end_time": e.end_time,
            "delay_min": d, "delay_band": _delay_band(d),
            "duration_min": _duration_of(e),
            "topic_covered": e.topic_covered, "homework": e.homework,
            "dpp_given": bool(e.dpp_given), "remarks": e.remarks,
            "teacher_id": e.teacher_id,
            "teacher_name": (teacher_map or {}).get(e.teacher_id, ""),
        })
    return rows


def _report_summary(rows):
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = date(today.year, today.month, 1)
    by_subject = {}
    wk_min = mo_min = 0
    delays = [r["delay_min"] for r in rows if r["delay_min"] is not None]
    bands = {"ontime": 0, "minor": 0, "late": 0}
    for r in rows:
        bands[r["delay_band"]] = bands.get(r["delay_band"], 0) + (1 if r["delay_band"] in bands else 0)
        if not r["date"]:
            continue
        try:
            d = date.fromisoformat(r["date"])
        except Exception:
            continue
        s = by_subject.setdefault(r["subject"], {"subject": r["subject"], "classes": 0,
                                                 "week_min": 0, "month_min": 0, "total_min": 0})
        s["classes"] += 1
        s["total_min"] += r["duration_min"]
        if d >= week_start:
            s["week_min"] += r["duration_min"]; wk_min += r["duration_min"]
        if d >= month_start:
            s["month_min"] += r["duration_min"]; mo_min += r["duration_min"]
    return {
        "week_hours": round(wk_min / 60.0, 1),
        "month_hours": round(mo_min / 60.0, 1),
        "classes_done": len(rows),
        "avg_delay": (round(sum(delays) / len(delays)) if delays else None),
        "on_time_pct": (round(bands["ontime"] * 100 / len(delays)) if delays else None),
        "bands": bands,
        "by_subject": sorted(by_subject.values(), key=lambda x: -x["total_min"]),
    }


@router.get("/class-reports")
def teacher_class_reports(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """The teacher's own submitted class reports + delay/hours analytics."""
    tp = get_teacher_profile(current_user, db)
    rows = _report_rows(db, tp.subjects or [])
    return {"summary": _report_summary(rows), "rows": rows[:60]}


# ============================================================ MATERIAL ANALYTICS
# Subject -> chapter -> part view of everything uploaded, with view/download
# counts and the actual student lists behind those counts. Shared shape so the
# teacher portal and the admin portal render from the same renderer.

def _material_tree(db, subjects=None):
    from models import Material, MaterialView, StudentProfile
    q = db.query(Material).filter(Material.material_type != "answer")
    if subjects is not None:
        if not subjects:
            return []
        q = q.filter(Material.subject.in_(subjects))
    mats = q.order_by(Material.created_at.desc()).all()
    ids = [m.id for m in mats]
    views, downloads = {}, {}
    if ids:
        for v in db.query(MaterialView).filter(MaterialView.material_id.in_(ids)).all():
            d = downloads if v.action == "download" else views
            d.setdefault(v.material_id, set()).add(v.student_id)
    tree = {}
    for m in mats:
        sub = tree.setdefault(m.subject or "General", {"subject": m.subject or "General", "chapters": {}})
        ch = sub["chapters"].setdefault(m.chapter or "General", {"chapter": m.chapter or "General", "items": []})
        ch["items"].append({
            "id": m.id, "part": m.part or "", "type": m.material_type,
            "category": m.category or "", "title": m.title or "",
            "filename": m.filename or "", "teacher_name": m.teacher_name or "",
            "date": str(m.created_at)[:10] if m.created_at else "",
            "views": len(views.get(m.id, ())), "downloads": len(downloads.get(m.id, ())),
        })
    out = []
    for s in tree.values():
        chapters = []
        for c in s["chapters"].values():
            c["items"].sort(key=lambda x: (x["part"], x["type"]))
            chapters.append(c)
        chapters.sort(key=lambda c: c["chapter"])
        out.append({"subject": s["subject"], "chapters": chapters})
    out.sort(key=lambda s: s["subject"])
    return out


def _material_audience(db, material_id):
    """Who viewed / downloaded this material."""
    from models import Material, MaterialView, StudentProfile, User
    m = db.query(Material).filter(Material.id == material_id).first()
    if not m:
        raise HTTPException(404, "Material not found")
    rows = db.query(MaterialView).filter(MaterialView.material_id == material_id).all()
    sids = list({r.student_id for r in rows})
    smap = {}
    if sids:
        for sp in db.query(StudentProfile).filter(StudentProfile.id.in_(sids)).all():
            smap[sp.id] = (sp.user.name if sp.user else ("Student #%d" % sp.id))
    seen, viewers, downloaders = {}, [], []
    for r in rows:
        nm = smap.get(r.student_id, "Student")
        key = (r.student_id, r.action)
        if key in seen:
            continue
        seen[key] = True
        entry = {"student_id": r.student_id, "name": nm, "at": str(r.created_at)[:16]}
        (downloaders if r.action == "download" else viewers).append(entry)
    return {"material": {"id": m.id, "title": m.title, "type": m.material_type,
                         "subject": m.subject, "chapter": m.chapter, "part": m.part},
            "viewers": viewers, "downloaders": downloaders}


@router.get("/materials-tree")
def teacher_materials_tree(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    return {"subjects": _material_tree(db, tp.subjects or [])}


@router.get("/material/{mid}/audience")
def teacher_material_audience(mid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    return _material_audience(db, mid)

# ==================================================================
#  SMART EXTRA CLASS — auto-shift ke saath
#  Teacher extra class daalta hai -> uske baad ki us subject ki saari
#  classes apne aap aage khisak jaati hain (sirf un weekdays par jinpe
#  us subject ki class hoti hai). Session end (default 10 Sept) ke baad
#  jaane par warning + extra weekdays ka suggestion.
# ==================================================================
import os as _os
from datetime import date as _date, timedelta as _td, datetime as _dt2

WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _default_end():
    raw = (_os.getenv("SESSION_END_DATE") or "").strip()
    if raw:
        try:
            return _dt2.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            pass
    today = _date.today()
    end = _date(today.year, 9, 10)          # fallback: 10 September
    if end < today:
        end = _date(today.year + 1, 9, 10)
    return end


def session_end_for(db, subject=None, class_level=None, batch=None):
    """Deadline priority: subject+class > subject > batch > global > default.
    Admin ise Settings -> Session Deadlines me set karta hai."""
    from models import SessionDeadline as _SD
    rows = db.query(_SD).all()
    if not rows:
        return _default_end(), "default"

    def find(scope, key):
        k = (key or "").strip().lower()
        for r in rows:
            if r.scope == scope and (r.key or "").strip().lower() == k:
                return r
        return None

    if subject and class_level:
        r = find("subject", f"{subject}|{class_level}")
        if r:
            return r.end_date, f"{subject} (Class {class_level})"
    if subject:
        r = find("subject", subject)
        if r:
            return r.end_date, subject
    if batch:
        r = find("batch", batch)
        if r:
            return r.end_date, batch
    r = find("global", "")
    if r:
        return r.end_date, "All batches"
    return _default_end(), "default"


def _subject_weekdays(entries):
    """Us subject ki classes kin weekdays par hoti hain (0=Mon)."""
    days = sorted({e.entry_date.weekday() for e in entries if e.entry_date})
    return days or [0, 2, 4]               # fallback: Mon/Wed/Fri


def _next_slots(start_after, weekdays, count, busy_dates):
    """Agli `count` free dates jo `weekdays` par aati hain (busy dates skip)."""
    out, d = [], start_after + _td(days=1)
    guard = 0
    while len(out) < count and guard < 800:
        guard += 1
        if d.weekday() in weekdays and d not in busy_dates:
            out.append(d)
        d += _td(days=1)
    return out


def _plan_shift(db, tp, subject, new_date):
    """Preview: extra class ke baad kya-kya shift hoga."""
    from models import TimetableEntry
    rows = db.query(TimetableEntry).filter(
        TimetableEntry.teacher_id == tp.id,
        TimetableEntry.subject == subject,
        TimetableEntry.status == "approved",
    ).all()
    dated = [e for e in rows if e.entry_date]
    weekdays = _subject_weekdays(dated)

    # jo classes new_date ke din ya uske baad hain, wo ek slot aage khiskengi
    affected = sorted([e for e in dated if e.entry_date >= new_date],
                      key=lambda e: (e.entry_date, e.id))
    end, end_src = session_end_for(db, subject=subject,
                                   class_level=(dated[0].class_name if dated else None),
                                   batch=(tp.batch.value if getattr(tp, "batch", None) else None))
    if not affected:
        return {"shifted": [], "weekdays": [WEEK[i] for i in weekdays], "overflow": False,
                "last_date": None, "session_end": str(end), "deadline_for": end_src}

    busy = {new_date}
    slots = _next_slots(new_date, weekdays, len(affected), busy)
    shifted, last = [], None
    for e, nd in zip(affected, slots):
        shifted.append({"id": e.id, "chapter": e.chapter, "part": e.part or "",
                        "from": str(e.entry_date), "to": str(nd),
                        "day": WEEK[nd.weekday()]})
        last = nd
    overflow = bool(last and last > end)
    # overflow -> baaki weekdays suggest karo (jinpe abhi class nahi hoti)
    free_days = [WEEK[i] for i in range(7) if i not in weekdays and i != 6]
    return {"shifted": shifted, "weekdays": [WEEK[i] for i in weekdays],
            "overflow": overflow, "last_date": str(last) if last else None,
            "session_end": str(end), "deadline_for": end_src, "suggest_days": free_days,
            "over_by": (last - end).days if overflow and last else 0}


@router.post("/extra-class/preview")
def extra_class_preview(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Extra class daalne se pehle dikhao ki kitni classes shift hongi."""
    tp = get_teacher_profile(current_user, db)
    subject = (payload.get("subject") or "").strip()
    if subject not in (tp.subjects or []):
        raise HTTPException(status_code=400, detail="This is not your subject")
    try:
        nd = _dt2.strptime(payload["date"], "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Please choose a valid date")
    if not bool(payload.get("shift", True)):
        end, src = session_end_for(db, subject=subject)
        return {"shifted": [], "overflow": False, "session_end": str(end), "deadline_for": src}
    return _plan_shift(db, tp, subject, nd)


@router.post("/extra-class")
def create_extra_class(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Extra class request + (optional) baaki classes ka auto-shift.
       Sab kuch admin approval par hi live hota hai."""
    from models import TimetableEntry, User, UserRole, Notification
    tp = get_teacher_profile(current_user, db)
    subject = (payload.get("subject") or "").strip()
    if subject not in (tp.subjects or []):
        raise HTTPException(status_code=400, detail="This is not your subject")
    try:
        nd = _dt2.strptime(payload["date"], "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Please choose a valid date")
    time_text = (payload.get("time") or "").strip()
    chapter = (payload.get("chapter") or payload.get("topic") or "Extra Class").strip()
    part = (payload.get("topic") or "").strip() or None
    do_shift = bool(payload.get("shift", True))

    plan = _plan_shift(db, tp, subject, nd) if do_shift else {"shifted": []}

    e = TimetableEntry(
        teacher_id=tp.id, subject=subject,
        class_name=payload.get("class_name", "Class 12"),
        chapter=chapter, part=part, entry_date=nd, day=WEEK[nd.weekday()],
        time_text=time_text or None, entry_type="chapter", status="pending",
    )
    db.add(e); db.flush()

    # shift ko abhi apply nahi karte — admin approve karega tab hoga
    if plan.get("shifted"):
        e.shift_plan = json.dumps(plan["shifted"])[:60000]

    for adm in db.query(User).filter(User.role == UserRole.admin).all():
        msg = (f"{current_user.name} ne {subject} ki extra class request ki hai "
               f"({nd} {time_text}).")
        if plan.get("shifted"):
            msg += f" Approve karne par {len(plan['shifted'])} aage ki classes auto-shift ho jayengi."
        db.add(Notification(user_id=adm.id, title="New Extra Class Request",
                            message=msg, notif_type="class_request"))
    db.commit(); db.refresh(e)
    return {"id": e.id, "shift_count": len(plan.get("shifted", [])),
            "overflow": plan.get("overflow", False),
            "message": "Request sent to the admin. Once approved, the class and the auto-shift will apply."}

# =====================================================================
# TEACHER ATTENDANCE (PUNCH IN / PUNCH OUT) + CONTRACT + PAYOUT
# =====================================================================
def _ist_now():
    """Railway server UTC pe chalta hai — IST me convert karke store/show karte hain."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def _fmt_t(dt):
    return dt.strftime("%I:%M %p") if dt else None

def _att_hours(a):
    if a and a.punch_in and a.punch_out:
        return round((a.punch_out - a.punch_in).total_seconds() / 3600, 1)
    return None

def _month_range(month: str):
    """'2026-07' -> (date(2026,7,1), date(2026,8,1)). Galat format pe current month."""
    try:
        y, m = month.split("-"); y = int(y); m = int(m)
        assert 1 <= m <= 12
    except Exception:
        n = _ist_now(); y, m = n.year, n.month
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end

# =====================================================================
# PERFORMANCE PAYOUT ENGINE (monthly task-based, 1 Aug 2026 se effective)
# =====================================================================
PAYOUT_PERF_START = "2026-08"   # is month se performance system apply hota hai

# default template: (key, label, source, weight%, target) - total weight 100
PERF_DEFAULT_TEMPLATE = [
    ("live_class", "Live Classes",            "auto",   40, 0),   # target auto = timetable count
    ("dpp",        "DPP (1 per chapter)",     "auto",   15, 0),   # target auto = us month padhe gaye chapters
    ("test",       "Weekly Tests",            "auto",   15, 4),
    ("doubt",      "Doubt Resolution",        "auto",   5,  8),
    ("content",    "Notes / Free Content",    "auto",   5,  4),
    ("oneshot",    "One Shot Videos",         "manual", 8,  8),
    ("rapid",      "Rapid Revision Videos",   "manual", 4,  4),
    ("ytlive",     "YouTube Live Sessions",   "manual", 4,  4),
    ("shorts",     "Shorts",                  "manual", 4,  8),
]

_PAYOUT_TABLES_READY = False
def _ensure_payout_tables(db):
    """payout_templates / payout_tasks / payout_months tables pehli use me bana do
    (server pe Base.metadata.create_all na ho to bhi upgrade ho jaye). MySQL/Postgres
    dono dialects handle; har statement best-effort."""
    global _PAYOUT_TABLES_READY
    if _PAYOUT_TABLES_READY:
        return
    from sqlalchemy import text as _text
    stmts = [
        """CREATE TABLE IF NOT EXISTS payout_templates (
             id INTEGER PRIMARY KEY, teacher_id INTEGER, key VARCHAR(30),
             label VARCHAR(80), target INTEGER DEFAULT 0,
             weight_pct FLOAT DEFAULT 0, source VARCHAR(10) DEFAULT 'manual',
             sort INTEGER DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS ix_payout_templates_teacher ON payout_templates (teacher_id)",
        """CREATE TABLE IF NOT EXISTS payout_tasks (
             id INTEGER PRIMARY KEY, teacher_id INTEGER, month VARCHAR(7),
             key VARCHAR(30), title VARCHAR(200), status VARCHAR(20) DEFAULT 'pending',
             ref_id INTEGER, done_date DATE, note VARCHAR(300),
             approved_by VARCHAR(120), approved_at DATETIME, created_at DATETIME)""",
        "CREATE INDEX IF NOT EXISTS ix_payout_tasks_teacher ON payout_tasks (teacher_id)",
        "CREATE INDEX IF NOT EXISTS ix_payout_tasks_month ON payout_tasks (month)",
        """CREATE TABLE IF NOT EXISTS payout_months (
             id INTEGER PRIMARY KEY, teacher_id INTEGER, month VARCHAR(7),
             status VARCHAR(20) DEFAULT 'finalized', snapshot TEXT,
             finalized_at DATETIME, paid_at DATETIME, created_at DATETIME)""",
        "CREATE INDEX IF NOT EXISTS ix_payout_months_teacher ON payout_months (teacher_id)",
    ]
    for st in stmts:
        try:
            db.execute(_text(st))
            db.commit()
        except Exception:
            db.rollback()
    _PAYOUT_TABLES_READY = True

def _chapter_key(name):
    """Chapter naam ko compare-able key me badalta hai: 'Chapter 3: Motion' -> 'n3',
    'Lesson 3 - X (Part 2)' -> 'n3'. Parts ignore hote hain (kisi bhi part ka DPP chalega).
    Number na mile to normalized string."""
    import re as _re
    s2 = _re.sub(r"\bpart\s*\d+\b", "", (name or "").lower())
    m = _re.search(r"(?:chapter|lesson|ch\.?|path|paath)\s*[-\u2013:.]?\s*(\d+)", s2)
    if m:
        return "n" + m.group(1)
    m2 = _re.search(r"(\d+)", s2)
    if m2:
        return "n" + m2.group(1)
    return _re.sub(r"[^a-z0-9\u0900-\u097F]+", " ", s2).strip()[:40]

def _is_grammar_entry(name):
    """Grammar/writing entries ke liye DPP compulsory nahi (book chapters ke liye hi)."""
    import re as _re
    return bool(_re.search(r"grammar|tense|narration|direct|indirect|voice|clause|modal|preposition|conjunction|writing|essay|letter|notice|punctuation|comprehension", (name or ""), _re.I))

def _perf_seed(db, tp_id):
    """Teacher ka template nahi hai to default bana do (idempotent)."""
    from models import PayoutTemplate
    _ensure_payout_tables(db)
    if db.query(PayoutTemplate).filter(PayoutTemplate.teacher_id == tp_id).count():
        return
    for i, (k, lbl, src, w, tg) in enumerate(PERF_DEFAULT_TEMPLATE):
        db.add(PayoutTemplate(teacher_id=tp_id, key=k, label=lbl, source=src,
                              weight_pct=w, target=tg, sort=i))
    db.commit()

def compute_performance(db, teacher_id: int, month: str):
    """Month ka performance calculation - policy ke 5 rules ke saath:
    1. sab kuch same month me complete -> 100% payout
    2. same month me postpone karke complete -> completed (no deduction)
    3. next month me complete -> previous month ke liye count NAHI (delayed)
    4. delayed sirf record ke liye; next month ka assigned ho tabhi wahan count
    5. assigned/completed/pending/delayed/completion%/payout% sab auto.
    Category weight% ke hisaab se proportional deduction."""
    from models import (PayoutTemplate, PayoutTask, PayoutMonth, TimetableEntry,
                        DPP, Doubt, Material)
    start, end = _month_range(month)
    mk = start.strftime("%Y-%m")
    today = _ist_now().date()
    period_end = min(today, end - timedelta(days=1))
    is_current = (start.year == today.year and start.month == today.month)
    started = mk >= PAYOUT_PERF_START

    _perf_seed(db, teacher_id)
    tpl = db.query(PayoutTemplate).filter(
        PayoutTemplate.teacher_id == teacher_id).order_by(PayoutTemplate.sort, PayoutTemplate.id).all()

    tasks = db.query(PayoutTask).filter(
        PayoutTask.teacher_id == teacher_id, PayoutTask.month == mk).all()
    def _task_out(t):
        delayed = bool(t.done_date and not (start <= t.done_date < end))
        return {"id": t.id, "key": t.key, "title": t.title, "status": t.status,
                "done_date": str(t.done_date) if t.done_date else None,
                "note": t.note or "", "delayed": delayed,
                "approved_by": t.approved_by, "ref_id": t.ref_id,
                "created_at": t.created_at.strftime("%d %b") if t.created_at else ""}

    cats = []
    for t in tpl:
        target = t.target or 0
        done = pending = delayed = 0
        missing = []
        if not started:
            cats.append({"key": t.key, "label": t.label, "source": t.source,
                         "target": 0, "done": 0, "pending": 0, "delayed": 0,
                         "weight": t.weight_pct, "completion": 0})
            continue
        if t.key == "live_class":
            entries = db.query(TimetableEntry).filter(
                TimetableEntry.teacher_id == teacher_id,
                TimetableEntry.entry_date >= start, TimetableEntry.entry_date < end,
                TimetableEntry.entry_type == "chapter",
                TimetableEntry.status == "approved").all()
            missed = {x.ref_id for x in tasks if x.key == "live_class" and x.status == "missed"}
            target = len(entries)
            due = [e for e in entries if e.entry_date and e.entry_date <= period_end and e.id not in missed]
            done = len(due)
            pending = max(0, target - done)
        elif t.source == "auto":
            if t.key == "dpp":
                # RULE: 1 chapter = 1 DPP compulsory. Chapter ke 4 parts ho to kisi
                # bhi 1 part me DPP aa jaye -> chapter complete (baaki parts optional).
                # Kisi bhi part me DPP nahi -> wo chapter miss -> proportional deduction.
                entries = db.query(TimetableEntry).filter(
                    TimetableEntry.teacher_id == teacher_id,
                    TimetableEntry.entry_date >= start, TimetableEntry.entry_date < end,
                    TimetableEntry.entry_type == "chapter",
                    TimetableEntry.status == "approved").all()
                chapters = {}
                for e in entries:
                    if _is_grammar_entry(e.chapter):
                        continue
                    chapters.setdefault(((e.subject or "").strip().lower(), _chapter_key(e.chapter)),
                                        (e.chapter or "").strip())
                target = len(chapters)
                dpps = db.query(DPP).filter(
                    DPP.teacher_id == teacher_id, DPP.is_active == True,
                    DPP.created_at >= start, DPP.created_at < end).all()
                have = {((d.subject or "").strip().lower(), _chapter_key(d.reference)) for d in dpps}
                missing = [nm for k, nm in chapters.items() if k not in have]
                done = target - len(missing)
            elif t.key == "test":
                from models import Exam
                done = db.query(Exam).filter(Exam.teacher_id == teacher_id,
                        Exam.created_at >= start, Exam.created_at < end).count()
            elif t.key == "doubt":
                done = db.query(Doubt).filter(Doubt.teacher_id == teacher_id,
                        Doubt.resolved_at >= start, Doubt.resolved_at < end).count()
            elif t.key == "content":
                done = db.query(Material).filter(Material.teacher_id == teacher_id,
                        Material.created_at >= start, Material.created_at < end).count()
            pending = max(0, target - done) if target else 0
        else:  # manual - approved + done_date is month me ho tabhi count (rule 2/3)
            mine = [x for x in tasks if x.key == t.key and x.status == "approved"]
            in_month = [x for x in mine if x.done_date and start <= x.done_date < end]
            delayed = len([x for x in mine if x.done_date and not (start <= x.done_date < end)])
            done = len(in_month)
            pending = max(0, target - done) if target else 0
        completion = (min(done, target) / target) if target else 0
        row = {"key": t.key, "label": t.label, "source": t.source,
               "target": target, "done": done, "pending": pending,
               "delayed": delayed, "weight": t.weight_pct,
               "completion": round(completion, 4)}
        if t.key == "dpp" and started:
            row["missing"] = sorted(missing)
        cats.append(row)

    # weight renormalize: jin categories ka target 0 hai wo calculation se baahar
    active = [c for c in cats if c["target"] > 0]
    wsum = sum(c["weight"] for c in active) or 1
    perf_pct = sum(c["weight"] * c["completion"] for c in active) / wsum
    totals = {
        "target": sum(c["target"] for c in active),
        "done": sum(min(c["done"], c["target"]) for c in active),
        "pending": sum(c["pending"] for c in active),
        "delayed": sum(c["delayed"] for c in active),
        "completion_pct": round(perf_pct * 100, 1),
    }
    fin = db.query(PayoutMonth).filter(
        PayoutMonth.teacher_id == teacher_id, PayoutMonth.month == mk).first()
    return {
        "month": mk, "started": started, "is_current_month": is_current,
        "perf_start": PAYOUT_PERF_START,
        "perf_ratio": round(perf_pct, 6),
        "categories": cats, "totals": totals,
        "perf_pct": round(perf_pct * 100, 1),
        "tasks": [_task_out(x) for x in sorted(tasks, key=lambda z: (z.key, z.id))],
        "awaiting_approval": len([x for x in tasks if x.status == "pending"]),
        "finalized": bool(fin), "paid": bool(fin and fin.status == "paid"),
        "finalized_at": fin.finalized_at.strftime("%d %b %Y") if fin and fin.finalized_at else None,
    }

def compute_payout(db, teacher_id: int, month: str):
    """Transparent payout breakdown — teacher aur admin dono yahi dekhte hain.
    Net = Base + Allowances + Extras + Bonus - Manual Deductions - Attendance Deduction.
    Attendance Deduction = (working_days - present_days) x per-day rate."""
    from models import TeacherContract, TeacherAttendance, PayoutAdjustment
    c = db.query(TeacherContract).filter(TeacherContract.teacher_id == teacher_id).first()
    if not c:
        return None
    start, end = _month_range(month)
    month_key = start.strftime("%Y-%m")
    present = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == teacher_id,
        TeacherAttendance.att_date >= start, TeacherAttendance.att_date < end,
        TeacherAttendance.punch_in.isnot(None)).count()
    wd = c.working_days or 26
    base = c.base_salary or 0
    per_day = round(base / wd) if wd else 0
    absent = max(0, wd - present)
    att_deduction = per_day * absent
    adjs = db.query(PayoutAdjustment).filter(
        PayoutAdjustment.teacher_id == teacher_id,
        PayoutAdjustment.month == month_key).order_by(PayoutAdjustment.created_at).all()
    extras = sum(a.amount or 0 for a in adjs if a.kind == "extra")
    bonus = sum(a.amount or 0 for a in adjs if a.kind == "bonus")
    manual_ded = sum(a.amount or 0 for a in adjs if a.kind == "deduction")
    perf = compute_performance(db, teacher_id, month)
    gross = base + (c.allowances or 0)
    perf_pct = (perf.get("perf_ratio", perf["perf_pct"] / 100.0)) if (perf and perf["started"]) else 1.0
    perf_pay = round(gross * perf_pct)
    perf_ded = gross - perf_pay
    net = perf_pay + extras + bonus - manual_ded - att_deduction
    now = _ist_now()
    return {
        "month": month_key,
        "is_current_month": (start.year == now.year and start.month == now.month),
        "base_salary": base, "allowances": c.allowances or 0,
        "working_days": wd, "present_days": present, "absent_days": absent,
        "per_day_rate": per_day, "attendance_deduction": att_deduction,
        "extras": extras, "bonus": bonus, "manual_deductions": manual_ded,
        "gross_salary": gross, "performance": perf,
        "perf_pct": perf["perf_pct"] if perf["started"] else None,
        "perf_pay": perf_pay, "perf_deduction": perf_ded,
        "net_payout": net,
        "rules": [r.strip() for r in (c.rules_text or "").splitlines() if r.strip()],
        "adjustments": [{"id": a.id, "kind": a.kind, "amount": a.amount, "note": a.note or ""} for a in adjs],
        "designation": c.designation, "accepted": bool(c.accepted)
    }

# Faculty Service Agreement - Table A-0: sirf GROSS salary input hoti hai,
# breakup in fixed % se automatic banta hai (sabhi teachers ke liye same).
SALARY_SPLIT = [("basic", "Basic Pay", 0.50), ("hra", "House Rent Allowance (HRA)", 0.25),
                ("conveyance", "Conveyance / Transport Allowance", 0.05),
                ("medical", "Medical Reimbursement", 0.03125),
                ("lta", "Leave Travel Allowance (LTA)", 0.04375),
                ("special_allowance", "Special Academic Allowance", 0.125)]

_CONTRACT_COLS_READY = False
def _ensure_contract_columns(db):
    """teacher_contracts me salary-breakup columns pehli use me add karta hai."""
    global _CONTRACT_COLS_READY
    if _CONTRACT_COLS_READY:
        return
    from sqlalchemy import text as _text
    cols = ["basic", "hra", "conveyance", "medical", "lta", "special_allowance"]
    for col in cols:
        for ddl in ("ALTER TABLE teacher_contracts ADD COLUMN %s INTEGER NULL" % col,):
            try:
                db.execute(_text(ddl)); db.commit(); break
            except Exception:
                db.rollback()
    _CONTRACT_COLS_READY = True

def _salary_breakup(gross):
    """Gross se agreement-ke-% me breakup. Rounding ke baad total gross ke
    barabar rahe, isliye last component adjust hota hai."""
    gross = int(gross or 0)
    out, used = {}, 0
    for i, (k, _lbl, pct) in enumerate(SALARY_SPLIT):
        if i == len(SALARY_SPLIT) - 1:
            v = gross - used
        else:
            v = round(gross * pct)
            used += v
        out[k] = max(0, v)
    return out

# Annexure A (Penalty & Deduction Schedule) se auto rules - naye contract me pre-fill
DEFAULT_CONTRACT_RULES = """Har class scheduled time par shuru hogi; bina prior intimation ke 15 minute se zyada der ho to class delayed/missed mani jayegi.
Month me sirf 1 approved class re-scheduling allowed hai; uske baad har approved re-schedule par Rs 300/-, aur bina intimation/approval ke re-schedule par Rs 600/- per class deduction lagega.
Class notes, DPP aur lecture report har class ke baad prescribed interval me upload karna compulsory hai; delay par 1st instance Rs 200/-, 2nd Rs 400/-, 3rd aur uske baad har instance par Rs 700/- auto-deduction hoga.
Portal ke student doubts 24 hours me resolve karo; doubt pending >1 din Rs 100/-, >2 din Rs 300/-, >5 din Rs 600/- per doubt auto-deduction hoga.
Shorts, strategy, promotional aur recording tasks deadline tak submit karo; 1st delay par warning, 2nd delay se Rs 100/- per day deduction submission tak lagega.
Har Sunday doubt class + DPP solutions discussion compulsory hai.
Monthly payout portal verification ke baad next month ki first week me process hoga; salary confidential hai aur payout ke liye sirf designated Account Manager se contact karna hai."""

def _contract_out(c, teacher_name=""):
    gross = (c.base_salary or 0) + (c.allowances or 0)
    if c.basic is not None:
        brk = {"basic": c.basic, "hra": c.hra, "conveyance": c.conveyance,
               "medical": c.medical, "lta": c.lta, "special_allowance": c.special_allowance}
    else:
        brk = _salary_breakup(gross)
    brk_lbl = [{"key": k, "label": lbl, "amount": brk[k]} for k, lbl, _p in SALARY_SPLIT]
    return {
        "exists": True, "teacher_name": teacher_name,
        "designation": c.designation or "Subject Teacher",
        "joining_date": str(c.joining_date) if c.joining_date else None,
        "base_salary": c.base_salary or 0, "allowances": c.allowances or 0,
        "gross_salary": gross, "breakup": brk_lbl,
        "working_days": c.working_days or 26,
        "per_day_rate": round((c.base_salary or 0) / (c.working_days or 26)),
        "rules": [r.strip() for r in (c.rules_text or "").splitlines() if r.strip()],
        "accepted": bool(c.accepted),
        "accepted_at": c.accepted_at.strftime("%d %b %Y, %I:%M %p") if c.accepted_at else None,
        "signature_name": c.signature_name
    }

# ===== ATTENDANCE =====
@router.get("/attendance/today")
def attendance_today(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherAttendance
    now = _ist_now(); today = now.date()
    a = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tp.id, TeacherAttendance.att_date == today).first()
    return {
        "date": str(today), "day": today.strftime("%A"),
        "server_time": now.strftime("%I:%M:%S %p"),
        "punch_in": _fmt_t(a.punch_in if a else None),
        "punch_out": _fmt_t(a.punch_out if a else None),
        "hours": _att_hours(a)
    }

# ===== GEOFENCE (punch sirf office ke radius me) =====
_GEOFENCE_READY = False
def _ensure_geofence(db):
    """app_settings table + attendance ke location columns pehli use me bana/add karta hai."""
    global _GEOFENCE_READY
    if _GEOFENCE_READY:
        return
    from sqlalchemy import text as _text
    try:
        db.execute(_text("CREATE TABLE IF NOT EXISTS app_settings (key VARCHAR(50) PRIMARY KEY, value TEXT NULL)"))
        db.commit()
    except Exception:
        db.rollback()
    for col in ["in_lat FLOAT NULL", "in_lng FLOAT NULL", "in_dist INTEGER NULL", "in_office VARCHAR(80) NULL",
                "out_lat FLOAT NULL", "out_lng FLOAT NULL", "out_dist INTEGER NULL", "out_office VARCHAR(80) NULL"]:
        try:
            db.execute(_text("ALTER TABLE teacher_attendance ADD COLUMN %s" % col)); db.commit()
        except Exception:
            db.rollback()
    _GEOFENCE_READY = True

def _office_list(db):
    """Admin ke saare office branches: [{'name','lat','lng','radius'}, ...]. Empty list = geofence off."""
    from models import AppSetting
    import json as _json
    try:
        rows = {r.key: (r.value or "") for r in db.query(AppSetting).filter(
            AppSetting.key.in_(["offices", "office_lat", "office_lng", "office_radius"])).all()}
    except Exception:
        return []
    out = []
    raw = rows.get("offices") or ""
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, list):
                for o in data:
                    try:
                        lat = float(o.get("lat")); lng = float(o.get("lng"))
                        radius = float(o.get("radius") or 30)
                        name = str(o.get("name") or "Office").strip()[:80] or "Office"
                    except Exception:
                        continue
                    if -90 <= lat <= 90 and -180 <= lng <= 180 and radius > 0:
                        out.append({"name": name, "lat": lat, "lng": lng, "radius": radius})
        except Exception:
            out = []
    if not out and rows.get("office_lat"):
        # purana single-office setup -> auto migrate (naam "Main Office")
        try:
            lat = float(rows.get("office_lat")); lng = float(rows.get("office_lng") or "")
            radius = float(rows.get("office_radius") or 30)
            if -90 <= lat <= 90 and -180 <= lng <= 180 and radius > 0:
                out = [{"name": "Main Office", "lat": lat, "lng": lng, "radius": radius}]
        except Exception:
            out = []
    return out

def _nearest_office(offices, lat, lng):
    """Sabse nazdeek branch aur uska distance (m)."""
    best, best_d = None, None
    for o in offices:
        d = _haversine_m(lat, lng, o["lat"], o["lng"])
        if best is None or d < best_d:
            best, best_d = o, d
    return best, (int(round(best_d)) if best_d is not None else None)

def _haversine_m(lat1, lng1, lat2, lng2):
    """Do GPS points ke beech ka distance, meters me."""
    from math import radians, sin, cos, asin, sqrt
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lng2 - lng1)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * R * asin(sqrt(h))

def _geofence_check(db, lat, lng, accuracy):
    """Koi bhi branch set hai to nearest se distance validate karta hai. Returns (office|None, dist_m).
    Fail ho to HTTPException raise (403 'outside|<branch>|<dist>|<radius>' = geofence breach)."""
    offices = _office_list(db)
    if not offices:
        return None, None      # geofence off - purana behavior
    if lat is None or lng is None:
        raise HTTPException(status_code=400, detail="Location chahiye. Browser me location permission allow karo, tabhi punch hoga.")
    try:
        acc = float(accuracy or 0)
    except Exception:
        acc = 0
    if acc > 80:
        raise HTTPException(status_code=400, detail="Accurate location nahi mil pa rahi (+-%dm error). PC/laptop me GPS nahi hota - mobile ke Chrome/Safari se khuli jagah try karo." % int(acc))
    office, dist = _nearest_office(offices, float(lat), float(lng))
    if dist > office["radius"] + min(acc, 20):
        raise HTTPException(status_code=403, detail="outside|%s|%d|%d" % (office["name"], int(dist), int(office["radius"])))
    return office, dist

@router.get("/geofence")
def teacher_geofence_status(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Punch card pe dikhane ke liye: geofence on hai ya nahi + radius."""
    _ensure_geofence(db)
    offices = _office_list(db)
    if not offices:
        return {"active": False, "offices": []}
    return {"active": True, "offices": [{"name": o["name"], "radius": int(o["radius"])} for o in offices]}

@router.post("/attendance/punch-in")
def punch_in(payload: dict = Body(default={}), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherAttendance
    _ensure_geofence(db)
    office, dist = _geofence_check(db, payload.get("lat"), payload.get("lng"), payload.get("accuracy"))
    now = _ist_now(); today = now.date()
    a = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tp.id, TeacherAttendance.att_date == today).first()
    if a and a.punch_in:
        raise HTTPException(status_code=400, detail=f"You already punched in today at {_fmt_t(a.punch_in)}")
    if not a:
        a = TeacherAttendance(teacher_id=tp.id, att_date=today)
        db.add(a)
    a.punch_in = now
    if office:
        a.in_lat = float(payload.get("lat")); a.in_lng = float(payload.get("lng")); a.in_dist = dist
        a.in_office = office["name"]
    db.commit()
    msg = f"Punched in at {_fmt_t(now)}"
    if office:
        msg += " (%s - office se %dm)" % (office["name"], dist)
    return {"message": msg, "punch_in": _fmt_t(now), "distance": dist,
            "office": office["name"] if office else None}

@router.post("/attendance/punch-out")
def punch_out(payload: dict = Body(default={}), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherAttendance
    _ensure_geofence(db)
    office, dist = _geofence_check(db, payload.get("lat"), payload.get("lng"), payload.get("accuracy"))
    now = _ist_now(); today = now.date()
    a = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tp.id, TeacherAttendance.att_date == today).first()
    if not a or not a.punch_in:
        raise HTTPException(status_code=400, detail="Please punch in first")
    if a.punch_out:
        raise HTTPException(status_code=400, detail=f"You already punched out today at {_fmt_t(a.punch_out)}")
    a.punch_out = now
    if office:
        a.out_lat = float(payload.get("lat")); a.out_lng = float(payload.get("lng")); a.out_dist = dist
        a.out_office = office["name"]
    db.commit()
    msg = f"Punched out at {_fmt_t(now)}"
    if office:
        msg += " (%s - office se %dm)" % (office["name"], dist)
    return {"message": msg, "punch_out": _fmt_t(now), "hours": _att_hours(a), "distance": dist,
            "office": office["name"] if office else None}

@router.get("/attendance/history")
def attendance_history(month: str = "", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherAttendance
    start, end = _month_range(month)
    rows = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tp.id,
        TeacherAttendance.att_date >= start, TeacherAttendance.att_date < end
    ).order_by(TeacherAttendance.att_date.desc()).all()
    out = [{"date": str(r.att_date), "day": r.att_date.strftime("%A"),
            "punch_in": _fmt_t(r.punch_in), "punch_out": _fmt_t(r.punch_out),
            "hours": _att_hours(r)} for r in rows]
    total_hours = round(sum(x["hours"] or 0 for x in out), 1)
    return {"month": start.strftime("%Y-%m"), "rows": out,
            "present_days": sum(1 for x in out if x["punch_in"]), "total_hours": total_hours}

# ===== CONTRACT (APPOINTMENT LETTER) =====
@router.get("/contract")
def my_contract(db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherContract
    c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tp.id).first()
    if not c:
        return {"exists": False}
    return _contract_out(c, current_user.name)

@router.post("/contract/accept")
def accept_contract(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    from models import TeacherContract
    c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tp.id).first()
    if not c:
        raise HTTPException(status_code=404, detail="No appointment letter found for you")
    sig = (payload.get("signature_name") or "").strip()
    if len(sig) < 3:
        raise HTTPException(status_code=400, detail="Please type your full name as your digital signature")
    if not c.accepted:
        c.accepted = True
        c.accepted_at = _ist_now()
        c.signature_name = sig
        db.commit()
    return {"message": "Appointment letter accepted", "accepted_at": c.accepted_at.strftime("%d %b %Y, %I:%M %p")}

# ===== PAYOUT =====
@router.get("/payout")
def my_payout(month: str = "", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    tp = get_teacher_profile(current_user, db)
    p = compute_payout(db, tp.id, month)
    if not p:
        return {"exists": False}
    p["exists"] = True
    p["teacher_name"] = current_user.name
    return p

@router.get("/payout-tasks")
def my_payout_tasks(month: str = "", db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher ke manual-category tasks (marked work) us month ke liye."""
    tp = get_teacher_profile(current_user, db)
    perf = compute_performance(db, tp.id, month)
    return {"tasks": perf["tasks"], "categories": [
        {"key": c["key"], "label": c["label"], "source": c["source"], "target": c["target"]}
        for c in perf["categories"] if c["source"] == "manual" and c["target"] > 0]}

@router.post("/payout-task")
def mark_payout_task(payload: dict = Body(...), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher off-portal kaam (YouTube video, Short, Live session...) done mark
    karta hai. Admin approve karega tab count hoga. done_date us din ka jab kaam
    HUA - same month me hua to count, next month me hua to 'delayed' (policy)."""
    from models import PayoutTask, PayoutTemplate
    tp = get_teacher_profile(current_user, db)
    mk = (payload.get("month") or "").strip() or _ist_now().strftime("%Y-%m")
    if mk < PAYOUT_PERF_START:
        raise HTTPException(400, "Performance payout %s se shuru hoga" % PAYOUT_PERF_START)
    key = (payload.get("key") or "").strip()
    tpl = db.query(PayoutTemplate).filter(
        PayoutTemplate.teacher_id == tp.id, PayoutTemplate.key == key,
        PayoutTemplate.source == "manual").first()
    if not tpl or (tpl.target or 0) <= 0:
        raise HTTPException(400, "Ye category aapke monthly target me nahi hai")
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Kaam ka naam likhna zaroori hai")
    dd = None
    if payload.get("done_date"):
        try:
            dd = datetime.strptime(str(payload["done_date"])[:10], "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(400, "Date format galat hai")
    t = PayoutTask(teacher_id=tp.id, month=mk, key=key, title=title[:200],
                   status="pending", done_date=dd, note=(payload.get("note") or "")[:300])
    db.add(t); db.commit()
    return {"message": "Marked! Admin approve karte hi count ho jayega.", "id": t.id}

@router.delete("/payout-task/{tid}")
def delete_payout_task(tid: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    from models import PayoutTask
    tp = get_teacher_profile(current_user, db)
    t = db.query(PayoutTask).filter(PayoutTask.id == tid, PayoutTask.teacher_id == tp.id).first()
    if not t:
        raise HTTPException(404, "Task nahi mila")
    if t.status == "approved":
        raise HTTPException(400, "Approved task delete nahi ho sakta - admin se bolo")
    db.delete(t); db.commit()
    return {"message": "Task hata diya"}

# ===== TEACHER: CHANGE CLASS SLOT (subject ka time — aage ki saari classes) =====
@router.post("/change-slot")
def change_slot(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Teacher apne subject ka slot (time) change karta hai. Aaj se aage ki saari
    incomplete classes naye time pe shift ho jaati hain. Us subject ke students
    aur admins ko notification jaati hai. Completed/purani classes untouched."""
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry, StudentProfile, User, UserRole
    subject = (payload.get("subject") or "").strip()
    new_time = (payload.get("new_time") or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")
    if not new_time or len(new_time) > 20:
        raise HTTPException(status_code=400, detail="Please enter a valid time, e.g. 9:30 AM")
    if subject not in (tp.subjects or []):
        owned = db.query(TimetableEntry).filter(
            TimetableEntry.subject == subject, TimetableEntry.teacher_id == tp.id).first()
        if not owned:
            raise HTTPException(status_code=403, detail="This subject is not assigned to you")
    today = _ist_now().date()
    entries = db.query(TimetableEntry).filter(
        TimetableEntry.subject == subject,
        TimetableEntry.entry_date.isnot(None),
        TimetableEntry.entry_date >= today,
        TimetableEntry.completed == False
    ).all()
    if not entries:
        raise HTTPException(status_code=400, detail="No upcoming classes found for this subject")
    old_times = sorted({e.time_text for e in entries if e.time_text})
    for e in entries:
        e.time_text = new_time
    db.commit()
    # ---- notifications: subject ke students (fallback: sab students) + admins ----
    eff = today.strftime("%d %b")
    old_str = f" (earlier {', '.join(old_times)})" if old_times else ""
    s_title = "Class Timing Changed"
    s_msg = f"{subject} classes will now start at {new_time} effective {eff}{old_str}. Your timetable has been updated."
    students = db.query(StudentProfile).join(User, StudentProfile.user_id == User.id).filter(User.is_active == True).all()
    matched = [sp for sp in students if subject in (sp.subjects or [])]
    targets = matched if matched else students
    for sp in targets:
        notify(db, sp.user_id, s_title, s_msg, "timetable")
    a_msg = f"{current_user.name} moved {subject} to {new_time}{old_str}. {len(entries)} upcoming classes updated from {eff}."
    for admin in db.query(User).filter(User.role == UserRole.admin, User.is_active == True).all():
        notify(db, admin.id, "Slot Changed - " + subject, a_msg, "timetable")
    db.commit()
    return {"updated": len(entries), "students_notified": len(targets), "new_time": new_time}


# ---------- test editing / status flow (portal v2) ----------
@router.patch("/exam/{exam_id}")
def update_exam(exam_id: int, payload: dict = Body(...), db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Edit a test. Metadata is always updated; questions are fully replaced
    only when a non-empty "questions" list is sent."""
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    for f in ("title", "subject", "chapter", "medium", "test_type"):
        if payload.get(f) is not None:
            setattr(ex, f, payload.get(f))
    if payload.get("duration_min") is not None:
        try:
            ex.duration_min = int(payload.get("duration_min") or 60)
        except Exception:
            pass
    if "scheduled_at" in payload:
        ex.scheduled_at = _exam_parse_dt(payload.get("scheduled_at"))
    qs = payload.get("questions")
    if isinstance(qs, list) and qs:
        ttype = ex.test_type or "subjective"
        db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).delete()
        total = 0
        for i, q in enumerate(qs, start=1):
            try:
                mm = int(q.get("max_marks", 1) or 1)
            except Exception:
                mm = 1
            total += mm
            co = q.get("correct_option")
            opts_hi = q.get("options_hi") if ttype == "mcq" else None
            db.add(ExamQuestion(exam_id=ex.id, q_no=i,
                   question_text=q.get("question_text", ""),
                   max_marks=mm,
                   model_answer=q.get("model_answer"),
                   options=q.get("options") if ttype == "mcq" else None,
                   correct_option=(str(co) if co not in (None, "") else None),
                   image_b64=q.get("image_b64"),
                   question_text_hi=(q.get("question_text_hi") or None),
                   model_answer_hi=(q.get("model_answer_hi") or None),
                   options_hi=(opts_hi if opts_hi else None),
                   model_answer_image=q.get("model_answer_image"),
                   alt_image_b64=q.get("alt_image_b64"),
                   explanation=(q.get("explanation") or None),
                   explanation_hi=(q.get("explanation_hi") or None)))
        ex.total_marks = total
    db.commit()
    return {"id": ex.id, "title": ex.title, "total_marks": ex.total_marks,
            "scheduled_at": ex.scheduled_at.isoformat() if getattr(ex, "scheduled_at", None) else None}


@router.delete("/exam/{exam_id}")
def delete_exam(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Soft-delete a test (keeps attempts/marks, hides it everywhere)."""
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    ex.is_active = False
    db.commit()
    return {"status": "deleted", "id": exam_id}


@router.post("/attempt/{attempt_id}/marking")
def attempt_marking(attempt_id: int, db: Session = Depends(get_db), current_user=Depends(get_teacher)):
    """Flip an attempt from 'checking soon' to 'being checked by teacher'."""
    _ensure_exam_columns(db)
    tp = get_teacher_profile(current_user, db)
    att = db.query(ExamAttempt).filter(ExamAttempt.id == attempt_id).first()
    if not att:
        raise HTTPException(404, "Attempt not found")
    ex = db.query(Exam).filter(Exam.id == att.exam_id, Exam.teacher_id == tp.id).first()
    if not ex:
        raise HTTPException(403, "Not your test")
    if (att.status or "") == "grading":
        att.status = "marking"
        db.commit()
    return {"status": att.status}
