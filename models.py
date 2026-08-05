from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime,
    ForeignKey, Enum, Date, Time, JSON, Float
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
try:
    from sqlalchemy.dialects.mysql import MEDIUMTEXT
    _B64TEXT = Text().with_variant(MEDIUMTEXT(), "mysql")   # MySQL TEXT = 64KB limit hota hai
except Exception:
    _B64TEXT = Text()
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
    # v94: restricted sub-admin — NULL = full access (super admin),
    # otherwise JSON list of allowed admin section keys (e.g. ["dashboard","doubts"])
    allowed_sections = Column(JSON, nullable=True)
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
    plain_password = Column(String(255), nullable=True)  # admin ko current login credentials dikhane ke liye
    payout_passcode = Column(String(255), nullable=True)      # v89: payout section lock (hashed)
    letter_accept_version = Column(Integer, default=0)        # v89: payout gate — LETTER_VERSION se kam ho to re-accept
    passcode_reset_pending = Column(Boolean, default=False)   # v89: admin approval pending
    letter_remark = Column(Text, nullable=True)               # v90: appointment letter pe teacher ka doubt
    letter_remark_status = Column(String(20), nullable=True)  # v90: pending | resolved
    letter_remark_reply = Column(Text, nullable=True)         # v90: admin ka reply
    letter_remark_at = Column(DateTime, nullable=True)        # v90: remark kab aaya

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
    source       = Column(String(20), default="mvs_app")  # mvs_portal | mvs_app
    welcome_sent_at = Column(DateTime, nullable=True)     # WhatsApp welcome bheja gaya?
    email        = Column(String(160), nullable=True)
    subjects     = Column(JSON)   # ["Physics","Chemistry","Maths"]
    class_name   = Column(String(20))   # e.g. "12A"
    is_verified  = Column(Boolean, default=False)
    plain_password = Column(String(255), nullable=True)  # for phone-lookup onboarding
    class_level  = Column(String(5), nullable=True)      # "10" or "12"
    exam_session = Column(String(30), nullable=True)     # syllabus tracker: chosen exam session
    study_target = Column(String(10), nullable=True)     # syllabus tracker: pass | high
    exam_date    = Column(String(20), nullable=True)     # syllabus tracker: On Demand exam date
    exam_stream  = Column(String(4), nullable=True)      # syllabus tracker: NIOS stream 1 / 2 / 3 / 4
    goal         = Column(String(20), nullable=True)     # jee | neet | other
    goal_custom  = Column(String(120), nullable=True)    # handwritten goal when goal=other
    nios_ref     = Column(String(40), nullable=True)     # NIOS reference/enrollment no. (asked once)
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
    tt_entry_id    = Column(Integer, nullable=True)   # v86: timetable_entries wali request (class_entry NULL rehti hai)
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

# Bade file/PDF payloads ke liye — MySQL TEXT sirf 64KB hota hai, LONGTEXT chahiye
# (warna "Data too long for column" 500 aata hai). SQLite pe TEXT hi rehta hai.
try:
    from sqlalchemy.dialects.mysql import LONGTEXT as _LT3
    _FILETEXT = Text().with_variant(_LT3, "mysql")
except Exception:
    _FILETEXT = Text()

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
    attach_mime = Column(String(100), nullable=True)   # mime of the uploaded attachment (image/pdf/any)
    attach_name = Column(String(255), nullable=True)   # original filename
    audio_b64   = Column(_IMGTEXT, nullable=True)      # student's voice note (webm)
    answer_audio_b64 = Column(_IMGTEXT, nullable=True) # teacher's voice answer (webm)
    answer_attach_b64  = Column(_IMGTEXT, nullable=True)  # teacher's answer attachment
    answer_attach_mime = Column(String(100), nullable=True)
    answer_attach_name = Column(String(255), nullable=True)
    status      = Column(Enum(DoubtStatus), default=DoubtStatus.pending)
    created_at  = Column(DateTime, default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    # v93: doubt reassignment — teacher jisko answer nahi aata, woh doubt
    # kisi doosre teacher ya admin ko assign kar sakta hai. Assigner ki
    # taraf se doubt "resolved" maana jata hai; naya owner responsible.
    assigned_by_teacher_id = Column(Integer, nullable=True)   # jis teacher ne assign kiya
    assigned_by_name       = Column(String(160), nullable=True)
    assigned_at            = Column(DateTime, nullable=True)
    assigned_to_admin      = Column(Boolean, default=False)   # admin (MVS Foundation) ke paas

    student = relationship("StudentProfile", back_populates="doubts")
    teacher = relationship("TeacherProfile")


class DoubtResponse(Base):
    """v93: doubt thread — teacher / student / admin teeno likh sakte hain.
    Admin ka response MVS Foundation branding ke saath jata hai."""
    __tablename__ = "doubt_responses"

    id          = Column(Integer, primary_key=True)
    doubt_id    = Column(Integer, ForeignKey("doubts.id"), index=True)
    role        = Column(String(20))          # teacher | student | admin
    author_name = Column(String(160))
    author_teacher_id = Column(Integer, nullable=True)  # role=teacher ho to
    body        = Column(Text)
    created_at  = Column(DateTime, default=func.now())

# =============================================
# NOTIFICATION
# =============================================
class DppPack(Base):
    """Teacher ka DPP — created (editor se) ya uploaded (2 PDFs).
    Timetable-connected: subject -> chapter -> part."""
    __tablename__ = "dpp_packs"

    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"))
    subject     = Column(String(120))
    class_name  = Column(String(50), default="")
    chapter     = Column(String(250), default="")
    part        = Column(String(120), default="")
    title       = Column(String(250))
    medium      = Column(String(20), default="English")   # English / Hindi / Bilingual
    source      = Column(String(10), default="created")   # created | uploaded
    questions   = Column(JSON, default=list)              # created DPP ke Q+A
    q_pdf       = Column(_FILETEXT)  # base64 — questions paper (no answers)
    s_pdf       = Column(_FILETEXT)  # base64 — solutions paper (with answers)
    created_at  = Column(DateTime, default=func.now())


class DppAnswer(Base):
    """Student ka submitted DPP — checked + remarks flow (no marks)."""
    __tablename__ = "dpp_answers"

    id           = Column(Integer, primary_key=True)
    pack_id      = Column(Integer, ForeignKey("dpp_packs.id"))
    student_id   = Column(Integer, ForeignKey("student_profiles.id"))
    answer_b64   = Column(_FILETEXT)
    filename     = Column(String(250), default="dpp-answer.pdf")
    status       = Column(String(12), default="submitted")   # submitted | checked
    remarks      = Column(Text, default="")
    checked_by   = Column(String(120), default="")
    allow_resubmit = Column(Boolean, default=False)  # teacher ne re-submit on kiya ho
    submitted_at = Column(DateTime, default=func.now())
    checked_at   = Column(DateTime)


# =============================================
# VIDEO TASK MANAGER (production tasks for YouTube channels)
# =============================================
class VideoChannel(Base):
    __tablename__ = "video_channels"

    id         = Column(Integer, primary_key=True)
    name       = Column(String(160), unique=True)
    active     = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())


class VideoType(Base):
    """Video ka type (Short / Long / One Shot / Strategy ...). Admin naye add kar sakta hai."""
    __tablename__ = "video_types"

    id         = Column(Integer, primary_key=True)
    name       = Column(String(120), unique=True)
    active     = Column(Boolean, default=True)
    sort       = Column(Integer, default=0)
    streaming_scope = Column(String(12), default="both")   # both | live | recorded
    created_at = Column(DateTime, default=func.now())


class VideoTask(Base):
    """Production manager -> teacher video task: thumbnail + deadline + review workflow."""
    __tablename__ = "video_tasks"

    id             = Column(Integer, primary_key=True)
    teacher_id     = Column(Integer, ForeignKey("teacher_profiles.id"))
    title          = Column(String(300))
    channel_id     = Column(Integer, ForeignKey("video_channels.id"), nullable=True)
    channel_name   = Column(String(160), default="")
    video_type     = Column(String(120), default="")      # Short Video / Long Video / One Shot / Strategy ...
    thumbnail_b64  = Column(_B64TEXT, nullable=True)      # uploaded thumbnail image (compressed)
    thumbnail_link = Column(String(600), default="")      # ya drive link
    reference      = Column(Text, default="")             # manager ka reference/brief
    remarks        = Column(Text, default="")             # assignment remarks
    deadline       = Column(DateTime, nullable=True)
    status         = Column(String(20), default="assigned")
    # assigned -> submitted -> approved|editing_soon|editing_done|uploaded
    # rejected -> wapas assigned (reshoot, new deadline)
    proposed_by    = Column(String(10), default="admin")  # admin | teacher
    proposal_ok    = Column(String(10), default="")       # pending | approved | rejected
    submitted_link = Column(String(600), default="")
    vintage        = Column(String(10), default="")       # "" unverified | new | old (admin verifies)
    submitted_at   = Column(DateTime, nullable=True)
    on_time        = Column(Boolean, nullable=True)       # submit ke waqt set
    reviewed       = Column(Boolean, default=False)       # admin check = checking blink off
    review_remarks = Column(Text, default="")             # rejection reason etc.
    reject_count   = Column(Integer, default=0)
    warned_24h     = Column(Boolean, default=False)
    warned_overdue = Column(Boolean, default=False)
    kind           = Column(String(20), default="normal") # normal | one_shot | rapid_revision | project
    subject        = Column(String(160), default="")      # special/project task ka subject (class ke saath — "Physics 12")
    status_history = Column(Text, default="")             # JSON [{s, at, note}] — status timeline
    last_link_at   = Column(DateTime, nullable=True)      # special task me aakhri chapter link kab aaya
    admin_seen_at  = Column(DateTime, nullable=True)      # admin ne special update kab dekha (NEW blink)
    weekly_quota   = Column(Integer, default=0)           # project/one-shot: har week kitni videos chahiye
    weekly_day     = Column(String(12), default="")       # weekly deadline ka din — monday..sunday
    item_source    = Column(String(12), default="")       # project items kahan se: custom | syllabus
    streaming      = Column(String(20), default="")       # '' | recorded | live
    youtube_url    = Column(String(600), default="")      # published YouTube link (manager posts)
    yt_video_id    = Column(String(40), default="")       # extracted video id
    yt_views       = Column(Integer, nullable=True)       # last fetched view count
    yt_views_at    = Column(DateTime, nullable=True)      # last fetch time
    created_at     = Column(DateTime, default=func.now())
    updated_at     = Column(DateTime, default=func.now(), onupdate=func.now())


class VideoTaskChapter(Base):
    """Special video task (One Shot / Rapid Revision) ke chapter/subject rows —
    har row pe teacher apna video link lagata hai, progress auto ginti hai."""
    __tablename__ = "video_task_chapters"

    id           = Column(Integer, primary_key=True)
    task_id      = Column(Integer, ForeignKey("video_tasks.id"), index=True)
    title        = Column(String(300))                    # chapter naam (rapid revision me subject naam)
    sort         = Column(Integer, default=0)
    link         = Column(String(600), default="")
    submitted_at = Column(DateTime, nullable=True)
    edit_status  = Column(String(20), default="")         # production: editing_soon / editing_done / uploaded
    changed_at   = Column(DateTime, nullable=True)        # v91: link add/update/remove kab hua (admin blink)
    vintage      = Column(String(10), default="")         # "" unverified | new | old (admin verifies)


class VideoViewSnapshot(Base):
    """Real-time YouTube views ka history — har refresh pe ek row, taaki
    date/time-wise graph aur growth dikhaya ja sake."""
    __tablename__ = "video_view_snapshots"

    id          = Column(Integer, primary_key=True)
    task_id     = Column(Integer, ForeignKey("video_tasks.id"), index=True)
    views       = Column(Integer, default=0)
    captured_at = Column(DateTime, default=func.now(), index=True)


class DppEvent(Base):
    """DPP view/download tracking — kaunse student ne kab view/download kiya."""
    __tablename__ = "dpp_events"

    id         = Column(Integer, primary_key=True)
    pack_id    = Column(Integer, ForeignKey("dpp_packs.id"))
    student_id = Column(Integer, ForeignKey("student_profiles.id"))
    event      = Column(String(10), default="view")   # view | download
    created_at = Column(DateTime, default=func.now())


class DppChunk(Base):
    """Chunked upload ke parts — slow/strict networks pe bada single request mar
    jata hai, isliye file chhote chunks me aati hai aur assemble hoti hai."""
    __tablename__ = "dpp_chunks"

    id         = Column(Integer, primary_key=True)
    pack_id    = Column(Integer, ForeignKey("dpp_packs.id"))
    student_id = Column(Integer, ForeignKey("student_profiles.id"))
    upload_key = Column(String(64), index=True)
    idx        = Column(Integer)
    total      = Column(Integer)
    filename   = Column(String(250), default="")
    data       = Column(_FILETEXT)                 # base64 chunk (no dataURL prefix)
    created_at = Column(DateTime, default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    title      = Column(String(200))
    message    = Column(Text)
    notif_type = Column(String(50))   # reschedule_approved, reschedule_rejected, new_notes, test_reminder, doubt_resolved
    link       = Column(String(500), nullable=True)  # video_task link — click pe open
    image_url  = Column(String(500), nullable=True)  # v86: teacher photo endpoint — notification avatar
    is_read    = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    # v93: sent-notification views tracking (teacher → students batches)
    sender_id   = Column(Integer, nullable=True)      # bhejne wale user ka id
    sender_role = Column(String(20), nullable=True)   # teacher | admin
    batch_key   = Column(String(40), nullable=True)   # ek send = ek batch
    batch_label = Column(String(160), nullable=True)  # e.g. "Physics" / "All My Students"
    read_at     = Column(DateTime, nullable=True)     # kab dekha

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
    shift_plan   = Column(Text, nullable=True)   # extra-class ke saath auto-shift ka plan (JSON)
    completed       = Column(Boolean, default=False)
    completed_at    = Column(DateTime, nullable=True)
    topic_covered   = Column(String(300), nullable=True)
    start_time      = Column(String(20), nullable=True)
    end_time        = Column(String(20), nullable=True)
    homework        = Column(Text, nullable=True)
    dpp_given       = Column(Boolean, default=False)
    remarks         = Column(Text, nullable=True)
    resched_by      = Column(String(20), nullable=True)  # v86: kaun move kiya — teacher | admin | leave (NULL = kabhi move nahi)
    created_at  = Column(DateTime, default=func.now())


class StudioReport(Base):
    """v97: admin / studio manager ka class report — timetable ki kisi bhi class
    (part ya event) pe status + notes. Ek entry pe ek report (upsert); baad me
    edit ki ja sakti hai. Teacher ke lecture report se ALAG hai — ye studio /
    centre side ka internal note hai (recording, setup, issues...)."""
    __tablename__ = "studio_reports"

    id          = Column(Integer, primary_key=True)
    entry_id    = Column(Integer, ForeignKey("timetable_entries.id"), unique=True, index=True)
    # snapshot fields — entry baad me move/edit ho to bhi report ka context bana rahe
    entry_date  = Column(String(12), default="")        # YYYY-MM-DD
    day         = Column(String(20), default="")
    time_str    = Column(String(40), default="")
    subject     = Column(String(120), default="")
    class_name  = Column(String(60), default="")
    chapter     = Column(String(300), default="")
    part        = Column(String(300), default="")
    status      = Column(String(20), default="held")    # held | issues | cancelled
    notes       = Column(Text, default="")
    reporter    = Column(String(160), default="")       # studio manager / admin ka naam
    # v98: teacher report jaisa — actual class timing + class notes (PDF) upload
    start_time  = Column(String(20), default="")
    end_time    = Column(String(20), default="")
    notes_file_b64  = Column(_FILETEXT, nullable=True)  # base64 (dataURL ok) — class notes PDF
    notes_file_name = Column(String(255), default="")
    notes_file_mime = Column(String(100), default="")
    created_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime, default=func.now())
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())

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
    class_name  = Column(String(50), default="")            # "Class 10" / "Class 12" — "" = sabhi classes
    test_type   = Column(String(20), default="subjective")  # mcq | subjective
    medium      = Column(String(20), default="English")     # English | Hindi | Bilingual
    total_marks = Column(Integer, default=0)
    duration_min= Column(Integer, default=60)
    scheduled_at= Column(DateTime, nullable=True)        # test goes live at this date/time
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
    alt_image_b64      = Column(_BIGTEXT, nullable=True)   # figure for the part after an "OR" alternative
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
    attempted   = Column(JSON, nullable=True)            # [q_no] student says they attempted
    skipped     = Column(JSON, nullable=True)            # [q_no] student says they skipped
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

class SessionDeadline(Base):
    """Batch/subject-wise session end date. Timetable is auto-shifted only up to
    this date; beyond it the teacher is asked to use an extra weekday instead.
    scope: 'global' | 'batch' | 'subject'
      global  -> key = ''            (fallback for everything)
      batch   -> key = 'Lakshya Science'
      subject -> key = 'Physics'     (optionally 'Physics|12')
    """
    __tablename__ = "session_deadlines"

    id       = Column(Integer, primary_key=True)
    scope    = Column(String(20), default="global")
    key      = Column(String(120), default="")
    end_date = Column(Date, nullable=False)
    note     = Column(String(200), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class AppReview(Base):
    """Student app reviews — pehle admin approval, phir Play Store prompt.
    status: pending -> approved | resolved (issue fix karke dobara likhne ko bola)"""
    __tablename__ = "app_reviews"

    id          = Column(Integer, primary_key=True)
    student_id  = Column(Integer, ForeignKey("student_profiles.id"))
    rating      = Column(Integer, default=5)          # 1-5 stars
    review      = Column(Text)
    status      = Column(String(20), default="pending")   # pending|approved|resolved
    admin_note  = Column(Text, nullable=True)
    created_at  = Column(DateTime, server_default=func.now())
    reviewed_at = Column(DateTime, nullable=True)

    student = relationship("StudentProfile")


# =============================================
# TEACHER ATTENDANCE (punch in / punch out)
# =============================================
class TeacherAttendance(Base):
    """Ek row = ek teacher ka ek din. Pehla Punch In aur aakhri Punch Out
    store hota hai. Times IST me save hoti hain (server UTC ho to bhi)."""
    __tablename__ = "teacher_attendance"

    id         = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    att_date   = Column(Date, index=True)
    punch_in   = Column(DateTime, nullable=True)
    punch_out  = Column(DateTime, nullable=True)
    # geofence: punch kahan se hua (office se kitne meter door)
    in_lat     = Column(Float, nullable=True)
    in_lng     = Column(Float, nullable=True)
    in_dist    = Column(Integer, nullable=True)    # meters from office at punch-in
    in_office  = Column(String(80), nullable=True) # kaunse branch se punch-in
    out_lat    = Column(Float, nullable=True)
    out_lng    = Column(Float, nullable=True)
    out_dist   = Column(Integer, nullable=True)
    out_office = Column(String(80), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    teacher = relationship("TeacherProfile")


class TeacherWorkPolicy(Base):
    """Per-teacher smart work-timing policy (admin set karta hai).
    work_type: full_time (lunch break working hours me count nahi hota) | part_time.
    mode: fixed (entry/exit time pakka) | hours (sirf daily hours, timing free) |
          flexible (minimum 1 hour — poori tarah free).
    Net working hours = punch gap - break_minutes (sirf full_time me).
    Present = net >= required_hours; usse kam (par punch complete) = Present (Short).
    Extra hours sirf display ke liye hain — unka koi extra payout nahi."""
    __tablename__ = "teacher_work_policies"

    id             = Column(Integer, primary_key=True)
    teacher_id     = Column(Integer, ForeignKey("teacher_profiles.id"), unique=True, index=True)
    work_type      = Column(String(12), default="full_time")   # full_time | part_time
    mode           = Column(String(12), default="hours")       # fixed | hours | flexible
    required_hours = Column(Float, default=8.0)                # assigned net hours/day
    entry_time     = Column(String(5), default="")             # "09:30" (fixed mode)
    exit_time      = Column(String(5), default="")             # "18:30" (fixed mode)
    break_minutes  = Column(Integer, default=0)                # lunch break (full_time only)
    disabled       = Column(Boolean, default=False)            # v101: attendance off — target only (punch doosre app pe)
    updated_at     = Column(DateTime, nullable=True)

    teacher = relationship("TeacherProfile")


class TeacherLeave(Base):
    """Teacher ka leave request: start-end date + reason. Admin approve/reject karta hai.
    Approved leave absent ki jagah 'Leave' count hoti hai; bina approval ki chhutti = AB."""
    __tablename__ = "teacher_leaves"

    id           = Column(Integer, primary_key=True)
    teacher_id   = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    start_date   = Column(Date, index=True)
    end_date     = Column(Date, index=True)
    leave_type   = Column(String(30), default="full")   # full | half
    reason       = Column(Text, default="")
    status       = Column(String(20), default="pending", index=True)  # pending | approved | rejected
    paid         = Column(Boolean, default=False)       # v86: admin approve karte waqt choose karta hai — paid leave pe salary deduction NAHI
    admin_remark = Column(Text, default="")
    reviewed_at  = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, server_default=func.now())

    teacher = relationship("TeacherProfile")

# =============================================
# APP SETTINGS (office location etc. key-value)
# =============================================
class AppSetting(Base):
    __tablename__ = "app_settings"

    key   = Column(String(50), primary_key=True)
    value = Column(Text, nullable=True)


# =============================================
# TEACHER CONTRACT (appointment letter + payout rules)
# =============================================
class TeacherContract(Base):
    """Appointment letter ka data + payout ke base rules. Teacher pehli baar
    portal kholte hi letter accept karta hai (typed digital signature)."""
    __tablename__ = "teacher_contracts"

    id             = Column(Integer, primary_key=True)
    teacher_id     = Column(Integer, ForeignKey("teacher_profiles.id"), unique=True, index=True)
    designation    = Column(String(120), default="Subject Teacher")
    joining_date   = Column(Date, nullable=True)
    base_salary    = Column(Integer, default=0)    # monthly INR
    allowances     = Column(Integer, default=0)    # fixed monthly allowances INR
    working_days   = Column(Integer, default=26)   # payable working days per month
    rules_text     = Column(Text, nullable=True)   # one rule per line; letter + payout page dono me dikhta hai
    # salary breakup (Faculty Service Agreement Table A-0 ke % se auto-computed)
    basic            = Column(Integer, nullable=True)
    hra              = Column(Integer, nullable=True)
    conveyance       = Column(Integer, nullable=True)
    medical          = Column(Integer, nullable=True)
    lta              = Column(Integer, nullable=True)
    special_allowance = Column(Integer, nullable=True)
    accepted       = Column(Boolean, default=False)
    accepted_at    = Column(DateTime, nullable=True)
    signature_name = Column(String(120), nullable=True)
    created_at     = Column(DateTime, server_default=func.now())
    updated_at     = Column(DateTime, server_default=func.now(), onupdate=func.now())

    teacher = relationship("TeacherProfile")

# =============================================
# PERFORMANCE PAYOUT (monthly task-based salary, 1 Aug 2026 se)
# =============================================
class PayoutTemplate(Base):
    """Teacher ki monthly responsibilities ka template: har category ka target
    aur salary-weight (%). Admin edit karta hai; har mahine yahi se compute hota.
    source='auto' -> portal ka data khud count hota hai (classes, dpp, tests...);
    source='manual' -> teacher mark karta hai, admin approve karta hai."""
    __tablename__ = "payout_templates"

    id         = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    key        = Column(String(30))          # live_class | dpp | test | doubt | content | oneshot | rapid | ytlive | shorts | promo | crash | tandav
    label      = Column(String(80))
    target     = Column(Integer, default=0)  # monthly target count (0 = category off)
    weight_pct = Column(Float, default=0)    # salary ka kitna % is category pe
    source     = Column(String(10), default="manual")  # auto | manual
    sort       = Column(Integer, default=0)

    teacher = relationship("TeacherProfile")


class PayoutTask(Base):
    """Ek kaam ki entry. Manual categories me teacher 'done' mark karta hai aur
    admin approve karta hai. status: pending | approved | rejected | missed.
    done_date us din ki hoti hai jab kaam HUA - wahi decide karta hai ki kaunse
    mahine me count hoga (policy rule 2/3). 'missed' + ref_id = auto category ka
    exception (jaise scheduled class nahi hui)."""
    __tablename__ = "payout_tasks"

    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    month       = Column(String(7), index=True)     # "2026-08" - jis month ke target ka hissa
    key         = Column(String(30))
    title       = Column(String(200))
    status      = Column(String(20), default="pending")
    ref_id      = Column(Integer, nullable=True)
    done_date   = Column(Date, nullable=True)
    note        = Column(String(300), nullable=True)
    approved_by = Column(String(120), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at  = Column(DateTime, server_default=func.now())

    teacher = relationship("TeacherProfile")


class PayoutMonth(Base):
    """Month-end finalize/paid record. Finalize karte hi us waqt ka poora
    calculation snapshot freeze ho jaata hai (baad me data badle to bhi record
    nahi badalta)."""
    __tablename__ = "payout_months"

    id           = Column(Integer, primary_key=True)
    teacher_id   = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    month        = Column(String(7), index=True)
    status       = Column(String(20), default="finalized")  # finalized | paid
    snapshot     = Column(Text, nullable=True)              # JSON of full breakdown
    finalized_at = Column(DateTime, nullable=True)
    paid_at      = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, server_default=func.now())

    teacher = relationship("TeacherProfile")


# =============================================
# PAYOUT ADJUSTMENT (manual extra / bonus / deduction per month)
# =============================================
class PayoutAdjustment(Base):
    __tablename__ = "payout_adjustments"

    id         = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    month      = Column(String(7), index=True)        # "2026-07"
    kind       = Column(String(20), default="bonus")  # extra | bonus | deduction
    amount     = Column(Integer, default=0)           # INR
    note       = Column(String(200), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    teacher = relationship("TeacherProfile")


# =============================================
# TEACHER PAY CONFIG (v80 earnings model — per-teacher editable pay structure)
# =============================================
class TeacherPayConfig(Base):
    """Per-teacher pay structure + monthly content targets (admin editable).
    Defaults mirror the appointment-letter model: max potential 25,000 of which
    the class-conduct retainer (15,000) is 60%."""
    __tablename__ = "teacher_pay_configs"

    id               = Column(Integer, primary_key=True)
    teacher_id       = Column(Integer, ForeignKey("teacher_profiles.id"), unique=True, index=True)
    class_retainer   = Column(Integer, default=15000)   # 60% of max potential
    class_quality    = Column(Integer, default=1000)
    notes_dpp        = Column(Integer, default=2000)    # notes 40% + dpp 40% + tests 20%
    doubt_resolution = Column(Integer, default=1000)
    project_delivery = Column(Integer, default=6000)    # tasks 50% + content 50%
    tests_target     = Column(Integer, default=4)
    videos_target    = Column(Integer, default=8)
    live_target      = Column(Integer, default=4)
    shorts_target    = Column(Integer, default=8)
    # v95: target names editable + admin custom extra targets (display-only)
    target_labels    = Column(JSON, nullable=True)   # {"tests": "Weekly Tests", ...}
    custom_targets   = Column(JSON, nullable=True)   # [{"name": "Revision Sheets", "count": 10}]
    designation      = Column(String(120), default="")
    department       = Column(String(120), default="")
    employee_code    = Column(String(40), default="")
    bank_name        = Column(String(120), default="")
    account_no       = Column(String(60), default="")
    ifsc             = Column(String(20), default="")
    updated_at       = Column(DateTime, default=func.now(), onupdate=func.now())

    teacher = relationship("TeacherProfile")


# ===== NIOS SYLLABUS TRACKER =====
class SyllabusOverride(Base):
    """Admin edited syllabus for one subject. Overrides the built in seed data."""
    __tablename__ = "syllabus_overrides"
    id          = Column(Integer, primary_key=True)
    class_level = Column(String(5), index=True)
    code        = Column(String(20), index=True)
    payload     = Column(_BIGTEXT)                 # JSON: name, marks, expected, modules
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())


class SyllabusHidden(Base):
    """Subjects removed from the tracker by admin."""
    __tablename__ = "syllabus_hidden"
    id          = Column(Integer, primary_key=True)
    class_level = Column(String(5), index=True)
    code        = Column(String(20), index=True)


class ChapterPlan(Base):
    """One student's chapter selection for one subject."""
    __tablename__ = "chapter_plans"
    id                = Column(Integer, primary_key=True)
    student_id        = Column(Integer, ForeignKey("student_profiles.id"), index=True)
    subject_code      = Column(String(20), index=True)
    selected          = Column(Text)               # JSON list of lesson numbers
    done              = Column(Text)               # JSON list of lesson numbers
    tma_assumed       = Column(Float, nullable=True)
    practical_assumed = Column(Float, nullable=True)
    updated_at        = Column(DateTime, default=func.now(), onupdate=func.now())
