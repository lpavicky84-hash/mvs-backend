"""
MVS Services Chat — data model (student <-> admin, per purchased service).

Additive only. New tables are auto-created by Base.metadata.create_all.

Flow: student buys a service (MVSServiceInteraction.response == "yes") -> a chat
thread auto-enables for that (service, student). No admin approval needed to
enable; admin CAN disable a specific student's thread. Admin replies from a
WhatsApp-style "Services Chat" panel in the MVS Services section.

Speed: thread + message split so polling can fetch only NEW messages (since_id),
attachments served separately so they load once and are cached client-side.
"""
from sqlalchemy import (Column, Integer, String, Text, Boolean, DateTime,
                        func, UniqueConstraint, Index)
from database import Base

try:
    from sqlalchemy.dialects.mysql import MEDIUMTEXT
    _B64TEXT = Text().with_variant(MEDIUMTEXT(), "mysql")
except Exception:
    _B64TEXT = Text()


class ServiceChatThread(Base):
    """One chat thread per (service, student). Auto-enabled on purchase."""
    __tablename__ = "service_chat_threads"
    id              = Column(Integer, primary_key=True)
    service_id      = Column(Integer, index=True)       # mvs_services.id
    student_id      = Column(Integer, index=True)       # student_profiles.id
    student_user_id = Column(Integer, nullable=True)    # users.id (presence + notify)
    student_name    = Column(String(160), default="")
    student_phone   = Column(String(20), default="")
    student_class   = Column(String(20), default="")
    service_title   = Column(String(220), default="")   # denormalized for admin list
    enabled         = Column(Boolean, default=True)     # admin can disable this student's chat
    last_msg_at     = Column(DateTime, nullable=True)   # sort admin list (recent first)
    last_msg_text   = Column(String(240), default="")   # preview for admin list
    last_sender     = Column(String(10), default="")    # student | admin (preview tick)
    admin_last_read_id   = Column(Integer, default=0)   # highest msg id admin has read
    student_last_read_id = Column(Integer, default=0)   # highest msg id student has read
    student_last_seen    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("service_id", "student_id", name="uq_service_student_thread"),
        Index("ix_sct_lastmsg", "last_msg_at"),
    )


class ServiceChatMsg(Base):
    __tablename__ = "service_chat_msgs"
    id          = Column(Integer, primary_key=True, index=True)
    thread_id   = Column(Integer, index=True)
    sender_role = Column(String(10), default="")        # student | admin
    sender_name = Column(String(160), default="")
    message     = Column(Text, nullable=True)
    has_att     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=func.now())


class ServiceChatAttachment(Base):
    __tablename__ = "service_chat_attachments"
    id          = Column(Integer, primary_key=True)
    thread_id   = Column(Integer, index=True)
    message_id  = Column(Integer, index=True)
    kind        = Column(String(12), nullable=True)     # image | file
    url         = Column(_B64TEXT, nullable=True)       # R2 key/url OR base64 data-uri fallback
    filename    = Column(String(255), nullable=True)
    mime        = Column(String(120), nullable=True)
    size        = Column(Integer, default=0)
    created_at  = Column(DateTime, default=func.now())
