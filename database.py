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
    # Hobby plan: chhota MySQL + kam RAM. Bahut bada pool (80) MySQL ki apni RAM kha
    # deta tha aur RAM spike karta tha. Ab 50 max — connection-hold fixes ke saath fast
    # turnover, kam RAM. (Lakhs students ke liye Pro/bigger DB chahiye — infra note dekho.)
    pool_size=20,             # 30 -> 20
    max_overflow=30,          # 50 -> 30 (peak par 50 tak; pehle 80)
    # SABSE ZAROORI: overload par request 6s me fail ho (pehle 20s). Isse blocked thread
    # turant free hota hai -> FastAPI threadpool saturate nahi hota -> DB-free /health bhi
    # chalti rehti hai -> Railway app ko healthy dekhta hai -> RESTART/DEATH-SPIRAL nahi.
    pool_timeout=6,           # 20 -> 6 (fail-fast, graceful degradation)
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
