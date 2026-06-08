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
    ]
    for s in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(s))
                conn.commit()
        except Exception:
            pass  # column already exists — safe to ignore
ensure_columns()

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
