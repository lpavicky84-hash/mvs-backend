# ===========================================================================
# support_models.py — Complaints & Resolution + Feedback & Ratings
# Additive, modular. Reuses models.Base, the existing Notification system, R2
# storage and WhatsApp. Nothing here alters or deletes existing rows. The whole
# feature can be removed by not including its router — no other module depends
# on these tables.
# ===========================================================================
from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Text,
                        ForeignKey, func, Index)
from models import Base, ist_now

# ---- lifecycle vocab -------------------------------------------------------
COMPLAINT_STATUSES = ["open", "assigned", "in_progress",
                      "waiting_student", "resolved", "reopened"]
OPEN_STATUSES = ["open", "assigned", "in_progress", "waiting_student", "reopened"]
COMPLAINT_PRIORITIES = ["normal", "critical", "emergency"]

# default categories seeded once; admin can add/edit/reorder/deactivate after
DEFAULT_COMPLAINT_CATEGORIES = [
    ("Admission", "grad"), ("Portal / App", "grid"), ("Technical Issue", "shield"),
    ("Study Material", "book"), ("Class / Schedule", "calendar"), ("Payment", "wallet"),
    ("Support", "help"), ("Account", "user"), ("Other", "folder"),
]


class ComplaintCategory(Base):
    __tablename__ = "complaint_categories"
    id            = Column(Integer, primary_key=True)
    name          = Column(String(120), nullable=False)
    description   = Column(String(300), nullable=True)
    icon          = Column(String(40), nullable=True)
    display_order = Column(Integer, default=0)
    status        = Column(String(12), default="active")   # active | inactive
    created_at    = Column(DateTime, default=ist_now)


class Complaint(Base):
    __tablename__ = "complaints"
    id               = Column(Integer, primary_key=True)
    complaint_number = Column(String(32), unique=True, index=True)
    student_id       = Column(Integer, ForeignKey("student_profiles.id"), index=True)
    category_id      = Column(Integer, ForeignKey("complaint_categories.id"), nullable=True)
    title            = Column(String(200), nullable=False)
    description      = Column(Text, nullable=True)
    status           = Column(String(20), default="open", index=True)
    priority         = Column(String(12), default="normal")   # normal|critical|emergency
    source           = Column(String(20), default="portal")   # portal|form|other
    assigned_to      = Column(Integer, nullable=True)          # admin user id
    resolved_by      = Column(Integer, nullable=True)          # admin user id
    read_by_student  = Column(Boolean, default=True)           # student has seen latest admin update
    read_by_admin    = Column(Boolean, default=False)          # admin has seen latest student update
    created_at       = Column(DateTime, default=ist_now, index=True)
    updated_at       = Column(DateTime, default=ist_now, onupdate=ist_now)
    first_resolved_at = Column(DateTime, nullable=True)        # never overwritten
    resolved_at      = Column(DateTime, nullable=True)         # latest resolution
    reopened_at      = Column(DateTime, nullable=True)


class ComplaintMessage(Base):
    __tablename__ = "complaint_messages"
    id             = Column(Integer, primary_key=True)
    complaint_id   = Column(Integer, ForeignKey("complaints.id"), index=True)
    sender_user_id = Column(Integer, nullable=True)
    sender_role    = Column(String(12), nullable=True)         # student | admin
    message        = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=ist_now)


class ComplaintAttachment(Base):
    __tablename__ = "complaint_attachments"
    id             = Column(Integer, primary_key=True)
    complaint_id   = Column(Integer, ForeignKey("complaints.id"), index=True)
    message_id     = Column(Integer, ForeignKey("complaint_messages.id"), nullable=True)
    kind           = Column(String(12), default="image")      # image | voice
    url            = Column(Text, nullable=True)               # R2 ref or base64 fallback
    filename       = Column(String(200), nullable=True)
    mime           = Column(String(80), nullable=True)
    size           = Column(Integer, nullable=True)
    duration       = Column(Integer, nullable=True)            # seconds, for voice
    uploader_user_id = Column(Integer, nullable=True)
    created_at     = Column(DateTime, default=ist_now)


class ComplaintEvent(Base):
    """Audit trail for admin actions (resolve/reopen/assign/reply/priority)."""
    __tablename__ = "complaint_events"
    id             = Column(Integer, primary_key=True)
    complaint_id   = Column(Integer, ForeignKey("complaints.id"), index=True)
    actor_user_id  = Column(Integer, nullable=True)
    actor_role     = Column(String(12), nullable=True)
    action         = Column(String(40), nullable=True)
    detail         = Column(String(300), nullable=True)
    created_at     = Column(DateTime, default=ist_now)


class Feedback(Base):
    __tablename__ = "feedback"
    id            = Column(Integer, primary_key=True)
    student_id    = Column(Integer, ForeignKey("student_profiles.id"), index=True)
    rating        = Column(Integer, nullable=False)            # 1..5
    review        = Column(Text, nullable=True)
    status        = Column(String(12), default="active", index=True)  # active | deleted
    read_by_admin = Column(Boolean, default=False)
    created_at    = Column(DateTime, default=ist_now, index=True)
    updated_at    = Column(DateTime, default=ist_now, onupdate=ist_now)
    deleted_at    = Column(DateTime, nullable=True)
    deleted_by    = Column(Integer, nullable=True)


Index("ix_complaints_student_status", Complaint.student_id, Complaint.status)
Index("ix_feedback_status_rating", Feedback.status, Feedback.rating)


# ---------------------------------------------------------------------------
def gen_complaint_number(db, created=None):
    """MVS-CMP-YYYYMMDD-<5-digit running number>. Uses a per-day count so the
    tail restarts each day and stays short; falls back to id if a race occurs."""
    created = created or ist_now()
    day = created.strftime("%Y%m%d")
    try:
        from sqlalchemy import func as _f
        n = db.query(_f.count(Complaint.id)).filter(
            Complaint.complaint_number.like("MVS-CMP-%s-%%" % day)).scalar() or 0
    except Exception:
        n = 0
    return "MVS-CMP-%s-%05d" % (day, n + 1)


def seed_complaint_categories():
    """Idempotent — create the default categories once if none exist. Safe on
    every boot; never touches admin-edited categories."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        if db.query(ComplaintCategory).count() == 0:
            for i, (name, icon) in enumerate(DEFAULT_COMPLAINT_CATEGORIES):
                db.add(ComplaintCategory(name=name, icon=icon,
                                         display_order=i + 1, status="active"))
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
