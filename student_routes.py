from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_student
from models import (
    User, StudentProfile, TeacherProfile, ClassEntry, ClassStatus,
    DPP, DPPSubmission, Test, TestSubmission, TestStatus,
    SubmissionStatus, Doubt, DoubtStatus, Notification, Timetable
)
from schemas import (
    DPPSubmissionCreate, DPPSubmissionOut,
    TestSubmissionCreate, TestSubmissionOut,
    DoubtCreate, DoubtOut,
    StudentDashboard
)

router = APIRouter(prefix="/api/student", tags=["Student"])

def get_student_profile(user, db) -> StudentProfile:
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student profile nahi mila")
    return sp

def notify(db, user_id, title, message, notif_type):
    n = Notification(user_id=user_id, title=title, message=message, notif_type=notif_type)
    db.add(n)

# ===== DASHBOARD =====
@router.get("/dashboard", response_model=StudentDashboard)
def student_dashboard(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)

    dpps_total    = db.query(DPP).filter(DPP.subject.in_(sp.subjects or [])).count()
    dpps_submitted = db.query(DPPSubmission).filter(DPPSubmission.student_id == sp.id).count()
    tests_attempted = db.query(TestSubmission).filter(
        TestSubmission.student_id == sp.id,
        TestSubmission.status.in_([SubmissionStatus.submitted, SubmissionStatus.late_submitted])
    ).count()
    tests_missed = db.query(TestSubmission).filter(
        TestSubmission.student_id == sp.id,
        TestSubmission.status == SubmissionStatus.missed
    ).count()
    doubts_asked    = db.query(Doubt).filter(Doubt.student_id == sp.id).count()
    doubts_resolved = db.query(Doubt).filter(Doubt.student_id == sp.id, Doubt.status == DoubtStatus.resolved).count()

    # Classes attended = test submissions + a rough count based on activity
    classes_attended = dpps_submitted  # proxy count

    return StudentDashboard(
        classes_attended=classes_attended,
        dpps_submitted=dpps_submitted,
        dpps_total=dpps_total,
        tests_attempted=tests_attempted,
        tests_missed=tests_missed,
        doubts_asked=doubts_asked,
        doubts_resolved=doubts_resolved
    )

# ===== TIMETABLE (subject-filtered) =====
@router.get("/timetable")
def get_student_timetable(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    entries = db.query(Timetable).filter(
        Timetable.subject.in_(sp.subjects or []),
        Timetable.is_active == True
    ).order_by(Timetable.day_of_week, Timetable.start_time).all()
    return entries

# ===== TODAY'S CLASSES =====
@router.get("/classes/today")
def today_classes(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    classes = db.query(ClassEntry).filter(
        ClassEntry.subject.in_(sp.subjects or []),
        ClassEntry.scheduled_date == date.today()
    ).order_by(ClassEntry.scheduled_time).all()
    result = []
    for c in classes:
        teacher_name = ""
        if c.teacher and c.teacher.user:
            teacher_name = c.teacher.user.name
        result.append({
            "id": c.id,
            "subject": c.subject,
            "class_name": c.class_name,
            "topic": c.topic,
            "scheduled_time": str(c.scheduled_time),
            "status": c.status,
            "drive_link": c.drive_link,
            "teacher_name": teacher_name
        })
    return result

# ===== MATERIALS (class notes + DPPs) =====
@router.get("/materials/notes")
def get_notes(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Get all uploaded class notes (PDFs) for student's subjects"""
    sp = get_student_profile(current_user, db)
    classes = db.query(ClassEntry).filter(
        ClassEntry.subject.in_(sp.subjects or []),
        ClassEntry.status == ClassStatus.done,
        ClassEntry.drive_link != None
    ).order_by(ClassEntry.scheduled_date.desc()).all()
    return [
        {
            "id": c.id,
            "subject": c.subject,
            "topic": c.topic,
            "date": str(c.scheduled_date),
            "drive_link": c.drive_link
        }
        for c in classes
    ]

@router.get("/materials/dpps")
def get_available_dpps(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    dpps = db.query(DPP).filter(
        DPP.subject.in_(sp.subjects or []),
        DPP.is_active == True
    ).all()
    # Mark which ones student has submitted
    submitted_ids = {s.dpp_id for s in db.query(DPPSubmission).filter(DPPSubmission.student_id == sp.id).all()}
    return [
        {
            "id": d.id,
            "subject": d.subject,
            "dpp_type": d.dpp_type,
            "reference": d.reference,
            "drive_link": d.drive_link,
            "submitted": d.id in submitted_ids
        }
        for d in dpps
    ]

# ===== DPP SUBMISSION =====
@router.post("/dpp/submit", response_model=DPPSubmissionOut)
def submit_dpp(req: DPPSubmissionCreate, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)

    # Check not already submitted
    existing = db.query(DPPSubmission).filter(
        DPPSubmission.dpp_id == req.dpp_id,
        DPPSubmission.student_id == sp.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Yeh DPP aap pehle se submit kar chuke hain")

    sub = DPPSubmission(dpp_id=req.dpp_id, student_id=sp.id, drive_link=req.drive_link)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub

@router.get("/dpp/submissions")
def my_dpp_submissions(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    subs = db.query(DPPSubmission).filter(DPPSubmission.student_id == sp.id).all()
    return subs

# ===== TESTS =====
@router.get("/tests")
def get_student_tests(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    tests = db.query(Test).filter(
        Test.subject.in_(sp.subjects or []),
        Test.question_paper_link != None   # Only show if paper uploaded
    ).order_by(Test.test_date.desc()).all()

    submitted_test_ids = {s.test_id for s in db.query(TestSubmission).filter(TestSubmission.student_id == sp.id).all()}
    now = datetime.now()
    result = []
    for t in tests:
        test_deadline = datetime.combine(t.test_date, t.test_time)
        submission_deadline = test_deadline + timedelta(hours=6)
        time_left_secs = max(0, int((test_deadline + timedelta(minutes=t.duration_mins) - now).total_seconds()))
        can_submit = now < submission_deadline
        result.append({
            "id": t.id,
            "subject": t.subject,
            "class_name": t.class_name,
            "test_date": str(t.test_date),
            "test_time": str(t.test_time),
            "duration_mins": t.duration_mins,
            "question_paper_link": t.question_paper_link,
            "status": t.status,
            "submitted": t.id in submitted_test_ids,
            "can_submit": can_submit,
            "time_left_secs": time_left_secs
        })
    return result

@router.post("/tests/submit", response_model=TestSubmissionOut)
def submit_test(req: TestSubmissionCreate, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)

    test = db.query(Test).filter(Test.id == req.test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Test nahi mila")

    now = datetime.now()
    test_end = datetime.combine(test.test_date, test.test_time) + timedelta(minutes=test.duration_mins)
    submission_deadline = datetime.combine(test.test_date, test.test_time) + timedelta(hours=6)

    if now > submission_deadline:
        raise HTTPException(status_code=400, detail="Submission window band ho gayi (6 ghante baad)")

    # Determine status
    if now <= test_end:
        sub_status = SubmissionStatus.submitted
    else:
        sub_status = SubmissionStatus.late_submitted

    existing = db.query(TestSubmission).filter(
        TestSubmission.test_id == req.test_id,
        TestSubmission.student_id == sp.id
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Test pehle se submit hai")

    sub = TestSubmission(test_id=req.test_id, student_id=sp.id, drive_link=req.drive_link, status=sub_status)
    db.add(sub)
    db.commit()
    db.refresh(sub)

    # Notify teacher
    if test.teacher and test.teacher.user:
        notify(db, test.teacher.user.id,
               f"Test Submitted — {current_user.name}",
               f"{current_user.name} ne {test.subject} test submit ki ({sub_status})",
               "test_submitted")
    db.commit()
    return sub

# ===== DOUBTS =====
def _teacher_for_subject(db, subject):
    for tp in db.query(TeacherProfile).all():
        if tp.subjects and subject in tp.subjects:
            return tp
    return None

@router.get("/teacher-for-subject")
def teacher_for_subject(subject: str, db: Session = Depends(get_db), current_user=Depends(get_student)):
    tp = _teacher_for_subject(db, subject)
    if not tp or not tp.user:
        return {"found": False, "teacher_name": None, "teacher_id": None}
    return {"found": True, "teacher_name": tp.user.name, "teacher_user_id": tp.user.user_id, "teacher_id": tp.id}

@router.post("/doubts")
async def ask_doubt(
    subject: str = Form(...),
    topic: str = Form(""),
    question: str = Form(...),
    teacher_id: int = Form(0),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_student)
):
    import base64
    sp = get_student_profile(current_user, db)
    # auto-resolve teacher by subject if not provided
    tp = None
    if teacher_id:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_id).first()
    if not tp:
        tp = _teacher_for_subject(db, subject)
    img_b64 = None
    if file is not None:
        raw = await file.read()
        if raw:
            if len(raw) > 20 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Image 20MB se badi hai")
            img_b64 = base64.b64encode(raw).decode("ascii")
    doubt = Doubt(student_id=sp.id, teacher_id=(tp.id if tp else None),
                  subject=subject.strip(), topic=topic.strip(), question=question.strip(),
                  image_b64=img_b64)
    db.add(doubt)
    if tp and tp.user:
        notify(db, tp.user.id, f"Naya Doubt — {current_user.name}",
               f"Subject: {subject} | Topic: {topic} | {question[:100]}", "new_doubt")
    db.commit()
    db.refresh(doubt)
    return {"id": doubt.id, "message": "Doubt bhej diya!" + (f" Teacher: {tp.user.name}" if tp and tp.user else "")}

@router.get("/doubt/{did}/image")
def student_doubt_image(did: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    d = db.query(Doubt).filter(Doubt.id == did).first()
    if not d or not d.image_b64:
        raise HTTPException(status_code=404, detail="Image nahi")
    return Response(content=base64.b64decode(d.image_b64), media_type="image/jpeg")
    return doubt

@router.get("/doubts", response_model=List[DoubtOut])
def my_doubts(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    return db.query(Doubt).filter(Doubt.student_id == sp.id).order_by(Doubt.created_at.desc()).all()

# ===== PROGRESS =====
@router.get("/progress")
def get_progress(
    period: str = "weekly",
    db: Session = Depends(get_db),
    current_user=Depends(get_student)
):
    sp = get_student_profile(current_user, db)
    now = date.today()

    if period == "weekly":
        start = now - timedelta(days=now.weekday())
    elif period == "monthly":
        start = date(now.year, now.month, 1)
    else:  # quarterly
        quarter_month = ((now.month - 1) // 3) * 3 + 1
        start = date(now.year, quarter_month, 1)

    dpps_submitted = db.query(DPPSubmission).filter(
        DPPSubmission.student_id == sp.id,
        DPPSubmission.submitted_at >= datetime.combine(start, datetime.min.time())
    ).count()

    tests_attempted = db.query(TestSubmission).filter(
        TestSubmission.student_id == sp.id,
        TestSubmission.submitted_at >= datetime.combine(start, datetime.min.time()),
        TestSubmission.status.in_([SubmissionStatus.submitted, SubmissionStatus.late_submitted])
    ).count()

    doubts_asked = db.query(Doubt).filter(
        Doubt.student_id == sp.id,
        Doubt.created_at >= datetime.combine(start, datetime.min.time())
    ).count()

    return {
        "period": period,
        "from": str(start),
        "to": str(now),
        "dpps_submitted": dpps_submitted,
        "tests_attempted": tests_attempted,
        "doubts_asked": doubts_asked
    }

# ===== NOTIFICATIONS =====
@router.get("/notifications")
def get_notifications(db: Session = Depends(get_db), current_user=Depends(get_student)):
    return db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).order_by(Notification.created_at.desc()).limit(20).all()

@router.patch("/notifications/{notif_id}/read")
def mark_read(notif_id: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    n = db.query(Notification).filter(Notification.id == notif_id, Notification.user_id == current_user.id).first()
    if n:
        n.is_read = True
        db.commit()
    return {"ok": True}

# ===== PROFILE & SUBJECT SELECTION =====
@router.get("/profile")
def get_profile(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    return {
        "name": current_user.name,
        "class_level": sp.class_level,
        "subjects": sp.subjects or [],
        "batch": sp.batch,
        "class_name": sp.class_name
    }

@router.get("/available-subjects")
def available_subjects(class_level: str, db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import AvailableSubject
    subs = db.query(AvailableSubject).filter(
        AvailableSubject.class_level == class_level,
        AvailableSubject.is_active == True
    ).all()
    return [{"name": s.name, "code": s.code} for s in subs]

@router.post("/set-subjects")
def set_subjects(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    class_level = payload.get("class_level")
    subjects = payload.get("subjects", [])
    if class_level not in ("10", "12"):
        raise HTTPException(status_code=400, detail="Class 10 ya 12 select karein")
    if not subjects:
        raise HTTPException(status_code=400, detail="Kam se kam 1 subject select karein")
    if len(subjects) > 7:
        raise HTTPException(status_code=400, detail="7 se jyada subjects allowed nahi hain")
    sp.class_level = class_level
    sp.subjects = subjects
    db.commit()
    return {"message": "Subjects save ho gaye!", "subjects": subjects, "class_level": class_level}

# ===== TIMETABLE PLAN (chapter-wise, subject filtered) =====
@router.get("/timetable-plan")
def timetable_plan(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    from models import TimetableEntry
    from sqlalchemy import or_
    es = db.query(TimetableEntry).filter(
        TimetableEntry.subject.in_(sp.subjects or []),
        or_(TimetableEntry.status==None, TimetableEntry.status!='pending')
    ).order_by(TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.entry_date).all()
    result = []
    for e in es:
        tname = ""
        if e.teacher_id:
            from models import TeacherProfile
            tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
            if tp and tp.user:
                tname = tp.user.name
        result.append({
            "id": e.id, "subject": e.subject, "class_name": e.class_name,
            "chapter": e.chapter, "part": e.part,
            "date": str(e.entry_date) if e.entry_date else None,
            "day": e.day, "time": getattr(e,"time_text",None),
            "type": getattr(e,"entry_type",None) or "chapter", "teacher_name": tname
        })
    return result

# ===== STUDY MATERIAL (download from DB) =====
@router.get("/materials-v2")
def student_materials_v2(db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import Material
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    ms = db.query(Material).filter(
        Material.subject.in_(subs),
        Material.material_type.in_(["notes", "dpp", "other"])
    ).order_by(Material.subject, Material.chapter, Material.created_at.desc()).all()
    return [{"id": m.id, "subject": m.subject, "chapter": m.chapter, "type": m.material_type,
             "category": m.category, "title": m.title, "teacher_name": m.teacher_name,
             "date": str(m.created_at)[:10]} for m in ms]

@router.get("/material/{mid}/download")
def student_download(mid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m: raise HTTPException(status_code=404, detail="Nahi mila")
    data = base64.b64decode(m.content_b64)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{m.filename or "file.pdf"}"'})

# ===== STUDENT: DPP / TEST LIST (download + submit) =====
def _my_submission(db, sp, parent_id):
    from models import Material
    return db.query(Material).filter(
        Material.material_type == "answer", Material.parent_id == parent_id,
        Material.student_id == sp.id).first()

@router.get("/dpp-list")
def student_dpp_list(db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import Material
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    ms = db.query(Material).filter(Material.subject.in_(subs),
                                   Material.material_type == "dpp").order_by(Material.created_at.desc()).all()
    out = []
    for m in ms:
        sub = _my_submission(db, sp, m.id)
        out.append({"id": m.id, "subject": m.subject, "chapter": m.chapter, "title": m.title,
                    "teacher_name": m.teacher_name, "date": str(m.created_at)[:10],
                    "submitted": bool(sub), "submission_id": sub.id if sub else None, "marks": sub.marks if sub else None})
    return out

@router.get("/tests-list")
def student_tests_list(db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import Material
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    ms = db.query(Material).filter(Material.subject.in_(subs),
                                   Material.material_type == "test").order_by(Material.created_at.desc()).all()
    out = []
    for m in ms:
        sub = _my_submission(db, sp, m.id)
        out.append({"id": m.id, "subject": m.subject, "chapter": m.chapter, "title": m.title,
                    "teacher_name": m.teacher_name, "duration_min": m.duration_min,
                    "date": str(m.created_at)[:10],
                    "submitted": bool(sub), "submission_id": sub.id if sub else None, "marks": sub.marks if sub else None})
    return out

@router.post("/submit-answer")
async def submit_answer(
    file: UploadFile = File(...),
    parent_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_student)
):
    import base64
    from models import Material
    sp = get_student_profile(current_user, db)
    parent = db.query(Material).filter(Material.id == parent_id).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Item nahi mila")
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File 20MB se badi hai")
    # remove previous submission (resubmit)
    old = _my_submission(db, sp, parent_id)
    if old:
        db.delete(old); db.flush()
    m = Material(
        teacher_id=parent.teacher_id, teacher_name=parent.teacher_name,
        subject=parent.subject, chapter=parent.chapter,
        material_type="answer", title=f"{current_user.name} - {parent.title}",
        filename=file.filename, content_b64=base64.b64encode(raw).decode("ascii"),
        parent_id=parent_id, student_id=sp.id, student_name=current_user.name
    )
    db.add(m); db.commit(); db.refresh(m)
    # notify the teacher who uploaded
    try:
        from models import TeacherProfile
        if parent.teacher_id:
            tp2 = db.query(TeacherProfile).filter(TeacherProfile.id == parent.teacher_id).first()
            if tp2 and tp2.user:
                notify(db, tp2.user.id, f"📥 Submission: {parent.subject}",
                       f"{current_user.name} ne {parent.title} ka answer submit kiya hai.", "submission")
                db.commit()
    except Exception:
        db.rollback()
    return {"id": m.id, "message": "Submit ho gaya! Thank you 🎉"}
