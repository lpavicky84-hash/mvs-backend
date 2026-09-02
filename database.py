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
    # POOL IS PER-WORKER. total connections = workers x (pool_size + max_overflow).
    # With WEB_CONCURRENCY=2 this is 2 x (15+15) = 60 — comfortably under MySQL's ~150 limit,
    # and far less idle-connection RAM than the old 30/40 (which was 70/worker -> exploded with
    # auto-scaled workers). Fast connection release + fail-fast keep peaks graceful.
    pool_size=15,
    max_overflow=15,          # peak par 30 per worker
    # SABSE ZAROORI (har plan par): overload par request 8s me fail ho (pehle 20s). Blocked
    # thread turant free -> FastAPI threadpool saturate nahi -> DB-free /health chalti rehti
    # hai -> Railway app ko healthy dekhta hai -> koi RESTART/DEATH-SPIRAL nahi. App graceful
    # degrade karta hai (kuch requests fast-error), CRASH nahi.
    pool_timeout=8,           # 20 -> 8 (fail-fast, graceful degradation)
    pool_use_lifo=True,       # warm connection dobara use (spiky load me behtar, idle kam)
    connect_args={"connect_timeout": 10},  # DB connect 10s me fail ho (hang na kare)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # request crash (e.g. IntegrityError) -> aborted transaction ko rollback karke
        # connection saaf return karo (poisoned session/pool na bane).
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
