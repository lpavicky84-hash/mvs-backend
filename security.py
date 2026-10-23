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


def issue_session_token(db, user):
    """Login ke waqt call karo: ek naya random session-id (sid) banata hai, use user pe store karta
    hai (purana session supersede), aur token me sid daal ke return karta hai. Isse jab bhi koi NAYE
    device pe login karega, purane device ka token mismatch ho ke apne-aap logout ho jaayega."""
    import uuid
    sid = uuid.uuid4().hex
    token = create_access_token({"sub": str(user.id), "role": user.role, "sid": sid})
    try:
        user.active_session_token = sid
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
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    # DB hiccup (deadlock / lock-wait under load) ko kabhi 401 mat banao — warna valid user
    # galti se logout ho jaata tha. Ek baar retry, phir bhi fail ho to error bubble ho (500),
    # 401 nahi — taaki frontend token clear na kare.
    from sqlalchemy.exc import OperationalError
    try:
        user = db.query(User).filter(User.id == int(user_id)).first()
    except OperationalError:
        try:
            db.rollback()
        except Exception:
            pass
        user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User nahi mila ya inactive hai")
    # SINGLE SESSION (all roles): token ka sid user ke stored active sid se match karna chahiye.
    # Agar user ne kisi NAYE device pe login kiya -> stored sid badal gaya -> is purane token ka
    # sid alag hoga -> 401 SESSION_SUPERSEDED (frontend professional message dikha ke logout karega).
    # Backward-safe: sirf tab kick karo jab token me sid HO aur stored sid bhi HO aur dono alag hon
    # (purane bina-sid token aur scoped tokens kabhi galti se logout na ho; naye login par enforce).
    try:
        _tok_sid = payload.get("sid")
        _cur_sid = getattr(user, "active_session_token", None)
        if _tok_sid and _cur_sid and _tok_sid != _cur_sid:
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
