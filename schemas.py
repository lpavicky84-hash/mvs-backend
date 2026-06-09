from pydantic import BaseModel, EmailStr
from typing import Optional, List, Any
from datetime import date, time, datetime
from models import (
    UserRole, ClassStatus, RescheduleStatus,
    DPPType, TestStatus, SubmissionStatus, DoubtStatus, BatchName
)

# =============================================
# AUTH
# =============================================
class RegisterRequest(BaseModel):
    name: str
    user_id: str
    password: str
    role: UserRole
    phone: Optional[str] = None          # for students
    subjects: Optional[List[str]] = None # for teachers
    batch: Optional[str] = None
    class_name: Optional[str] = None
    gender: Optional[str] = None         # for teachers: male | female

class LoginRequest(BaseModel):
    user_id: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str
    user_db_id: int

# =============================================
# USER
# =============================================
class UserOut(BaseModel):
    id: int
    name: str
    user_id: str
    role: UserRole
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

# =============================================
# CLASS ENTRY
# =============================================
class ClassEntryCreate(BaseModel):
    subject: str
    class_name: str
    topic: str
    scheduled_date: date
    scheduled_time: time
    is_extra: bool = False

class ClassEntryUpdate(BaseModel):
    drive_link: Optional[str] = None
    status: Optional[ClassStatus] = None
    topic: Optional[str] = None

class ClassEntryOut(BaseModel):
    id: int
    subject: str
    class_name: str
    topic: str
    scheduled_date: date
    scheduled_time: time
    status: ClassStatus
    drive_link: Optional[str]
    is_extra: bool
    created_at: datetime
    class Config:
        from_attributes = True

# =============================================
# TIMETABLE
# =============================================
class TimetableCreate(BaseModel):
    subject: str
    class_name: str
    day_of_week: str
    start_time: time
    topic: str

class TimetableOut(BaseModel):
    id: int
    subject: str
    class_name: str
    day_of_week: str
    start_time: time
    topic: str
    is_active: bool
    class Config:
        from_attributes = True

# =============================================
# RESCHEDULE
# =============================================
class RescheduleCreate(BaseModel):
    class_entry_id: int
    new_date: date
    new_time: time
    reason: str

class RescheduleReview(BaseModel):
    status: RescheduleStatus   # approved or rejected
    admin_note: Optional[str] = None

class RescheduleOut(BaseModel):
    id: int
    class_entry_id: int
    original_date: date
    original_time: time
    new_date: date
    new_time: time
    reason: str
    status: RescheduleStatus
    admin_note: Optional[str]
    created_at: datetime
    class Config:
        from_attributes = True

# =============================================
# DPP
# =============================================
class DPPCreate(BaseModel):
    subject: str
    dpp_type: DPPType
    reference: str
    drive_link: str

class DPPOut(BaseModel):
    id: int
    subject: str
    dpp_type: DPPType
    reference: str
    drive_link: str
    is_active: bool
    created_at: datetime
    class Config:
        from_attributes = True

class DPPSubmissionCreate(BaseModel):
    dpp_id: int
    drive_link: str

class DPPSubmissionOut(BaseModel):
    id: int
    dpp_id: int
    drive_link: str
    submitted_at: datetime
    class Config:
        from_attributes = True

# =============================================
# TEST
# =============================================
class TestCreate(BaseModel):
    subject: str
    class_name: str
    test_date: date
    test_time: time
    duration_mins: int

class TestPaperUpload(BaseModel):
    test_id: int
    question_paper_link: str

class TestOut(BaseModel):
    id: int
    subject: str
    class_name: str
    test_date: date
    test_time: time
    duration_mins: int
    question_paper_link: Optional[str]
    status: TestStatus
    created_at: datetime
    class Config:
        from_attributes = True

class TestSubmissionCreate(BaseModel):
    test_id: int
    drive_link: str

class TestSubmissionOut(BaseModel):
    id: int
    test_id: int
    drive_link: str
    status: SubmissionStatus
    submitted_at: datetime
    class Config:
        from_attributes = True

# =============================================
# DOUBT
# =============================================
class DoubtCreate(BaseModel):
    teacher_id: int
    subject: str
    topic: str
    question: str
    image_link: Optional[str] = None

class DoubtResolve(BaseModel):
    answer: str
    answer_image_link: Optional[str] = None

class DoubtOut(BaseModel):
    id: int
    subject: str
    topic: str
    question: str
    image_link: Optional[str]
    answer: Optional[str]
    answer_image_link: Optional[str]
    status: DoubtStatus
    created_at: datetime
    resolved_at: Optional[datetime]
    class Config:
        from_attributes = True

# =============================================
# NOTIFICATION
# =============================================
class NotificationOut(BaseModel):
    id: int
    title: str
    message: str
    notif_type: str
    is_read: bool
    created_at: datetime
    class Config:
        from_attributes = True

# =============================================
# DASHBOARD SUMMARY
# =============================================
class TeacherDashboard(BaseModel):
    total_done: int
    total_pending: int
    total_rescheduled: int
    monthly_done: int
    monthly_pending: int
    weekly_done: int
    reschedule_this_month: int
    reschedule_limit: int = 2
    total_dpps: int
    total_tests: int
    unresolved_doubts: int

class AdminDashboard(BaseModel):
    total_teachers: int
    total_students: int
    total_classes_done: int
    total_pending: int
    pending_reschedules: int
    unresolved_doubts: int

class StudentDashboard(BaseModel):
    classes_attended: int
    dpps_submitted: int
    dpps_total: int
    tests_attempted: int
    tests_missed: int
    doubts_asked: int
    doubts_resolved: int
