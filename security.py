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
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("ACCESS_TOKEN_EXPIRE_HOURS", 24))

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
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User nahi mila ya inactive hai")
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
