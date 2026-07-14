from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_admin, hash_password
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
def _derive_subject_classes(profile, db):
    """Purane teachers jinka subject_classes save nahi hua — flat subjects list se
    subject+class pairs reconstruct karo (AvailableSubject table ki madad se).
    Jaise ["Painting","History","Painting"] -> Painting/10, History/12, Painting/12"""
    sc = profile.subject_classes or []
    if sc:
        return sc
    flat = [x for x in (profile.subjects or []) if x]
    if not flat:
        return []
    from models import AvailableSubject as _AS
    rows = db.query(_AS).all()
    by_name = {}
    for r in rows:
        by_name.setdefault((r.name or "").strip().lower(), []).append(str(r.class_level or ""))
    out, used = [], {}
    for nm in flat:
        key = (nm or "").strip().lower()
        classes = sorted(set(by_name.get(key, [])))          # e.g. ["10","12"]
        i = used.get(key, 0)
        cls = classes[i] if i < len(classes) else (classes[-1] if classes else "")
        used[key] = i + 1
        out.append({"subject": nm, "class": cls})
    return out


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
                "profile_id": profile.id,
                "name": t.name,
                "user_id": t.user_id,
                "phone": profile.phone,
                "has_photo": bool(profile.photo_b64),
                "is_active": t.is_active,
                "subjects": profile.subjects,
                "subject_classes": _derive_subject_classes(profile, db),
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
        phone=(req.phone or None),
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
                "profile_id": sp.id,
                "name": s.name,
                "user_id": s.user_id,
                "phone": sp.phone,
                "email": sp.email,
                "batch": sp.batch_name or (sp.batch.value if hasattr(sp.batch,"value") else sp.batch),
                "batch_name": sp.batch_name,
                "class_level": sp.class_level,
                "has_photo": bool(sp.photo_b64),
                "subjects": sp.subjects,
                "class_name": sp.class_name,
                "is_verified": sp.is_verified,
                "source": getattr(sp, "source", None) or "mvs_app",
                "medium": getattr(sp, "medium", None),
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
        result.get(s.class_level, []).append({
            "id": s.id, "name": s.name, "code": s.code,
            "mode": (s.mode or "live")})
    return result


@router.post("/subjects/{subject_id}/mode")
def set_subject_mode(subject_id: int, payload: dict, db: Session = Depends(get_db),
                     _=Depends(get_admin)):
    """Mark a subject as LIVE (timetable-driven) or RECORDED (watched in the
    Manish Verma Classes App). Recorded subjects have no timetable, so students
    who pick them see a 'Recorded classes' card instead of an empty timetable."""
    from models import AvailableSubject
    mode = (payload.get("mode") or "").strip().lower()
    if mode not in ("live", "recorded"):
        raise HTTPException(status_code=400, detail="mode must be 'live' or 'recorded'")
    s = db.query(AvailableSubject).filter(AvailableSubject.id == subject_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Subject not found")
    s.mode = mode
    db.commit()
    return {"message": "%s is now %s" % (s.name, mode), "mode": mode}

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
def add_subject(class_level: str, name: str, code: str = "", mode: str = "live", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AvailableSubject
    if class_level not in ("10", "12"):
        raise HTTPException(status_code=400, detail="class_level 10 ya 12 hona chahiye")
    s = AvailableSubject(class_level=class_level, name=name, code=code,
                         mode=(mode if mode in ("live", "recorded") else "live"), is_active=True)
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
        tname = ""; tphoto = False; tpid = None
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
        if tp and tp.user:
            tname = tp.user.name; tphoto = bool(tp.photo_b64); tpid = tp.id
        result.append({
            "id": e.id, "subject": e.subject, "class_name": e.class_name,
            "chapter": e.chapter, "part": e.part,
            "date": str(e.entry_date) if e.entry_date else None,
            "day": e.day, "time": getattr(e,"time_text",None),
            "type": getattr(e,"entry_type",None) or "chapter",
            "teacher_name": tname, "teacher_id": tpid, "teacher_has_photo": tphoto
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

# ===== PHOTOS + STUDENT LIST + BULK-BY-PHONE =====
def _img_response(b64):
    import base64
    from fastapi import Response
    if not b64:
        raise HTTPException(status_code=404, detail="Photo nahi")
    return Response(content=base64.b64decode(b64), media_type="image/jpeg")

@router.post("/teacher/{tid}/photo")
async def admin_upload_teacher_photo(tid: int, file: UploadFile = File(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    import base64
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher nahi mila")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo 5MB se badi hai")
    tp.photo_b64 = base64.b64encode(raw).decode("ascii")
    db.commit()
    return {"message": "Photo upload ho gayi!"}

@router.get("/teacher/{tid}/photo")
def admin_teacher_photo(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    return _img_response(tp.photo_b64 if tp else None)

@router.get("/student/{sid}/photo")
def admin_student_photo(sid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    return _img_response(sp.photo_b64 if sp else None)

@router.get("/students-list")
def admin_students_list(q: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    rows = db.query(StudentProfile).all()
    ql = q.strip().lower()
    out = []
    for sp in rows:
        nm = sp.user.name if sp.user else ""
        if ql and ql not in nm.lower() and ql not in (sp.phone or ""):
            continue
        out.append({"id": sp.id, "name": nm, "phone": sp.phone, "class": sp.class_level,
                    "subjects": sp.subjects or [], "has_photo": bool(sp.photo_b64),
                    "user_id": sp.user.user_id if sp.user else None})
    out.sort(key=lambda x: x["name"].lower())
    return {"total": len(out), "students": out}

@router.post("/students/bulk-phone")
def admin_bulk_phone(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Paste phone numbers (no Excel). Each line: 'phone' or 'phone,Name'."""
    text = payload.get("text", "") or ""
    created, skipped = 0, 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        phone = parts[0]
        name = parts[1] if len(parts) > 1 and parts[1] else None
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 10:
            skipped += 1; continue
        phone = digits[-10:]
        if db.query(StudentProfile).filter(StudentProfile.phone == phone).first():
            skipped += 1; continue
        if not name:
            name = "Student " + phone[-4:]
        # MVS-prefixed student user id
        i = 1
        while True:
            cand = f"MVSS{i:04d}"
            if not db.query(User).filter(User.user_id == cand).first():
                break
            i += 1
        u = User(name=name, user_id=cand, password=hash_password(phone),
                 role=UserRole.student, is_active=True)
        db.add(u); db.flush()
        db.add(StudentProfile(user_id=u.id, phone=phone, subjects=[], class_name="",
                              is_verified=True, plain_password=phone))
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped,
            "message": f"{created} students add hue, {skipped} skip (duplicate/galat)."}

def _normalize_batch(text):
    """Bullet-proof: lamba batch naam ko canonical short naam mein badlo."""
    if not text:
        return None
    t = str(text).lower()
    if "science" in t:
        return "Lakshya (Science)"
    if "commerce" in t:
        return "Lakshya (Commerce)"
    if "arts" in t:
        return "Lakshya (Arts)"
    if "udaan" in t or "class 10" in t or "10th" in t:
        return "Udaan Class 10th"
    s = str(text).strip()
    return s[:60] if s else None

# ===== ADMIN: BULK IMPORT FROM APP SALES SHEET (name + phone + batch) =====
@router.post("/students/bulk-import")
def admin_bulk_import(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Frontend Appx sales sheet parse karke {students:[{name,phone,batch}]} bhejega."""
    rows = payload.get("students", []) or []
    created, updated, skipped = 0, 0, 0
    duplicates = []   # MVS Portal se aaye students jinka phone sheet me bhi hai — verify karne ke liye
    for r in rows:
        phone = "".join(ch for ch in str(r.get("phone", "")) if ch.isdigit())
        if len(phone) < 10:
            skipped += 1; continue
        phone = phone[-10:]
        name = (r.get("name") or "").strip() or ("Student " + phone[-4:])
        batch = _normalize_batch(r.get("batch"))
        email = (r.get("email") or "").strip() or None
        existing = db.query(StudentProfile).filter(StudentProfile.phone == phone).first()
        if existing:
            src = getattr(existing, "source", None) or "mvs_app"
            if src != "mvs_portal":
                # PRIORITY: kahin yeh student MVS Portal par to nahi? -> transfer
                try:
                    if _sync_one_from_portal(existing, db):
                        src = "mvs_portal"
                except Exception:
                    pass
            if src == "mvs_portal":
                # DUPLICATE: MVS Portal wala student hi rahega — dubara add NAHI hoga
                duplicates.append({"phone": phone,
                                   "sheet_name": name,
                                   "existing_name": existing.user.name if existing.user else "",
                                   "existing_user_id": existing.user.user_id if existing.user else "",
                                   "existing_batch": existing.batch_name or "",
                                   "source": "mvs_portal"})
                continue
            # MVS APP wala pehle se hai -> refresh (same as before)
            if batch:
                existing.batch_name = batch
            if email:
                existing.email = email
            if existing.user and name and existing.user.name == ("Student " + phone[-4:]):
                existing.user.name = name
            updated += 1
            continue
        # naya student — pehle dekho MVS Portal ka to nahi (priority rule)
        psrc, psubs, pmed, pcls = "mvs_app", [], None, None
        try:
            from ext_materials import portal_fetch_student
            st = portal_fetch_student(phone)
            if st and st.get("unlocked"):
                psrc = "mvs_portal"
                psubs = st.get("subjects") or []
                pmed = st.get("medium")
                pcls = st.get("class_level")
                if st.get("name"):
                    name = st["name"]
        except Exception:
            pass
        i = 1
        while True:
            cand = f"MVSS{i:04d}"
            if not db.query(User).filter(User.user_id == cand).first():
                break
            i += 1
        u = User(name=name, user_id=cand, password=hash_password(phone),
                 role=UserRole.student, is_active=True)
        db.add(u); db.flush()
        db.add(StudentProfile(user_id=u.id, phone=phone, subjects=psubs, class_name="",
                              batch_name=batch, email=email, is_verified=True,
                              plain_password=phone, source=psrc,
                              medium=pmed, class_level=pcls))
        if psrc == "mvs_portal":
            duplicates.append({"phone": phone, "sheet_name": name,
                               "existing_name": name, "existing_user_id": cand,
                               "existing_batch": batch or "", "source": "mvs_portal",
                               "note": "Sheet me tha, par MVS Portal par mila -> MVS Portal me add kiya"})
        else:
            created += 1
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped,
            "duplicates": duplicates,
            "message": f"{created} new students, {updated} updated, {skipped} skipped (invalid phone), {len(duplicates)} duplicate(s) already on MVS Portal."}

# ===== ADMIN: EDIT + DELETE TEACHER / STUDENT =====
from sqlalchemy import text as _sqltext

@router.patch("/teacher/{tid}")
def edit_teacher(tid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher nahi mila")
    if "name" in payload and tp.user:
        tp.user.name = (payload["name"] or "").strip() or tp.user.name
    if "phone" in payload:
        tp.phone = (payload.get("phone") or "").strip() or None
    if "subjects" in payload and isinstance(payload["subjects"], list):
        tp.subjects = [s.strip() for s in payload["subjects"] if s.strip()]
    if "is_active" in payload and tp.user:
        tp.user.is_active = bool(payload["is_active"])
    db.commit()
    return {"message": "Teacher update ho gaya"}

@router.delete("/teacher/{tid}")
def delete_teacher(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher nahi mila")
    uid = tp.user_id
    stmts = [
        ("UPDATE doubts SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("UPDATE timetable_entries SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("UPDATE materials SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM reschedule_requests WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM class_entries WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM dpps WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM tests WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM notifications WHERE user_id=:u", {"u": uid}),
        ("DELETE FROM teacher_profiles WHERE id=:t", {"t": tid}),
        ("DELETE FROM users WHERE id=:u", {"u": uid}),
    ]
    for sql, p in stmts:
        db.execute(_sqltext(sql), p)
    db.commit()
    return {"message": "Teacher delete ho gaya"}

@router.patch("/student/{sid}")
def edit_student(sid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student nahi mili")
    if "name" in payload and sp.user:
        sp.user.name = (payload["name"] or "").strip() or sp.user.name
    if "phone" in payload:
        digits = "".join(ch for ch in str(payload.get("phone", "")) if ch.isdigit())
        if digits:
            sp.phone = digits[-10:]
            sp.plain_password = sp.plain_password or sp.phone
    if "email" in payload:
        sp.email = (payload.get("email") or "").strip() or None
    if "batch_name" in payload:
        sp.batch_name = (payload.get("batch_name") or "").strip() or None
    if "medium" in payload:
        m = (payload.get("medium") or "").strip()
        sp.medium = m if m in ("Hindi", "English") else None
    if "class_level" in payload:
        sp.class_level = (payload.get("class_level") or "").strip() or None
    if "subjects" in payload and isinstance(payload["subjects"], list):
        sp.subjects = [s.strip() for s in payload["subjects"] if s.strip()]
    db.commit()
    return {"message": "Student update ho gaya"}

@router.delete("/student/{sid}")
def delete_student(sid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student nahi mili")
    uid = sp.user_id
    stmts = [
        ("DELETE FROM doubts WHERE student_id=:s", {"s": sid}),
        ("DELETE FROM dpp_submissions WHERE student_id=:s", {"s": sid}),
        ("DELETE FROM test_submissions WHERE student_id=:s", {"s": sid}),
        ("DELETE FROM materials WHERE student_id=:s", {"s": sid}),
        ("DELETE FROM notifications WHERE user_id=:u", {"u": uid}),
        ("DELETE FROM student_profiles WHERE id=:s", {"s": sid}),
        ("DELETE FROM users WHERE id=:u", {"u": uid}),
    ]
    for sql, p in stmts:
        db.execute(_sqltext(sql), p)
    db.commit()
    return {"message": "Student delete ho gayi"}

# ===== ADMIN: SEND NOTIFICATION TO A SINGLE TEACHER =====
@router.post("/teacher/{tid}/notify")
def notify_single_teacher(tid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp or not tp.user:
        raise HTTPException(status_code=404, detail="Teacher not found")
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required")
    notify(db, tp.user.id, "📢 " + title, message, "admin_message")
    db.commit()
    return {"message": f"Notification sent to {tp.user.name}"}

# ===== ADMIN: LIVE STUDENT PRESENCE =====
@router.get("/live-students")
def admin_live_students(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    now = datetime.now()
    cutoff = now - timedelta(minutes=2)
    sps = db.query(StudentProfile).filter(
        StudentProfile.last_seen != None,
        StudentProfile.last_seen >= cutoff
    ).all()
    out = []
    for sp in sps:
        start = sp.session_start or sp.last_seen
        out.append({
            "name": sp.user.name if sp.user else "Student",
            "phone": sp.phone,
            "user_id": sp.user.user_id if sp.user else "",
            "batch": sp.batch_name or "",
            "duration_seconds": max(0, int((now - start).total_seconds())),
            "last_seen_seconds": int((now - sp.last_seen).total_seconds()),
        })
    out.sort(key=lambda x: -x["duration_seconds"])
    return {"count": len(out), "students": out}

# ===== ADMIN: NOTIFICATIONS (bell) =====
@router.get("/notifications")
def admin_notifications(db: Session = Depends(get_db), current_user=Depends(get_admin)):
    notifs = db.query(Notification).filter(
        Notification.user_id == current_user.id
    ).order_by(Notification.created_at.desc()).limit(50).all()
    return [{"id": n.id, "title": n.title, "message": n.message,
             "is_read": n.is_read,
             "created_at": n.created_at.isoformat() if n.created_at else None} for n in notifs]

@router.patch("/notifications/{notif_id}/read")
def admin_notif_read(notif_id: int, db: Session = Depends(get_db), current_user=Depends(get_admin)):
    n = db.query(Notification).filter(
        Notification.id == notif_id, Notification.user_id == current_user.id
    ).first()
    if n:
        n.is_read = True
        db.commit()
    return {"ok": True}

# ===== ADMIN: DOUBTS OVERSIGHT (full thread of every doubt) =====
@router.get("/doubts")
def admin_all_doubts(status: str = None, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Doubt, StudentProfile, TeacherProfile
    q = db.query(Doubt).order_by(Doubt.created_at.desc())
    if status in ("pending", "resolved"):
        q = q.filter(Doubt.status == status)
    out = []
    for d in q.all():
        sp = db.query(StudentProfile).filter(StudentProfile.id == d.student_id).first()
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == d.teacher_id).first() if d.teacher_id else None
        out.append({
            "id": d.id,
            "student_name": (sp.user.name if sp and sp.user else "Unknown student"),
            "student_phone": (sp.phone if sp else None),
            "teacher_name": (tp.user.name if tp and tp.user else "Unassigned"),
            "subject": d.subject,
            "topic": d.topic,
            "question": d.question,
            "has_image": bool(d.image_b64),
            "attach_mime": d.attach_mime, "attach_name": d.attach_name,
            "has_voice": bool(d.audio_b64), "has_answer_voice": bool(d.answer_audio_b64),
            "has_answer_file": bool(d.answer_attach_b64), "answer_attach_mime": d.answer_attach_mime,
            "answer": d.answer,
            "answer_image_link": d.answer_image_link,
            "status": d.status.value if hasattr(d.status, "value") else d.status,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
        })
    return out

@router.get("/doubt/{did}/image")
def admin_doubt_image(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Doubt
    import base64
    from fastapi import Response
    d = db.query(Doubt).filter(Doubt.id == did).first()
    if not d or not d.image_b64:
        return _img_response(None)
    return Response(content=base64.b64decode(d.image_b64),
                    media_type=(d.attach_mime or "image/jpeg"),
                    headers={"Content-Disposition": f'inline; filename="{(d.attach_name or "file").replace(chr(34), "")}"'})

@router.get("/doubt/{did}/voice")
def admin_doubt_voice(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Doubt
    import base64
    from fastapi import Response
    d = db.query(Doubt).filter(Doubt.id == did).first()
    if not d or not d.audio_b64:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=base64.b64decode(d.audio_b64), media_type="audio/webm")

@router.get("/doubt/{did}/answer-file")
def admin_doubt_answer_file(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Doubt
    import base64
    from fastapi import Response
    d = db.query(Doubt).filter(Doubt.id == did).first()
    if not d or not d.answer_attach_b64:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=base64.b64decode(d.answer_attach_b64),
                    media_type=d.answer_attach_mime or "application/octet-stream")

@router.get("/doubt/{did}/answer-voice")
def admin_doubt_answer_voice(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Doubt
    import base64
    from fastapi import Response
    d = db.query(Doubt).filter(Doubt.id == did).first()
    if not d or not d.answer_audio_b64:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=base64.b64decode(d.answer_audio_b64), media_type="audio/webm")

# ===== ADMIN: QUESTION BANK (global materials, Hindi/English, no-compress or link) =====
@router.post("/questionbank")
async def admin_upload_questionbank(
    title: str = Form(...),
    medium: str = Form("English"),
    category: str = Form("Question Bank"),
    subject: str = Form("General"),
    external_link: str = Form(""),
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
    _=Depends(get_admin)
):
    import base64
    from models import Material, StudentProfile
    link = (external_link or "").strip()
    content_b64 = None
    fname = None
    if file is not None and file.filename:
        raw = await file.read()
        # NO compression — stored as-is. Cap to keep MySQL packet safe.
        if len(raw) > 30 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="File is larger than 30MB. Please use the link option for very large files.")
        content_b64 = base64.b64encode(raw).decode("ascii")
        fname = file.filename
    elif not link:
        raise HTTPException(status_code=400, detail="Provide a PDF file or a link.")
    m = Material(
        teacher_id=None, teacher_name="Admin", subject=(subject.strip() or "General"),
        material_type="other", category=(category.strip() or "Question Bank"),
        title=(title.strip() or fname or "Question Bank"), filename=fname,
        content_b64=content_b64, medium=(medium.strip() or "English"),
        is_global=True, external_link=(link or None)
    )
    db.add(m); db.commit(); db.refresh(m)
    # notify ALL students
    try:
        for sp in db.query(StudentProfile).all():
            if sp.user:
                db.add(Notification(user_id=sp.user.id,
                    title=f"📘 New {category.strip() or 'Question Bank'} ({medium.strip()})",
                    message=f"{title.strip()} is now available in the Question Bank.",
                    notif_type="questionbank"))
        db.commit()
    except Exception:
        db.rollback()
    return {"id": m.id, "message": "Question Bank uploaded."}

@router.get("/questionbank")
def admin_list_questionbank(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Material
    ms = db.query(Material).filter(Material.is_global == True).order_by(Material.created_at.desc()).all()
    return [{"id": m.id, "title": m.title, "category": m.category, "medium": m.medium,
             "subject": m.subject, "has_file": bool(m.content_b64), "external_link": m.external_link,
             "filename": m.filename, "date": str(m.created_at)[:10]} for m in ms]

@router.patch("/material/{mid}/approval")
def admin_material_approval(mid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Material not found")
    st = (payload.get("status") or "").strip()
    if st not in ("approved", "pending", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status")
    m.approval_status = st
    db.commit()
    return {"message": f"Material marked {st}."}

@router.delete("/material/{mid}")
def admin_delete_material(mid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Material not found")
    db.delete(m); db.commit()
    return {"message": "Material deleted."}


# ===================================================================== CLASS REPORTS
@router.get("/class-reports")
def admin_class_reports(teacher_id: int = 0, db: Session = Depends(get_db),
                        current_user=Depends(get_admin)):
    """Every teacher's submitted class reports, with delay + teaching-hours
    analytics. Reuses the same computation the teacher portal uses, so both
    sides always agree."""
    from teacher_routes import _report_rows, _report_summary
    from models import TeacherProfile
    tmap = {}
    teachers = []
    for tp in db.query(TeacherProfile).all():
        nm = tp.user.name if tp.user else ("Teacher #%d" % tp.id)
        tmap[tp.id] = nm
        teachers.append({"id": tp.id, "name": nm, "subjects": tp.subjects or []})
    rows = _report_rows(db, None, teacher_map=tmap)
    if teacher_id:
        rows = [r for r in rows if r["teacher_id"] == teacher_id]
    # per-teacher leaderboard of punctuality / hours
    per_teacher = []
    for t in teachers:
        tr = [r for r in rows if r["teacher_id"] == t["id"]] if not teacher_id else rows
        if teacher_id and t["id"] != teacher_id:
            continue
        s = _report_summary(tr)
        per_teacher.append({"teacher_id": t["id"], "name": t["name"],
                            "subjects": t["subjects"], **s})
    per_teacher.sort(key=lambda x: (-(x["on_time_pct"] if x["on_time_pct"] is not None else -1),
                                    -x["month_hours"]))
    return {"summary": _report_summary(rows), "rows": rows[:80],
            "teachers": teachers, "per_teacher": per_teacher}


# ============================================================ MATERIAL ANALYTICS
@router.get("/materials-tree")
def admin_materials_tree(db: Session = Depends(get_db), current_user=Depends(get_admin)):
    """Every subject's uploaded material, chapter/part-wise, with engagement."""
    from teacher_routes import _material_tree
    return {"subjects": _material_tree(db, None)}


@router.get("/material/{mid}/audience")
def admin_material_audience(mid: int, db: Session = Depends(get_db), current_user=Depends(get_admin)):
    from teacher_routes import _material_audience
    return _material_audience(db, mid)


# ==================================================== LIVE USERS (students + teachers)
LIVE_WINDOW_MIN = 3


@router.get("/live-users")
def admin_live_users(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Who is online right now (students AND teachers), which section they are on,
    how many times each person has logged in, plus who has never logged in - so
    the admin can call the inactive ones."""
    from models import UserSession, User, UserRole, StudentProfile, TeacherProfile
    now = datetime.now()
    cutoff = now - timedelta(minutes=LIVE_WINDOW_MIN)
    sessions = db.query(UserSession).all()

    by_user = {}
    for s in sessions:
        d = by_user.setdefault(s.user_id, {"count": 0, "last": None, "live": None})
        d["count"] += 1
        if not d["last"] or (s.last_seen and s.last_seen > d["last"]):
            d["last"] = s.last_seen
        if s.last_seen and s.last_seen >= cutoff:
            d["live"] = s

    users = db.query(User).filter(User.role.in_([UserRole.student, UserRole.teacher])).all()
    phones = {}
    for sp in db.query(StudentProfile).all():
        phones[sp.user_id] = sp.phone
    for tp in db.query(TeacherProfile).all():
        phones.setdefault(tp.user_id, getattr(tp, "phone", None))

    live, offline, never = [], [], []
    for u in users:
        role = getattr(u.role, "value", str(u.role))
        d = by_user.get(u.id)
        base = {"user_id": u.id, "name": u.name, "code": u.user_id, "role": role,
                "phone": phones.get(u.id) or "", "logins": (d["count"] if d else 0)}
        if not d or not d["last"]:
            never.append(base)
            continue
        base["last_seen"] = str(d["last"])[:16]
        base["last_seen_min"] = int((now - d["last"]).total_seconds() // 60)
        if d["live"]:
            s = d["live"]
            base["page"] = s.current_page or "\u2014"
            base["duration_min"] = max(0, int((now - (s.started_at or s.last_seen)).total_seconds() // 60))
            live.append(base)
        else:
            offline.append(base)

    live.sort(key=lambda x: -x["duration_min"])
    offline.sort(key=lambda x: x["last_seen_min"])
    never.sort(key=lambda x: x["name"])
    return {"live": live, "offline": offline, "never": never,
            "counts": {"live": len(live), "students_live": sum(1 for x in live if x["role"] == "student"),
                       "teachers_live": sum(1 for x in live if x["role"] == "teacher"),
                       "never": len(never)}}


@router.get("/user/{user_id}/sessions")
def admin_user_sessions(user_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Every time this person came online - the 'recent' list the admin scrolls."""
    from models import UserSession, User
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    rows = db.query(UserSession).filter(UserSession.user_id == user_id).order_by(
        UserSession.started_at.desc()).limit(60).all()
    out = []
    for s in rows:
        mins = 0
        if s.started_at and s.last_seen:
            mins = max(0, int((s.last_seen - s.started_at).total_seconds() // 60))
        out.append({"started": str(s.started_at)[:16], "last_seen": str(s.last_seen)[:16],
                    "minutes": mins, "page": s.current_page or "\u2014"})
    return {"name": u.name, "code": u.user_id,
            "role": getattr(u.role, "value", str(u.role)),
            "logins": len(out), "sessions": out}


# ==================================================== CLASS COMPLIANCE (missed / delayed)
DELAY_WARN_THRESHOLD = 2      # more than this many late classes in the month -> warn


@router.get("/class-compliance")
def admin_class_compliance(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Per-teacher punctuality: classes that were MISSED (scheduled date passed and
    never marked done) and classes that started LATE, with a month-wise count so
    repeat offenders are obvious."""
    from teacher_routes import _delay_of, _delay_band, _duration_of
    from models import TimetableEntry, TeacherProfile
    today = date.today()
    month_start = date(today.year, today.month, 1)

    tmap = {}
    for tp in db.query(TeacherProfile).all():
        tmap[tp.id] = {"id": tp.id, "name": (tp.user.name if tp.user else "Teacher #%d" % tp.id),
                       "user_id": tp.user_id, "subjects": tp.subjects or []}

    entries = db.query(TimetableEntry).filter(
        TimetableEntry.entry_type == "chapter",
        (TimetableEntry.status == None) | (TimetableEntry.status != "pending")).all()

    missed, late_rows = [], []
    per = {}
    for e in entries:
        t = tmap.get(e.teacher_id)
        tname = t["name"] if t else ""
        p = per.setdefault(e.teacher_id, {"missed": 0, "late": 0, "late_month": 0,
                                          "ontime": 0, "done": 0, "delays": []})
        # MISSED: the class date has passed but it was never marked done
        if e.entry_date and e.entry_date < today and not getattr(e, "completed", False):
            p["missed"] += 1
            missed.append({"id": e.id, "teacher_id": e.teacher_id, "teacher_name": tname,
                           "subject": e.subject, "chapter": e.chapter, "part": e.part,
                           "date": str(e.entry_date), "slot": e.time_text,
                           "days_ago": (today - e.entry_date).days})
            continue
        if not getattr(e, "completed", False):
            continue
        p["done"] += 1
        d = _delay_of(e)
        band = _delay_band(d)
        if band == "ontime":
            p["ontime"] += 1
        elif band in ("minor", "late"):
            p["late"] += 1
            p["delays"].append(d)
            if e.entry_date and e.entry_date >= month_start:
                p["late_month"] += 1
            late_rows.append({"id": e.id, "teacher_id": e.teacher_id, "teacher_name": tname,
                              "subject": e.subject, "chapter": e.chapter, "part": e.part,
                              "date": str(e.entry_date) if e.entry_date else "",
                              "slot": e.time_text, "started": e.start_time,
                              "delay_min": d, "band": band})

    teachers = []
    for tid, p in per.items():
        t = tmap.get(tid)
        if not t:
            continue
        avg = round(sum(p["delays"]) / len(p["delays"])) if p["delays"] else None
        total = p["done"]
        teachers.append({
            "teacher_id": tid, "name": t["name"], "subjects": t["subjects"],
            "classes_done": total, "missed": p["missed"],
            "late": p["late"], "late_this_month": p["late_month"], "ontime": p["ontime"],
            "avg_delay": avg,
            "on_time_pct": (round(p["ontime"] * 100 / total) if total else None),
            "at_risk": p["late_month"] > DELAY_WARN_THRESHOLD or p["missed"] > 0,
        })
    teachers.sort(key=lambda x: (-(x["missed"]), -(x["late_this_month"])))
    missed.sort(key=lambda x: x["date"], reverse=True)
    late_rows.sort(key=lambda x: x["date"], reverse=True)
    return {"teachers": teachers, "missed": missed[:60], "late": late_rows[:60],
            "totals": {"missed": len(missed), "late": len(late_rows),
                       "at_risk": sum(1 for t in teachers if t["at_risk"])},
            "threshold": DELAY_WARN_THRESHOLD}


@router.post("/warn-teacher/{teacher_id}")
def admin_warn_teacher(teacher_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Send the punctuality reminder to a teacher who keeps starting late."""
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == teacher_id).first()
    if not tp or not tp.user:
        raise HTTPException(status_code=404, detail="Teacher not found")
    msg = ("Aapki classes baar-baar late shuru ho rahi hain.\n\n"
           "Isse MVS Foundation ki reputation par asar padta hai aur bachche panic hote hain. "
           "Ye aapki monthly report par bhi impact karega.\n\n"
           "Please classes time par shuru karein.")
    db.add(Notification(user_id=tp.user.id, title="\u26a0\ufe0f Class Punctuality Reminder",
                        message=msg, notif_type="warning"))
    db.commit()
    return {"message": "Reminder sent to %s" % tp.user.name}


# ==================================================== DOUBTS OVERVIEW (subject cards)
@router.get("/doubts-overview")
def admin_doubts_overview(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Subject cards for the doubts page: who teaches it, how many doubts came in,
    how many are resolved, how many are still pending - and how long the oldest
    pending one has been waiting."""
    from models import Doubt, DoubtStatus, TeacherProfile
    now = datetime.now()
    ds = db.query(Doubt).all()
    tmap = {}
    for tp in db.query(TeacherProfile).all():
        for s in (tp.subjects or []):
            tmap.setdefault(s, tp.user.name if tp.user else "")
    by = {}
    for d in ds:
        sub = d.subject or "General"
        c = by.setdefault(sub, {"subject": sub, "teacher": tmap.get(sub, ""),
                                "total": 0, "resolved": 0, "pending": 0,
                                "oldest_pending_min": None})
        c["total"] += 1
        resolved = (getattr(d.status, "value", str(d.status)) == "resolved")
        if resolved:
            c["resolved"] += 1
        else:
            c["pending"] += 1
            if d.created_at:
                mins = int((now - d.created_at).total_seconds() // 60)
                if c["oldest_pending_min"] is None or mins > c["oldest_pending_min"]:
                    c["oldest_pending_min"] = mins
    out = sorted(by.values(), key=lambda x: (-x["pending"], x["subject"]))
    return {"subjects": out,
            "totals": {"total": len(ds),
                       "pending": sum(c["pending"] for c in out),
                       "resolved": sum(c["resolved"] for c in out)}}

# ------------------------------------------------------------------
#  ADMIN PASSWORD RESET (teacher/student) — purana password hashed
#  hota hai isliye dekha nahi ja sakta; admin naya set/generate karta hai.
# ------------------------------------------------------------------
import secrets as _secrets, string as _string

@router.post("/reset-password")
def admin_reset_password(payload: dict, db: Session = Depends(get_db), current_user=Depends(get_admin)):
    role = (payload.get("role") or "").strip()          # 'teacher' | 'student'
    profile_id = payload.get("profile_id")
    new_pass = (payload.get("password") or "").strip()

    if role == "teacher":
        prof = db.query(TeacherProfile).filter(TeacherProfile.id == profile_id).first()
    elif role == "student":
        prof = db.query(StudentProfile).filter(StudentProfile.id == profile_id).first()
    else:
        raise HTTPException(status_code=400, detail="Role must be teacher or student")
    if not prof:
        raise HTTPException(status_code=404, detail="Profile not found")

    user = db.query(User).filter(User.id == prof.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    if not new_pass:  # auto-generate friendly password
        new_pass = "MVS@" + "".join(_secrets.choice(_string.digits) for _ in range(4))
    if len(new_pass) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user.password = hash_password(new_pass)
    if role == "student":
        prof.plain_password = new_pass  # keep phone-lookup onboarding in sync
    db.commit()
    return {"message": "Password reset successfully", "name": user.name,
            "user_id": user.user_id, "password": new_pass}

# ------------------------------------------------------------------
#  MVS PORTAL <-> CRM STUDENT OVERVIEW
#  Kitne students portal se aaye, kitne app (sheet) se, aur portal par
#  kitne unlocked students ne abhi tak batch select hi nahi kiya.
# ------------------------------------------------------------------
@router.get("/portal-overview")
def portal_overview(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile as _SP
    profs = db.query(_SP).all()
    total = len(profs)
    portal = sum(1 for x in profs if (getattr(x, "source", None) or "mvs_app") == "mvs_portal")
    app = total - portal
    existing_phones = {x.phone for x in profs if x.phone}
    pending, portal_reachable = [], False
    try:
        from ext_materials import portal_unlocked_students
        lst = portal_unlocked_students()
        if lst is not None:
            portal_reachable = True
            for st in lst:
                ph = "".join(ch for ch in str(st.get("phone", "")) if ch.isdigit())[-10:]
                if len(ph) == 10 and ph not in existing_phones:
                    pending.append({"name": st.get("name") or "", "phone": ph,
                                    "class_level": str(st.get("class_level") or st.get("class") or ""),
                                    "session": st.get("session") or ""})
    except Exception:
        pass
    return {"total": total, "mvs_portal": portal, "mvs_app": app,
            "portal_reachable": portal_reachable,
            "pending_count": len(pending), "pending": pending[:300]}


# ------------------------------------------------------------------
#  DANGER: DELETE ALL STUDENTS (fresh re-upload ke liye)
# ------------------------------------------------------------------
@router.delete("/students/all")
def delete_all_students(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    if (payload or {}).get("confirm") != "DELETE ALL STUDENTS":
        raise HTTPException(status_code=400, detail='Type "DELETE ALL STUDENTS" to confirm.')
    import models as M
    from models import StudentProfile as _SP, User as _U, UserRole as _UR

    stu_users = db.query(_U).filter(_U.role == _UR.student).all()
    stu_uids = [u.id for u in stu_users]
    total = db.query(_SP).count()
    if not stu_uids and not total:
        return {"message": "No students to delete.", "deleted": 0}

    errors = []

    def wipe(fn, label):
        try:
            with db.begin_nested():   # savepoint: ek fail hua to sirf wahi rollback hota hai
                fn()
        except Exception as e:
            errors.append(f"{label}: {type(e).__name__}")

    # Model-driven cleanup: har mapped table jisme student_id/user_id column hai
    for mp in list(M.Base.registry.mappers):
        cls = mp.class_
        name = cls.__name__
        if name in ("StudentProfile", "User"):
            continue
        cols = {c.key for c in mp.columns}
        if "student_id" in cols:
            if name == "Material":
                # teacher-uploaded materials (student_id NULL) safe rehte hain
                wipe(lambda c=cls: db.query(c).filter(c.student_id.isnot(None))
                     .delete(synchronize_session=False), name)
            else:
                wipe(lambda c=cls: db.query(c).delete(synchronize_session=False), name)
        elif "user_id" in cols and stu_uids:
            for i in range(0, len(stu_uids), 500):
                chunk = stu_uids[i:i + 500]
                wipe(lambda c=cls, ch=chunk: db.query(c).filter(c.user_id.in_(ch))
                     .delete(synchronize_session=False), name)

    wipe(lambda: db.query(_SP).delete(synchronize_session=False), "StudentProfile")
    wipe(lambda: db.query(_U).filter(_U.role == _UR.student)
         .delete(synchronize_session=False), "User")
    db.commit()

    remaining = db.query(_SP).count()
    if remaining:
        raise HTTPException(status_code=500,
                            detail=f"Deletion incomplete — {remaining} students still remain. Issues: {', '.join(sorted(set(errors))[:6]) or 'unknown'}")
    return {"message": f"All {total} students deleted. You can now upload fresh data.",
            "deleted": total}

# ==================================================================
#  MVS PORTAL PRIORITY SYNC
#  Rule: agar koi student MVS Portal par exist karta hai to woh HAMESHA
#  "mvs_portal" category ka hai — chahe pehle sheet (MVS App) se add hua ho.
#  Yeh sync MVS App students ko portal par check karke unhe transfer kar
#  deta hai aur unka data (class, medium, subjects) portal se refresh karta hai.
# ==================================================================
def _sync_one_from_portal(sp, db):
    """Ek student ko portal par check karo. True agar mvs_portal me transfer hua."""
    from ext_materials import portal_fetch_student
    if not sp.phone:
        return False
    st = portal_fetch_student(sp.phone)
    if not st or not st.get("unlocked"):
        return False
    sp.source = "mvs_portal"                       # priority: portal jeet-ta hai
    if st.get("subjects"):
        sp.subjects = st["subjects"]
    if st.get("medium"):
        sp.medium = st["medium"]
    if st.get("class_level"):
        sp.class_level = st["class_level"]
    if st.get("name") and sp.user and (not sp.user.name or sp.user.name.startswith("Student ")):
        sp.user.name = st["name"]
    return True


@router.post("/students/sync-portal")
def sync_students_with_portal(payload: dict = None, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Sabhi MVS App students ko MVS Portal par check karke transfer karo."""
    from models import StudentProfile as _SP
    from ext_materials import _cfg
    url, key = _cfg()
    if not url or not key:
        raise HTTPException(status_code=503, detail="MVS Portal connection is not configured")
    limit = int((payload or {}).get("limit") or 400)
    app_students = db.query(_SP).filter(
        (_SP.source == "mvs_app") | (_SP.source.is_(None))).limit(limit).all()
    moved = []
    for sp in app_students:
        try:
            if _sync_one_from_portal(sp, db):
                moved.append({"name": sp.user.name if sp.user else "", "phone": sp.phone,
                              "user_id": sp.user.user_id if sp.user else ""})
        except Exception:
            continue
    db.commit()
    return {"checked": len(app_students), "moved": len(moved), "students": moved[:200],
            "message": f"{len(moved)} student(s) moved from MVS App to MVS Portal."}


# ==================================================================
#  WHATSAPP — WELCOME MESSAGE (sirf MVS App students ko)
# ==================================================================
@router.get("/whatsapp/status")
def whatsapp_status(db: Session = Depends(get_db), _=Depends(get_admin)):
    import whatsapp as W
    from models import StudentProfile as _SP
    pend = db.query(_SP).filter(
        ((_SP.source == "mvs_app") | (_SP.source.is_(None))),
        _SP.welcome_sent_at.is_(None), _SP.phone.isnot(None)).count()
    sent = db.query(_SP).filter(_SP.welcome_sent_at.isnot(None)).count()
    c = W.cfg()
    return {"configured": W.is_configured(), "provider": c["provider"],
            "missing": W.missing(), "campaign": c["campaign"], "params": c["params"],
            "pending": pend, "sent": sent, "link": c["link"], "template": c["template"],
            "sample": W.build_message("Rahul Sharma", "Lakshya Science", "9876543210"),
            "sample_params": W.build_params("Rahul Sharma", "Lakshya Science", "9876543210")}


@router.get("/whatsapp/pending")
def whatsapp_pending(db: Session = Depends(get_db), _=Depends(get_admin)):
    """MVS App students jinhe abhi welcome message nahi gaya."""
    from models import StudentProfile as _SP
    rows = db.query(_SP).filter(
        ((_SP.source == "mvs_app") | (_SP.source.is_(None))),
        _SP.welcome_sent_at.is_(None), _SP.phone.isnot(None)).all()
    return [{"profile_id": x.id, "name": x.user.name if x.user else "Student",
             "phone": x.phone, "batch": x.batch_name or ""} for x in rows]


@router.post("/whatsapp/send-welcome")
def whatsapp_send_welcome(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Welcome message bhejo. payload:
       {"profile_ids": [1,2,3]}  ya  {"all_pending": true}
       {"template": "...", "resend": false}"""
    import whatsapp as W
    from datetime import datetime as _dt
    from models import StudentProfile as _SP
    if not W.is_configured():
        raise HTTPException(status_code=503,
                            detail="WhatsApp is not configured. Missing on Railway: " + ", ".join(W.missing()))
    payload = payload or {}
    template = (payload.get("template") or "").strip() or None
    resend = bool(payload.get("resend"))

    q = db.query(_SP).filter(_SP.phone.isnot(None))
    if payload.get("all_pending"):
        q = q.filter(((_SP.source == "mvs_app") | (_SP.source.is_(None))))
        if not resend:
            q = q.filter(_SP.welcome_sent_at.is_(None))
    else:
        ids = payload.get("profile_ids") or []
        if not ids:
            raise HTTPException(status_code=400, detail="No students selected")
        q = q.filter(_SP.id.in_(ids))
    students = q.limit(int(payload.get("limit") or 200)).all()

    sent, failed = 0, []
    for sp in students:
        # MVS Portal students ko welcome nahi bhejte (unka apna flow hai)
        if (getattr(sp, "source", None) or "mvs_app") == "mvs_portal":
            continue
        name = sp.user.name if sp.user else "Student"
        msg = W.build_message(name, sp.batch_name or "", sp.phone, template)
        ok, detail = W.send(sp.phone, text=msg, name=name, batch=sp.batch_name or "")
        if ok:
            sp.welcome_sent_at = _dt.now()
            sent += 1
        else:
            failed.append({"name": name, "phone": sp.phone, "error": detail[:120]})
    db.commit()
    return {"sent": sent, "failed": len(failed), "errors": failed[:25],
            "message": f"{sent} message(s) sent, {len(failed)} failed."}

@router.post("/whatsapp/test")
def whatsapp_test(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Apne number par ek test message bhejo."""
    import whatsapp as W
    phone = "".join(ch for ch in str((payload or {}).get("phone") or "") if ch.isdigit())[-10:]
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit phone")
    if not W.is_configured():
        raise HTTPException(status_code=503,
                            detail="WhatsApp is not configured. Missing on Railway: " + ", ".join(W.missing()))
    name = (payload or {}).get("name") or "Test Student"
    batch = (payload or {}).get("batch") or "Lakshya Science"
    ok, detail = W.send(phone, text=W.build_message(name, batch, phone), name=name, batch=batch)
    return {"ok": ok, "detail": detail[:300],
            "params_sent": W.build_params(name, batch, phone)}
