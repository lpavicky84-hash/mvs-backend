import os
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from database import get_db
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "mvs-foundation-change-this")
ALGORITHM = "HS256"
# Staff/teachers/students keep the portal open for days — a 24h token logged them out
# mid-work ("Token invalid ya expire ho gaya"). 30-day default (env can still override).
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", 720))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def _pw_bytes(password: str) -> bytes:
    # bcrypt only uses the first 72 bytes — truncate to avoid ValueError on bcrypt 4.x
    return (password or "").encode("utf-8")[:72]


def hash_password(password: str) -> str:
    # Use bcrypt directly (version-independent). passlib's bcrypt backend self-test
    # breaks on newer bcrypt builds ("password cannot be longer than 72 bytes"), which
    # would make every teacher/student add fail. Direct bcrypt avoids that entirely.
    try:
        import bcrypt as _bcrypt
        return _bcrypt.hashpw(_pw_bytes(password), _bcrypt.gensalt()).decode("utf-8")
    except Exception:
        return pwd_context.hash((password or "")[:72])


def verify_password(plain: str, hashed: str) -> bool:
    try:
        import bcrypt as _bcrypt
        return _bcrypt.checkpw(_pw_bytes(plain), (hashed or "").encode("utf-8"))
    except Exception:
        try:
            return pwd_context.verify((plain or "")[:72], hashed)
        except Exception:
            return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


# ---- Single-session (all roles) ----
# Code the frontend looks for to show the "signed in on another device" message.
SESSION_SUPERSEDED = "SESSION_SUPERSEDED"


def _role_val(user):
    try:
        return getattr(user.role, "value", user.role)
    except Exception:
        return None


def _ua_is_mobile(request):
    """User-Agent se phone/tablet vs computer pehchano. Phone => mobile slot, warna computer slot.
    (iPad iPadOS 13+ Mac jaisa UA bhejta hai -> computer slot, theek hai.)"""
    try:
        ua = (request.headers.get("user-agent") or "") if request is not None else ""
    except Exception:
        ua = ""
    import re as _re
    return bool(_re.search(r"(Mobile|Android|iPhone|iPod|Windows Phone|BlackBerry|Opera Mini|IEMobile|webOS)", ua or "", _re.I))


def issue_session_token(db, user, request=None):
    """Login ke waqt call karo: naya random session-id (sid) banata hai aur token me daal ke return.
    - STUDENT: do slot — 1 mobile + 1 computer. Login ki device-type ka slot naye sid se replace hota
      hai (doosra slot waisa hi rehta hai) -> ek phone + ek computer saath; usi type ki nayi device
      purani ko logout karti hai. Teen device (2 phone / 2 computer) kabhi saath active nahi.
    - NON-STUDENT (teacher/admin/production/editor/youtuber/graphics): koi single-session enforce NAHI
      -> ek saath kitni bhi device par login chal sakta hai (team ki problem fix)."""
    import uuid, json
    sid = uuid.uuid4().hex
    token = create_access_token({"sub": str(user.id), "role": user.role, "sid": sid})
    try:
        if _role_val(user) == "student":
            slot = "m" if _ua_is_mobile(request) else "d"
            cur = {}
            try:
                cur = json.loads(getattr(user, "active_sessions", None) or "{}")
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
            cur[slot] = sid
            cur = {k: cur[k] for k in ("m", "d") if cur.get(k)}
            user.active_sessions = json.dumps(cur)
            user.active_session_token = sid   # legacy/online-detect field (students)
        else:
            # non-students: store last sid for info, but get_current_user ismein enforce NAHI karta
            user.active_session_token = sid
            user.active_sessions = None
        db.add(user)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return token

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid ya expire ho gaya"
        )

def get_current_user(request: Request, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    from models import User
    # Authorization header primary; agar na ho (jaise <img src> / window.open jo header nahi
    # bhej sakte) to ?t= / ?token= query se lo. Isse R2 file seedha browser se load ho jaati.
    if not token:
        token = request.query_params.get("t") or request.query_params.get("token")
    if not token:
        # iOS "Add to Home Screen" PWA par localStorage relaunch pe kho sakti hai -> student
        # logout dikhta tha. Login par server ne jo persistent httpOnly cookie (mvs_auth) set ki
        # thi usse auth kar lo -> session bulletproof bana rehta hai.
        try:
            token = request.cookies.get("mvs_auth")
        except Exception:
            token = None
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    # DB hiccup (deadlock / lock-wait under load) ko kabhi 401 mat banao — warna valid user
    # galti se logout ho jaata tha. Ek baar retry, phir bhi fail ho to error bubble ho (500),
    # 401 nahi — taaki frontend token clear na kare.
    from sqlalchemy.exc import (OperationalError, ResourceClosedError,
                                InvalidRequestError, StatementError, DBAPIError,
                                PendingRollbackError)
    _DB_TRANSIENT = (OperationalError, ResourceClosedError, InvalidRequestError,
                     StatementError, DBAPIError, PendingRollbackError)
    try:
        user = db.query(User).filter(User.id == int(user_id)).first()
    except _DB_TRANSIENT:
        # Dead/stale connection (jaise lambe WhatsApp send ke dauraan MySQL ne idle conn maar di)
        # ko kabhi 401/500 mat banao. Connection reset karke ek baar retry -> fresh pre-pinged conn.
        try:
            db.rollback()
        except Exception:
            try:
                db.close()
            except Exception:
                pass
        try:
            user = db.query(User).filter(User.id == int(user_id)).first()
        except _DB_TRANSIENT:
            try:
                db.rollback()
            except Exception:
                pass
            user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User nahi mila ya inactive hai")
    # SESSION ENFORCEMENT — sirf STUDENTS par (Class Manager). Baaki sabhi roles
    # (teacher/admin/production/editor/youtuber/graphics) par KOI enforcement nahi ->
    # ek saath multiple device par login allowed (team ki problem fix).
    # Student: token ka sid do allowed slots (mobile/computer) me se kisi se match hona chahiye.
    # Backward-safe: active_sessions set na ho (purane logged-in student / bina-sid token) to kick NAHI.
    try:
        if _role_val(user) == "student":
            _tok_sid = payload.get("sid")
            if _tok_sid:
                import json
                _raw = getattr(user, "active_sessions", None)
                if _raw:
                    try:
                        _slots = json.loads(_raw) or {}
                    except Exception:
                        _slots = {}
                    _valid = set(v for v in _slots.values() if v)
                    if _valid and _tok_sid not in _valid:
                        raise HTTPException(status_code=401, detail=SESSION_SUPERSEDED)
    except HTTPException:
        raise
    except Exception:
        pass
    return user

def require_role(*roles):
    def checker(current_user=Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required roles: {roles}"
            )
        return current_user
    return checker

get_teacher = require_role("teacher")
get_admin   = require_role("admin")
get_student = require_role("student")
get_any     = require_role("admin", "teacher", "student")

# ---- Production ecosystem role guards ----
get_production_manager = require_role("production_manager")
get_editor             = require_role("editor")
get_youtuber           = require_role("youtuber")
get_graphics           = require_role("graphics")
# Admin has oversight over the whole production ecosystem.
get_pm_or_admin        = require_role("production_manager", "admin")
get_production_any     = require_role(
    "admin", "production_manager", "editor", "youtuber", "graphics")
