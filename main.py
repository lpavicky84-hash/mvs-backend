import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from database import engine, Base
from sqlalchemy import text
import models  # triggers model registration
import category_models  # category workspace tables (additive, Phase 3)
import support_models   # complaints & feedback tables (additive)
import support_routes   # complaints & feedback API (additive)
import auth_routes
import teacher_routes
import admin_routes
import student_routes
import ext_materials
import syllabus_routes
import video_tasks
# Production ecosystem (v-prod)
import production_routes
import editor_routes
import youtuber_routes
import graphics_routes
import translation_routes
import category_routes  # category workspace API (Phase 4)

load_dotenv()

# ===== CREATE TABLES (startup-resilient) =====
# v166: agar DB ek pal ke liye busy/slow ho (pool pressure), create_all startup ko
# crash NAHI karega — warna Railway healthcheck fail ho jaata hai aur naya deploy hi
# live nahi ho paata. Tables pehle se bani hain, isliye fail hone par bhi app start
# hone do; DB free hote hi normal chalega.
import time as _boot_time
for _boot_try in range(3):
    try:
        Base.metadata.create_all(bind=engine)
        break
    except Exception as _boot_err:
        try:
            print("create_all attempt", _boot_try + 1, "failed:", str(_boot_err)[:200])
        except Exception:
            pass
        if _boot_try < 2:
            _boot_time.sleep(3)

# ===== LIGHTWEIGHT MIGRATIONS (add new columns to existing tables) =====
def ensure_columns():
    stmts = [
        "ALTER TABLE student_profiles ADD COLUMN plain_password VARCHAR(255)",
        "ALTER TABLE teacher_profiles ADD COLUMN plain_password VARCHAR(255)",
        "ALTER TABLE teacher_profiles ADD COLUMN payout_passcode VARCHAR(255)",
        "ALTER TABLE teacher_profiles ADD COLUMN letter_accept_version INTEGER DEFAULT 0",
        "ALTER TABLE teacher_profiles ADD COLUMN passcode_reset_pending BOOLEAN DEFAULT FALSE",
        "ALTER TABLE teacher_profiles ADD COLUMN letter_remark TEXT",
        "ALTER TABLE teacher_profiles ADD COLUMN letter_remark_status VARCHAR(20)",
        "ALTER TABLE teacher_profiles ADD COLUMN letter_remark_reply TEXT",
        "ALTER TABLE teacher_profiles ADD COLUMN letter_remark_at DATETIME",
        "ALTER TABLE video_task_chapters ADD COLUMN changed_at DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN deadline_req DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN deadline_req_reason VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN deadline_req_status VARCHAR(20)",
        "ALTER TABLE video_tasks ADD COLUMN quality_rating INTEGER",
        "ALTER TABLE video_tasks ADD COLUMN quality_note VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN remarks_audience VARCHAR(10)",
        "ALTER TABLE production_staff_profiles ADD COLUMN rank1_since DATETIME",
        "ALTER TABLE production_staff_profiles ADD COLUMN rank_appreciated_at DATETIME",
        "ALTER TABLE graphics_tasks ADD COLUMN drive_link VARCHAR(600)",
        "ALTER TABLE graphics_tasks ADD COLUMN deadline DATETIME",
        "ALTER TABLE graphics_tasks ADD COLUMN priority VARCHAR(12)",
        "ALTER TABLE graphics_tasks ADD COLUMN quality_rating INTEGER",
        "ALTER TABLE graphics_tasks ADD COLUMN quality_note VARCHAR(400)",
        "ALTER TABLE video_tasks ADD COLUMN quality_dims TEXT",
        "ALTER TABLE video_tasks ADD COLUMN ontime_appreciated BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN description TEXT",
        "ALTER TABLE video_tasks ADD COLUMN thumbnail_required BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN no_resubmit BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN reference_video TEXT",
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
        "ALTER TABLE exam_questions ADD COLUMN image_b64 LONGTEXT",
        "ALTER TABLE exam_questions MODIFY COLUMN correct_option VARCHAR(255)",
        "ALTER TABLE exams ADD COLUMN medium VARCHAR(20)",
        "ALTER TABLE exam_questions ADD COLUMN question_text_hi TEXT",
        "ALTER TABLE video_task_chapters ADD COLUMN edit_status VARCHAR(20) DEFAULT ''",
        "ALTER TABLE exam_questions ADD COLUMN model_answer_hi TEXT",
        "ALTER TABLE exam_questions ADD COLUMN options_hi JSON",
        "ALTER TABLE exam_questions ADD COLUMN model_answer_image LONGTEXT",
        "ALTER TABLE exam_questions ADD COLUMN explanation TEXT",
        "ALTER TABLE exam_questions ADD COLUMN explanation_hi TEXT",
        "ALTER TABLE materials ADD COLUMN part VARCHAR(200)",
        "ALTER TABLE available_subjects ADD COLUMN mode VARCHAR(12) DEFAULT 'live'",
        "ALTER TABLE notifications ADD COLUMN link VARCHAR(500)",
        "ALTER TABLE video_tasks MODIFY thumbnail_b64 MEDIUMTEXT",  # MySQL only; SQLite pe skip
        "ALTER TABLE doubts MODIFY topic TEXT",  # v: long student doubt topics overflowed VARCHAR(200)
        "ALTER TABLE video_tasks ADD COLUMN video_type VARCHAR(120) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN kind VARCHAR(20) DEFAULT 'normal'",
        "ALTER TABLE video_tasks ADD COLUMN subject VARCHAR(160) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN status_history TEXT",
        "ALTER TABLE video_tasks ADD COLUMN last_link_at DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN admin_seen_at DATETIME",
        "ALTER TABLE video_tasks ADD COLUMN weekly_quota INTEGER DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN weekly_day VARCHAR(12) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN item_source VARCHAR(12) DEFAULT ''",
        "ALTER TABLE student_profiles ADD COLUMN medium VARCHAR(12)",
        "ALTER TABLE doubts ADD COLUMN attach_mime VARCHAR(100)",
        "ALTER TABLE doubts ADD COLUMN attach_name VARCHAR(255)",
        "ALTER TABLE doubts ADD COLUMN audio_b64 LONGTEXT",
        "ALTER TABLE doubts ADD COLUMN answer_audio_b64 LONGTEXT",
        "ALTER TABLE doubts ADD COLUMN answer_attach_b64 LONGTEXT",
        "ALTER TABLE doubts ADD COLUMN answer_attach_mime VARCHAR(100)",
        "ALTER TABLE doubts ADD COLUMN answer_attach_name VARCHAR(255)",
        "ALTER TABLE student_profiles ADD COLUMN source VARCHAR(20) DEFAULT 'mvs_app'",
        "ALTER TABLE student_profiles ADD COLUMN welcome_sent_at DATETIME",
        "ALTER TABLE timetable_entries ADD COLUMN shift_plan TEXT",
        # ===== NIOS Syllabus Tracker =====
        "ALTER TABLE student_profiles ADD COLUMN exam_session VARCHAR(30)",
        "ALTER TABLE student_profiles ADD COLUMN study_target VARCHAR(10)",
        "ALTER TABLE student_profiles ADD COLUMN exam_date VARCHAR(20)",
        "ALTER TABLE student_profiles ADD COLUMN exam_stream VARCHAR(4)",
        # ===== v93: doubt threads + reassignment + notification views =====
        "ALTER TABLE doubts ADD COLUMN assigned_by_teacher_id INTEGER",
        "ALTER TABLE doubts ADD COLUMN assigned_by_name VARCHAR(160)",
        "ALTER TABLE doubts ADD COLUMN assigned_at DATETIME",
        "ALTER TABLE doubts ADD COLUMN assigned_to_admin BOOLEAN DEFAULT 0",
        "ALTER TABLE notifications ADD COLUMN sender_id INTEGER",
        "ALTER TABLE notifications ADD COLUMN sender_role VARCHAR(20)",
        "ALTER TABLE notifications ADD COLUMN batch_key VARCHAR(40)",
        "ALTER TABLE notifications ADD COLUMN batch_label VARCHAR(160)",
        "ALTER TABLE notifications ADD COLUMN read_at DATETIME",
        # v94: restricted sub-admin sections
        "ALTER TABLE users ADD COLUMN allowed_sections JSON",
        # v95: editable monthly target labels + custom targets
        "ALTER TABLE teacher_pay_configs ADD COLUMN target_labels JSON",
        "ALTER TABLE teacher_pay_configs ADD COLUMN custom_targets JSON",
        # v98: studio report — actual timing + class notes upload
        "ALTER TABLE studio_reports ADD COLUMN start_time VARCHAR(20)",
        "ALTER TABLE studio_reports ADD COLUMN end_time VARCHAR(20)",
        "ALTER TABLE studio_reports ADD COLUMN notes_file_b64 LONGTEXT",
        "ALTER TABLE studio_reports ADD COLUMN notes_file_name VARCHAR(255)",
        "ALTER TABLE studio_reports ADD COLUMN notes_file_mime VARCHAR(100)",
        # v101: per-teacher attendance disable (target-only mode)
        "ALTER TABLE teacher_work_policies ADD COLUMN disabled BOOLEAN DEFAULT 0",
        # v111: permanent fix for class-report upload 500s — lectures.class_level was
        # VARCHAR(5), so any non-digit class name ('Class 10' = 8 chars) made MySQL
        # strict mode reject the whole save (error 1406 -> bare 500 -> browser showed
        # "Could not reach the server"). Widened so no class value can ever overflow;
        # the endpoints still normalise to the canonical '10'/'12' before saving.
        "ALTER TABLE lectures MODIFY COLUMN class_level VARCHAR(20)",
        # Payout salary lifecycle (Phase 5): finalized -> in_progress -> credited -> receipt_confirmed
        "ALTER TABLE payout_months ADD COLUMN in_progress_at DATETIME",
        "ALTER TABLE payout_months ADD COLUMN receipt_confirmed_at DATETIME",
        "ALTER TABLE payout_months ADD COLUMN pay_ref VARCHAR(120)",
        # Payout biometric (WebAuthn) fallback
        "ALTER TABLE payout_access ADD COLUMN webauthn_creds TEXT",
        "ALTER TABLE payout_access ADD COLUMN reg_challenge VARCHAR(255)",
        "ALTER TABLE payout_access ADD COLUMN auth_challenge VARCHAR(255)",
        # ---- Production ecosystem (v-prod) ----
        # Extend the users.role ENUM to allow the new production roles (keeps existing values).
        "ALTER TABLE users MODIFY COLUMN role ENUM('admin','teacher','student','production_manager','editor','youtuber','graphics') NOT NULL",
        # Widen the role column that stores the role name as text so 'production_manager' (18 chars) fits.
        "ALTER TABLE user_sessions MODIFY COLUMN role VARCHAR(30)",
        # New VideoTask lifecycle / creator columns (new tables are auto-created by create_all).
        "ALTER TABLE video_tasks ADD COLUMN ref_code VARCHAR(30) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN creator_type VARCHAR(20) DEFAULT 'teacher'",
        "ALTER TABLE video_tasks ADD COLUMN youtuber_id INTEGER NULL",
        "ALTER TABLE video_tasks ADD COLUMN approval_required BOOLEAN NULL",
        "ALTER TABLE video_tasks ADD COLUMN lifecycle VARCHAR(30) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN priority VARCHAR(10) DEFAULT 'normal'",
        "ALTER TABLE video_tasks ADD COLUMN editor_id INTEGER NULL",
        "ALTER TABLE video_tasks ADD COLUMN graphics_id INTEGER NULL",
        "ALTER TABLE video_tasks ADD COLUMN editing_progress INTEGER DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN editing_started_at DATETIME NULL",
        "ALTER TABLE video_tasks ADD COLUMN editing_done_at DATETIME NULL",
        "ALTER TABLE video_tasks ADD COLUMN editing_seconds INTEGER DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN edited_link VARCHAR(600) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN qc_status VARCHAR(20) DEFAULT ''",
        "ALTER TABLE video_tasks ADD COLUMN revision_count INTEGER DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN on_hold BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN cancelled BOOLEAN DEFAULT 0",
        "ALTER TABLE video_tasks ADD COLUMN published_at DATETIME NULL",
    ]
    for s in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(s))
                conn.commit()
        except Exception:
            pass  # column already exists — safe to ignore
ensure_columns()


def ensure_fk_cascade():
    """Make child tables that FK to video_tasks use ON DELETE CASCADE, so deleting a
    video task never fails with MySQL 1451. Fully idempotent and never breaks startup:
    the actual constraint name is looked up from information_schema (auto-generated
    names like `video_task_comments_ibfk_1` differ across environments)."""
    targets = [
        # (child table, fk column)
        ("video_task_comments", "task_id"),
    ]
    for tbl, col in targets:
        try:
            with engine.connect() as conn:
                # find the current FK constraint name for this column -> video_tasks(id)
                row = conn.execute(text(
                    "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                    "AND COLUMN_NAME = :c AND REFERENCED_TABLE_NAME = 'video_tasks' LIMIT 1"
                ), {"t": tbl, "c": col}).fetchone()
                # check whether it is already ON DELETE CASCADE
                rule = conn.execute(text(
                    "SELECT DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = :t LIMIT 1"
                ), {"t": tbl}).fetchone()
                if row and (not rule or (rule[0] or "").upper() != "CASCADE"):
                    cname = row[0]
                    conn.execute(text("ALTER TABLE %s DROP FOREIGN KEY %s" % (tbl, cname)))
                    conn.execute(text(
                        "ALTER TABLE %s ADD CONSTRAINT %s_cascade FOREIGN KEY (%s) "
                        "REFERENCES video_tasks(id) ON DELETE CASCADE" % (tbl, tbl, col)))
                    conn.commit()
        except Exception:
            pass  # if anything is off, the app-level purge still cleans children safely
ensure_fk_cascade()

# ===== PERFORMANCE INDEXES (scale ke liye — 4500 se lakhs students) =====
# Hot filter/join columns pe indexes. Bina index ke MySQL poora table scan karta hai
# (4500 pe theek, par lakhs pe bahut slow). Index lookups table bade hone par bhi fast
# rehte hain — isliye students badhne / bulk upload / zyada live users pe performance
# girti nahi. ensure_columns wale hi safe pattern me: index pehle se ho to MySQL error
# deta hai jo yahan silently ignore ho jaata hai (idempotent).
def ensure_indexes():
    idx = [
        # Students — list filters + counts + portal sync ke hot columns
        "CREATE INDEX ix_sp_class_level ON student_profiles (class_level)",
        "CREATE INDEX ix_sp_source ON student_profiles (source)",
        "CREATE INDEX ix_sp_exam_session ON student_profiles (exam_session)",
        "CREATE INDEX ix_sp_medium ON student_profiles (medium)",
        "CREATE INDEX ix_sp_batch_name ON student_profiles (batch_name)",
        "CREATE INDEX ix_sp_last_seen ON student_profiles (last_seen)",
        "CREATE INDEX ix_sp_nios_ref ON student_profiles (nios_ref)",
        "CREATE INDEX ix_sp_src_cls ON student_profiles (source, class_level)",
        # Doubts — teacher/admin dashboards, status filters, sorting
        "CREATE INDEX ix_doubt_teacher ON doubts (teacher_id)",
        "CREATE INDEX ix_doubt_student ON doubts (student_id)",
        "CREATE INDEX ix_doubt_status ON doubts (status)",
        "CREATE INDEX ix_doubt_subject ON doubts (subject)",
        "CREATE INDEX ix_doubt_created ON doubts (created_at)",
        # Video tasks — Task Manager lists, proposals, per-teacher
        "CREATE INDEX ix_vt_teacher ON video_tasks (teacher_id)",
        "CREATE INDEX ix_vt_status ON video_tasks (status)",
        "CREATE INDEX ix_vt_proposal_ok ON video_tasks (proposal_ok)",
        "CREATE INDEX ix_vt_kind ON video_tasks (kind)",
        "CREATE INDEX ix_vt_created ON video_tasks (created_at)",
        # Lectures / materials — teacher reports + study material lookups
        "CREATE INDEX ix_lecture_created ON lectures (created_at)",
        "CREATE INDEX ix_material_teacher ON materials (teacher_id)",
        "CREATE INDEX ix_material_subject ON materials (subject)",
        "CREATE INDEX ix_material_type ON materials (material_type)",
    ]
    for s in idx:
        try:
            with engine.connect() as conn:
                conn.execute(text(s))
                conn.commit()
        except Exception:
            pass  # index already exists (ya column mismatch) — safe to ignore
ensure_indexes()

# ===== LIGHTWEIGHT DATA MIGRATIONS (one-time content fixes, idempotent) =====
# Doubt SLA policy update: purane contracts ki rules_text me "24 hours + din-wise
# Rs 100/300/600" wali doubt line ko naye "average 15 hours SLA (proportional)" se
# badal do. Sirf us line ko chhuata hai jo doubts ke baare me hai; baaki rules waise
# rehte hain. Idempotent — jis contract me 15 hours pehle se hai wo skip ho jaata hai.
def ensure_data_migrations():
    NEW_DOUBT_LINE = ("Portal ke student doubts average 15 hours ke andar resolve karna zaroori hai; "
                      "is 15 hours SLA se zyada delay par doubt resolution component ke payout me proportional kami hogi.")
    try:
        from models import TeacherContract
        from database import SessionLocal
        db = SessionLocal()
        try:
            rows = (db.query(TeacherContract)
                    .filter(TeacherContract.rules_text.like("%student doubts%"))
                    .all())
            changed = 0
            for c in rows:
                txt = c.rules_text or ""
                lines = txt.splitlines()
                new_lines = []
                touched = False
                for ln in lines:
                    s = ln.strip()
                    if s.lower().startswith("portal ke student doubts") and "15 hours" not in s:
                        new_lines.append(NEW_DOUBT_LINE)
                        touched = True
                    else:
                        new_lines.append(ln)
                if touched:
                    c.rules_text = "\n".join(new_lines)
                    changed += 1
            if changed:
                db.commit()
                try: print("data-migration: doubt SLA updated on", changed, "contract(s)")
                except Exception: pass
        finally:
            db.close()
    except Exception as _e:
        try: print("ensure_data_migrations skipped:", str(_e)[:160])
        except Exception: pass
ensure_data_migrations()

# ===== AUTO R2 MIGRATION — background me khud chale (Start dabane ki zaroorat nahi) =====
def _auto_migrate_loop():
    import time
    from database import SessionLocal
    try:
        import r2_migrate as RM
        import r2_storage as R2S
        if not R2S.is_configured():
            return
    except Exception:
        return
    kinds = ['photos_student', 'photos_teacher', 'materials', 'notes', 'lecture_pdf',
             'lecture_dpp', 'thumbnails', 'doubt_img', 'doubt_audio', 'doubt_ans_audio',
             'doubt_ans_file', 'dpp_answers',
             'exam_q_img', 'exam_q_ans_img', 'exam_q_alt_img', 'dpp_q_pdf', 'dpp_s_pdf',
             'exam_ans_img', 'lecture_q_img']
    time.sleep(25)  # app ko boot hone do
    while True:
        migrated_any = False
        try:
            for kind in kinds:
                after_id = 0
                for _ in range(3000):  # safety cap per kind
                    db = SessionLocal()
                    try:
                        # bigger batch (20) so base64 DB se jaldi khali ho -> MySQL RAM/cost gire
                        res = RM.migrate_batch(db, kind, after_id, 20)
                        db.commit()
                    except Exception:
                        try: db.rollback()
                        except Exception: pass
                        res = None
                    finally:
                        db.close()
                    if not res or res.get("error"):
                        break
                    if res.get("has_more"):
                        migrated_any = True
                        after_id = res.get("last_id", after_id)
                        time.sleep(0.6)   # gentle par tez — DB/R2 par load na aaye
                    else:
                        break
        except Exception:
            pass
        # Jab tak base64 bacha hai (migrated_any) tez chalte raho (30s). Sab R2 par aa gaya
        # to 12 min so jao — naya file aate hi agle cycle me migrate ho jaayega.
        time.sleep(30 if migrated_any else 720)

def _start_auto_migrate():
    import threading
    try:
        threading.Thread(target=_auto_migrate_loop, daemon=True).start()
    except Exception:
        pass

_start_auto_migrate()

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

# ===== CATEGORY WORKSPACE BACKFILL (additive, idempotent) =====
# Creates NIOS + DU SOL categories, default feature flags, assigns every existing
# teacher to NIOS and mirrors their subjects. Safe to run on every boot.
try:
    support_models.seed_complaint_categories()
except Exception:
    pass
try:
    category_models.backfill_categories()
except Exception as _cat_err:
    try:
        print("category backfill skipped:", str(_cat_err)[:200])
    except Exception:
        pass

# ===== APP =====
app = FastAPI(
    title="MVS Foundation CRM API",
    description="Teacher · Student · Admin Portal Backend",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ===== GLOBAL ERROR SAFETY NET =====
# Unhandled exceptions must still return a JSON response from INSIDE the CORS
# middleware. Without this, a crashed request answers with a bare proxy/uvicorn
# 500 page (no Access-Control-Allow-Origin header), the browser blocks it, and
# the portal shows a misleading "Could not reach the server" network error even
# though the server is up. This handler converts every crash into a readable
# JSON error so the real message always reaches the user.
from fastapi.responses import JSONResponse
import logging as _logging

# Per-request access logs (har ping/200 OK) band -> Railway ka 500 logs/sec flood + CPU kam.
_logging.getLogger("uvicorn.access").setLevel(_logging.WARNING)
_mvs_log = _logging.getLogger("mvs")

try:
    from production_core import TransitionError as _TransitionError
    @app.exception_handler(_TransitionError)
    async def _transition_error_handler(request, exc):
        # Illegal production lifecycle move — controlled state machine rejected it.
        return JSONResponse(status_code=400, content={"detail": str(exc) or "That action is not allowed for this task right now."})
except Exception:
    pass

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    # EK concise line — poora 30-line traceback NAHI (warna pool-timeout pe log flood ho jaata hai).
    try:
        _mvs_log.error("ReqError %s %s -> %s: %s", request.method,
                       request.url.path, type(exc).__name__, str(exc)[:200])
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on the server — please try again in a moment. If it keeps happening, contact support."},
    )

# ===== DB POOL HEALTH: pre-ping on checkout (database.py ke bina bhi kaam karta hai) =====
# Railway MySQL idle connections drop kar deta hai. Bina pre-ping, pool DEAD
# connections serve karta hai -> query hang -> "QueuePool limit reached, timeout"
# -> login/ping tak fail. Har checkout par SELECT 1: dead connection discard karke
# fresh deta hai -> pool hamesha healthy. (= pool_pre_ping=True ka effect.)
try:
    from sqlalchemy import event as _sa_event, exc as _sa_exc
    from database import engine as _db_engine

    import time as _pool_time

    @_sa_event.listens_for(_db_engine, "checkin")
    def _mvs_pool_checkin(dbapi_conn, conn_record):
        try:
            conn_record.info["mvs_returned_at"] = _pool_time.time()
        except Exception:
            pass

    @_sa_event.listens_for(_db_engine, "checkout")
    def _mvs_pool_checkout_ping(dbapi_conn, conn_record, conn_proxy):
        # v167: SELECT 1 SIRF tab jab connection 30s+ idle raha ho (MySQL ne tab drop
        # kiya ho sakta hai). Busy load me connections turant reuse hote hain -> ping SKIP
        # -> har request par extra query NAHI -> DB par kaafi kam load, pool jaldi free.
        try:
            _ret = conn_record.info.get("mvs_returned_at", 0)
            if (_pool_time.time() - _ret) < 30:
                return
        except Exception:
            pass
        try:
            cur = dbapi_conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except Exception:
            # dead connection -> pool ko batao discard karke naya banaye
            raise _sa_exc.DisconnectionError("stale DB connection - reconnecting")
    _mvs_log.warning("DB pool pre-ping (idle-only) enabled")

    # v166/v167: har naye connection par query-time + socket cap. Pool/DB pressure me
    # koi SELECT 10s se zyada nahi chalti aur dead socket 15s me error deta hai -> connection
    # turant pool me wapas -> baaki requests chalti rehti hain (death spiral tuit jaata hai).
    @_sa_event.listens_for(_db_engine, "connect")
    def _mvs_conn_query_timeout(dbapi_conn, conn_record):
        try:
            conn_record.info["mvs_returned_at"] = _pool_time.time()
        except Exception:
            pass
        try:
            _sock = getattr(dbapi_conn, "_sock", None)
            if _sock is not None:
                _sock.settimeout(15)
        except Exception:
            pass
        try:
            cur = dbapi_conn.cursor()
            try:
                cur.execute("SET SESSION max_execution_time = 10000")   # 10s cap (MySQL, ms)
            except Exception:
                pass
            cur.close()
        except Exception:
            pass
except Exception as _e:
    try:
        _mvs_log.error("pool pre-ping setup failed: %s", _e)
    except Exception:
        pass

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

# ===== GZIP: responses compress karo (HTML/JSON ~70-80% chhoti -> egress bill + speed) =====
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)

# ===== ROUTERS =====
app.include_router(auth_routes.router)
app.include_router(teacher_routes.router)
app.include_router(admin_routes.router)
app.include_router(student_routes.router)
app.include_router(ext_materials.router)
app.include_router(syllabus_routes.router)
app.include_router(video_tasks.router)
app.include_router(production_routes.router)
app.include_router(editor_routes.router)
app.include_router(youtuber_routes.router)
app.include_router(graphics_routes.router)
app.include_router(translation_routes.router)
app.include_router(category_routes.router)
app.include_router(support_routes.router)

# ===== ROOT =====
# ===== ROOT: serve the portal =====
# If mvs_portal_connected.html sits next to this file in the repo, the portal
# opens directly at the Railway URL. If the file is missing, the old JSON
# status reply is returned so nothing ever breaks.
from fastapi.responses import FileResponse

_PORTAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mvs_portal_connected.html")

# Portal ka bada JS ab alag file me (browser ise CACHE karta hai -> refresh par
# 304, re-download nahi -> egress bahut kam + fast). FileResponse ETag/Last-Modified
# khud set karta hai, isliye update karne par browser naya le lega.
_APP_JS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mvs_app.js")

@app.get("/mvs_app.js")
def _serve_app_js():
    if os.path.exists(_APP_JS_FILE):
        # no-cache + revalidate: har deploy ke baad browser turant nayi JS le (purani cached
        # version ki wajah se fixes miss na hon). ETag se 304 milega agar file unchanged.
        return FileResponse(_APP_JS_FILE, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache, must-revalidate"})
    return JSONResponse(status_code=404, content={"detail": "mvs_app.js not found"})


# ===== Notification image serve (admin-attached image) — koi bhi logged-in user (jise
# notif mila) load kar sake, isliye admin router ke bahar app-level route. =====
@app.get("/api/notif-image")
def _serve_notif_image(k: str = ""):
    from fastapi import HTTPException as _HX
    if not k:
        raise _HX(404, "Not found")
    import urllib.parse as _up
    ref = _up.unquote(k)
    return __import__("r2_storage").file_response(ref, "image/jpeg", "notif.jpg", False)


# ===== R2 TEST (temporary — verify karne ke baad hata denge) =====
@app.get("/r2-test")
def _r2_test():
    try:
        import r2_storage as R2
        if not R2.is_configured():
            return {"ok": False, "detail": "R2 env vars set nahi hain (R2_ACCOUNT_ID/ACCESS_KEY/SECRET/BUCKET)."}
        url = R2.upload_bytes("test/hello.txt", b"MVS Class Manager -> R2 working!", "text/plain")
        return {"ok": True, "url": url,
                "message": "R2 upload OK. Upar wali 'url' browser me kholo -> text dikhna chahiye."}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:400]}


# ===== R2 BULK MIGRATION (one-time — purane base64 -> R2) =====
def _r2_mig_secret():
    return (os.getenv("R2_ACCOUNT_ID") or "").strip()


@app.get("/r2-optimize")
def _r2_optimize(key: str = ""):
    """Migration ke baad MySQL me jo freed space (purana base64) bacha hai use reclaim karta hai
    (OPTIMIZE TABLE). Kam traffic me chalao. Browser: /r2-optimize?key=<R2_ACCOUNT_ID>"""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key — R2 Account ID daalo"})
    from database import SessionLocal
    from sqlalchemy import text as _t
    tables = ["student_profiles", "teacher_profiles", "materials", "video_tasks",
              "dpp_packs", "exam_questions", "doubts", "dpp_answers", "exam_attempts",
              "class_reports", "lectures"]
    done = {}
    db = SessionLocal()
    try:
        for tb in tables:
            try:
                db.execute(_t("OPTIMIZE TABLE %s" % tb))
                done[tb] = "ok"
            except Exception as e:
                done[tb] = "skip (%s)" % str(e)[:60]
        try: db.commit()
        except Exception: pass
    finally:
        db.close()
    return {"optimized": done, "note": "DB space reclaimed. Queries + RAM ab lighter."}


@app.get("/r2-diag")
def _r2_diag(key: str = "", url: str = "", attempt: int = 0):
    """Ek R2 file ki asli bytes check karo — valid image/pdf hai, corrupt hai, HTML error
    page hai, ya 404. Browser me kholo: /r2-diag?key=<R2_ACCOUNT_ID>&url=<r2 url>"""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key — R2 Account ID daalo"})
    import urllib.request, urllib.error
    val = url or ""
    if attempt and not val:
        from database import SessionLocal
        from models import ExamAttempt
        db = SessionLocal()
        try:
            a = db.query(ExamAttempt).filter(ExamAttempt.id == int(attempt)).first()
            val = (getattr(a, "answer_image_b64", "") if a else "") or ""
        finally:
            db.close()
    out = {"stored_is_url": isinstance(val, str) and val.startswith("http"),
           "value_len": len(val or ""), "value_head": (val or "")[:80]}
    if isinstance(val, str) and val.startswith("http"):
        out["url"] = val
        try:
            req = urllib.request.Request(val, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36"})
            with urllib.request.urlopen(req, timeout=20) as r:
                head = r.read(512)
                out["http_status"] = getattr(r, "status", None)
                out["content_type"] = r.headers.get("Content-Type")
                out["content_length_header"] = r.headers.get("Content-Length")
            out["first_bytes_hex"] = head[:16].hex()
            if head[:3] == b"\xff\xd8\xff":
                out["verdict"] = "VALID JPEG — file theek hai (dikkat serve/CORS me thi)"
            elif head[:8].startswith(b"\x89PNG"):
                out["verdict"] = "VALID PNG — file theek hai"
            elif head[:4] == b"%PDF":
                out["verdict"] = "VALID PDF — file theek hai"
            elif head.lstrip()[:1] == b"<":
                out["verdict"] = "HTML PAGE mila (Cloudflare challenge/error) — file block ho rahi ya nahi mili"
            else:
                out["verdict"] = "CORRUPT/UNKNOWN — valid image/pdf ke magic bytes nahi (migration me kharab hui)"
        except urllib.error.HTTPError as e:
            out["http_status"] = e.code
            out["verdict"] = "HTTP %s — file R2 par nahi mili / block (404 = object hi nahi hai)" % e.code
        except Exception as e:
            out["verdict"] = "FETCH FAILED"
            out["error"] = str(e)[:200]
    else:
        out["verdict"] = "abhi bhi base64 (R2 par nahi gaya)" if val else "khaali/None"
    return out


@app.get("/r2-migrate-batch")
def _r2_migrate_batch(key: str = "", kind: str = "", after_id: int = 0, limit: int = 10):
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key"})
    from database import SessionLocal
    import r2_migrate
    db = SessionLocal()
    try:
        # materials bade hote hain -> chhota batch (RAM safe)
        lim = min(int(limit or 10), 5 if kind == "materials" else 25)
        return r2_migrate.migrate_batch(db, kind, int(after_id or 0), lim)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


@app.get("/r2-rewrite")
def _r2_rewrite(key: str = "", old: str = "", new: str = ""):
    """Stored R2 URLs ka host badlo (r2.dev -> cdn.mvsfoundation.in). Sirf string
    swap, koi file dobara upload nahi. Fast bulk UPDATE."""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key"})
    old = (old or "").strip().rstrip("/")
    new = (new or "").strip().rstrip("/")
    if not old or not new or not old.startswith("http") or not new.startswith("http"):
        return JSONResponse(status_code=400, content={"error": "old aur new dono full https URL do"})
    from database import SessionLocal
    from sqlalchemy import func, update
    db = SessionLocal()
    try:
        from models import (StudentProfile, TeacherProfile, Material, StudioReport,
                            Lecture, VideoTask, Doubt, DppAnswer)
        targets = [
            (StudentProfile, "photo_b64"), (TeacherProfile, "photo_b64"),
            (Material, "content_b64"), (StudioReport, "notes_file_b64"),
            (Lecture, "pdf_b64"), (Lecture, "dpp_b64"), (VideoTask, "thumbnail_b64"),
            (Doubt, "image_b64"), (Doubt, "audio_b64"),
            (Doubt, "answer_audio_b64"), (Doubt, "answer_attach_b64"),
            (DppAnswer, "answer_b64"),
        ]
        try:
            from models import ExamQuestion, DppPack, ExamAttempt, LectureQuestion
            targets += [(ExamQuestion, "image_b64"), (ExamQuestion, "model_answer_image"),
                        (ExamQuestion, "alt_image_b64"), (DppPack, "q_pdf"), (DppPack, "s_pdf"),
                        (ExamAttempt, "answer_image_b64"), (LectureQuestion, "image_b64")]
        except Exception:
            pass
        out = {}
        total = 0
        for Model, field in targets:
            col = getattr(Model, field)
            try:
                r = db.execute(update(Model).where(col.like(old + "%"))
                               .values({field: func.replace(col, old, new)}))
                out["%s.%s" % (Model.__name__, field)] = r.rowcount
                total += r.rowcount or 0
            except Exception as e:
                out["%s.%s" % (Model.__name__, field)] = "err: " + str(e)[:80]
        db.commit()
        return {"ok": True, "total_rewritten": total, "detail": out, "old": old, "new": new}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


@app.get("/r2-normalize")
def _r2_normalize(key: str = ""):
    """Har stored R2 URL ka host -> mvsdatabase.com (R2_PUBLIC_URL) bana do, path same
    rakhte hue. r2.dev / purane cdn / kisi bhi host waale URLs sab ek hi custom domain
    par aa jaayenge. Self-driving: ek hi call me saare fields. Jo pehle se
    mvsdatabase.com par hain unhe chhodta hai (idempotent). URL chhoti string hai isliye
    base64 blobs load nahi hote."""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key"})
    import r2_storage as R2
    pub = ""
    try:
        pub = (R2._cfg().get("public_url") or "").rstrip("/")
    except Exception:
        pub = ""
    if not pub or not pub.startswith("http"):
        return JSONResponse(status_code=400, content={"error": "R2_PUBLIC_URL set nahi hai"})
    from database import SessionLocal
    db = SessionLocal()
    try:
        from models import (StudentProfile, TeacherProfile, Material, StudioReport,
                            Lecture, VideoTask, Doubt, DppAnswer)
        targets = [
            (StudentProfile, "photo_b64"), (TeacherProfile, "photo_b64"),
            (Material, "content_b64"), (StudioReport, "notes_file_b64"),
            (Lecture, "pdf_b64"), (Lecture, "dpp_b64"), (VideoTask, "thumbnail_b64"),
            (Doubt, "image_b64"), (Doubt, "audio_b64"),
            (Doubt, "answer_audio_b64"), (Doubt, "answer_attach_b64"),
            (DppAnswer, "answer_b64"),
        ]
        try:
            from models import ExamQuestion, DppPack, ExamAttempt, LectureQuestion
            targets += [(ExamQuestion, "image_b64"), (ExamQuestion, "model_answer_image"),
                        (ExamQuestion, "alt_image_b64"), (DppPack, "q_pdf"), (DppPack, "s_pdf"),
                        (ExamAttempt, "answer_image_b64"), (LectureQuestion, "image_b64")]
        except Exception:
            pass
        out = {}
        total = 0
        for Model, field in targets:
            col = getattr(Model, field)
            changed = 0
            try:
                # sirf http URLs jo pehle se pub par NAHI hain — id + url load karo (chhoti)
                rows = db.query(Model.id, col).filter(
                    col.like("http%"), ~col.like(pub + "/%")).all()
                for rid, val in rows:
                    try:
                        parts = str(val).split("/", 3)
                        if len(parts) < 4 or not parts[3]:
                            continue
                        new_url = pub + "/" + parts[3]
                        db.query(Model).filter(Model.id == rid).update(
                            {field: new_url}, synchronize_session=False)
                        changed += 1
                    except Exception:
                        pass
                out["%s.%s" % (Model.__name__, field)] = changed
                total += changed
            except Exception as e:
                out["%s.%s" % (Model.__name__, field)] = "err: " + str(e)[:60]
        db.commit()
        return {"ok": True, "total_normalized": total, "target_host": pub, "detail": out}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


@app.get("/r2-cleanup-chunks")
def _r2_cleanup_chunks(key: str = "", hours: int = 12):
    """Adhoore/abandoned uploads ke orphan DPP chunks delete karo. Real upload minutes
    me assemble ho jaata hai (aur tabhi chunks delete ho jaate hain) — jo 'hours' se
    purane pade hain woh abandoned hain, dead weight. Ye base64 chunk data DB se hata
    ke size + RAM kam karega. Safe: sirf purane chunks, in-flight uploads ko haath nahi."""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key"})
    from database import SessionLocal
    from datetime import datetime, timedelta
    from sqlalchemy import func as _f
    db = SessionLocal()
    try:
        from models import DppChunk
        try:
            hrs = max(1, int(hours or 12))
        except Exception:
            hrs = 12
        cutoff = datetime.utcnow() - timedelta(hours=hrs)
        before = db.query(_f.count(DppChunk.id)).scalar() or 0
        deleted = db.query(DppChunk).filter(DppChunk.created_at < cutoff).delete(synchronize_session=False)
        db.commit()
        after = db.query(_f.count(DppChunk.id)).scalar() or 0
        return {"ok": True, "deleted_chunks": int(deleted or 0),
                "older_than_hours": hrs, "chunks_before": int(before),
                "chunks_left": int(after)}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


@app.get("/r2-count")
def _r2_count(key: str = ""):
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key — R2 Account ID daalo"})
    from database import SessionLocal
    from sqlalchemy import func, case
    db = SessionLocal()
    try:
        from models import StudentProfile, TeacherProfile, Material

        def ph(Model, col):
            b = db.query(func.count()).select_from(Model).filter(col.isnot(None), ~col.like("http%")).scalar() or 0
            r = db.query(func.count()).select_from(Model).filter(col.like("http%")).scalar() or 0
            return {"in_db_base64": int(b), "on_r2": int(r), "total": int(b) + int(r)}

        rows = db.query(
            Material.material_type,
            func.count().label("total"),
            func.sum(case((Material.content_b64.like("http%"), 1), else_=0)).label("on_r2"),
            func.sum(case((Material.content_b64.isnot(None) & ~Material.content_b64.like("http%"), 1), else_=0)).label("b64"),
        ).group_by(Material.material_type).all()
        LABEL = {"notes": "Class Notes", "dpp": "DPP", "test": "Tests",
                 "answer": "Student Answers", "other": "Question Bank / Study Material"}
        materials = []
        for mt, total, on_r2, b64 in rows:
            materials.append({"type": mt or "unknown", "label": LABEL.get(mt, mt or "unknown"),
                              "total": int(total or 0), "in_db_base64": int(b64 or 0), "on_r2": int(on_r2 or 0)})
        return {
            "student_photos": ph(StudentProfile, StudentProfile.photo_b64),
            "teacher_photos": ph(TeacherProfile, TeacherProfile.photo_b64),
            "materials_by_type": materials,
            "other_tables": _r2_count_all(db),
            "note": "in_db_base64 = abhi DB me (migrate hona baaki). on_r2 = R2 par ho gaye. Migration ke baad in_db_base64=0 aur on_r2 me sab. SAB in_db_base64=0 hone par hi OPTIMIZE TABLE chalao.",
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


def _r2_count_all(db):
    """Har base64 column ka in_db_base64 vs on_r2 count — poori picture (OPTIMIZE se pehle
    confirm karne ke liye ki har table clear ho gaya)."""
    from sqlalchemy import func
    out = {}
    try:
        import models as M
        # (table_label, Model attr name, column attr name)
        targets = [
            ("lecture_pdf", "Lecture", "pdf_b64"),
            ("lecture_dpp", "Lecture", "dpp_b64"),
            ("dpp_answers", "DppAnswer", "answer_b64"),
            ("exam_attempt_answers", "ExamAttempt", "answer_image_b64"),
            ("exam_question_img", "ExamQuestion", "image_b64"),
            ("exam_question_alt_img", "ExamQuestion", "alt_image_b64"),
            ("lecture_question_img", "LectureQuestion", "image_b64"),
            ("doubt_img", "Doubt", "image_b64"),
            ("doubt_audio", "Doubt", "audio_b64"),
            ("doubt_answer_audio", "Doubt", "answer_audio_b64"),
            ("doubt_answer_attach", "Doubt", "answer_attach_b64"),
            ("video_task_thumbnail", "VideoTask", "thumbnail_b64"),
            ("studio_report_notes", "StudioReport", "notes_file_b64"),
            ("production_staff_photos", "ProductionStaffProfile", "photo_b64"),
            ("youtuber_photos", "YouTuberProfile", "photo_b64"),
        ]
        for label, model_name, col_name in targets:
            try:
                Model = getattr(M, model_name, None)
                if Model is None:
                    continue
                col = getattr(Model, col_name, None)
                if col is None:
                    continue
                b = db.query(func.count()).select_from(Model).filter(col.isnot(None), col != "", ~col.like("http%")).scalar() or 0
                r = db.query(func.count()).select_from(Model).filter(col.like("http%")).scalar() or 0
                out[label] = {"in_db_base64": int(b), "on_r2": int(r), "total": int(b) + int(r)}
            except Exception:
                pass
    except Exception:
        pass
    return out


@app.get("/r2-status")
def _r2_status(key: str = ""):
    """Verification: har blob field ke liye ginti — kitne abhi bhi base64 (DB me), kitne
    mvsdatabase.com par, kitne kisi aur host par (r2.dev/purana cdn). base64=0 aur
    other_host=0 -> sab migrate ho gaya aur sab custom domain par hai."""
    if not _r2_mig_secret() or key != _r2_mig_secret():
        return JSONResponse(status_code=403, content={"error": "bad key — R2 Account ID daalo"})
    import r2_storage as R2
    try:
        pub = (R2._cfg().get("public_url") or "").rstrip("/")
    except Exception:
        pub = ""
    from database import SessionLocal
    from sqlalchemy import func
    db = SessionLocal()
    try:
        from models import (StudentProfile, TeacherProfile, Material, StudioReport,
                            Lecture, VideoTask, Doubt, DppAnswer)
        fields = [
            (StudentProfile, "photo_b64", "Student photos"),
            (TeacherProfile, "photo_b64", "Teacher photos"),
            (Material, "content_b64", "Study material / notes / QB"),
            (StudioReport, "notes_file_b64", "Class notes"),
            (Lecture, "pdf_b64", "Lecture PDF"), (Lecture, "dpp_b64", "Lecture DPP"),
            (VideoTask, "thumbnail_b64", "Video thumbnails"),
            (Doubt, "image_b64", "Doubt images"), (Doubt, "audio_b64", "Doubt audio"),
            (Doubt, "answer_audio_b64", "Doubt answer audio"),
            (Doubt, "answer_attach_b64", "Doubt answer files"),
            (DppAnswer, "answer_b64", "DPP answers"),
        ]
        try:
            from models import ExamQuestion, DppPack, ExamAttempt, LectureQuestion
            fields += [
                (ExamQuestion, "image_b64", "Exam question figures"),
                (ExamQuestion, "model_answer_image", "Exam answer figures"),
                (ExamQuestion, "alt_image_b64", "Exam OR-alt figures"),
                (DppPack, "q_pdf", "DPP question PDFs"), (DppPack, "s_pdf", "DPP solution PDFs"),
                (ExamAttempt, "answer_image_b64", "Exam answer sheets"),
                (LectureQuestion, "image_b64", "Lecture quiz images"),
            ]
        except Exception:
            pass
        detail = []
        t_b64 = t_ok = t_other = 0
        for Model, field, label in fields:
            col = getattr(Model, field)
            try:
                b64 = db.query(func.count()).select_from(Model).filter(
                    col.isnot(None), ~col.like("http%")).scalar() or 0
                http = db.query(func.count()).select_from(Model).filter(
                    col.like("http%")).scalar() or 0
                ok = 0
                if pub:
                    ok = db.query(func.count()).select_from(Model).filter(
                        col.like(pub + "/%")).scalar() or 0
                other = int(http) - int(ok)
                if (b64 or http):
                    detail.append({"item": label, "base64_left": int(b64),
                                   "on_mvsdatabase": int(ok), "other_host": int(other)})
                t_b64 += int(b64); t_ok += int(ok); t_other += int(other)
            except Exception as e:
                detail.append({"item": label, "error": str(e)[:60]})
        return {"ok": True, "target_host": pub,
                "totals": {"base64_left": t_b64, "on_mvsdatabase": t_ok, "other_host": t_other},
                "all_good": (t_b64 == 0 and t_other == 0),
                "detail": detail,
                "note": "base64_left=0 aur other_host=0 -> sab migrate + sab mvsdatabase.com par."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)[:300]})
    finally:
        db.close()


@app.get("/r2-migrate")
def _r2_migrate_page():
    html = """<!doctype html><html><head><meta charset=utf-8><title>R2 Migration</title>
<style>body{font-family:system-ui;background:#0b0b0b;color:#ddd;padding:20px;max-width:820px;margin:auto}
h2{color:#e8b84b}.bar{height:14px;background:#222;border-radius:8px;overflow:hidden;margin:8px 0}
.fill{height:100%;background:linear-gradient(90deg,#b8941f,#e8b84b);width:0}
pre{background:#111;padding:12px;border-radius:8px;max-height:60vh;overflow:auto;font-size:12px;white-space:pre-wrap}
input{padding:8px;border-radius:6px;border:1px solid #333;background:#161616;color:#ddd;width:340px}
button{padding:9px 16px;border-radius:8px;border:none;background:#b8941f;color:#111;font-weight:800;cursor:pointer}</style></head>
<body><h2>R2 Bulk Migration (purane files -> R2)</h2>
<p>Apna <b>R2 Account ID</b> daal ke Start dabao. Page khula rehne do — apne aap
sab shift karega. Beech me ruk jaye to dobara Start (jaha se chhoda wahi se aage).</p>
<input id=k placeholder="R2 Account ID (secret)"> <button onclick="startMig()">Start</button> <button onclick="fixUrls()" style="background:#2b6cb0;color:#fff">Fix URLs &rarr; mvsdatabase.com</button> <button onclick="cleanChunks()" style="background:#8a5a1f;color:#fff">Clean orphan chunks</button> <button onclick="checkStatus()" style="background:#2f855a;color:#fff">Check status</button>
<div class=bar><div class=fill id=f></div></div>
<pre id=log></pre>
<script>
const kinds=[['photos_student',20],['photos_teacher',20],['materials',5],
             ['notes',5],['lecture_pdf',5],['lecture_dpp',5],['thumbnails',15],
             ['doubt_img',15],['doubt_audio',10],['doubt_ans_audio',10],
             ['doubt_ans_file',10],['dpp_answers',5],
             ['exam_q_img',10],['exam_q_ans_img',10],['exam_q_alt_img',10],
             ['dpp_q_pdf',5],['dpp_s_pdf',5],
             ['exam_ans_img',10],['lecture_q_img',10]];
let ki=0,afterId=0,totalMig=0,running=false;
const log=m=>{const l=document.getElementById('log');l.textContent+=m+"\\n";l.scrollTop=l.scrollHeight;};
function startMig(){ if(running)return; running=true; ki=0; afterId=0; totalMig=0;
  document.getElementById('log').textContent=''; log('Starting...'); run(); }
async function fixUrls(){
  const key=document.getElementById('k').value.trim();
  if(!key){ log('Pehle R2 Account ID daalo, phir Fix URLs dabao.'); return; }
  log('\\nFixing all URLs -> mvsdatabase.com ...');
  try{
    const r=await fetch(`/r2-normalize?key=${encodeURIComponent(key)}`);
    const d=await r.json();
    if(d.error){ log('ERROR: '+d.error+' (key sahi hai?)'); return; }
    log('\\u2705 Done. Total URLs fixed: '+(d.total_normalized||0)+' -> '+(d.target_host||''));
    for(const k in (d.detail||{})){ if(d.detail[k]) log('  '+k+': '+d.detail[k]); }
  }catch(e){ log('ERROR: '+e); }
}
async function cleanChunks(){
  const key=document.getElementById('k').value.trim();
  if(!key){ log('Pehle R2 Account ID daalo, phir Clean dabao.'); return; }
  log('\\nCleaning orphan DPP chunks (12h+ purane)...');
  try{
    const r=await fetch(`/r2-cleanup-chunks?key=${encodeURIComponent(key)}&hours=12`);
    const d=await r.json();
    if(d.error){ log('ERROR: '+d.error+' (key sahi hai?)'); return; }
    log('\\u2705 Deleted '+(d.deleted_chunks||0)+' orphan chunks. Left: '+(d.chunks_left||0));
  }catch(e){ log('ERROR: '+e); }
}
async function checkStatus(){
  const key=document.getElementById('k').value.trim();
  if(!key){ log('Pehle R2 Account ID daalo, phir Check dabao.'); return; }
  log('\\nChecking status...');
  try{
    const r=await fetch(`/r2-status?key=${encodeURIComponent(key)}`);
    const d=await r.json();
    if(d.error){ log('ERROR: '+d.error+' (key sahi hai?)'); return; }
    const t=d.totals||{};
    log('Target host: '+(d.target_host||'?'));
    (d.detail||[]).forEach(x=>{
      if(x.error){ log('  '+x.item+': err '+x.error); return; }
      const flag=(x.base64_left===0&&x.other_host===0)?'\\u2705':'\\u26a0\\ufe0f';
      log('  '+flag+' '+x.item+': base64='+x.base64_left+', mvsdatabase='+x.on_mvsdatabase+', other='+x.other_host);
    });
    log('\\nTOTAL -> base64 baaki: '+(t.base64_left||0)+' | mvsdatabase.com: '+(t.on_mvsdatabase||0)+' | doosre host: '+(t.other_host||0));
    log(d.all_good ? '\\u2705 SAB THIK: sab migrate ho gaya aur sab mvsdatabase.com par hai.'
                   : '\\u26a0\\ufe0f Abhi kuch baaki: base64 waale ke liye Start, doosre-host waale ke liye Fix URLs dabao.');
  }catch(e){ log('ERROR: '+e); }
}
async function run(){
  if(ki>=kinds.length){ log('\\n\u2705 ALL DONE. Total migrated: '+totalMig); document.getElementById('f').style.width='100%'; running=false; return; }
  const key=document.getElementById('k').value.trim();
  const [kind,limit]=kinds[ki];
  try{
    const r=await fetch(`/r2-migrate-batch?key=${encodeURIComponent(key)}&kind=${kind}&after_id=${afterId}&limit=${limit}`);
    const d=await r.json();
    if(d.error){ log('ERROR: '+d.error+' (key sahi hai?)'); running=false; return; }
    totalMig+=d.migrated||0;
    log(`${kind}: +${d.migrated} moved, ${d.skipped} skip  (id>${afterId})${d.total!=null?'  total~'+d.total:''}`);
    if(d.has_more){ afterId=d.last_id; }
    else { log('--- '+kind+' DONE ---'); ki++; afterId=0; }
    setTimeout(run, 250);
  }catch(e){ log('network error, retry 3s...'); setTimeout(run,3000); }
}
</script></body></html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)

@app.get("/")
def root():
    if os.path.exists(_PORTAL_FILE):
        return FileResponse(_PORTAL_FILE, media_type="text/html")
    return {
        "app": "MVS Foundation CRM",
        "version": "1.0.0",
        "status": "running ✅",
        "docs": "/docs",
        "portals": ["teacher", "admin", "student"]
    }

@app.get("/portal")
def portal():
    if os.path.exists(_PORTAL_FILE):
        return FileResponse(_PORTAL_FILE, media_type="text/html")
    return {"error": "portal file not deployed"}

# Path-based portal entry points — Teacher aur Admin ke liye alag URL. Dono same SPA serve
# karte hain; frontend path padh ke sahi portal khol deta hai. Student ka URL sirf root
# (app.mvsfoundation.in) hi rehta hai — uska flow bilkul waisa ka waisa (approved template).
@app.get("/teacher")
@app.get("/admin")
@app.get("/production")
@app.get("/editor")
@app.get("/youtuber")
@app.get("/graphics")
def portal_entry():
    if os.path.exists(_PORTAL_FILE):
        return FileResponse(_PORTAL_FILE, media_type="text/html")
    return {"error": "portal file not deployed"}

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/syllabus")
def health_syllabus():
    """Which subjects are verified and usable by the tracker, and why not."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        out = {}
        for cl in ("10", "12"):
            items = []
            for s in syllabus_routes.subject_list(db, cl, include_hidden=True):
                items.append({
                    "code": s["code"], "name": s["name"],
                    "status": s.get("status", "pending"),
                    "issues": s.get("issues", []),
                })
            out[cl] = {
                "ready": [i["code"] for i in items if i["status"] == "ready"],
                "needs_review": [{"code": i["code"], "name": i["name"], "issues": i["issues"]}
                                 for i in items if i["status"] == "needs_review"],
                "pending": len([i for i in items if i["status"] == "pending"]),
                "total": len(items),
            }
        return {"status": "ok", "syllabus": out}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:300]}
    finally:
        db.close()
