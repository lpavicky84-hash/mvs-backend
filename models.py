from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, Enum, Date, Time, JSON, Float
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
import enum

# ===== ENUMS =====
class UserRole(str, enum.Enum):
    admin   = "admin"
    teacher = "teacher"
    student = "student"

class ClassStatus(str, enum.Enum):
    pending      = "pending"
    done         = "done"
    rescheduled  = "rescheduled"

class RescheduleStatus(str, enum.Enum):
    pending  = "pending"
    approved = "approved"
    rejected = "rejected"

class DPPType(str, enum.Enum):
    classwise   = "classwise"
    chapterwise = "chapterwise"

class SubjectType(str, enum.Enum):
    live     = "live"
    recorded = "recorded"

class TestStatus(str, enum.Enum):
    upcoming  = "upcoming"
    active    = "active"
    completed = "completed"

class SubmissionStatus(str, enum.Enum):
    submitted      = "submitted"
    late_submitted = "late_submitted"
    missed         = "missed"

class DoubtStatus(str, enum.Enum):
    pending  = "pending"
    resolved = "resolved"

class BatchName(str, enum.Enum):
    lakshya_science  = "Lakshya Science"
    lakshya_commerce = "Lakshya Commerce"
    lakshya_arts     = "Lakshya Arts"
    udaan_10         = "Udaan Class 10"

# =============================================
# USER (Teachers, Students, Admins)
# =============================================
class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(120), nullable=False)
    user_id    = Column(String(20), unique=True, nullable=False, index=True)  # e.g. RS001
    password   = Column(String(255), nullable=False)
    role       = Column(Enum(UserRole), nullable=False)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # Relations
    teacher_profile = relationship("TeacherProfile", back_populates="user", uselist=False)
    student_profile = relationship("StudentProfile", back_populates="user", uselist=False)
    notifications   = relationship("Notification", back_populates="user")

# =============================================
# TEACHER PROFILE
# =============================================
from sqlalchemy import Text as _T3
try:
    from sqlalchemy.dialects.mysql import LONGTEXT as _LT3
    _PHOTO = _T3().with_variant(_LT3, "mysql")
except Exception:
    _PHOTO = _T3()

class TeacherProfile(Base):
    __tablename__ = "teacher_profiles"

    id             = Column(Integer, primary_key=True)
    user_id        = Column(Integer, ForeignKey("users.id"), unique=True)
    subjects       = Column(JSON)        # flat ["Physics","Chemistry"]
    subject_classes = Column(JSON)       # [{"subject":"Physics","class":"12"}, ...]
    gender         = Column(String(10), nullable=True)   # male | female
    phone          = Column(String(15), nullable=True)
    photo_b64      = Column(_PHOTO, nullable=True)
    batch          = Column(String(50))
    reschedule_count_this_month = Column(Integer, default=0)
    reschedule_reset_month = Column(Integer, default=0)  # month number

    user    = relationship("User", back_populates="teacher_profile")
    classes = relationship("ClassEntry", back_populates="teacher")
    dpps    = relationship("DPP", back_populates="teacher")
    tests   = relationship("Test", back_populates="teacher")

# =============================================
# STUDENT PROFILE
# =============================================
class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"), unique=True)
    phone        = Column(String(15), unique=True)
    batch        = Column(Enum(BatchName))
    batch_name   = Column(String(160), nullable=True)  # free-text batch from app sales sheet
    medium       = Column(String(12), nullable=True)   # Hindi | English
    email        = Column(String(160), nullable=True)
    subjects     = Column(JSON)   # ["Physics","Chemistry","Maths"]
    class_name   = Column(String(20))   # e.g. "12A"
    is_verified  = Column(Boolean, default=False)
    plain_password = Column(String(255), nullable=True)  # for phone-lookup onboarding
    class_level  = Column(String(5), nullable=True)      # "10" or "12"
    photo_b64    = Column(_PHOTO, nullable=True)
    active_session_token = Column(String(255), nullable=True)  # Single session
    last_seen    = Column(DateTime, nullable=True)
    session_start= Column(DateTime, nullable=True)

    user              = relationship("User", back_populates="student_profile")
    test_submissions  = relationship("TestSubmission", back_populates="student")
    dpp_submissions   = relationship("DPPSubmission", back_populates="student")
    doubts            = relationship("Doubt", back_populates="student")

# =============================================
# TIMETABLE (uploaded by teacher)
# =============================================
class Timetable(Base):
    __tablename__ = "timetables"

    id         = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject    = Column(String(60))
    class_name = Column(String(20))
    day_of_week = Column(String(15))     # Monday, Tuesday...
    start_time  = Column(Time)
    topic       = Column(String(200))
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=func.now())

    teacher = relationship("TeacherProfile")

# =============================================
# CLASS ENTRY (each class instance)
# =============================================
class ClassEntry(Base):
    __tablename__ = "class_entries"

    id          = Column(Integer, primary_key=True, index=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(60))
    class_name  = Column(String(20))
    topic       = Column(String(200))
    scheduled_date = Column(Date, nullable=False)
    scheduled_time = Column(Time, nullable=False)
    status      = Column(Enum(ClassStatus), default=ClassStatus.pending)
    drive_link  = Column(String(500), nullable=True)   # PDF link
    is_extra    = Column(Boolean, default=False)       # Extra class flag
    created_at  = Column(DateTime, default=func.now())
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())

    teacher      = relationship("TeacherProfile", back_populates="classes")
    reschedule   = relationship("RescheduleRequest", back_populates="class_entry", uselist=False)

# =============================================
# RESCHEDULE REQUEST
# =============================================
class RescheduleRequest(Base):
    __tablename__ = "reschedule_requests"

    id             = Column(Integer, primary_key=True)
    class_entry_id = Column(Integer, ForeignKey("class_entries.id"), unique=True)
    teacher_id     = Column(Integer, ForeignKey("teacher_profiles.id"))
    original_date  = Column(Date)
    original_time  = Column(Time)
    new_date       = Column(Date)
    new_time       = Column(Time)
    reason         = Column(Text)
    status         = Column(Enum(RescheduleStatus), default=RescheduleStatus.pending)
    admin_note     = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=func.now())
    reviewed_at    = Column(DateTime, nullable=True)

    class_entry = relationship("ClassEntry", back_populates="reschedule")
    teacher     = relationship("TeacherProfile")

# =============================================
# DPP
# =============================================
class DPP(Base):
    __tablename__ = "dpps"

    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(60))
    dpp_type    = Column(Enum(DPPType))
    reference   = Column(String(100))   # class name OR chapter name
    drive_link  = Column(String(500))
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=func.now())

    teacher     = relationship("TeacherProfile", back_populates="dpps")
    submissions = relationship("DPPSubmission", back_populates="dpp")

# =============================================
# DPP SUBMISSION (by student)
# =============================================
class DPPSubmission(Base):
    __tablename__ = "dpp_submissions"

    id         = Column(Integer, primary_key=True)
    dpp_id     = Column(Integer, ForeignKey("dpps.id"))
    student_id = Column(Integer, ForeignKey("student_profiles.id"))
    drive_link = Column(String(500))
    submitted_at = Column(DateTime, default=func.now())

    dpp     = relationship("DPP", back_populates="submissions")
    student = relationship("StudentProfile", back_populates="dpp_submissions")

# =============================================
# TEST
# =============================================
class Test(Base):
    __tablename__ = "tests"

    id             = Column(Integer, primary_key=True)
    teacher_id     = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject        = Column(String(60))
    class_name     = Column(String(20))
    test_date      = Column(Date)
    test_time      = Column(Time)
    duration_mins  = Column(Integer)       # e.g. 180
    question_paper_link = Column(String(500), nullable=True)
    status         = Column(Enum(TestStatus), default=TestStatus.upcoming)
    created_at     = Column(DateTime, default=func.now())

    teacher     = relationship("TeacherProfile", back_populates="tests")
    submissions = relationship("TestSubmission", back_populates="test")

# =============================================
# TEST SUBMISSION (by student)
# =============================================
class TestSubmission(Base):
    __tablename__ = "test_submissions"

    id           = Column(Integer, primary_key=True)
    test_id      = Column(Integer, ForeignKey("tests.id"))
    student_id   = Column(Integer, ForeignKey("student_profiles.id"))
    drive_link   = Column(String(500))
    status       = Column(Enum(SubmissionStatus))
    submitted_at = Column(DateTime, default=func.now())

    test    = relationship("Test", back_populates="submissions")
    student = relationship("StudentProfile", back_populates="test_submissions")

# =============================================
# DOUBT
# =============================================
from sqlalchemy import Text as _T2
try:
    from sqlalchemy.dialects.mysql import LONGTEXT as _LT2
    _IMGTEXT = _T2().with_variant(_LT2, "mysql")
except Exception:
    _IMGTEXT = _T2()

class Doubt(Base):
    __tablename__ = "doubts"

    id          = Column(Integer, primary_key=True)
    student_id  = Column(Integer, ForeignKey("student_profiles.id"))
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(60))
    topic       = Column(String(200))
    question    = Column(Text)
    image_link  = Column(String(500), nullable=True)
    image_b64   = Column(_IMGTEXT, nullable=True)   # direct-uploaded doubt image
    answer      = Column(Text, nullable=True)
    answer_image_link = Column(String(500), nullable=True)
    status      = Column(Enum(DoubtStatus), default=DoubtStatus.pending)
    created_at  = Column(DateTime, default=func.now())
    resolved_at = Column(DateTime, nullable=True)

    student = relationship("StudentProfile", back_populates="doubts")
    teacher = relationship("TeacherProfile")

# =============================================
# NOTIFICATION
# =============================================
class Notification(Base):
    __tablename__ = "notifications"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    title      = Column(String(200))
    message    = Column(Text)
    notif_type = Column(String(50))   # reschedule_approved, reschedule_rejected, new_notes, test_reminder, doubt_resolved
    is_read    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="notifications")

# =============================================
# AVAILABLE SUBJECTS (admin-managed master list per class)
# =============================================
class AvailableSubject(Base):
    __tablename__ = "available_subjects"

    id          = Column(Integer, primary_key=True)
    class_level = Column(String(5))    # "10" or "12"
    name        = Column(String(120))
    code        = Column(String(20))
    mode        = Column(String(12), default="live")   # live | recorded
    is_active   = Column(Boolean, default=True)

# =============================================
# TIMETABLE ENTRY (chapter + part + date + day; from Excel upload)
# =============================================
class TimetableEntry(Base):
    __tablename__ = "timetable_entries"

    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(60))
    class_name  = Column(String(40))
    chapter     = Column(String(200))
    part        = Column(String(200), nullable=True)
    entry_date  = Column(Date, nullable=True)
    day         = Column(String(20), nullable=True)
    time_text   = Column(String(40), nullable=True)
    entry_type  = Column(String(20), default="chapter")  # chapter | event
    status      = Column(String(20), default="approved") # approved | pending  (teacher extra-class needs approval)
    completed       = Column(Boolean, default=False)
    completed_at    = Column(DateTime, nullable=True)
    topic_covered   = Column(String(300), nullable=True)
    start_time      = Column(String(20), nullable=True)
    end_time        = Column(String(20), nullable=True)
    homework        = Column(Text, nullable=True)
    dpp_given       = Column(Boolean, default=False)
    remarks         = Column(Text, nullable=True)
    created_at  = Column(DateTime, default=func.now())

# =============================================
# STUDY MATERIAL (PDF stored as base64 in DB) — notes / dpp / test / answer
# =============================================
from sqlalchemy import Text as _Text
try:
    from sqlalchemy.dialects.mysql import LONGTEXT as _LONGTEXT
    _BIGTEXT = _Text().with_variant(_LONGTEXT, "mysql")
except Exception:
    _BIGTEXT = _Text()

class Material(Base):
    __tablename__ = "materials"

    id            = Column(Integer, primary_key=True)
    teacher_id    = Column(Integer, ForeignKey("teacher_profiles.id"), nullable=True)
    teacher_name  = Column(String(120), nullable=True)
    subject       = Column(String(60))
    class_name    = Column(String(40), nullable=True)
    chapter       = Column(String(200), nullable=True)
    part          = Column(String(200), nullable=True)   # which class/part of the chapter
    material_type = Column(String(20))    # notes | dpp | test | answer | other
    category      = Column(String(60), nullable=True)   # for 'other' materials
    title         = Column(String(200), nullable=True)
    filename      = Column(String(200), nullable=True)
    content_b64   = Column(_BIGTEXT)       # base64 PDF
    duration_min  = Column(Integer, nullable=True)   # for tests
    parent_id     = Column(Integer, nullable=True)   # answer -> test id
    marks         = Column(String(20), nullable=True)  # teacher's marks on a submission
    student_id    = Column(Integer, nullable=True)    # answer -> who submitted
    student_name  = Column(String(120), nullable=True)
    medium        = Column(String(20), nullable=True)    # Hindi | English (for question bank)
    is_global     = Column(Boolean, default=False)        # visible to ALL students
    external_link = Column(String(500), nullable=True)    # original PDF link (no-compress option)
    approval_status = Column(String(20), default="approved")  # approved | pending | rejected
    created_at    = Column(DateTime, default=func.now())

class MaterialView(Base):
    __tablename__ = "material_views"
    id          = Column(Integer, primary_key=True)
    material_id = Column(Integer, index=True)
    student_id  = Column(Integer, index=True)
    action      = Column(String(12))   # view | download
    created_at  = Column(DateTime, default=func.now())

class ExamView(Base):
    """Student engagement with a test: opened it, or downloaded the paper."""
    __tablename__ = "exam_views"
    id          = Column(Integer, primary_key=True)
    exam_id     = Column(Integer, index=True)
    student_id  = Column(Integer, index=True)
    action      = Column(String(12))   # view | download
    created_at  = Column(DateTime, default=func.now())


class Exam(Base):
    __tablename__ = "exams"
    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, index=True)
    teacher_name= Column(String(120))
    subject     = Column(String(120))
    title       = Column(String(200))
    chapter     = Column(String(200), nullable=True)
    test_type   = Column(String(20), default="subjective")  # mcq | subjective
    medium      = Column(String(20), default="English")     # English | Hindi | Bilingual
    total_marks = Column(Integer, default=0)
    duration_min= Column(Integer, default=60)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=func.now())

class ExamQuestion(Base):
    __tablename__ = "exam_questions"
    id          = Column(Integer, primary_key=True)
    exam_id     = Column(Integer, index=True)
    q_no        = Column(Integer)
    question_text = Column(Text)
    max_marks   = Column(Integer, default=1)
    model_answer= Column(Text, nullable=True)      # for subjective AI grading
    options     = Column(JSON, nullable=True)      # for mcq: ["A","B","C","D"]
    correct_option = Column(String(255), nullable=True)  # for mcq: correct option text
    image_b64   = Column(_BIGTEXT, nullable=True)  # optional figure attached to the question
    question_text_hi   = Column(Text, nullable=True)       # Hindi version (bilingual)
    model_answer_hi    = Column(Text, nullable=True)       # Hindi model answer (bilingual)
    options_hi         = Column(JSON, nullable=True)       # Hindi mcq options (bilingual)
    model_answer_image = Column(_BIGTEXT, nullable=True)   # optional diagram for the model answer
    explanation        = Column(Text, nullable=True)       # mcq: shown to students after submit
    explanation_hi     = Column(Text, nullable=True)       # Hindi explanation (bilingual)

class ExamAttempt(Base):
    __tablename__ = "exam_attempts"
    id          = Column(Integer, primary_key=True)
    exam_id     = Column(Integer, index=True)
    student_id  = Column(Integer, index=True)
    student_name= Column(String(120), nullable=True)
    status      = Column(String(20), default="pending")  # pending | grading | graded
    answer_image_b64 = Column(_BIGTEXT, nullable=True)   # handwritten upload
    mcq_answers = Column(JSON, nullable=True)            # {q_no: selected}
    total_awarded = Column(Float, default=0)
    overall_feedback = Column(Text, nullable=True)
    verdict     = Column(String(40), nullable=True)
    submitted_at= Column(DateTime, default=func.now())
    graded_at   = Column(DateTime, nullable=True)

class ExamResult(Base):
    __tablename__ = "exam_results"
    id          = Column(Integer, primary_key=True)
    attempt_id  = Column(Integer, index=True)
    q_no        = Column(Integer)
    marks_awarded = Column(Float, default=0)
    max_marks   = Column(Integer, default=1)
    remark      = Column(Text, nullable=True)


# ============================================================
#  SMART LECTURE VERIFICATION SYSTEM
# ============================================================
class Lecture(Base):
    """A lecture report a teacher publishes after teaching (in the MVC App).
    Optionally linked to a timetable entry, but can be standalone too."""
    __tablename__ = "lectures"
    id            = Column(Integer, primary_key=True)
    teacher_id    = Column(Integer, index=True)
    teacher_name  = Column(String(120), nullable=True)
    subject       = Column(String(80), index=True)
    class_level   = Column(String(5), nullable=True)     # "10" | "12"
    chapter       = Column(String(200), nullable=True)
    part          = Column(String(200), nullable=True)
    title         = Column(String(240))
    timetable_entry_id = Column(Integer, nullable=True, index=True)  # optional link
    lecture_date  = Column(Date, nullable=True)
    # report body
    summary       = Column(Text, nullable=True)
    homework      = Column(Text, nullable=True)
    pdf_b64       = Column(_BIGTEXT, nullable=True)
    pdf_filename  = Column(String(200), nullable=True)
    dpp_b64       = Column(_BIGTEXT, nullable=True)
    dpp_filename  = Column(String(200), nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=func.now())


class LectureQuestion(Base):
    """Mandatory verification question(s) attached to a lecture. A random one is
    shown to the student when they try to mark the lecture done."""
    __tablename__ = "lecture_questions"
    id            = Column(Integer, primary_key=True)
    lecture_id    = Column(Integer, index=True)
    qtype         = Column(String(20))   # mcq | image_mcq | numerical | fill_blank | true_false
    question      = Column(Text)
    question_hi   = Column(Text, nullable=True)
    image_b64     = Column(_BIGTEXT, nullable=True)          # optional question image
    options       = Column(JSON, nullable=True)             # ["a","b","c","d"] for mcq
    options_hi    = Column(JSON, nullable=True)
    option_images = Column(JSON, nullable=True)             # [b64,...] for image_mcq
    correct       = Column(Text)         # correct option text / numeric / blank / "true"/"false"
    tolerance     = Column(Float, nullable=True)            # numerical answer tolerance
    created_at    = Column(DateTime, default=func.now())


class LectureVerification(Base):
    """One row per (student, lecture): tracks verification state, attempts and cooldown."""
    __tablename__ = "lecture_verifications"
    id            = Column(Integer, primary_key=True)
    lecture_id    = Column(Integer, index=True)
    student_id    = Column(Integer, index=True)
    status        = Column(String(16), default="pending")   # pending | verified
    attempts      = Column(Integer, default=0)
    last_attempt  = Column(DateTime, nullable=True)
    cooldown_until= Column(DateTime, nullable=True)
    verified_at   = Column(DateTime, nullable=True)
    xp_awarded    = Column(Integer, default=0)


class StudentStats(Base):
    """Gamification + streak state per student (single row each)."""
    __tablename__ = "student_stats"
    id            = Column(Integer, primary_key=True)
    student_id    = Column(Integer, unique=True, index=True)
    xp            = Column(Integer, default=0)
    streak        = Column(Integer, default=0)          # current consecutive-day streak
    best_streak   = Column(Integer, default=0)
    last_active_day = Column(Date, nullable=True)
    badges        = Column(JSON, nullable=True)          # ["first_verify","week_streak",...]
    prev_rank     = Column(Integer, nullable=True)       # for rank-movement tracker
    updated_at    = Column(DateTime, default=func.now(), onupdate=func.now())


class UserSession(Base):
    """One row per login. Powers: live users (students AND teachers), what page
    they are on right now, how many times they have logged in, and who has never
    logged in at all."""
    __tablename__ = "user_sessions"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, index=True)
    role         = Column(String(12), index=True)     # student | teacher | admin
    started_at   = Column(DateTime, default=func.now(), index=True)
    last_seen    = Column(DateTime, default=func.now(), index=True)
    current_page = Column(String(40), nullable=True)  # which section they are on
    ip           = Column(String(45), nullable=True)


class ActivityLog(Base):
    """Lightweight per-student activity feed + consistency-calendar source."""
    __tablename__ = "activity_logs"
    id            = Column(Integer, primary_key=True)
    student_id    = Column(Integer, index=True)
    kind          = Column(String(24))    # lecture | dpp | test | doubt | material | xp
    text          = Column(String(240))
    xp            = Column(Integer, default=0)
    day           = Column(Date, index=True)
    created_at    = Column(DateTime, default=func.now())
