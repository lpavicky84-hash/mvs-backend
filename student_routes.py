from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body, BackgroundTasks, Response
import base64
import re
from sqlalchemy.orm import Session
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_student
import grading
from models import (
    User, StudentProfile, TeacherProfile, ClassEntry, ClassStatus,
    DPP, DPPSubmission, Test, TestSubmission, TestStatus,
    SubmissionStatus, Doubt, DoubtStatus, Notification, Timetable
    , Exam, ExamQuestion, ExamAttempt, ExamResult
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

    from models import Material
    dpps_total    = db.query(Material).filter(Material.subject.in_(sp.subjects or []), Material.material_type == "dpp").count()
    _answers = db.query(Material).filter(Material.student_id == sp.id, Material.material_type == "answer").all()
    _pids = [a.parent_id for a in _answers if a.parent_id]
    _pt = {}
    if _pids:
        for pm in db.query(Material).filter(Material.id.in_(_pids)).all():
            _pt[pm.id] = pm.material_type
    dpps_submitted = sum(1 for a in _answers if _pt.get(a.parent_id) == "dpp")
    tests_attempted = sum(1 for a in _answers if _pt.get(a.parent_id) == "test")
    tests_missed = 0
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

# ===== ACADEMIC WORKSPACE (dashboard aggregation) =====
@router.get("/workspace")
def student_workspace(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Everything the redesigned dashboard needs, computed from real data:
    today's priority, pending work, per-subject material progress, study
    overview, upcoming deadlines, recent activity. No fabricated numbers."""
    from models import Material, MaterialView, TimetableEntry
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    today = date.today()

    # ---- my answer submissions -> which parents are done
    answers = db.query(Material).filter(
        Material.student_id == sp.id, Material.material_type == "answer").all()
    done_parents = set(a.parent_id for a in answers if a.parent_id)

    # ---- DPPs & tests (Material-based) for my subjects
    dpps = db.query(Material).filter(
        Material.subject.in_(subs), Material.material_type == "dpp").all() if subs else []
    tests = db.query(Material).filter(
        Material.subject.in_(subs), Material.material_type == "test").all() if subs else []
    pending_dpps = [m for m in dpps if m.id not in done_parents]
    pending_tests = [m for m in tests if m.id not in done_parents]

    # ---- online exams (ExamAttempt-based)
    pending_exams = []
    try:
        exams = db.query(Exam).filter(Exam.is_active == True).all() if subs else []
        for ex in exams:
            if ex.subject and subs and ex.subject not in subs:
                continue
            att = db.query(ExamAttempt).filter(
                ExamAttempt.exam_id == ex.id, ExamAttempt.student_id == sp.id).first()
            if not att:
                pending_exams.append(ex)
    except Exception:
        pass

    # ---- notes / materials read tracking
    notes = db.query(Material).filter(
        Material.subject.in_(subs), Material.material_type == "notes").all() if subs else []
    viewed_ids = set()
    try:
        for v in db.query(MaterialView).filter(MaterialView.student_id == sp.id).all():
            viewed_ids.add(v.material_id)
    except Exception:
        pass
    all_learn = [m for m in (notes + dpps + tests) ]
    unread = [m for m in all_learn if m.id not in viewed_ids]

    # ---- per-subject material (chapter) progress
    prog = []
    for sub in subs:
        chaps = set()
        done = set()
        for m in all_learn:
            if m.subject != sub:
                continue
            key = (m.chapter or m.title or ("m%d" % m.id))
            chaps.add(key)
            if m.id in viewed_ids or m.id in done_parents:
                done.add(key)
        total = len(chaps)
        prog.append({"subject": sub, "done": len(done), "total": total,
                     "pct": (round(len(done) * 100 / total) if total else 0)})

    # ---- upcoming deadlines from timetable entries flagged as test/exam/assignment
    deadlines = []
    try:
        tt = db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subs)).all() if subs else []
        for e in tt:
            et = (getattr(e, "entry_type", "") or "").lower()
            if e.entry_date and e.entry_date >= today and et in ("test", "exam", "assignment", "dpp"):
                days = (e.entry_date - today).days
                deadlines.append({
                    "subject": e.subject, "title": e.chapter or e.part or et.title(),
                    "type": et, "date": str(e.entry_date), "days_left": days,
                    "urgency": "high" if days <= 1 else ("med" if days <= 3 else "low")})
        deadlines.sort(key=lambda x: x["date"])
    except Exception:
        pass

    # ---- recent activity (my views/downloads/submissions)
    activity = []
    try:
        recent_views = db.query(MaterialView).filter(
            MaterialView.student_id == sp.id).order_by(MaterialView.created_at.desc()).limit(8).all()
        mids = [v.material_id for v in recent_views]
        mmap = {m.id: m for m in db.query(Material).filter(Material.id.in_(mids)).all()} if mids else {}
        for v in recent_views:
            m = mmap.get(v.material_id)
            if not m:
                continue
            verb = "Downloaded" if v.action == "download" else "Opened"
            activity.append({"text": "%s %s \u2014 %s" % (verb, (m.material_type or "material").title(),
                                                          m.title or m.subject or ""),
                             "subject": m.subject, "when": str(v.created_at)[:16]})
    except Exception:
        pass
    for a in sorted(answers, key=lambda x: x.created_at or datetime.min, reverse=True)[:4]:
        activity.append({"text": "Submitted an answer sheet", "subject": a.subject,
                         "when": str(a.created_at)[:16]})
    activity = activity[:8]

    # ---- today's priority: first pending high-value task
    priority = None
    if pending_exams:
        e = pending_exams[0]
        priority = {"kind": "test", "label": "Attempt %s Test" % (e.subject or ""),
                    "title": e.title, "action": "tests"}
    elif pending_tests:
        m = pending_tests[0]
        priority = {"kind": "test", "label": "Attempt %s Test" % (m.subject or ""),
                    "title": m.title, "action": "tests"}
    elif pending_dpps:
        m = pending_dpps[0]
        priority = {"kind": "dpp", "label": "Complete %s DPP" % (m.subject or ""),
                    "title": m.title, "action": "dpp"}
    elif unread:
        m = unread[0]
        priority = {"kind": "material", "label": "Read %s Material" % (m.subject or ""),
                    "title": m.title, "action": "materials"}
    else:
        priority = {"kind": "lecture", "label": "Watch today's lecture",
                    "title": "Open the Manish Verma Classes App", "action": "mvc"}

    # ---- today's tasks checklist
    tasks = [
        {"text": "Watch today's lecture", "done": False, "kind": "lecture"},
        {"text": "Complete a DPP", "done": len(pending_dpps) == 0 and len(dpps) > 0, "kind": "dpp"},
        {"text": "Attempt a Test", "done": (len(pending_tests) + len(pending_exams)) == 0 and (len(tests) > 0 or True is False), "kind": "test"},
        {"text": "Read study notes", "done": len(unread) == 0 and len(all_learn) > 0, "kind": "material"},
    ]

    return {
        "priority": priority,
        "tasks": tasks,
        "pending": {
            "dpps": len(pending_dpps), "tests": len(pending_tests) + len(pending_exams),
            "unread": len(unread), "deadlines_today": sum(1 for d in deadlines if d["days_left"] == 0),
        },
        "overview": {
            "materials_read": len(viewed_ids),
            "materials_total": len(all_learn),
            "dpp_pct": (round((len(dpps) - len(pending_dpps)) * 100 / len(dpps)) if dpps else 0),
            "test_pct": (round((len(tests) - len(pending_tests)) * 100 / len(tests)) if tests else 0),
            "subjects": len(subs),
        },
        "material_progress": prog,
        "deadlines": deadlines[:6],
        "activity": activity,
    }


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
    voice: UploadFile = File(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_student)
):
    import base64
    if not (subject or "").strip():
        raise HTTPException(status_code=400, detail="Please select a subject first")
    sp = get_student_profile(current_user, db)
    # auto-resolve teacher by subject if not provided
    tp = None
    if teacher_id:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_id).first()
    if not tp:
        tp = _teacher_for_subject(db, subject)
    img_b64 = attach_mime = attach_name = None
    if file is not None:
        raw = await file.read()
        if raw:
            if len(raw) > 20 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Attachment is larger than 20MB")
            img_b64 = base64.b64encode(raw).decode("ascii")
            attach_mime = file.content_type or "application/octet-stream"
            attach_name = (file.filename or "attachment")[:250]
    audio_b64 = None
    if voice is not None:
        vraw = await voice.read()
        if vraw:
            if len(vraw) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Voice note is larger than 10MB")
            audio_b64 = base64.b64encode(vraw).decode("ascii")
    doubt = Doubt(student_id=sp.id, teacher_id=(tp.id if tp else None),
                  subject=subject.strip(), topic=topic.strip(), question=question.strip(),
                  image_b64=img_b64, attach_mime=attach_mime, attach_name=attach_name,
                  audio_b64=audio_b64)
    db.add(doubt)
    if tp and tp.user:
        notify(db, tp.user.id, f"Naya Doubt — {current_user.name}",
               f"Subject: {subject} | Topic: {topic} | {question[:100]}", "new_doubt")
    db.commit()
    db.refresh(doubt)
    return {"id": doubt.id, "message": "Doubt bhej diya!" + (f" Teacher: {tp.user.name}" if tp and tp.user else "")}

def _doubt_media(b64, mime, name):
    import base64
    from fastapi import Response
    if not b64:
        raise HTTPException(status_code=404, detail="Not found")
    safe = (name or "file").replace('"', "")
    return Response(content=base64.b64decode(b64),
                    media_type=mime or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{safe}"'})

def _own_doubt(did, db, current_user):
    sp = get_student_profile(current_user, db)
    d = db.query(Doubt).filter(Doubt.id == did, Doubt.student_id == sp.id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Doubt not found")
    return d

@router.get("/doubt/{did}/image")
def student_doubt_image(did: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    d = _own_doubt(did, db, current_user)
    return _doubt_media(d.image_b64, d.attach_mime or "image/jpeg", d.attach_name)

@router.get("/doubt/{did}/voice")
def student_doubt_voice(did: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    d = _own_doubt(did, db, current_user)
    return _doubt_media(d.audio_b64, "audio/webm", "voice.webm")

@router.get("/doubt/{did}/answer-voice")
def student_doubt_answer_voice(did: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    d = _own_doubt(did, db, current_user)
    return _doubt_media(d.answer_audio_b64, "audio/webm", "answer.webm")

@router.get("/doubt/{did}/answer-file")
def student_doubt_answer_file(did: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    d = _own_doubt(did, db, current_user)
    return _doubt_media(d.answer_attach_b64, d.answer_attach_mime, d.answer_attach_name)
    return doubt

@router.get("/doubts")
def my_doubts(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    out = []
    for d in db.query(Doubt).filter(Doubt.student_id == sp.id).order_by(Doubt.created_at.desc()).all():
        out.append({"id": d.id, "subject": d.subject, "topic": d.topic, "question": d.question,
                    "answer": d.answer, "answer_image_link": d.answer_image_link,
                    "status": d.status.value if hasattr(d.status, "value") else d.status,
                    "created_at": str(d.created_at)[:16],
                    "has_file": bool(d.image_b64), "attach_mime": d.attach_mime, "attach_name": d.attach_name,
                    "has_voice": bool(d.audio_b64), "has_answer_voice": bool(d.answer_audio_b64),
                    "has_answer_file": bool(d.answer_attach_b64), "answer_attach_mime": d.answer_attach_mime})
    return out

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

    from models import Material
    _start_dt = datetime.combine(start, datetime.min.time())
    _answers = db.query(Material).filter(
        Material.student_id == sp.id, Material.material_type == "answer",
        Material.created_at >= _start_dt
    ).all()
    _pids = [a.parent_id for a in _answers if a.parent_id]
    _pt = {}
    if _pids:
        for pm in db.query(Material).filter(Material.id.in_(_pids)).all():
            _pt[pm.id] = pm.material_type
    dpps_submitted = sum(1 for a in _answers if _pt.get(a.parent_id) == "dpp")
    tests_attempted = sum(1 for a in _answers if _pt.get(a.parent_id) == "test")

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
        "user_id": current_user.user_id,
        "phone": sp.phone,
        "email": sp.email,
        "class_level": sp.class_level,
        "medium": sp.medium,
        "subjects": sp.subjects or [],
        "batch": sp.batch,
        "batch_name": sp.batch_name,
        "class_name": sp.class_name,
        "has_photo": bool(sp.photo_b64)
    }

@router.get("/available-subjects")
def available_subjects(class_level: str, db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import AvailableSubject
    subs = db.query(AvailableSubject).filter(
        AvailableSubject.class_level == class_level,
        AvailableSubject.is_active == True
    ).all()
    return [{"name": s.name, "code": s.code} for s in subs]

# Batch master list: name -> (class_level, session_bucket)
STUDENT_BATCHES = {
    "Lakshya Science":  ("12", "stream2"),
    "Lakshya Commerce": ("12", "stream2"),
    "Lakshya Arts":     ("12", "stream2"),
    "Manzil Batch":     ("12", "stream2"),
    "Udaan Class 10":   ("10", "stream2"),
    "Aarambh Batch":    ("10", "stream2"),
    "Safalta Batch":    ("12", "syc"),
    "Jeet Batch":       ("10", "syc"),
}

@router.get("/batches")
def student_batches(current_user=Depends(get_student)):
    """Batch list for the onboarding screen."""
    return [{"name": n, "class_level": c[0]} for n, c in STUDENT_BATCHES.items()]

@router.post("/set-subjects")
def set_subjects(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    class_level = payload.get("class_level")
    subjects = payload.get("subjects", [])
    medium = (payload.get("medium") or "").strip()
    batch_name = (payload.get("batch_name") or "").strip()
    if batch_name:
        if batch_name not in STUDENT_BATCHES:
            raise HTTPException(status_code=400, detail="Please select a valid batch")
        class_level = STUDENT_BATCHES[batch_name][0]  # batch decides the class
    if class_level not in ("10", "12"):
        raise HTTPException(status_code=400, detail="Please select Class 10 or 12")
    if medium and medium not in ("Hindi", "English"):
        raise HTTPException(status_code=400, detail="Please select Hindi or English medium")
    if not subjects:
        raise HTTPException(status_code=400, detail="Please select at least one subject")
    if len(subjects) > 7:
        raise HTTPException(status_code=400, detail="Maximum 7 subjects are allowed")
    sp.class_level = class_level
    sp.subjects = subjects
    if medium:
        sp.medium = medium
    if batch_name:
        sp.batch_name = batch_name
    db.commit()
    return {"message": "Profile saved successfully!", "subjects": subjects,
            "class_level": class_level, "medium": sp.medium, "batch_name": sp.batch_name}

# ===== TIMETABLE PLAN (chapter-wise, subject filtered) =====
@router.get("/my-subjects-mode")
def student_subject_modes(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Which of the student's subjects are LIVE (have a timetable) and which are
    RECORDED (watched in the Manish Verma Classes App). Recorded subjects have no
    timetable, so the portal shows a 'watch in the app' card instead of nothing."""
    from models import AvailableSubject
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    if not subs:
        return {"live": [], "recorded": []}
    rows = db.query(AvailableSubject).filter(
        AvailableSubject.name.in_(subs), AvailableSubject.is_active == True).all()
    mode_by_name = {r.name: (r.mode or "live") for r in rows}
    live, rec = [], []
    for s in subs:
        (rec if mode_by_name.get(s, "live") == "recorded" else live).append(s)
    return {"live": live, "recorded": rec}


@router.get("/timetable-plan")
def timetable_plan(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    from models import TimetableEntry
    from sqlalchemy import or_
    es = db.query(TimetableEntry).filter(
        TimetableEntry.subject.in_(sp.subjects or []),
        or_(TimetableEntry.status==None, TimetableEntry.status!='pending')
    ).order_by(TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.entry_date).all()
    # lectures linked to timetable entries (for the "Mark Done" verification)
    lecs = db.query(Lecture).filter(
        Lecture.is_active == True, Lecture.subject.in_(sp.subjects or []),
        Lecture.timetable_entry_id != None).all() if (sp.subjects or []) else []
    lec_by_tt = {}
    for l in lecs:
        lec_by_tt.setdefault(l.timetable_entry_id, l)   # first active lecture per entry
    my_verif = {}
    if lecs:
        lec_ids = [l.id for l in lecs]
        for v in db.query(LectureVerification).filter(
                LectureVerification.student_id == sp.id,
                LectureVerification.lecture_id.in_(lec_ids)).all():
            my_verif[v.lecture_id] = v
    result = []
    for e in es:
        tname = ""
        if e.teacher_id:
            from models import TeacherProfile
            tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
            if tp and tp.user:
                tname = tp.user.name
        lec = lec_by_tt.get(e.id)
        verif_status = None
        cooling = False
        if lec:
            v = my_verif.get(lec.id)
            verif_status = (v.status if v else "pending")
            cooling = bool(v and v.cooldown_until and v.cooldown_until > datetime.utcnow())
        result.append({
            "id": e.id, "subject": e.subject, "class_name": e.class_name,
            "chapter": e.chapter, "part": e.part,
            "date": str(e.entry_date) if e.entry_date else None,
            "day": e.day, "time": getattr(e,"time_text",None),
            "type": getattr(e,"entry_type",None) or "chapter",
            "teacher_id": e.teacher_id, "teacher_name": tname,
            "lecture_id": (lec.id if lec else None),
            "verif_status": verif_status, "cooling": cooling,
        })
    return result

# ===== STUDY MATERIAL (download from DB) =====
@router.get("/question-bank")
def student_question_bank(db: Session = Depends(get_db), current_user=Depends(get_student)):
    from models import Material
    ms = db.query(Material).filter(Material.is_global == True).order_by(Material.created_at.desc()).all()
    return [{"id": m.id, "title": m.title, "category": m.category, "medium": m.medium or "English",
             "subject": m.subject, "has_file": bool(m.content_b64), "external_link": m.external_link,
             "filename": m.filename, "date": str(m.created_at)[:10]} for m in ms]

def _log_material(db, mid, student_id, action):
    """Track student view/download. Views deduped per student; downloads counted each time."""
    try:
        from models import MaterialView
        if action == "view":
            ex = db.query(MaterialView).filter(MaterialView.material_id == mid,
                MaterialView.student_id == student_id, MaterialView.action == "view").first()
            if ex:
                return
        db.add(MaterialView(material_id=mid, student_id=student_id, action=action))
        db.commit()
    except Exception:
        db.rollback()

@router.get("/material/{mid}/view")
def student_material_view(mid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m or not m.content_b64:
        raise HTTPException(status_code=404, detail="Not found")
    sp = get_student_profile(current_user, db)
    _log_material(db, mid, sp.id, "view")
    return Response(content=base64.b64decode(m.content_b64), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{m.filename or "file.pdf"}"'})

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
             "filename": m.filename, "date": str(m.created_at)[:10]} for m in ms]

@router.get("/material/{mid}/download")
def student_download(mid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m: raise HTTPException(status_code=404, detail="Not found")
    sp = get_student_profile(current_user, db)
    _log_material(db, mid, sp.id, "download")
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
    background_tasks: BackgroundTasks = None,
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
    # gamification: reward the submission (XP + streak + activity)
    try:
        _award_xp(db, sp.id, _XP_DPP, "dpp", "Submitted DPP: %s" % (parent.title or parent.subject or ""))
        db.commit()
    except Exception:
        db.rollback()
    # notify the teacher AFTER responding (background) so the student's upload
    # returns instantly instead of waiting on the notification write
    if background_tasks is not None and parent.teacher_id:
        background_tasks.add_task(
            _notify_submission, parent.teacher_id, parent.subject or "",
            parent.title or "", current_user.name)
    return {"id": m.id, "message": "Submit ho gaya! Thank you 🎉"}


def _notify_submission(teacher_id, subject, title, student_name):
    """Runs after the response is sent - notifies the teacher of a new submission."""
    from database import SessionLocal
    from models import TeacherProfile
    db = SessionLocal()
    try:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_id).first()
        if tp and tp.user:
            notify(db, tp.user.id, "📥 Submission: %s" % subject,
                   "%s ne %s ka answer submit kiya hai." % (student_name, title), "submission")
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

# ===== STUDENT: OWN PHOTO + KNOW YOUR TEACHER =====
@router.post("/photo")
async def student_set_photo(file: UploadFile = File(...), db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    sp = get_student_profile(current_user, db)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo 5MB se badi hai")
    sp.photo_b64 = base64.b64encode(raw).decode("ascii")
    db.commit()
    return {"message": "Profile photo set ho gayi!"}

@router.get("/my-photo")
def student_my_photo(db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    sp = get_student_profile(current_user, db)
    if not sp.photo_b64:
        raise HTTPException(status_code=404, detail="Photo nahi")
    return Response(content=base64.b64decode(sp.photo_b64), media_type="image/jpeg")

@router.get("/has-photo")
def student_has_photo(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    return {"has_photo": bool(sp.photo_b64)}

@router.get("/my-teachers")
def student_my_teachers(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Know Your Teacher — student ke subjects ke teachers (photo + name + subject)."""
    from models import TeacherProfile
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    out = []
    seen = set()
    for s in subs:
        tp = _teacher_for_subject(db, s)
        if tp and tp.user:
            key = (tp.id, s)
            if key in seen:
                continue
            seen.add(key)
            out.append({"teacher_id": tp.id, "name": tp.user.name, "subject": s,
                        "has_photo": bool(tp.photo_b64),
                        "suffix": "Ma'am" if (tp.gender or "").lower() == "female" else "Sir"})
    return out

@router.get("/teacher/{tid}/photo")
def student_teacher_photo(tid: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    import base64
    from fastapi import Response
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp or not tp.photo_b64:
        raise HTTPException(status_code=404, detail="Photo nahi")
    return Response(content=base64.b64decode(tp.photo_b64), media_type="image/jpeg")

# ===== LIVE PRESENCE: student heartbeat =====
@router.post("/ping")
def student_ping(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    now = datetime.now()
    if not sp.last_seen or (now - sp.last_seen) > timedelta(minutes=5):
        sp.session_start = now
    sp.last_seen = now
    db.commit()
    return {"ok": True}


# ===================== EXAM / TEST ENGINE (student) =====================
def _exam_save_results(db, att, results):
    for r in results:
        db.add(ExamResult(attempt_id=att.id, q_no=r["q_no"],
               marks_awarded=r["marks"], max_marks=r["max"], remark=r.get("remark", "")))

def _exam_verdict(awarded, total):
    if not total:
        return "Good"
    p = awarded / total * 100
    return "Excellent" if p >= 80 else ("Good" if p >= 50 else "Needs Improvement")

def _exam_verdict_line(verdict, teacher):
    if verdict == "Excellent":
        return "Excellent work! Keep it up. \u2014 %s" % teacher
    if verdict == "Good":
        return "Good effort. A little more practice will help. \u2014 %s" % teacher
    return "This needs improvement. Please revise and try again. \u2014 %s" % teacher

def _exam_thankyou(teacher):
    return "Thank you for submitting your test. Your answers have been received. \u2014 %s" % teacher


def _fmt_marks(v):
    try:
        return ("%g" % float(v))
    except Exception:
        return str(v)


def _notify_exam_result(db, att, ex):
    """Create a student notification when a test result becomes available."""
    try:
        sp = db.query(StudentProfile).filter(StudentProfile.id == att.student_id).first()
        if sp and sp.user_id:
            db.add(Notification(
                user_id=sp.user_id,
                title="Result ready: %s" % (ex.title or "Test"),
                message="Your test has been checked. You scored %s/%s. Tap to view your result and download your answer sheet."
                        % (_fmt_marks(att.total_awarded), ex.total_marks),
                notif_type="exam_result"))
    except Exception:
        pass


def _bg_grade_attempt(attempt_id, mime_type="image/jpeg"):
    """Runs AFTER the response is sent (FastAPI BackgroundTasks) so the upload stays
    fast. Grades the handwritten sheet, saves marks, and notifies the student."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        att = db.query(ExamAttempt).filter(ExamAttempt.id == attempt_id).first()
        if not att or att.status == "graded":
            return
        ex = db.query(Exam).filter(Exam.id == att.exam_id).first()
        if not ex:
            return
        qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).order_by(ExamQuestion.q_no).all()
        results, total, feedback, verdict = grading.grade_subjective(qs, att.answer_image_b64 or "", mime_type)
        if results is None:
            # Could not grade (e.g. AI busy) - leave as 'grading'; teacher can grade/retry.
            return
        teacher = ex.teacher_name or "your teacher"
        db.query(ExamResult).filter(ExamResult.attempt_id == att.id).delete()
        for r in results:
            db.add(ExamResult(attempt_id=att.id, q_no=r["q_no"], marks_awarded=r["marks"],
                              max_marks=r["max"], remark=r.get("remark", "")))
        att.total_awarded = total
        att.status = "graded"
        att.graded_at = datetime.utcnow()
        att.verdict = verdict or _exam_verdict(total, ex.total_marks)
        att.overall_feedback = feedback or _exam_verdict_line(att.verdict, teacher)
        _notify_exam_result(db, att, ex)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

@router.get("/exams")
def student_exams(db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    q = db.query(Exam).filter(Exam.is_active == True)
    if subs:
        q = q.filter(Exam.subject.in_(subs))
    rows = q.order_by(Exam.created_at.desc()).all()
    out = []
    for e in rows:
        att = db.query(ExamAttempt).filter(ExamAttempt.exam_id == e.id, ExamAttempt.student_id == sp.id).order_by(ExamAttempt.submitted_at.desc()).first()
        nq = db.query(ExamQuestion).filter(ExamQuestion.exam_id == e.id).count()
        out.append({"id": e.id, "title": e.title, "subject": e.subject, "chapter": e.chapter,
                    "test_type": e.test_type, "total_marks": e.total_marks, "duration_min": e.duration_min,
                    "questions": nq, "teacher_name": e.teacher_name,
                    "status": att.status if att else "not_attempted",
                    "awarded": att.total_awarded if att else None})
    return out

def _log_exam_action(db, exam_id, student_id, action):
    """Record a student's engagement with a test (once per student per action)."""
    from models import ExamView
    try:
        seen = db.query(ExamView).filter(ExamView.exam_id == exam_id,
                                         ExamView.student_id == student_id,
                                         ExamView.action == action).first()
        if not seen:
            db.add(ExamView(exam_id=exam_id, student_id=student_id, action=action))
            db.commit()
    except Exception:
        db.rollback()


@router.get("/exam/{exam_id}/paper")
def student_exam_paper(exam_id: int, medium: str = "english", db: Session = Depends(get_db),
                       current_user=Depends(get_student)):
    """Download the question paper PDF (counts as a download)."""
    sp = get_student_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.is_active == True).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    if ex.subject and ex.subject not in (sp.subjects or []):
        raise HTTPException(403, "Not your subject")
    qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.q_no).all()
    from exam_pdf import build_exam_pdf
    data = build_exam_pdf(ex, qs, medium)
    _log_exam_action(db, exam_id, sp.id, "download")
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s.pdf"' % (ex.title or "paper")})


@router.get("/exam/{exam_id}")
def student_get_exam(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.is_active == True).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    att = db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == sp.id).first()
    _log_exam_action(db, exam_id, sp.id, "view")
    qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.q_no).all()
    questions = [{"q_no": q.q_no, "question_text": q.question_text, "max_marks": q.max_marks,
                  "question_text_hi": q.question_text_hi,
                  "options": q.options if ex.test_type == "mcq" else None,
                  "options_hi": q.options_hi if ex.test_type == "mcq" else None,
                  "image_b64": q.image_b64} for q in qs]
    return {"id": ex.id, "title": ex.title, "subject": ex.subject, "chapter": ex.chapter,
            "test_type": ex.test_type, "medium": ex.medium, "duration_min": ex.duration_min, "total_marks": ex.total_marks,
            "teacher_name": ex.teacher_name, "questions": questions,
            "already_submitted": bool(att and att.status == "graded")}

@router.post("/exam/{exam_id}/submit")
def student_submit_exam(exam_id: int, payload: dict = Body(...), background_tasks: BackgroundTasks = None, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id, Exam.is_active == True).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    graded = db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == sp.id, ExamAttempt.status == "graded").first()
    if graded:
        raise HTTPException(400, "You have already submitted this test")
    qs = db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).order_by(ExamQuestion.q_no).all()
    db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == sp.id).delete()
    att = ExamAttempt(exam_id=exam_id, student_id=sp.id, student_name=current_user.name, status="grading")
    db.add(att); db.flush()
    try:
        _award_xp(db, sp.id, _XP_TEST, "test", "Attempted test: %s" % (ex.title or ex.subject or ""))
    except Exception:
        pass
    teacher = ex.teacher_name or "your teacher"
    if ex.test_type == "mcq":
        att.mcq_answers = payload.get("mcq_answers") or {}
        results, total = grading.grade_mcq(qs, att.mcq_answers)
        _exam_save_results(db, att, results)
        att.total_awarded = total; att.status = "graded"; att.graded_at = datetime.utcnow()
        att.verdict = _exam_verdict(total, ex.total_marks)
        att.overall_feedback = _exam_verdict_line(att.verdict, teacher)
        _notify_exam_result(db, att, ex)
        db.commit()
        return {"status": "graded", "message": _exam_thankyou(teacher), "teacher_name": teacher}
    # subjective: store the sheet - the TEACHER checks it manually from their portal
    # (AI auto-checking is disabled to keep the system free of API costs)
    img = payload.get("answer_image_b64") or ""
    if not img:
        raise HTTPException(400, "Please upload your handwritten answer sheet")
    att.answer_image_b64 = img
    att.status = "grading"   # shown to the student as "with teacher for checking"
    db.commit()
    return {"status": "grading", "message": _exam_thankyou(teacher), "teacher_name": teacher,
            "note": "Your answer sheet has been received. %s will check it and your marks will appear here with a notification." % teacher}

@router.get("/exam/{exam_id}/result")
def student_exam_result(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    sp = get_student_profile(current_user, db)
    ex = db.query(Exam).filter(Exam.id == exam_id).first()
    if not ex:
        raise HTTPException(404, "Test not found")
    att = db.query(ExamAttempt).filter(ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == sp.id).order_by(ExamAttempt.submitted_at.desc()).first()
    if not att:
        raise HTTPException(404, "No attempt found")
    qmap = {q.q_no: q for q in db.query(ExamQuestion).filter(ExamQuestion.exam_id == exam_id).all()}
    res = db.query(ExamResult).filter(ExamResult.attempt_id == att.id).order_by(ExamResult.q_no).all()
    sel_map = att.mcq_answers or {}
    items = []
    for r in res:
        qq = qmap.get(r.q_no)
        it = {"q_no": r.q_no, "question": (qq.question_text if qq else ""),
              "question_hi": (qq.question_text_hi if qq else None),
              "marks": r.marks_awarded, "max": r.max_marks, "remark": r.remark}
        if ex.test_type == "mcq" and qq:
            your = sel_map.get(str(r.q_no), sel_map.get(r.q_no))
            it.update({
                "options": qq.options or [], "options_hi": qq.options_hi,
                "your_answer": your, "correct_answer": qq.correct_option,
                "is_correct": bool(r.max_marks) and (r.marks_awarded or 0) >= r.max_marks,
                "explanation": qq.explanation, "explanation_hi": qq.explanation_hi,
            })
        items.append(it)
    return {"status": att.status, "title": ex.title, "teacher_name": ex.teacher_name,
            "total_awarded": att.total_awarded, "total_marks": ex.total_marks,
            "verdict": att.verdict, "feedback": att.overall_feedback,
            "test_type": ex.test_type, "medium": ex.medium, "results": items,
            "has_answer": bool(att.answer_image_b64)}


@router.get("/exam/{exam_id}/answer")
def student_answer_sheet(exam_id: int, db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Download the student's own uploaded handwritten answer sheet."""
    sp = get_student_profile(current_user, db)
    att = db.query(ExamAttempt).filter(
        ExamAttempt.exam_id == exam_id, ExamAttempt.student_id == sp.id
    ).order_by(ExamAttempt.submitted_at.desc()).first()
    if not att or not att.answer_image_b64:
        raise HTTPException(404, "No answer sheet found")
    raw = att.answer_image_b64
    mime = "image/jpeg"
    if "," in raw and raw.startswith("data:"):
        header, raw = raw.split(",", 1)
        try:
            mime = header.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
        except Exception:
            mime = "image/jpeg"
    try:
        data = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "Could not read the answer sheet")
    # once checked, stamp the awarded marks on the sheet itself (photo or PDF)
    if (att.status or "") == "graded":
        ex = db.query(Exam).filter(Exam.id == exam_id).first()
        if ex:
            res = db.query(ExamResult).filter(
                ExamResult.attempt_id == att.id).order_by(ExamResult.q_no).all()
            per_q = [{"q_no": r.q_no, "marks": r.marks_awarded, "max": r.max_marks}
                     for r in res]
            from exam_pdf import stamp_marks_on_answer
            data, mime = stamp_marks_on_answer(
                data, mime, att.total_awarded or 0, ex.total_marks, att.verdict,
                per_q=per_q)
    return Response(content=data, media_type=mime)


# ============================================================
#  SMART LECTURE VERIFICATION — STUDENT SIDE + GAMIFICATION
# ============================================================
from models import (Lecture, LectureQuestion, LectureVerification,
                    StudentStats, ActivityLog)

_XP_LECTURE = 20
_XP_DPP = 15
_XP_TEST = 25
_MAX_ATTEMPTS = 3          # wrong tries before a cooldown kicks in
_COOLDOWN_MIN = 10         # minutes to wait after exhausting attempts

_BADGES = [
    ("first_verify", "First Step", "Verify your first lecture"),
    ("five_verify", "Getting Serious", "Verify 5 lectures"),
    ("week_streak", "On Fire", "7-day study streak"),
    ("dpp_10", "DPP Machine", "Submit 10 DPPs"),
    ("test_5", "Test Ace", "Attempt 5 tests"),
    ("xp_500", "Rising Star", "Earn 500 XP"),
]


def _get_stats(db, student_id):
    st = db.query(StudentStats).filter(StudentStats.student_id == student_id).first()
    if not st:
        st = StudentStats(student_id=student_id, xp=0, streak=0, best_streak=0, badges=[])
        db.add(st); db.flush()
    return st


def _touch_streak(st):
    """Update the consecutive-day streak based on today vs last active day."""
    today = date.today()
    if st.last_active_day == today:
        return
    if st.last_active_day == today - timedelta(days=1):
        st.streak = (st.streak or 0) + 1
    else:
        st.streak = 1
    st.best_streak = max(st.best_streak or 0, st.streak)
    st.last_active_day = today


def _award_xp(db, student_id, amount, kind, text):
    st = _get_stats(db, student_id)
    st.xp = (st.xp or 0) + amount
    _touch_streak(st)
    db.add(ActivityLog(student_id=student_id, kind=kind, text=text, xp=amount, day=date.today()))
    _recompute_badges(db, student_id, st)


def _recompute_badges(db, student_id, st):
    have = set(st.badges or [])
    vcount = db.query(LectureVerification).filter(
        LectureVerification.student_id == student_id, LectureVerification.status == "verified").count()
    from models import Material
    dpp_count = db.query(Material).filter(Material.student_id == student_id,
                                          Material.material_type == "answer").count()
    test_count = db.query(ExamAttempt).filter(ExamAttempt.student_id == student_id).count()
    checks = {
        "first_verify": vcount >= 1, "five_verify": vcount >= 5,
        "week_streak": (st.streak or 0) >= 7, "dpp_10": dpp_count >= 10,
        "test_5": test_count >= 5, "xp_500": (st.xp or 0) >= 500,
    }
    for k, ok in checks.items():
        if ok:
            have.add(k)
    st.badges = list(have)


def _check_answer(q, ans):
    """Grade a verification answer. Returns True/False. Never trusts the client
    for correctness - correctness is computed here from the stored answer."""
    ans = ("" if ans is None else str(ans)).strip()
    correct = (q.correct or "").strip()
    if not ans:
        return False
    t = q.qtype
    if t == "true_false":
        return ans.lower() in (correct.lower(), correct.lower()[:1])
    if t == "numerical":
        try:
            got = float(ans); exp = float(correct)
            tol = q.tolerance if q.tolerance is not None else 0.001
            return abs(got - exp) <= tol
        except Exception:
            return ans.lower() == correct.lower()
    if t in ("mcq", "image_mcq"):
        # accept exact option text OR option index (0/1/2/3) OR letter (A/B/C/D)
        if ans.lower() == correct.lower():
            return True
        opts = q.options or []
        idx = None
        if ans.isdigit():
            idx = int(ans)
        elif len(ans) == 1 and ans.upper() in "ABCD":
            idx = "ABCD".index(ans.upper())
        if idx is not None and 0 <= idx < len(opts):
            return (opts[idx] or "").strip().lower() == correct.lower()
        return False
    # fill_blank: case-insensitive, allow comma-separated acceptable answers
    accepts = [a.strip().lower() for a in re.split(r"[|,/]", correct) if a.strip()]
    return ans.lower() in accepts if accepts else (ans.lower() == correct.lower())


def _lecture_public(q, include_correct=False):
    """Serialize a question for the student popup - correct answer NEVER sent."""
    d = {"id": q.id, "qtype": q.qtype, "question": q.question, "question_hi": q.question_hi,
         "has_image": bool(q.image_b64), "options": q.options or [],
         "options_hi": q.options_hi or [],
         "option_images_count": len(q.option_images or [])}
    return d


@router.get("/lectures")
def student_lectures(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Lectures for the student's subjects with their verification state."""
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    lecs = db.query(Lecture).filter(
        Lecture.is_active == True, Lecture.subject.in_(subs)).order_by(
        Lecture.created_at.desc()).all() if subs else []
    out = []
    for l in lecs:
        v = db.query(LectureVerification).filter(
            LectureVerification.lecture_id == l.id, LectureVerification.student_id == sp.id).first()
        cooling = bool(v and v.cooldown_until and v.cooldown_until > datetime.utcnow())
        out.append({
            "id": l.id, "title": l.title, "subject": l.subject, "chapter": l.chapter,
            "part": l.part, "teacher_name": l.teacher_name,
            "date": str(l.lecture_date) if l.lecture_date else str(l.created_at)[:10],
            "summary": l.summary, "homework": l.homework,
            "has_pdf": bool(l.pdf_b64), "has_dpp": bool(l.dpp_b64),
            "status": (v.status if v else "pending"),
            "attempts": (v.attempts if v else 0),
            "cooling": cooling,
            "cooldown_until": (v.cooldown_until.isoformat() if cooling else None),
        })
    return out


@router.get("/lecture/{lecture_id}/question")
def get_verification_question(lecture_id: int, db: Session = Depends(get_db),
                              current_user=Depends(get_student)):
    """Return a RANDOM verification question (without the answer) for the popup."""
    import random
    sp = get_student_profile(current_user, db)
    lec = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.is_active == True).first()
    if not lec:
        raise HTTPException(404, "Lecture not found")
    if lec.subject not in (sp.subjects or []):
        raise HTTPException(403, "Not your subject")
    v = db.query(LectureVerification).filter(
        LectureVerification.lecture_id == lecture_id, LectureVerification.student_id == sp.id).first()
    if v and v.status == "verified":
        raise HTTPException(400, "Already verified")
    if v and v.cooldown_until and v.cooldown_until > datetime.utcnow():
        wait = int((v.cooldown_until - datetime.utcnow()).total_seconds() // 60) + 1
        raise HTTPException(429, "Too many attempts. Try again in %d min." % wait)
    qs = db.query(LectureQuestion).filter(LectureQuestion.lecture_id == lecture_id).all()
    if not qs:
        raise HTTPException(400, "No verification question set for this lecture")
    q = random.choice(qs)
    return {"lecture_id": lecture_id, "question": _lecture_public(q),
            "attempts_left": _MAX_ATTEMPTS - (v.attempts if v else 0)}


@router.get("/lecture/{lecture_id}/file")
def student_lecture_file(lecture_id: int, kind: str = "pdf", db: Session = Depends(get_db),
                         current_user=Depends(get_student)):
    """Download a lecture's notes PDF or DPP PDF."""
    sp = get_student_profile(current_user, db)
    lec = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.is_active == True).first()
    if not lec:
        raise HTTPException(404, "Lecture not found")
    if lec.subject not in (sp.subjects or []):
        raise HTTPException(403, "Not your subject")
    raw = lec.dpp_b64 if kind == "dpp" else lec.pdf_b64
    fname = (lec.dpp_filename if kind == "dpp" else lec.pdf_filename) or ("%s.pdf" % kind)
    if not raw:
        raise HTTPException(404, "File not available")
    try:
        data = base64.b64decode(raw.split(",")[-1])
    except Exception:
        raise HTTPException(400, "Bad file")
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})


@router.get("/lecture-question/{qid}/image")
def lecture_question_image(qid: int, which: int = -1, db: Session = Depends(get_db),
                           current_user=Depends(get_student)):
    q = db.query(LectureQuestion).filter(LectureQuestion.id == qid).first()
    if not q:
        raise HTTPException(404, "Not found")
    raw = None
    if which >= 0:
        imgs = q.option_images or []
        if which < len(imgs):
            raw = imgs[which]
    else:
        raw = q.image_b64
    if not raw:
        raise HTTPException(404, "No image")
    try:
        data = base64.b64decode(raw.split(",")[-1])
    except Exception:
        raise HTTPException(400, "Bad image")
    return Response(content=data, media_type="image/jpeg")


@router.post("/lecture/{lecture_id}/verify")
def verify_lecture(lecture_id: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Grade the student's verification answer. On correct: mark verified, award
    XP, update streak/badges. On wrong: increment attempts, apply cooldown when
    exhausted. Correctness is computed server-side - the client cannot fake it."""
    sp = get_student_profile(current_user, db)
    lec = db.query(Lecture).filter(Lecture.id == lecture_id, Lecture.is_active == True).first()
    if not lec:
        raise HTTPException(404, "Lecture not found")
    if lec.subject not in (sp.subjects or []):
        raise HTTPException(403, "Not your subject")
    qid = payload.get("question_id")
    q = db.query(LectureQuestion).filter(
        LectureQuestion.id == qid, LectureQuestion.lecture_id == lecture_id).first()
    if not q:
        raise HTTPException(400, "Invalid question")

    v = db.query(LectureVerification).filter(
        LectureVerification.lecture_id == lecture_id, LectureVerification.student_id == sp.id).first()
    if not v:
        v = LectureVerification(lecture_id=lecture_id, student_id=sp.id, status="pending", attempts=0)
        db.add(v); db.flush()
    if v.status == "verified":
        return {"correct": True, "already": True, "message": "Already verified"}
    if v.cooldown_until and v.cooldown_until > datetime.utcnow():
        wait = int((v.cooldown_until - datetime.utcnow()).total_seconds() // 60) + 1
        raise HTTPException(429, "Please wait %d min before trying again." % wait)

    ok = _check_answer(q, payload.get("answer"))
    v.attempts = (v.attempts or 0) + 1
    v.last_attempt = datetime.utcnow()

    if ok:
        v.status = "verified"; v.verified_at = datetime.utcnow(); v.xp_awarded = _XP_LECTURE
        v.cooldown_until = None
        _award_xp(db, sp.id, _XP_LECTURE, "lecture", "Verified lecture: %s" % (lec.title or lec.subject))
        db.commit()
        st = _get_stats(db, sp.id)
        return {"correct": True, "message": "Lecture verified! +%d XP" % _XP_LECTURE,
                "xp": st.xp, "streak": st.streak}
    else:
        left = _MAX_ATTEMPTS - v.attempts
        if left <= 0:
            v.cooldown_until = datetime.utcnow() + timedelta(minutes=_COOLDOWN_MIN)
            v.attempts = 0
            db.commit()
            raise HTTPException(429, "Incorrect. Too many tries - wait %d min and revise the lecture." % _COOLDOWN_MIN)
        db.commit()
        return {"correct": False, "attempts_left": left,
                "message": "Incorrect answer. Revise the lecture and try again. (%d left)" % left}


# ============================================================
#  ACADEMIC PERFORMANCE DASHBOARD (analytics + leaderboard)
# ============================================================
def _student_xp_map(db):
    """XP for every student (from StudentStats), used for global ranking."""
    rows = db.query(StudentStats).all()
    return {r.student_id: (r.xp or 0) for r in rows}


def _compute_ranks(db, sp):
    """Real batch + overall ranks across all students, by XP."""
    xp_map = _student_xp_map(db)
    all_students = db.query(StudentProfile).all()
    # ensure every student has an xp entry (default 0)
    scored = []
    for s in all_students:
        scored.append((s.id, xp_map.get(s.id, 0), (s.batch_name or "")))
    # overall
    ordered = sorted(scored, key=lambda x: x[1], reverse=True)
    overall_rank = next((i + 1 for i, (sid, _, _) in enumerate(ordered) if sid == sp.id), None)
    total = len(ordered) or 1
    # batch
    my_batch = sp.batch_name or ""
    batch_list = sorted([x for x in scored if x[2] == my_batch], key=lambda x: x[1], reverse=True)
    batch_rank = next((i + 1 for i, (sid, _, _) in enumerate(batch_list) if sid == sp.id), None)
    top_pct = round((overall_rank / total) * 100) if overall_rank else 100
    return {"overall_rank": overall_rank, "overall_total": total,
            "batch_rank": batch_rank, "batch_total": len(batch_list) or 1,
            "top_percent": top_pct}


@router.get("/performance")
def student_performance(db: Session = Depends(get_db), current_user=Depends(get_student)):
    """Everything the Academic Performance Dashboard needs, all from real data."""
    from models import Material, MaterialView, TimetableEntry
    sp = get_student_profile(current_user, db)
    subs = sp.subjects or []
    today = date.today()
    st = _get_stats(db, sp.id)

    # ---- lectures & verification
    lecs = db.query(Lecture).filter(Lecture.is_active == True, Lecture.subject.in_(subs)).all() if subs else []
    lec_ids = [l.id for l in lecs]
    my_verifs = db.query(LectureVerification).filter(
        LectureVerification.student_id == sp.id).all()
    verified_ids = set(v.lecture_id for v in my_verifs if v.status == "verified")
    lectures_total = len(lecs)
    lectures_verified = sum(1 for lid in lec_ids if lid in verified_ids)

    # ---- DPP & tests (real)
    answers = db.query(Material).filter(
        Material.student_id == sp.id, Material.material_type == "answer").all()
    done_parents = set(a.parent_id for a in answers if a.parent_id)
    dpps = db.query(Material).filter(Material.subject.in_(subs), Material.material_type == "dpp").all() if subs else []
    dpp_total = len(dpps); dpp_done = sum(1 for m in dpps if m.id in done_parents)

    exam_attempts = db.query(ExamAttempt).filter(ExamAttempt.student_id == sp.id).all()
    graded = [a for a in exam_attempts if (a.status or "") == "graded"]
    exams_all = db.query(Exam).filter(Exam.is_active == True).all()
    exams_mine = [e for e in exams_all if (not e.subject) or (e.subject in subs)]
    test_total = len(exams_mine)
    test_done = len(exam_attempts)
    # avg test score %
    pct_list = []
    for a in graded:
        ex = next((e for e in exams_all if e.id == a.exam_id), None)
        if ex and ex.total_marks:
            pct_list.append(min(100, round((a.total_awarded or 0) / ex.total_marks * 100)))
    avg_test = round(sum(pct_list) / len(pct_list)) if pct_list else 0

    # ---- materials read
    viewed = set()
    try:
        for v in db.query(MaterialView).filter(MaterialView.student_id == sp.id).all():
            viewed.add(v.material_id)
    except Exception:
        pass
    notes = db.query(Material).filter(Material.subject.in_(subs), Material.material_type == "notes").all() if subs else []
    learn = notes + dpps
    read_pct = round(len([m for m in learn if m.id in viewed]) * 100 / len(learn)) if learn else 0

    # ---- subject-wise progress (blend of verified lectures + dpp done + notes read)
    subj_prog = []
    radar = []
    for sub in subs:
        s_lecs = [l for l in lecs if l.subject == sub]
        s_lec_v = sum(1 for l in s_lecs if l.id in verified_ids)
        s_dpp = [m for m in dpps if m.subject == sub]
        s_dpp_d = sum(1 for m in s_dpp if m.id in done_parents)
        s_notes = [m for m in notes if m.subject == sub]
        s_notes_r = sum(1 for m in s_notes if m.id in viewed)
        parts = []
        if s_lecs: parts.append(s_lec_v / len(s_lecs))
        if s_dpp: parts.append(s_dpp_d / len(s_dpp))
        if s_notes: parts.append(s_notes_r / len(s_notes))
        pct = round(sum(parts) / len(parts) * 100) if parts else 0
        subj_prog.append({"subject": sub, "pct": pct,
                          "lectures": "%d/%d" % (s_lec_v, len(s_lecs)),
                          "dpp": "%d/%d" % (s_dpp_d, len(s_dpp))})
        radar.append({"subject": sub, "value": pct})

    # ---- chapter completion cards (per subject/chapter, from lectures)
    chapters = {}
    for l in lecs:
        key = (l.subject, l.chapter or "General")
        c = chapters.setdefault(key, {"subject": l.subject, "chapter": l.chapter or "General",
                                      "total": 0, "done": 0})
        c["total"] += 1
        if l.id in verified_ids:
            c["done"] += 1
    chapter_cards = []
    for c in chapters.values():
        c["pct"] = round(c["done"] * 100 / c["total"]) if c["total"] else 0
        chapter_cards.append(c)
    chapter_cards.sort(key=lambda x: (-x["pct"], x["subject"]))

    # ---- overall progress + academic health score (weighted, 0-100)
    lec_pct = round(lectures_verified * 100 / lectures_total) if lectures_total else 0
    dpp_pct = round(dpp_done * 100 / dpp_total) if dpp_total else 0
    test_attempt_pct = round(test_done * 100 / test_total) if test_total else 0
    overall = round((lec_pct + dpp_pct + test_attempt_pct + read_pct) / 4)
    # health score = weighted mix incl. consistency & test quality
    consistency = min(100, (st.streak or 0) * 12)
    health = round(0.30 * lec_pct + 0.20 * dpp_pct + 0.20 * avg_test +
                   0.15 * read_pct + 0.15 * consistency)
    health = max(0, min(100, health))
    health_label = ("Excellent" if health >= 80 else "Good" if health >= 60
                    else "Needs Work" if health >= 40 else "At Risk")

    # ---- weekly/monthly/quarterly performance graphs (XP per day buckets)
    def series(days):
        start = today - timedelta(days=days - 1)
        buckets = {}
        logs = db.query(ActivityLog).filter(
            ActivityLog.student_id == sp.id, ActivityLog.day >= start).all()
        for lg in logs:
            k = str(lg.day)
            buckets[k] = buckets.get(k, 0) + (lg.xp or 0)
        out = []
        for i in range(days):
            d = start + timedelta(days=i)
            out.append({"label": d.strftime("%d/%m"), "value": buckets.get(str(d), 0)})
        return out
    graphs = {"weekly": series(7), "monthly": series(30), "quarterly": series(90)}

    # ---- consistency calendar (last ~17 weeks, GitHub style)
    cal_start = today - timedelta(days=119)
    cal_logs = db.query(ActivityLog).filter(
        ActivityLog.student_id == sp.id, ActivityLog.day >= cal_start).all()
    day_xp = {}
    for lg in cal_logs:
        day_xp[str(lg.day)] = day_xp.get(str(lg.day), 0) + (lg.xp or 0)
    calendar = []
    for i in range(120):
        d = cal_start + timedelta(days=i)
        xp = day_xp.get(str(d), 0)
        lvl = 0 if xp == 0 else 1 if xp < 20 else 2 if xp < 40 else 3 if xp < 70 else 4
        calendar.append({"date": str(d), "level": lvl})

    # ---- ranks + movement
    ranks = _compute_ranks(db, sp)
    movement = None
    if st.prev_rank and ranks["overall_rank"]:
        movement = st.prev_rank - ranks["overall_rank"]  # +ve = moved up
    # store current as prev for next time (rank movement tracker)
    st.prev_rank = ranks["overall_rank"]

    # ---- badges (unlocked list with meta)
    have = set(st.badges or [])
    badges = [{"key": k, "name": n, "desc": d, "earned": (k in have)} for (k, n, d) in _BADGES]

    # ---- activity timeline
    acts = db.query(ActivityLog).filter(ActivityLog.student_id == sp.id).order_by(
        ActivityLog.created_at.desc()).limit(10).all()
    timeline = [{"kind": a.kind, "text": a.text, "xp": a.xp, "when": str(a.created_at)[:16]} for a in acts]

    # ---- upcoming targets (nudges from real gaps)
    targets = []
    pend_lec = lectures_total - lectures_verified
    if pend_lec > 0:
        targets.append({"text": "Verify %d pending lecture%s" % (pend_lec, "s" if pend_lec != 1 else ""), "kind": "lecture"})
    if (dpp_total - dpp_done) > 0:
        targets.append({"text": "Complete %d DPP%s" % (dpp_total - dpp_done, "s" if (dpp_total - dpp_done) != 1 else ""), "kind": "dpp"})
    if (test_total - test_done) > 0:
        targets.append({"text": "Attempt %d test%s" % (test_total - test_done, "s" if (test_total - test_done) != 1 else ""), "kind": "test"})
    if (st.streak or 0) < 7:
        targets.append({"text": "Reach a 7-day streak (%d/7)" % (st.streak or 0), "kind": "streak"})
    if not targets:
        targets.append({"text": "You're all caught up - keep the streak alive!", "kind": "done"})

    # ---- AI-style insights (rule-based, no API cost)
    insights = []
    if lectures_total and lec_pct < 50:
        insights.append("Aap ne sirf %d%% lectures verify kiye hain - regular verification se ranking tezi se badhegi." % lec_pct)
    if avg_test and avg_test < 50 and pct_list:
        insights.append("Test average %d%% hai. Weak chapters revise karke dobara attempt karein." % avg_test)
    if (st.streak or 0) >= 3:
        insights.append("Bahut badhiya! %d-din ki streak chal rahi hai - rozana thoda padhke isse aur lamba karein." % st.streak)
    if subj_prog:
        weakest = min(subj_prog, key=lambda x: x["pct"])
        if weakest["pct"] < 60:
            insights.append("%s sabse peeche hai (%d%%). Is week ka focus %s pe rakhein." % (weakest["subject"], weakest["pct"], weakest["subject"]))
    if not insights:
        insights.append("Aap consistent ja rahe hain - isi tarah rozana thoda-thoda karke top rank ki taraf badhein!")

    db.commit()  # persist prev_rank + any badge/stat touch

    return {
        "health": {"score": health, "label": health_label},
        "overall_pct": overall,
        "xp": st.xp or 0, "streak": st.streak or 0, "best_streak": st.best_streak or 0,
        "level": (st.xp or 0) // 100 + 1,
        "subject_progress": subj_prog,
        "graphs": graphs,
        "chapter_cards": chapter_cards[:12],
        "lectures": {"verified": lectures_verified, "total": lectures_total, "pct": lec_pct},
        "dpp": {"done": dpp_done, "total": dpp_total, "pct": dpp_pct},
        "tests": {"done": test_done, "total": test_total, "attempt_pct": test_attempt_pct,
                  "avg_score": avg_test, "graded": len(graded)},
        "leaderboard": ranks,
        "rank_movement": movement,
        "calendar": calendar,
        "badges": badges,
        "radar": radar,
        "timeline": timeline,
        "targets": targets,
        "insights": insights,
    }

# ==================================================================
#  MANISH VERMA CLASSES — LIVE PLAY STORE INFO
#  Play Store listing se rating/reviews/downloads/icon nikaal ke 12h
#  cache karte hain. Scrape fail ho to known values fallback.
# ==================================================================
import re as _re
import time as _time

_APP_INFO_CACHE = {"at": 0, "data": None}
_APP_PKG = "com.manish.verma.classes"
_APP_FALLBACK = {"name": "Manish Verma Classes", "dev": "MVS FOUNDATION",
                 "rating": "4.6", "reviews": "1.53K", "downloads": "50K+",
                 "icon": None, "live": False}


@router.get("/app-info")
def mvc_app_info(current_user=Depends(get_student)):
    now = _time.time()
    if _APP_INFO_CACHE["data"] and now - _APP_INFO_CACHE["at"] < 43200:
        return _APP_INFO_CACHE["data"]
    data = dict(_APP_FALLBACK)
    try:
        import httpx
        r = httpx.get(f"https://play.google.com/store/apps/details?id={_APP_PKG}&hl=en",
                      timeout=12, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            h = r.text
            m = _re.search(r'Rated ([0-9.]+) stars', h)
            if m:
                data["rating"] = m.group(1)
                data["live"] = True
            m = _re.search(r'([\d.,]+[KMB]?)\s*reviews', h)
            if m:
                data["reviews"] = m.group(1)
            m = _re.search(r'>([\d.,]+[KMB]\+)<[^>]*>(?:</div>)?\s*<div[^>]*>Downloads', h)
            if not m:
                m = _re.search(r'([\d.,]+[KMB]\+)(?=(?:(?!Downloads).){0,80}Downloads)', h, _re.S)
            if m:
                data["downloads"] = m.group(1)
            m = _re.search(r'property="og:image" content="([^"]+)"', h)
            if m:
                data["icon"] = m.group(1)
    except Exception:
        pass
    _APP_INFO_CACHE["at"] = now
    _APP_INFO_CACHE["data"] = data
    return data
