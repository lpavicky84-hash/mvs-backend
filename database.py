import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:password@localhost:3306/mvs_foundation"
)

# Railway sometimes gives "mysql://" — convert to pymysql driver
if DATABASE_URL.startswith("mysql://"):
    DATABASE_URL = DATABASE_URL.replace("mysql://", "mysql+pymysql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,       # dead connection auto-refresh (Railway MySQL idle drop)
    pool_recycle=280,         # MySQL wait_timeout se pehle connection recycle
    pool_size=20,             # 10 -> 20 (base connections zyada)
    max_overflow=40,          # 20 -> 40 (peak par 60 tak jaa sakta hai)
    pool_timeout=30,          # connection ke liye max 30s wait
    pool_use_lifo=True,       # warm connection dobara use (spiky load me behtar, idle kam)
    connect_args={"connect_timeout": 10},  # DB connect 10s me fail ho (hang na kare)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
