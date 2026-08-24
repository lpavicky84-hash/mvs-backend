"""
Category management + feature configuration API.

Admin endpoints  (require admin role): manage categories and their features.
Teacher endpoint (require teacher role): list the categories assigned to me
(used by the header workspace switcher).

internal_key is generated once on create and NEVER editable from these routes.
Renaming display_name never touches internal_key, so all foreign keys stay valid.
"""
from fastapi import (APIRouter, Depends, HTTPException, Body, UploadFile,
                     File, Form, Request)
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_db
from security import get_admin, get_teacher
from category_models import (Category, CategoryFeature, FEATURE_CATALOG,
                             DU_SOL_DEFAULT_FEATURES)
import category_service as CS


def admin_guard(request: Request, current_user=Depends(get_admin)):
    """Section-aware admin gate for category endpoints. Full admins (allowed_sections
    is None) pass. Restricted sub-admins must have the section that the request path
    maps to — reusing admin_routes' ADMIN_SECTION_MAP so the new 'categories' and
    'matcheck' sections are enforced at the API layer, not just hidden in the UI.
    Imported lazily to avoid any import-order coupling."""
    try:
        from admin_routes import (ADMIN_SECTION_MAP, ADMIN_SECTION_ALIASES,
                                  admin_allowed_sections)
    except Exception:
        return current_user
    allowed = admin_allowed_sections(current_user)
    path = request.url.path
    if allowed is not None and path.startswith("/api/admin/"):
        parts = path.split("/")
        first = parts[3] if len(parts) > 3 else ""
        sec = ADMIN_SECTION_MAP.get(first)
        if sec is not None:
            acceptable = ADMIN_SECTION_ALIASES.get(sec, {sec})
            if allowed.isdisjoint(acceptable):
                raise HTTPException(status_code=403,
                                    detail="You do not have access to this section.")
    return current_user

router = APIRouter(tags=["Categories"])


# ===========================================================================
# ADMIN — categories
# ===========================================================================
@router.get("/api/admin/categories")
def admin_list_categories(db: Session = Depends(get_db), _=Depends(admin_guard)):
    cats = CS.all_categories(db, include_inactive=True)
    return {"categories": [CS.category_dict(db, c) for c in cats],
            "features": [{"key": k, "label": lbl} for k, lbl in FEATURE_CATALOG]}


@router.post("/api/admin/categories")
def admin_create_category(payload: dict = Body(...),
                          db: Session = Depends(get_db), _=Depends(admin_guard)):
    name = str((payload or {}).get("display_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Display name is required.")
    key = str((payload or {}).get("internal_key") or "").strip() or CS.unique_key(db, name)
    key = CS.slugify_key(key)
    if db.query(Category).filter(Category.internal_key == key).first():
        key = CS.unique_key(db, key)
    order = payload.get("display_order")
    if order is None:
        order = (db.query(Category).count() or 0) + 1
    cat = Category(
        internal_key=key, display_name=name,
        short_name=str(payload.get("short_name") or "").strip() or name,
        description=str(payload.get("description") or "").strip() or None,
        icon=str(payload.get("icon") or "").strip() or None,
        display_order=int(order), status="active")
    db.add(cat)
    db.flush()
    # seed feature rows; new categories start with a sensible professional default
    default_on = set(payload.get("features") or []) or set(DU_SOL_DEFAULT_FEATURES)
    for key_, _lbl in FEATURE_CATALOG:
        db.add(CategoryFeature(category_id=cat.id, feature_key=key_,
                               enabled=(key_ in default_on)))
    db.commit()
    return {"ok": True, "category": CS.category_dict(db, cat, with_features=True)}


@router.patch("/api/admin/categories/{cid}")
def admin_edit_category(cid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(admin_guard)):
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    # internal_key is immutable — silently ignored if sent
    if "display_name" in payload and str(payload["display_name"]).strip():
        cat.display_name = str(payload["display_name"]).strip()
    if "short_name" in payload:
        cat.short_name = str(payload["short_name"]).strip() or None
    if "description" in payload:
        cat.description = str(payload["description"]).strip() or None
    if "icon" in payload:
        cat.icon = str(payload["icon"]).strip() or None
    if "display_order" in payload and payload["display_order"] is not None:
        cat.display_order = int(payload["display_order"])
    if "status" in payload and str(payload["status"]) in ("active", "inactive"):
        cat.status = str(payload["status"])
    db.commit()
    return {"ok": True, "category": CS.category_dict(db, cat)}


@router.post("/api/admin/categories/{cid}/status")
def admin_toggle_category(cid: int, payload: dict = Body(...),
                          db: Session = Depends(get_db), _=Depends(admin_guard)):
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    st = str((payload or {}).get("status") or "").strip()
    cat.status = "inactive" if st == "inactive" else "active"
    db.commit()
    return {"ok": True, "status": cat.status}


# ===========================================================================
# ADMIN — feature configuration
# ===========================================================================
@router.get("/api/admin/categories/{cid}/features")
def admin_get_features(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    CS.ensure_feature_rows(db, cat.id)
    db.commit()
    return {"category": CS.category_dict(db, cat, with_counts=False, with_features=True)}


@router.put("/api/admin/categories/{cid}/features")
def admin_set_features(cid: int, payload: dict = Body(...),
                       db: Session = Depends(get_db), _=Depends(admin_guard)):
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    mapping = payload.get("features")
    if not isinstance(mapping, dict):
        raise HTTPException(status_code=400,
                            detail="Send { features: { feature_key: true/false } }.")
    CS.set_features(db, cat.id, mapping)
    db.commit()
    return {"ok": True,
            "category": CS.category_dict(db, cat, with_counts=False, with_features=True)}


# ===========================================================================
# ADMIN — assign categories + subjects to a teacher (Phase 6)
# ===========================================================================
@router.get("/api/admin/teachers/{tid}/category-access")
def admin_teacher_category_access(tid: int, db: Session = Depends(get_db),
                                  _=Depends(admin_guard)):
    from models import TeacherProfile
    from category_models import (CategorySubject, TeacherCategory,
                                 TeacherCategorySubject)
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found.")
    assigned_cats = {tc.category_id for tc in db.query(TeacherCategory)
                     .filter(TeacherCategory.teacher_id == tid).all()}
    assigned_subs = {x.category_subject_id for x in db.query(TeacherCategorySubject)
                     .filter(TeacherCategorySubject.teacher_id == tid).all()}
    out = []
    for c in CS.all_categories(db, include_inactive=False):
        subs = db.query(CategorySubject).filter(
            CategorySubject.category_id == c.id,
            CategorySubject.status == "active"
        ).order_by(CategorySubject.display_order, CategorySubject.name).all()
        out.append({
            "id": c.id, "internal_key": c.internal_key,
            "display_name": c.display_name,
            "assigned": c.id in assigned_cats,
            "subjects": [{"id": s.id, "name": s.name, "code": s.code or "",
                          "assigned": s.id in assigned_subs} for s in subs],
        })
    return {"teacher_id": tid, "categories": out}


@router.put("/api/admin/teachers/{tid}/category-access")
def admin_set_teacher_category_access(tid: int, payload: dict = Body(...),
                                      db: Session = Depends(get_db),
                                      _=Depends(admin_guard)):
    from models import TeacherProfile
    from category_models import (CategorySubject, TeacherCategory,
                                 TeacherCategorySubject)
    tp = db.query(TeacherProfile).filter(TeacherProfile.id == tid).first()
    if not tp:
        raise HTTPException(status_code=404, detail="Teacher not found.")
    access = (payload or {}).get("access") or []
    want_cats, want_subs = set(), set()
    for a in access:
        try:
            cid = int(a.get("category_id"))
        except Exception:
            continue
        want_cats.add(cid)
        for sid in (a.get("subject_ids") or []):
            try:
                want_subs.add((cid, int(sid)))
            except Exception:
                pass
    # sync category assignments
    existing_tc = {tc.category_id: tc for tc in db.query(TeacherCategory)
                   .filter(TeacherCategory.teacher_id == tid).all()}
    for cid in want_cats:
        if cid in existing_tc:
            existing_tc[cid].status = "active"
        else:
            db.add(TeacherCategory(teacher_id=tid, category_id=cid, status="active"))
    for cid, tc in existing_tc.items():
        if cid not in want_cats:
            db.delete(tc)
    # sync subject assignments
    existing_tcs = {(x.category_id, x.category_subject_id): x
                    for x in db.query(TeacherCategorySubject)
                    .filter(TeacherCategorySubject.teacher_id == tid).all()}
    for (cid, sid) in want_subs:
        if cid in want_cats and (cid, sid) not in existing_tcs:
            db.add(TeacherCategorySubject(teacher_id=tid, category_id=cid,
                                          category_subject_id=sid))
    for key, x in existing_tcs.items():
        if key[0] not in want_cats or key not in want_subs:
            db.delete(x)
    db.commit()
    return {"ok": True}


# ===========================================================================
# ADMIN — category subjects (Phase 8)
# ===========================================================================
@router.get("/api/admin/categories/{cid}/subjects")
def admin_list_cat_subjects(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategorySubject
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    subs = db.query(CategorySubject).filter(CategorySubject.category_id == cid) \
        .order_by(CategorySubject.display_order, CategorySubject.name).all()
    return {"category": {"id": cat.id, "display_name": cat.display_name,
                         "internal_key": cat.internal_key},
            "subjects": [{"id": s.id, "name": s.name, "code": s.code or "",
                          "description": s.description or "",
                          "display_order": s.display_order or 0,
                          "status": s.status or "active"} for s in subs]}


@router.post("/api/admin/categories/{cid}/subjects")
def admin_add_cat_subject(cid: int, payload: dict = Body(...),
                          db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategorySubject
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    name = str((payload or {}).get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Subject name is required.")
    order = payload.get("display_order")
    if order is None:
        order = (db.query(CategorySubject).filter(
            CategorySubject.category_id == cid).count() or 0) + 1
    s = CategorySubject(category_id=cid, name=name,
                        code=str(payload.get("code") or "").strip() or None,
                        description=str(payload.get("description") or "").strip() or None,
                        display_order=int(order), status="active")
    db.add(s)
    db.commit()
    return {"ok": True, "id": s.id}


@router.patch("/api/admin/category-subjects/{sid}")
def admin_edit_cat_subject(sid: int, payload: dict = Body(...),
                           db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategorySubject
    s = db.query(CategorySubject).filter(CategorySubject.id == sid).first()
    if not s:
        raise HTTPException(status_code=404, detail="Subject not found.")
    if "name" in payload and str(payload["name"]).strip():
        s.name = str(payload["name"]).strip()
    if "code" in payload:
        s.code = str(payload["code"]).strip() or None
    if "description" in payload:
        s.description = str(payload["description"]).strip() or None
    if "display_order" in payload and payload["display_order"] is not None:
        s.display_order = int(payload["display_order"])
    if "status" in payload and str(payload["status"]) in ("active", "inactive"):
        s.status = str(payload["status"])
    db.commit()
    return {"ok": True}


# ===========================================================================
# TEACHER — my subjects in the active category (Phase 8)
# ===========================================================================
@router.get("/api/teacher/subjects")
def teacher_my_subjects(category_id: int, db: Session = Depends(get_db),
                        me=Depends(get_teacher)):
    from category_models import (CategorySubject, TeacherCategorySubject,
                                 MaterialSubmission)
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        return {"subjects": []}
    CS.assert_teacher_category(db, tid, category_id)   # 403 if not assigned
    sub_ids = [x.category_subject_id for x in db.query(TeacherCategorySubject).filter(
        TeacherCategorySubject.teacher_id == tid,
        TeacherCategorySubject.category_id == category_id).all()]
    if not sub_ids:
        return {"subjects": []}
    subs = db.query(CategorySubject).filter(
        CategorySubject.id.in_(sub_ids),
        CategorySubject.status == "active"
    ).order_by(CategorySubject.display_order, CategorySubject.name).all()
    out = []
    for s in subs:
        base = db.query(MaterialSubmission).filter(
            MaterialSubmission.teacher_id == tid,
            MaterialSubmission.category_subject_id == s.id)
        total = base.count()
        approved = base.filter(MaterialSubmission.status == "approved").count()
        pending = base.filter(MaterialSubmission.status.in_(
            ["submitted", "under_review", "changes_required", "resubmitted"])).count()
        out.append({"id": s.id, "name": s.name, "code": s.code or "",
                    "description": s.description or "",
                    "materials": total, "approved": approved, "pending": pending})
    return {"subjects": out}


# ===========================================================================
# CATEGORY PAYOUT (Phase 14) — additive; NIOS payroll engine untouched
# ===========================================================================
def _month_bounds(month):
    """'YYYY-MM' -> (first_date, last_date). Defaults to current month."""
    from datetime import date
    import calendar
    try:
        y, m = int(month[:4]), int(month[5:7])
    except Exception:
        t = date.today(); y, m = t.year, t.month
    last = calendar.monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)


@router.get("/api/admin/categories/{cid}/work-types")
def admin_list_work_types(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryWorkType, CategoryPayRate
    from datetime import date
    wts = db.query(CategoryWorkType).filter(CategoryWorkType.category_id == cid) \
        .order_by(CategoryWorkType.display_order, CategoryWorkType.id).all()
    out = []
    for w in wts:
        cur = CS.effective_rate(db, w.id, date.today())
        rates = db.query(CategoryPayRate).filter(CategoryPayRate.work_type_id == w.id) \
            .order_by(CategoryPayRate.effective_from.desc(), CategoryPayRate.id.desc()).all()
        out.append({"id": w.id, "key": w.key or "", "label": w.label,
                    "unit": w.unit or "per_item", "source": w.source or "manual",
                    "is_active": bool(w.is_active), "current_rate": cur,
                    "rates": [{"id": r.id, "amount": float(r.amount or 0),
                               "effective_from": str(r.effective_from or "")[:10],
                               "note": r.note or ""} for r in rates]})
    return {"work_types": out}


@router.post("/api/admin/categories/{cid}/work-types")
def admin_add_work_type(cid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryWorkType
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    label = str((payload or {}).get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label is required.")
    src = str(payload.get("source") or "manual")
    if src not in ("auto_material", "manual"):
        src = "manual"
    order = (db.query(CategoryWorkType).filter(
        CategoryWorkType.category_id == cid).count() or 0) + 1
    w = CategoryWorkType(category_id=cid, key=CS.slugify_key(label),
                         label=label, unit=str(payload.get("unit") or "per_item"),
                         source=src, display_order=order, is_active=True)
    db.add(w)
    db.commit()
    return {"ok": True, "id": w.id}


@router.patch("/api/admin/work-types/{wid}")
def admin_edit_work_type(wid: int, payload: dict = Body(...),
                         db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryWorkType
    w = db.query(CategoryWorkType).filter(CategoryWorkType.id == wid).first()
    if not w:
        raise HTTPException(status_code=404, detail="Work type not found.")
    if "label" in payload and str(payload["label"]).strip():
        w.label = str(payload["label"]).strip()
    if "unit" in payload:
        w.unit = str(payload["unit"]) or "per_item"
    if "is_active" in payload:
        w.is_active = bool(payload["is_active"])
    db.commit()
    return {"ok": True}


@router.post("/api/admin/work-types/{wid}/rates")
def admin_add_rate(wid: int, payload: dict = Body(...),
                   db: Session = Depends(get_db), me=Depends(admin_guard)):
    from category_models import CategoryWorkType, CategoryPayRate
    w = db.query(CategoryWorkType).filter(CategoryWorkType.id == wid).first()
    if not w:
        raise HTTPException(status_code=404, detail="Work type not found.")
    try:
        amount = float(payload.get("amount"))
    except Exception:
        raise HTTPException(status_code=400, detail="Valid amount required.")
    eff = _parse_deadline(str(payload.get("effective_from") or "") + "T00:00:00")
    eff = eff.date() if eff else __import__("datetime").date.today()
    db.add(CategoryPayRate(work_type_id=wid, category_id=w.category_id, amount=amount,
                           effective_from=eff, note=str(payload.get("note") or "").strip() or None,
                           created_by=me.id))
    db.commit()
    return {"ok": True}


@router.get("/api/teacher/category-earnings")
def teacher_category_earnings(category_id: int, month: str = "",
                              db: Session = Depends(get_db), me=Depends(get_teacher)):
    from category_models import (CategoryWorkType, MaterialSubmission)
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        raise HTTPException(status_code=404, detail="Teacher profile not found.")
    cat = CS.assert_teacher_category(db, tid, category_id)
    first, last = _month_bounds(month)
    mkey = first.strftime("%Y-%m")

    wts = db.query(CategoryWorkType).filter(
        CategoryWorkType.category_id == category_id,
        CategoryWorkType.is_active == True).order_by(  # noqa: E712
        CategoryWorkType.display_order).all()
    lines = []
    total = 0.0
    for w in wts:
        rate = CS.effective_rate(db, w.id, last)
        units = 0
        if w.source == "auto_material":
            # approved materials whose approval landed in this month (by updated_at)
            q = db.query(MaterialSubmission).filter(
                MaterialSubmission.teacher_id == tid,
                MaterialSubmission.category_id == category_id,
                MaterialSubmission.status == "approved")
            units = sum(1 for m in q.all()
                        if first <= (m.updated_at.date() if m.updated_at else first) <= last)
        amount = round(units * rate, 2)
        total += amount
        lines.append({"work_type": w.label, "unit": w.unit or "per_item",
                      "source": w.source or "manual", "rate": rate,
                      "units": units, "amount": amount,
                      "auto": (w.source == "auto_material")})
    return {"category": {"id": cat.id, "display_name": cat.display_name,
                         "internal_key": cat.internal_key},
            "month": mkey, "lines": lines, "total": round(total, 2),
            "note": "Estimated workspace earnings based on approved work and the rate "
                    "in effect. This is separate from your main salary/payroll."}


# ===========================================================================
# TEACHER — category performance (Phase 13; NIOS performance untouched)
# ===========================================================================
@router.get("/api/teacher/category-performance")
def teacher_category_performance(category_id: int, db: Session = Depends(get_db),
                                 me=Depends(get_teacher)):
    from category_models import (MaterialSubmission, CategorySubject,
                                 TeacherCategorySubject)
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        raise HTTPException(status_code=404, detail="Teacher profile not found.")
    cat = CS.assert_teacher_category(db, tid, category_id)

    base = db.query(MaterialSubmission).filter(
        MaterialSubmission.teacher_id == tid,
        MaterialSubmission.category_id == category_id)

    def _c(*st):
        return base.filter(MaterialSubmission.status.in_(list(st))).count()

    total = base.count()
    approved = _c("approved")
    rejected = _c("rejected")
    changes = _c("changes_required")
    pending = _c("submitted", "under_review", "resubmitted")
    decided = approved + rejected
    approval_rate = round(approved * 100.0 / decided, 1) if decided else 0.0

    # subject breakdown (only the teacher's assigned subjects in this category)
    sub_ids = [x.category_subject_id for x in db.query(TeacherCategorySubject).filter(
        TeacherCategorySubject.teacher_id == tid,
        TeacherCategorySubject.category_id == category_id).all()]
    subjects = []
    if sub_ids:
        for s in db.query(CategorySubject).filter(
                CategorySubject.id.in_(sub_ids)).order_by(
                CategorySubject.display_order, CategorySubject.name).all():
            sb = base.filter(MaterialSubmission.category_subject_id == s.id)
            s_pending = sb.filter(MaterialSubmission.status.in_(
                ["submitted", "under_review", "resubmitted", "changes_required"])).count()
            subjects.append({"name": s.name, "code": s.code or "",
                             "total": sb.count(),
                             "approved": sb.filter(MaterialSubmission.status == "approved").count(),
                             "pending": s_pending})

    return {
        "category": {"id": cat.id, "internal_key": cat.internal_key,
                     "display_name": cat.display_name},
        "totals": {"total": total, "approved": approved, "rejected": rejected,
                   "changes": changes, "pending": pending,
                   "in_review": pending + changes},
        "approval_rate": approval_rate,
        "subjects": subjects,
    }


# ===========================================================================
# MATERIAL CHECKER (Phase 12B) — threaded review chat + attachments
# ===========================================================================
def _att_dict(a):
    return {"id": a.id, "kind": a.kind or "file", "filename": a.filename or "file",
            "mime": a.mime or "", "is_image": (a.mime or "").startswith("image/")}


def _msg_dict(db, msg):
    from category_models import MaterialAttachment
    atts = db.query(MaterialAttachment).filter(
        MaterialAttachment.message_id == msg.id).all()
    return {"id": msg.id, "sender_role": msg.sender_role or "",
            "sender_user_id": msg.sender_user_id,
            "message": msg.message or "",
            "at": str(msg.created_at or "")[:19],
            "read_by_teacher": bool(msg.read_by_teacher),
            "read_by_admin": bool(msg.read_by_admin),
            "attachments": [_att_dict(a) for a in atts]}


def _get_submission_for(db, sid, is_admin, user):
    from category_models import MaterialSubmission
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == sid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if not is_admin:
        tid = CS.teacher_id_for_user(db, user)
        if not tid or m.teacher_id != tid:
            raise HTTPException(status_code=404, detail="Submission not found.")
    return m


async def _post_message(db, m, sender_role, sender_id, message, files):
    from category_models import MaterialMessage, MaterialAttachment
    msg = MaterialMessage(submission_id=m.id, sender_user_id=sender_id,
                          sender_role=sender_role, message=(message or "").strip() or None,
                          read_by_teacher=(sender_role == "teacher"),
                          read_by_admin=(sender_role == "admin"))
    db.add(msg)
    db.flush()
    r2 = __import__("r2_storage")
    for f in (files or []):
        if not f:
            continue
        raw = await f.read()
        if not raw:
            continue
        ct = f.content_type or "application/octet-stream"
        ref = r2.store_file_value(r2.new_key("mat-chat", f.filename or "file"), raw, ct)
        db.add(MaterialAttachment(submission_id=m.id, message_id=msg.id,
                                  kind=("image" if ct.startswith("image/") else "file"),
                                  url=ref, filename=f.filename or "file", mime=ct,
                                  uploader_user_id=sender_id))
    _log_event(db, m.category_id, m.teacher_id, m.id, sender_id, sender_role,
               "message", (message or "")[:120] or "attachment")
    db.commit()
    return msg


@router.get("/api/teacher/material-submissions/{sid}/messages")
def teacher_list_messages(sid: int, db: Session = Depends(get_db), me=Depends(get_teacher)):
    from category_models import MaterialMessage
    m = _get_submission_for(db, sid, False, me)
    msgs = db.query(MaterialMessage).filter(MaterialMessage.submission_id == sid) \
        .order_by(MaterialMessage.created_at).all()
    # mark admin messages as read by teacher
    changed = False
    for x in msgs:
        if x.sender_role == "admin" and not x.read_by_teacher:
            x.read_by_teacher = True
            changed = True
    if changed:
        db.commit()
    return {"messages": [_msg_dict(db, x) for x in msgs]}


@router.post("/api/teacher/material-submissions/{sid}/messages")
async def teacher_post_message(sid: int, message: str = Form(""),
                               files: list[UploadFile] = File(default=[]),
                               db: Session = Depends(get_db), me=Depends(get_teacher)):
    m = _get_submission_for(db, sid, False, me)
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_message(db, m, "teacher", me.id, message, files)
    return {"ok": True, "message": _msg_dict(db, msg)}


@router.get("/api/admin/material-submissions/{sid}/messages")
def admin_list_messages(sid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import MaterialMessage
    _get_submission_for(db, sid, True, None)
    msgs = db.query(MaterialMessage).filter(MaterialMessage.submission_id == sid) \
        .order_by(MaterialMessage.created_at).all()
    changed = False
    for x in msgs:
        if x.sender_role == "teacher" and not x.read_by_admin:
            x.read_by_admin = True
            changed = True
    if changed:
        db.commit()
    return {"messages": [_msg_dict(db, x) for x in msgs]}


@router.post("/api/admin/material-submissions/{sid}/messages")
async def admin_post_message(sid: int, message: str = Form(""),
                             files: list[UploadFile] = File(default=[]),
                             db: Session = Depends(get_db), me=Depends(admin_guard)):
    m = _get_submission_for(db, sid, True, None)
    if not (message or "").strip() and not files:
        raise HTTPException(status_code=400, detail="Empty message.")
    msg = await _post_message(db, m, "admin", me.id, message, files)
    return {"ok": True, "message": _msg_dict(db, msg)}


def _attachment_response(db, aid, is_admin, user, download):
    from category_models import MaterialAttachment
    a = db.query(MaterialAttachment).filter(MaterialAttachment.id == aid).first()
    if not a:
        raise HTTPException(status_code=404, detail="Attachment not found.")
    _get_submission_for(db, a.submission_id, is_admin, user)   # authorizes
    r2 = __import__("r2_storage")
    return r2.file_response(a.url, a.mime or "application/octet-stream",
                            a.filename or "file", download)


@router.get("/api/admin/material-attachments/{aid}/view")
def admin_view_attachment(aid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    return _attachment_response(db, aid, True, None, False)


@router.get("/api/admin/material-attachments/{aid}/download")
def admin_download_attachment(aid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    return _attachment_response(db, aid, True, None, True)


@router.get("/api/teacher/material-attachments/{aid}/view")
def teacher_view_attachment(aid: int, db: Session = Depends(get_db), me=Depends(get_teacher)):
    return _attachment_response(db, aid, False, me, False)


@router.get("/api/teacher/material-attachments/{aid}/download")
def teacher_download_attachment(aid: int, db: Session = Depends(get_db), me=Depends(get_teacher)):
    return _attachment_response(db, aid, False, me, True)


# ===========================================================================
# MATERIAL CHECKER (Phase 12A) — submissions, versions, status, review
# ===========================================================================
_MS_ACTIVE = ["submitted", "under_review", "changes_required", "resubmitted"]
_MS_DECISIONS = {"under_review", "changes_required", "approved", "rejected"}


def _parse_deadline(v):
    if not v:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(v).replace("Z", "").strip()[:19])
    except Exception:
        return None


def _log_event(db, category_id, teacher_id, submission_id, actor_id, actor_role,
               action, detail=None):
    from category_models import CategoryEvent
    try:
        db.add(CategoryEvent(category_id=category_id, teacher_id=teacher_id,
                             submission_id=submission_id, actor_user_id=actor_id,
                             actor_role=actor_role, action=action, detail=detail))
    except Exception:
        pass


def _sub_dict(db, m, with_versions=False, teacher_name=None):
    from category_models import MaterialVersion, CategorySubject
    subj = None
    if m.category_subject_id:
        s = db.query(CategorySubject).filter(CategorySubject.id == m.category_subject_id).first()
        subj = s.name if s else None
    d = {"id": m.id, "title": m.title, "material_type": m.material_type or "",
         "description": m.description or "", "reference": m.reference or "",
         "status": m.status, "priority": m.priority or "normal",
         "deadline": str(m.deadline or "")[:19] or None,
         "current_version": m.current_version or 0,
         "category_id": m.category_id, "subject_id": m.category_subject_id,
         "subject": subj or "", "teacher_id": m.teacher_id,
         "created_at": str(m.created_at or "")[:19],
         "updated_at": str(m.updated_at or "")[:19]}
    if teacher_name is not None:
        d["teacher"] = teacher_name
    if with_versions:
        vs = db.query(MaterialVersion).filter(MaterialVersion.submission_id == m.id) \
            .order_by(MaterialVersion.version_no).all()
        d["versions"] = [{"id": v.id, "version_no": v.version_no,
                          "filename": v.filename or "", "file_size": v.file_size or 0,
                          "status_at": v.status_at or "", "remarks": v.remarks or "",
                          "created_at": str(v.created_at or "")[:19]} for v in vs]
    return d


def _add_version(db, submission, raw, file, uploader_id, status_at, remarks=None):
    from category_models import MaterialVersion
    r2 = __import__("r2_storage")
    ref = r2.store_file_value(r2.new_key("mat-versions", file.filename or "file"),
                              raw, file.content_type or "application/octet-stream")
    vno = (submission.current_version or 0) + 1
    v = MaterialVersion(submission_id=submission.id, version_no=vno,
                        uploader_user_id=uploader_id, file_url=ref,
                        filename=file.filename or "file", file_size=len(raw),
                        mime=file.content_type or "", status_at=status_at, remarks=remarks)
    db.add(v)
    submission.current_version = vno
    return v


# ---- Teacher ----
@router.post("/api/teacher/material-submissions")
async def teacher_submit_material(file: UploadFile = File(...),
                                  category_id: str = Form(...), title: str = Form(...),
                                  subject_id: str = Form(""), material_type: str = Form("resource"),
                                  description: str = Form(""), reference: str = Form(""),
                                  db: Session = Depends(get_db), me=Depends(get_teacher)):
    from category_models import MaterialSubmission
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        raise HTTPException(status_code=403, detail="No teacher profile.")
    cid = int(category_id) if str(category_id).isdigit() else 0
    CS.assert_teacher_category(db, tid, cid)
    sid = None
    if str(subject_id).strip().isdigit():
        sid = int(subject_id)
        CS.assert_teacher_subject(db, tid, sid)
    m = MaterialSubmission(category_id=cid, category_subject_id=sid, teacher_id=tid,
                           title=str(title).strip() or (file.filename or "Material"),
                           material_type=material_type, description=str(description).strip() or None,
                           reference=str(reference).strip() or None, status="submitted",
                           priority="normal", current_version=0)
    db.add(m)
    db.flush()
    raw = await file.read()
    _add_version(db, m, raw, file, me.id, "submitted")
    _log_event(db, cid, tid, m.id, me.id, "teacher", "submitted", m.title)
    db.commit()
    return {"ok": True, "id": m.id}


@router.get("/api/teacher/material-submissions")
def teacher_list_submissions(category_id: int, db: Session = Depends(get_db),
                             me=Depends(get_teacher)):
    from category_models import MaterialSubmission
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        return {"submissions": []}
    CS.assert_teacher_category(db, tid, category_id)
    subs = db.query(MaterialSubmission).filter(
        MaterialSubmission.teacher_id == tid,
        MaterialSubmission.category_id == category_id
    ).order_by(MaterialSubmission.updated_at.desc()).all()
    return {"submissions": [_sub_dict(db, m) for m in subs]}


@router.get("/api/teacher/material-submissions/{sid}")
def teacher_submission_detail(sid: int, db: Session = Depends(get_db),
                              me=Depends(get_teacher)):
    from category_models import MaterialSubmission
    tid = CS.teacher_id_for_user(db, me)
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == sid).first()
    if not m or m.teacher_id != tid:
        raise HTTPException(status_code=404, detail="Submission not found.")
    return {"submission": _sub_dict(db, m, with_versions=True)}


@router.post("/api/teacher/material-submissions/{sid}/resubmit")
async def teacher_resubmit(sid: int, file: UploadFile = File(...),
                           remarks: str = Form(""), db: Session = Depends(get_db),
                           me=Depends(get_teacher)):
    from category_models import MaterialSubmission
    tid = CS.teacher_id_for_user(db, me)
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == sid).first()
    if not m or m.teacher_id != tid:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if m.status in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail="This submission is closed.")
    raw = await file.read()
    _add_version(db, m, raw, file, me.id, "resubmitted", str(remarks).strip() or None)
    m.status = "resubmitted"
    _log_event(db, m.category_id, tid, m.id, me.id, "teacher", "resubmitted", m.title)
    db.commit()
    return {"ok": True, "version": m.current_version}


# ---- Admin ----
@router.get("/api/admin/material-submissions")
def admin_review_queue(category_id: int = 0, status: str = "",
                       db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import MaterialSubmission
    from models import TeacherProfile, User
    q = db.query(MaterialSubmission)
    if category_id:
        q = q.filter(MaterialSubmission.category_id == category_id)
    if status:
        q = q.filter(MaterialSubmission.status == status)
    subs = q.order_by(MaterialSubmission.updated_at.desc()).limit(300).all()
    # teacher names
    tids = list({m.teacher_id for m in subs})
    names = {}
    if tids:
        rows = db.query(TeacherProfile.id, User.name).join(
            User, User.id == TeacherProfile.user_id).filter(
            TeacherProfile.id.in_(tids)).all()
        names = {r[0]: r[1] for r in rows}
    return {"submissions": [_sub_dict(db, m, teacher_name=names.get(m.teacher_id, ""))
                            for m in subs]}


@router.get("/api/admin/material-submissions/{sid}")
def admin_submission_detail(sid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import MaterialSubmission
    from models import TeacherProfile, User
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == sid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Submission not found.")
    row = db.query(User.name).join(TeacherProfile, TeacherProfile.user_id == User.id) \
        .filter(TeacherProfile.id == m.teacher_id).first()
    return {"submission": _sub_dict(db, m, with_versions=True,
                                    teacher_name=(row[0] if row else ""))}


@router.post("/api/admin/material-submissions/{sid}/review")
def admin_review_submission(sid: int, payload: dict = Body(...),
                            db: Session = Depends(get_db), me=Depends(admin_guard)):
    from category_models import MaterialSubmission, MaterialVersion
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == sid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Submission not found.")
    decision = str((payload or {}).get("decision") or "").strip()
    if decision and decision not in _MS_DECISIONS:
        raise HTTPException(status_code=400, detail="Invalid decision.")
    if decision:
        m.status = decision
    if "priority" in payload and str(payload["priority"]) in ("low", "normal", "high"):
        m.priority = str(payload["priority"])
    if "deadline" in payload:
        m.deadline = _parse_deadline(payload.get("deadline"))
    remarks = str((payload or {}).get("remarks") or "").strip()
    if remarks:
        cur = db.query(MaterialVersion).filter(
            MaterialVersion.submission_id == m.id).order_by(
            MaterialVersion.version_no.desc()).first()
        if cur:
            cur.remarks = ((cur.remarks + "\n") if cur.remarks else "") + remarks
    _log_event(db, m.category_id, m.teacher_id, m.id, me.id, "admin",
               decision or "reviewed", remarks or None)
    db.commit()
    return {"ok": True, "status": m.status}


# ---- version download (both roles, authorized) ----
def _download_version(db, vid, is_admin, user):
    from category_models import MaterialVersion, MaterialSubmission
    v = db.query(MaterialVersion).filter(MaterialVersion.id == vid).first()
    if not v:
        raise HTTPException(status_code=404, detail="Version not found.")
    m = db.query(MaterialSubmission).filter(MaterialSubmission.id == v.submission_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Submission not found.")
    if not is_admin:
        tid = CS.teacher_id_for_user(db, user)
        if not tid or m.teacher_id != tid:
            raise HTTPException(status_code=403, detail="Not your submission.")
    r2 = __import__("r2_storage")
    return r2.file_response(v.file_url, v.mime or "application/octet-stream",
                            v.filename or "file", True)


@router.get("/api/admin/material-versions/{vid}/download")
def admin_download_version(vid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    return _download_version(db, vid, True, None)


@router.get("/api/teacher/material-versions/{vid}/download")
def teacher_download_version(vid: int, db: Session = Depends(get_db), me=Depends(get_teacher)):
    return _download_version(db, vid, False, me)


# ===========================================================================
# SUBJECT MATERIALS (Phase 11) — admin upload, teacher secure view
# ===========================================================================
_MAT_TYPES = {"notes", "book", "pdf", "ppt", "qbank", "reference", "resource"}


def _mat_dict(m, subj_name=None):
    return {"id": m.id, "title": m.title, "material_type": m.material_type or "resource",
            "description": m.description or "", "filename": m.filename or "",
            "file_size": m.file_size or 0, "mime": m.mime or "",
            "version": m.version or 1, "subject_id": m.category_subject_id,
            "subject": subj_name or "", "is_active": bool(m.is_active),
            "created_at": str(m.created_at or "")[:19]}


@router.post("/api/admin/categories/{cid}/materials")
async def admin_upload_material(cid: int, file: UploadFile = File(...),
                                title: str = Form(...), material_type: str = Form("resource"),
                                subject_id: str = Form(""), description: str = Form(""),
                                db: Session = Depends(get_db), me=Depends(admin_guard)):
    from category_models import CategoryMaterial, CategorySubject
    cat = db.query(Category).filter(Category.id == cid).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    sid = None
    if str(subject_id).strip().isdigit():
        sid = int(subject_id)
        s = db.query(CategorySubject).filter(CategorySubject.id == sid,
                                             CategorySubject.category_id == cid).first()
        if not s:
            raise HTTPException(status_code=400, detail="Subject not in this category.")
    mtype = material_type if material_type in _MAT_TYPES else "resource"
    raw = await file.read()
    r2 = __import__("r2_storage")
    ref = r2.store_file_value(r2.new_key("cat-materials", file.filename or "file"),
                              raw, file.content_type or "application/octet-stream")
    m = CategoryMaterial(category_id=cid, category_subject_id=sid,
                         title=str(title).strip() or (file.filename or "Material"),
                         material_type=mtype, description=str(description).strip() or None,
                         filename=file.filename or "file", file_ref=ref,
                         file_size=len(raw), mime=file.content_type or "",
                         uploaded_by=me.id, is_active=True)
    db.add(m)
    db.commit()
    return {"ok": True, "id": m.id}


@router.get("/api/admin/categories/{cid}/materials")
def admin_list_materials(cid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryMaterial, CategorySubject
    subj_name = {s.id: s.name for s in db.query(CategorySubject).filter(
        CategorySubject.category_id == cid).all()}
    mats = db.query(CategoryMaterial).filter(CategoryMaterial.category_id == cid) \
        .order_by(CategoryMaterial.created_at.desc()).all()
    return {"materials": [_mat_dict(m, subj_name.get(m.category_subject_id)) for m in mats]}


@router.patch("/api/admin/category-materials/{mid}")
def admin_edit_material(mid: int, payload: dict = Body(...),
                        db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryMaterial
    m = db.query(CategoryMaterial).filter(CategoryMaterial.id == mid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Material not found.")
    if "title" in payload and str(payload["title"]).strip():
        m.title = str(payload["title"]).strip()
    if "description" in payload:
        m.description = str(payload["description"]).strip() or None
    if "is_active" in payload:
        m.is_active = bool(payload["is_active"])
    db.commit()
    return {"ok": True}


@router.get("/api/admin/category-materials/{mid}/download")
def admin_download_material(mid: int, db: Session = Depends(get_db), _=Depends(admin_guard)):
    from category_models import CategoryMaterial
    m = db.query(CategoryMaterial).filter(CategoryMaterial.id == mid).first()
    if not m:
        raise HTTPException(status_code=404, detail="Material not found.")
    r2 = __import__("r2_storage")
    return r2.file_response(m.file_ref, m.mime or "application/octet-stream",
                            m.filename or "file", True)


@router.get("/api/teacher/materials")
def teacher_list_materials(category_id: int, subject_id: int = 0,
                           db: Session = Depends(get_db), me=Depends(get_teacher)):
    from category_models import CategoryMaterial, CategorySubject
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        return {"materials": []}
    CS.assert_teacher_category(db, tid, category_id)
    my_subs = set(CS.teacher_subject_ids(db, tid, category_id))
    q = db.query(CategoryMaterial).filter(
        CategoryMaterial.category_id == category_id,
        CategoryMaterial.is_active == True)  # noqa: E712
    if subject_id:
        if subject_id not in my_subs:
            raise HTTPException(status_code=403, detail="Not your subject.")
        q = q.filter(CategoryMaterial.category_subject_id == subject_id)
    mats = q.order_by(CategoryMaterial.created_at.desc()).all()
    # a teacher sees category-wide materials (no subject) + materials for THEIR subjects
    visible = [m for m in mats if (m.category_subject_id is None
                                   or m.category_subject_id in my_subs)]
    subj_name = {s.id: s.name for s in db.query(CategorySubject).filter(
        CategorySubject.category_id == category_id).all()}
    return {"materials": [_mat_dict(m, subj_name.get(m.category_subject_id)) for m in visible]}


@router.get("/api/teacher/category-materials/{mid}/download")
def teacher_download_material(mid: int, db: Session = Depends(get_db), me=Depends(get_teacher)):
    from category_models import CategoryMaterial
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        raise HTTPException(status_code=403, detail="No teacher profile.")
    m = db.query(CategoryMaterial).filter(CategoryMaterial.id == mid,
                                          CategoryMaterial.is_active == True).first()  # noqa: E712
    if not m:
        raise HTTPException(status_code=404, detail="Material not found.")
    # secure authorization: teacher must be in the category, and if the material is
    # subject-scoped, must be assigned that subject
    CS.assert_teacher_category(db, tid, m.category_id)
    if m.category_subject_id is not None:
        if m.category_subject_id not in set(CS.teacher_subject_ids(db, tid, m.category_id)):
            raise HTTPException(status_code=403, detail="Not your subject.")
    r2 = __import__("r2_storage")
    return r2.file_response(m.file_ref, m.mime or "application/octet-stream",
                            m.filename or "file", True)


# ===========================================================================
# TEACHER — category workspace dashboard (Phase 10)
# ===========================================================================
@router.get("/api/teacher/category-dashboard")
def teacher_category_dashboard(category_id: int, db: Session = Depends(get_db),
                               me=Depends(get_teacher)):
    from category_models import (Category, CategorySubject, TeacherCategorySubject,
                                 MaterialSubmission, CategoryEvent)
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        raise HTTPException(status_code=404, detail="Teacher profile not found.")
    cat = CS.assert_teacher_category(db, tid, category_id)   # 403 if not assigned

    sub_ids = [x.category_subject_id for x in db.query(TeacherCategorySubject).filter(
        TeacherCategorySubject.teacher_id == tid,
        TeacherCategorySubject.category_id == category_id).all()]
    subs = []
    if sub_ids:
        subs = db.query(CategorySubject).filter(
            CategorySubject.id.in_(sub_ids), CategorySubject.status == "active"
        ).order_by(CategorySubject.display_order, CategorySubject.name).all()

    base = db.query(MaterialSubmission).filter(
        MaterialSubmission.teacher_id == tid,
        MaterialSubmission.category_id == category_id)
    materials = base.count()
    approved = base.filter(MaterialSubmission.status == "approved").count()
    pending = base.filter(MaterialSubmission.status.in_(
        ["submitted", "under_review", "resubmitted"])).count()
    changes = base.filter(MaterialSubmission.status == "changes_required").count()

    subj_out = []
    for s in subs:
        sb = base.filter(MaterialSubmission.category_subject_id == s.id)
        subj_out.append({"id": s.id, "name": s.name, "code": s.code or "",
                         "materials": sb.count(),
                         "approved": sb.filter(MaterialSubmission.status == "approved").count(),
                         "pending": sb.filter(MaterialSubmission.status.in_(
                             ["submitted", "under_review", "changes_required", "resubmitted"])).count()})

    recent = []
    for ev in db.query(CategoryEvent).filter(
            CategoryEvent.category_id == category_id,
            CategoryEvent.teacher_id == tid
    ).order_by(CategoryEvent.created_at.desc()).limit(8).all():
        recent.append({"action": ev.action or "", "detail": ev.detail or "",
                       "at": str(ev.created_at or "")[:19]})

    return {
        "category": {"id": cat.id, "internal_key": cat.internal_key,
                     "display_name": cat.display_name},
        "kpis": {"active_tasks": pending + changes, "materials": materials,
                 "pending_reviews": pending + changes, "approved": approved},
        "subjects": subj_out,
        "recent": recent,
    }


# ===========================================================================
# TEACHER — my categories (for the header switcher)
# ===========================================================================
@router.get("/api/teacher/categories")
def teacher_my_categories(db: Session = Depends(get_db), me=Depends(get_teacher)):
    tid = CS.teacher_id_for_user(db, me)
    if not tid:
        return {"categories": [], "multi": False}
    cats = CS.teacher_categories(db, tid)
    out = []
    for c in cats:
        d = CS.category_dict(db, c, with_counts=False)
        d["features"] = sorted(CS.enabled_features(db, c.id))
        out.append(d)
    return {"categories": out, "multi": len(out) > 1}
