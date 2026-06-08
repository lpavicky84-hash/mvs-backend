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
