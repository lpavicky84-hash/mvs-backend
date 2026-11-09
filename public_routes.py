"""
Public shareable MCQ tests — Google-Form style.
A teacher turns ON a public link for an MCQ test; anyone with the link (guest, no portal
login) fills a short detail form, takes the test, and gets an instant premium result.

Privacy split:
- ALL personal details (phone, email, city, NIOS ref, custom fields) live here + are visible
  ONLY to the admin tracker. The teacher sees just name + score.
- The public GET NEVER returns correct answers / explanations (no answer leak before submit).
- Name + phone are always required; one submission per phone per exam (open-link abuse guard).
- Guests live in public_exam_attempts only — never touch real portal StudentProfiles.
"""
import re as _re
import json as _json
from datetime import datetime as _dt, timedelta as _td
from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session

from database import get_db
from models import Exam, ExamQuestion, PublicExamAttempt, PublicExamSession
import grading

# server-authoritative timer: submit kitne late tak accept ho (network/lag ke liye thodi chhoot).
# Resume-safe timing isse bachati hai; ye grace sirf honest last-second submits ke liye hai.
_SUBMIT_GRACE_SEC = 120

router = APIRouter(prefix="/api/public", tags=["public"])

# predefined guest fields (name + phone are ALWAYS required, not listed here)
PUBLIC_FIELD_LABELS = {
    "email": "Email",
    "class": "Class / Course",
    "city": "City",
    "nios_ref": "NIOS Reference Number",
}
_KNOWN_KEYS = set(PUBLIC_FIELD_LABELS.keys())


def public_fields_cfg(ex):
    """Return the ordered list of guest fields as rich dicts:
    {key, label, type('text'|'select'), options[], required}.
    Prefers the JSON public_field_config; falls back to the legacy public_fields CSV
    (where 'class' becomes a dropdown with sensible defaults)."""
    raw = (getattr(ex, "public_field_config", "") or "").strip()
    if raw:
        try:
            data = _json.loads(raw)
            flds = data.get("fields") if isinstance(data, dict) else data
            out = []
            for f in (flds or []):
                if not isinstance(f, dict):
                    continue
                k = str(f.get("key") or "").strip()
                if not k:
                    continue
                lbl = (str(f.get("label") or "").strip()
                       or PUBLIC_FIELD_LABELS.get(k, k))
                typ = "select" if (f.get("type") in ("select", "dropdown")) else "text"
                opts = ([str(o).strip() for o in (f.get("options") or []) if str(o).strip()]
                        if typ == "select" else [])
                req = bool(f.get("required", True))
                out.append({"key": k, "label": lbl, "type": typ,
                            "options": opts, "required": req})
            if out:
                return out
        except Exception:
            pass
    # ---- legacy fallback: CSV of known keys ----
    keys = [k.strip() for k in (getattr(ex, "public_fields", "") or "").split(",") if k.strip()]
    out = []
    for k in keys:
        if k not in PUBLIC_FIELD_LABELS:
            continue
        if k == "class":
            out.append({"key": "class", "label": PUBLIC_FIELD_LABELS["class"],
                        "type": "select", "options": ["Class 10", "Class 12"], "required": True})
        else:
            out.append({"key": k, "label": PUBLIC_FIELD_LABELS[k],
                        "type": "text", "options": [], "required": True})
    return out


def _ist_now():
    """Current wall-clock time in IST. scheduled_at is stored as naive IST (the time the
    teacher typed), and all MVS users are in India, so we compare against IST now."""
    return _dt.utcnow() + _td(hours=5, minutes=30)


def _window_close_at(ex):
    """Scheduled public test ka submission window close time = scheduled_at + duration.
    Non-scheduled ya bina-duration test ka koi window close nahi (None)."""
    sched = getattr(ex, "scheduled_at", None)
    dur = int(getattr(ex, "duration_min", 0) or 0)
    if sched is None or dur <= 0:
        return None
    return sched + _td(minutes=dur)


def _window_closed(ex):
    """Scheduled test ka time (scheduled_at + duration) khatam ho gaya? (window band)."""
    close_at = _window_close_at(ex)
    return (close_at is not None and _ist_now() >= close_at)


def _rank_rows(db, exam_id):
    """All attempts for an exam, ranked: highest score first; on a TIE, whoever submitted
    EARLIER ranks higher (created_at asc); id asc is the final stable tie-break."""
    return (db.query(PublicExamAttempt)
            .filter(PublicExamAttempt.exam_id == exam_id)
            .order_by(PublicExamAttempt.total_awarded.desc(),
                      PublicExamAttempt.created_at.asc(),
                      PublicExamAttempt.id.asc()).all())


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
    """Guest view of the test — questions + options ONLY (no correct answers).
    Also bumps the open/view counter for the admin tracker."""
    ex = _exam_by_token(db, token)
    # view counter (link opened) — best-effort, never blocks the page
    try:
        ex.public_views = (int(getattr(ex, "public_views", 0) or 0)) + 1
        db.commit()
    except Exception:
        db.rollback()
    sched = getattr(ex, "scheduled_at", None)
    _mode = (getattr(ex, "exam_mode", None) or "NORMAL").strip().upper()
    meta = {"id": ex.id, "title": ex.title, "subject": ex.subject, "medium": ex.medium,
            "duration_min": ex.duration_min, "total_marks": ex.total_marks,
            "teacher_name": ex.teacher_name, "fields": public_fields_cfg(ex),
            "scheduled_at": sched.isoformat() if sched else None,
            # Competitive scoring info (frontend: negative-marking notice + correct "out of")
            "exam_mode": _mode,
            "mark_correct": float(getattr(ex, "mark_correct", 1.0) or 0),
            "mark_wrong": float(getattr(ex, "mark_wrong", 0.0) or 0),
            "mark_unanswered": float(getattr(ex, "mark_unanswered", 0.0) or 0),
            "integrity": _integrity_cfg(ex)}
    # SCHEDULED TEST: before the start time, show only the countdown + details — never the
    # questions (so the paper can't be read early). Opens automatically at the scheduled time.
    if sched is not None:
        now = _ist_now()
        if now < sched:
            qn = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).count()
            if _mode == "COMPETITIVE":
                meta["total_marks"] = round(qn * meta["mark_correct"], 2)
            meta.update({"not_started": True,
                         "seconds_to_start": max(0, int((sched - now).total_seconds())),
                         "question_count": qn, "questions": []})
            return meta
        # WINDOW CLOSED: scheduled_at + duration ke baad test band — naye students ko paper nahi
        close_at = _window_close_at(ex)
        if close_at is not None and now >= close_at:
            qn = db.query(ExamQuestion).filter(ExamQuestion.exam_id == ex.id).count()
            if _mode == "COMPETITIVE":
                meta["total_marks"] = round(qn * meta["mark_correct"], 2)
            meta.update({"closed": True, "closed_at": close_at.isoformat(),
                         "question_count": qn, "questions": []})
            return meta
    qs = (db.query(ExamQuestion)
          .filter(ExamQuestion.exam_id == ex.id)
          .order_by(ExamQuestion.q_no).all())
    questions = [{
        "q_no": q.q_no, "question_text": q.question_text,
        "question_text_hi": q.question_text_hi,
        "options": q.options or [], "options_hi": q.options_hi,
        "max_marks": q.max_marks, "image_b64": q.image_b64,
    } for q in qs]
    if _mode == "COMPETITIVE":
        meta["total_marks"] = round(len(questions) * meta["mark_correct"], 2)
    meta.update({"not_started": False, "question_count": len(questions), "questions": questions})
    return meta


def _validate_details(ex, details):
    """name + phone + har required field validate karo; custom fields `extra` me collect.
    Submit aur Start dono isi se validate karte hain (ek hi jagah logic)."""
    details = details or {}
    name = (str(details.get("name") or "")).strip()
    phone = _re.sub(r"[^0-9]", "", str(details.get("phone") or ""))
    if not name:
        raise HTTPException(400, "Please enter your name.")
    if len(phone) < 10:
        raise HTTPException(400, "Please enter a valid 10-digit phone number.")
    extra = {}
    for f in public_fields_cfg(ex):
        v = str(details.get(f["key"]) or "").strip()
        if f["required"] and not v:
            raise HTTPException(400, "Please fill: %s" % f["label"])
        if f["key"] not in _KNOWN_KEYS and v:
            extra[f["key"]] = {"label": f["label"], "value": v}
    return name, phone, extra


def _get_session(db, exam_id, phone):
    return (db.query(PublicExamSession)
            .filter(PublicExamSession.exam_id == exam_id,
                    PublicExamSession.phone == phone).first())


def _create_attempt(db, ex, name, phone, details, ans, ip=None,
                    violations=0, integrity_log=None):
    """Grade answers through the central service and persist ONE PublicExamAttempt. Returns the row."""
    details = details or {}
    ans = ans or {}
    extra = {}
    for f in public_fields_cfg(ex):
        v = str(details.get(f["key"]) or "").strip()
        if f["key"] not in _KNOWN_KEYS and v:
            extra[f["key"]] = {"label": f["label"], "value": v}
    qs = (db.query(ExamQuestion)
          .filter(ExamQuestion.exam_id == ex.id)
          .order_by(ExamQuestion.q_no).all())
    _mode = (getattr(ex, "exam_mode", None) or "NORMAL").strip().upper()
    _results, tot = grading.score_objective(
        qs, ans, exam_mode=_mode,
        mark_correct=getattr(ex, "mark_correct", 1.0),
        mark_wrong=getattr(ex, "mark_wrong", 0.0),
        mark_unanswered=getattr(ex, "mark_unanswered", 0.0))
    out_of = (tot["total_possible"] if _mode == "COMPETITIVE" else (ex.total_marks or 0))
    viol = int(violations or 0)
    row = PublicExamAttempt(
        exam_id=ex.id, name=(name or "Student")[:160], phone=phone[:20],
        email=((str(details.get("email") or "")).strip()[:160] or None),
        class_course=((str(details.get("class") or "")).strip()[:120] or None),
        city=((str(details.get("city") or "")).strip()[:120] or None),
        nios_ref=((str(details.get("nios_ref") or "")).strip()[:60] or None),
        extra=(extra or None),
        mcq_answers=ans, total_awarded=tot["total_awarded"], total_marks=out_of,
        correct_count=tot["correct"], wrong_count=tot["wrong"],
        unanswered_count=tot["unanswered"], q_total=len(qs),
        violations=viol, flagged=bool(viol > 0), integrity_log=(integrity_log or None),
        ip=((ip or "")[:50]))
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ranking_state(ex):
    """Ranking kab dikhe — AUTOMATIC (koi manual nahi):
    - Non-scheduled test -> hamesha visible (live, jaise-jaise students submit karein).
    - Scheduled test -> tabhi visible jab submission window close ho jaaye
      (scheduled_at + duration_min). Usse pehle ranking chhupi — fairness ke liye."""
    sched = getattr(ex, "scheduled_at", None)
    if sched is None:
        return {"visible": True, "unlock_at": None, "seconds_to_unlock": 0}
    dur = int(getattr(ex, "duration_min", 0) or 0)
    close = sched + _td(minutes=dur)
    now = _ist_now()
    vis = (now >= close)
    return {"visible": vis, "unlock_at": close,
            "seconds_to_unlock": (0 if vis else max(0, int((close - now).total_seconds())))}


def _leaderboard_payload(db, ex, my_phone=None, my_row=None):
    """Current ranking state + top 10 + my_rank. Har call pe fresh compute hota hai,
    isliye jaise-jaise students submit karein ranking live update hoti hai."""
    rk = _ranking_state(ex)
    if not rk["visible"]:
        total = db.query(PublicExamAttempt).filter(PublicExamAttempt.exam_id == ex.id).count()
        return {"ranking_visible": False, "ranking_pending": True,
                "ranking_at": (rk["unlock_at"].isoformat() if rk["unlock_at"] else None),
                "seconds_to_ranking": rk["seconds_to_unlock"],
                "leaderboard": [], "my_rank": None, "total_students": total}
    ranked = _rank_rows(db, ex.id)
    leaderboard = [{"rank": i + 1, "name": (r.name or "Student"),
                    "score": r.total_awarded, "total": r.total_marks,
                    "correct": r.correct_count}
                   for i, r in enumerate(ranked[:10])]
    my_rank = None
    if my_row is not None:
        my_rank = next((i + 1 for i, r in enumerate(ranked) if r.id == my_row.id), None)
    elif my_phone:
        mp = _re.sub(r"[^0-9]", "", str(my_phone))
        if len(mp) >= 10:
            my_rank = next((i + 1 for i, r in enumerate(ranked)
                            if _re.sub(r"[^0-9]", "", str(r.phone or "")) == mp), None)
    return {"ranking_visible": True, "ranking_pending": False, "ranking_at": None,
            "seconds_to_ranking": 0, "leaderboard": leaderboard,
            "my_rank": my_rank, "total_students": len(ranked)}


def _build_result(db, ex, row):
    """Result payload kisi bhi attempt row se banao. Score + answers HAMESHA turant dikhte hain.
    Ranking visibility automatic (_ranking_state): non-scheduled turant live; scheduled test ka
    time khatam (scheduled_at+duration) hone par auto reveal. Koi manual release nahi."""
    _mode = (getattr(ex, "exam_mode", None) or "NORMAL").strip().upper()
    ans = row.mcq_answers or {}
    qs = (db.query(ExamQuestion)
          .filter(ExamQuestion.exam_id == ex.id)
          .order_by(ExamQuestion.q_no).all())
    results, _tot = grading.score_objective(
        qs, ans, exam_mode=_mode,
        mark_correct=getattr(ex, "mark_correct", 1.0),
        mark_wrong=getattr(ex, "mark_wrong", 0.0),
        mark_unanswered=getattr(ex, "mark_unanswered", 0.0))
    _rmap = {r["q_no"]: r for r in results}
    review = [{
        "q_no": q.q_no, "question_text": q.question_text, "question_text_hi": q.question_text_hi,
        "options": q.options or [], "options_hi": q.options_hi,
        "your_answer": (ans.get(str(q.q_no), ans.get(q.q_no))),
        "correct_answer": q.correct_option, "explanation": q.explanation,
        "marks": (_rmap.get(q.q_no, {}).get("marks")),
        "status": (_rmap.get(q.q_no, {}).get("status")),
    } for q in qs]
    lb = _leaderboard_payload(db, ex, my_row=row)
    out = {"total_awarded": row.total_awarded, "total_marks": (row.total_marks or 0),
           "correct": row.correct_count,
           "wrong": int(getattr(row, "wrong_count", 0) or 0),
           "unanswered": int(getattr(row, "unanswered_count", 0) or 0),
           "q_total": row.q_total, "title": ex.title, "results": review,
           "exam_mode": _mode,
           "mark_correct": float(getattr(ex, "mark_correct", 1.0) or 0),
           "mark_wrong": float(getattr(ex, "mark_wrong", 0.0) or 0),
           "mark_unanswered": float(getattr(ex, "mark_unanswered", 0.0) or 0)}
    out.update(lb)
    return out


def _finalize_session(db, ex, sess, auto=False, ip=None):
    """Session ke saved answers se attempt bana ke session ko final mark karo
    (server-side auto-submit: time khatam / student reload pe)."""
    row = _create_attempt(db, ex, sess.name or "Student", sess.phone,
                          (sess.details or {}), (sess.answers or {}), ip=ip,
                          violations=int(getattr(sess, "violations", 0) or 0),
                          integrity_log=getattr(sess, "integrity_log", None))
    sess.submitted = True
    sess.auto_submitted = bool(auto)
    sess.updated_at = _ist_now()
    db.commit()
    return row


def _client_ip(request):
    return ((request.client.host if (request and request.client) else "") or "")


# ---------- Integrity / Secure mode (Phase 3) ----------
# Ek app-switch / exit = EK violation (client coalesce karta hai, server seq se dedup).
_VIOLATION_TYPES = {"VISIBILITY_HIDDEN", "WINDOW_BLUR", "FULLSCREEN_EXIT", "COPY_ATTEMPT",
                    "PASTE_ATTEMPT", "CONTEXT_MENU_ATTEMPT", "PRINT_SHORTCUT_ATTEMPT",
                    "PAGE_EXIT"}


def _integrity_cfg(ex):
    """Per-test integrity policy — server owns it. Mode se flags derive hote hain:
    OFF = kuch nahi; STANDARD = detect + warn + flag (auto-submit NAHI);
    STRICT = fullscreen + copy/paste/right-click block + max_violations ke baad auto-submit."""
    mode = (getattr(ex, "integrity_mode", None) or "OFF").strip().upper()
    if mode not in ("OFF", "STANDARD", "STRICT"):
        mode = "OFF"
    maxv = int(getattr(ex, "max_violations", 3) or 0)
    if maxv < 1:
        maxv = 3
    strict = (mode == "STRICT")
    on = (mode != "OFF")
    return {"mode": mode, "monitor": on, "warn": on,
            "require_fullscreen": strict, "max_violations": maxv,
            "auto_submit": strict, "block_copy": strict, "block_paste": strict,
            "block_context": strict, "block_print": strict,
            # Randomization (independent of integrity mode — OFF hone par bhi shuffle ho sakta hai)
            "randomize_questions": bool(getattr(ex, "randomize_questions", False)),
            "randomize_options": bool(getattr(ex, "randomize_options", False))}


@router.post("/exam/{token}/start")
def public_start(token: str, payload: dict = Body(...), request: Request = None,
                 db: Session = Depends(get_db)):
    """Server-authoritative START: ek attempt session banata/resume karta hai, server `started_at`
    + `expires_at` store karta hai, aur server time + bacha hua time deta hai. Timer isi se chalta
    hai (localStorage se nahi), isliye reload/band karne pe time reset nahi hota."""
    ex = _exam_by_token(db, token)
    sched = getattr(ex, "scheduled_at", None)
    if sched is not None and _ist_now() < sched:
        raise HTTPException(403, "This test has not started yet.")
    name, phone, _extra = _validate_details(ex, payload.get("details") or {})
    # already submitted (idempotent) -> seedha result do, dubara attempt nahi
    existing = (db.query(PublicExamAttempt)
                .filter(PublicExamAttempt.exam_id == ex.id,
                        PublicExamAttempt.phone == phone).first())
    if existing:
        return {"already": True, "result": _build_result(db, ex, existing)}
    close_at = _window_close_at(ex)
    now = _ist_now()
    # SCHEDULED WINDOW CLOSED: time khatam. Jisne pehle start kiya tha (session hai) uska
    # auto-submit karke result; naya student -> "closed" (dubara attempt nahi bana sakta).
    if close_at is not None and now >= close_at:
        _s = _get_session(db, ex.id, phone)
        if _s is not None and not _s.submitted:
            row = _finalize_session(db, ex, _s, auto=True, ip=_client_ip(request))
            return {"already": True, "result": _build_result(db, ex, row)}
        return {"closed": True, "closed_at": close_at.isoformat()}
    dur = int(ex.duration_min or 0)
    sess = _get_session(db, ex.id, phone)
    if sess is None:
        sess = PublicExamSession(
            exam_id=ex.id, phone=phone[:20], name=name[:160],
            details=(payload.get("details") or {}),
            started_at=now, duration_min=dur,
            # scheduled test -> deadline = global window end (late starter ko fresh time nahi)
            expires_at=(close_at if close_at is not None
                        else ((now + _td(minutes=dur)) if dur > 0 else None)),
            answers={}, submitted=False)
        db.add(sess)
        db.commit()
        db.refresh(sess)
    elif sess.submitted:
        # session final ho chuka par attempt nahi mila (rare) -> dubara start block
        raise HTTPException(400, "You have already submitted this test from this number.")
    # scheduled resume: deadline hamesha global window end
    if close_at is not None and sess.expires_at != close_at:
        sess.expires_at = close_at
        db.commit()
    # time already khatam + submit nahi -> server abhi finalize kare (reopen auto-submit)
    if sess.expires_at is not None and now >= sess.expires_at:
        row = _finalize_session(db, ex, sess, auto=True, ip=_client_ip(request))
        return {"already": True, "result": _build_result(db, ex, row)}
    secs_left = (max(0, int((sess.expires_at - now).total_seconds()))
                 if sess.expires_at is not None else None)
    return {"already": False, "session_id": sess.id,
            "started_at": sess.started_at.isoformat() if sess.started_at else None,
            "expires_at": sess.expires_at.isoformat() if sess.expires_at else None,
            "server_now": now.isoformat(), "seconds_left": secs_left,
            "duration_min": sess.duration_min, "saved_answers": (sess.answers or {}),
            "integrity": _integrity_cfg(ex),
            "violations": int(getattr(sess, "violations", 0) or 0)}


@router.post("/exam/{token}/save")
def public_save(token: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Autosave + heartbeat: answers server pe save (refresh pe wapas), aur server ka
    bacha hua time + expired flag wapas. Ek hi writer (phone) hai to last-write-wins safe hai."""
    ex = _exam_by_token(db, token)
    phone = _re.sub(r"[^0-9]", "", str((payload.get("phone")) or ""))
    now = _ist_now()
    sess = _get_session(db, ex.id, phone) if len(phone) >= 10 else None
    if not sess or sess.submitted:
        return {"ok": False, "server_now": now.isoformat()}
    ans = payload.get("mcq_answers")
    if isinstance(ans, dict):
        sess.answers = ans
        sess.updated_at = now
        db.commit()
    secs_left = (max(0, int((sess.expires_at - now).total_seconds()))
                 if sess.expires_at is not None else None)
    expired = (sess.expires_at is not None and now >= sess.expires_at)
    return {"ok": True, "server_now": now.isoformat(),
            "seconds_left": secs_left, "expired": bool(expired)}


@router.post("/exam/{token}/event")
def public_event(token: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Integrity event report (tab-switch, fullscreen exit, copy/paste, etc.).
    Client ek app-switch ko EK event me coalesce karta hai; server seq se dedup karta hai
    aur violation count badhata hai. STRICT me limit par pahunchte hi limit_reached=True
    (client auto-submit karega). Server policy ka maalik hai."""
    ex = _exam_by_token(db, token)
    cfg = _integrity_cfg(ex)
    phone = _re.sub(r"[^0-9]", "", str((payload.get("phone")) or ""))
    sess = _get_session(db, ex.id, phone) if len(phone) >= 10 else None
    if not sess or sess.submitted or cfg["mode"] == "OFF":
        return {"ok": False, "violations": (int(getattr(sess, "violations", 0) or 0) if sess else 0),
                "limit_reached": False}
    etype = str(payload.get("type") or "").strip().upper()[:40]
    seq = payload.get("seq")
    now = _ist_now()
    log = list(getattr(sess, "integrity_log", None) or [])
    # dedup: same client seq already recorded? (network retry safety)
    if seq is not None and any(e.get("seq") == seq for e in log[-30:]):
        reached = (cfg["auto_submit"] and int(sess.violations or 0) >= cfg["max_violations"])
        return {"ok": True, "violations": int(sess.violations or 0),
                "max_violations": cfg["max_violations"], "limit_reached": bool(reached)}
    counted = etype in _VIOLATION_TYPES
    log.append({"type": (etype or "UNKNOWN"), "at": now.isoformat(),
                "seq": seq, "counted": bool(counted)})
    sess.integrity_log = log[-100:]
    if counted:
        sess.violations = int(sess.violations or 0) + 1
    sess.updated_at = now
    db.commit()
    reached = (cfg["auto_submit"] and cfg["max_violations"] > 0
               and int(sess.violations or 0) >= cfg["max_violations"])
    return {"ok": True, "violations": int(sess.violations or 0),
            "max_violations": cfg["max_violations"], "limit_reached": bool(reached)}


@router.get("/exam/{token}/myresult")
def public_myresult(token: str, phone: str = "", db: Session = Depends(get_db)):
    """Jis phone ne submit kiya hai uska result seedha do — taaki dubara aane par student ko
    form fill na karna pade, direct result page khule. Nahi mila to found:False."""
    ex = _exam_by_token(db, token)
    ph = _re.sub(r"[^0-9]", "", str(phone or ""))
    if len(ph) < 10:
        return {"found": False}
    row = (db.query(PublicExamAttempt)
           .filter(PublicExamAttempt.exam_id == ex.id,
                   PublicExamAttempt.phone == ph).first())
    if not row:
        return {"found": False}
    return {"found": True, "result": _build_result(db, ex, row)}


@router.get("/exam/{token}/leaderboard")
def public_leaderboard(token: str, phone: str = "", db: Session = Depends(get_db)):
    """Live ranking poll — result screen har ~20s ye call karke ranking refresh karti hai.
    Scheduled test me jab tak time khatam na ho, ranking_pending True rehta hai (ranking chhupi)."""
    ex = _exam_by_token(db, token)
    return _leaderboard_payload(db, ex, my_phone=phone)


@router.post("/exam/{token}/submit")
def public_submit(token: str, payload: dict = Body(...), request: Request = None,
                  db: Session = Depends(get_db)):
    ex = _exam_by_token(db, token)
    _sched = getattr(ex, "scheduled_at", None)
    if _sched is not None and _ist_now() < _sched:
        raise HTTPException(403, "This test has not started yet.")
    name, phone, _extra = _validate_details(ex, payload.get("details") or {})

    # IDEMPOTENT: is phone ne already submit kiya -> wahi result do (dusra row nahi, error nahi).
    existing = (db.query(PublicExamAttempt)
                .filter(PublicExamAttempt.exam_id == ex.id,
                        PublicExamAttempt.phone == phone).first())
    if existing:
        sess = _get_session(db, ex.id, phone)
        if sess and not sess.submitted:
            sess.submitted = True
            sess.updated_at = _ist_now()
            db.commit()
        return _build_result(db, ex, existing)

    # SCHEDULED WINDOW CLOSED: jo student window ke andar start nahi hua (koi session nahi)
    # wo ab submit na kar sake. Jisne start kiya tha (session hai) uska submit accept (wo window me tha).
    _close_at = _window_close_at(ex)
    if _close_at is not None and _ist_now() >= _close_at:
        if _get_session(db, ex.id, phone) is None:
            raise HTTPException(403, "This test is now closed.")

    ans = payload.get("mcq_answers") or {}
    # integrity: session se violations lo (attempt me permanently store honge)
    sess = _get_session(db, ex.id, phone)
    viol = int(getattr(sess, "violations", 0) or 0) if sess else 0
    ilog = getattr(sess, "integrity_log", None) if sess else None
    row = _create_attempt(db, ex, name, phone, payload.get("details") or {},
                          ans, ip=_client_ip(request),
                          violations=viol, integrity_log=ilog)
    # session finalize (agar server timer use hua tha)
    if sess and not sess.submitted:
        sess.submitted = True
        sess.auto_submitted = bool(payload.get("auto"))
        sess.updated_at = _ist_now()
        db.commit()
    return _build_result(db, ex, row)
