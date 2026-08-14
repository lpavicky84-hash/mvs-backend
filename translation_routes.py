"""
translation_routes.py — admin controls for the subject-aware Hindi translation system.

Endpoints (all admin-only):
  Glossary   : GET/POST/PATCH/DELETE /api/admin/glossary
  Settings   : GET/POST              /api/admin/translation/settings
  Reviews    : GET                   /api/admin/translation/reviews
               POST                  /api/admin/translation/reviews/{rid}/resolve
  Overview   : GET                   /api/admin/translation/overview
  Legacy scan: POST                  /api/admin/translation/scan
  Retranslate: POST                  /api/admin/translation/retranslate/{qid}
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session

from database import get_db
from security import get_admin
from models import (TranslationGlossary, TranslationReview, TranslationSetting,
                    TranslationVersion, Exam, ExamQuestion, LectureQuestion)
import translation_engine as te
import grading

router = APIRouter(prefix="/api/admin", tags=["translation"])


def _record_version(db, content_type, content_id, ref, old, new, reason, by=None):
    """Store a before/after snapshot for audit + revert. Skips no-op changes."""
    old = old or ""
    new = new or ""
    if old == new:
        return
    db.add(TranslationVersion(content_type=content_type, content_id=content_id,
                              question_ref=ref, field=ref, old_value=old, new_value=new,
                              reason=reason, created_by=by))


# ==================================================================== glossary
def _gloss_out(g):
    return {"id": g.id, "subject": g.subject or "", "chapter": g.chapter or "",
            "english_term": g.english_term or "", "preferred_hindi": g.preferred_hindi or "",
            "alternate_hindi": g.alternate_hindi or "", "do_not_translate": bool(g.do_not_translate),
            "transliteration_ok": bool(g.transliteration_ok), "locked": bool(g.locked),
            "priority": g.priority or 0, "notes": g.notes or ""}


@router.get("/glossary")
def glossary_list(subject: str = "", q: str = "", db: Session = Depends(get_db), me=Depends(get_admin)):
    query = db.query(TranslationGlossary)
    if subject:
        query = query.filter(TranslationGlossary.subject == subject)
    if q:
        like = "%" + q + "%"
        query = query.filter(TranslationGlossary.english_term.like(like))
    rows = query.order_by(TranslationGlossary.subject, TranslationGlossary.english_term).limit(500).all()
    subjects = [s[0] for s in db.query(TranslationGlossary.subject).distinct().all() if s[0]]
    return {"terms": [_gloss_out(g) for g in rows], "subjects": sorted(subjects)}


@router.post("/glossary")
def glossary_add(body: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    eng = (body.get("english_term") or "").strip()
    if not eng:
        raise HTTPException(400, "english_term is required")
    g = TranslationGlossary(
        subject=(body.get("subject") or "").strip(), chapter=(body.get("chapter") or "").strip(),
        english_term=eng, preferred_hindi=(body.get("preferred_hindi") or "").strip(),
        alternate_hindi=(body.get("alternate_hindi") or "").strip(),
        do_not_translate=bool(body.get("do_not_translate")),
        transliteration_ok=bool(body.get("transliteration_ok")),
        locked=bool(body.get("locked")), priority=int(body.get("priority") or 0),
        notes=(body.get("notes") or "").strip(), created_by=getattr(me, "id", None))
    db.add(g); db.commit(); db.refresh(g)
    return {"ok": True, "term": _gloss_out(g)}


@router.patch("/glossary/{gid}")
def glossary_edit(gid: int, body: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    g = db.query(TranslationGlossary).filter(TranslationGlossary.id == gid).first()
    if not g:
        raise HTTPException(404, "Not found")
    for f in ("subject", "chapter", "english_term", "preferred_hindi", "alternate_hindi", "notes"):
        if f in body:
            setattr(g, f, (body.get(f) or "").strip())
    for f in ("do_not_translate", "transliteration_ok", "locked"):
        if f in body:
            setattr(g, f, bool(body.get(f)))
    if "priority" in body:
        g.priority = int(body.get("priority") or 0)
    db.commit()
    return {"ok": True, "term": _gloss_out(g)}


@router.delete("/glossary/{gid}")
def glossary_del(gid: int, db: Session = Depends(get_db), me=Depends(get_admin)):
    g = db.query(TranslationGlossary).filter(TranslationGlossary.id == gid).first()
    if g:
        db.delete(g); db.commit()
    return {"ok": True}


# ==================================================================== settings
_DEFAULT_SETTINGS = {"gemini_verification": "on", "strict_mode": "off", "confidence_threshold": "70"}


@router.get("/translation/settings")
def settings_get(db: Session = Depends(get_db), me=Depends(get_admin)):
    out = dict(_DEFAULT_SETTINGS)
    for s in db.query(TranslationSetting).all():
        out[s.key] = s.value
    return {"settings": out}


@router.post("/translation/settings")
def settings_set(body: dict = Body(...), db: Session = Depends(get_db), me=Depends(get_admin)):
    for k, v in (body or {}).items():
        row = db.query(TranslationSetting).filter(TranslationSetting.key == k).first()
        if row:
            row.value = str(v)
        else:
            db.add(TranslationSetting(key=k, value=str(v)))
    db.commit()
    return settings_get(db, me)


# ==================================================================== reviews
def _review_out(r):
    try:
        issues = json.loads(r.issues) if r.issues else []
    except Exception:
        issues = []
    return {"id": r.id, "content_type": r.content_type or "", "content_id": r.content_id,
            "question_ref": r.question_ref or "", "subject": r.subject or "", "chapter": r.chapter or "",
            "english_text": r.english_text or "", "current_hindi": r.current_hindi or "",
            "suggested_hindi": r.suggested_hindi or "", "issues": issues,
            "confidence": r.confidence or 0, "source": r.source or "", "status": r.status or "pending",
            "created_at": r.created_at.strftime("%d %b %Y") if r.created_at else ""}


@router.get("/translation/reviews")
def reviews_list(status: str = "pending", db: Session = Depends(get_db), me=Depends(get_admin)):
    q = db.query(TranslationReview)
    if status:
        q = q.filter(TranslationReview.status == status)
    rows = q.order_by(TranslationReview.confidence.asc(), TranslationReview.id.desc()).limit(200).all()
    return {"reviews": [_review_out(r) for r in rows]}


@router.post("/translation/reviews/{rid}/resolve")
def review_resolve(rid: int, body: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_admin)):
    r = db.query(TranslationReview).filter(TranslationReview.id == rid).first()
    if not r:
        raise HTTPException(404, "Not found")
    action = (body.get("action") or "").lower()   # approve | reject | apply
    final_hi = body.get("hindi")
    if action == "reject":
        r.status = "rejected"
    else:
        # approve/apply: write the chosen Hindi back to the source question when possible
        text = final_hi if final_hi is not None else (r.suggested_hindi or r.current_hindi)
        _apply_review_to_source(db, r, text, getattr(me, "id", None))
        r.suggested_hindi = text
        r.status = "fixed"
    r.resolved_at = datetime.utcnow()
    r.resolved_by = getattr(me, "id", None)
    db.commit()
    return {"ok": True}


def _apply_review_to_source(db, r, text, me_id=None):
    """Write reviewed Hindi back into the source question field referenced by question_ref."""
    ct = "lecture" if r.content_type == "lecture" else "exam"
    if ct == "lecture":
        q = db.query(LectureQuestion).filter(LectureQuestion.id == r.content_id).first()
        if not q:
            return
        ref = r.question_ref or ""
        if ref.startswith("option:"):
            try:
                idx = int(ref.split(":", 1)[1]); opts = list(q.options_hi or [])
                while len(opts) <= idx:
                    opts.append("")
                _record_version(db, ct, q.id, ref, opts[idx], text, "review", me_id)
                opts[idx] = text; q.options_hi = opts
            except Exception:
                pass
        else:
            _record_version(db, ct, q.id, "question", q.question_hi or "", text, "review", me_id)
            q.question_hi = text
        return
    q = db.query(ExamQuestion).filter(ExamQuestion.id == r.content_id).first()
    if not q:
        return
    ref = r.question_ref or ""
    if ref.startswith("option:"):
        try:
            idx = int(ref.split(":", 1)[1]); opts = list(q.options_hi or [])
            while len(opts) <= idx:
                opts.append("")
            _record_version(db, ct, q.id, ref, opts[idx], text, "review", me_id)
            opts[idx] = text; q.options_hi = opts
        except Exception:
            pass
    elif ref == "answer":
        _record_version(db, ct, q.id, "answer", q.model_answer_hi or "", text, "review", me_id)
        q.model_answer_hi = text
    elif ref == "explanation":
        _record_version(db, ct, q.id, "explanation", q.explanation_hi or "", text, "review", me_id)
        q.explanation_hi = text
    else:
        _record_version(db, ct, q.id, "question", q.question_text_hi or "", text, "review", me_id)
        q.question_text_hi = text


# ==================================================================== overview
@router.get("/translation/overview")
def overview(db: Session = Depends(get_db), me=Depends(get_admin)):
    total_q = db.query(ExamQuestion).count()
    with_hi = db.query(ExamQuestion).filter(ExamQuestion.question_text_hi != None,
                                            ExamQuestion.question_text_hi != "").count()
    pending = db.query(TranslationReview).filter(TranslationReview.status == "pending").count()
    fixed = db.query(TranslationReview).filter(TranslationReview.status == "fixed").count()
    gloss = db.query(TranslationGlossary).count()
    locked = db.query(TranslationGlossary).filter(TranslationGlossary.locked == True).count()
    return {"total_questions": total_q, "translated_questions": with_hi,
            "pending_reviews": pending, "fixed_reviews": fixed,
            "glossary_terms": gloss, "locked_terms": locked}


# ==================================================================== legacy scan
@router.post("/translation/scan")
def legacy_scan(body: dict = Body(default={}), db: Session = Depends(get_db), me=Depends(get_admin)):
    """Scan translated questions (content_type: exam | lecture): auto-repair safe issues
    (ordinals) in place and flag low-confidence ones into the review queue. Batched."""
    subject = (body.get("subject") or "").strip()
    ctype = (body.get("content_type") or "exam").strip().lower()
    limit = min(int(body.get("limit") or 200), 500)
    offset = int(body.get("offset") or 0)
    me_id = getattr(me, "id", None)
    counts = {"scanned": 0, "verified": 0, "repaired": 0, "flagged": 0}

    if ctype == "lecture":
        base = db.query(LectureQuestion).filter(LectureQuestion.question_hi != None,
                                                LectureQuestion.question_hi != "")
        rows = base.order_by(LectureQuestion.id).offset(offset).limit(limit).all()
        for q in rows:
            counts["scanned"] += 1
            opts_en = list(q.options or []); opts_hi = list(q.options_hi or [])
            res = te.scan_pair(q.question or "", q.question_hi or "", "", "", opts_en, opts_hi)
            cls = res["classification"]
            if cls == "verified":
                counts["verified"] += 1; continue
            if cls == "repair":
                nv = te.repair(q.question_hi or "")
                _record_version(db, "lecture", q.id, "question", q.question_hi or "", nv, "repair", me_id)
                q.question_hi = nv
                if opts_hi:
                    q.options_hi = [te.repair(o) for o in opts_hi]
                counts["repaired"] += 1; continue
            _flag(db, "lecture", q.id, "question", "", "", q.question or "", q.question_hi or "", res)
            counts["flagged"] += 1
        db.commit()
        remaining = base.count() - (offset + len(rows))
        return {"ok": True, "counts": counts, "next_offset": offset + len(rows), "remaining": max(0, remaining)}

    # default: exam questions
    eq = db.query(ExamQuestion).filter(ExamQuestion.question_text_hi != None,
                                       ExamQuestion.question_text_hi != "")
    exams = {e.id: e for e in db.query(Exam).all()}
    rows = eq.order_by(ExamQuestion.id).offset(offset).limit(limit).all()
    for q in rows:
        ex = exams.get(q.exam_id)
        subj = (ex.subject if ex else "") or ""
        chap = (ex.chapter if ex else "") or ""
        if subject and subj != subject:
            continue
        counts["scanned"] += 1
        opts_en = list(q.options or []); opts_hi = list(q.options_hi or [])
        res = te.scan_pair(q.question_text or "", q.question_text_hi or "",
                           q.model_answer or "", q.model_answer_hi or "", opts_en, opts_hi)
        cls = res["classification"]
        if cls == "verified":
            counts["verified"] += 1; continue
        if cls == "repair":
            nv = te.repair(q.question_text_hi or "")
            _record_version(db, "exam", q.id, "question", q.question_text_hi or "", nv, "repair", me_id)
            q.question_text_hi = nv
            if q.model_answer_hi:
                q.model_answer_hi = te.repair(q.model_answer_hi)
            if q.explanation_hi:
                q.explanation_hi = te.repair(q.explanation_hi)
            if opts_hi:
                q.options_hi = [te.repair(o) for o in opts_hi]
            counts["repaired"] += 1; continue
        _flag(db, "exam", q.id, "question", subj, chap, q.question_text or "", q.question_text_hi or "", res)
        counts["flagged"] += 1
    db.commit()
    remaining = eq.count() - (offset + len(rows))
    return {"ok": True, "counts": counts, "next_offset": offset + len(rows), "remaining": max(0, remaining)}


def _flag(db, ctype, cid, ref, subj, chap, en, hi, res):
    """Create a pending review row (deduped) for a problematic translation."""
    existing = db.query(TranslationReview).filter(
        TranslationReview.content_id == cid, TranslationReview.content_type == ctype,
        TranslationReview.status == "pending").first()
    if existing:
        return
    db.add(TranslationReview(
        content_type=ctype, content_id=cid, question_ref=ref, subject=subj, chapter=chap,
        english_text=en, current_hindi=hi, suggested_hindi="",
        issues=json.dumps(res["issues"], ensure_ascii=False),
        confidence=res["confidence"], source="scan", status="pending"))


# ==================================================================== retranslate
@router.post("/translation/retranslate/{qid}")
def retranslate(qid: int, db: Session = Depends(get_db), me=Depends(get_admin)):
    """Re-run a single exam question through the verified pipeline (glossary + validation)
    and save the new Hindi. Returns the new confidence."""
    q = db.query(ExamQuestion).filter(ExamQuestion.id == qid).first()
    if not q:
        raise HTTPException(404, "Question not found")
    ex = db.query(Exam).filter(Exam.id == q.exam_id).first()
    subj = (ex.subject if ex else "") or ""
    chap = (ex.chapter if ex else "") or ""
    opts = list(q.options or [])
    tr = grading.translate_question_to_hindi(
        q.question_text or "", q.model_answer or "", opts if opts else None,
        subj, chapter=chap, db=db)
    if not tr:
        raise HTTPException(502, "Translation service unavailable")
    if tr.get("question"):
        _record_version(db, "exam", q.id, "question", q.question_text_hi or "", tr["question"], "retranslate", getattr(me, "id", None))
        q.question_text_hi = tr["question"]
    if tr.get("answer"):
        _record_version(db, "exam", q.id, "answer", q.model_answer_hi or "", tr["answer"], "retranslate", getattr(me, "id", None))
        q.model_answer_hi = tr["answer"]
    if tr.get("options") and len(tr["options"]) == len(opts):
        q.options_hi = tr["options"]
    # resolve any pending review for this question
    for r in db.query(TranslationReview).filter(TranslationReview.content_id == q.id,
                                                TranslationReview.content_type == "exam",
                                                TranslationReview.status == "pending").all():
        r.status = "fixed"; r.suggested_hindi = tr.get("question") or ""
        r.resolved_at = datetime.utcnow(); r.resolved_by = getattr(me, "id", None)
    db.commit()
    return {"ok": True, "confidence": tr.get("confidence"), "issues": tr.get("issues", [])}


# ==================================================================== version history
@router.get("/translation/versions")
def versions_list(content_type: str = "", content_id: int = 0, db: Session = Depends(get_db), me=Depends(get_admin)):
    q = db.query(TranslationVersion)
    if content_type:
        q = q.filter(TranslationVersion.content_type == content_type)
    if content_id:
        q = q.filter(TranslationVersion.content_id == content_id)
    rows = q.order_by(TranslationVersion.id.desc()).limit(100).all()
    return {"versions": [{
        "id": v.id, "content_type": v.content_type, "content_id": v.content_id,
        "question_ref": v.question_ref, "old_value": v.old_value, "new_value": v.new_value,
        "reason": v.reason, "at": v.created_at.strftime("%d %b %Y %H:%M") if v.created_at else ""
    } for v in rows]}


@router.post("/translation/versions/{vid}/revert")
def version_revert(vid: int, db: Session = Depends(get_db), me=Depends(get_admin)):
    """Restore the old_value of a change back into the source question."""
    v = db.query(TranslationVersion).filter(TranslationVersion.id == vid).first()
    if not v:
        raise HTTPException(404, "Not found")
    me_id = getattr(me, "id", None)
    if v.content_type == "lecture":
        q = db.query(LectureQuestion).filter(LectureQuestion.id == v.content_id).first()
        if q and v.question_ref == "question":
            _record_version(db, "lecture", q.id, "question", q.question_hi or "", v.old_value, "revert", me_id)
            q.question_hi = v.old_value
    else:
        q = db.query(ExamQuestion).filter(ExamQuestion.id == v.content_id).first()
        if q:
            ref = v.question_ref
            cur = {"question": q.question_text_hi, "answer": q.model_answer_hi,
                   "explanation": q.explanation_hi}.get(ref, q.question_text_hi)
            _record_version(db, "exam", q.id, ref, cur or "", v.old_value, "revert", me_id)
            if ref == "answer":
                q.model_answer_hi = v.old_value
            elif ref == "explanation":
                q.explanation_hi = v.old_value
            else:
                q.question_text_hi = v.old_value
    db.commit()
    return {"ok": True}
