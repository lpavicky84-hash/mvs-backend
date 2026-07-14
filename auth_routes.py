from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from datetime import datetime

from database import get_db
from security import hash_password, verify_password, create_access_token, get_current_user
from models import User, TeacherProfile, StudentProfile, UserRole
from schemas import RegisterRequest, LoginRequest, TokenResponse, UserOut

router = APIRouter(prefix="/api/auth", tags=["Auth"])

def generate_user_id(name: str, db: Session) -> str:
    """Auto-generate unique user ID from name e.g. Rahul Sharma → RS001"""
    parts = name.strip().split()
    prefix = ""
    if len(parts) >= 2:
        prefix = (parts[0][0] + parts[1][0]).upper()
    elif len(parts) == 1:
        prefix = parts[0][:2].upper()
    else:
        prefix = "US"

    # Find next available number
    i = 1
    while True:
        uid = f"{prefix}{str(i).zfill(3)}"
        existing = db.query(User).filter(User.user_id == uid).first()
        if not existing:
            return uid
        i += 1

@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    # Check if user_id taken
    existing = db.query(User).filter(User.user_id == req.user_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Yeh User ID already le li gayi hai")

    # Create user
    user = User(
        name=req.name,
        user_id=req.user_id,
        password=hash_password(req.password),
        role=req.role,
        is_active=True
    )
    db.add(user)
    db.flush()

    # Create profile based on role
    if req.role == UserRole.teacher:
        profile = TeacherProfile(
            user_id=user.id,
            subjects=req.subjects or [],
            batch=req.batch or "",
            reschedule_count_this_month=0,
            reschedule_reset_month=datetime.now().month
        )
        db.add(profile)

    elif req.role == UserRole.student:
        if not req.phone:
            raise HTTPException(status_code=400, detail="Student ke liye phone number zaroori hai")
        # Check phone uniqueness
        existing_phone = db.query(StudentProfile).filter(StudentProfile.phone == req.phone).first()
        if existing_phone:
            raise HTTPException(status_code=400, detail="Yeh phone number already registered hai")
        profile = StudentProfile(
            user_id=user.id,
            phone=req.phone,
            batch=req.batch,
            subjects=req.subjects or [],
            class_name=req.class_name or "",
            is_verified=True
        )
        db.add(profile)

    db.commit()
    db.refresh(user)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(
        access_token=token,
        role=user.role,
        name=user.name,
        user_db_id=user.id
    )

@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user or not verify_password(req.password, user.password):
        raise HTTPException(status_code=401, detail="User ID ya Password galat hai")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account inactive hai. Admin se contact karein.")

    # Single session enforcement for students
    if user.role == UserRole.student and user.student_profile:
        token = create_access_token({"sub": str(user.id), "role": user.role})
        user.student_profile.active_session_token = token
        db.commit()
        return TokenResponse(access_token=token, role=user.role, name=user.name, user_db_id=user.id)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    return TokenResponse(access_token=token, role=user.role, name=user.name, user_db_id=user.id)

@router.get("/me", response_model=UserOut)
def get_me(current_user=Depends(get_current_user)):
    return current_user

@router.get("/generate-uid")
def gen_uid(name: str, db: Session = Depends(get_db)):
    """Admin use — auto-generate user ID from name"""
    return {"user_id": generate_user_id(name, db)}

@router.get("/lookup-by-phone")
def lookup_by_phone(phone: str, db: Session = Depends(get_db)):
    """Student onboarding — phone se apni credentials fetch karein"""
    from models import StudentProfile
    phone = phone.strip()
    sp = db.query(StudentProfile).filter(StudentProfile.phone == phone).first()
    if not sp or not sp.user:
        return {"found": False}
    return {
        "found": True,
        "name": sp.user.name,
        "user_id": sp.user.user_id,
        "password": sp.plain_password or ""
    }


# ==================================================== MVS PORTAL SSO ONBOARDING
# Class Manager (MVS Portal) se aane wale students: agar CRM me already hain to
# seedha login; nahi hain (aur portal par class-access UNLOCKED hai) to unki
# details portal se auto-fetch hoti hain — student sirf apna BATCH chunta hai.
import os as _os, hmac as _hmac, hashlib as _hashlib, base64 as _b64, json as _sjson


def _sso_phone(payload):
    """Token (signed) ya seedha phone se mobile nikaalo. (phone, error) return."""
    token = (payload.get("token") or "").strip()
    if token and "." in token:
        secret = (_os.getenv("CRM_SSO_SECRET") or _os.getenv("CRON_SECRET") or "").encode()
        try:
            p64, sig = token.split(".", 1)
            pad = p64 + "=" * (-len(p64) % 4)
            pj = _sjson.loads(_b64.urlsafe_b64decode(pad))
            if secret:
                want = _b64.urlsafe_b64encode(
                    _hmac.new(secret, p64.encode(), _hashlib.sha256).digest()
                ).rstrip(b"=").decode()
                if not _hmac.compare_digest(want, sig):
                    return None, "Invalid login link. Please open Class Manager from the MVS Portal again."
            if pj.get("x") and float(pj["x"]) < datetime.now().timestamp() * 1000:
                return None, "This login link has expired. Please open Class Manager from the MVS Portal again."
            ph = "".join(ch for ch in str(pj.get("m") or "") if ch.isdigit())[-10:]
            if len(ph) == 10:
                return ph, None
        except Exception:
            return None, "Invalid login link."
    ph = "".join(ch for ch in str(payload.get("phone") or "") if ch.isdigit())[-10:]
    if len(ph) == 10:
        return ph, None
    return None, "Phone number missing."


def _portal_batches(class_level, session):
    """MVS Portal student ko sirf uske session + class ke batches dikhte hain.
       April/October  -> LIVE  : Lakshya (12) / Udaan (10)
       On Demand/SYC  -> REC.  : Safalta (12) / Jeet (10)
       Manzil aur Aarambh sirf MVS App ke liye hain — portal students ko kabhi nahi."""
    t = "".join(ch for ch in (session or "").lower() if ch.isalnum())
    syc = ("ondemand" in t) or ("syc" in t)
    cls = str(class_level or "")
    if syc:
        names = ["Safalta Batch"] if cls != "10" else ["Jeet Batch"]
        if cls not in ("10", "12"):
            names = ["Safalta Batch", "Jeet Batch"]
        mode = "rec"
    else:
        if cls == "10":
            names = ["Udaan Class 10"]
        elif cls == "12":
            names = ["Lakshya Science", "Lakshya Commerce", "Lakshya Arts"]
        else:
            names = ["Lakshya Science", "Lakshya Commerce", "Lakshya Arts", "Udaan Class 10"]
        mode = "live"
    from student_routes import STUDENT_BATCHES
    return [{"name": n, "class_level": STUDENT_BATCHES[n][0], "mode": mode}
            for n in names if n in STUDENT_BATCHES]


@router.post("/sso-lookup")
def sso_lookup(payload: dict, db: Session = Depends(get_db)):
    phone, err = _sso_phone(payload or {})
    if err:
        raise HTTPException(status_code=400, detail=err)
    sp = db.query(StudentProfile).filter(StudentProfile.phone == phone).first()
    if sp and sp.user:
        # PRIORITY RULE: student MVS Portal se aa raha hai -> uska source hamesha
        # mvs_portal ho jata hai (chahe pehle sheet/MVS App se add hua ho), aur
        # uska data portal se refresh ho jata hai.
        if (getattr(sp, "source", None) or "mvs_app") != "mvs_portal":
            try:
                from admin_routes import _sync_one_from_portal
                if _sync_one_from_portal(sp, db):
                    db.commit()
            except Exception:
                db.rollback()
        return {"found": True, "name": sp.user.name,
                "user_id": sp.user.user_id, "password": sp.plain_password or ""}
    # CRM me nahi — MVS Portal se details
    from ext_materials import portal_fetch_student, portal_probe_student
    st = portal_fetch_student(phone)
    if not st:
        code, msg = portal_probe_student(phone)
        return {"found": False, "portal": False, "reason": code, "detail": msg}
    if not st["unlocked"]:
        return {"found": False, "portal": True, "locked": True, "name": st["name"]}
    batches = _portal_batches(st["class_level"], st["session"])
    return {"found": False, "portal": True, "locked": False,
            "profile": {"name": st["name"], "phone": phone, "class_level": st["class_level"],
                        "medium": st["medium"], "subjects": st["subjects"], "session": st["session"]},
            "batches": batches}


@router.post("/sso-register")
def sso_register(payload: dict, db: Session = Depends(get_db)):
    phone, err = _sso_phone(payload or {})
    if err:
        raise HTTPException(status_code=400, detail=err)
    batch_name = (payload.get("batch_name") or "").strip()
    from student_routes import STUDENT_BATCHES
    if batch_name not in STUDENT_BATCHES:
        raise HTTPException(status_code=400, detail="Please select a valid batch")
    if batch_name in ("Manzil Batch", "Aarambh Batch"):
        raise HTTPException(status_code=400,
                            detail="This batch is not available for MVS Portal students.")
    # already exists? (double-tap safety)
    sp = db.query(StudentProfile).filter(StudentProfile.phone == phone).first()
    if sp and sp.user:
        token = create_access_token({"sub": str(sp.user.id), "role": sp.user.role})
        return {"access_token": token, "role": "student", "name": sp.user.name, "existing": True}
    # portal par verify (server-to-server — spoof-proof)
    from ext_materials import portal_fetch_student
    st = portal_fetch_student(phone)
    if not st:
        raise HTTPException(status_code=404, detail="No MVS Portal student found for this phone. Please contact the admin.")
    if not st["unlocked"]:
        raise HTTPException(status_code=403, detail="Your class access is not unlocked yet. Please contact the admin.")
    allowed = {b["name"] for b in _portal_batches(st["class_level"], st["session"])}
    if batch_name not in allowed:
        raise HTTPException(status_code=400,
                            detail="This batch is not available for your class/session. Please pick one of the shown batches.")
    name = st["name"] or ("Student " + phone[-4:])
    i = 1
    while True:
        cand = f"MVSS{i:04d}"
        if not db.query(User).filter(User.user_id == cand).first():
            break
        i += 1
    u = User(name=name, user_id=cand, password=hash_password(phone),
             role=UserRole.student, is_active=True)
    db.add(u); db.flush()
    db.add(StudentProfile(user_id=u.id, phone=phone,
                          subjects=st["subjects"] or [],
                          class_level=st["class_level"] or STUDENT_BATCHES[batch_name][0],
                          medium=st["medium"], batch_name=batch_name,
                          class_name="", is_verified=True, plain_password=phone,
                          source="mvs_portal"))
    db.commit()
    token = create_access_token({"sub": str(u.id), "role": u.role})
    return {"access_token": token, "role": "student", "name": name, "user_id": cand}


# ==================================================== PRESENCE (all roles)
SESSION_IDLE_MIN = 3      # no ping for this long => the session is considered over


@router.post("/ping")
def presence_ping(payload: dict = None, request: Request = None,
                  db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Heartbeat from any logged-in user. Keeps the live view fresh, records which
    section they are on, and starts a new session row after an idle gap (which is
    what makes the login count meaningful)."""
    from models import UserSession
    from datetime import timedelta
    payload = payload or {}
    page = (payload.get("page") or "").strip()[:40] or None
    now = datetime.now()
    role = getattr(current_user.role, "value", str(current_user.role))
    s = db.query(UserSession).filter(
        UserSession.user_id == current_user.id).order_by(UserSession.last_seen.desc()).first()
    if s and s.last_seen and (now - s.last_seen) <= timedelta(minutes=SESSION_IDLE_MIN):
        s.last_seen = now
        if page:
            s.current_page = page
    else:
        s = UserSession(user_id=current_user.id, role=role, started_at=now,
                        last_seen=now, current_page=page,
                        ip=(request.client.host if request and request.client else None))
        db.add(s)
    # keep the student's own columns in sync (used elsewhere)
    if role == "student":
        sp = db.query(StudentProfile).filter(StudentProfile.user_id == current_user.id).first()
        if sp:
            if not sp.session_start or not sp.last_seen or (now - sp.last_seen) > timedelta(minutes=SESSION_IDLE_MIN):
                sp.session_start = now
            sp.last_seen = now
    db.commit()
    return {"ok": True}
