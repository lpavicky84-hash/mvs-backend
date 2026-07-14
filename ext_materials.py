"""
EXTERNAL STUDY MATERIAL BRIDGE
==============================
Connects this CRM to the MVS Foundation Student Portal (Admin Console,
Backend V7.1) so that all study materials already uploaded there —
Notes, Question Bank, Syllabus, PYQs, TMA Solutions, Practical
Solutions, etc. — appear inside the Student & Teacher portals here
WITHOUT re-uploading anything.

How it works
------------
  CRM (this app)  --->  Student Portal  /api/integration/materials       (JSON list)
  CRM (this app)  --->  Student Portal  /api/integration/material/{id}/file  (file bytes)

Both calls are server-to-server with a shared secret key, so the key is
never exposed to browsers and no CORS setup is needed on either side.
The list is cached in memory for 5 minutes to keep things fast.

Railway environment variables (this CRM service)
------------------------------------------------
  STUDENT_PORTAL_URL = https://<student-portal-backend-domain>      (no trailing slash)
  STUDENT_PORTAL_KEY = <same secret that the Student Portal checks>

If these are not set, the endpoints respond with {"configured": false}
and the frontend shows a friendly "connection pending" card — nothing
breaks.
"""
import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_db
from security import get_current_user

router = APIRouter(prefix="/api/ext", tags=["External Study Material"])

CACHE_SECONDS = 300          # material list cache (5 min)
FILE_CACHE_SECONDS = 3600    # small per-file cache (1 hr)
FILE_CACHE_MAX = 20          # keep at most N files in memory

_list_cache = {"data": None, "ts": 0.0}
_file_cache = {}             # id -> {"bytes":..., "ctype":..., "ts":...}


def _cfg():
    url = (os.getenv("STUDENT_PORTAL_URL") or "").strip().rstrip("/")
    key = (os.getenv("STUDENT_PORTAL_KEY") or "").strip()
    return url, key


def _normalize(raw):
    """Accept slightly different field names from the Student Portal and
    normalize into the shape the frontend expects."""
    out = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        link = m.get("link") or m.get("external_link") or m.get("url") or ""
        kind = m.get("kind") or ("link" if link and not m.get("has_file") else "file")
        out.append({
            "id": str(m.get("id") or m.get("material_id") or ""),
            "title": m.get("title") or m.get("name") or "Untitled",
            "category": m.get("category") or m.get("tab") or m.get("section") or "Other",
            "session": m.get("session") or m.get("batch") or m.get("stream") or "",
            "class_level": str(m.get("class_level") or m.get("class") or "") or None,
            "subject": m.get("subject") or "",
            "medium": m.get("medium") or "",
            "kind": kind,
            "link": link or None,
            "filename": m.get("filename") or "",
            "updated_at": m.get("updated_at") or m.get("created_at") or "",
        })
    # newest first if timestamps present
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


# ------------------------------------------------------------------
#  STUDENT-AWARE FILTERING
#  Batch decides session + class; medium & chosen subjects narrow it
#  further, exactly like the MVS Portal shows each student their own
#  materials. Teachers/admins see everything (they filter in the UI).
# ------------------------------------------------------------------
def _norm(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def _sess_bucket(session_text):
    """Classify a material's session into 'stream2' / 'syc'.
    MVS Portal par har material kisi ek session ka hota hai — blank session
    ko default (April/October/Stream 2) maana jata hai, warna woh dono
    tabs mein duplicate dikhta hai."""
    t = _norm(session_text)
    if "ondemand" in t or "syc" in t:
        return "syc"
    return "stream2"


def _batch_bucket(batch_name):
    """Student's batch -> (session_bucket, class_level)."""
    b = (batch_name or "").lower()
    if "safalta" in b:
        return "syc", "12"
    if "jeet" in b:
        return "syc", "10"
    if "udaan" in b or "aarambh" in b:
        return "stream2", "10"
    if "lakshya" in b or "manzil" in b:
        return "stream2", "12"
    return None, None


def _subject_match(mat_subject, student_subjects_norm):
    """Material subject vs student's chosen subjects (fuzzy, code-tolerant).
    Materials with no subject (syllabus, sample papers, etc.) show to all."""
    m = _norm(mat_subject)
    if not m:
        return True
    m_nodigits = re.sub(r"\d+", "", m)  # "dataentry336" -> "dataentry" (code stripped)
    for stu in student_subjects_norm:
        if not stu:
            continue
        stu_nd = re.sub(r"\d+", "", stu)
        for a in {m, m_nodigits}:
            for b in {stu, stu_nd}:
                if not a or not b:
                    continue
                if a == b:
                    return True
                shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
                if len(shorter) >= 4 and shorter in longer:
                    return True
    return False


def _filter_for_student(mats, sp):
    sess, cls = _batch_bucket(getattr(sp, "batch_name", None) or
                              (getattr(sp, "batch", None).value if getattr(sp, "batch", None) else ""))
    if not cls:
        cls = getattr(sp, "class_level", None)
    medium = (getattr(sp, "medium", None) or "").lower()
    subs_norm = [_norm(x) for x in (getattr(sp, "subjects", None) or [])]

    out = []
    for m in mats:
        # session (bucket-based; blank = stream2 default)
        if sess and _sess_bucket(m.get("session")) != sess:
            continue
        # class
        mc = _norm(m.get("class_level"))
        if cls and mc and mc != _norm(cls):
            continue
        # medium ('Both' / blank always passes)
        mm = (m.get("medium") or "").lower()
        if medium and mm and mm not in ("both", "bilingual") and mm != medium:
            continue
        # subject
        if subs_norm and not _subject_match(m.get("subject"), subs_norm):
            continue
        out.append(m)
    ctx = {"batch": getattr(sp, "batch_name", None) or "",
           "class_level": cls or "", "medium": getattr(sp, "medium", None) or "",
           "session_bucket": sess or ""}
    return out, ctx


def _filter_for_teacher(mats, tp):
    """Teachers see only the subjects (and classes) they teach.
    subject_classes: [{"subject":"Physics","class":"12"}, ...]"""
    sc = tp.subject_classes or []
    subs = [x.get("subject") for x in sc if isinstance(x, dict) and x.get("subject")]
    if not subs:
        subs = tp.subjects or []
    subs_norm = [_norm(x) for x in subs]
    if not subs_norm:          # profile incomplete -> show nothing extra, not everything
        return [], {"subjects": []}
    # NOTE: teachers multiple batches me padhate hain — isliye session/batch par
    # filter NAHI hota. Frontend ke session tabs se woh batch-wise dekh lete hain.
    # per-subject allowed classes (agar available hon)
    cls_map = {}
    for x in sc:
        if isinstance(x, dict) and x.get("subject"):
            cls_map.setdefault(_norm(x["subject"]), set()).add(str(x.get("class") or ""))

    out = []
    for m in mats:
        msub = m.get("subject") or ""
        if msub and not _subject_match(msub, subs_norm):
            continue
        if not msub:
            # general items (syllabus/sample papers without subject) sabko dikhao
            out.append(m)
            continue
        # class check: material ki class teacher ki us subject ki classes me honi chahiye
        mc = _norm(m.get("class_level"))
        if mc:
            allowed = set()
            for sn, classes in cls_map.items():
                a, b = (sn, _norm(msub)) if len(sn) <= len(_norm(msub)) else (_norm(msub), sn)
                if sn == _norm(msub) or (len(a) >= 4 and a in b):
                    allowed |= {c for c in classes if c}
            if allowed and mc not in {_norm(c) for c in allowed}:
                continue
        out.append(m)
    return out, {"subjects": subs}


def _fetch_list(refresh=False):
    url, key = _cfg()
    now = time.time()
    if not refresh and _list_cache["data"] is not None and (now - _list_cache["ts"]) < CACHE_SECONDS:
        return _list_cache["data"], True, False
    try:
        r = httpx.get(url + "/api/integration/materials",
                      headers={"X-MVS-KEY": key}, timeout=30)
        r.raise_for_status()
        data = r.json()
        raw = data.get("materials") if isinstance(data, dict) else data
        mats = _normalize(raw)
        _list_cache["data"] = mats
        _list_cache["ts"] = now
        return mats, False, False
    except Exception as e:
        if _list_cache["data"] is not None:
            return _list_cache["data"], True, True
        raise HTTPException(status_code=502,
                            detail=f"Could not connect to MVS Portal: {e}")


def portal_get(path, timeout=20):
    """Server-to-server GET to the MVS Portal integration API. None on any failure."""
    url, key = _cfg()
    if not url or not key:
        return None
    try:
        r = httpx.get(url + path, headers={"X-MVS-KEY": key}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def portal_probe_student(phone):
    """Diagnostic: (status, detail) — kyun student nahi mila."""
    url, key = _cfg()
    if not url or not key:
        return "not_configured", "STUDENT_PORTAL_URL / STUDENT_PORTAL_KEY Railway par set nahi hain."
    try:
        r = httpx.get(url + f"/api/integration/student?phone={phone}",
                      headers={"X-MVS-KEY": key}, timeout=20)
    except Exception as e:
        return "unreachable", f"MVS Portal se connect nahi ho paya: {e}"
    if r.status_code == 404:
        return "endpoint_missing", "MVS Portal par /api/integration/student endpoint abhi bana hi nahi hai (404)."
    if r.status_code == 401:
        return "bad_key", "MVS Portal ne key reject kar di (401). STUDENT_PORTAL_KEY match nahi kar rahi."
    if r.status_code != 200:
        return "http_error", f"MVS Portal ne {r.status_code} return kiya."
    try:
        d = r.json()
    except Exception:
        return "bad_json", "MVS Portal ne valid JSON nahi bheja."
    if not d.get("found"):
        return "not_on_portal", "Is phone se koi student MVS Portal par nahi mila."
    if not _is_included(d):
        return "locked", "Is student ka Class Access 'Not Included' hai (MVS Portal par 'Included' hona chahiye)."
    return "ok", d


def _is_included(d):
    """MVS Portal ka 'Classes (Class Joining)' field: Included / Not Included.
    Boolean flag bhi accept karte hain (jo bhi format portal bheje)."""
    for k in ("class_joining", "class_access", "classes", "class_access_status"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            t = _norm(v)                      # "not included" -> "notincluded"
            return t.startswith("included") or t == "yes" or t == "true"
        if isinstance(v, bool):
            return v
    v = d.get("class_access_unlocked")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = _norm(v)
        return t.startswith("included") or t in ("yes", "true")
    return True   # field hi nahi bheja -> block mat karo


def portal_fetch_student(phone):
    """MVS Portal se ek student ki details (SSO onboarding ke liye)."""
    d = portal_get(f"/api/integration/student?phone={phone}")
    if not d or not d.get("found"):
        return None
    subs = []
    for x in (d.get("subjects") or []):
        nm = x.get("name") if isinstance(x, dict) else str(x)
        nm = (nm or "").split("(")[0].strip()
        if nm:
            subs.append(nm)
    return {
        "name": (d.get("name") or "").strip() or None,
        "phone": str(d.get("phone") or phone),
        "class_level": str(d.get("class_level") or d.get("class") or "") or None,
        "medium": (d.get("medium") or "").strip() or None,
        "session": (d.get("session") or "").strip(),
        "subjects": subs,
        "unlocked": _is_included(d),
    }


def portal_unlocked_students():
    """Sabhi unlocked (class-access) portal students — pending list ke liye."""
    d = portal_get("/api/integration/unlocked-students", timeout=30)
    if not d:
        return None
    return d.get("students", d if isinstance(d, list) else [])


@router.get("/materials")
def ext_materials(refresh: int = 0, db: Session = Depends(get_db),
                  current_user=Depends(get_current_user)):
    """Study materials from the Student Portal.
    Students get a personalised list (batch/session + class + medium + subjects);
    teachers and admins get everything."""
    url, key = _cfg()
    if not url or not key:
        return {"configured": False, "materials": []}

    mats, cached, stale = _fetch_list(refresh=bool(refresh))

    role = getattr(current_user.role, "value", str(current_user.role))
    if role == "student":
        from models import StudentProfile
        sp = db.query(StudentProfile).filter(
            StudentProfile.user_id == current_user.id).first()
        if sp:
            mats, ctx = _filter_for_student(mats, sp)
            return {"configured": True, "cached": cached, "stale": stale,
                    "role": "student", "ctx": ctx, "materials": mats}

    if role == "teacher":
        from models import TeacherProfile
        tp = db.query(TeacherProfile).filter(
            TeacherProfile.user_id == current_user.id).first()
        if tp:
            mats, ctx = _filter_for_teacher(mats, tp)
            return {"configured": True, "cached": cached, "stale": stale,
                    "role": "teacher", "ctx": ctx, "materials": mats}

    # admin: full library
    return {"configured": True, "cached": cached, "stale": stale,
            "role": role, "materials": mats}


@router.get("/material/{mid}/file")
def ext_material_file(mid: str, current_user=Depends(get_current_user)):
    """Stream one material file from the Student Portal to the browser."""
    url, key = _cfg()
    if not url or not key:
        raise HTTPException(status_code=503, detail="MVS Portal connection is not configured")

    now = time.time()
    c = _file_cache.get(mid)
    if c and (now - c["ts"]) < FILE_CACHE_SECONDS:
        return Response(content=c["bytes"], media_type=c["ctype"],
                        headers={"Content-Disposition": c["disp"]})

    try:
        r = httpx.get(f"{url}/api/integration/material/{mid}/file",
                      headers={"X-MVS-KEY": key}, timeout=120,
                      follow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"File fetch failed: {e}")

    ctype = r.headers.get("content-type", "application/pdf")
    disp = r.headers.get("content-disposition") or f'inline; filename="material_{mid}.pdf"'

    # tiny in-memory cache so repeated opens are instant
    if len(_file_cache) >= FILE_CACHE_MAX:
        oldest = min(_file_cache, key=lambda k: _file_cache[k]["ts"])
        _file_cache.pop(oldest, None)
    _file_cache[mid] = {"bytes": r.content, "ctype": ctype, "disp": disp, "ts": now}

    return Response(content=r.content, media_type=ctype,
                    headers={"Content-Disposition": disp})


@router.get("/student-check")
def ext_student_check(phone: str, current_user=Depends(get_current_user)):
    """Admin debug: is phone ke liye MVS Portal se kya response aa raha hai."""
    ph = "".join(ch for ch in str(phone) if ch.isdigit())[-10:]
    status, detail = portal_probe_student(ph)
    return {"phone": ph, "status": status,
            "detail": detail if isinstance(detail, str) else "Student mil gaya",
            "data": detail if not isinstance(detail, str) else None}


@router.get("/status")
def ext_status(current_user=Depends(get_current_user)):
    """Quick connection health check (used for debugging)."""
    url, key = _cfg()
    if not url or not key:
        return {"configured": False}
    try:
        r = httpx.get(url + "/api/integration/materials",
                      headers={"X-MVS-KEY": key}, timeout=15)
        ok = r.status_code == 200
        n = 0
        if ok:
            d = r.json()
            n = len(d.get("materials", d if isinstance(d, list) else []))
        return {"configured": True, "reachable": ok, "status_code": r.status_code, "count": n}
    except Exception as e:
        return {"configured": True, "reachable": False, "error": str(e)}
