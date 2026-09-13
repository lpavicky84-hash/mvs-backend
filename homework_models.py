"""
Homework Checker — data model (student <-> subject teacher).

Additive only. Mirrors the Material Checker (teacher<->admin) but for the
student<->teacher flow. New tables are auto-created by Base.metadata.create_all.
Status flow: submitted -> under_review -> changes_required -> resubmitted -> checking_done.
"""
from sqlalchemy import (Column, Integer, String, Text, Boolean, DateTime,
                        ForeignKey, func)
from database import Base


class HomeworkSubmission(Base):
    __tablename__ = "homework_submissions"
    id              = Column(Integer, primary_key=True)
    student_id      = Column(Integer, index=True)   # student_profiles.id
    teacher_id      = Column(Integer, index=True)   # teacher_profiles.id (subject se resolve)
    subject         = Column(String(120))
    title           = Column(String(200))
    description     = Column(Text, nullable=True)
    status          = Column(String(24), default="submitted")
    current_version = Column(Integer, default=1)
    chat_allowed    = Column(Boolean, default=False)   # teacher student ko reply allow kare
    created_at      = Column(DateTime, default=func.now())
    updated_at      = Column(DateTime, default=func.now(), onupdate=func.now())


class HomeworkVersion(Base):
    __tablename__ = "homework_versions"
    id            = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey("homework_submissions.id"), index=True)
    version_no    = Column(Integer, default=1)
    file_url      = Column(Text, nullable=True)     # R2 key/url or base64
    filename      = Column(String(255), nullable=True)
    file_size     = Column(Integer, default=0)
    mime          = Column(String(100), nullable=True)
    remarks       = Column(Text, nullable=True)     # teacher remark on this version
    created_at    = Column(DateTime, default=func.now())


class HomeworkMessage(Base):
    __tablename__ = "homework_messages"
    id              = Column(Integer, primary_key=True)
    submission_id   = Column(Integer, ForeignKey("homework_submissions.id"), index=True)
    sender_user_id  = Column(Integer, nullable=True)
    sender_role     = Column(String(20), nullable=True)   # student | teacher
    message         = Column(Text, nullable=True)
    read_by_student = Column(Boolean, default=False)
    read_by_teacher = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=func.now())


class HomeworkAttachment(Base):
    __tablename__ = "homework_attachments"
    id               = Column(Integer, primary_key=True)
    submission_id    = Column(Integer, ForeignKey("homework_submissions.id"), index=True)
    message_id       = Column(Integer, ForeignKey("homework_messages.id"), nullable=True, index=True)
    kind             = Column(String(12), nullable=True)  # image | file
    url              = Column(Text, nullable=True)
    filename         = Column(String(255), nullable=True)
    mime             = Column(String(100), nullable=True)
    uploader_user_id = Column(Integer, nullable=True)
    created_at       = Column(DateTime, default=func.now())
