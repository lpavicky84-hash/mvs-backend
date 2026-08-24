"""
Category-based teacher workspace — data model (additive foundation).

This module ONLY ADDS new tables. It never alters or reads-destructively any
existing table. The existing NIOS portal keeps using teacher_profiles.subjects
exactly as before; this category layer sits alongside it.

Design rules honoured (from the spec):
- Every category has an immutable `internal_key` used for all foreign keys and
  logic. `display_name` is editable and NEVER used as an identity.
- Teacher <-> Category is a real many-to-many (teacher_categories), so one
  teacher account serves every assigned category.
- Category features are data-driven (category_features), not hard-coded ifs.
- Subjects belong to a category (category_subjects); teachers are assigned
  category+subject via teacher_category_subjects.
- Material Checker (submissions / versions / threaded messages / attachments)
  is one reusable engine across all categories.
"""
from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Text,
                        ForeignKey, func, JSON, UniqueConstraint, Index)

from models import Base


# ---------------------------------------------------------------------------
# Feature catalogue — the sidebar sections a category can switch on/off.
# Keys are stable; labels are for the admin UI only.
# ---------------------------------------------------------------------------
FEATURE_CATALOG = [
    ("dashboard",         "Dashboard"),
    ("my_subjects",       "My Subjects"),
    ("my_tasks",          "My Tasks"),
    ("timetable",         "Time Table"),
    ("dpp",               "DPP"),
    ("tests",             "Tests"),
    ("classes_material",  "Classes Material"),
    ("study_material",    "Study Material"),
    ("subject_materials", "Subject Materials"),
    ("material_checker",  "Material Checker"),
    ("students",          "Students"),
    ("doubts",            "Doubts"),
    ("performance",       "Performance"),
    ("payout",            "Payout / Incentives"),
    ("notifications",     "Notifications"),
    ("content_calendar",  "Content Calendar"),
    ("profile",           "Profile"),
]
FEATURE_KEYS = [k for k, _ in FEATURE_CATALOG]

# Default feature set per seeded category (spec sections 6 & 7).
# NIOS keeps EVERYTHING it has today so nothing disappears for existing teachers.
NIOS_DEFAULT_FEATURES = set(FEATURE_KEYS) - {"content_calendar"}
DU_SOL_DEFAULT_FEATURES = {
    "dashboard", "my_subjects", "my_tasks", "subject_materials",
    "material_checker", "performance", "payout", "notifications", "profile",
}

# Statuses used by the Material Checker lifecycle.
MATERIAL_STATUSES = ["draft", "submitted", "under_review", "changes_required",
                     "resubmitted", "approved", "rejected"]


class Category(Base):
    """A teacher workspace type (NIOS, DU SOL, IGNOU, ...)."""
    __tablename__ = "categories"

    id            = Column(Integer, primary_key=True)
    internal_key  = Column(String(40), unique=True, nullable=False, index=True)  # immutable
    display_name  = Column(String(120), nullable=False)   # editable
    short_name    = Column(String(40), nullable=True)
    description   = Column(Text, nullable=True)
    icon          = Column(String(40), nullable=True)
    display_order = Column(Integer, default=0)
    status        = Column(String(20), default="active")  # active | inactive
    created_at    = Column(DateTime, default=func.now())
    updated_at    = Column(DateTime, default=func.now(), onupdate=func.now())


class CategoryFeature(Base):
    """Which sidebar features/sections a category exposes (data-driven)."""
    __tablename__ = "category_features"
    __table_args__ = (UniqueConstraint("category_id", "feature_key",
                                        name="uq_catfeature"),)

    id          = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.id"), index=True)
    feature_key = Column(String(40), nullable=False)
    enabled     = Column(Boolean, default=False)
    created_at  = Column(DateTime, default=func.now())
    updated_at  = Column(DateTime, default=func.now(), onupdate=func.now())


class TeacherCategory(Base):
    """Teacher <-> Category assignment (many-to-many)."""
    __tablename__ = "teacher_categories"
    __table_args__ = (UniqueConstraint("teacher_id", "category_id",
                                        name="uq_teachercat"),)

    id          = Column(Integer, primary_key=True)
    teacher_id  = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), index=True)
    status      = Column(String(20), default="active")
    assigned_at = Column(DateTime, default=func.now())


class CategorySubject(Base):
    """A subject that belongs to a category (e.g. UG-PG -> Economics)."""
    __tablename__ = "category_subjects"

    id            = Column(Integer, primary_key=True)
    category_id   = Column(Integer, ForeignKey("categories.id"), index=True)
    name          = Column(String(120), nullable=False)
    code          = Column(String(40), nullable=True)
    description   = Column(Text, nullable=True)
    display_order = Column(Integer, default=0)
    status        = Column(String(20), default="active")
    # For NIOS backfill we remember the class this subject came from (optional).
    class_level   = Column(String(10), nullable=True)
    created_at    = Column(DateTime, default=func.now())


class TeacherCategorySubject(Base):
    """Teacher <-> (category, subject) assignment."""
    __tablename__ = "teacher_category_subjects"
    __table_args__ = (UniqueConstraint("teacher_id", "category_subject_id",
                                        name="uq_teachercatsubj"),)

    id                  = Column(Integer, primary_key=True)
    teacher_id          = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    category_id         = Column(Integer, ForeignKey("categories.id"), index=True)
    category_subject_id = Column(Integer, ForeignKey("category_subjects.id"), index=True)
    assigned_at         = Column(DateTime, default=func.now())


# ---------------------------------------------------------------------------
# Material Checker — one reusable engine for every category.
# (Tables defined now so the schema is ready; APIs/UI land in a later phase.)
# ---------------------------------------------------------------------------
class MaterialSubmission(Base):
    __tablename__ = "material_submissions"

    id                  = Column(Integer, primary_key=True)
    category_id         = Column(Integer, ForeignKey("categories.id"), index=True)
    category_subject_id = Column(Integer, ForeignKey("category_subjects.id"),
                                 nullable=True, index=True)
    teacher_id          = Column(Integer, ForeignKey("teacher_profiles.id"), index=True)
    title               = Column(String(200), nullable=False)
    material_type       = Column(String(40), nullable=True)   # PPT | Notes | Video | ...
    description         = Column(Text, nullable=True)
    reference           = Column(String(600), nullable=True)
    status              = Column(String(30), default="draft", index=True)
    priority            = Column(String(12), nullable=True)   # low | normal | high
    deadline            = Column(DateTime, nullable=True)
    current_version     = Column(Integer, default=0)
    created_at          = Column(DateTime, default=func.now())
    updated_at          = Column(DateTime, default=func.now(), onupdate=func.now())


class MaterialVersion(Base):
    __tablename__ = "material_versions"

    id               = Column(Integer, primary_key=True)
    submission_id    = Column(Integer, ForeignKey("material_submissions.id"), index=True)
    version_no       = Column(Integer, default=1)
    uploader_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    file_url         = Column(String(600), nullable=True)   # R2 key / URL
    filename         = Column(String(200), nullable=True)
    file_size        = Column(Integer, nullable=True)
    mime             = Column(String(80), nullable=True)
    status_at        = Column(String(30), nullable=True)    # status when this version was cut
    remarks          = Column(Text, nullable=True)
    created_at       = Column(DateTime, default=func.now())


class MaterialMessage(Base):
    """One entry in the threaded review conversation."""
    __tablename__ = "material_messages"

    id               = Column(Integer, primary_key=True)
    submission_id    = Column(Integer, ForeignKey("material_submissions.id"), index=True)
    sender_user_id   = Column(Integer, ForeignKey("users.id"), nullable=True)
    sender_role      = Column(String(20), nullable=True)   # teacher | admin
    message          = Column(Text, nullable=True)
    read_by_teacher  = Column(Boolean, default=False)
    read_by_admin    = Column(Boolean, default=False)
    created_at       = Column(DateTime, default=func.now())


class MaterialAttachment(Base):
    """Attachment on a message or a version (bytes live in R2; only meta here)."""
    __tablename__ = "material_attachments"

    id               = Column(Integer, primary_key=True)
    submission_id    = Column(Integer, ForeignKey("material_submissions.id"), index=True)
    message_id       = Column(Integer, ForeignKey("material_messages.id"),
                              nullable=True, index=True)
    version_id       = Column(Integer, ForeignKey("material_versions.id"),
                              nullable=True, index=True)
    kind             = Column(String(20), default="review")  # review | version | brief
    url              = Column(String(600), default="")       # R2 key / URL
    filename         = Column(String(200), nullable=True)
    mime             = Column(String(80), nullable=True)
    uploader_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at       = Column(DateTime, default=func.now())


class CategoryEvent(Base):
    """Append-only activity timeline for category work (never overwritten)."""
    __tablename__ = "category_events"

    id            = Column(Integer, primary_key=True)
    category_id   = Column(Integer, ForeignKey("categories.id"), index=True)
    teacher_id    = Column(Integer, nullable=True, index=True)
    submission_id = Column(Integer, nullable=True, index=True)
    actor_user_id = Column(Integer, nullable=True)
    actor_role    = Column(String(20), nullable=True)
    action        = Column(String(60), nullable=True)   # submitted | approved | ...
    detail        = Column(Text, nullable=True)
    old_value     = Column(String(200), nullable=True)
    new_value     = Column(String(200), nullable=True)
    created_at    = Column(DateTime, default=func.now())


# ---------------------------------------------------------------------------
# Idempotent backfill — safe to run on every boot. Creates the seed categories,
# their default features, backfills existing teachers into NIOS, and mirrors the
# NIOS subject master + each teacher's existing subjects into the category layer.
# Nothing here alters or deletes existing rows.
# ---------------------------------------------------------------------------
def _ensure_category(db, internal_key, display_name, short_name, order, features):
    cat = db.query(Category).filter(Category.internal_key == internal_key).first()
    if not cat:
        cat = Category(internal_key=internal_key, display_name=display_name,
                       short_name=short_name, display_order=order, status="active")
        db.add(cat); db.flush()
    # ensure a row for every feature in the catalogue (missing ones only)
    existing = {f.feature_key for f in db.query(CategoryFeature)
                .filter(CategoryFeature.category_id == cat.id).all()}
    for key in FEATURE_KEYS:
        if key not in existing:
            db.add(CategoryFeature(category_id=cat.id, feature_key=key,
                                   enabled=(key in features)))
    return cat


def backfill_categories():
    """Create seed categories + features, assign existing teachers to NIOS, and
    mirror NIOS subjects. Fully idempotent and defensive."""
    from database import SessionLocal
    try:
        from models import TeacherProfile, AvailableSubject
    except Exception:
        return
    db = SessionLocal()
    try:
        nios = _ensure_category(db, "nios", "NIOS", "NIOS", 1, NIOS_DEFAULT_FEATURES)
        _ensure_category(db, "du_sol", "DU SOL", "DU SOL", 2, DU_SOL_DEFAULT_FEATURES)
        db.commit()

        # NIOS subject master -> category_subjects (mirror by distinct name+class)
        have = {(s.name, s.class_level) for s in db.query(CategorySubject)
                .filter(CategorySubject.category_id == nios.id).all()}
        order = 0
        name_to_catsubj = {}
        for av in db.query(AvailableSubject).all():
            key = (av.name, av.class_level)
            if key not in have:
                cs = CategorySubject(category_id=nios.id, name=av.name, code=av.code,
                                     class_level=av.class_level, display_order=order,
                                     status="active" if av.is_active else "inactive")
                db.add(cs); db.flush()
                have.add(key)
                order += 1
        db.commit()
        for cs in db.query(CategorySubject).filter(CategorySubject.category_id == nios.id).all():
            name_to_catsubj.setdefault(str(cs.name).strip().lower(), cs)

        # Assign every existing teacher to NIOS + mirror their subjects
        assigned = {tc.teacher_id for tc in db.query(TeacherCategory)
                    .filter(TeacherCategory.category_id == nios.id).all()}
        tcs_have = {(x.teacher_id, x.category_subject_id) for x in
                    db.query(TeacherCategorySubject)
                    .filter(TeacherCategorySubject.category_id == nios.id).all()}
        for tp in db.query(TeacherProfile).all():
            if tp.id not in assigned:
                db.add(TeacherCategory(teacher_id=tp.id, category_id=nios.id, status="active"))
            for subj in (tp.subjects or []):
                cs = name_to_catsubj.get(str(subj).strip().lower())
                if cs and (tp.id, cs.id) not in tcs_have:
                    db.add(TeacherCategorySubject(teacher_id=tp.id, category_id=nios.id,
                                                  category_subject_id=cs.id))
                    tcs_have.add((tp.id, cs.id))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
