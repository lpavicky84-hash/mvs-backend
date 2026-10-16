"""
Public shareable MCQ tests — Google-Form style.
A teacher turns ON a public link for an MCQ test; anyone with the link (guest, no portal
login) fills a short detail form, takes the test, and gets an instant premium result.
The teacher sees every guest response and can export them as CSV.

Security notes:
- Only an MCQ test that is EXPLICITLY public_enabled + active is reachable.
- The public GET NEVER returns correct answers / explanations (no answer leak before submit).
- Name + phone are always required; one submission per phone per exam (open-link abuse guard).
- Nothing here touches real portal StudentProfiles — guests live in public_exam_attempts only.
"""
import re as _re
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session

from database import get_db
from models import Exam, ExamQuestion, PublicExamAttempt
import grading

router = APIRouter(prefix="/api/public", tags=["public"])

# optional guest fields the teacher can ask for (name + phone are ALWAYS required)
PUBLIC_FIELD_LABELS = {
    "email": "Email",
    "class": "Class / Course",
    "city": "City",
    "nios_ref": "NIOS Reference Number",
}


def public_fields_list(ex):
    raw = (getattr(ex, "public_fields", "") or "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return [{"key": k, "label": PUBLIC_FIELD_LABELS[k]} for k in keys if k in PUBLIC_FIELD_LABELS]


def _exam_by_token(db, token):
    token = (token or "").strip()
    if not token:
        raise HTTPException(404, "This test link is not available.")
    ex = db.query(Exam).filter(Exam.public_token == token).first()
    if not ex or not bool(getattr(ex, "public_enabled", False)) or not ex.is_active:
        raise HTTPException(404, "This test link is not available.")
    if (ex.test_type or "") != "mcq":
        raise HTTPException(400, "This test is not available through a public link.")
    return ex


@router.get("/exam/{token}")
def public_get_exam(token: str, db: Session = Depends(get_db)):
    """Guest view of the test — questions + options ONLY (no correct answers)."""
    ex = _exam_by_token(db, token)
    qs = (db.query(ExamQuestion)
          .filter(ExamQuestion.exam_id == ex.id)
          .order_by(ExamQuestion.q_no).all())
    questions = [{
        "q_no": q.q_no, "question_text": q.question_text,
        "question_text_hi": q.question_text_hi,
        "options": q.options or [], "options_hi": q.options_hi,
        "max_marks": q.max_marks, "image_b64": q.image_b64,
    } for q in qs]
    return {"id": ex.id, "title": ex.title, "subject": ex.subject, "medium": ex.medium,
            "duration_min": ex.duration_min, "total_marks": ex.total_marks,
            "teacher_name": ex.teacher_name, "fields": public_fields_list(ex),
            "question_count": len(questions), "questions": questions}


@router.post("/exam/{token}/submit")
def public_submit(token: str, payload: dict = Body(...), request: Request = None,
                  db: Session = Depends(get_db)):
    ex = _exam_by_token(db, token)
    details = payload.get("details") or {}
    name = (str(details.get("name") or "")).strip()
    phone = _re.sub(r"[^0-9]", "", str(details.get("phone") or ""))
    if not name:
        raise HTTPException(400, "Please enter your name.")
    if len(phone) < 10:
        raise HTTPException(400, "Please enter a valid 10-digit phone number.")
    for f in public_fields_list(ex):
        if not str(details.get(f["key"]) or "").strip():
            raise HTTPException(400, "Please fill: %s" % f["label"])
    # one attempt per phone per exam (open link abuse guard)
    if db.query(PublicExamAttempt).filter(PublicExamAttempt.exam_id == ex.id,
                                          PublicExamAttempt.phone == phone).first():
        raise HTTPException(400, "You have already submitted this test from this number.")

    ans = payload.get("mcq_answers") or {}
    qs = (db.query(ExamQuestion)
          .filter(ExamQuestion.exam_id == ex.id)
          .order_by(ExamQuestion.q_no).all())
    results, total = grading.grade_mcq(qs, ans)
    correct = sum(1 for r in results
                  if (r.get("max") or 0) > 0 and (r.get("marks") or 0) >= (r.get("max") or 0))

    row = PublicExamAttempt(
        exam_id=ex.id, name=name[:160], phone=phone[:20],
        email=((str(details.get("email") or "")).strip()[:160] or None),
        class_course=((str(details.get("class") or "")).strip()[:120] or None),
        city=((str(details.get("city") or "")).strip()[:120] or None),
        nios_ref=((str(details.get("nios_ref") or "")).strip()[:60] or None),
        mcq_answers=ans, total_awarded=total, total_marks=(ex.total_marks or 0),
        correct_count=correct, q_total=len(qs),
        ip=(((request.client.host if (request and request.client) else "") or "")[:50]))
    db.add(row)
    db.commit()

    # instant result — ab submit ho gaya to correct answers safe to reveal
    review = [{
        "q_no": q.q_no, "question_text": q.question_text, "question_text_hi": q.question_text_hi,
        "options": q.options or [], "options_hi": q.options_hi,
        "your_answer": (ans.get(str(q.q_no), ans.get(q.q_no))),
        "correct_answer": q.correct_option, "explanation": q.explanation,
    } for q in qs]
    return {"total_awarded": total, "total_marks": (ex.total_marks or 0),
            "correct": correct, "q_total": len(qs), "title": ex.title, "results": review}
