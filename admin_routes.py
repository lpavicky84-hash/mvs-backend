import re
import secrets
import string
try:
    import subjects_registry as _SR
except Exception:
    _SR = None
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Body, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, date, timedelta
from typing import List, Optional

from database import get_db
from security import get_admin, hash_password
from models import (
    User, TeacherProfile, StudentProfile, ClassEntry, ClassStatus,
    RescheduleRequest, RescheduleStatus, Doubt, DoubtStatus,
    DPP, Test, TestSubmission, DPPSubmission, Notification, UserRole,
    Exam, ExamQuestion, ExamAttempt
)
from schemas import (
    RescheduleReview, RescheduleOut, UserOut, AdminDashboard,
    RegisterRequest
)
from security import hash_password

# ==================================================== v94: RESTRICTED SUB-ADMIN ACCESS
# Super admin (allowed_sections = NULL) ko sab kuch milta hai.
# Restricted admin ke liye sirf listed sections ke endpoints chalte hain.
# Map: pehla path segment (/api/admin/<segment>/...) -> sidebar section key.
ADMIN_SECTION_MAP = {
    "dashboard": "dashboard", "portal-overview": "dashboard", "activity": "dashboard", "my-ip": "dashboard",
    "pending-classes": "approvals", "tt-reschedules": "approvals", "reschedules": "approvals",
    "app-reviews": "approvals", "letter-remarks": "approvals", "class": "approvals",
    "live-students": "live", "live-users": "live", "user": "live",
    "timetable-entry": "timetable", "timetable-all": "timetable", "timetable-clear": "timetable",
    "timetable-pdf": "timetable", "timetable-pdf-commit": "timetable", "timetable-subject": "timetable",
    "studio-reports": "timetable",
    "class-report-backfill": "timetable",
    "questionbank": "qbank",
    "material": "material", "materials-tree": "material", "pending-materials": "material",
    "doubt": "doubts", "doubts": "doubts", "doubts-overview": "doubts",
    "exam": "tests", "exams": "tests", "dpp-packs": "tests",
    "teacher": "teachers", "teachers": "teachers", "warn-teacher": "teachers", "credentials": "teachers",
    "class-reports": "reports",
    "dpp-rankings": "tranks",
    "class-compliance": "compliance",
    "attendance": "attendance", "leaves": "attendance", "office-location": "attendance", "session-deadlines": "attendance",
    "ai-format-config": "subjects",
    "student": "students", "students": "students", "students-list": "students",
    "student-counts": "students", "reset-password": "students",
    "subjects": "subjects",
    "earnings": "payouts", "payout-task": "payouts", "payouts": "payouts", "payout-approvals": "payouts",
    "payout-adjust": "payouts", "passcode-resets": "payouts",
    "contracts-overview": "payouts", "contracts-bulk": "payouts",
    "admins": "admins", "reset-data": "admins", "whatsapp": "admins", "orphan-data": "admins",
    "notify": "notify", "notify-targets": "notify", "broadcast": "notify",
    "video-channels": "vtasks", "video-types": "vtasks", "video-tasks": "vtasks",
}

# Kuch sections ke endpoints ek se zyada section-permission se khulte hain.
# Urgent Videos page bhi wahi /api/admin/video-tasks endpoints use karta hai jo
# Task Manager karta hai — isliye 'urgent' section wale admin ko bhi allow karo.
ADMIN_SECTION_ALIASES = {"vtasks": {"vtasks", "urgent"}}


def admin_allowed_sections(user):
    """None = full access; otherwise set of allowed section keys."""
    secs = getattr(user, "allowed_sections", None)
    if secs is None:
        return None
    return set(secs or [])


def admin_section_guard(request: Request, current_user=Depends(get_admin)):
    """Router-level guard: restricted sub-admin sirf apne allowed sections ke
    endpoints call kar sakta hai. Unmapped endpoints (e.g. notifications bell)
    sabke liye open rehte hain — safe default."""
    allowed = admin_allowed_sections(current_user)
    if allowed is None:
        return current_user
    path = request.url.path  # e.g. /api/admin/earnings/configs
    if not path.startswith("/api/admin/"):
        return current_user
    parts = path.split("/")
    first = parts[3] if len(parts) > 3 else ""
    sec = ADMIN_SECTION_MAP.get(first)
    if sec is not None:
        acceptable = ADMIN_SECTION_ALIASES.get(sec, {sec})
        if allowed.isdisjoint(acceptable):
            raise HTTPException(status_code=403, detail="You do not have access to this section.")
    return current_user


router = APIRouter(prefix="/api/admin", tags=["Admin"], dependencies=[Depends(admin_section_guard)])

def notify(db, user_id: int, title: str, message: str, notif_type: str):
    n = Notification(user_id=user_id, title=title, message=message, notif_type=notif_type)
    db.add(n)

# ===== DASHBOARD =====
@router.get("/dashboard", response_model=AdminDashboard)
def admin_dashboard(db: Session = Depends(get_db), _=Depends(get_admin)):
    total_teachers  = db.query(User).filter(User.role == UserRole.teacher).count()
    total_students  = db.query(User).filter(User.role == UserRole.student).count()
    # Classes done = teachers dwara submit ki gayi LECTURES (ClassEntry purana system tha,
    # ab teachers timetable se lecture report daalte hain). Pending = timetable ke chapter
    # entries jinka din aa chuka par abhi tak lecture nahi aaya.
    from models import Lecture, TimetableEntry
    total_done = db.query(Lecture).count()
    _today = date.today()
    _done_tt = set(x[0] for x in db.query(Lecture.timetable_entry_id).filter(
        Lecture.timetable_entry_id.isnot(None)).all())
    total_pending = 0
    for _e in db.query(TimetableEntry).filter(TimetableEntry.entry_type == "chapter").all():
        if _e.entry_date and _e.entry_date <= _today and _e.id not in _done_tt:
            total_pending += 1
    pending_rs      = db.query(RescheduleRequest).filter(RescheduleRequest.status == RescheduleStatus.pending).count()
    # unresolved = jo resolved nahi, PLUS resolved par jinpe naya follow-up aaya (attention chahiye)
    # — bilkul subject-wise doubts section jaise, taaki dashboard aur section match karein.
    unresolved = 0
    for d in db.query(Doubt).all():
        is_resolved = (getattr(d.status, "value", str(d.status)) == "resolved")
        if not is_resolved:
            unresolved += 1
        else:
            try:
                if _doubt_needs_attention(db, d.id):
                    unresolved += 1
            except Exception:
                pass

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
        raise HTTPException(status_code=404, detail="Request not found")
    if rs.status != RescheduleStatus.pending:
        raise HTTPException(status_code=400, detail="This request has already been processed")

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
    return {"message": f"Reschedule {req.status}. The teacher has been notified."}

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
    # AHEM: guess mat karo. Agar subject ka naam sirf EK class me hai to hi class
    # assign hoti hai. Economics/Hindi/Painting jaise naam dono classes me hote
    # hain — unke liye class blank rehti hai (admin Edit se sahi set karega).
    out, used = [], {}
    for nm in flat:
        key = (nm or "").strip().lower()
        classes = sorted(set(by_name.get(key, [])))
        if len(classes) == 1:
            cls = classes[0]
        elif len(classes) > 1 and flat.count(nm) >= len(classes):
            # teacher ke paas subject utni hi baar hai jitni classes -> dono padhata hai
            i = used.get(key, 0)
            cls = classes[i] if i < len(classes) else ""
            used[key] = i + 1
        else:
            cls = ""      # ambiguous -> koi guess nahi
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
        plain_password=req.password,
    )
    db.add(profile)
    db.commit()
    return {"message": f"Teacher {req.name} added successfully!", "user_id": candidate}

@router.patch("/teachers/{user_id}/toggle")
def toggle_teacher(user_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    user = db.query(User).filter(User.id == user_id, User.role == UserRole.teacher).first()
    if not user:
        raise HTTPException(status_code=404, detail="Teacher not found")
    user.is_active = not user.is_active
    db.commit()
    status = "active" if user.is_active else "inactive"
    return {"message": f"Teacher {user.name} is now {status}"}

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
                "subjects": (_SR.canon_list(sp.subjects, sp.class_level) if _SR else sp.subjects),
                "class_name": sp.class_name,
                "is_verified": sp.is_verified,
                "source": getattr(sp, "source", None) or "mvs_app",
                "medium": getattr(sp, "medium", None),
                "exam_session": getattr(sp, "exam_session", None),
                "exam_stream": getattr(sp, "exam_stream", None),
                "nios_ref": getattr(sp, "nios_ref", None),
                "is_active": s.is_active,
                "dpp_submitted": dpp_submitted,
                "tests_attempted": test_attempted,
            })
    return result

@router.post("/students/add")
def add_student(req: RegisterRequest, db: Session = Depends(get_db), _=Depends(get_admin)):
    existing = db.query(User).filter(User.user_id == req.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="This User ID already exists")
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
    return {"message": f"Student {req.name} added successfully!"}

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
            from models import Lecture as _Lec
            done = db.query(_Lec).filter(
                _Lec.teacher_id == tp.id, _Lec.subject == subject).count()
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
def _admin_json(u: User):
    secs = getattr(u, "allowed_sections", None)
    return {
        "id": u.id, "name": u.name, "user_id": u.user_id,
        "is_active": bool(u.is_active),
        "full_access": secs is None,
        "sections": (sorted(secs) if isinstance(secs, (list, tuple)) else None),
        "created_at": u.created_at.isoformat() if getattr(u, "created_at", None) else None,
    }


class AdminCreateIn(BaseModel):
    name: str
    user_id: Optional[str] = None     # blank -> auto-generate
    password: Optional[str] = None    # blank -> auto-generate
    sections: Optional[List[str]] = None  # null/omitted -> full access


class AdminSectionsIn(BaseModel):
    sections: Optional[List[str]] = None  # null -> full access


class AdminEditIn(BaseModel):
    """v94.1: naam + access dono ek saath edit. sections null + full_access true = full access."""
    name: Optional[str] = None
    sections: Optional[List[str]] = None
    full_access: Optional[bool] = None


def _clean_sections(raw):
    secs = sorted({s for s in (raw or []) if isinstance(s, str) and s.strip()})
    if "dashboard" not in secs:
        secs.insert(0, "dashboard")
    return secs


@router.get("/me")
def admin_me(db: Session = Depends(get_db), me=Depends(get_admin)):
    """Logged-in admin ka access profile — frontend nav ko isi se filter karta hai."""
    secs = getattr(me, "allowed_sections", None)
    return {
        "id": getattr(me, "id", None),
        "name": getattr(me, "name", "Admin") or "Admin",
        "user_id": getattr(me, "user_id", "") or "",
        "full_access": secs is None,
        "sections": (sorted(secs) if isinstance(secs, (list, tuple)) else ([] if secs is not None else None)),
    }


@router.get("/admins")
def list_admins(db: Session = Depends(get_db), _=Depends(get_admin)):
    rows = db.query(User).filter(User.role == UserRole.admin).order_by(User.created_at.asc()).all()
    return {"admins": [_admin_json(u) for u in rows]}


@router.post("/admins/add")
def add_admin(req: AdminCreateIn, db: Session = Depends(get_db), _=Depends(get_admin)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    uid = (req.user_id or "").strip()
    if uid:
        if db.query(User).filter(User.user_id == uid).first():
            raise HTTPException(status_code=400, detail="This User ID already exists")
    else:
        uid = None
        for _ in range(25):
            cand = "adm_" + "".join(secrets.choice(string.digits) for _ in range(4))
            if not db.query(User).filter(User.user_id == cand).first():
                uid = cand
                break
        if not uid:
            raise HTTPException(status_code=500, detail="Could not generate a unique User ID. Please try again.")
    pwd = (req.password or "").strip() or "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    secs = _clean_sections(req.sections) if req.sections is not None else None
    user = User(
        name=name, user_id=uid,
        password=hash_password(pwd),
        role=UserRole.admin, is_active=True,
        allowed_sections=secs,
    )
    db.add(user)
    db.commit()
    out = _admin_json(user)
    out["message"] = f"Admin {name} added successfully!"
    out["password"] = pwd  # shown once to the creator so they can share it
    return out


@router.post("/admins/{admin_id}/sections")
def set_admin_sections(admin_id: int, req: AdminSectionsIn,
                       db: Session = Depends(get_db), me=Depends(get_admin)):
    if me is not None and getattr(me, "id", None) == admin_id:
        raise HTTPException(status_code=400, detail="You cannot change your own access.")
    u = db.query(User).filter(User.id == admin_id, User.role == UserRole.admin).first()
    if not u:
        raise HTTPException(status_code=404, detail="Admin not found")
    u.allowed_sections = _clean_sections(req.sections) if req.sections is not None else None
    db.commit()
    out = _admin_json(u)
    out["message"] = "Access updated."
    return out


@router.post("/admins/{admin_id}/edit")
def edit_admin(admin_id: int, req: AdminEditIn,
               db: Session = Depends(get_db), me=Depends(get_admin)):
    """Naam rename + access update — ek hi call me. Naam apna bhi badal sakte ho;
    access (sections) sirf doosre admins ka badal sakte ho."""
    u = db.query(User).filter(User.id == admin_id, User.role == UserRole.admin).first()
    if not u:
        raise HTTPException(status_code=404, detail="Admin not found")
    if req.name is not None:
        nm = req.name.strip()
        if not nm:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        u.name = nm[:120]
    is_self = me is not None and getattr(me, "id", None) == admin_id
    if req.full_access or req.sections is not None:
        if is_self:
            raise HTTPException(status_code=400, detail="You cannot change your own access.")
        u.allowed_sections = None if req.full_access else _clean_sections(req.sections)
    db.commit()
    out = _admin_json(u)
    out["message"] = "Admin updated."
    return out


@router.post("/admins/{admin_id}/toggle")
def toggle_admin(admin_id: int, db: Session = Depends(get_db), me=Depends(get_admin)):
    if me is not None and getattr(me, "id", None) == admin_id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    u = db.query(User).filter(User.id == admin_id, User.role == UserRole.admin).first()
    if not u:
        raise HTTPException(status_code=404, detail="Admin not found")
    u.is_active = not u.is_active
    db.commit()
    out = _admin_json(u)
    out["message"] = f"{u.name} is now {'active' if u.is_active else 'deactivated'}."
    return out

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
    return {"message": f"Notification sent to {len(users)} users."}

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
        raise HTTPException(status_code=404, detail="Subject not found")
    s.is_active = False   # soft delete
    db.commit()
    return {"message": f"{s.name} deleted"}

@router.post("/subjects")
def add_subject(class_level: str, name: str, code: str = "", mode: str = "live", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AvailableSubject
    if class_level not in ("10", "12"):
        raise HTTPException(status_code=400, detail="class_level must be 10 or 12")
    s = AvailableSubject(class_level=class_level, name=name, code=code,
                         mode=(mode if mode in ("live", "recorded") else "live"), is_active=True)
    db.add(s)
    db.commit()
    return {"message": f"{name} added"}

# ===== TIMETABLE (all teachers) =====
@router.get("/timetable-all")
def timetable_all(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile, Lecture
    es = db.query(TimetableEntry).order_by(
        TimetableEntry.subject, TimetableEntry.chapter, TimetableEntry.entry_date
    ).all()
    # v99: kin entries ka class report (lecture) upload ho chuka hai — ek hi query me
    lec_entry_ids = set(x[0] for x in db.query(Lecture.timetable_entry_id).filter(
        Lecture.is_active == True, Lecture.timetable_entry_id.isnot(None)).all())
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
            "teacher_name": tname, "teacher_id": tpid, "teacher_has_photo": tphoto,
            "completed": bool(e.completed), "has_lecture": (e.id in lec_entry_ids),
            "topic_covered": e.topic_covered or "", "start_time": e.start_time or "",
            "end_time": e.end_time or "", "homework": e.homework or "",
            "remarks": e.remarks or "",
        })
    return result

# ===== v97: ADMIN STUDIO REPORTS (per timetable class) =====
def _studio_report_json(r):
    return {
        "id": r.id, "entry_id": r.entry_id,
        "entry_date": r.entry_date or "", "day": r.day or "", "time": r.time_str or "",
        "subject": r.subject or "", "class_name": r.class_name or "",
        "chapter": r.chapter or "", "part": r.part or "",
        "status": r.status or "held", "notes": r.notes or "",
        "reporter": r.reporter or "",
        "start_time": getattr(r, "start_time", "") or "",
        "end_time": getattr(r, "end_time", "") or "",
        "has_notes_file": bool(getattr(r, "notes_file_b64", None)),
        "notes_file_name": getattr(r, "notes_file_name", "") or "",
        "updated_at": r.updated_at.strftime("%d %b %Y, %I:%M %p") if r.updated_at else "",
    }

@router.get("/studio-reports")
def studio_reports_list(entry_ids: str = "", db: Session = Depends(get_db),
                        _=Depends(get_admin)):
    """Timetable page ke liye — saari (ya di gayi entry_ids ki) studio reports."""
    from models import StudioReport
    q = db.query(StudioReport)
    ids = [int(x) for x in (entry_ids or "").split(",") if x.strip().isdigit()]
    if ids:
        q = q.filter(StudioReport.entry_id.in_(ids))
    rows = q.order_by(StudioReport.updated_at.desc()).limit(1000).all()
    return {"reports": [_studio_report_json(r) for r in rows]}

@router.post("/studio-reports")
def studio_report_upsert(payload: dict = Body(...), db: Session = Depends(get_db),
                         admin=Depends(get_admin)):
    """Ek class (entry) pe ek report — dobara submit karne pe update ho jati hai.
    Studio manager / admin class ki recording, setup ya issue ka note rakhta hai."""
    from models import StudioReport, TimetableEntry
    try:
        entry_id = int(payload.get("entry_id") or 0)
    except Exception:
        entry_id = 0
    if not entry_id:
        raise HTTPException(400, "entry_id is required")
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id).first()
    if not e:
        raise HTTPException(404, "Timetable entry not found")
    status = (payload.get("status") or "held").strip().lower()
    if status not in ("held", "issues", "cancelled"):
        raise HTTPException(400, "Status must be held, issues or cancelled")
    notes = (payload.get("notes") or "").strip()[:4000]
    r = db.query(StudioReport).filter(StudioReport.entry_id == entry_id).first()
    if not r:
        r = StudioReport(entry_id=entry_id, created_by=admin.id if admin else None)
        db.add(r)
    # reporter: payload > purani value > logged-in admin ka naam
    reporter = ((payload.get("reporter") or "").strip()[:160]
                or (r.reporter or "").strip()
                or (admin.name if admin else ""))
    # snapshot hamesha entry ki latest position ka rahe (reschedule ke baad bhi sahi)
    r.entry_date = str(e.entry_date) if e.entry_date else ""
    r.day = e.day or ""
    r.time_str = getattr(e, "time_text", "") or ""
    r.subject = e.subject or ""
    r.class_name = e.class_name or ""
    r.chapter = e.chapter or ""
    r.part = e.part or ""
    r.status = status
    r.notes = notes
    r.reporter = reporter
    # v98: actual timing + class notes upload (PDF)
    r.start_time = (payload.get("start_time") or "").strip()[:20]
    r.end_time = (payload.get("end_time") or "").strip()[:20]
    fb64 = (payload.get("notes_file_b64") or "").strip()
    if fb64:
        r.notes_file_b64 = fb64
        r.notes_file_name = ((payload.get("notes_file_name") or "notes.pdf").strip()[:255])
        r.notes_file_mime = ((payload.get("notes_file_mime") or "application/pdf").strip()[:100])
    elif payload.get("remove_notes_file"):
        r.notes_file_b64 = None
        r.notes_file_name = ""
        r.notes_file_mime = ""
    db.commit()
    db.refresh(r)
    return {"ok": True, "report": _studio_report_json(r)}

@router.get("/studio-reports/{rid}/file")
def studio_report_file(rid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """v98: studio report pe attached class notes (PDF) download."""
    import base64 as _b64
    from fastapi import Response
    from models import StudioReport
    r = db.query(StudioReport).filter(StudioReport.id == rid).first()
    if not r or not r.notes_file_b64:
        raise HTTPException(404, "File not available")
    try:
        data = _b64.b64decode(r.notes_file_b64.split(",")[-1])
    except Exception:
        raise HTTPException(400, "Bad file")
    fname = r.notes_file_name or "notes.pdf"
    mime = r.notes_file_mime or "application/pdf"
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": 'attachment; filename="%s"' % fname})

@router.delete("/studio-reports/{rid}")
def studio_report_delete(rid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudioReport
    r = db.query(StudioReport).filter(StudioReport.id == rid).first()
    if not r:
        raise HTTPException(404, "Report not found")
    db.delete(r)
    db.commit()
    return {"ok": True, "message": "Report removed"}

# ===== v99: ADMIN CLASS-REPORT BACKFILL (past classes ka academic report + notes) =====
def _lec_cls5(x):
    """lectures.class_level is VARCHAR(5) (meant for '10' / '12'). Timetable
    class_name can be 'Class 10' (8 chars) — inserting it raw makes MySQL strict
    mode reject the whole class report (error 1406, "Data too long"). Normalize:
    first digit run wins ('Class 10' -> '10'); no digits -> raw, truncated;
    empty -> None. Result can never exceed 5 chars."""
    import re as _re
    raw = str(x or "").strip()
    if not raw:
        return None
    m = _re.search(r"\d+", raw)
    return (m.group(0) if m else raw)[:5]


@router.post("/class-report-backfill")
async def class_report_backfill(payload: dict = Body(...), db: Session = Depends(get_db),
                          admin=Depends(get_admin)):
    """Purani classes (batch start se aaj tak) ka class report studio manager /
    admin upload karta hai — entry completed mark hoti hai aur summary + class
    notes (PDF) students ke Lectures feed me chale jate hain. Current date se
    aage ki daily classes ka report teacher khud karta hai (teacher flow alag).
    Dobara submit karne pe SAME lecture update hota hai — duplicate nahi banta."""
    from models import TimetableEntry, TeacherProfile, Lecture, Material
    try:
        entry_id = int(payload.get("entry_id") or 0)
    except Exception:
        entry_id = 0
    if not entry_id:
        raise HTTPException(400, "entry_id is required")
    e = db.query(TimetableEntry).filter(TimetableEntry.id == entry_id).first()
    if not e:
        raise HTTPException(404, "Timetable entry not found")
    topic = (payload.get("topic_covered") or "").strip()[:300]
    summary = (payload.get("summary") or "").strip()
    homework = (payload.get("homework") or "").strip()
    pdf_b64 = (payload.get("pdf_b64") or "").strip() or None
    pdf_name = ((payload.get("pdf_filename") or "notes.pdf").strip()[:200])  # Lecture.pdf_filename / Material.filename are VARCHAR(200)
    if not topic and not summary and not pdf_b64:
        raise HTTPException(400, "Add a topic, summary or the class notes PDF")

    # 1) class completed mark karo (teacher ke class report jaisa hi)
    e.completed = True
    e.completed_at = datetime.now()
    e.topic_covered = topic or e.chapter or None
    e.start_time = (payload.get("start_time") or "").strip()[:20] or None
    e.end_time = (payload.get("end_time") or "").strip()[:20] or None
    e.homework = homework or None
    e.remarks = (payload.get("remarks") or "").strip() or None

    # 2) student-facing lecture — pehle se ho to update, warna naya
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
    tname = (tp.user.name if tp and tp.user else "") or "Admin"
    # v123: single-class subject (Social Science=10) pe official class force karo —
    # entry pe galat class likhi ho to bhi material/lecture sahi class tag paaye
    from teacher_routes import _subj_class_digits as _scd
    _fx = _scd(db, e.subject)
    eff_cls = ("Class " + _fx) if _fx else (e.class_name or "")
    lec = db.query(Lecture).filter(Lecture.timetable_entry_id == entry_id,
                                   Lecture.is_active == True).first()
    created = False
    if not lec:
        lec = Lecture(teacher_id=e.teacher_id, teacher_name=tname,
                      subject=e.subject or "", class_level=_lec_cls5(eff_cls),
                      chapter=(e.chapter or None), part=(e.part or None),
                      title=((e.chapter or "Lecture") + ((" – " + e.part) if e.part else ""))[:240],
                      timetable_entry_id=entry_id, lecture_date=e.entry_date,
                      is_active=True)
        db.add(lec); db.flush()
        created = True
    if summary:
        lec.summary = summary
    if homework:
        lec.homework = homework
    if pdf_b64:
        lec.pdf_b64 = pdf_b64
        lec.pdf_filename = pdf_name
    # Main save — a DB failure here must come back as a readable JSON error,
    # never a bare 500 (the global handler in main.py keeps CORS headers on).
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "Could not save the class report right now — please try again. If it keeps failing, contact support.")
    # Material mirror — best-effort. The lecture feed already works once the
    # commit above succeeded, so a mirror problem must never fail the upload.
    # tp.id (not e.teacher_id) keeps the FK valid even for old entries whose
    # teacher profile no longer exists.
    if pdf_b64:
        try:
            db.add(Material(teacher_id=(tp.id if tp else None), teacher_name=tname,
                            subject=e.subject or "", class_name=(eff_cls or None),
                            chapter=(e.chapter or None), part=(e.part or None),
                            material_type="notes", title=(lec.title or e.subject or "Class Notes")[:200],
                            filename=pdf_name, content_b64=pdf_b64.split(",")[-1]))
            db.commit()
        except Exception:
            db.rollback()
    return {"ok": True, "lecture_id": lec.id, "created": created,
            "message": ("Class report uploaded" if created else "Class report updated")}

# ===== v125: BULLETPROOF FALLBACK TIMETABLE PARSER =====
# tt_parser 0 rows de (ya module hi na ho) to bhi upload kabhi fail nahi hona
# chahiye — ye generic parser kisi bhi Date/Day/Time/Chapter/Part table PDF ko
# samajhta hai: bordered/borderless tables, wrapped cells, page-break continuations,
# har common date format (11-Aug-2026, 11/08/2026, 2026-08-11, 5 Jan 2026...).
_TT_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
_TT_MONTHS.update({m[:3]: v for m, v in list(_TT_MONTHS.items())})
# Common informal abbreviations people actually type in Indian timetables.
_TT_MONTHS.update({"sept": 9})
_TT_DATE_RX = re.compile(r"(\d{1,2})\s*[/\-.]\s*([A-Za-z]{3,9}|\d{1,2})\s*[/\-.]\s*(\d{2,4})")
_TT_DATE_SPC_RX = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s*,?\s*(\d{2,4})")
_TT_ISO_RX = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

def _tt_norm_year(y):
    """2-digit year -> 20xx (Indian timetables); 4-digit stays as-is."""
    y = int(y)
    return y + 2000 if y < 100 else y

def _tt_build_date(d, mon, y):
    """(day, month-token, year) -> date | None. month-token digit ya naam dono chalte hain."""
    try:
        d = int(d)
        if isinstance(mon, str) and not mon.isdigit():
            mo = _TT_MONTHS.get(mon.strip().strip(".").lower())
        else:
            mo = int(mon)
        if not mo:
            return None
        return date(_tt_norm_year(y), mo, d)
    except Exception:
        return None

def _tt_flex_date(s):
    """'11-Aug-2026' | '11/08/2026' | '2026-08-11' | '5 Jan 2026' | '11-Sept-26'
    -> date | None. Cell me thoda extra text (jaise 'Date: 11-Aug-2026 (Tue)')
    ho to bhi date nikal leta hai. Numeric format hamesha DD/MM/YYYY (Indian)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        m = _TT_ISO_RX.search(s)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _TT_DATE_RX.search(s) or _TT_DATE_SPC_RX.search(s)
        if not m:
            return None
        return _tt_build_date(m.group(1), m.group(2), m.group(3))
    except Exception:
        return None

def _tt_clean_cell(s):
    """Wrapped table cell -> single clean line. 'Cultural (1757-\\n1857)' -> '(1757-1857)'."""
    s = (s or "").replace("\r", "\n")
    s = re.sub(r"-\n", "-", s)           # hyphen pe toota hua word/number jodo
    s = re.sub(r"\s*\n\s*", " ", s)      # wrapped lines -> space
    return re.sub(r"\s{2,}", " ", s).strip()

def _tt_col_map(header_cells):
    """Header row se column indexes: date/day/time/chapter/part. None agar header nahi."""
    m = {}
    for idx, c in enumerate(header_cells or []):
        k = re.sub(r"\s+", " ", (c or "").strip().lower())
        k2 = k.replace(" ", "")
        if not k:
            continue
        if k.startswith("date") or k2 == "dt":
            m.setdefault("date", idx)
        elif k.startswith("day"):
            m.setdefault("day", idx)
        elif k.startswith("time") or k2 == "slot":
            m.setdefault("time", idx)
        elif "chapter" in k or "topic" in k or "lesson" in k or "unit" in k:
            m.setdefault("chapter", idx)
        elif k.startswith("part"):
            m.setdefault("part", idx)
    return m if ("date" in m and "chapter" in m) else None

def _detect_tt_subject(text):
    """PDF ke text se subject dhundho (registry se verify karke). Mila to raw naam, warna None."""
    if _SR is None:
        return None
    for ln in (text or "").split("\n")[:25]:
        s = re.sub(r"^subject\s*[:\-]\s*", "", ln.strip(), flags=re.I).strip()
        s = re.sub(r"\s*\(\d{3}\)\s*$", "", s).strip()   # "Social Science (213)" -> "Social Science"
        if not s or len(s) > 60:
            continue
        try:
            if _SR.canon_subject(s, "10") or _SR.canon_subject(s, "12"):
                return s
        except Exception:
            pass
    return None

def _fallback_parse_timetable_pdf(raw, force_subject=None, class_name="Class 12"):
    """Generic timetable table parser — tt_parser ka safety net.
    Row shape bilkul tt_parser jaisi: {subject, chapter, part, date, day, time, type}."""
    import io as _io
    import pdfplumber
    # Event detector (tests, doubt classes, PYQ/revision slots) — taaki direct
    # save (preview ke bina) pe bhi ye rows chapter na ban jayein aur progress
    # counting me na aayein. Module na mile to sab "chapter" — kabhi crash nahi.
    try:
        from syllabus_routes import _looks_like_event as _tt_is_event
    except Exception:
        def _tt_is_event(_t):
            return False
    rows = []
    colmap = None
    subj_detected = None
    with pdfplumber.open(_io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            # Ek kharab/tedhi page kabhi poore parse ko na girae — har page apne
            # try/except me. Baaki pages ki rows tab bhi mil jayengi.
            try:
                tables = page.extract_tables() or []
                if not tables:  # borderless table — text-position strategy
                    try:
                        tables = page.extract_tables(
                            {"vertical_strategy": "text", "horizontal_strategy": "text"}) or []
                    except Exception:
                        tables = []
            except Exception:
                tables = []
            for tbl in tables:
                if not tbl:
                    continue
                start = 0
                maybe = _tt_col_map(tbl[0])
                if maybe:
                    colmap = maybe
                    start = 1
                elif colmap is None:
                    # headerless table: first data row se layout infer karo
                    # (Date, Day, Time, Chapter, Part) — date col 0 me parse hona chahiye
                    first = [_tt_clean_cell(c) for c in (tbl[0] or [])]
                    if len(first) >= 4 and _tt_flex_date(first[0]):
                        colmap = {"date": 0, "day": 1, "time": 2, "chapter": 3}
                        if len(first) >= 5:
                            colmap["part"] = 4
                        start = 0
                    else:
                        continue
                for r in tbl[start:]:
                    cells = [_tt_clean_cell(c) for c in (r or [])]
                    if not any(cells):
                        continue
                    def _g(key, _cells=cells):
                        i = colmap.get(key)
                        return _cells[i] if (i is not None and i < len(_cells)) else ""
                    d = _tt_flex_date(_g("date"))
                    ch = _g("chapter")
                    if not d:
                        # page-break pe toota hua wrapped chapter — pichli row me jodo
                        if ch and rows and not _g("day") and not _g("time") and not _g("part"):
                            rows[-1]["chapter"] = (rows[-1]["chapter"] + " " + ch).strip()
                        continue
                    if not ch:
                        continue
                    rows.append({
                        "subject": force_subject or subj_detected or "",
                        "chapter": ch,
                        "part": _g("part") or None,
                        "date": d.isoformat(),
                        "day": (_g("day") or "").strip().title() or None,
                        "time": ((_g("time") or "").upper()
                                 .replace("A.M.", "AM").replace("P.M.", "PM")) or None,
                        "type": ("event" if _tt_is_event(ch) else "chapter"),
                    })
            if not force_subject and not subj_detected:
                try:
                    subj_detected = _detect_tt_subject(page.extract_text() or "")
                except Exception:
                    pass
    final_subj = force_subject or subj_detected
    if final_subj:
        for r in rows:
            r["subject"] = final_subj
    return rows

# ===== ADMIN: PDF TIMETABLE UPLOAD (all subjects) =====
@router.post("/timetable-pdf")
async def admin_upload_timetable_pdf(
    file: UploadFile = File(...),
    class_name: str = Form("Class 12"),
    subject: str = Form(""),
    replace: str = Form("false"),
    preview: str = Form("false"),
    from_date: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(get_admin)
):
    from models import TimetableEntry
    raw = await file.read()
    # v125: pehle tt_parser; 0 rows ya koi bhi error pe apna generic fallback parser.
    # Upload kabhi bhi parser-limitation ki wajah se fail nahi hona chahiye.
    rows = []
    _errs = []
    try:
        import tt_parser
        rows = tt_parser.parse_pdf(raw, force_subject=(subject.strip() or None)) or []
    except Exception as e:
        _errs.append(str(e)[:120])
    if not rows:
        try:
            rows = _fallback_parse_timetable_pdf(
                raw, force_subject=(subject.strip() or None), class_name=class_name) or []
        except Exception as e:
            _errs.append(str(e)[:120])
    if not rows:
        raise HTTPException(status_code=400,
            detail="No valid row found in the PDF — it should have a Date / Day / Time / Chapter table."
                   + (f" ({'; '.join(_errs)})" if _errs else ""))
    if not any((r.get("subject") or "").strip() for r in rows):
        raise HTTPException(status_code=400,
            detail="Subject could not be detected from this PDF — please choose it in the Subject dropdown and upload again.")
    # Subject naam canonical karo (PHYSICS/Physics/Data Entry Op (229) -> official NIOS naam)
    if _SR is not None:
        for r in rows:
            r["subject"] = _SR.canon_display(r.get("subject"), class_name)
    # v124: partial replace — from_date mila to sirf us date se AAGE ki rows lo;
    # us se pahle ka timetable bilkul as-is rehta hai (na delete, na insert).
    from_dt = None
    if (from_date or "").strip():
        try:
            from_dt = datetime.strptime(from_date.strip(), "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="from_date must be in YYYY-MM-DD format.")
    skipped_before = 0
    if from_dt:
        _kept = []
        for r in rows:
            try:
                _rd = datetime.strptime(r.get("date") or "", "%Y-%m-%d").date()
            except Exception:
                _rd = None
            if _rd is not None and _rd >= from_dt:
                _kept.append(r)
            else:
                skipped_before += 1
        rows = _kept
        if not rows:
            raise HTTPException(status_code=400,
                detail=f"No classes on or after {from_dt.isoformat()} were found in this PDF — nothing was changed.")
    subjects_found = sorted(set(r["subject"] for r in rows))
    # preview mode: sirf parsed rows dikhao, DB me kuch save mat karo
    if preview.lower() == "true":
        try:
            import syllabus_routes
            rows = syllabus_routes.annotate_timetable_rows(db, class_name, rows)
        except Exception:
            pass
        return {"added": 0, "subjects": subjects_found, "preview": rows,
                "from_date": (from_dt.isoformat() if from_dt else ""),
                "skipped_before_date": skipped_before}
    # replace sirf SAME CLASS ki entries hatao — dusri class ka same-name subject alag timetable hai
    if replace.lower() == "true":
        _delq = db.query(TimetableEntry).filter(
            TimetableEntry.subject.in_(subjects_found),
            TimetableEntry.class_name == class_name
        )
        if from_dt:
            # entry_date NULL ya from_dt se pahle wali entries haath hi nahi lagti
            _delq = _delq.filter(TimetableEntry.entry_date >= from_dt)
        _delq.delete(synchronize_session=False)
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
    return {"added": added, "subjects": subjects_found,
            "from_date": (from_dt.isoformat() if from_dt else ""),
            "skipped_before_date": skipped_before}

# ===== ADMIN: TIMETABLE PDF COMMIT (preview me edit/delete/split ke baad final save) =====
@router.post("/timetable-pdf-commit")
def admin_timetable_pdf_commit(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry
    rows = payload.get("rows") or []
    class_name = (payload.get("class_name") or "Class 12").strip()
    replace = str(payload.get("replace") or "false")
    # v124: partial replace — from_date se pahle ki purani entries as-is rakho
    from_dt = None
    _fd = (payload.get("from_date") or "").strip()
    if _fd:
        try:
            from_dt = datetime.strptime(_fd, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(status_code=400, detail="from_date must be in YYYY-MM-DD format.")
    clean = []
    for r in rows:
        sub = (r.get("subject") or "").strip()
        ch = (r.get("chapter") or "").strip()
        if not sub or not ch:
            continue
        clean.append({"subject": sub, "chapter": ch, "part": (r.get("part") or "").strip() or None,
                      "date": r.get("date") or "", "day": (r.get("day") or "").strip(),
                      "time": (r.get("time") or "").strip(), "type": r.get("type") or "chapter"})
    if from_dt:
        # from_date se pahle ki rows commit hi mat karo — wo purani entries ke saath
        # pahle se safe hain (double-safety, preview pehle hi filter kar chuka hota hai)
        _kept = []
        for r in clean:
            try:
                _rd = datetime.strptime(r["date"], "%Y-%m-%d").date()
            except Exception:
                _rd = None
            if _rd is not None and _rd >= from_dt:
                _kept.append(r)
        clean = _kept
    if not clean:
        raise HTTPException(status_code=400, detail="No valid rows left — keep at least 1 chapter.")
    if _SR is not None:
        for r in clean:
            r["subject"] = _SR.canon_display(r.get("subject"), class_name)
    subjects_found = sorted(set(r["subject"] for r in clean))
    if replace.lower() == "true":
        _delq = db.query(TimetableEntry).filter(
            TimetableEntry.subject.in_(subjects_found),
            TimetableEntry.class_name == class_name
        )
        if from_dt:
            _delq = _delq.filter(TimetableEntry.entry_date >= from_dt)
        _delq.delete(synchronize_session=False)
    added = 0
    for r in clean:
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
    return {"added": added, "subjects": subjects_found,
            "from_date": (from_dt.isoformat() if from_dt else "")}

# ===== ADMIN: SEND NOTIFICATION (target teachers/students/all) =====
@router.get("/notify-targets")
def admin_notify_targets(db: Session = Depends(get_db), _=Depends(get_admin)):
    """v93: admin notify modal — har target pe kitne users jayenge (live counts)."""
    q = db.query(User).filter(User.is_active == True, User.role != "admin")
    teachers = q.filter(User.role == "teacher").count()
    students = q.filter(User.role == "student").count()
    return {"teachers": teachers, "students": students, "all": teachers + students}

@router.post("/notify")
def admin_notify(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    title = (payload.get("title") or "").strip()
    message = (payload.get("message") or "").strip()
    target = (payload.get("target") or "all").strip()   # teachers | students | all
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required")
    q = db.query(User).filter(User.is_active == True, User.role != "admin")
    if target == "teachers":
        q = q.filter(User.role == "teacher")
    elif target == "students":
        q = q.filter(User.role == "student")
    users = q.all()
    for u in users:
        notify(db, u.id, "📢 " + title, message, "admin_broadcast")
    db.commit()
    return {"message": f"Notification sent to {len(users)} people!", "count": len(users)}

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
        raise HTTPException(status_code=400, detail="File is larger than 20MB")
    if _SR is not None:
        subject = _SR.canon_display(subject.strip(), class_name)
    # v123: single-class subject pe official class force (Social Science=10 etc.) —
    # frontend se galat/hardcoded class aaye to bhi split nahi hoga
    from teacher_routes import _subj_class_digits as _scd
    _cls_fixed = _scd(db, subject.strip())
    if _cls_fixed:
        class_name = "Class " + _cls_fixed
    elif not class_name.strip():
        class_name = "Class 12"
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
                                 message=f"Admin uploaded {label} for {subject.strip()}.", notif_type="new_material")
                db.add(n)
        db.commit()
    except Exception:
        db.rollback()
    return {"id": m.id, "message": "Uploaded successfully!"}

@router.get("/material/{mid}/download")
def admin_download(mid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    import base64
    from fastapi import Response
    from models import Material
    m = db.query(Material).filter(Material.id == mid).first()
    if not m: raise HTTPException(status_code=404, detail="Not found")
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
        raise HTTPException(status_code=404, detail="Not found")
    e.status = "approved"
    # ---- AUTO-SHIFT: extra class ke saath jo plan bana tha, ab apply karo ----
    shifted = 0
    if getattr(e, "shift_plan", None):
        try:
            import json as _json
            from datetime import datetime as _dtx
            plan = _json.loads(e.shift_plan)
            for row in plan:
                tgt = db.query(TimetableEntry).filter(TimetableEntry.id == row.get("id")).first()
                if not tgt or not row.get("to"):
                    continue
                nd = _dtx.strptime(row["to"], "%Y-%m-%d").date()
                tgt.entry_date = nd
                tgt.day = nd.strftime("%a")
                shifted += 1
            e.shift_plan = None
        except Exception:
            db.rollback()
            e.status = "approved"
    # notify teacher
    if e.teacher_id:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first()
        if tp and tp.user:
            msg = f"Aapki {e.subject} extra class ({e.entry_date}) approve ho gayi."
            if shifted:
                msg += f" {shifted} later classes were shifted automatically."
            db.add(Notification(user_id=tp.user.id, title="Extra Class Approved",
                                message=msg, notif_type="class_approved"))
    # notify students of that subject
    _nk = (_SR.canon_norm(e.subject) if _SR else e.subject)
    for sp in db.query(StudentProfile).all():
        if sp.subjects and _nk in {(_SR.canon_norm(x) if _SR else x) for x in sp.subjects} and sp.user:
            db.add(Notification(user_id=sp.user.id, title=f"New Class: {e.subject}",
                                message=f"An extra class was added for {e.subject} ({e.entry_date} {e.time_text or ''}). See the time table.",
                                notif_type="new_class"))
    db.commit()
    return {"message": "Class approved!" + (f" {shifted} upcoming classes auto-shifted." if shifted else ""),
            "shifted": shifted}

@router.post("/class/{eid}/reject")
def reject_class(eid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry, TeacherProfile, Notification
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    tid = e.teacher_id; subj = e.subject
    db.delete(e)
    if tid:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
        if tp and tp.user:
            db.add(Notification(user_id=tp.user.id, title="Extra Class Rejected",
                                message=f"Your {subj} extra class request was rejected.", notif_type="class_rejected"))
    db.commit()
    return {"message": "Rejected"}

# ===== ADMIN: SUBJECT-WISE STUDENT COUNTS =====

def _exam_ranking_rows(db, exam_id):
    """Graded attempts -> ranked rows (top 3 podium + list). Shared by all roles."""
    from models import Exam, ExamAttempt, StudentProfile
    exam = db.query(Exam).filter(Exam.id == exam_id).first()
    if not exam:
        return None
    atts = (db.query(ExamAttempt)
            .filter(ExamAttempt.exam_id == exam_id, ExamAttempt.status == "graded")
            .all())
    rows = []
    for a in atts:
        sp = db.query(StudentProfile).filter(StudentProfile.id == a.student_id).first()
        nm = a.student_name or ((sp.user.name if sp and sp.user else "") or "Student")
        att_n = None
        if a.attempted:
            att_n = len(a.attempted)
        elif a.mcq_answers:
            att_n = len([v for v in (a.mcq_answers or {}).values() if v not in (None, "", [])])
        rows.append({"student_id": a.student_id, "name": nm,
                     "marks": round(float(a.total_awarded or 0), 1),
                     "attempted": att_n,
                     "batch": (sp.batch_name if sp else None),
                     "has_photo": bool(sp and sp.photo_b64)})
    rows.sort(key=lambda r: (-r["marks"], r["name"].lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"exam": {"id": exam.id, "title": exam.title, "subject": exam.subject,
                     "chapter": exam.chapter, "total_marks": exam.total_marks,
                     "test_type": exam.test_type},
            "graded": len(rows), "rows": rows}

@router.get("/exam/{exam_id}/ranking")
def admin_exam_ranking(exam_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    data = _exam_ranking_rows(db, exam_id)
    if not data:
        raise HTTPException(status_code=404, detail="Test not found")
    data["me_id"] = None
    return data


@router.get("/student-counts")
def admin_student_counts(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Subject counts SPLIT BY CLASS LEVEL — same-name subjects (English 202 vs
    English 302, Hindi, Data Entry Operations) alag alag cards me dikhte hain."""
    from models import StudentProfile, AvailableSubject
    import re as _re
    def _sk(name):
        t = str(name or "")
        t = _re.sub(r"\((?:class\s*)?\d+(?:th)?\)", " ", t, flags=_re.I)
        t = _re.sub(r"[^a-z0-9]+", " ", t.lower())
        return " ".join(t.split()).strip()
    students = db.query(StudentProfile).all()
    code_map, class_map = {}, {}
    for a in db.query(AvailableSubject).all():
        code_map[(_sk(a.name), str(a.class_level or "").strip())] = a.code
        class_map.setdefault(_sk(a.name), str(a.class_level or "").strip())
    counts = {}
    for sp in students:
        cls = str(sp.class_level or "").strip() or "?"
        seen = set()   # ek student ka same canonical subject 2 baar na gine
        for s in (sp.subjects or []):
            if _SR is not None:
                key = (_SR.canon_key(s, cls), cls)
                if key in seen:
                    continue
                seen.add(key)
                disp = _SR.canon_display(s, cls)
                can = _SR.canon_subject(s, cls)
                code = (can or {}).get("code") or code_map.get((_sk(disp), cls))
            else:
                key = (s, cls)
                if key in seen:
                    continue
                seen.add(key)
                disp = s
                code = code_map.get((_sk(s), cls))
            k = (disp, cls, code)
            counts[k] = counts.get(k, 0) + 1
    out = [{"subject": k[0], "class": k[1], "code": k[2], "count": v}
           for k, v in counts.items()]
    out.sort(key=lambda x: (-x["count"], (x["subject"] or ""), x["class"]))
    return {"total_students": len(students), "subjects": out}

# ===== ADMIN: DELETE A TIMETABLE CLASS (admin-only) =====
@router.delete("/timetable-entry/{eid}")
def admin_delete_tt(eid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    db.delete(e); db.commit()
    return {"message": "Class deleted"}

# ===== ADMIN: FULL EDIT OF ANY TIMETABLE ENTRY =====
@router.patch("/timetable-entry/{eid}")
def admin_edit_tt(eid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Admin kisi bhi teacher/subject ki entry ke saare fields edit kar sakta hai.
    Day HAMESHA date se auto-calculate hota hai (galat day kabhi save nahi hoga)."""
    from models import TimetableEntry
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    def _s(k):
        return (payload.get(k) or "").strip()
    if "subject" in payload and _s("subject"):
        e.subject = _s("subject")
    if "class_name" in payload and _s("class_name"):
        e.class_name = _s("class_name")
    if "chapter" in payload and _s("chapter"):
        e.chapter = _s("chapter")
    if "part" in payload:
        e.part = _s("part") or None
    if "time" in payload:
        e.time_text = _s("time") or None
    if "type" in payload and _s("type") in ("chapter", "event"):
        e.entry_type = _s("type")
    if "entry_date" in payload:
        d = _s("entry_date")
        if d:
            try:
                e.entry_date = datetime.strptime(d, "%Y-%m-%d").date()
                e.day = e.entry_date.strftime("%A")
            except Exception:
                raise HTTPException(status_code=400, detail="Date must be in YYYY-MM-DD format")
        else:
            e.entry_date = None
            e.day = _s("day") or None
    db.commit()
    return {"message": "Entry updated", "id": e.id}

# ===== ADMIN: DIRECT RESCHEDULE (teacher ke count/salary pe koi asar NAHI) =====
@router.post("/timetable-entry/{eid}/reschedule")
def admin_reschedule_tt(eid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(get_admin)):
    """Teacher centre pe hai par class ka time/date badalna hai — admin directly move
    kare. Ye ADMIN move hai: teacher ke reschedule count me NAHI jata, salary pe asar
    NAHI. Teacher + students (photo ke saath) ko notification jaati hai."""
    from models import TimetableEntry, TeacherProfile, Notification
    from teacher_routes import _ensure_v86, _notify_class_moved
    _ensure_v86(db)
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid).first()
    if not e:
        raise HTTPException(status_code=404, detail="Entry not found")
    try:
        nd = datetime.strptime((payload.get("new_date") or "").strip(), "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Please choose a valid new date")
    nt = (payload.get("new_time") or "").strip()
    od, ot = e.entry_date, e.time_text
    e.entry_date = nd
    e.day = nd.strftime("%A")
    if nt:
        e.time_text = nt
    e.resched_by = "admin"
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == e.teacher_id).first() if e.teacher_id else None
    if tp:
        _notify_class_moved(db, tp, [(e, od, ot)], "Class Rescheduled — ",
                            "The schedule was updated by the admin.")
        if tp.user_id:
            db.add(Notification(
                user_id=tp.user_id, title="Class Rescheduled by Admin",
                message=(f"Admin ne aapki {e.subject} ({e.class_name or ''}) class "
                         f"{od} {ot or ''} se {nd} {e.time_text or ''} pe move ki hai"
                         + (f". Reason: {(payload.get('reason') or '').strip()}" if (payload.get('reason') or '').strip() else "")
                         + ". Ye aapke reschedule count ya salary pe asar nahi daalegi."),
                notif_type="timetable"))
    db.commit()
    return {"ok": True, "message": "Class moved. Teacher and students have been notified — teacher ke reschedule count/salary par koi asar nahi."}

# ===== ADMIN: TEACHER TT-RESCHEDULE REQUESTS (timetable entries) =====
@router.get("/tt-reschedules/pending")
def admin_tt_reschedules_pending(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Teacher ne timetable page se bheji reschedule requests (topic-edit modal)."""
    from models import RescheduleRequest, RescheduleStatus, TimetableEntry, TeacherProfile
    from teacher_routes import _ensure_v86
    _ensure_v86(db)
    rows = db.query(RescheduleRequest).filter(
        RescheduleRequest.status == RescheduleStatus.pending,
        RescheduleRequest.tt_entry_id != None).order_by(RescheduleRequest.created_at.desc()).all()
    out = []
    for r in rows:
        e = db.query(TimetableEntry).filter(TimetableEntry.id == r.tt_entry_id).first()
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == r.teacher_id).first()
        out.append({"id": r.id,
                    "teacher": tp.user.name if tp and tp.user else "",
                    "subject": e.subject if e else "", "class_name": (e.class_name if e else "") or "",
                    "chapter": (e.chapter if e else "") or "",
                    "original_date": str(e.entry_date) if e and e.entry_date else str(r.original_date or ""),
                    "original_time": ((e.time_text if e else "") or ""),
                    "new_date": str(r.new_date) if r.new_date else "",
                    "new_time": r.new_time.strftime("%I:%M %p") if r.new_time else "",
                    "reason": r.reason or "",
                    "created_at": r.created_at.strftime("%d %b %Y, %I:%M %p") if r.created_at else ""})
    return out

@router.post("/tt-reschedules/{rid}/review")
def admin_tt_reschedule_review(rid: int, payload: dict = Body(default={}),
                               db: Session = Depends(get_db), _=Depends(get_admin)):
    """Approve => entry move + teacher ka monthly reschedule count +1 + notifications.
    Reject => teacher ko note ke saath notification."""
    from models import (RescheduleRequest, RescheduleStatus, TimetableEntry,
                        TeacherProfile, Notification)
    from teacher_routes import _ist_now, _ensure_v86, _notify_class_moved
    _ensure_v86(db)
    rs = db.query(RescheduleRequest).filter(RescheduleRequest.id == rid).first()
    if not rs or rs.tt_entry_id is None:
        raise HTTPException(status_code=404, detail="Request not found")
    if rs.status != RescheduleStatus.pending:
        raise HTTPException(status_code=400, detail="This request has already been processed")
    action = (payload.get("status") or payload.get("action") or "").strip().lower()
    if action not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    note = (payload.get("admin_note") or payload.get("note") or "").strip()
    rs.status = RescheduleStatus.approved if action == "approved" else RescheduleStatus.rejected
    rs.admin_note = note or None
    rs.reviewed_at = _ist_now()
    e = db.query(TimetableEntry).filter(TimetableEntry.id == rs.tt_entry_id).first()
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == rs.teacher_id).first()
    if action == "approved":
        if not e:
            raise HTTPException(status_code=404, detail="The timetable entry no longer exists")
        od, ot = e.entry_date, e.time_text
        e.entry_date = rs.new_date
        e.day = rs.new_date.strftime("%A")
        if rs.new_time:
            e.time_text = rs.new_time.strftime("%I:%M %p").lstrip("0")
        e.resched_by = "teacher"          # TEACHER request — count me jayegi
        if tp:
            now = _ist_now()
            if tp.reschedule_reset_month != now.month:
                tp.reschedule_count_this_month = 0
                tp.reschedule_reset_month = now.month
            tp.reschedule_count_this_month = (tp.reschedule_count_this_month or 0) + 1
            _notify_class_moved(db, tp, [(e, od, ot)], "Class Rescheduled — ",
                                "The class was rescheduled by your teacher.")
        if tp and tp.user_id:
            db.add(Notification(
                user_id=tp.user_id, title="✅ Reschedule Approved",
                message=(f"Aapki {e.subject} ({e.class_name or ''}) class {od} {ot or ''} se "
                         f"{e.entry_date} {e.time_text or ''} pe move ho gayi."
                         + (f" Note: {note}" if note else "")),
                notif_type="reschedule_approved"))
    else:
        if tp and tp.user_id:
            db.add(Notification(
                user_id=tp.user_id, title="❌ Reschedule Rejected",
                message=(f"{e.subject if e else 'Class'} ki reschedule request reject ho gayi."
                         + (f" Note: {note}" if note else "")),
                notif_type="reschedule_rejected"))
    db.commit()
    return {"ok": True, "status": action}

# ===== ADMIN: DELETE ENTIRE SUBJECT TIMETABLE (one click, bulletproof on frontend) =====
@router.delete("/timetable-subject")
def admin_delete_tt_subject(subject: str, class_level: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek subject ka poora timetable delete. class_level ('10'/'12') optional filter hai
    taaki same naam wale subjects (e.g. Economics 10 vs 12) alag-alag delete ho saken."""
    from models import TimetableEntry
    subject = (subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required")
    q = db.query(TimetableEntry).filter(TimetableEntry.subject == subject)
    cl = (class_level or "").strip()
    if cl in ("10", "12"):
        q = q.filter(TimetableEntry.class_name.like(f"%{cl}%"))
    n = q.delete(synchronize_session=False)
    db.commit()
    return {"deleted": n, "message": f"{n} entries deleted for {subject}"}

# ===== ADMIN: CLEAR TIMETABLE (whole class or everything) =====
@router.delete("/timetable-clear")
def admin_clear_tt(class_level: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Poora timetable ya ek class ka timetable delete. Frontend pe type-to-confirm hai."""
    from models import TimetableEntry
    q = db.query(TimetableEntry)
    cl = (class_level or "").strip()
    if cl in ("10", "12"):
        q = q.filter(TimetableEntry.class_name.like(f"%{cl}%"))
    n = q.delete(synchronize_session=False)
    db.commit()
    return {"deleted": n, "message": f"{n} timetable entries deleted"}

# ===== PHOTOS + STUDENT LIST + BULK-BY-PHONE =====
def _img_response(b64):
    import base64
    from fastapi import Response
    if not b64:
        raise HTTPException(status_code=404, detail="No photo")
    return Response(content=base64.b64decode(b64), media_type="image/jpeg")

@router.post("/teacher/{tid}/photo")
async def admin_upload_teacher_photo(tid: int, file: UploadFile = File(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    import base64
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Photo is larger than 5MB")
    tp.photo_b64 = base64.b64encode(raw).decode("ascii")
    db.commit()
    return {"message": "Photo uploaded!"}

@router.delete("/teacher/{tid}/photo")
def remove_teacher_photo(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    tp.photo_b64 = None
    db.commit()
    return {"message": "Photo removed"}


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
def admin_students_list(q: str = "", subject: str = "", cls: str = "", session: str = "",
                        medium: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    import re as _re
    def _sk(name):
        if _SR is not None:
            return _SR.canon_norm(name)
        t = str(name or "")
        t = _re.sub(r"\((?:class\s*)?\d+(?:th)?\)", " ", t, flags=_re.I)
        t = _re.sub(r"[^a-z0-9]+", " ", t.lower())
        return " ".join(t.split()).strip()
    rows = db.query(StudentProfile).all()
    ql = q.strip().lower()
    want = _sk(subject) if subject else ""
    want_cls = (cls or "").strip()
    want_sess = (session or "").strip()
    want_med = (medium or "").strip().lower()
    out = []
    for sp in rows:
        nm = sp.user.name if sp.user else ""
        if ql and ql not in nm.lower() and ql not in (sp.phone or ""):
            continue
        ssubs = sp.subjects or []
        if want and want not in {_sk(x) for x in ssubs}:
            continue
        if want_cls and str(sp.class_level or "").strip() != want_cls:
            continue
        if want_sess and (sp.exam_session or "") != want_sess:
            continue
        if want_med and (sp.medium or "").strip().lower() != want_med:
            continue
        disp_subs = _SR.canon_list(ssubs, sp.class_level) if _SR else ssubs
        out.append({"id": sp.id, "name": nm, "phone": sp.phone, "class": sp.class_level,
                    "subjects": disp_subs, "all_subjects": disp_subs, "has_photo": bool(sp.photo_b64),
                    "batch": sp.batch_name, "medium": sp.medium, "email": sp.email,
                    "class_name": sp.class_name, "nios_ref": sp.nios_ref,
                    "exam_session": sp.exam_session, "exam_stream": sp.exam_stream,
                    "goal": (sp.goal_custom if sp.goal == "other" else sp.goal),
                    "last_seen": sp.last_seen.strftime("%d %b %Y, %I:%M %p") if sp.last_seen else None,
                    "is_verified": bool(sp.is_verified),
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
    """Bullet-proof: lamba sales batch naam ko canonical short naam mein badlo.
    Ye naam STUDENT_BATCHES (onboarding) ke keys se EXACTLY match karte hain, taaki
    import aur student onboarding dono ek hi batch naam use karein."""
    if not text:
        return None
    t = str(text).lower()
    if "lakshya" in t and "science" in t:
        return "Lakshya Science"
    if "lakshya" in t and "commerce" in t:
        return "Lakshya Commerce"
    if "lakshya" in t and ("arts" in t or "art " in t):
        return "Lakshya Arts"
    if "science" in t:
        return "Lakshya Science"
    if "commerce" in t:
        return "Lakshya Commerce"
    if "arts" in t:
        return "Lakshya Arts"
    if "udaan" in t or "class 10" in t or "10th" in t:
        return "Udaan Class 10"
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
        _st_exam = None
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
                _st_exam = dict(st)  # exam info niche profile banne ke baad apply hogi
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
        _nsp = StudentProfile(user_id=u.id, phone=phone, subjects=psubs, class_name="",
                              batch_name=batch, email=email, is_verified=True,
                              plain_password=phone, source=psrc,
                              medium=pmed, class_level=pcls)
        db.add(_nsp)
        if _st_exam:
            try:
                _apply_portal_exam_info(_nsp, _st_exam, db)
            except Exception:
                pass
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
        raise HTTPException(status_code=404, detail="Teacher not found")
    if "name" in payload and tp.user:
        tp.user.name = (payload["name"] or "").strip() or tp.user.name
    if "phone" in payload:
        tp.phone = (payload.get("phone") or "").strip() or None
    if "subject_classes" in payload and isinstance(payload["subject_classes"], list):
        sc = []
        for x in payload["subject_classes"]:
            if not (isinstance(x, dict) and (x.get("subject") or "").strip()):
                continue
            x = dict(x)
            if _SR is not None:
                x["subject"] = _SR.canon_display(x["subject"].strip(), x.get("class") or x.get("class_name"))
            sc.append(x)
        tp.subject_classes = sc
        seen = []
        for x in sc:
            if x["subject"] not in seen:
                seen.append(x["subject"])
        tp.subjects = seen
    elif "subjects" in payload and isinstance(payload["subjects"], list):
        _raw = [s.strip() for s in payload["subjects"] if s.strip()]
        tp.subjects = _SR.canon_list(_raw) if _SR else _raw
    if "is_active" in payload and tp.user:
        tp.user.is_active = bool(payload["is_active"])
    db.commit()
    return {"message": "Teacher updated"}

@router.delete("/teacher/{tid}")
def delete_teacher(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    uid = tp.user_id
    stmts = [
        ("UPDATE doubts SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("UPDATE timetable_entries SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("UPDATE materials SET teacher_id=NULL WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM reschedule_requests WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM class_entries WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM dpps WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM tests WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM exam_results WHERE attempt_id IN (SELECT id FROM exam_attempts WHERE exam_id IN (SELECT id FROM exams WHERE teacher_id=:t))", {"t": tid}),
        ("DELETE FROM exam_attempts WHERE exam_id IN (SELECT id FROM exams WHERE teacher_id=:t)", {"t": tid}),
        ("DELETE FROM exam_questions WHERE exam_id IN (SELECT id FROM exams WHERE teacher_id=:t)", {"t": tid}),
        ("DELETE FROM exam_views WHERE exam_id IN (SELECT id FROM exams WHERE teacher_id=:t)", {"t": tid}),
        ("DELETE FROM exams WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM lecture_questions WHERE lecture_id IN (SELECT id FROM lectures WHERE teacher_id=:t)", {"t": tid}),
        ("DELETE FROM lectures WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM timetables WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM teacher_attendance WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM teacher_contracts WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM payout_adjustments WHERE teacher_id=:t", {"t": tid}),
        ("DELETE FROM dpp_submissions WHERE dpp_id NOT IN (SELECT id FROM dpps)", {}),
        ("DELETE FROM test_submissions WHERE test_id NOT IN (SELECT id FROM tests)", {}),
        ("DELETE FROM notifications WHERE user_id=:u", {"u": uid}),
        ("DELETE FROM teacher_profiles WHERE id=:t", {"t": tid}),
        ("DELETE FROM users WHERE id=:u", {"u": uid}),
    ]
    for sql, p in stmts:
        db.execute(_sqltext(sql), p)
    db.commit()
    return {"message": "Teacher deleted"}

@router.patch("/student/{sid}")
def edit_student(sid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student not found")
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
        _raw = [s.strip() for s in payload["subjects"] if s.strip()]
        sp.subjects = _SR.canon_list(_raw, sp.class_level) if _SR else _raw
    if payload.get("exam_session"):
        sid = (payload.get("exam_session") or "").strip()[:30]
        mapped = _map_session_text(db, sid)
        if not mapped:
            raise HTTPException(status_code=400, detail="Unknown exam session")
        sp.exam_session = mapped
        stv = _stream_for_session(db, mapped)
        if stv:
            sp.exam_stream = stv
    # NOTE: empty exam_session / nios_ref form value se PURANA value kabhi
    # erase nahi hota — sirf non-empty value hi update karti hai.
    # Admin chahe to explicit nios_ref_clear=true bhej ke reference hata sakta
    # hai (test/dummy refs) — har jagah (profile, result cards, admin views)
    # live read hota hai, isliye sab jagah se apne aap update ho jaata hai.
    if payload.get("nios_ref_clear"):
        sp.nios_ref = None
    elif payload.get("nios_ref"):
        ref = (payload.get("nios_ref") or "").strip().upper()[:40]
        sp.nios_ref = ref
    db.commit()
    return {"message": "Student updated"}

@router.delete("/student/{sid}")
def delete_student(sid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import StudentProfile
    sp = db.query(StudentProfile).filter(StudentProfile.id == sid).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Student not found")
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
    return {"message": "Student deleted"}

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

@router.patch("/notifications/read-all")
def admin_notif_read_all(db: Session = Depends(get_db), current_user=Depends(get_admin)):
    """Panel/bell view karte hi sab notifications ek saath read — badge/blink clear."""
    db.query(Notification).filter(Notification.user_id == current_user.id,
                                  Notification.is_read == False).update(
        {"is_read": True, "read_at": datetime.now()}, synchronize_session=False)
    db.commit()
    return {"ok": True}


@router.patch("/notifications/{notif_id}/read")
def admin_notif_read(notif_id: int, db: Session = Depends(get_db), current_user=Depends(get_admin)):
    n = db.query(Notification).filter(
        Notification.id == notif_id, Notification.user_id == current_user.id
    ).first()
    if n:
        n.is_read = True
        if not n.read_at:
            n.read_at = datetime.now()
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
            # v93: thread + reassignment context
            "assigned_to_admin": bool(getattr(d, "assigned_to_admin", False)),
            "assigned_by_name": getattr(d, "assigned_by_name", None),
            "owner_name": ("MVS Foundation" if getattr(d, "assigned_to_admin", False)
                           else (tp.user.name if tp and tp.user else "Unassigned")),
            "needs_attention": _doubt_needs_attention(db, d.id),
            "responses": _admin_doubt_resps(db, d.id),
        })
    return out

def _admin_doubt_resps(db, did):
    """v93: doubt thread responses — admin view ke liye."""
    from models import DoubtResponse
    out = []
    for r in (db.query(DoubtResponse).filter(DoubtResponse.doubt_id == did)
              .order_by(DoubtResponse.created_at.asc(), DoubtResponse.id.asc()).all()):
        out.append({"id": r.id, "role": r.role, "author_name": r.author_name,
                    "body": r.body, "mine": (r.role == "admin"),
                    "author_tid": (r.author_teacher_id if r.role == "teacher" else None),
                    "created_at": r.created_at.isoformat() if r.created_at else None})
    return out

def _doubt_needs_attention(db, did):
    """FOLLOW-UP SYSTEM HATA DIYA: resolved doubt ab kabhi reopen nahi hota (student
    dobara poochhega to naya doubt banega)."""
    return False

@router.post("/doubts/{did}/respond")
def admin_doubt_respond(did: int, payload: dict, db: Session = Depends(get_db),
                        current_user=Depends(get_admin)):
    """v93: admin doubt thread pe likhe — response MVS Foundation branding ke
    saath jata hai. Pending doubt ho to admin ka jawab final maana jata hai
    (status resolved)."""
    from models import Doubt, DoubtResponse, DoubtStatus, StudentProfile, TeacherProfile
    d = db.query(Doubt).get(did)
    if not d:
        raise HTTPException(status_code=404, detail="Doubt not found")
    body = (payload.get("body") or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Response text is required")
    db.add(DoubtResponse(doubt_id=d.id, role="admin", author_name="MVS Foundation", body=body))
    was_pending = (d.status.value if hasattr(d.status, "value") else d.status) != "resolved"
    if was_pending:
        if not d.answer:
            d.answer = body
        d.status = DoubtStatus.resolved
        d.resolved_at = datetime.now()
    sp = db.query(StudentProfile).filter(StudentProfile.id == d.student_id).first()
    if sp and sp.user:
        notify(db, sp.user.id, "🏛️ MVS Foundation Replied to Your Doubt",
               f"Your {d.subject or ''} doubt got an official response: {body[:120]}", "doubt_resolved")
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == d.teacher_id).first() if d.teacher_id else None
    if tp and tp.user:
        notify(db, tp.user.id, "🏛️ Admin Replied on a Doubt",
               f"MVS Foundation posted an official reply on the {d.subject or ''} doubt by "
               f"{sp.user.name if sp and sp.user else 'a student'}.", "doubt")
    db.commit()
    return {"message": "Official response posted",
            "responses": _admin_doubt_resps(db, d.id),
            "status": (d.status.value if hasattr(d.status, "value") else d.status)}

@router.delete("/doubt/{did}")
def admin_delete_doubt(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Admin kisi bhi doubt ko delete kar sakta hai (demo/test doubts) —
    student aur teacher dono portals se turant hat jata hai."""
    d = db.query(Doubt).get(did)
    if not d:
        raise HTTPException(status_code=404, detail="Doubt not found")
    db.delete(d)
    db.commit()
    return {"message": "Doubt deleted"}


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


# ==================================================== LIVE USERS (students + teachers + admins)
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

    users = db.query(User).filter(User.role.in_([UserRole.student, UserRole.teacher, UserRole.admin])).all()
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
                       "admins_live": sum(1 for x in live if x["role"] == "admin"),
                       "never": len(never)}}


@router.get("/user/{user_id}/sessions")
def admin_user_sessions(user_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Every time this person came online - the 'recent' list the admin scrolls."""
    from models import UserSession, User, StudentProfile, TeacherProfile
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    rows = db.query(UserSession).filter(UserSession.user_id == user_id).order_by(
        UserSession.started_at.desc()).limit(60).all()
    # full profile detail for the click-view (batch / phone / class / medium / subjects)
    detail = {}
    sp = db.query(StudentProfile).filter(StudentProfile.user_id == user_id).first()
    if sp:
        detail = {"batch": sp.batch_name or (sp.batch.value if hasattr(sp.batch, "value") else sp.batch) or "",
                  "phone": sp.phone or "", "class_level": sp.class_level or "",
                  "class_name": sp.class_name or "", "medium": sp.medium or "",
                  "subjects": sp.subjects or [], "email": sp.email or ""}
    else:
        tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user_id).first()
        if tp:
            detail = {"batch": tp.batch or "", "phone": tp.phone or "",
                      "subjects": tp.subjects or []}
    out = []
    for s in rows:
        mins = 0
        if s.started_at and s.last_seen:
            mins = max(0, int((s.last_seen - s.started_at).total_seconds() // 60))
        out.append({"started": str(s.started_at)[:16], "last_seen": str(s.last_seen)[:16],
                    "minutes": mins, "page": s.current_page or "\u2014"})
    return {"name": u.name, "code": u.user_id,
            "role": getattr(u.role, "value", str(u.role)),
            "logins": len(out), "sessions": out, "detail": detail}


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
    msg = ("Your classes have been starting late repeatedly.\n\n"
           "This affects MVS Foundation's reputation and makes the children anxious. "
           "It will also reflect in your monthly report.\n\n"
           "Please start your classes on time.")
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
            # v112: student ka naya follow-up -> phir se attention maangta hai
            if _doubt_needs_attention(db, d.id):
                c["pending"] += 1
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

@router.get("/credentials")
def admin_view_credentials(role: str, profile_id: int, db: Session = Depends(get_db),
                           _=Depends(get_admin)):
    """Admin ko kisi teacher/student ke CURRENT login credentials dikhao —
    bina reset kiye. plain_password sirf tab available jab account portal se
    bana ho ya kabhi admin ne reset kiya ho (hash se purana recover nahi hota)."""
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
    return {"name": user.name, "user_id": user.user_id,
            "password": (getattr(prof, "plain_password", None) or None)}


# ===== v89: PAYOUT PASSCODE RESET — admin approval ke baad hi reset =====
@router.get("/passcode-resets")
def admin_passcode_resets(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Jin teachers ne payout passcode reset maanga hai (pending approvals)."""
    rows = (db.query(TeacherProfile, User)
              .join(User, TeacherProfile.user_id == User.id)
              .filter(TeacherProfile.passcode_reset_pending == True)
              .order_by(TeacherProfile.id).all())
    return [{"teacher_id": tp.id, "name": u.name, "user_id": u.user_id,
             "subjects": tp.subjects or [], "has_photo": bool(tp.photo_b64)}
            for tp, u in rows]


@router.post("/passcode-resets/{tid}/review")
def admin_passcode_reset_review(tid: int, payload: dict, db: Session = Depends(get_db),
                                _=Depends(get_admin)):
    """Approve → purana passcode clear (teacher naya banayega). Reject → kuch nahi badalta."""
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    status = (payload.get("status") or "").strip().lower()
    if status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    tp.passcode_reset_pending = False
    u = db.query(User).filter(User.id == tp.user_id).first()
    if status == "approved":
        tp.payout_passcode = None
        if u:
            db.add(Notification(user_id=u.id, title="Passcode Reset Approved",
                                message="Your payout passcode reset request has been approved by the admin. Open the Payout section to set a new passcode.",
                                notif_type="passcode_reset_approved"))
    else:
        if u:
            db.add(Notification(user_id=u.id, title="Passcode Reset Rejected",
                                message="Your payout passcode reset request has been rejected. If you did not send this request, please contact the admin.",
                                notif_type="passcode_reset_rejected"))
    db.commit()
    return {"message": "Passcode reset " + status, "teacher_id": tid, "status": status}


# ===== v90: LETTER REMARKS — teacher ke doubts admin check karke reply kare =====
@router.get("/letter-remarks")
def admin_letter_remarks(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Jin teachers ne appointment letter pe doubt/remark bheja hai (pending)."""
    rows = (db.query(TeacherProfile, User)
              .join(User, TeacherProfile.user_id == User.id)
              .filter(TeacherProfile.letter_remark_status == "pending")
              .order_by(TeacherProfile.letter_remark_at.desc().nullslast())
              .all())
    out = []
    for tp, u in rows:
        out.append({"teacher_id": tp.id, "name": u.name, "user_id": u.user_id,
                    "subjects": tp.subjects or [], "has_photo": bool(tp.photo_b64),
                    "remark": tp.letter_remark or "",
                    "at": tp.letter_remark_at.isoformat() if tp.letter_remark_at else ""})
    return out


@router.post("/letter-remarks/{tid}/review")
def admin_letter_remark_review(tid: int, payload: dict, db: Session = Depends(get_db),
                               _=Depends(get_admin)):
    """Reply likhkar remark resolve karo — teacher ko notification chala jaata hai.
    Letter me kuch badalna ho to Payouts → Pay Structure se update karke yahan bata do."""
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    if tp.letter_remark_status != "pending":
        raise HTTPException(status_code=400, detail="This teacher has no pending remark.")
    reply = (payload.get("reply") or "").strip()
    if len(reply) < 3:
        raise HTTPException(status_code=400, detail="Please write a reply the teacher can understand.")
    tp.letter_remark_status = "resolved"
    tp.letter_remark_reply = reply
    u = db.query(User).filter(User.id == tp.user_id).first()
    if u:
        db.add(Notification(user_id=u.id, title="Letter Remark — Admin Reply",
                            message="The admin has replied to your appointment letter remark: " + reply[:180] + ("..." if len(reply) > 180 else ""),
                            notif_type="letter_remark_resolved"))
    db.commit()
    return {"message": "Reply sent — remark resolved.", "teacher_id": tid, "status": "resolved"}


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
    prof.plain_password = new_pass  # teacher+student dono — admin ko credentials visible rahen
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
_EXAM_KEY_CANDIDATES = {
    "session": ("exam_session", "session", "exam_session_label", "examsession"),
    "stream":  ("exam_stream", "stream", "nios_stream", "examstream"),
    "ref":     ("nios_ref", "nios_reference", "niosref", "nios_ref_no", "niosrefno",
                "reference", "ref", "ref_no", "refno", "reference_no", "referenceno",
                "enrollment", "enrollment_no", "enrollmentno", "enrollment_number",
                "enrolment_no", "enroll_no", "enrollno", "registration_no",
                "registration_number", "reg_no", "regno", "admission_no",
                "adm_no", "admno", "student_ref", "roll_no", "uid"),
}


def _portal_pick(st, keys):
    # case-insensitive bhi: class manager 'refNo' / 'Ref_No' / 'NIOS_REF' bheje to bhi mile
    lower_map = {}
    for k in st.keys():
        try:
            lower_map.setdefault(str(k).lower(), k)
        except Exception:
            pass
    for k in keys:
        v = st.get(k)
        if v and str(v).strip():
            return str(v).strip()
        lk = lower_map.get(k.lower())
        if lk is not None:
            v = st.get(lk)
            if v and str(v).strip():
                return str(v).strip()
    return ""


def _map_session_text(db, text):
    """Class-manager ka exam session text (e.g. 'Stream 2', 'October 2026')
    ko hamare session id (stream2 / oct2026 / ondemand / apr2027) se map karo.
    Match na mile to '' (kuch overwrite mat karo)."""
    t = (text or "").strip().lower()
    if not t:
        return ""
    t2 = t.replace(" ", "")
    try:
        from syllabus_routes import _sessions
        sess = _sessions(db)
    except Exception:
        sess = []
    for x in sess:
        xid = (x.get("id") or "").lower()
        lbl = (x.get("label") or "").lower()
        if t2 == xid or t == lbl:
            return xid
    for x in sess:  # substring match on label: 'stream 2' ⊂ 'stream 2 examination'
        xid = (x.get("id") or "").lower()
        lbl2 = (x.get("label") or "").lower().replace(" ", "")
        if t2 and t2 in lbl2:
            return xid
    if "ondemand" in t2 or "odes" in t2:
        return "ondemand"
    if "stream2" in t2:
        return "stream2"
    if "stream1" in t2:
        # NIOS stream 1 = public exam — nearest upcoming tma session
        for x in sess:
            if x.get("tma", True) and (x.get("id") or "").startswith("oct"):
                return x.get("id") or ""
        for x in sess:
            if x.get("tma", True):
                return x.get("id") or ""
    return ""


def _stream_for_session(db, session_id):
    try:
        from syllabus_routes import _sessions
        for x in _sessions(db):
            if (x.get("id") or "") == session_id:
                return "1" if x.get("tma", True) else "2"
    except Exception:
        pass
    return ""


def _apply_portal_exam_info(sp, st, db=None):
    """Class manager se exam session / stream / NIOS ref copy karo.
    RULE: kabhi bhi empty value se overwrite nahi — jo class manager
    na bheje wo portal ka apna value bana rahe."""
    if not isinstance(st, dict):
        return
    # session: pehle direct id-ish keys, phir free-text ko map karke
    raw_sess = _portal_pick(st, _EXAM_KEY_CANDIDATES["session"])
    raw_stream = _portal_pick(st, _EXAM_KEY_CANDIDATES["stream"])
    ref = _portal_pick(st, _EXAM_KEY_CANDIDATES["ref"])
    # class manager ke 'Exam Session' field me aksar 'Stream 2' likha hota hai
    blob = " ".join(x for x in [raw_sess, raw_stream] if x)
    new_sid = ""
    if raw_sess and not re.search(r"stream", raw_sess, re.I):
        new_sid = _map_session_text(db, raw_sess) or (raw_sess[:30] if re.fullmatch(r"[a-z0-9_-]+", raw_sess, re.I) else "")
    if not new_sid and blob:
        new_sid = _map_session_text(db, blob)
    if new_sid:
        sp.exam_session = new_sid
        stv = _stream_for_session(db, new_sid)
        if stv:
            sp.exam_stream = stv
    elif raw_stream:
        m = re.search(r"([1-4])", raw_stream)
        if m:
            sp.exam_stream = m.group(1)
    if ref:
        sp.nios_ref = ref.upper()[:40]


def _sync_one_from_portal(sp, db):
    """Ek student ko portal par check karo. True agar mvs_portal me transfer hua."""
    from ext_materials import portal_fetch_student
    if not sp.phone:
        return False
    st = portal_fetch_student(sp.phone)
    if not st or not st.get("unlocked"):
        return False
    sp.source = "mvs_portal"                       # priority: portal jeet-ta hai
    if st.get("class_level"):
        sp.class_level = st["class_level"]
    if st.get("subjects"):
        # class manager ke raw naam (PHYSICS / SCIENCE / DATA ENTRY) official
        # NIOS naam pe canonical karo — duplicate subjects kabhi nahi banenge
        sp.subjects = _SR.canon_list(st["subjects"], sp.class_level) if _SR else st["subjects"]
    if st.get("medium"):
        sp.medium = st["medium"]
    if st.get("name") and sp.user and (not sp.user.name or sp.user.name.startswith("Student ")):
        sp.user.name = st["name"]
    _apply_portal_exam_info(sp, st, db)
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
    # existing MVS Portal students: class manager se exam info REFRESH karo
    # (galat auto-guess stream/session repair ho jata hai; empty kabhi overwrite nahi)
    refreshed = 0
    portal_students = db.query(_SP).filter(
        _SP.source == "mvs_portal", _SP.phone.isnot(None)).limit(limit).all()
    for sp in portal_students:
        try:
            from ext_materials import portal_fetch_student
            st = portal_fetch_student(sp.phone)
            if st and st.get("unlocked"):
                before = (sp.exam_session, sp.exam_stream, sp.nios_ref)
                _apply_portal_exam_info(sp, st, db)
                if (sp.exam_session, sp.exam_stream, sp.nios_ref) != before:
                    refreshed += 1
        except Exception:
            continue
    db.commit()
    return {"checked": len(app_students), "moved": len(moved), "students": moved[:200],
            "exam_refreshed": refreshed,
            "message": f"{len(moved)} student(s) moved from MVS App to MVS Portal. "
                       f"{refreshed} portal student(s) exam info refreshed."}


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

@router.get("/whatsapp/config")
def whatsapp_get_config(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Combirds/WhatsApp config (portal me set kiya hua) — API key masked."""
    from models import AppSetting
    keys = ["wa_api_url", "wa_api_key", "wa_format", "wa_welcome",
            "wa_announce", "wa_welcome_msg", "wa_lang", "wa_link", "wa_sender"]
    rows = {r.key: (r.value or "") for r in
            db.query(AppSetting).filter(AppSetting.key.in_(keys)).all()}
    key = rows.pop("wa_api_key", "")
    rows["wa_api_key_set"] = bool(key)
    rows["wa_api_key_masked"] = (("\u2022" * max(0, len(key) - 4) + key[-4:]) if key else "")
    return rows


@router.post("/whatsapp/config")
def whatsapp_set_config(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AppSetting
    allowed = ["wa_api_url", "wa_api_key", "wa_format", "wa_welcome",
               "wa_announce", "wa_welcome_msg", "wa_lang", "wa_link", "wa_sender"]
    for k in allowed:
        if k not in (payload or {}):
            continue
        v = (payload.get(k) or "").strip()
        if k == "wa_api_key" and not v:
            continue   # blank -> purani key ko preserve karo
        row = db.query(AppSetting).filter(AppSetting.key == k).first()
        if not row:
            db.add(AppSetting(key=k, value=v))
        else:
            row.value = v
    db.commit()
    return {"ok": True}


@router.post("/whatsapp/announce")
def whatsapp_announce(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Announcement (Template 2) — custom message sabhi/selected MVS App students ko."""
    import whatsapp as W
    from models import StudentProfile as _SP
    if not W.is_configured():
        raise HTTPException(status_code=503,
                            detail="WhatsApp not configured: " + ", ".join(W.missing()))
    msg = (payload.get("message") or "").strip().replace("\n", " ").replace("\r", " ")
    if not msg:
        raise HTTPException(status_code=400, detail="Message is required")
    q = db.query(_SP).filter(_SP.phone.isnot(None),
                             ((_SP.source == "mvs_app") | (_SP.source.is_(None))))
    batch = (payload.get("batch") or "").strip()
    if batch:
        q = q.filter(_SP.batch_name == batch)
    ids = payload.get("profile_ids")
    if ids:
        q = q.filter(_SP.id.in_(ids))
    students = q.limit(int(payload.get("limit") or 500)).all()
    sent, failed = 0, []
    for sp in students:
        if (getattr(sp, "source", None) or "mvs_app") == "mvs_portal":
            continue
        name = sp.user.name if sp.user else "Student"
        ok, detail = W.send_announce(sp.phone, name, msg)
        if ok:
            sent += 1
        else:
            failed.append({"name": name, "phone": sp.phone, "error": detail[:120]})
    return {"sent": sent, "failed": len(failed), "errors": failed[:25],
            "message": f"{sent} sent, {len(failed)} failed."}


# ==================================================================
#  SESSION DEADLINES (batch / subject wise)
#  Timetable auto-shift in dates se aage nahi jaata. Priority:
#  subject+class > subject > batch > global > default (10 Sept)
# ==================================================================
@router.get("/session-deadlines")
def list_session_deadlines(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import SessionDeadline as _SD
    from teacher_routes import _default_end
    rows = db.query(_SD).order_by(_SD.scope, _SD.key).all()
    return {
        "default": str(_default_end()),
        "deadlines": [{"id": r.id, "scope": r.scope, "key": r.key or "",
                       "end_date": str(r.end_date), "note": r.note or ""} for r in rows],
    }


@router.post("/session-deadlines")
def set_session_deadline(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import SessionDeadline as _SD
    from datetime import datetime as _dtx
    scope = (payload.get("scope") or "global").strip()
    if scope not in ("global", "batch", "subject"):
        raise HTTPException(status_code=400, detail="scope must be global, batch or subject")
    key = (payload.get("key") or "").strip()
    if scope != "global" and not key:
        raise HTTPException(status_code=400, detail="Please select a batch or subject")
    if scope == "global":
        key = ""
    try:
        end = _dtx.strptime(payload["end_date"], "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Please choose a valid end date")

    row = db.query(_SD).filter(_SD.scope == scope, _SD.key == key).first()
    if row:
        row.end_date = end
        row.note = (payload.get("note") or "").strip() or None
    else:
        db.add(_SD(scope=scope, key=key, end_date=end,
                   note=(payload.get("note") or "").strip() or None))
    db.commit()
    return {"message": "Deadline saved."}


@router.delete("/session-deadlines/{did}")
def delete_session_deadline(did: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import SessionDeadline as _SD
    row = db.query(_SD).filter(_SD.id == did).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.delete(row); db.commit()
    return {"message": "Deadline removed."}

# ==================================================================
#  APP REVIEWS (moderation)
# ==================================================================
@router.get("/app-reviews")
def list_app_reviews(status: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AppReview
    q = db.query(AppReview).order_by(AppReview.id.desc())
    if status:
        q = q.filter(AppReview.status == status)
    out = []
    for r in q.limit(300).all():
        out.append({"id": r.id, "rating": r.rating, "review": r.review or "",
                    "status": r.status, "admin_note": r.admin_note or "",
                    "student": r.student.user.name if r.student and r.student.user else "Student",
                    "phone": r.student.phone if r.student else "",
                    "batch": r.student.batch_name if r.student else "",
                    "created_at": str(r.created_at or "")})
    return out


@router.patch("/app-reviews/{rid}")
def action_app_review(rid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import AppReview, Notification
    from datetime import datetime as _dtx
    r = db.query(AppReview).filter(AppReview.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    action = (payload.get("action") or "").strip()
    note = (payload.get("note") or "").strip() or None
    if action not in ("approve", "resolve"):
        raise HTTPException(status_code=400, detail="action must be approve or resolve")
    r.status = "approved" if action == "approve" else "resolved"
    r.admin_note = note
    r.reviewed_at = _dtx.now()
    if r.student and r.student.user:
        if action == "approve":
            msg = "Your review has been approved! You can now post it on the Play Store straight from the portal."
        else:
            msg = "Your review has been addressed." + (f" Note: {note}" if note else "") + " You can update your review now."
        db.add(Notification(user_id=r.student.user.id, title="Your App Review",
                            message=msg, notif_type="app_review"))
    db.commit()
    return {"message": "Review " + ("approved." if action == "approve" else "marked as resolved.")}


@router.delete("/app-reviews/{rid}")
def delete_app_review(rid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """v86: admin review-log entry delete kar sakta hai."""
    from models import AppReview
    r = db.query(AppReview).filter(AppReview.id == rid).first()
    if not r:
        raise HTTPException(status_code=404, detail="Review not found")
    db.delete(r); db.commit()
    return {"message": "Review deleted."}

# ==================================================================
#  DANGER: RESET PORTAL DATA (category-wise)
#  Users (students/teachers/admins), profiles, subjects aur deadlines
#  SAFE rehte hain — sirf chuna hua content data delete hota hai.
# ==================================================================
RESET_CATEGORIES = {
    "timetable":     ["TimetableEntry"],
    "materials":     ["Material", "MaterialView"],
    "doubts":        ["Doubt"],
    "submissions":   ["DPPSubmission", "TestSubmission", "ExamAttempt", "ExamResult", "ExamView"],
    "progress":      ["LectureVerification", "StudentStats"],
    "notifications": ["Notification"],
    "reviews":       ["AppReview"],
}


@router.post("/reset-data")
def reset_portal_data(payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    if (payload or {}).get("confirm") != "RESET PORTAL DATA":
        raise HTTPException(status_code=400, detail='Type "RESET PORTAL DATA" to confirm.')
    cats = [c for c in (payload.get("categories") or []) if c in RESET_CATEGORIES]
    if not cats:
        raise HTTPException(status_code=400, detail="Select at least one category.")
    import models as M
    results, errors = {}, []
    for cat in cats:
        total = 0
        for cls_name in RESET_CATEGORIES[cat]:
            cls = getattr(M, cls_name, None)
            if cls is None:
                continue
            try:
                with db.begin_nested():
                    total += db.query(cls).delete(synchronize_session=False)
            except Exception as e:
                errors.append(f"{cls_name}: {type(e).__name__}")
        results[cat] = total
    db.commit()
    return {"message": "Portal data reset complete.", "deleted": results,
            "errors": errors[:6]}

# =====================================================================
# ADMIN: TEACHER ATTENDANCE + CONTRACTS (APPOINTMENT LETTERS) + PAYOUTS
# =====================================================================
def _teacher_name_map(db):
    from models import TeacherProfile
    out = {}
    for tp in db.query(TeacherProfile).all():
        out[tp.id] = tp.user.name if tp.user else f"Teacher {tp.id}"
    return out

@router.get("/attendance")
def admin_attendance_day(day: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek din ki attendance — saare active teachers, punch in/out + leave status ke saath."""
    from models import TeacherAttendance, TeacherProfile, User, TeacherLeave
    from teacher_routes import (_ist_now, _fmt_t, _att_hours, _ensure_geofence,
                                _policy_map, _policy_required, _policy_label,
                                _net_hours, _extra_hours)
    _ensure_geofence(db)
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date() if day else _ist_now().date()
    except Exception:
        d = _ist_now().date()
    rows = {a.teacher_id: a for a in db.query(TeacherAttendance).filter(TeacherAttendance.att_date == d).all()}
    lrows = {l.teacher_id: l for l in db.query(TeacherLeave).filter(
        TeacherLeave.status == "approved",
        TeacherLeave.start_date <= d, TeacherLeave.end_date >= d).all()}
    tps = db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all()
    pols = _policy_map(db, [tp.id for tp in tps])
    out = []
    for tp in tps:
        a = rows.get(tp.id)
        lv = lrows.get(tp.id)
        pol = pols.get(tp.id) or {}
        req = _policy_required(pol)
        status = "absent"
        if a and a.punch_in and a.punch_out:
            # Present (Short) bhi PRESENT hi hai — bas assigned hours se kam
            status = "done" if (_net_hours(a, pol) or 0) >= req else "short"
        elif a and a.punch_in:
            status = "working"
        elif lv:
            status = "leave"
        elif pol.get("disabled"):
            status = "offsite"        # v101: attendance disabled — target only, na present na absent
        out.append({"teacher_id": tp.id, "name": tp.user.name if tp.user else "",
                    "disabled": bool(pol.get("disabled")),
                    "punch_in": _fmt_t(a.punch_in) if a else None,
                    "punch_out": _fmt_t(a.punch_out) if a else None,
                    "in_dist": a.in_dist if a else None, "out_dist": a.out_dist if a else None,
                    "in_office": a.in_office if a else None, "out_office": a.out_office if a else None,
                    "leave_type": (lv.leave_type if lv else None),
                    "hours": _att_hours(a), "net_hours": _net_hours(a, pol),
                    "extra_hours": _extra_hours(a, pol), "required_hours": req,
                    "policy_label": _policy_label(pol), "work_type": pol.get("work_type"),
                    "mode": pol.get("mode"), "policy_set": bool(pol.get("configured")),
                    "status": status})
    out.sort(key=lambda x: (x["status"] in ("absent",), x["name"]))
    return {"date": str(d), "day": d.strftime("%A"), "teachers": out,
            "present": sum(1 for x in out if x["status"] in ("done", "short", "working", "leave")),
            "total": len(out)}

@router.get("/attendance/month")
def admin_attendance_month(month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Month summary — teacher-wise present / leave / short / absent + net & extra hours.
    Present = net hours (gap - lunch break) >= assigned required hours.
    Short day (required se kam) bhi PRESENT count hota hai. Extra hours sirf display —
    unka koi payout nahi. Approved leave alag count; bina punch/leave = AB."""
    from models import TeacherAttendance, TeacherProfile, User
    from teacher_routes import (_month_range, _ensure_geofence, _leave_days_map, _elapsed_days,
                                _ist_now, _policy_map, _policy_required, _policy_label,
                                _day_status, _net_hours, _extra_hours)
    _ensure_geofence(db)
    start, end = _month_range(month)
    rows = db.query(TeacherAttendance).filter(
        TeacherAttendance.att_date >= start, TeacherAttendance.att_date < end).all()
    by_tid = {}
    for a in rows:
        by_tid.setdefault(a.teacher_id, []).append(a)
    tps = db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all()
    pols = _policy_map(db, [tp.id for tp in tps])
    today = _ist_now().date()
    elapsed = _elapsed_days(start, end)
    out = []
    for tp in tps:
        pol = pols.get(tp.id) or {}
        req = _policy_required(pol)
        trows = by_tid.get(tp.id, [])
        full = sum(1 for a in trows if _day_status(a, pol) == "present")
        short = sum(1 for a in trows if _day_status(a, pol) == "short")
        present = full + short          # short day bhi PRESENT
        net = round(sum(_net_hours(a, pol) or 0 for a in trows), 1)
        extra = round(sum(_extra_hours(a, pol) for a in trows), 1)
        lvmap = _leave_days_map(db, tp.id, start, end)
        leave_days = round(sum(lvmap.values()), 1)
        leave_elapsed = round(sum(v for d, v in lvmap.items() if d <= today), 1)
        absent = 0 if pol.get("disabled") else max(0, round(elapsed - present - leave_elapsed))
        out.append({"teacher_id": tp.id, "name": tp.user.name if tp.user else "",
                    "disabled": bool(pol.get("disabled")),
                    "present_days": present, "full_days": full, "short_days": short,
                    "leave_days": leave_days, "absent_days": absent,
                    "total_hours": net, "extra_hours": extra, "required_hours": req,
                    "policy_label": _policy_label(pol), "work_type": pol.get("work_type"),
                    "mode": pol.get("mode"), "policy_set": bool(pol.get("configured"))})
    out.sort(key=lambda x: -x["present_days"])
    return {"month": start.strftime("%Y-%m"), "elapsed_days": elapsed, "teachers": out}

@router.get("/attendance/teacher/{tid}")
def admin_attendance_teacher(tid: int, month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek teacher ki date-wise attendance (admin detail view) + policy-aware status."""
    from models import TeacherAttendance
    from teacher_routes import (_month_range, _fmt_t, _att_hours, _ensure_geofence,
                                _leave_days_map, _policy_dict, _policy_required, _policy_label,
                                _day_status, _net_hours, _extra_hours)
    _ensure_geofence(db)
    start, end = _month_range(month)
    rows = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tid,
        TeacherAttendance.att_date >= start, TeacherAttendance.att_date < end
    ).order_by(TeacherAttendance.att_date.desc()).all()
    lvmap = _leave_days_map(db, tid, start, end)
    pol = _policy_dict(db, tid)
    req = _policy_required(pol)

    def _st(r):
        return _day_status(r, pol) if (r and (r.punch_in or r.punch_out)) else ""

    return {"month": start.strftime("%Y-%m"),
            "leave_dates": sorted(str(d) for d in lvmap.keys()),
            "policy": {**pol, "required": req, "label": _policy_label(pol)},
            "required_hours": req,
            "rows": [{"date": str(r.att_date), "day": r.att_date.strftime("%A"),
                      "punch_in": _fmt_t(r.punch_in), "punch_out": _fmt_t(r.punch_out),
                      "hours": _att_hours(r), "net_hours": _net_hours(r, pol),
                      "extra_hours": _extra_hours(r, pol),
                      "status": _st(r)} for r in rows]}


# ===== ADMIN: MANUAL PUNCH (teacher punch karna bhool gaya ho) =====
@router.post("/attendance/punch")
def admin_add_punch(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Teacher present tha par punch karna bhool gaya — admin kisi bhi date ke liye
    punch-in / punch-out time add kare. Policy ke hisaab se present/short apne aap
    calculate hoga; teacher ko notification jaati hai."""
    from models import TeacherAttendance, TeacherProfile, Notification
    from teacher_routes import _ist_now, _fmt_t, _ensure_geofence
    _ensure_geofence(db)
    tid = payload.get("teacher_id")
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first() if tid else None
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    try:
        d = datetime.strptime((payload.get("date") or "").strip(), "%Y-%m-%d").date() \
            if (payload.get("date") or "").strip() else _ist_now().date()
    except Exception:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD")
    def _hm(s):
        try:
            h, m = str(s or "").strip().split(":")
            h, m = int(h), int(m)
            assert 0 <= h <= 23 and 0 <= m <= 59
            return h, m
        except Exception:
            return None
    pin_raw, pout_raw = (payload.get("punch_in") or "").strip(), (payload.get("punch_out") or "").strip()
    pin, pout = _hm(pin_raw), _hm(pout_raw)
    if not pin:
        raise HTTPException(status_code=400, detail="Punch-in time is required (HH:MM)")
    if pout_raw and not pout:
        raise HTTPException(status_code=400, detail="Punch-out time must be HH:MM")
    if pin and pout and (pout[0] * 60 + pout[1]) <= (pin[0] * 60 + pin[1]):
        raise HTTPException(status_code=400, detail="Punch-out must be after punch-in")
    a = db.query(TeacherAttendance).filter(
        TeacherAttendance.teacher_id == tp.id, TeacherAttendance.att_date == d).first()
    if not a:
        a = TeacherAttendance(teacher_id=tp.id, att_date=d)
        db.add(a)
    a.punch_in = datetime.combine(d, datetime.min.time()).replace(hour=pin[0], minute=pin[1])
    a.punch_out = datetime.combine(d, datetime.min.time()).replace(hour=pout[0], minute=pout[1]) if pout else None
    a.in_office = a.in_office or "Admin entry"
    if pout:
        a.out_office = a.out_office or "Admin entry"
    if tp.user_id:
        db.add(Notification(
            user_id=tp.user_id, title="Punch Added by Admin",
            message=(f"Admin ne aapki {d.strftime('%d %b %Y')} ki punch entry add ki hai — "
                     f"In {_fmt_t(a.punch_in)}"
                     + (f", Out {_fmt_t(a.punch_out)}" if a.punch_out else "")
                     + ". Galat lage to admin se sampark karein."),
            notif_type="attendance"))
    db.commit()
    return {"ok": True, "message": "Punch saved.",
            "date": str(d), "punch_in": _fmt_t(a.punch_in), "punch_out": _fmt_t(a.punch_out)}

# ===== SMART WORK TIMING (per-teacher policy — admin set karta hai) =====
@router.get("/attendance/policies")
def admin_work_policies(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Saare active teachers ki work-timing policy (unset = default 8h/day full time)."""
    from models import TeacherProfile, User
    from teacher_routes import _policy_map, _policy_required, _policy_label
    tps = db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all()
    pols = _policy_map(db, [tp.id for tp in tps])
    out = []
    for tp in sorted(tps, key=lambda t: (t.user.name if t.user else "")):
        pol = pols.get(tp.id) or {}
        subs = tp.subjects if isinstance(tp.subjects, list) else []
        subs = [s if isinstance(s, str) else (s.get("name") or "") for s in subs]
        out.append({"teacher_id": tp.id, "name": tp.user.name if tp.user else "",
                    "subjects": [s for s in subs if s],
                    "policy": {**pol, "required": _policy_required(pol), "label": _policy_label(pol)}})
    return {"teachers": out}

@router.post("/attendance/policy")
def admin_set_work_policy(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek teacher ki smart timing save karo:
    work_type: full_time (lunch break skip) | part_time (no break);
    mode: fixed (entry/exit pakka) | hours (sirf daily hours) | flexible (min 1h).
    Validations: required >= 1h; fixed me exit > entry; break sirf full_time."""
    from models import TeacherProfile, TeacherWorkPolicy
    from teacher_routes import (WORK_TYPES, POLICY_MODES, MAX_WORK_HOURS, MIN_PRESENT_HOURS,
                                _parse_hhmm, _policy_from_row, _policy_required, _policy_label,
                                _ist_now)
    tid = payload.get("teacher_id")
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first() if tid else None
    if not tp:
        raise HTTPException(404, "Teacher not found")
    wt = (payload.get("work_type") or "full_time").strip().lower()
    if wt not in WORK_TYPES:
        raise HTTPException(400, "Work type must be full_time or part_time")
    mode = (payload.get("mode") or "hours").strip().lower()
    if mode not in POLICY_MODES:
        raise HTTPException(400, "Mode must be fixed, hours or flexible")
    try:
        brk = int(payload.get("break_minutes") or 0)
    except Exception:
        raise HTTPException(400, "Break minutes must be a number")
    if brk < 0 or brk > 240:
        raise HTTPException(400, "Break must be between 0 and 240 minutes")
    if wt != "full_time":
        brk = 0                                  # lunch break sirf full-time me
    entry = (payload.get("entry_time") or "").strip()
    exit_ = (payload.get("exit_time") or "").strip()
    try:
        req_h = float(payload.get("required_hours") or 0)
    except Exception:
        raise HTTPException(400, "Required hours must be a number")
    if mode == "fixed":
        en, ex = _parse_hhmm(entry), _parse_hhmm(exit_)
        if en is None or ex is None:
            raise HTTPException(400, "Fixed mode needs valid entry and exit times (HH:MM)")
        if ex <= en:
            raise HTTPException(400, "Exit time must be after entry time")
        span_h = (ex - en) / 60.0
        if span_h > MAX_WORK_HOURS:
            raise HTTPException(400, "Entry-exit span cannot exceed %d hours" % MAX_WORK_HOURS)
        if brk >= (ex - en):
            raise HTTPException(400, "Break cannot be longer than the entry-exit span")
        req_h = round(span_h - brk / 60.0, 2)    # fixed mode me required auto = span - break
        if req_h < MIN_PRESENT_HOURS:
            raise HTTPException(400, "Fixed timing must allow at least 1 working hour after the break")
    elif mode == "hours":
        if req_h < MIN_PRESENT_HOURS or req_h > MAX_WORK_HOURS:
            raise HTTPException(400, "Required hours must be between 1 and %d" % MAX_WORK_HOURS)
        entry, exit_ = "", ""
    else:                                        # flexible — sirf minimum 1h
        req_h = MIN_PRESENT_HOURS
        entry, exit_ = "", ""
    disabled = bool(payload.get("disabled"))      # v101: attendance off — target only
    p = db.query(TeacherWorkPolicy).filter(TeacherWorkPolicy.teacher_id == tp.id).first()
    if not p:
        p = TeacherWorkPolicy(teacher_id=tp.id)
        db.add(p)
    p.work_type, p.mode = wt, mode
    p.required_hours = req_h
    p.entry_time, p.exit_time = entry, exit_
    p.break_minutes = brk
    p.disabled = disabled
    p.updated_at = _ist_now()
    db.commit()
    pol = _policy_from_row(p, tp.id)
    msg = "Work timing saved for %s — %s" % (tp.user.name if tp.user else "teacher", _policy_label(pol))
    if disabled:
        msg += " · attendance disabled (target only)"
    return {"message": msg,
            "teacher_id": tp.id,
            "policy": {**pol, "required": _policy_required(pol), "label": _policy_label(pol)}}


# ===== LEAVE REQUESTS (admin review) =====
@router.get("/leaves")
def admin_leaves(status: str = "", teacher_id: int = 0,
                 db: Session = Depends(get_db), _=Depends(get_admin)):
    """Saare leave requests — default pending pehle, teacher ka naam ke saath."""
    from models import TeacherLeave, TeacherProfile, User
    q = db.query(TeacherLeave)
    if status:
        q = q.filter(TeacherLeave.status == status)
    if teacher_id:
        q = q.filter(TeacherLeave.teacher_id == teacher_id)
    rows = q.order_by(TeacherLeave.created_at.desc()).limit(200).all()
    names = {}
    for tp in db.query(TeacherProfile).all():
        u = db.query(User).filter(User.id == tp.user_id).first()
        names[tp.id] = u.name if u else f"Teacher #{tp.id}"
    pend = db.query(TeacherLeave).filter(TeacherLeave.status == "pending").count()
    return {"pending_count": pend, "leaves": [{
        "id": r.id, "teacher_id": r.teacher_id,
        "teacher": names.get(r.teacher_id, f"Teacher #{r.teacher_id}"),
        "start_date": str(r.start_date), "end_date": str(r.end_date),
        "days": (r.end_date - r.start_date).days + 1,
        "leave_type": r.leave_type, "reason": r.reason or "",
        "status": r.status, "admin_remark": r.admin_remark or "",
        "paid": bool(getattr(r, "paid", False)),
        "reviewed_at": r.reviewed_at.strftime("%d %b %Y, %I:%M %p") if r.reviewed_at else "",
        "created_at": r.created_at.strftime("%d %b %Y") if r.created_at else "",
    } for r in rows]}


@router.post("/leaves/{lid}/review")
def admin_leave_review(lid: int, payload: dict = Body(default={}),
                       db: Session = Depends(get_db), _=Depends(get_admin)):
    """Approve / reject leave. v86: approve karte waqt PAID/UNPAID select hota hai
    (paid => koi salary deduction nahi). Approved FULL leave ki us-din ki classes
    apne aap aage move ho jaati hain (reschedule-approval me NAHI jaati, teacher
    ke reschedule count/salary pe asar NAHI) aur students ko teacher ki photo ke
    saath notification jaati hai."""
    from models import TeacherLeave, TeacherProfile, Notification
    from teacher_routes import _ist_now, _ensure_v86, _auto_move_leave_classes, _notify_class_moved
    _ensure_v86(db)
    lv = db.query(TeacherLeave).filter(TeacherLeave.id == lid).first()
    if not lv:
        raise HTTPException(status_code=404, detail="Leave request not found")
    if lv.status != "pending":
        raise HTTPException(status_code=400, detail=f"This request is already {lv.status}")
    action = (payload.get("action") or "").strip().lower()
    if action not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="Action must be 'approved' or 'rejected'")
    lv.status = action
    lv.admin_remark = (payload.get("remark") or "").strip()
    lv.paid = bool(payload.get("paid", False)) if action == "approved" else False
    lv.reviewed_at = _ist_now()
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == lv.teacher_id).first()
    moved = []
    if action == "approved" and tp:
        try:
            moved = _auto_move_leave_classes(db, tp, lv)
        except Exception:
            moved = []
    if tp and tp.user_id:
        rng = f'{lv.start_date.strftime("%d %b")} - {lv.end_date.strftime("%d %b")}'
        if action == "approved":
            tmsg = (f"Your leave ({rng}) has been approved as "
                    f"{'PAID — iski koi salary deduction nahi hogi' if lv.paid else 'UNPAID — per-day rate se deduction hogi'}"
                    + (f": {lv.admin_remark}" if lv.admin_remark else "."))
            if moved:
                tmsg += (f" Leave ke dinon ki {len(moved)} class(es) apne aap aage move ho gayi hain "
                         f"(ye aapke reschedule count me nahi judegi). Time table check karein.")
            db.add(Notification(user_id=tp.user_id, title="✅ Leave Approved",
                                message=tmsg, notif_type="leave"))
        else:
            db.add(Notification(user_id=tp.user_id, title="❌ Leave Rejected",
                                message=f"Your leave request ({rng}) was rejected"
                                        + (f": {lv.admin_remark}" if lv.admin_remark else "."),
                                notif_type="leave"))
    if moved:
        tname = tp.user.name if tp.user else "Your teacher"
        _notify_class_moved(db, tp, moved, "Class Rescheduled — ",
                            f"{tname} is on approved leave, so the class has been moved.")
    db.commit()
    return {"ok": True, "status": lv.status, "paid": bool(lv.paid),
            "classes_moved": len(moved)}

# ===== CONTRACTS =====
@router.get("/teacher/{tid}/contract")
def admin_get_contract(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import TeacherContract, TeacherProfile
    from teacher_routes import _contract_out
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tid).first()
    if not c:
        return {"exists": False, "teacher_name": tp.user.name if tp.user else ""}
    return _contract_out(c, tp.user.name if tp.user else "")

@router.post("/teacher/{tid}/contract")
def admin_set_contract(tid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Contract create/update. require_reaccept=True bhejo to teacher ko letter
    dobara accept karna padega (terms badalne pe)."""
    from models import TeacherContract, TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    from teacher_routes import _ensure_contract_columns as _ecc
    _ecc(db)   # pehle migration (rollback pending work uda sakta hai, isliye sabse pehle)
    c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tid).first()
    creating = c is None
    if creating:
        c = TeacherContract(teacher_id=tid)
        db.add(c)
    _old_gross = (c.base_salary or 0) + (c.allowances or 0)
    if "designation" in payload:
        c.designation = (payload.get("designation") or "Subject Teacher").strip() or "Subject Teacher"
    if "joining_date" in payload:
        d = (payload.get("joining_date") or "").strip()
        if d:
            try:
                c.joining_date = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                raise HTTPException(status_code=400, detail="Joining date must be in YYYY-MM-DD format")
        else:
            c.joining_date = None
    def _num(k, cur):
        try:
            return max(0, int(payload.get(k)))
        except Exception:
            return cur
    from teacher_routes import _salary_breakup, DEFAULT_CONTRACT_RULES
    if "gross_salary" in payload:
        # AUTO MODE (Faculty Service Agreement): sirf gross do - breakup, rules,
        # working days sab agreement ke % se khud set hote hain.
        gross = _num("gross_salary", 0)
        if gross <= 0:
            raise HTTPException(status_code=400, detail="Gross salary must be greater than 0")
        c.base_salary = gross
        c.allowances = 0
        c.working_days = 26
        for k, v in _salary_breakup(gross).items():
            setattr(c, k, v)
        if not (payload.get("rules_text") or "").strip():
            c.rules_text = DEFAULT_CONTRACT_RULES
    else:
        if "base_salary" in payload:
            c.base_salary = _num("base_salary", c.base_salary or 0)
        if "allowances" in payload:
            c.allowances = _num("allowances", c.allowances or 0)
        if "working_days" in payload:
            wd = _num("working_days", c.working_days or 26)
            c.working_days = min(31, max(1, wd))
    if "rules_text" in payload and (payload.get("rules_text") or "").strip():
        c.rules_text = (payload.get("rules_text") or "").strip()
    _new_gross = (c.base_salary or 0) + (c.allowances or 0)
    if creating or payload.get("require_reaccept") or _old_gross != _new_gross:
        c.accepted = False
        c.accepted_at = None
        c.signature_name = None
    db.commit()
    return {"message": "Contract saved. The teacher will see the appointment letter on next portal open."
            if (creating or payload.get("require_reaccept")) else "Contract saved."}

# ===== PAYOUTS =====
@router.get("/payouts")
def admin_payouts(month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Month ke liye saare teachers ka payout overview (contract ho ya na ho)."""
    from models import TeacherProfile, User, TeacherContract
    from teacher_routes import compute_payout, _month_range
    start, _e = _month_range(month)
    mk = start.strftime("%Y-%m")
    out = []
    for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
        name = tp.user.name if tp.user else ""
        p = compute_payout(db, tp.id, mk)
        if p:
            p.update({"teacher_id": tp.id, "name": name, "configured": True})
            out.append(p)
        else:
            out.append({"teacher_id": tp.id, "name": name, "configured": False, "month": mk})
    out.sort(key=lambda x: (not x["configured"], x["name"]))
    total = sum(x.get("net_payout", 0) for x in out if x.get("configured"))
    return {"month": mk, "teachers": out, "total_net": total}

@router.get("/teacher/{tid}/payout")
def admin_teacher_payout(tid: int, month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    from teacher_routes import compute_payout
    p = compute_payout(db, tid, month)
    if not p:
        return {"exists": False}
    p["exists"] = True
    return p

# ===== EARNINGS MODEL (v80) — appointment-letter rule, admin controls =====
@router.get("/earnings")
def admin_earnings(month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Saare active teachers ka earnings overview (appointment-letter model)."""
    from teacher_routes import earnings_payload, _ist_now
    month = (month or "").strip() or _ist_now().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="Invalid month (use YYYY-MM).")
    out = []
    for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
        out.append(earnings_payload(db, tp, month))
    out.sort(key=lambda x: x["teacher"]["name"])
    # v104: target-only teachers ka payment amount nahi dikhata — sirf estimated %.
    # Unke amounts ₹ totals ka hissa nahi hote.
    total = sum(x["earnings"]["net_payable"] for x in out if not x.get("target_only"))
    tonly = sum(1 for x in out if x.get("target_only"))
    avg = round(sum(x["earnings"]["perf_score"] for x in out) / len(out)) if out else 0
    return {"month": month, "teachers": out, "total_net": total, "avg_perf": avg,
            "target_only_count": tonly}


@router.get("/earnings/teacher/{tid}")
def admin_earnings_teacher(tid: int, month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek teacher ka full earnings payload (slip / letter / detail modal ke liye)."""
    from teacher_routes import earnings_payload, _ist_now
    month = (month or "").strip() or _ist_now().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise HTTPException(status_code=400, detail="Invalid month (use YYYY-MM).")
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    return earnings_payload(db, tp, month)


@router.get("/earnings/configs")
def admin_earnings_configs(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Har teacher ki pay structure + monthly targets (editor list)."""
    from teacher_routes import get_pay_config, EARNINGS_PAY_FIELDS, EARNINGS_TARGET_FIELDS
    out = []
    for tp in db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(User.is_active == True).all():
        cfg = get_pay_config(db, tp.id)
        row = {"teacher_id": tp.id, "name": tp.user.name if tp.user else "",
               "subjects": tp.subjects or [], "saved": cfg.id is not None,
               "designation": cfg.designation or "", "department": cfg.department or "",
               "employee_code": cfg.employee_code or "", "bank_name": cfg.bank_name or "",
               "account_no": cfg.account_no or "", "ifsc": cfg.ifsc or ""}
        for k in EARNINGS_PAY_FIELDS + EARNINGS_TARGET_FIELDS:
            row[k] = int(getattr(cfg, k) or 0)
        out.append(row)
    out.sort(key=lambda x: x["name"])
    return {"configs": out}


@router.post("/earnings/config")
def admin_earnings_config_save(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Teacher ki pay structure + targets + bank details save karo (upsert)."""
    from models import TeacherPayConfig
    from teacher_routes import EARNINGS_PAY_FIELDS, EARNINGS_TARGET_FIELDS, EARNINGS_DEFAULTS
    tid = int(payload.get("teacher_id") or 0)
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    cfg = db.query(TeacherPayConfig).filter(TeacherPayConfig.teacher_id == tid).first()
    if not cfg:
        cfg = TeacherPayConfig(teacher_id=tid, **EARNINGS_DEFAULTS)
        db.add(cfg)

    def _num(key, cur):
        v = payload.get(key, None)
        if v is None or v == "":
            return cur
        try:
            v = int(v)
        except Exception:
            raise HTTPException(status_code=400, detail="%s must be a number" % key)
        if v < 0:
            raise HTTPException(status_code=400, detail="%s cannot be negative" % key)
        return v

    for k in EARNINGS_PAY_FIELDS + EARNINGS_TARGET_FIELDS:
        cur = getattr(cfg, k)
        setattr(cfg, k, _num(k, int(cur if cur is not None else EARNINGS_DEFAULTS[k])))
    for k in ("designation", "department", "employee_code", "bank_name", "account_no", "ifsc"):
        if k in payload and payload[k] is not None:
            setattr(cfg, k, str(payload[k]).strip()[:120])
    # v95: editable target names (core 4) + custom extra targets (add/delete)
    if isinstance(payload.get("target_labels"), dict):
        lab = {}
        for k in ("tests", "videos", "live", "shorts"):
            v = str(payload["target_labels"].get(k) or "").strip()[:60]
            if v:
                lab[k] = v
        cfg.target_labels = lab
    if isinstance(payload.get("custom_targets"), list):
        out = []
        for c in payload["custom_targets"][:12]:
            if not isinstance(c, dict):
                continue
            nm = str(c.get("name") or "").strip()[:60]
            if not nm:
                continue
            try:
                cnt = max(0, min(999, int(c.get("count") or 0)))
            except Exception:
                cnt = 0
            out.append({"name": nm, "count": cnt})
        cfg.custom_targets = out
    db.commit()
    return {"message": "Pay structure saved for %s." % (tp.user.name if tp.user else "teacher")}

# ===== PERFORMANCE PAYOUT (template, approvals, finalize) =====
@router.get("/teacher/{tid}/payout-template")
def admin_get_payout_template(tid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Teacher ki monthly responsibility template (target + weight%)."""
    from teacher_routes import _perf_seed
    from models import PayoutTemplate
    _perf_seed(db, tid)
    rows = db.query(PayoutTemplate).filter(PayoutTemplate.teacher_id == tid).order_by(PayoutTemplate.sort, PayoutTemplate.id).all()
    return {"template": [{"id": r.id, "key": r.key, "label": r.label, "target": r.target or 0,
                          "weight": r.weight_pct or 0, "source": r.source} for r in rows],
            "weight_sum": sum(r.weight_pct or 0 for r in rows)}

@router.put("/teacher/{tid}/payout-template")
def admin_set_payout_template(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Template save: [{key,label,target,weight,source}]. Weight sum 100 hona
    chahiye (0-target wali categories calculation se baahar ho jaati hain)."""
    from teacher_routes import _perf_seed
    from models import PayoutTemplate
    _perf_seed(db, tid)
    items = payload.get("template") or []
    if not items:
        raise HTTPException(400, "Template khaali hai")
    wsum = sum(float(i.get("weight") or 0) for i in items)
    if abs(wsum - 100) > 0.01:
        raise HTTPException(400, "Weight ka total 100%% hona chahiye (abhi %.1f%%)" % wsum)
    rows = {r.key: r for r in db.query(PayoutTemplate).filter(PayoutTemplate.teacher_id == tid).all()}
    for i, it in enumerate(items):
        key = (it.get("key") or "").strip()
        if not key:
            continue
        r = rows.get(key)
        if not r:
            r = PayoutTemplate(teacher_id=tid, key=key)
            db.add(r)
        r.label = (it.get("label") or r.label or key)[:80]
        r.target = max(0, int(it.get("target") or 0))
        r.weight_pct = max(0.0, float(it.get("weight") or 0))
        r.source = "auto" if it.get("source") == "auto" else "manual"
        r.sort = i
    db.commit()
    return {"message": "Template saved - every month will now be calculated from it"}

@router.get("/payout-approvals")
def admin_payout_approvals(month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Saare teachers ke pending manual tasks (approve/reject queue)."""
    from models import PayoutTask, TeacherProfile, User
    from teacher_routes import _month_range
    start, _e = _month_range(month)
    mk = start.strftime("%Y-%m")
    rows = db.query(PayoutTask).filter(PayoutTask.status == "pending", PayoutTask.month == mk).order_by(PayoutTask.created_at).all()
    out = []
    for t in rows:
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == t.teacher_id).first()
        out.append({"id": t.id, "teacher_id": t.teacher_id,
                    "teacher_name": (tp.user.name if tp and tp.user else ""),
                    "key": t.key, "title": t.title, "note": t.note or "",
                    "done_date": str(t.done_date) if t.done_date else None,
                    "created_at": t.created_at.strftime("%d %b, %I:%M %p") if t.created_at else ""})
    return {"month": mk, "pending": out}

@router.post("/payout-task/{task_id}/approve")
def admin_approve_payout_task(task_id: int, db: Session = Depends(get_db), current_user=Depends(get_admin)):
    from models import PayoutTask
    t = db.query(PayoutTask).filter(PayoutTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task nahi mila")
    t.status = "approved"
    t.approved_by = getattr(current_user, "name", "Admin") or "Admin"
    t.approved_at = datetime.utcnow()
    db.commit()
    return {"message": "Approved - it now counts"}

@router.post("/payout-task/{task_id}/reject")
def admin_reject_payout_task(task_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import PayoutTask
    t = db.query(PayoutTask).filter(PayoutTask.id == task_id).first()
    if not t:
        raise HTTPException(404, "Task nahi mila")
    t.status = "rejected"
    db.commit()
    return {"message": "Rejected"}

@router.post("/teacher/{tid}/payout-missed-class")
def admin_flag_missed_class(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Scheduled class nahi hui -> us month ke live_class count se ghatao."""
    from models import PayoutTask, TimetableEntry
    from teacher_routes import _month_range
    eid = int(payload.get("entry_id") or 0)
    e = db.query(TimetableEntry).filter(TimetableEntry.id == eid, TimetableEntry.teacher_id == tid).first()
    if not e:
        raise HTTPException(404, "Class entry nahi mili")
    mk = e.entry_date.strftime("%Y-%m") if e.entry_date else ""
    if not mk:
        raise HTTPException(400, "Entry ka date nahi hai")
    flag = (payload.get("missed") is True) or (str(payload.get("missed")) == "true")
    ex = db.query(PayoutTask).filter(PayoutTask.teacher_id == tid, PayoutTask.key == "live_class",
                                     PayoutTask.status == "missed", PayoutTask.ref_id == eid).first()
    if flag and not ex:
        db.add(PayoutTask(teacher_id=tid, month=mk, key="live_class",
                          title="Class not held: %s (%s)" % (e.chapter or e.subject, e.entry_date),
                          status="missed", ref_id=eid, done_date=e.entry_date,
                          note=(payload.get("note") or "")[:300]))
        db.commit()
        return {"message": "Marked as missed - removed from this month's count"}
    if not flag and ex:
        db.delete(ex); db.commit()
        return {"message": "Missed flag removed"}
    return {"message": "No change"}

@router.get("/teacher/{tid}/payout-classes")
def admin_payout_classes(tid: int, month: str = "", db: Session = Depends(get_db), _=Depends(get_admin)):
    """Us month ki scheduled classes + missed flags (detail modal ke liye)."""
    from models import TimetableEntry, PayoutTask
    from teacher_routes import _month_range
    start, end = _month_range(month)
    mk = start.strftime("%Y-%m")
    entries = db.query(TimetableEntry).filter(
        TimetableEntry.teacher_id == tid,
        TimetableEntry.entry_date >= start, TimetableEntry.entry_date < end,
        TimetableEntry.entry_type == "chapter", TimetableEntry.status == "approved"
    ).order_by(TimetableEntry.entry_date).all()
    missed = {x.ref_id for x in db.query(PayoutTask).filter(
        PayoutTask.teacher_id == tid, PayoutTask.key == "live_class", PayoutTask.status == "missed").all()}
    return {"month": mk, "classes": [
        {"id": e.id, "date": str(e.entry_date), "subject": e.subject,
         "chapter": e.chapter or "", "missed": e.id in missed} for e in entries]}

@router.post("/teacher/{tid}/payout-finalize")
def admin_finalize_payout(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Month lock: poora breakdown snapshot me freeze. Paid mark alag action."""
    import json as _json
    from models import PayoutMonth
    from teacher_routes import compute_payout, _month_range
    mk = _month_range(payload.get("month") or "")[0].strftime("%Y-%m")
    p = compute_payout(db, tid, mk)
    if not p:
        raise HTTPException(400, "Is teacher ka payout configured nahi hai")
    rec = db.query(PayoutMonth).filter(PayoutMonth.teacher_id == tid, PayoutMonth.month == mk).first()
    if not rec:
        rec = PayoutMonth(teacher_id=tid, month=mk)
        db.add(rec)
    rec.snapshot = _json.dumps(p, default=str)
    rec.status = "finalized"
    rec.finalized_at = datetime.utcnow()
    db.commit()
    return {"message": "Month finalized - snapshot saved"}

@router.post("/teacher/{tid}/payout-paid")
def admin_payout_paid(tid: int, payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import PayoutMonth
    from teacher_routes import _month_range
    mk = _month_range(payload.get("month") or "")[0].strftime("%Y-%m")
    rec = db.query(PayoutMonth).filter(PayoutMonth.teacher_id == tid, PayoutMonth.month == mk).first()
    if not rec:
        raise HTTPException(400, "Pehle month finalize karo")
    rec.status = "paid"
    rec.paid_at = datetime.utcnow()
    db.commit()
    return {"message": "Marked as paid"}

# ===== OFFICE LOCATION (punch geofence) =====
@router.get("/office-location")
def admin_get_office(db: Session = Depends(get_db), _=Depends(get_admin)):
    from teacher_routes import _ensure_geofence, _office_list, _office_ips
    _ensure_geofence(db)
    offices = _office_list(db)
    unknown = []
    try:
        from models import AppSetting
        import json as _json
        row = db.query(AppSetting).filter(AppSetting.key == "unknown_ips").first()
        data = _json.loads(row.value) if row and row.value else []
        have = set(_office_ips(db))
        unknown = [x for x in data if isinstance(x, dict) and x.get("ip") and x["ip"] not in have][:10]
    except Exception:
        unknown = []
    return {"active": bool(offices),
            "offices": [{"name": o["name"], "lat": o["lat"], "lng": o["lng"], "radius": int(o["radius"])} for o in offices],
            "ips": _office_ips(db), "unknown_ips": unknown}

@router.get("/my-ip")
def admin_my_ip(request: Request, _=Depends(get_admin)):
    """Admin ke current device ka public IP — office WiFi add karne ke liye."""
    from teacher_routes import _client_ip
    return {"ip": _client_ip(request)}

@router.post("/office-location")
def admin_set_office(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Office branches (multi-location) save karo. Body: {'offices':[{name,lat,lng,radius},...]}
    ya {'clear': true} (geofence band). Save ke baad teachers sirf kisi branch ke radius me se hi punch kar payenge."""
    from teacher_routes import _ensure_geofence
    from models import AppSetting
    import json as _json
    _ensure_geofence(db)
    def _set(k, v):
        row = db.query(AppSetting).filter(AppSetting.key == k).first()
        if not row:
            row = AppSetting(key=k); db.add(row)
        row.value = str(v)
    if payload.get("clear"):
        _set("offices", ""); _set("office_lat", ""); _set("office_lng", ""); _set("office_ips", "")
        db.commit()
        return {"message": "Geofence turned off - punching is now allowed from anywhere"}
    # office WiFi/broadband IPs (optional) — in se aaye punch GPS ke bina allowed
    # v121: IP count limit hata di (19+ office WiFis) — sirf format validate hota hai
    import re as _re
    raw_ips = payload.get("ips") or []
    if not isinstance(raw_ips, list):
        raise HTTPException(status_code=400, detail="WiFi IPs list bhejo")
    clean_ips = []
    for ip in raw_ips:
        ip = str(ip).strip()
        if not ip:
            continue
        if not (_re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or ":" in ip):
            raise HTTPException(status_code=400, detail=f"Invalid IP address: {ip}")
        if ip not in clean_ips:
            clean_ips.append(ip)
    _set("office_ips", _json.dumps(clean_ips))
    raw = payload.get("offices")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="Kam se kam 1 office branch bhejo")
    if len(raw) > 6:
        raise HTTPException(status_code=400, detail="You can add at most 6 branches")
    clean, names = [], set()
    for o in raw:
        name = str((o or {}).get("name") or "").strip()[:80]
        if not name:
            raise HTTPException(status_code=400, detail="Every branch needs a name")
        if name.lower() in names:
            raise HTTPException(status_code=400, detail=f"Branch name '{name}' is used twice - use distinct names")
        names.add(name.lower())
        try:
            lat = float(o.get("lat")); lng = float(o.get("lng"))
            radius = int(o.get("radius") or 30)
        except Exception:
            raise HTTPException(status_code=400, detail=f"'{name}' has invalid latitude/longitude")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(status_code=400, detail=f"'{name}' latitude/longitude is out of range")
        if radius < 5 or radius > 500:
            raise HTTPException(status_code=400, detail=f"Keep '{name}' radius between 5-500 metres (20-50m is practical due to GPS error)")
        clean.append({"name": name, "lat": lat, "lng": lng, "radius": radius})
    _set("offices", _json.dumps(clean))
    _set("office_lat", ""); _set("office_lng", "")   # purane single-office keys hatao
    db.commit()
    names_txt = ", ".join(o["name"] for o in clean)
    msg = f"{len(clean)} office branch(es) saved ({names_txt}) - punching is now limited to their radius"
    if clean_ips:
        msg += f". PC punching from office WiFi ({len(clean_ips)} IP) works without GPS"
    return {"message": msg}

# ===== AI FORMATTING CONFIG (v122: subject-wise / all-subjects AI toggle) =====
_AI_FMT_KEY = "ai_format_cfg"

def _ai_fmt_cfg(db: Session) -> dict:
    """AppSetting se AI formatting config padho — {'all': bool, 'subjects': [names]}."""
    from models import AppSetting
    import json as _json
    cfg = {"all": False, "subjects": []}
    row = db.query(AppSetting).filter(AppSetting.key == _AI_FMT_KEY).first()
    if row and row.value:
        try:
            data = _json.loads(row.value)
            cfg["all"] = bool(data.get("all"))
            subs = data.get("subjects") or []
            if isinstance(subs, list):
                cfg["subjects"] = sorted({str(s).strip()[:80] for s in subs if str(s).strip()})
        except Exception:
            pass
    return cfg

@router.get("/ai-format-config")
def admin_get_ai_format(db: Session = Depends(get_db), _=Depends(get_admin)):
    """AI formatting config — admin Subjects screen ke toggle ke liye."""
    import os as _os
    cfg = _ai_fmt_cfg(db)
    cfg["ai_available"] = bool((_os.environ.get("GEMINI_API_KEY") or "").strip())
    return cfg

@router.post("/ai-format-config")
def admin_set_ai_format(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """AI formatting config save karo. Body: {'all': bool, 'subjects': [name, ...]}"""
    from models import AppSetting
    import json as _json
    subs = payload.get("subjects") or []
    if not isinstance(subs, list):
        raise HTTPException(status_code=400, detail="subjects list bhejo")
    _seen = {}
    for s in subs:
        n = str(s).strip()[:80]
        if n and n.lower() not in _seen:
            _seen[n.lower()] = n
    clean = sorted(_seen.values())
    cfg = {"all": bool(payload.get("all")), "subjects": clean}
    row = db.query(AppSetting).filter(AppSetting.key == _AI_FMT_KEY).first()
    blob = _json.dumps(cfg, ensure_ascii=False)
    if row:
        row.value = blob
    else:
        db.add(AppSetting(key=_AI_FMT_KEY, value=blob))
    db.commit()
    if cfg["all"]:
        msg = "AI formatting is now ACTIVE for all subjects"
    elif clean:
        msg = f"AI formatting is ACTIVE for {len(clean)} subject(s): {', '.join(clean)}"
    else:
        msg = "AI formatting is OFF - offline formatting will be used everywhere"
    return {"message": msg, **cfg}

# ===== SALARY BULK SETUP (sabhi teachers, sirf gross - baaki sab auto) =====
@router.get("/contracts-overview")
def admin_contracts_overview(db: Session = Depends(get_db), _=Depends(get_admin)):
    """Saare active teachers ka contract/salary status - bulk setup screen ke liye."""
    from models import TeacherProfile, TeacherContract, User
    from teacher_routes import _ensure_contract_columns
    _ensure_contract_columns(db)
    tps = db.query(TeacherProfile).join(User, TeacherProfile.user_id == User.id).filter(
        User.is_active == True).order_by(User.name).all()
    out = []
    for tp in tps:
        c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tp.id).first()
        out.append({"teacher_id": tp.id, "name": tp.user.name if tp.user else "",
                    "subjects": tp.subjects or [],
                    "designation": (c.designation if c else "Subject Teacher"),
                    "joining_date": str(c.joining_date) if c and c.joining_date else None,
                    "gross_salary": ((c.base_salary or 0) + (c.allowances or 0)) if c else 0,
                    "accepted": bool(c and c.accepted), "has_contract": bool(c)})
    return {"teachers": out}

@router.post("/contracts-bulk")
def admin_contracts_bulk(payload: dict = Body(...), db: Session = Depends(get_db), _=Depends(get_admin)):
    """Ek hi baar me saare teachers ki gross salary set karo. Breakup (Basic/HRA/
    Conv/Medical/LTA/Special), standard rules (Annexure A) aur 26 working days
    SAB automatic - Faculty Service Agreement ke % ke hisaab se. Salary badalne
    par teacher ko letter re-accept karna hota hai."""
    from models import TeacherProfile, TeacherContract
    from teacher_routes import _salary_breakup, _ensure_contract_columns, DEFAULT_CONTRACT_RULES
    _ensure_contract_columns(db)
    items = payload.get("items") or []
    saved, skipped = [], []
    for it in items:
        tid = int(it.get("teacher_id") or 0)
        gross = int(it.get("gross_salary") or 0)
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
        if not tp:
            continue
        if gross <= 0:
            skipped.append(tp.user.name if tp.user else str(tid))
            continue
        c = db.query(TeacherContract).filter(TeacherContract.teacher_id == tid).first()
        creating = c is None
        if creating:
            c = TeacherContract(teacher_id=tid)
            db.add(c)
        old_gross = (c.base_salary or 0) + (c.allowances or 0)
        c.designation = (it.get("designation") or c.designation or "Subject Teacher").strip() or "Subject Teacher"
        jd = (it.get("joining_date") or "").strip()
        if jd:
            try:
                c.joining_date = datetime.strptime(jd, "%Y-%m-%d").date()
            except Exception:
                pass
        c.base_salary = gross
        c.allowances = 0
        c.working_days = 26
        for k, v in _salary_breakup(gross).items():
            setattr(c, k, v)
        if not (c.rules_text or "").strip():
            c.rules_text = DEFAULT_CONTRACT_RULES
        if creating or old_gross != gross:
            c.accepted = False
            c.accepted_at = None
            c.signature_name = None
        saved.append(tp.user.name if tp.user else str(tid))
    db.commit()
    msg = "Contract set for %d teacher(s) (breakup + rules auto-filled)." % len(saved)
    if skipped:
        msg += " %d skip (salary 0/khaali): %s" % (len(skipped), ", ".join(skipped))
    return {"message": msg, "saved": saved, "skipped": skipped}

@router.post("/teacher/{tid}/payout-adjust")
def admin_add_adjustment(tid: int, payload: dict, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import PayoutAdjustment, TeacherProfile
    from teacher_routes import _month_range
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found")
    kind = (payload.get("kind") or "").strip()
    if kind not in ("extra", "bonus", "deduction"):
        raise HTTPException(status_code=400, detail="Type must be extra, bonus or deduction")
    try:
        amount = int(payload.get("amount"))
        assert amount > 0
    except Exception:
        raise HTTPException(status_code=400, detail="Amount must be a positive number")
    start, _e = _month_range((payload.get("month") or "").strip())
    a = PayoutAdjustment(teacher_id=tid, month=start.strftime("%Y-%m"), kind=kind,
                         amount=amount, note=(payload.get("note") or "").strip()[:200] or None)
    db.add(a)
    db.commit()
    return {"message": "Adjustment added", "id": a.id}

@router.delete("/payout-adjust/{aid}")
def admin_delete_adjustment(aid: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import PayoutAdjustment
    a = db.query(PayoutAdjustment).filter(PayoutAdjustment.id == aid).first()
    if not a:
        raise HTTPException(status_code=404, detail="Adjustment not found")
    db.delete(a)
    db.commit()
    return {"message": "Adjustment removed"}


# ===== EXAM / TEST TRACKER (subject-wise, all teachers) =====
_EXAM_COLS_READY_ADMIN = False

def _ensure_exam_columns(db):
    """Add scheduled_at / attempted / skipped columns on first use (MySQL/Postgres/SQLite).
    Runs once per process; every ALTER is best-effort so existing databases upgrade themselves."""
    global _EXAM_COLS_READY_ADMIN
    if _EXAM_COLS_READY_ADMIN:
        return
    from sqlalchemy import text as _text
    stmts = [
        ("ALTER TABLE exams ADD COLUMN scheduled_at DATETIME NULL",
         "ALTER TABLE exams ADD COLUMN scheduled_at TIMESTAMP NULL"),
        ("ALTER TABLE exam_attempts ADD COLUMN attempted JSON NULL",
         "ALTER TABLE exam_attempts ADD COLUMN attempted TEXT NULL"),
        ("ALTER TABLE exam_attempts ADD COLUMN skipped JSON NULL",
         "ALTER TABLE exam_attempts ADD COLUMN skipped TEXT NULL"),
    ]
    for group in stmts:
        for st in group:
            try:
                db.execute(_text(st))
                db.commit()
                break
            except Exception:
                db.rollback()
    _EXAM_COLS_READY_ADMIN = True


@router.get("/exams")
def admin_exams(db: Session = Depends(get_db), _=Depends(get_admin)):
    """All tests across teachers with submission tracking (for the admin Tests Tracker)."""
    _ensure_exam_columns(db)
    rows = db.query(Exam).filter(Exam.is_active == True).order_by(Exam.created_at.desc()).all()
    out = []
    for e in rows:
        nq = db.query(ExamQuestion).filter(ExamQuestion.exam_id == e.id).count()
        na = db.query(ExamAttempt).filter(ExamAttempt.exam_id == e.id).count()
        ng = db.query(ExamAttempt).filter(ExamAttempt.exam_id == e.id,
                                          ExamAttempt.status == "graded").count()
        out.append({"id": e.id, "title": e.title, "subject": e.subject, "chapter": e.chapter,
                    "teacher_name": e.teacher_name, "test_type": e.test_type, "medium": e.medium,
                    "total_marks": e.total_marks, "duration_min": e.duration_min,
                    "questions": nq, "attempts": na, "graded": ng,
                    "scheduled_at": e.scheduled_at.isoformat() if getattr(e, "scheduled_at", None) else None,
                    "created_at": e.created_at.isoformat() if e.created_at else None})
    return out


# ===== ORPHAN DATA CLEANUP (content left behind by deleted teachers) =====
_ORPHAN_SQL = "teacher_id IS NOT NULL AND teacher_id NOT IN (SELECT id FROM teacher_profiles)"

def _orphan_counts(db):
    def c(sql):
        try:
            return int(db.execute(_sqltext(sql)).scalar() or 0)
        except Exception:
            db.rollback()
            return 0
    return {
        "tests": c("SELECT COUNT(*) FROM exams WHERE " + _ORPHAN_SQL),
        "lectures": c("SELECT COUNT(*) FROM lectures WHERE " + _ORPHAN_SQL),
        "timetables": c("SELECT COUNT(*) FROM timetables WHERE " + _ORPHAN_SQL),
        "dpp_submissions": c("SELECT COUNT(*) FROM dpp_submissions WHERE dpp_id NOT IN (SELECT id FROM dpps)"),
        "test_submissions": c("SELECT COUNT(*) FROM test_submissions WHERE test_id NOT IN (SELECT id FROM tests)"),
    }

_ORPHAN_DELETE_STMTS = [
    "DELETE FROM exam_results WHERE attempt_id IN (SELECT id FROM exam_attempts WHERE exam_id IN (SELECT id FROM exams WHERE " + _ORPHAN_SQL + "))",
    "DELETE FROM exam_attempts WHERE exam_id IN (SELECT id FROM exams WHERE " + _ORPHAN_SQL + ")",
    "DELETE FROM exam_questions WHERE exam_id IN (SELECT id FROM exams WHERE " + _ORPHAN_SQL + ")",
    "DELETE FROM exam_views WHERE exam_id IN (SELECT id FROM exams WHERE " + _ORPHAN_SQL + ")",
    "DELETE FROM exams WHERE " + _ORPHAN_SQL,
    "DELETE FROM lecture_questions WHERE lecture_id IN (SELECT id FROM lectures WHERE " + _ORPHAN_SQL + ")",
    "DELETE FROM lectures WHERE " + _ORPHAN_SQL,
    "DELETE FROM timetables WHERE " + _ORPHAN_SQL,
    "DELETE FROM teacher_attendance WHERE " + _ORPHAN_SQL,
    "DELETE FROM teacher_contracts WHERE " + _ORPHAN_SQL,
    "DELETE FROM payout_adjustments WHERE " + _ORPHAN_SQL,
    "DELETE FROM dpp_submissions WHERE dpp_id NOT IN (SELECT id FROM dpps)",
    "DELETE FROM test_submissions WHERE test_id NOT IN (SELECT id FROM tests)",
]

@router.get("/orphan-data/summary")
def orphan_data_summary(db: Session = Depends(get_db), _=Depends(get_admin)):
    """How much leftover content exists from teachers that no longer exist."""
    counts = _orphan_counts(db)
    return {"counts": counts, "total": sum(counts.values())}

@router.post("/orphan-data/cleanup")
def orphan_data_cleanup(db: Session = Depends(get_db), _=Depends(get_admin)):
    """One click: remove every leftover item (tests, lectures, timetables, orphan
    submissions) uploaded by teachers whose accounts have been deleted."""
    counts = _orphan_counts(db)
    if sum(counts.values()):
        for sql in _ORPHAN_DELETE_STMTS:
            try:
                db.execute(_sqltext(sql))
                db.commit()
            except Exception:
                db.rollback()
    return {"removed": counts, "total": sum(counts.values())}


# ===== DPP RANKINGS — admin: sab packs + submission counts =====
@router.get("/dpp-rankings")
def admin_dpp_rankings(db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import DppPack, DppAnswer, DppEvent, TeacherProfile, User as _U
    packs = db.query(DppPack).order_by(DppPack.created_at.desc()).all()
    out = []
    for pk in packs:
        subs = (db.query(DppAnswer)
                .filter(DppAnswer.pack_id == pk.id, DppAnswer.status != "staged")
                .all())
        checked = sum(1 for a in subs if a.status == "checked")
        tname = ""
        tp = db.query(TeacherProfile).filter(TeacherProfile.id == pk.teacher_id).first()
        if tp and tp.user_id:
            u = db.query(_U).filter(_U.id == tp.user_id).first()
            tname = u.name if u else ""
        if not subs and not tname:
            tname = ""
        out.append({"id": pk.id, "title": pk.title or "DPP", "subject": pk.subject or "",
                    "chapter": pk.chapter or "", "part": pk.part or "",
                    "class_name": getattr(pk, "class_name", "") or "",
                    "medium": pk.medium or "", "source": pk.source or "",
                    "teacher": tname,
                    "created_at": pk.created_at.strftime("%d %b %Y") if pk.created_at else "",
                    "submitted": len(subs), "checked": checked,
                    "pending": len(subs) - checked,
                    "views": (db.query(DppEvent.student_id)
                              .filter(DppEvent.pack_id == pk.id, DppEvent.event == "view")
                              .distinct().count()),
                    "downloads": (db.query(DppEvent.student_id)
                                  .filter(DppEvent.pack_id == pk.id, DppEvent.event == "download")
                                  .distinct().count())})
    return {"packs": out}


@router.get("/dpp-packs/{pack_id}/ranking")
def admin_dpp_ranking(pack_id: int, db: Session = Depends(get_db), _=Depends(get_admin)):
    """Admin ranking rows — same details as teacher (names clickable)."""
    from models import DppPack, DppAnswer, StudentProfile, User as _U2
    pk = db.query(DppPack).filter(DppPack.id == pack_id).first()
    if not pk:
        raise HTTPException(404, "DPP pack not found")
    rows = (db.query(DppAnswer)
            .filter(DppAnswer.pack_id == pack_id, DppAnswer.status != "staged")
            .order_by(DppAnswer.submitted_at.asc(), DppAnswer.id.asc()).all())
    out = []
    for i, a in enumerate(rows):
        srow = db.query(StudentProfile).filter(StudentProfile.id == a.student_id).first()
        nm = ""
        if srow:
            u = db.query(_U2).filter(_U2.id == srow.user_id).first()
            nm = (u.name if u else "") or ""
        nm = nm or f"Student #{a.student_id}"
        out.append({"rank": i + 1, "name": nm, "student_id": a.student_id,
                    "submitted_at": a.submitted_at.strftime("%d %b %Y, %I:%M %p") if a.submitted_at else "",
                    "status": a.status or "submitted",
                    "checked_at": a.checked_at.strftime("%d %b %Y, %I:%M %p") if a.checked_at else "",
                    "checked_by": a.checked_by or "",
                    "remarks": a.remarks or "",
                    "filename": a.filename or "",
                    "class_name": (srow.class_name if srow else "") or "",
                    "phone": (srow.phone if srow else "") or ""})
    return {"pack": {"id": pk.id, "title": pk.title or "DPP", "subject": pk.subject or "",
                     "chapter": pk.chapter or "", "part": pk.part or "",
                     "class_name": getattr(pk, "class_name", "") or ""},
            "count": len(out), "rows": out}


@router.get("/dpp-packs/{pack_id}/track-list")
def admin_dpp_track_list(pack_id: int, event: str = "view",
                         db: Session = Depends(get_db), _=Depends(get_admin)):
    from models import DppPack, DppEvent, StudentProfile, User as _U3
    pk = db.query(DppPack).filter(DppPack.id == pack_id).first()
    if not pk:
        raise HTTPException(404, "DPP pack not found")
    ev = (event or "view").lower()
    if ev not in ("view", "download"):
        raise HTTPException(400, "event 'view' ya 'download' hona chahiye")
    rows = (db.query(DppEvent)
            .filter(DppEvent.pack_id == pack_id, DppEvent.event == ev)
            .order_by(DppEvent.created_at.desc(), DppEvent.id.desc()).all())
    seen = set()
    out = []
    for e in rows:
        if e.student_id in seen:
            continue
        seen.add(e.student_id)
        nm = ""
        srow = db.query(StudentProfile).filter(StudentProfile.id == e.student_id).first()
        if srow:
            u = db.query(_U3).filter(_U3.id == srow.user_id).first()
            nm = (u.name if u else "") or ""
        out.append({"name": nm or f"Student #{e.student_id}",
                    "at": e.created_at.strftime("%d %b %Y, %I:%M %p") if e.created_at else ""})
    return {"pack": {"id": pk.id, "title": pk.title or "DPP"},
            "event": ev, "count": len(out), "rows": out}
