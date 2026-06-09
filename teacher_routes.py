from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_teacher, get_current_user
from models import (
    User, TeacherProfile, ClassEntry, ClassStatus,
    RescheduleRequest, RescheduleStatus, DPP, Test, Doubt,
    DoubtStatus, Timetable, Notification, TestStatus
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

    # Check monthly limit
    if tp.reschedule_count_this_month >= 2:
        raise HTTPException(
            status_code=429,
            detail="LIMIT_REACHED: Aapne is mahine ki 2 reschedule limit poori kar li hai. Next month active hoga."
        )

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
@router.get("/doubts", response_model=List[DoubtOut])
def get_doubts(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    tp = get_teacher_profile(current_user, db)
    q = db.query(Doubt).filter(Doubt.teacher_id == tp.id)
    if status:
        q = q.filter(Doubt.status == status)
    return q.order_by(Doubt.created_at.desc()).all()

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
        "time": getattr(e, "time_text", None), "type": getattr(e, "entry_type", None) or "chapter"
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
    tp = get_teacher_profile(current_user, db)
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id, TimetableEntry.teacher_id == tp.id).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entry nahi mili")
    db.delete(e); db.commit()
    return {"message": "Delete ho gaya"}

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
    if replace.lower() == "true":
        db.query(TimetableEntry).filter(
            TimetableEntry.teacher_id == tp.id,
            TimetableEntry.subject.in_(subjects_found)
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
    db.commit()
    return _serialize_tt(e)

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
    material_type: str = Form("notes"),   # notes | dpp | test
    title: str = Form(""),
    duration_min: int = Form(0),
    db: Session = Depends(get_db),
    current_user=Depends(get_teacher)
):
    import base64
    from models import Material
    tp = get_teacher_profile(current_user, db)
    raw = await file.read()
    if len(raw) > 7 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File 7MB se badi hai. Chhoti PDF use karein.")
    b64 = base64.b64encode(raw).decode("ascii")
    m = Material(
        teacher_id=tp.id, teacher_name=current_user.name, subject=subject.strip(),
        class_name=class_name.strip(), chapter=chapter.strip(),
        material_type=material_type.strip(), title=(title.strip() or file.filename),
        filename=file.filename, content_b64=b64,
        duration_min=(duration_min or None)
    )
    db.add(m); db.commit(); db.refresh(m)
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
    es = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs)).order_by(
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
    es = db.query(TimetableEntry).filter(
        TimetableEntry.subject.in_(subs), TimetableEntry.entry_date == today).all()
    mats = db.query(Material).filter(Material.subject.in_(subs)).all()
    out = []
    for e in es:
        notes = any(m.chapter == e.chapter and m.subject == e.subject and m.material_type == "notes" for m in mats)
        dpp = any(m.chapter == e.chapter and m.subject == e.subject and m.material_type == "dpp" for m in mats)
        d = _serialize_tt(e); d["notes"] = notes; d["dpp"] = dpp
        out.append(d)
    out.sort(key=lambda x: x.get("time") or "")
    return out
