"""One-time bulk migration: purane base64 photos/materials -> R2.

Sirf un rows ko chhuta hai jinme base64 hai (jo 'http' se shuru NAHI hote).
Upload fail ho to base64 waise ka waisa rehta hai (data loss nahi). Batch + cursor.
"""
import base64


def _spec(kind):
    from models import StudentProfile, TeacherProfile, Material
    if kind == "photos_student":
        return StudentProfile, "photo_b64", "photos/student", "image/jpeg", ".jpg"
    if kind == "photos_teacher":
        return TeacherProfile, "photo_b64", "photos/teacher", "image/jpeg", ".jpg"
    if kind == "materials":
        return Material, "content_b64", "materials", "application/pdf", ".pdf"
    # ---- extra fields (Phase 2d) ----
    try:
        from models import StudioReport
        if kind == "notes":
            return StudioReport, "notes_file_b64", "notes", "application/pdf", ".pdf"
    except Exception:
        pass
    try:
        from models import Lecture
        if kind == "lecture_pdf":
            return Lecture, "pdf_b64", "lectures", "application/pdf", ".pdf"
        if kind == "lecture_dpp":
            return Lecture, "dpp_b64", "lectures", "application/pdf", ".pdf"
    except Exception:
        pass
    try:
        from models import VideoTask
        if kind == "thumbnails":
            return VideoTask, "thumbnail_b64", "thumbnails", "image/jpeg", ".jpg"
    except Exception:
        pass
    try:
        from models import Doubt
        if kind == "doubt_img":
            return Doubt, "image_b64", "doubt-img", "image/jpeg", ".jpg"
        if kind == "doubt_audio":
            return Doubt, "audio_b64", "doubt-audio", "audio/webm", ".webm"
        if kind == "doubt_ans_audio":
            return Doubt, "answer_audio_b64", "doubt-audio", "audio/webm", ".webm"
        if kind == "doubt_ans_file":
            return Doubt, "answer_attach_b64", "doubt-attach", "application/octet-stream", ".bin"
    except Exception:
        pass
    try:
        from models import DppAnswer
        if kind == "dpp_answers":
            return DppAnswer, "answer_b64", "dpp-answers", "application/pdf", ".pdf"
    except Exception:
        pass
    # ---- Phase 3: exam question figures + DPP generated PDFs (Railway -> R2) ----
    try:
        from models import ExamQuestion
        if kind == "exam_q_img":
            return ExamQuestion, "image_b64", "exam-q", "image/jpeg", ".jpg"
        if kind == "exam_q_ans_img":
            return ExamQuestion, "model_answer_image", "exam-q", "image/jpeg", ".jpg"
        if kind == "exam_q_alt_img":
            return ExamQuestion, "alt_image_b64", "exam-q", "image/jpeg", ".jpg"
    except Exception:
        pass
    try:
        from models import DppPack
        if kind == "dpp_q_pdf":
            return DppPack, "q_pdf", "dpp-pdf", "application/pdf", ".pdf"
        if kind == "dpp_s_pdf":
            return DppPack, "s_pdf", "dpp-pdf", "application/pdf", ".pdf"
    except Exception:
        pass
    return None


def migrate_batch(db, kind, after_id=0, limit=10):
    import r2_storage as R2
    if not R2.is_configured():
        return {"error": "R2 not configured"}
    spec = _spec(kind)
    if not spec:
        return {"error": "bad kind"}
    Model, field, prefix, ctype, ext = spec
    col = getattr(Model, field)

    base_q = db.query(Model).filter(col.isnot(None), ~col.like("http%"))
    total = base_q.count() if after_id == 0 else None
    rows = base_q.filter(Model.id > after_id).order_by(Model.id).limit(limit).all()

    migrated = skipped = 0
    last_id = after_id
    for r in rows:
        last_id = r.id
        val = getattr(r, field)
        if not val or (isinstance(val, str) and val.startswith("http")):
            skipped += 1
            continue
        try:
            raw = base64.b64decode(val.split(",")[-1])
            fn = getattr(r, "filename", None) or ("file" + ext)
            url = R2.upload_bytes(R2.new_key(prefix, fn), raw, ctype)
            setattr(r, field, url)
            migrated += 1
        except Exception:
            skipped += 1  # base64 waise ka waisa reh gaya
    db.commit()
    return {"kind": kind, "checked": len(rows), "migrated": migrated,
            "skipped": skipped, "last_id": last_id,
            "has_more": len(rows) == limit, "total": total}
