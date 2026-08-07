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
import ext_materials
import syllabus_routes
import video_tasks

load_dotenv()

# ===== CREATE TABLES =====
Base.metadata.create_all(bind=engine)

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
    ]
    for s in stmts:
        try:
            with engine.connect() as conn:
                conn.execute(text(s))
                conn.commit()
        except Exception:
            pass  # column already exists — safe to ignore
ensure_columns()

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

    @_sa_event.listens_for(_db_engine, "checkout")
    def _mvs_pool_checkout_ping(dbapi_conn, conn_record, conn_proxy):
        try:
            cur = dbapi_conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        except Exception:
            # dead connection -> pool ko batao discard karke naya banaye
            raise _sa_exc.DisconnectionError("stale DB connection - reconnecting")
    _mvs_log.warning("DB pool pre-ping (checkout) enabled")
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
        return FileResponse(_APP_JS_FILE, media_type="application/javascript")
    return JSONResponse(status_code=404, content={"detail": "mvs_app.js not found"})


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
            "note": "in_db_base64 = abhi DB me (migrate hona baaki). on_r2 = R2 par ho gaye. Migration ke baad in_db_base64=0 aur on_r2 me sab.",
        }
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
<input id=k placeholder="R2 Account ID (secret)"> <button onclick="startMig()">Start</button>
<div class=bar><div class=fill id=f></div></div>
<pre id=log></pre>
<script>
const kinds=[['photos_student',20],['photos_teacher',20],['materials',5],
             ['notes',5],['lecture_pdf',5],['lecture_dpp',5],['thumbnails',15],
             ['doubt_img',15],['doubt_audio',10],['doubt_ans_audio',10],
             ['doubt_ans_file',10],['dpp_answers',5]];
let ki=0,afterId=0,totalMig=0,running=false;
const log=m=>{const l=document.getElementById('log');l.textContent+=m+"\\n";l.scrollTop=l.scrollHeight;};
function startMig(){ if(running)return; running=true; ki=0; afterId=0; totalMig=0;
  document.getElementById('log').textContent=''; log('Starting...'); run(); }
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
