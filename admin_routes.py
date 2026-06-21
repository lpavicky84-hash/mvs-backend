from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_admin
from models import (
    User, TeacherProfile, StudentProfile, ClassEntry, ClassStatus,
    RescheduleRequest, RescheduleStatus, Doubt, DoubtStatus,
    DPP, Test, TestSubmission, DPPSubmission, Notification, UserRole
)
from schemas import (
    RescheduleReview, RescheduleOut, UserOut, AdminDashboard,
    RegisterRequest
)
from security import hash_password

router = APIRouter(prefix="/api/admin", tags=["Admin"])

def notify(db, user_id: int, title: str, message: str, notif_type: str):
    n = Notification(user_id=user_id, title=title, message=message, notif_type=notif_type)
    db.add(n)

# ===== DASHBOARD =====
@router.get("/dashboard", response_model=AdminDashboard)
def admin_dashboard(db: Session = Depends(get_db), _=Depends(get_admin)):
    total_teachers  = db.query(User).filter(User.role == UserRole.teacher).count()
    total_students  = db.query(User).filter(User.role == UserRole.student).count()
    total_done      = db.query(ClassEntry).filter(ClassEntry.status == ClassStatus.done).count()
    total_pending   = db.query(ClassEntry).filter(ClassEntry.status == ClassStatus.pending).count()
    pending_rs      = db.query(RescheduleRequest).filter(RescheduleRequest.status == RescheduleStatus.pending).count()
    unresolved      = db.query(Doubt).filter(Doubt.status == DoubtStatus.pending).count()

    return AdminDashboard(
        total_teachers=total_teachers, total_students=total_students,
        total_classes_done=total_done, total_pending=total_pending,
        pending_reschedules=pending_rs, unresolved_doubts=unresolved
    )

# ===== RESCHEDULE APPROVALS =====
@router.get("/reschedules/pending", response_model=List[RescheduleOut])
def get_pending_reschedules(db: Session = Depends(get_db), _=Depends(get_admin)):
    return db.query(RescheduleRequest).filter(
        RescheduleRequest.status == RescheduleStatus.pending
    ).order_by(RescheduleRequest.created_at.desc()).all()

@router.patch("/reschedules/{rs_id}/review")
def review_reschedule(
    rs_id: int,
    req: RescheduleReview,
    db: Session = Depends(get_db),
    current_admin=Depends(get_admin)
):
    rs = db.query(RescheduleRequest).filter(RescheduleRequest.id == rs_id).first()
    if not rs:
        raise HTTPException(status_code=404, detail="Request nahi mili")
    if rs.status != RescheduleStatus.pending:
        raise HTTPException(status_code=400, detail="Yeh request already process ho chuki hai")

    rs.status = req.status
    rs.admin_note = req.admin_note
    rs.reviewed_at = datetime.now()

    class_entry = rs.class_entry
    teacher_user = db.query(User).filter(User.id == rs.teacher.user_id).first()

    if req.status == RescheduleStatus.approved:
        # Update class date/time
        class_entry.scheduled_date = rs.new_date
        class_entry.scheduled_time = rs.new_time
        class_entry.status = ClassStatus.rescheduled

        # Increment teacher's reschedule count
        rs.teacher.reschedule_count_this_month += 1

        # Notify teacher
        if teacher_user:
            notify(db, teacher_user.id,
                   "✅ Reschedule Approved!",
                   f"{class_entry.subject} ({class_entry.class_name}) reschedule approved ho gaya. Nayi date: {rs.new_date}, {rs.new_time}",
                   "reschedule_approved")

        # Notify all students of affected class (filter in Python — works on all DBs)
        all_students = db.query(StudentProfile).all()
        students = [sp for sp in all_students if sp.subjects and class_entry.subject in sp.subjects]
        for sp in students:
            if sp.user:
                notify(db, sp.user.id,
                       f"📅 Class Rescheduled — {class_entry.subject}",
                       f"{teacher_user.name if teacher_user else 'Teacher'} ki {class_entry.subject} class {rs.original_date} se {rs.new_date}, {rs.new_time} pe ho gayi.",
                       "class_rescheduled")
    else:
        # Rejected — revert class to pending
        class_entry.status = ClassStatus.pending
        if teacher_user:
            notify(db, teacher_user.id,
                   "❌ Reschedule Rejected",
                   f"{class_entry.subject} ({class_entry.class_name}) ki reschedule request reject ho gayi. Note: {req.admin_note or 'No reason given'}",
                   "reschedule_rejected")

    db.commit()
    return {"message": f"Reschedule {req.status} kar diya. Teacher ko notification chali gayi."}

# ===== TEACHER MANAGEMENT =====
@router.get("/teachers")
def get_all_teachers(db: Session = Depends(get_db), _=Depends(get_admin)):
    teachers = db.query(User).filter(User.role == UserRole.teacher).all()
    result = []
    for t in teachers:
        profile = t.teacher_profile
        now = datetime.now()
        month_start = date(now.year, now.month, 1)
        week_start = date.today() - timedelta(days=date.today().weekday())

        if profile:
            classes_done = db.query(ClassEntry).filter(
                ClassEntry.teacher_id == profile.id,
                ClassEntry.status == ClassStatus.done
            ).count()
            monthly_done = db.query(ClassEntry).filter(
                ClassEntry.teacher_id == profile.id,
                ClassEntry.status == ClassStatus.done,
                ClassEntry.scheduled_date >= month_start
            ).count()
            result.append({
                "id": t.id,
                "name": t.name,
                "user_id": t.user_id,
                "is_active": t.is_active,
                "subjects": profile.subjects,
                "batch": profile.batch,
                "total_classes_done": classes_done,
                "monthly_classes_done": monthly_done,
                "reschedule_this_month": profile.reschedule_count_this_month,
                "reschedule_limit": 2
            })
    return result

@router.post("/teachers/add")
def add_teacher(req: RegisterRequest, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Admin adds a teacher — auto-generates a professional MVS user ID"""
    # Generate professional teacher ID: MVS + initials + number  (e.g. MVSVV01)
    parts = req.name.strip().split()
    initials = "".join(p[0] for p in parts[:2]).upper() if parts else "TR"
    i = 1
    while True:
        candidate = f"MVS{initials}{i:02d}"
        if not db.query(User).filter(User.user_id == candidate).first():
            break
        i += 1
    user = User(
        name=req.name,
        user_id=candidate,
        password=hash_password(req.password),
        role=UserRole.teacher,
        is_active=True
    )
    db.add(user)
    db.flush()

    profile = TeacherProfile(
        user_id=user.id,
        subjects=req.subjects or [],
        subject_classes=[],
        gender=(req.gender or "").strip().lower() or None,
        batch=req.batch or "",
    )
    db.add(profile)
    db.commit()
    return {"message": f"Teacher {req.name} add ho gaya!", "user_id": candidate}

@router.patch("/teachers/{user_id}/toggle")
def toggle_teacher(user_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    user = db.query(User).filter(User.id == user_id, User.role == UserRole.teacher).first()
    if not user:
        raise HTTPException(status_code=404, detail="Teacher nahi mila")
    user.is_active = not user.is_active
    db.commit()
    status = "active" if user.is_active else "inactive"
    return {"message": f"Teacher {user.name} ab {status} hai"}

# ===== STUDENT MANAGEMENT =====
@router.get("/students")
def get_all_students(db: Session = Depends(get_db), _=Depends(get_admin)):
    students = db.query(User).filter(User.role == UserRole.student).all()
    result = []
    for s in students:
        sp = s.student_profile
        if sp:
            dpp_submitted  = db.query(DPPSubmission).filter(DPPSubmission.student_id == sp.id).count()
            test_attempted = db.query(TestSubmission).filter(TestSubmission.student_id == sp.id).count()
            result.append({
                "id": s.id,
                "name": s.name,
                "user_id": s.user_id,
                "phone": sp.phone,
                "batch": sp.batch,
                "subjects": sp.subjects,
                "class_name": sp.class_name,
                "is_verified": sp.is_verified,
                "is_active": s.is_active,
                "dpp_submitted": dpp_submitted,
                "tests_attempted": test_attempted,
            })
    return result

@router.post("/students/add")
def add_student(req: RegisterRequest, db: Session = Depends(get_db), _=Depends(get_admin)):
    existing = db.query(User).filter(User.user_id == req.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Yeh User ID already hai")
    if req.phone:
        existing_phone = db.query(StudentProfile).filter(StudentProfile.phone == req.phone).first()
        if existing_phone:
            raise HTTPException(status_code=400, detail="Phone number already registered")

    user = User(
        name=req.name, user_id=req.user_id,
        password=hash_password(req.password),
        role=UserRole.student, is_active=True
    )
    db.add(user)
    db.flush()

    sp = StudentProfile(
        user_id=user.id, phone=req.phone,
        batch=req.batch, subjects=req.subjects or [],
        class_name=req.class_name or "", is_verified=True,
        plain_password=req.password
    )
    db.add(sp)
    db.commit()
    return {"message": f"Student {req.name} add ho gaya!"}

# ===== TEACHER ACTIVITY MONITOR =====
@router.get("/activity")
def teacher_activity(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Complete activity of all teachers"""
    teachers = db.query(TeacherProfile).all()
    now = datetime.now()
    month_start = date(now.year, now.month, 1)
    result = []
    for tp in teachers:
        user = tp.user
        for subject in (tp.subjects or []):
            done = db.query(ClassEntry).filter(
                ClassEntry.teacher_id == tp.id,
                ClassEntry.subject == subject,
                ClassEntry.status == ClassStatus.done
            ).count()
            pending = db.query(ClassEntry).filter(
                ClassEntry.teacher_id == tp.id,
                ClassEntry.subject == subject,
                ClassEntry.status == ClassStatus.pending
            ).count()
            dpps  = db.query(DPP).filter(DPP.teacher_id == tp.id, DPP.subject == subject).count()
            tests = db.query(Test).filter(Test.teacher_id == tp.id, Test.subject == subject).count()
            doubts_resolved = db.query(Doubt).filter(
                Doubt.teacher_id == tp.id,
                Doubt.subject == subject,
                Doubt.status == DoubtStatus.resolved
            ).count()
            result.append({
                "teacher_name": user.name if user else "Unknown",
                "subject": subject,
                "classes_done": done,
                "classes_pending": pending,
                "dpps_given": dpps,
                "tests_conducted": tests,
                "doubts_resolved": doubts_resolved,
                "reschedules_this_month": tp.reschedule_count_this_month
            })
    return result

# ===== ADMIN USER MANAGEMENT =====
@router.post("/admins/add")
def add_admin(req: RegisterRequest, db: Session = Depends(get_db), _=Depends(get_admin)):
    existing = db.query(User).filter(User.user_id == req.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Yeh User ID already hai")
    user = User(
        name=req.name, user_id=req.user_id,
        password=hash_password(req.password),
        role=UserRole.admin, is_active=True
    )
    db.add(user)
    db.commit()
    return {"message": f"Admin {req.name} add ho gaya! User ID: {req.user_id}"}

# ===== NOTIFICATIONS TO ALL =====
@router.post("/broadcast")
def broadcast_notification(
    title: str, message: str, target_role: Optional[str] = None,
    db: Session = Depends(get_db), _=Depends(get_admin)
):
    q = db.query(User).filter(User.is_active == True)
    if target_role:
        q = q.filter(User.role == target_role)
    users = q.all()
    for u in users:
        notify(db, u.id, title, message, "broadcast")
    db.commit()
    return {"message": f"{len(users)} users ko notification bhej di gayi."}

# ===== SUBJECT MANAGEMENT =====
@router.get("/subjects")
def get_subjects(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AvailableSubject
    subs = db.query(AvailableSubject).filter(AvailableSubject.is_active == True).all()
    result = {"10": [], "12": []}
    for s in subs:
        result.get(s.class_level, []).append({"id": s.id, "name": s.name, "code": s.code})
    return result

@router.delete("/subjects/{subject_id}")
def delete_subject(subject_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AvailableSubject
    s = db.query(AvailableSubject).filter(AvailableSubject.id == subject_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Subject nahi mila")
    s.is_active = False   # soft delete
    db.commit()
    return {"message": f"{s.name} delete ho gaya"}

@router.post("/subjects")
def add_subject(class_level: str, name: str, code: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AvailableSubject
    if class_level not in ("10", "12"):
        raise HTTPException(status_code=400, detail="class_level 10 ya 12 hona chahiye")
    s = AvailableSubject(class_level=class_level, name=name, code=code, is_active=True)
    db.add(s)
    db.commit()
    return {"message": f"{name} add ho gaya"}

# ===== TIMETABLE (all teachers) =====
@router.get("/timetable-all")
def timetable_all(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile
    es = db.query(TimetableEntry).order_by(
        TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.entry_date
    ).all()
    result = []
    for e in es:
        tname = ""
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

# ===== ADMIN: PDF TIMETABLE UPLOAD (all subjects) =====
@router.post("/timetable-pdf")
async def admin_upload_timetable_pdf(
    file: UploadFile = File(...),
    class_name: str = Form("Class 12"),
    subject: str = Form(""),
    replace: str = Form("false"),
    db: Session = Depends(get_db),
    _=Depends(get_admin)
):
    from models import TimetableEntry
    import tt_parser
    raw = await file.read()
    try:
        rows = tt_parser.parse_pdf(raw, force_subject=(subject.strip() or None))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF parse error: {e}")
    if not rows:
        raise HTTPException(status_code=400, detail="PDF se koi valid row nahi mili.")
    subjects_found = sorted(set(r["subject"] for r in rows))
    if replace.lower() == "true":
        db.query(TimetableEntry).filter(TimetableEntry.subject.in_(subjects_found)).delete(synchronize_session=False)
    added = 0
    for r in rows:
        edate = None
        try: edate = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except Exception: pass
        db.add(TimetableEntry(
            teacher_id=None, subject=r["subject"], class_name=class_name,
            chapter=r["chapter"], part=r["part"], entry_date=edate,
            day=r["day"] or None, time_text=r["time"] or None, entry_type=r["type"]
        ))
        added += 1
    db.commit()
    return {"added": added, "subjects": subjects_found}

# ===== ADMIN: SEND NOTIFICATION (target teachers/students/all) =====
@router.post("/notify")
def admin_notify(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    target = (payload.get("target") or "all").strip()   # teachers | students | all
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title aur message zaroori hain")
    q = db.query(User).filter(User.is_active == True, User.role != "admin")
    if target == "teachers":
        q = q.filter(User.role == "teacher")
    elif target == "students":
        q = q.filter(User.role == "student")
    users = q.all()
    for u in users:
        notify(db, u.id, "📢 " + title, message, "admin_broadcast")
    db.commit()
    return {"message": f"{len(users)} logo ko notification bhej di!", "count": len(users)}

# ===== ADMIN: MATERIAL UPLOAD (direct PDF) + pending view =====
@router.post("/material")
async def admin_upload_material(
    file: UploadFile = File(...),
    subject: str = Form(...),
    class_name: str = Form("Class 12"),
    chapter: str = Form(""),
    material_type: str = Form("notes"),
    title: str = Form(""),
    category: str = Form(""),
    duration_min: int = Form(0),
    db: Session = Depends(get_db),
    _=Depends(get_admin)
):
    import base64
    from models import Material, StudentProfile
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File 20MB se badi hai")
    m = Material(
        teacher_id=None, teacher_name="Admin", subject=subject.strip(),
        class_name=class_name.strip(), chapter=chapter.strip(),
        material_type=material_type.strip(), title=(title.strip() or file.filename),
        category=(category.strip() or None), filename=file.filename,
        content_b64=base64.b64encode(raw).decode("ascii"),
        duration_min=(duration_min or None)
    )
    db.add(m); db.commit(); db.refresh(m)
    # notify students of subject
    try:
        label = {"notes": "Class Notes", "dpp": "DPP", "test": "Test"}.get(material_type.strip(), (category.strip() or "Material"))
        for sp in db.query(StudentProfile).all():
            if sp.subjects and subject.strip() in sp.subjects and sp.user:
                n = Notification(user_id=sp.user.id, title=f"📚 New {label}: {subject.strip()}",
                                 message=f"Admin ne {subject.strip()} ke liye {label} upload ki hai.", notif_type="new_material")
                db.add(n)
        db.commit()
    except Exception:
        db.rollback()
    return {"id": m.id, "message": "Upload ho gaya!"}

@router.get("/material/{mid}/download")
def admin_download(mid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m: raise HTTPException(status_code=404, detail="Nahi mila")
    return Response(content=base64.b64decode(m.content_b64), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{m.filename or "file.pdf"}"'})

@router.get("/pending-materials")
def admin_pending_materials(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Chapters (from timetable) jinki notes ya dpp abhi upload nahi hui."""
    from models import TimetableEntry, Material
    chapters = db.query(TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.teacher_id).filter(
        TimetableEntry.entry_type == "chapter").distinct().all()
    mats = db.query(Material).all()
    out = []
    for subj, ch, tid in chapters:
        if not ch: continue
        notes = any(m.subject == subj and m.chapter == ch and m.material_type == "notes" for m in mats)
        dpp = any(m.subject == subj and m.chapter == ch and m.material_type == "dpp" for m in mats)
        if not notes or not dpp:
            tname = None
            if tid:
                tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
                tname = tp.user.name if tp and tp.user else None
            out.append({"subject": subj, "chapter": ch, "teacher": tname,
                        "notes": notes, "dpp": dpp})
    return out

# ===== ADMIN: EXTRA-CLASS APPROVAL =====
@router.get("/pending-classes")
def admin_pending_classes(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile
    es = db.query(TimetableEntry).filter(TimetableEntry.status == "pending").order_by(TimetableEntry.entry_date).all()
    out = []
    for e in es:
        tname = None
        if e.teacher_id:
            tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
            tname = tp.user.name if tp and tp.user else None
        out.append({"id": e.id, "teacher": tname, "subject": e.subject, "class_name": e.class_name,
                    "topic": e.chapter, "date": str(e.entry_date) if e.entry_date else None,
                    "day": e.day, "time": e.time_text})
    return out

@router.post("/class/{eid}/approve")
def approve_class(eid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile, StudentProfile, Notification
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Nahi mila")
    e.status = "approved"
    # notify teacher
    if e.teacher_id:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
        if tp and tp.user:
            db.add(Notification(user_id=tp.user.id, title="Extra Class Approved",
                                message=f"Aapki {e.subject} extra class ({e.date if hasattr(e,'date') else e.entry_date}) approve ho gayi.",
                                notif_type="class_approved"))
    # notify students of that subject
    for sp in db.query(StudentProfile).all():
        if sp.subjects and e.subject in sp.subjects and sp.user:
            db.add(Notification(user_id=sp.user.id, title=f"New Class: {e.subject}",
                                message=f"{e.subject} ki extra class add hui hai ({e.entry_date} {e.time_text or ''}). Time table dekho.",
                                notif_type="new_class"))
    db.commit()
    return {"message": "Class approve ho gayi!"}

@router.post("/class/{eid}/reject")
def reject_class(eid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile, Notification
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Nahi mila")
    tid = e.teacher_id; subj = e.subject
    db.delete(e)
    if tid:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
        if tp and tp.user:
            db.add(Notification(user_id=tp.user.id, title="Extra Class Rejected",
                                message=f"Aapki {subj} extra class request reject ho gayi.", notif_type="class_rejected"))
    db.commit()
    return {"message": "Reject ho gaya"}

# ===== ADMIN: SUBJECT-WISE STUDENT COUNTS =====
@router.get("/student-counts")
def admin_student_counts(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile, AvailableSubject
    students = db.query(StudentProfile).all()
    counts = {}
    for sp in students:
        for s in (sp.subjects or []):
            counts[s] = counts.get(s, 0) + 1
    # attach class level from AvailableSubject if available
    subj_class = {a.name: a.class_level for a in db.query(AvailableSubject).all()}
    out = [{"subject": k, "class": subj_class.get(k), "count": v} for k, v in counts.items()]
    out.sort(key=lambda x: -x["count"])
    return {"total_students": len(students), "subjects": out}

# ===== ADMIN: DELETE A TIMETABLE CLASS (admin-only) =====
@router.delete("/timetable-entry/{eid}")
def admin_delete_tt(eid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entry nahi mili")
    db.delete(e); db.commit()
    return {"message": "Class delete ho gayi"}
