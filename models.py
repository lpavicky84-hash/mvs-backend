from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, Enum, Date, Time, JSON
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
class TeacherProfile(Base):
    __tablename__ = "teacher_profiles"

    id             = Column(Integer, primary_key=True)
    user_id        = Column(Integer, ForeignKey("users.id"), unique=True)
    subjects       = Column(JSON)        # flat ["Physics","Chemistry"]
    subject_classes = Column(JSON)       # [{"subject":"Physics","class":"12"}, ...]
    gender         = Column(String(10), nullable=True)   # male | female
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
    subjects     = Column(JSON)   # ["Physics","Chemistry","Maths"]
    class_name   = Column(String(20))   # e.g. "12A"
    is_verified  = Column(Boolean, default=False)
    plain_password = Column(String(255), nullable=True)  # for phone-lookup onboarding
    class_level  = Column(String(5), nullable=True)      # "10" or "12"
    active_session_token = Column(String(255), nullable=True)  # Single session

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
class Doubt(Base):
    __tablename__ = "doubts"

    id          = Column(Integer, primary_key=True)
    student_id  = Column(Integer, ForeignKey("student_profiles.id"))
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(60))
    topic       = Column(String(200))
    question    = Column(Text)
    image_link  = Column(String(500), nullable=True)
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
    material_type = Column(String(20))    # notes | dpp | test | answer | other
    category      = Column(String(60), nullable=True)   # for 'other' materials
    title         = Column(String(200), nullable=True)
    filename      = Column(String(200), nullable=True)
    content_b64   = Column(_BIGTEXT)       # base64 PDF
    duration_min  = Column(Integer, nullable=True)   # for tests
    parent_id     = Column(Integer, nullable=True)   # answer -> test id
    student_id    = Column(Integer, nullable=True)    # answer -> who submitted
    student_name  = Column(String(120), nullable=True)
    created_at    = Column(DateTime, default=func.now())
