import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from database import engine, Base
from sqlalchemy import text
import models  # triggers model registration
import auth_routes
import teacher_routes
import admin_routes
import student_routes

load_dotenv()

# ===== CREATE TABLES =====
Base.metadata.create_all(bind=engine)

# ===== LIGHTWEIGHT MIGRATIONS (add new columns to existing tables) =====
def ensure_columns():
    stmts = [
        "ALTER TABLE student_profiles ADD COLUMN plain_password VARCHAR(255)",
        "ALTER TABLE student_profiles ADD COLUMN class_level VARCHAR(5)",
        "ALTER TABLE timetable_entries ADD COLUMN time_text VARCHAR(40)",
        "ALTER TABLE timetable_entries ADD COLUMN entry_type VARCHAR(20)",
        "ALTER TABLE teacher_profiles ADD COLUMN gender VARCHAR(10)",
        "ALTER TABLE teacher_profiles ADD COLUMN subject_classes JSON",
        "ALTER TABLE materials ADD COLUMN category VARCHAR(60)",
        "ALTER TABLE materials ADD COLUMN marks VARCHAR(20)",
        "ALTER TABLE timetable_entries ADD COLUMN status VARCHAR(20)",
        "ALTER TABLE doubts ADD COLUMN image_b64 LONGTEXT",
        "ALTER TABLE teacher_profiles ADD COLUMN phone VARCHAR(15)",
        "ALTER TABLE teacher_profiles ADD COLUMN photo_b64 LONGTEXT",
        "ALTER TABLE student_profiles ADD COLUMN photo_b64 LONGTEXT",
        "ALTER TABLE student_profiles ADD COLUMN batch_name VARCHAR(160)",
        "ALTER TABLE student_profiles ADD COLUMN email VARCHAR(160)",
        "ALTER TABLE materials ADD COLUMN medium VARCHAR(20)",
        "ALTER TABLE materials ADD COLUMN is_global BOOLEAN DEFAULT 0",
        "ALTER TABLE materials ADD COLUMN external_link VARCHAR(500)",
        "ALTER TABLE timetable_entries ADD COLUMN completed BOOLEAN DEFAULT 0",
        "ALTER TABLE timetable_entries ADD COLUMN completed_at DATETIME",
        "ALTER TABLE timetable_entries ADD COLUMN topic_covered VARCHAR(300)",
        "ALTER TABLE timetable_entries ADD COLUMN start_time VARCHAR(20)",
        "ALTER TABLE timetable_entries ADD COLUMN end_time VARCHAR(20)",
        "ALTER TABLE timetable_entries ADD COLUMN homework TEXT",
        "ALTER TABLE timetable_entries ADD COLUMN dpp_given BOOLEAN DEFAULT 0",
        "ALTER TABLE timetable_entries ADD COLUMN remarks TEXT",
        "ALTER TABLE materials ADD COLUMN approval_status VARCHAR(20) DEFAULT 'approved'",
        "ALTER TABLE student_profiles ADD COLUMN last_seen DATETIME",
        "ALTER TABLE student_profiles ADD COLUMN session_start DATETIME",
    ]
    for s in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(s))
                conn.commit()
        except Exception:
            pass  # column already exists — safe to ignore
ensure_columns()

# ===== SEED AVAILABLE SUBJECTS (NIOS lists) — only if table empty =====
def seed_subjects():
    from database import SessionLocal
    from models import AvailableSubject
    db = SessionLocal()
    try:
        if db.query(AvailableSubject).count() > 0:
            return  # already seeded — don't re-add (preserves admin deletions)
        class10 = [
            ("Hindi","201"),("English","202"),("Mathematics","211"),
            ("Science and Technology","212"),("Social Science","213"),
            ("Economics","214"),("Business Studies","215"),("Home Science","216"),
            ("Psychology","222"),("Indian Culture and Heritage","223"),
            ("Accountancy","224"),("Painting","225"),("Data Entry Operations","229"),
        ]
        class12 = [
            ("Hindi","301"),("English","302"),("Sanskrit","309"),
            ("Mathematics","311"),("Physics","312"),("Chemistry","313"),
            ("Biology","314"),("History","315"),("Geography","316"),
            ("Political Science","317"),("Economics","318"),("Business Studies","319"),
            ("Accountancy","320"),("Home Science","321"),("Psychology","328"),
            ("Computer Science","330"),("Sociology","331"),("Painting","332"),
            ("Environmental Science","333"),("Mass Communication","335"),
            ("Data Entry Operations","336"),("Introduction to Law","338"),
            ("Library and Information Science","339"),
        ]
        for name, code in class10:
            db.add(AvailableSubject(class_level="10", name=name, code=code, is_active=True))
        for name, code in class12:
            db.add(AvailableSubject(class_level="12", name=name, code=code, is_active=True))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
seed_subjects()

# ===== APP =====
app = FastAPI(
    title="MVS Foundation CRM API",
    description="Teacher · Student · Admin Portal Backend",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ===== CORS =====
# allow_credentials must be False when using "*" so that local HTML files
# (file:// origin = "null") and any browser can connect without CORS errors.
frontend_url = os.getenv("FRONTEND_URL", "*")
if frontend_url == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ===== ROUTERS =====
app.include_router(auth_routes.router)
app.include_router(teacher_routes.router)
app.include_router(admin_routes.router)
app.include_router(student_routes.router)

# ===== ROOT =====
@app.get("/")
def root():
    return {
        "app": "MVS Foundation CRM",
        "version": "1.0.0",
        "status": "running ✅",
        "docs": "/docs",
        "portals": ["teacher", "admin", "student"]
    }

@app.get("/health")
def health():
    return {"status": "ok"}
