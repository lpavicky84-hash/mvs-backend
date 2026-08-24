"""
Category service — the single reusable layer for category access + feature
configuration. Every route (admin or teacher) goes through these helpers so
authorization and feature logic live in ONE place (no scattered
`if category == "DU SOL"` conditions, no display_name used as identity).
"""
import re
from fastapi import HTTPException
from sqlalchemy import func

from category_models import (
    Category, CategoryFeature, TeacherCategory, CategorySubject,
    TeacherCategorySubject, FEATURE_CATALOG, FEATURE_KEYS,
)


# ---------------------------------------------------------------------------
# internal_key generation (immutable once created)
# ---------------------------------------------------------------------------
def slugify_key(name):
    k = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return k or "category"


def unique_key(db, name):
    base = slugify_key(name)
    key, n = base, 1
    while db.query(Category).filter(Category.internal_key == key).first():
        n += 1
        key = "%s_%d" % (base, n)
    return key


# ---------------------------------------------------------------------------
# Category lookups + serialisation
# ---------------------------------------------------------------------------
def get_category(db, ident):
    """Look up by numeric id or by internal_key. Never by display_name."""
    q = db.query(Category)
    if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
        return q.filter(Category.id == int(ident)).first()
    return q.filter(Category.internal_key == str(ident)).first()


def all_categories(db, include_inactive=True):
    q = db.query(Category)
    if not include_inactive:
        q = q.filter(Category.status == "active")
    return q.order_by(Category.display_order, Category.id).all()


def enabled_features(db, category_id):
    return {f.feature_key for f in db.query(CategoryFeature)
            .filter(CategoryFeature.category_id == category_id,
                    CategoryFeature.enabled == True).all()}  # noqa: E712


def feature_on(db, category_id, feature_key):
    row = db.query(CategoryFeature).filter(
        CategoryFeature.category_id == category_id,
        CategoryFeature.feature_key == feature_key).first()
    return bool(row and row.enabled)


def set_features(db, category_id, mapping):
    """mapping = {feature_key: bool}. Only known feature keys are written."""
    changed = 0
    for key, val in (mapping or {}).items():
        if key not in FEATURE_KEYS:
            continue
        row = db.query(CategoryFeature).filter(
            CategoryFeature.category_id == category_id,
            CategoryFeature.feature_key == key).first()
        if not row:
            row = CategoryFeature(category_id=category_id, feature_key=key,
                                  enabled=bool(val))
            db.add(row)
        else:
            row.enabled = bool(val)
        changed += 1
    return changed


def category_dict(db, cat, with_counts=True, with_features=False):
    d = {
        "id": cat.id,
        "internal_key": cat.internal_key,
        "display_name": cat.display_name,
        "short_name": cat.short_name or "",
        "description": cat.description or "",
        "icon": cat.icon or "",
        "display_order": cat.display_order or 0,
        "status": cat.status or "active",
    }
    if with_counts:
        d["teacher_count"] = db.query(func.count(TeacherCategory.id)).filter(
            TeacherCategory.category_id == cat.id).scalar() or 0
        d["subject_count"] = db.query(func.count(CategorySubject.id)).filter(
            CategorySubject.category_id == cat.id,
            CategorySubject.status == "active").scalar() or 0
        d["feature_count"] = db.query(func.count(CategoryFeature.id)).filter(
            CategoryFeature.category_id == cat.id,
            CategoryFeature.enabled == True).scalar() or 0  # noqa: E712
    if with_features:
        on = enabled_features(db, cat.id)
        d["features"] = [{"key": k, "label": lbl, "enabled": k in on}
                         for k, lbl in FEATURE_CATALOG]
    return d


def ensure_feature_rows(db, category_id):
    """Make sure a category has a row for every feature in the catalogue."""
    have = {f.feature_key for f in db.query(CategoryFeature)
            .filter(CategoryFeature.category_id == category_id).all()}
    for key in FEATURE_KEYS:
        if key not in have:
            db.add(CategoryFeature(category_id=category_id, feature_key=key,
                                   enabled=False))


# ---------------------------------------------------------------------------
# Teacher <-> category access (the authorization backbone)
# ---------------------------------------------------------------------------
def teacher_id_for_user(db, user):
    from models import TeacherProfile
    tp = db.query(TeacherProfile).filter(TeacherProfile.user_id == user.id).first()
    return tp.id if tp else None


def teacher_category_ids(db, teacher_id):
    return [tc.category_id for tc in db.query(TeacherCategory).filter(
        TeacherCategory.teacher_id == teacher_id,
        TeacherCategory.status == "active").all()]


def teacher_categories(db, teacher_id):
    """Active categories assigned to a teacher, in display order."""
    ids = teacher_category_ids(db, teacher_id)
    if not ids:
        return []
    return db.query(Category).filter(
        Category.id.in_(ids), Category.status == "active"
    ).order_by(Category.display_order, Category.id).all()


def teacher_in_category(db, teacher_id, category_id):
    return db.query(TeacherCategory).filter(
        TeacherCategory.teacher_id == teacher_id,
        TeacherCategory.category_id == category_id,
        TeacherCategory.status == "active").first() is not None


def assert_teacher_category(db, teacher_id, category_id):
    """403 unless the teacher is assigned to this (active) category."""
    cat = db.query(Category).filter(Category.id == category_id,
                                    Category.status == "active").first()
    if not cat or not teacher_in_category(db, teacher_id, category_id):
        raise HTTPException(status_code=403,
                            detail="You do not have access to this workspace.")
    return cat


def teacher_subject_ids(db, teacher_id, category_id=None):
    q = db.query(TeacherCategorySubject).filter(
        TeacherCategorySubject.teacher_id == teacher_id)
    if category_id is not None:
        q = q.filter(TeacherCategorySubject.category_id == category_id)
    return [x.category_subject_id for x in q.all()]


def assert_teacher_subject(db, teacher_id, category_subject_id):
    """403 unless this subject is assigned to the teacher (and its category too)."""
    cs = db.query(CategorySubject).filter(
        CategorySubject.id == category_subject_id).first()
    if not cs:
        raise HTTPException(status_code=404, detail="Subject not found.")
    assert_teacher_category(db, teacher_id, cs.category_id)
    ok = db.query(TeacherCategorySubject).filter(
        TeacherCategorySubject.teacher_id == teacher_id,
        TeacherCategorySubject.category_subject_id == category_subject_id).first()
    if not ok:
        raise HTTPException(status_code=403,
                            detail="You are not assigned to this subject.")
    return cs


# ---------------------------------------------------------------------------
# Category pay rates (Phase 14) — effective-dated rate resolution.
# NIOS payroll never calls into this.
# ---------------------------------------------------------------------------
def effective_rate(db, work_type_id, on_date):
    """Rate in force for a work type on `on_date`: the latest CategoryPayRate whose
    effective_from is on/before that date. Returns 0.0 if none set yet."""
    from category_models import CategoryPayRate
    r = db.query(CategoryPayRate).filter(
        CategoryPayRate.work_type_id == work_type_id,
        CategoryPayRate.effective_from <= on_date
    ).order_by(CategoryPayRate.effective_from.desc(),
               CategoryPayRate.id.desc()).first()
    return float(r.amount) if r else 0.0
