from fastapi import APIRouter, Depends, HTTPException
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
    """Admin adds a teacher with custom user_id and password"""
    existing = db.query(User).filter(User.user_id == req.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Yeh User ID already hai")

    user = User(
        name=req.name,
        user_id=req.user_id,
        password=hash_password(req.password),
        role=UserRole.teacher,
        is_active=True
    )
    db.add(user)
    db.flush()

    profile = TeacherProfile(
        user_id=user.id,
        subjects=req.subjects or [],
        batch=req.batch or "",
    )
    db.add(profile)
    db.commit()
    return {"message": f"Teacher {req.name} add ho gaya! User ID: {req.user_id}"}

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
