import os
import logging
import time as _time
os.environ["TZ"] = "Asia/Kolkata"
try:
    _time.tzset()
except Exception:
    pass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import aiofiles

from database import init_db, get_db, get_setting, set_setting
from job_runner import run_status_check
try:
    import mvs_sync
except Exception:
    mvs_sync = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SECRET_KEY  = os.environ.get("SECRET_KEY",  "nios-tracker-secret-2025-mvs")
PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
PORTAL_PASS = os.environ.get("PORTAL_PASS", "MVS2025")
EXCEL_PATH  = os.environ.get("EXCEL_PATH",  os.path.join(os.environ.get("DATA_DIR", "."), "students.xlsx"))
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 12

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer()

app = FastAPI(title="NIOS Status Tracker", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PORTAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NIOS Status Tracker — MVS Foundation</title>
<style>
  :root{
    --primary:#4F46E5; --primary-dark:#4338CA; --primary-light:#EEF2FF;
    --sidebar:#1E293B; --sidebar-hover:#334155; --sidebar-text:#CBD5E1; --sidebar-muted:#64748B;
    --bg:#F1F5F9; --card:#FFFFFF; --border:#E2E8F0; --text:#0F172A;
    --muted:#64748B; --success:#16A34A; --danger:#DC2626; --warn:#EA580C;
    --th-bg:#F8FAFC; --row-hover:#FAFBFF; --chip:#F1F5F9; --soft:#F8FAFC; --dup-bg:#FFF7ED;
    --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
    --shadow-lg:0 10px 25px rgba(0,0,0,.08);
  }
  html[data-theme="dark"]{
    --primary:#6366F1; --primary-dark:#4F46E5; --primary-light:#1E1B4B;
    --sidebar:#0B1220; --sidebar-hover:#1E293B; --sidebar-text:#CBD5E1; --sidebar-muted:#64748B;
    --bg:#0F172A; --card:#1E293B; --border:#334155; --text:#E2E8F0;
    --muted:#94A3B8; --success:#22C55E; --danger:#F87171; --warn:#FB923C;
    --th-bg:#172033; --row-hover:#243043; --chip:#334155; --soft:#172033; --dup-bg:#3a2a18;
    --shadow:0 1px 3px rgba(0,0,0,.4); --shadow-lg:0 10px 25px rgba(0,0,0,.5);
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
  a{text-decoration:none}
  ::-webkit-scrollbar{width:8px;height:8px}
  ::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:4px}

  #login-screen{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#4F46E5 0%,#7C3AED 100%);z-index:1000}
  .login-card{background:var(--card);border-radius:18px;padding:42px;width:380px;
    box-shadow:0 20px 60px rgba(0,0,0,.3)}
  .login-card .logo{width:60px;height:60px;background:linear-gradient(135deg,#4F46E5,#7C3AED);
    border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 18px}
  .login-card h2{text-align:center;font-size:21px;margin-bottom:4px}
  .login-card p.sub{text-align:center;color:var(--muted);font-size:13px;margin-bottom:26px}
  .login-card label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:#334155}
  .login-card input{width:100%;padding:12px 14px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;margin-bottom:16px;transition:.2s}
  .login-card input:focus{outline:none;border-color:var(--primary)}
  .login-card button{width:100%;padding:13px;background:var(--primary);color:#fff;border:none;
    border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:.2s}
  .login-card button:hover{background:var(--primary-dark)}
  #login-error{color:var(--danger);font-size:13px;text-align:center;margin-top:12px;min-height:18px}

  #app{display:none;min-height:100vh}
  .sidebar{position:fixed;left:0;top:0;bottom:0;width:240px;background:var(--sidebar);
    padding:20px 0 12px;display:flex;flex-direction:column;z-index:100}
  .sidebar .brand{padding:0 22px 20px;display:flex;align-items:center;gap:11px;
    border-bottom:1px solid rgba(255,255,255,.08);margin-bottom:12px}
  .sidebar .brand .ic{width:38px;height:38px;background:linear-gradient(135deg,#4F46E5,#7C3AED);
    border-radius:10px;display:flex;align-items:center;justify-content:center}
  .sidebar .brand .ic svg{width:21px;height:21px}
  .sidebar .brand .tx b{color:#fff;font-size:15px;display:block}
  .sidebar .brand .tx span{color:#94A3B8;font-size:11px}
  .nav-scroll{flex:1;overflow-y:auto}
  .side-foot{border-top:1px solid rgba(255,255,255,.08);padding-top:8px;margin-top:6px}
  .nav-item{display:flex;align-items:center;gap:12px;padding:11px 22px;color:var(--sidebar-text);
    cursor:pointer;font-size:14px;font-weight:500;transition:.15s;border-left:3px solid transparent}
  .nav-item:hover{background:var(--sidebar-hover);color:#fff}
  .nav-item.active{background:var(--sidebar-hover);color:#fff;border-left-color:#818CF8;font-weight:600}
  .nav-item.active .ic{color:#A5B4FC}
  .nav-item.logout:hover{background:rgba(248,113,113,.14);color:#FCA5A5}
  .nav-item .ic{width:20px;height:20px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  .nav-item .ic svg{width:19px;height:19px}
  .nav-item .badge-count{margin-left:auto;background:var(--danger);color:#fff;
    font-size:11px;padding:1px 7px;border-radius:10px;font-weight:700;display:none}
  .nav-sep{padding:14px 22px 6px;color:var(--sidebar-muted);font-size:11px;font-weight:700;
    text-transform:uppercase;letter-spacing:.5px}

  .main{margin-left:240px;min-height:100vh}
  .topbar{background:var(--card);border-bottom:1px solid var(--border);padding:0 28px;height:64px;
    display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
  .topbar h1{font-size:19px;font-weight:700}
  .topbar .right{display:flex;align-items:center;gap:16px}

  .bell-wrap{position:relative;cursor:pointer}
  .bell-btn{width:42px;height:42px;border-radius:10px;background:var(--bg);display:flex;
    align-items:center;justify-content:center;font-size:19px;transition:.15s;border:1px solid var(--border)}
  .bell-btn:hover{background:var(--primary-light)}
  .bell-badge{position:absolute;top:-4px;right:-4px;background:var(--danger);color:#fff;
    font-size:10px;font-weight:700;min-width:18px;height:18px;border-radius:9px;
    display:none;align-items:center;justify-content:center;padding:0 4px;border:2px solid #fff}
  .bell-dropdown{position:absolute;right:0;top:52px;width:360px;max-height:440px;overflow-y:auto;
    background:var(--card);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow-lg);
    display:none;z-index:200}
  .bell-dropdown.open{display:block}
  .bell-head{padding:14px 18px;border-bottom:1px solid var(--border);font-weight:700;
    font-size:14px;display:flex;align-items:center;justify-content:space-between}
  .bell-head .cnt{background:var(--warn);color:#fff;font-size:11px;padding:2px 9px;border-radius:10px}
  .notif-item{padding:13px 18px;border-bottom:1px solid var(--border);display:flex;gap:11px}
  .notif-item:last-child{border-bottom:none}
  .notif-item .nm{color:var(--text)}
  .notif-item .dot{width:9px;height:9px;border-radius:50%;background:var(--warn);margin-top:5px;flex-shrink:0}
  .notif-item .nm{font-weight:600;font-size:13px}
  .notif-item .rf{font-size:12px;color:var(--primary);font-family:monospace}
  .notif-item .rk{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.4}
  .notif-empty{padding:34px 18px;text-align:center;color:var(--muted);font-size:13px}

  .content{padding:28px}
  .page-section{display:none}
  .page-section.active{display:block}

  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;
    padding:22px;box-shadow:var(--shadow);margin-bottom:20px}
  .card h3{font-size:15px;margin-bottom:16px;font-weight:700}

  .stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px;margin-bottom:22px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;
    box-shadow:var(--shadow);position:relative;overflow:hidden}
  .stat .ic{position:absolute;right:16px;top:16px;width:38px;height:38px;border-radius:10px;
    display:flex;align-items:center;justify-content:center}
  .stat .ic svg{width:20px;height:20px}
  .stat .lbl{font-size:13px;color:var(--muted);font-weight:600;margin-bottom:8px}
  .stat .val{font-size:30px;font-weight:800;line-height:1}
  .stat .bar{position:absolute;left:0;bottom:0;height:4px;width:100%}

  .btn{display:inline-flex;align-items:center;gap:8px;padding:11px 20px;border-radius:10px;
    font-size:14px;font-weight:600;cursor:pointer;border:none;transition:.15s}
  .btn-primary{background:var(--primary);color:#fff}
  .btn-primary:hover{background:var(--primary-dark);transform:translateY(-1px);box-shadow:0 4px 12px rgba(79,70,229,.3)}
  .btn-outline{background:var(--card);color:var(--primary);border:2px solid var(--primary)}
  .btn-outline:hover{background:var(--primary-light)}
  .btn-success{background:var(--success);color:#fff}
  .btn-success:hover{filter:brightness(1.08)}
  .btn-sm{padding:8px 15px;font-size:13px;border-radius:8px}
  .btn-dl{padding:6px 11px;font-size:12px;font-weight:600;border-radius:7px;cursor:pointer;
    border:1.5px solid var(--primary);background:var(--primary-light);color:var(--primary);transition:.15s}
  .btn-dl:hover{background:var(--primary);color:#fff}
  .btn-dl.loading{background:var(--primary);color:#fff;pointer-events:none;opacity:.92}
  .btn-dl svg{vertical-align:-2px}
  .dl-spin{display:inline-block;width:11px;height:11px;border:2px solid rgba(255,255,255,.45);
    border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:-1px}

  .filter-bar{display:flex;gap:12px;margin-bottom:18px;flex-wrap:wrap}
  .filter-bar input,.filter-bar select{padding:11px 14px;border:2px solid var(--border);
    border-radius:10px;font-size:14px;transition:.15s;background:var(--card);color:var(--text)}
  .filter-bar input{flex:1;min-width:200px}
  .filter-bar input:focus,.filter-bar select:focus{outline:none;border-color:var(--primary)}
  table{width:100%;border-collapse:collapse}
  thead th{text-align:left;padding:13px 14px;font-size:12px;font-weight:700;color:var(--muted);
    text-transform:uppercase;letter-spacing:.4px;background:var(--th-bg);border-bottom:2px solid var(--border)}
  tbody td{padding:14px;border-bottom:1px solid var(--border);font-size:14px;vertical-align:top}
  tbody tr:hover{background:var(--row-hover)}
  .ref-tag{background:var(--chip);padding:3px 8px;border-radius:6px;font-family:monospace;font-size:13px}

  .run-live{color:#2563EB;font-weight:700;font-size:13px}
  .run-done{color:#16A34A;font-weight:700;font-size:13px}
  .run-cancel{color:#94A3B8;font-weight:700;font-size:13px}
  .run-err{color:var(--danger);font-weight:700;font-size:12px}
  .btn-cancel{margin-left:8px;background:#FEE2E2;color:#B91C1C;border:1px solid #FCA5A5;
    padding:3px 11px;border-radius:7px;font-size:12px;font-weight:700;cursor:pointer}
  .btn-cancel:hover{background:#FCA5A5;color:#7F1D1D}

  .card-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
  .card-head h3{margin-bottom:0}
  .btn-refresh{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:8px;
    font-size:13px;font-weight:600;cursor:pointer;border:1px solid var(--border);
    background:var(--soft);color:var(--text);transition:.15s}
  .btn-refresh:hover{background:var(--primary);color:#fff;border-color:var(--primary)}
  .btn-refresh:hover svg{color:#fff}

  .progress-banner{background:var(--card);border:1px solid var(--primary);border-left:4px solid var(--primary);
    border-radius:12px;padding:16px 18px;margin-bottom:18px;box-shadow:var(--shadow)}
  .pb-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:9px}
  .pb-title{display:flex;align-items:center;gap:9px;font-weight:700;font-size:14px;color:var(--text)}
  .pb-pct{font-weight:800;font-size:18px;color:var(--primary)}
  .pb-track{height:9px;background:var(--chip);border-radius:6px;overflow:hidden}
  .pb-fill{height:100%;background:linear-gradient(90deg,#6366F1,#8B5CF6);border-radius:6px;
    transition:width .5s ease;width:0%}
  .pb-sub{font-size:12px;color:var(--muted);margin-top:7px}
  .pb-spin{width:15px;height:15px;border:2.5px solid var(--chip);border-top-color:var(--primary);
    border-radius:50%;animation:spin .8s linear infinite;display:inline-block}
  @keyframes spin{to{transform:rotate(360deg)}}

  .timer-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px}
  .timer-chip{flex:1;min-width:230px;display:flex;align-items:center;gap:12px;background:var(--card);
    border:1px solid var(--border);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
  .timer-chip .tc-ic{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;
    justify-content:center;background:var(--primary-light);color:var(--primary);flex-shrink:0}
  .timer-chip.soon{border-color:var(--warn);border-left:4px solid var(--warn)}
  .timer-chip.soon .tc-ic{background:rgba(234,88,12,.12);color:var(--warn)}
  .tc-lbl{font-size:12px;color:var(--muted);font-weight:600}
  .tc-time{font-size:18px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
  .tc-grp{font-size:11px;color:var(--muted)}
  .run-prog{font-size:11px;color:var(--muted);font-weight:600;margin-left:8px}

  .badge{display:inline-block;padding:5px 12px;border-radius:20px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-pending{background:#FEF9C3;color:#854D0E}
  .b-docs{background:#FFEDD5;color:#9A3412}
  .b-required{background:#FED7AA;color:#9A3412}
  .b-verified{background:#DCFCE7;color:#166534}
  .b-approved{background:#CCFBF1;color:#115E59}
  .b-confirmed{background:#86EFAC;color:#14532D}
  .b-rejected{background:#FECACA;color:#991B1B}
  .b-error{background:#E2E8F0;color:#475569}
  .b-syc{background:#EDE9FE;color:#5B21B6}

  .pg-bar{display:flex;align-items:center;justify-content:space-between;margin-top:18px;flex-wrap:wrap;gap:12px}
  .pg-controls{display:flex;gap:6px;align-items:center}
  .pg-controls button{padding:8px 13px;border:1px solid var(--border);background:var(--card);color:var(--text);
    border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:.15s}
  .pg-controls button:hover:not(:disabled){background:var(--primary-light);border-color:var(--primary)}
  .pg-controls button.active{background:var(--primary);color:#fff;border-color:var(--primary)}
  .pg-controls button:disabled{opacity:.4;cursor:not-allowed}
  .perpage{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
  .perpage select{padding:7px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;background:var(--card);color:var(--text)}

  .drop{border:2.5px dashed var(--border);border-radius:14px;padding:48px;text-align:center;
    transition:.2s;cursor:pointer;background:#FAFBFF}
  .drop:hover,.drop.drag{border-color:var(--primary);background:var(--primary-light)}
  .drop .ic{margin-bottom:12px;color:var(--primary)}
  .drop .ic svg{width:42px;height:42px}

  .up-stats{display:flex;gap:14px;flex-wrap:wrap}
  .up-stat{flex:1;min-width:150px;background:var(--soft);border:1px solid var(--border);
    border-radius:12px;padding:14px 16px}
  .up-stat.dup{border-left:4px solid var(--warn)}
  .up-stat.new{border-left:4px solid var(--success)}
  .us-lbl{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:6px}
  .us-val{font-size:26px;font-weight:800;line-height:1}
  .up-stat.dup .us-val{color:var(--warn)}
  .up-stat.new .us-val{color:var(--success)}
  .prev-head{font-size:13px;font-weight:700;margin-bottom:8px;color:var(--text)}
  .prev-box{max-height:380px;overflow:auto;border:1px solid var(--border);border-radius:12px}
  .prev-box table{width:100%}
  .prev-box thead th{position:sticky;top:0;z-index:1}
  .dup-tag{display:inline-block;background:#FEF3C7;color:#B45309;font-size:11px;font-weight:700;
    padding:2px 9px;border-radius:10px}
  .new-tag{display:inline-block;background:#DCFCE7;color:#15803D;font-size:11px;font-weight:700;
    padding:2px 9px;border-radius:10px}
  .up-prog{background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
  .up-prog-top{display:flex;justify-content:space-between;font-weight:700;font-size:13px;margin-bottom:8px}
  .up-track{height:8px;background:var(--chip);border-radius:5px;overflow:hidden}
  .up-fill{height:100%;width:0%;background:linear-gradient(90deg,#6366F1,#8B5CF6);border-radius:5px;transition:width .2s}
  .up-eta{font-size:12px;color:var(--muted);margin-top:7px}

  .legend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .legend-item{padding:14px 16px;border-radius:11px}
  .legend-item .nm{font-weight:700;font-size:13px}
  .legend-item .ds{font-size:11px;opacity:.75;font-weight:500}

  .dist-row{display:flex;align-items:center;gap:12px;margin-bottom:11px}
  .dist-row .nm{width:230px;font-size:13px;font-weight:600;flex-shrink:0}
  .dist-row .track{flex:1;height:22px;background:#F1F5F9;border-radius:11px;overflow:hidden}
  .dist-row .fill{height:100%;border-radius:11px;transition:width .5s}
  .dist-row .ct{width:40px;text-align:right;font-weight:700;font-size:13px}

  .empty{text-align:center;color:var(--muted);padding:40px;font-size:14px}

  #toast{position:fixed;bottom:28px;right:28px;background:var(--text);color:#fff;padding:14px 22px;
    border-radius:11px;font-size:14px;box-shadow:var(--shadow-lg);opacity:0;transform:translateY(20px);
    transition:.3s;z-index:2000;max-width:360px}
  #toast.show{opacity:1;transform:translateY(0)}

  @media(max-width:820px){
    .sidebar{width:64px}.sidebar .brand .tx,.nav-item span.lbl,.nav-sep{display:none}
    .nav-item{justify-content:center;padding:14px}.main{margin-left:64px}
    .bell-dropdown{width:300px}
  }
</style>
</head>
<body>

<div id="login-screen">
  <div class="login-card">
    <img src="/logo.png" alt="MVS Foundation" style="width:68px;height:68px;border-radius:50%;display:block;margin:0 auto 18px;object-fit:cover;box-shadow:0 4px 14px rgba(0,0,0,.18)">
    <h2>NIOS Status Tracker</h2>
    <p class="sub">MVS Foundation — Admin Portal</p>
    <label>Username</label>
    <input type="text" id="lg-user" placeholder="admin" autocomplete="username">
    <label>Password</label>
    <input type="password" id="lg-pass" placeholder="enter password" autocomplete="current-password"
      onkeydown="if(event.key==='Enter')doLogin()">
    <button onclick="doLogin()">Sign In</button>
    <div id="login-error"></div>
  </div>
</div>

<div id="app">
  <aside class="sidebar">
    <div class="brand">
      <img src="/logo.png" alt="MVS" style="width:38px;height:38px;border-radius:50%;object-fit:cover;flex-shrink:0">
      <div class="tx"><b>NIOS Tracker</b><span>MVS Foundation</span></div>
    </div>
    <nav class="nav-scroll">
    <div class="nav-item active" data-page="dashboard" onclick="nav('dashboard')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></svg></span><span class="lbl">Dashboard</span></div>
    <div class="nav-item" data-page="students" onclick="nav('students')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg></span><span class="lbl">Active Students</span></div>
    <div class="nav-item" data-page="confirmed" onclick="nav('confirmed')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></span><span class="lbl">Confirmed</span></div>
    <div class="nav-item" data-page="required" onclick="nav('required')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="11" x2="12" y2="15"/><line x1="12" y1="18" x2="12" y2="18"/></svg></span><span class="lbl">Required</span>
      <span class="badge-count" id="nav-required-badge">0</span></div>
    <div class="nav-item" data-page="syc" onclick="nav('syc')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg></span><span class="lbl">SYC Students</span></div>
    <div class="nav-sep">Activity</div>
    <div class="nav-item" data-page="history" onclick="nav('history')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/></svg></span><span class="lbl">Change History</span></div>
    <div class="nav-item" data-page="runlogs" onclick="nav('runlogs')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></span><span class="lbl">Run Logs</span></div>
    <div class="nav-sep">Manage</div>
    <div class="nav-item" data-page="upload" onclick="nav('upload')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></span><span class="lbl">Upload Excel</span></div>
    <div class="nav-item" data-page="settings" onclick="nav('settings')">
      <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></span><span class="lbl">Settings</span></div>
    </nav>
    <div class="side-foot">
      <div class="nav-item" onclick="toggleTheme()">
        <span class="ic" id="theme-ic"></span><span class="lbl" id="theme-lbl">Dark Mode</span></div>
      <div class="nav-item logout" onclick="doLogout()">
        <span class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg></span><span class="lbl">Logout</span></div>
    </div>
  </aside>

  <div class="main">
    <div class="topbar">
      <h1 id="page-title">Dashboard</h1>
      <div class="right">
        <button class="btn btn-success btn-sm" id="run-now-btn" onclick="runNow()">
          <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"/></svg> Run Now</button>
        <div class="bell-btn" onclick="refreshPage(this)" title="Refresh this page" style="cursor:pointer">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
        </div>
        <div class="bell-wrap">
          <div class="bell-btn" onclick="toggleBell(event)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="19" height="19"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
            <span class="bell-badge" id="bell-badge">0</span></div>
          <div class="bell-dropdown" id="bell-dropdown" onclick="event.stopPropagation()">
            <div class="bell-head">Document Required
              <span class="cnt" id="bell-head-cnt">0</span></div>
            <div id="bell-list"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="content">
      <section id="sec-dashboard" class="page-section active">
        <div id="run-progress" class="progress-banner" style="display:none">
          <div class="pb-top">
            <span class="pb-title"><span class="pb-spin"></span><span id="pb-label">Checking students…</span></span>
            <span class="pb-pct" id="pb-pct">0%</span>
          </div>
          <div class="pb-track"><div class="pb-fill" id="pb-fill" style="width:0%"></div></div>
          <div class="pb-sub" id="pb-sub">0 / 0 done</div>
        </div>

        <div id="next-runs" class="timer-row"></div>

        <div class="stat-grid" id="stat-grid"></div>
        <div class="card">
          <h3>Session-wise Students</h3>
          <div id="session-counts"><div class="empty">Loading...</div></div>
        </div>
        <div class="card">
          <h3>Status Distribution</h3>
          <div id="distribution"><div class="empty">Loading...</div></div>
        </div>
        <div class="card">
          <div class="card-head">
            <h3>Recent Runs</h3>
            <button class="btn-refresh" onclick="loadDashboard()" title="Refresh">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
              Refresh</button>
          </div>
          <div style="overflow-x:auto"><table>
            <thead><tr><th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th></tr></thead>
            <tbody id="recent-runs"></tbody>
          </table></div>
        </div>
      </section>

      <section id="sec-students" class="page-section">
        <div class="card">
          <div class="filter-bar">
            <input type="text" id="s-search" placeholder="Search name / reference / email..." oninput="debounceStudents()">
            <select id="s-status" onchange="loadStudents(1)">
              <option value="">All Statuses</option>
              <option>Pending</option><option>Documents Verification In Progress</option>
              <option>Document Required</option><option>Verified</option>
              <option>Approved</option><option>Rejected</option><option>Fetch Error</option>
            </select>
            <select id="s-session" onchange="loadStudents(1)"><option value="">All Sessions</option></select>
            <select id="s-class" onchange="loadStudents(1)"><option value="">All Classes</option><option value="10">Class 10</option><option value="12">Class 12</option></select>
            <select id="s-datepreset" onchange="onDatePreset('s',()=>loadStudents(1))">
              <option value="">All dates</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="7d">Last 7 days</option>
              <option value="custom">Custom range…</option>
            </select>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('normal')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div class="filter-bar" id="s-daterow" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="s-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="s-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadStudents(1)">Apply</button>
          </div>
          <div id="s-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Last Checked</th>
            </tr></thead><tbody id="s-body"></tbody></table>
          </div>
          <div class="pg-bar" id="s-pg"></div>
        </div>
      </section>

      <section id="sec-confirmed" class="page-section">
        <div class="card">
          <h3>Admission Confirmed Students</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            Their admission is confirmed. Document download links appear here.</p>
          <div class="filter-bar">
            <input type="text" id="c-search" placeholder="Search name / reference / email..." oninput="debounceConfirmed()">
            <select id="c-status" onchange="loadConfirmed(1)">
              <option value="">All Statuses</option>
              <option>Admission Confirmed</option><option>Admitted</option>
            </select>
            <select id="c-session" onchange="loadConfirmed(1)"><option value="">All Sessions</option></select>
            <select id="c-class" onchange="loadConfirmed(1)"><option value="">All Classes</option><option value="10">Class 10</option><option value="12">Class 12</option></select>
            <select id="c-datepreset" onchange="onDatePreset('c',()=>loadConfirmed(1))">
              <option value="">All dates</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="7d">Last 7 days</option>
              <option value="custom">Custom range…</option>
            </select>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('confirmed')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div class="filter-bar" id="c-daterow" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="c-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="c-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadConfirmed(1)">Apply</button>
          </div>
          <div id="c-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th>
              <th>Status</th><th>Downloads</th><th>Confirmed On</th>
            </tr></thead><tbody id="c-body"></tbody></table>
          </div>
          <div class="pg-bar" id="c-pg"></div>
        </div>
      </section>

      <section id="sec-syc" class="page-section">
        <div class="card">
          <h3>SYC Students</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            SYC students are <b>not status-checked</b>. Their Hall Ticket is fetched directly using
            <b>Enrollment Number + Date of Birth</b>. Click "Download Hall Ticket" — it shows progress and opens the hall ticket in the correct format (then use Save as PDF).</p>
          <div class="filter-bar">
            <input type="text" id="syc-search" placeholder="Search name / enrollment / mobile..." oninput="debounceSyc()">
            <button class="btn btn-outline btn-sm" onclick="loadSyc(1)">Refresh</button>
          </div>
          <div id="syc-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Enrollment No</th><th>Student Name</th><th>Mobile</th><th>DOB</th><th>Status</th><th>Hall Ticket</th><th>Remove</th>
            </tr></thead><tbody id="syc-body"></tbody></table>
          </div>
          <div class="pg-bar" id="syc-pg"></div>
        </div>
      </section>

      <section id="sec-required" class="page-section">
        <div class="card">
          <h3>Document Required — Action Needed</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            These need to be resolved by the counsellor. Once resolved, they return to the Active list on the next run.</p>
          <div class="filter-bar">
            <input type="text" id="r-search" placeholder="Search name / reference / email..." oninput="debounceRequired()">
            <select id="r-status" onchange="loadRequired(1)">
              <option value="">All Statuses</option>
              <option>Document Required</option>
            </select>
            <select id="r-session" onchange="loadRequired(1)"><option value="">All Sessions</option></select>
            <button class="btn btn-outline btn-sm" onclick="exportStudents('required')">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div id="r-count" style="font-size:13px;color:var(--muted);margin-bottom:14px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>#</th><th>Reference No</th><th>Student Name</th><th>Session</th><th>RC Comment / Remark</th>
            </tr></thead><tbody id="r-body"></tbody></table>
          </div>
          <div class="pg-bar" id="r-pg"></div>
        </div>
      </section>

      <section id="sec-history" class="page-section">
        <div class="card">
          <h3>Status Change History</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Choose a date &amp; time range — e.g. use "Custom range" to view confirmations from yesterday 6:30 PM until now.</p>
          <div class="filter-bar">
            <select id="h-preset" onchange="onHistPreset()">
              <option value="all">All time</option>
              <option value="today">Today</option>
              <option value="yesterday">Yesterday</option>
              <option value="24h">Last 24 hours</option>
              <option value="custom">Custom range…</option>
            </select>
            <select id="h-status" onchange="loadHistory(1)">
              <option value="">All status changes</option>
              <option>Admission Confirmed</option><option>Verified</option>
              <option>Documents Verification In Progress</option><option>Document Required</option>
              <option>Approved</option><option>Rejected</option>
            </select>
            <button class="btn btn-outline btn-sm" onclick="exportHistory()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              Export Excel</button>
          </div>
          <div class="filter-bar" id="h-custom" style="display:none">
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">From
              <input type="datetime-local" id="h-from"></label>
            <label style="font-size:13px;color:var(--muted);display:flex;align-items:center;gap:8px">To
              <input type="datetime-local" id="h-to"></label>
            <button class="btn btn-primary btn-sm" onclick="loadHistory(1)">Apply</button>
          </div>
          <div id="h-count" style="font-size:13px;color:var(--muted);margin-bottom:12px"></div>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Reference No</th><th>Student</th><th>Old Status</th><th>New Status</th><th>Changed At</th>
            </tr></thead><tbody id="h-body"></tbody></table>
          </div>
          <div class="pg-bar" id="h-pg"></div>
        </div>
      </section>

      <section id="sec-runlogs" class="page-section">
        <div class="card">
          <h3>Run Logs</h3>
          <div style="overflow-x:auto">
            <table><thead><tr>
              <th>Run At</th><th>Type</th><th>Checked</th><th>Changed</th><th>Failed</th><th>Status</th>
            </tr></thead><tbody id="rl-body"></tbody></table>
          </div>
        </div>
      </section>

      <section id="sec-upload" class="page-section">
        <div class="card">
          <h3>Upload Student Excel</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
            Upload an Excel file (.xlsx). Columns: Student Name, Mobile No, Class, Reference Number, Enrol No, Email, Date of Birth, Admission Session.</p>
          <div style="background:var(--soft);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin-bottom:18px">
            <div style="font-size:13px;font-weight:600;margin-bottom:4px">&#128196; Not sure about the format? Download a sample sheet:</div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:10px">Fill your data in the same column order, then upload.</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <button class="btn btn-outline btn-sm" onclick="downloadSample('regular')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Sample — Regular (On Demand / Stream 2 / April-October)</button>
              <button class="btn btn-outline btn-sm" onclick="downloadSample('syc')">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Sample — SYC Students</button>
            </div>
          </div>
          <div class="drop" id="drop" onclick="document.getElementById('file-input').click()">
            <div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg></div>
            <div style="font-weight:600;font-size:15px">Click or drag Excel file here</div>
            <div style="color:var(--muted);font-size:13px;margin-top:5px">.xlsx files only</div>
          </div>
          <input type="file" id="file-input" accept=".xlsx,.xls" style="display:none" onchange="handleFile(this.files[0])">
          <div id="upload-status" style="margin-top:16px"></div>
          <div id="upload-summary" style="margin-top:16px"></div>
          <div id="upload-preview" style="margin-top:16px"></div>
          <div style="margin-top:18px;display:flex;gap:12px">
            <button class="btn btn-outline btn-sm" onclick="downloadExcel()">Download Updated Excel</button>
          </div>
        </div>
      </section>

      <section id="sec-settings" class="page-section">
        <div class="card">
          <h3>⏱ Recheck Intervals</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:18px">
            Set separate intervals for each group. Confirmed students are skipped automatically.</p>
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap">
            <span style="width:280px;font-weight:600">📗 Regular (On Demand + Stream 2)</span>
            <input type="number" id="iv-regular" min="1" max="72" value="6"
              style="width:90px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:15px">
            <span style="color:var(--muted)">hours</span>
          </div>
          <div style="display:flex;gap:12px;align-items:center;margin-bottom:18px;flex-wrap:wrap">
            <span style="width:280px;font-weight:600">📘 Public Exam (April / October + any year)</span>
            <input type="number" id="iv-public" min="1" max="72" value="12"
              style="width:90px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:15px">
            <span style="color:var(--muted)">hours</span>
          </div>
          <button class="btn btn-primary btn-sm" onclick="saveIntervals()">Save Intervals</button>
          <div id="iv-status" style="margin-top:12px;font-size:13px"></div>
        </div>
        <div class="card">
          <h3>&#128172; WhatsApp Auto-Send</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            When a student's status becomes <b>Admission Confirmed</b>, their documents are
            automatically sent to their WhatsApp as secure links (each student receives this <b>only once</b>).</p>
          <div id="wa-config" style="font-size:13px;margin-bottom:14px;color:var(--muted)">Loading…</div>
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;margin-bottom:18px">
            <input type="checkbox" id="wa-enabled" onchange="saveWa()" style="width:18px;height:18px;cursor:pointer">
            <span style="font-weight:600">Enable auto-send</span>
          </label>
          <div style="border-top:1px solid var(--border);padding-top:16px">
            <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
              <b>Send a test message</b> — choose a template, enter your number, and a sample message will be sent:</p>
            <div style="display:flex;gap:10px;flex-wrap:wrap">
              <select id="wa-group" style="padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
                <option value="ondemand">On Demand (3inone)</option>
                <option value="stream2">Stream 2 (str2toc1)</option>
                <option value="public">Public April/October</option>
                <option value="syc">SYC (Hall Ticket)</option>
              </select>
              <input type="text" id="wa-num" placeholder="WhatsApp number (e.g. 7065187637)"
                style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
              <button class="btn btn-primary btn-sm" onclick="testWa()">Send Test</button>
            </div>
          </div>
          <div id="wa-status" style="margin-top:12px;font-size:13px"></div>
        </div>
        <div class="card">
          <h3>Status Colour Legend</h3>
          <div class="legend-grid">
            <div class="legend-item b-pending"><div class="nm">Pending</div><div class="ds">Awaiting review</div></div>
            <div class="legend-item b-docs"><div class="nm">Documents Verification In Progress</div><div class="ds">Under review</div></div>
            <div class="legend-item b-required"><div class="nm">Document Required</div><div class="ds">Action needed by counsellor</div></div>
            <div class="legend-item b-verified"><div class="nm">Verified</div><div class="ds">Documents verified</div></div>
            <div class="legend-item b-approved"><div class="nm">Approved</div><div class="ds">Application approved</div></div>
            <div class="legend-item b-confirmed"><div class="nm">Admission Confirmed</div><div class="ds">Admission confirmed</div></div>
            <div class="legend-item b-rejected"><div class="nm">Rejected</div><div class="ds">Application rejected</div></div>
            <div class="legend-item b-syc"><div class="nm">SYC</div><div class="ds">SYC student — hall ticket ready</div></div>
          </div>
        </div>
        <div class="card">
          <h3>Phase 2 — Test Login & Find Download Links</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            Enter a <b>confirmed student's</b> Reference No + DOB. This logs in and finds the download links on the dashboard.
            (Used to verify how the links appear.)</p>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">
            <input type="text" id="dbg-ref" placeholder="Reference No (e.g. D0026300046)"
              style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
            <input type="text" id="dbg-dob" placeholder="DOB DD-MM-YYYY (e.g. 08-08-2007)"
              style="flex:1;min-width:180px;padding:11px;border:2px solid var(--border);border-radius:10px;font-size:14px">
          </div>
          <button class="btn btn-primary btn-sm" onclick="testLogin()">Test Login & Find Links</button>
          <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
            <p style="color:var(--muted);font-size:13px;margin-bottom:10px">
              <b>Inspect document page</b> — run this if a download fails (to see how the PDF is embedded):</p>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
              <select id="dbg-kind" style="padding:10px;border:2px solid var(--border);border-radius:10px;font-size:14px">
                <option value="id_card">ID Card</option>
                <option value="app_form">Application Form</option>
                <option value="hall_ticket">Hall Ticket</option>
              </select>
              <button class="btn btn-outline btn-sm" onclick="inspectDoc()">Inspect Doc Page</button>
              <button class="btn btn-outline btn-sm" onclick="findAddr()">Find RC Address (Stream 2)</button>
            </div>
          </div>
          <div id="dbg-status" style="margin-top:12px;font-size:13px"></div>
          <pre id="dbg-result" style="margin-top:12px;background:#0F172A;color:#A5F3FC;padding:16px;
            border-radius:10px;font-size:12px;overflow-x:auto;max-height:400px;display:none;white-space:pre-wrap"></pre>
        </div>
        <div class="card" style="border:1px solid var(--danger)">
          <h3 style="color:var(--danger)">&#9888; Danger Zone — Clear All Data</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:16px">
            This will <b>permanently delete</b> all students, statuses, change history and the uploaded sheet.
            Your settings (intervals, WhatsApp) will be kept. You will need to upload a new sheet afterwards.</p>
          <button class="btn btn-sm" style="background:var(--danger);color:#fff" onclick="resetData()">Clear All Data</button>
          <div id="reset-status" style="margin-top:12px;font-size:13px"></div>
        </div>
      </section>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
const API = window.location.origin;
let TOKEN = "";
let perPage = 20;
let sessionsLoaded = false;

async function doLogin(){
  const u=document.getElementById("lg-user").value;
  const p=document.getElementById("lg-pass").value;
  const err=document.getElementById("login-error");
  err.textContent="";
  try{
    const r=await fetch(API+"/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:u,password:p})});
    if(!r.ok){err.textContent="Invalid username or password";return;}
    const d=await r.json();
    TOKEN=d.token;
    document.getElementById("login-screen").style.display="none";
    document.getElementById("app").style.display="block";
    loadDashboard();
    loadNextRuns();
    startProgressPoll();
    setInterval(refreshBell,60000);
    setInterval(loadNextRuns,60000);
  }catch(e){err.textContent="Connection error: "+e.message;}
}

async function api(path,method="GET",body=null){
  const opt={method,headers:{"Authorization":"Bearer "+TOKEN}};
  if(body){opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body);}
  const r=await fetch(API+path,opt);
  if(r.status===401){location.reload();throw new Error("Session expired");}
  if(!r.ok){const e=await r.json().catch(()=>({detail:"Error"}));throw new Error(e.detail||"Request failed");}
  return r.json();
}

/* ---------- icons ---------- */
const DL_ICON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13" style="vertical-align:-2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const SUN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>';
const MOON='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
const CLOCK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="18" height="18"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';

/* ---------- theme ---------- */
function applyTheme(t){
  document.documentElement.setAttribute("data-theme",t);
  const ic=document.getElementById("theme-ic"),lb=document.getElementById("theme-lbl");
  if(ic)ic.innerHTML=(t==="dark")?SUN:MOON;
  if(lb)lb.textContent=(t==="dark")?"Light Mode":"Dark Mode";
}
function toggleTheme(){
  const cur=(document.documentElement.getAttribute("data-theme")==="dark")?"light":"dark";
  try{localStorage.setItem("nios_theme",cur);}catch(e){}
  applyTheme(cur);
}
(function(){var t="light";try{t=localStorage.getItem("nios_theme")||"light";}catch(e){}applyTheme(t);})();

/* ---------- logout ---------- */
function doLogout(){
  if(!confirm("Log out of the portal?")) return;
  TOKEN="";stopProgressPoll();if(timerInt){clearInterval(timerInt);timerInt=null;}
  document.getElementById("app").style.display="none";
  document.getElementById("login-screen").style.display="flex";
  const p=document.getElementById("lg-pass");if(p)p.value="";
}

/* ---------- helpers ---------- */
function secActive(id){const e=document.getElementById("sec-"+id);return e&&e.classList.contains("active");}

/* ---------- live progress ---------- */
let progInt=null,wasRunning=false;
function startProgressPoll(){if(progInt)return;pollProgress();progInt=setInterval(pollProgress,2500);}
function stopProgressPoll(){if(progInt){clearInterval(progInt);progInt=null;}}
async function pollProgress(){
  try{
    const d=await api("/api/progress");
    const box=document.getElementById("run-progress");if(!box)return;
    if(d.running){
      wasRunning=true;box.style.display="block";
      const pct=d.percent||0;
      document.getElementById("pb-pct").textContent=pct+"%";
      document.getElementById("pb-fill").style.width=pct+"%";
      document.getElementById("pb-sub").innerHTML=
        (d.current||0)+" / "+(d.total||0)+" checked &nbsp;·&nbsp; "+
        "<b style='color:var(--success)'>"+(d.changed||0)+"</b> changed &nbsp;·&nbsp; "+
        (d.same||0)+" same &nbsp;·&nbsp; <b>"+(d.remaining||0)+"</b> remaining";
      const g=d.group_type==="public"?"Public (April / October)":(d.group_type==="regular"?"On Demand + Stream 2":"all");
      document.getElementById("pb-label").textContent="Checking "+g+" students…";
      // live filtering: refresh whatever view the counsellor is on, as it happens
      if(secActive("dashboard"))loadDashboard();
      else if(secActive("confirmed"))loadConfirmed(1);
      else if(secActive("required"))loadRequired(1);
      else if(secActive("students"))loadStudents(1);
    }else{
      box.style.display="none";
      if(wasRunning){wasRunning=false;
        if(secActive("dashboard"))loadDashboard();
        if(secActive("runlogs"))loadRunLogs();}
    }
  }catch(e){}
}

/* ---------- next-run timers ---------- */
let timers=[],timerInt=null;
async function loadNextRuns(){
  try{
    const d=await api("/api/next-runs");
    timers=(d.runs||[]).map(r=>({label:r.label,remain:(r.seconds==null?null:r.seconds),at:Date.now()}));
    renderTimers();
    if(!timerInt)timerInt=setInterval(renderTimers,1000);
  }catch(e){}
}
function fmtDur(s){
  if(s==null)return "Not scheduled";if(s<0)s=0;
  const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
  let o="";if(h>0)o+=h+"h ";o+=(m<10&&h>0?"0":"")+m+"m ";o+=(ss<10?"0":"")+ss+"s";return o;
}
function renderTimers(){
  const el=document.getElementById("next-runs");if(!el)return;
  if(!timers.length){el.innerHTML="";return;}
  el.innerHTML=timers.map(t=>{
    let rem=(t.remain==null)?null:Math.max(0,t.remain-Math.floor((Date.now()-t.at)/1000));
    const soon=(rem!=null&&rem<=1800);
    return '<div class="timer-chip'+(soon?' soon':'')+'">'+
      '<div class="tc-ic">'+CLOCK+'</div><div>'+
      '<div class="tc-lbl">Next auto-run'+(soon?' — running soon':'')+'</div>'+
      '<div class="tc-time">'+fmtDur(rem)+'</div>'+
      '<div class="tc-grp">'+t.label+'</div></div></div>';
  }).join("");
}

const titles={dashboard:"Dashboard",students:"Active Students",confirmed:"Confirmed Students",
  syc:"SYC Students",
  required:"Document Required",history:"Change History",runlogs:"Run Logs",upload:"Upload Excel",settings:"Settings"};
function refreshPage(btn){
  // reload the data of whichever page is currently open (no full reload, stays logged in)
  if(btn){var ic=btn.querySelector("svg");if(ic){ic.style.transition="transform .6s";ic.style.transform="rotate(360deg)";
    setTimeout(function(){ic.style.transition="";ic.style.transform="";},650);}}
  const sec=document.querySelector(".page-section.active");
  const page=sec?sec.id.replace("sec-",""):"dashboard";
  nav(page);
  refreshBell();
  showToast("Refreshed");
}

function nav(page){
  document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n.dataset.page===page));
  document.querySelectorAll(".page-section").forEach(s=>s.classList.remove("active"));
  document.getElementById("sec-"+page).classList.add("active");
  document.getElementById("page-title").textContent=titles[page];
  if(page==="dashboard")loadDashboard();
  if(page==="students")loadStudents(1);
  if(page==="confirmed")loadConfirmed(1);
  if(page==="syc")loadSyc(1);
  if(page==="required")loadRequired(1);
  if(page==="history")loadHistory();
  if(page==="runlogs")loadRunLogs();
  if(page==="settings"){loadIntervals();loadWa();}
}

async function loadDashboard(){
  try{
    const d=await api("/api/dashboard");
    const c=d.counts||{};
    document.getElementById("stat-grid").innerHTML=
      statCard("Total Students",d.total_students,SI.users,"#4F46E5")+
      statCard("Changes Today",c.changes_today||0,SI.activity,"#0891B2")+
      statCard("Admission Confirmed",c.confirmed||0,SI.check,"#16A34A")+
      statCard("SYC Students",c.syc||0,SI.users,"#7C3AED")+
      statCard("Verified",c.verified||0,SI.shield,"#65A30D")+
      statCard("Document Required",c.document_required||0,SI.file,"#EA580C")+
      statCard("In Verification",c.doc_verification||0,SI.loader,"#D97706");
    renderDistribution(d.status_distribution,d.total_students);
    renderSessionCounts(d.session_counts||[]);
    renderRuns(d.recent_runs,"recent-runs");
    renderBell(d.notifications||[]);
  }catch(e){showToast("Error: "+e.message);}
}
function renderSessionCounts(arr){
  const el=document.getElementById("session-counts");
  if(!arr.length){el.innerHTML='<div class="empty">No data yet</div>';return;}
  el.innerHTML='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px">'+
    arr.map(s=>'<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;'+
      'background:var(--soft);border:1px solid var(--border);border-radius:11px">'+
      '<span style="font-size:13px;font-weight:600">'+(s.session||"—")+'</span>'+
      '<span style="font-size:18px;font-weight:800;color:var(--primary)">'+s.cnt+'</span></div>').join("")+'</div>';
}
function statCard(lbl,val,svg,col){
  return '<div class="stat"><div class="ic" style="background:'+col+'1A;color:'+col+'">'+svg+
    '</div><div class="lbl">'+lbl+'</div><div class="val">'+val+
    '</div><div class="bar" style="background:'+col+'"></div></div>';
}
const SI={
  users:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
  activity:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
  shield:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>',
  file:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
  loader:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>'
};
const DIST_COLOURS={
  "Pending":"#FBBF24","Documents Verification In Progress":"#FB923C","Document Required":"#F97316",
  "Verified":"#4ADE80","Approved":"#2DD4BF","Admission Confirmed":"#22C55E",
  "Rejected":"#EF4444","Fetch Error":"#94A3B8","Unknown":"#CBD5E1"};
function renderDistribution(dist,total){
  const el=document.getElementById("distribution");
  if(!dist||!dist.length){el.innerHTML='<div class="empty">No data yet. Upload Excel & run a check.</div>';return;}
  const max=Math.max.apply(null,dist.map(d=>d.cnt).concat([1]));
  el.innerHTML=dist.map(d=>{
    const col=DIST_COLOURS[d.current_status]||"#CBD5E1";
    const w=(d.cnt/max*100).toFixed(0);
    return '<div class="dist-row"><div class="nm">'+(d.current_status||"Unknown")+'</div>'+
      '<div class="track"><div class="fill" style="width:'+w+'%;background:'+col+'"></div></div>'+
      '<div class="ct">'+d.cnt+'</div></div>';
  }).join("");
}
function renderRuns(runs,id){
  const el=document.getElementById(id);
  if(!runs||!runs.length){el.innerHTML='<tr><td colspan="6" class="empty">No runs yet</td></tr>';return;}
  el.innerHTML=runs.map(r=>{
    var st;
    if(r.status==="running"){
      st='<span class="run-live">● running</span>'+
         ' <button class="btn-cancel" onclick="cancelRun('+r.id+')">Cancel</button>';
    }else if(r.status==="cancelled"){
      st='<span class="run-cancel">✕ cancelled</span>';
    }else if(r.status==="completed"){
      st='<span class="run-done">✓ completed</span>';
    }else{
      st='<span class="run-err">'+r.status+'</span>';
    }
    return '<tr><td>'+r.run_at+'</td><td><span class="ref-tag">'+(r.group_type||"all")+
      '</span></td><td>'+r.total_checked+'</td><td style="color:var(--primary);font-weight:700">'+r.total_changed+
      '</td><td style="color:'+(r.total_failed?'var(--danger)':'inherit')+'">'+r.total_failed+
      '</td><td>'+st+'</td></tr>';
  }).join("");
}

async function cancelRun(rid){
  if(!confirm("Cancel this run?")) return;
  try{
    const fd=new FormData();fd.append("run_id",rid);
    const res=await fetch("/api/cancel-run",{method:"POST",headers:{Authorization:"Bearer "+TOKEN},body:fd});
    const d=await res.json();
    if(!res.ok) throw new Error(d.detail||"failed");
    showToast("✓ "+(d.message||"Cancelled"));
    const dash=document.getElementById("sec-dashboard");
    const rlog=document.getElementById("sec-runlogs");
    if(dash&&dash.classList.contains("active")) loadDashboard();
    if(rlog&&rlog.classList.contains("active")) loadRunLogs();
  }catch(e){showToast(""+e.message);}
}

function toggleBell(e){e.stopPropagation();document.getElementById("bell-dropdown").classList.toggle("open");}
document.addEventListener("click",()=>document.getElementById("bell-dropdown").classList.remove("open"));
async function refreshBell(){try{const d=await api("/api/dashboard");renderBell(d.notifications||[]);}catch(e){}}
function renderBell(notifs){
  const n=notifs.length;
  const badge=document.getElementById("bell-badge");
  const navB=document.getElementById("nav-required-badge");
  badge.textContent=n;badge.style.display=n?"flex":"none";
  navB.textContent=n;navB.style.display=n?"inline-block":"none";
  document.getElementById("bell-head-cnt").textContent=n;
  const list=document.getElementById("bell-list");
  list.innerHTML=n?notifs.map(x=>'<div class="notif-item"><div class="dot"></div><div>'+
    '<div class="nm">'+(x.student_name||"—")+'</div>'+
    '<div class="rf">'+(x.reference_no||"No ref")+'</div>'+
    (x.remark?'<div class="rk">'+x.remark+'</div>':"")+'</div></div>').join("")
    :'<div class="notif-empty">No pending documents</div>';
}

let stTimer,cTimer,rTimer;
function debounceStudents(){clearTimeout(stTimer);stTimer=setTimeout(()=>loadStudents(1),400);}
function debounceConfirmed(){clearTimeout(cTimer);cTimer=setTimeout(()=>loadConfirmed(1),400);}
function debounceRequired(){clearTimeout(rTimer);rTimer=setTimeout(()=>loadRequired(1),400);}

function badge(s){
  const m={"Pending":"b-pending","Documents Verification In Progress":"b-docs","Document Required":"b-required",
    "Verified":"b-verified","Approved":"b-approved","Admission Confirmed":"b-confirmed","Rejected":"b-rejected","SYC":"b-syc"};
  return '<span class="badge '+(m[s]||'b-error')+'">'+(s||'Unknown')+'</span>';
}
function dlLinks(s){
  if(!s.reference_no||!s.dob) return '<span style="color:var(--warn);font-size:11px">ref/DOB missing</span>';
  const sess=(s.session||"").toLowerCase();
  const isPublic=sess.includes("april")||sess.includes("october")||sess.includes("public");
  const isStream2=sess.includes("stream 2");
  let b=[dlBtn(s,"id_card","ID Card")];
  if(isPublic){
    // Public exam students: ONLY id card
  }else if(isStream2){
    b.push(dlBtn(s,"app_form","App Form"));          // Stream 2: id + app form
  }else{
    b.push(dlBtn(s,"app_form","App Form"));          // On Demand: all three
    b.push(dlBtn(s,"hall_ticket","Hall Ticket"));
  }
  return '<div style="display:flex;flex-wrap:wrap;gap:5px">'+b.join("")+'</div>';
}
function dlBtn(s,kind,label){
  return '<button class="btn-dl" onclick="downloadDoc(this,&quot;'+s.reference_no+'&quot;,&quot;'+
    (s.dob||"")+'&quot;,&quot;'+kind+'&quot;,&quot;'+label+'&quot;)">'+DL_ICON+' '+label+'</button>';
}
function waBtn(s){
  if(!s.row_key)return "";
  const sent=(s.whatsapp_sent==1);
  return '<button class="btn-dl" style="background:#16A34A;color:#fff;border-color:#16A34A;margin-top:4px" '+
    'onclick="resendWa(&quot;'+s.row_key+'&quot;,this)">'+(sent?'Resend WhatsApp':'Send WhatsApp')+'</button>';
}
async function resendWa(rowKey,btn){
  if(btn&&btn.dataset.busy==="1")return;
  if(!confirm("Send the documents to this student on WhatsApp?"))return;
  let orig="";
  if(btn){btn.dataset.busy="1";orig=btn.innerHTML;btn.innerHTML="Sending...";}
  try{const r=await api("/api/wa-resend","POST",{row_key:rowKey});
    if(r.ok)showToast("WhatsApp sent successfully");
    else showToast("Failed: "+(r.info||"error"));}
  catch(e){showToast("Error: "+e.message);}
  finally{if(btn){btn.innerHTML=orig;btn.dataset.busy="";}}
}
async function downloadDoc(btn,ref,dob,kind,name){
  if(btn&&btn.dataset.busy==="1")return;          // already running -> ignore extra clicks
  let orig="",pct=1,fake=null;
  if(btn){
    btn.dataset.busy="1";btn.classList.add("loading");orig=btn.innerHTML;
    btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';
    fake=setInterval(()=>{ if(pct<92){pct+=Math.max(1,Math.floor((92-pct)/9));
      btn.innerHTML='<span class="dl-spin"></span> '+pct+'%';}},650);
  }
  const restore=(txt)=>{
    if(fake)clearInterval(fake);
    if(btn){ btn.innerHTML=txt||orig;
      setTimeout(()=>{btn.innerHTML=orig;btn.dataset.busy="";btn.classList.remove("loading");},1400); }
  };
  try{
    const q=new URLSearchParams({ref:ref,dob:dob,kind:kind});
    const r=await fetch(API+"/api/download-doc?"+q.toString(),{headers:{"Authorization":"Bearer "+TOKEN}});
    if(!r.ok){const e=await r.json().catch(()=>({detail:"failed"}));showToast("Error: "+(e.detail||"download failed"));restore();return;}
    const ctype=r.headers.get("Content-Type")||"";
    const blob=await r.blob();
    const url=URL.createObjectURL(blob);
    if(ctype.includes("pdf")){
      const a=document.createElement("a");a.href=url;a.download=name.replace(/ /g,"_")+"_"+ref+".pdf";a.click();
      showToast(name+" downloaded");
    }else{
      const w=window.open(url,"_blank");
      if(!w){showToast("Popup blocked — please allow popups and click again");}
      else{showToast(name+" opened — tap 'Save as PDF' at the top");}
    }
    setTimeout(()=>URL.revokeObjectURL(url),120000);
    restore('100%');
  }catch(e){showToast("Error: "+e.message);restore();}
}
function fillSessions(arr){
  if(!arr)return;
  ["s-session","c-session","r-session"].forEach(id=>{
    const sel=document.getElementById(id);
    if(!sel)return;
    const cur=sel.value;
    sel.length=1;  // keep only the first "All Sessions" option
    arr.forEach(s=>{if(s){const o=document.createElement("option");o.value=s;o.textContent=s;sel.appendChild(o);}});
    sel.value=cur; // restore previous selection
  });
}

async function loadStudents(page){
  page=page||1;
  const dr=dateRange("s");
  const q=new URLSearchParams({page:page,per_page:perPage,view:"normal",
    search:document.getElementById("s-search").value,
    status_filter:document.getElementById("s-status").value,
    session_filter:document.getElementById("s-session").value,
    class_filter:fval("s-class"),date_from:dr.from,date_to:dr.to});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("s-count").textContent="Showing "+d.students.length+" of "+d.total+" active students";
    const b=document.getElementById("s-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_checked||"—")+'</td></tr>').join("")
      :'<tr><td colspan="6" class="empty">No active students found</td></tr>';
    renderPg("s-pg",page,d.pages,"loadStudents");
  }catch(e){showToast(""+e.message);}
}

async function loadConfirmed(page){
  page=page||1;
  const dr=dateRange("c");
  const q=new URLSearchParams({page:page,per_page:perPage,view:"confirmed",
    search:document.getElementById("c-search").value,
    status_filter:(document.getElementById("c-status")?document.getElementById("c-status").value:""),
    session_filter:document.getElementById("c-session").value,
    class_filter:fval("c-class"),date_from:dr.from,date_to:dr.to});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("c-count").textContent=d.total+" confirmed students";
    const b=document.getElementById("c-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td>'+badge(s.current_status)+'</td><td style="font-size:12px">'+dlLinks(s)+waBtn(s)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+(s.last_changed||"—")+'</td></tr>').join("")
      :'<tr><td colspan="7" class="empty">No confirmed students yet</td></tr>';
    renderPg("c-pg",page,d.pages,"loadConfirmed");
  }catch(e){showToast(""+e.message);}
}

let sycTimer;
function debounceSyc(){clearTimeout(sycTimer);sycTimer=setTimeout(()=>loadSyc(1),400);}
async function loadSyc(page){
  page=page||1;
  const search=fval("syc-search");
  try{
    const q=new URLSearchParams({page:page,search:search});
    const d=await api("/api/syc?"+q.toString());
    document.getElementById("syc-count").textContent=(d.total||0)+" SYC student(s)";
    const tb=document.getElementById("syc-body");
    if(!d.students||!d.students.length){tb.innerHTML='<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">No SYC students yet. Upload a sheet with ADMISSION SESSION = SYC and click Run Now.</td></tr>';document.getElementById("syc-pg").innerHTML="";return;}
    tb.innerHTML=d.students.map((s,i)=>sycRow(s,(page-1)*20+i+1)).join("");
    renderPg("syc-pg",page,d.pages,"loadSyc");
  }catch(e){showToast(""+e.message);}
}
function sycRow(s,i){
  var nm=(s.student_name||'').replace(/"/g,'&quot;');
  return '<tr><td>'+i+'</td><td>'+(s.enrollment_no||'—')+'</td><td>'+(s.student_name||'')+'</td>'+
    '<td>'+(s.mobile||'')+'</td><td>'+(s.dob||'')+'</td>'+
    '<td><span class="badge b-syc">SYC &#10003; Ready</span></td><td>'+
    '<button class="btn-dl" onclick="downloadSycHall(&quot;'+s.row_key+'&quot;,this)">'+DL_ICON+' Download Hall Ticket</button>'+
    '<div class="syc-prog" style="display:none;height:7px;background:#E2E8F0;border-radius:4px;margin-top:7px;overflow:hidden;max-width:220px"><div class="syc-bar" style="height:100%;width:0;background:#4F46E5;transition:width .35s"></div></div>'+
    '<div class="syc-msg" style="font-size:11px;color:var(--muted);margin-top:3px"></div></td>'+
    '<td><button title="Delete this SYC student" onclick="deleteSyc(&quot;'+s.row_key+'&quot;,&quot;'+nm+'&quot;)" '+
    'style="background:rgba(239,68,68,.12);color:#DC2626;border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:6px 9px;cursor:pointer;font-weight:600">'+
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14" style="vertical-align:-2px"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button></td></tr>';
}
async function deleteSyc(rowKey,name){
  if(!confirm("Delete SYC student "+name+"?\\n\\nThis removes ONLY this SYC student. No other data is affected."))return;
  try{
    await api("/api/syc-delete?row_key="+encodeURIComponent(rowKey),"POST");
    showToast("Deleted "+name);
    loadSyc(1);
    try{loadDashboard();}catch(e){}
  }catch(e){showToast(""+e.message);}
}
async function downloadSycHall(rowKey,btn){
  if(btn.dataset.busy==="1")return;
  btn.dataset.busy="1";
  const cell=btn.parentElement;
  const prog=cell.querySelector(".syc-prog"),bar=cell.querySelector(".syc-bar"),msg=cell.querySelector(".syc-msg");
  prog.style.display="block";bar.style.background="#4F46E5";bar.style.width="3%";msg.textContent="Fetching from NIOS... (~15 sec)";
  let p=3;const tick=setInterval(function(){p=Math.min(p+(p<70?7:2),92);bar.style.width=p+"%";},900);
  try{
    const r=await fetch(API+"/api/syc-doc?"+new URLSearchParams({row_key:rowKey}).toString(),{headers:{"Authorization":"Bearer "+TOKEN}});
    clearInterval(tick);
    if(!r.ok){let dt="failed";try{dt=(await r.json()).detail||dt;}catch(e){}throw new Error(dt);}
    bar.style.width="100%";msg.textContent="Opening...";
    const blob=await r.blob();
    window.open(URL.createObjectURL(blob),"_blank");
    msg.innerHTML='<span style="color:var(--success)">&#10003; Opened — use "Save as PDF" to download</span>';
  }catch(e){clearInterval(tick);bar.style.background="var(--danger)";bar.style.width="100%";msg.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
  finally{btn.dataset.busy="";setTimeout(function(){prog.style.display="none";},5000);}
}

async function loadRequired(page){
  page=page||1;
  const q=new URLSearchParams({page:page,per_page:perPage,view:"required",
    search:document.getElementById("r-search").value,
    status_filter:(document.getElementById("r-status")?document.getElementById("r-status").value:""),
    session_filter:document.getElementById("r-session").value});
  try{
    const d=await api("/api/students?"+q.toString());
    fillSessions(d.sessions);
    document.getElementById("r-count").textContent=d.total+" students need documents";
    const b=document.getElementById("r-body");
    b.innerHTML=d.students.length?d.students.map((s,i)=>'<tr>'+
      '<td style="color:var(--muted)">'+((page-1)*perPage+i+1)+'</td>'+
      '<td><span class="ref-tag">'+(s.reference_no||"—")+'</span></td>'+
      '<td>'+(s.student_name||"—")+'</td><td style="font-size:13px">'+(s.session||"—")+'</td>'+
      '<td style="font-size:13px;color:var(--warn);max-width:420px">'+(s.remark||"(no comment captured)")+'</td></tr>').join("")
      :'<tr><td colspan="5" class="empty">No pending documents</td></tr>';
    renderPg("r-pg",page,d.pages,"loadRequired");
  }catch(e){showToast(""+e.message);}
}

function renderPg(id,page,total,fnName){
  const el=document.getElementById(id);
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="'+fnName+'('+(page-1)+')" '+(page<=1?"disabled":"")+'>‹ Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="'+fnName+'('+i+')">'+i+'</button>';
  ctrl+='<button onclick="'+fnName+'('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next ›</button></div>';
  const sel='<div class="perpage">Per page: <select onchange="setPerPage(this.value,&quot;'+fnName+'&quot;)">'+
    [10,20,50,100].map(n=>'<option value="'+n+'" '+(n===perPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
function setPerPage(v,fnName){perPage=parseInt(v);window[fnName](1);}

let histPerPage=10;
function fmtDT(d){
  const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
}
function dtLocalToStr(v){ // "2026-06-11T18:30" -> "2026-06-11 18:30:00"
  if(!v)return "";
  v=v.replace("T"," ");
  return v.length===16?v+":00":v;
}
function onHistPreset(){
  const custom=document.getElementById("h-preset").value==="custom";
  document.getElementById("h-custom").style.display=custom?"flex":"none";
  if(custom){
    const f=document.getElementById("h-from"),t=document.getElementById("h-to");
    if(f&&!f.value){const y=new Date();y.setDate(y.getDate()-1);y.setHours(18,30,0,0);f.value=toLocalInput(y);}
    if(t&&!t.value){t.value=toLocalInput(new Date());}
  }else loadHistory(1);
}
function toLocalInput(d){const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+"T"+p(d.getHours())+":"+p(d.getMinutes());}
function histRange(){
  const preset=fval("h-preset");
  const now=new Date();
  let from="",to="";
  if(preset==="today"){const s=new Date(now);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="yesterday"){const s=new Date(now);s.setDate(s.getDate()-1);s.setHours(0,0,0,0);
    const e=new Date(s);e.setHours(23,59,59,0);from=fmtDT(s);to=fmtDT(e);}
  else if(preset==="24h"){const s=new Date(now.getTime()-86400000);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="custom"){from=dtLocalToStr(fval("h-from"));to=dtLocalToStr(fval("h-to"));}
  return {from,to};
}
async function loadHistory(page){
  page=page||1;
  const rg=histRange();
  const q=new URLSearchParams({page:page,per_page:histPerPage,from_dt:rg.from,to_dt:rg.to,
    status:fval("h-status")});
  try{
    const d=await api("/api/history?"+q.toString());
    const items=d.items||[];
    const cnt=document.getElementById("h-count");
    if(cnt)cnt.textContent=d.total+" change(s)"+(rg.from?" in selected range":"");
    document.getElementById("h-body").innerHTML=items.length?items.map(x=>'<tr>'+
      '<td><span class="ref-tag">'+(x.reference_no||"—")+'</span></td><td>'+(x.student_name||"—")+'</td>'+
      '<td>'+badge(x.old_status)+'</td><td>'+badge(x.new_status)+'</td>'+
      '<td style="font-size:12px;color:var(--muted)">'+x.changed_at+'</td></tr>').join("")
      :'<tr><td colspan="5" class="empty">No changes in this range</td></tr>';
    renderHistPg(d.page,d.pages,d.total);
  }catch(e){showToast(""+e.message);}
}
async function exportHistory(){
  const rg=histRange();
  const q=new URLSearchParams({from_dt:rg.from,to_dt:rg.to,status:fval("h-status")});
  try{
    showToast("Preparing Excel...");
    const r=await fetch(API+"/api/export-history?"+q.toString(),{headers:{Authorization:"Bearer "+TOKEN}});
    if(!r.ok){showToast("Export failed");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;a.download="nios_change_history.xlsx";a.click();
    URL.revokeObjectURL(url);showToast("Excel downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function renderHistPg(page,total,totalRows){
  const el=document.getElementById("h-pg");if(!el)return;
  if(!total||total<=0){el.innerHTML="";return;}
  let ctrl='<div class="pg-controls">';
  ctrl+='<button onclick="loadHistory('+(page-1)+')" '+(page<=1?"disabled":"")+'>‹ Prev</button>';
  const start=Math.max(1,page-2),end=Math.min(total,page+2);
  for(let i=start;i<=end;i++)ctrl+='<button class="'+(i===page?'active':'')+'" onclick="loadHistory('+i+')">'+i+'</button>';
  ctrl+='<button onclick="loadHistory('+(page+1)+')" '+(page>=total?"disabled":"")+'>Next ›</button></div>';
  const sel='<div class="perpage">'+(totalRows!=null?'<span>'+totalRows+' changes</span> · ':'')+
    'Per page: <select onchange="histPerPage=parseInt(this.value);loadHistory(1)">'+
    [10,20,30,50,100].map(n=>'<option value="'+n+'" '+(n===histPerPage?"selected":"")+'>'+n+'</option>').join("")+
    '</select></div>';
  el.innerHTML=ctrl+sel;
}
async function loadRunLogs(){
  try{const l=await api("/api/run-logs");renderRuns(l,"rl-body");}catch(e){showToast(""+e.message);}
}

const drop=document.getElementById("drop");
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("drag");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("drag");}));
drop.addEventListener("drop",e=>{if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0]);});
function handleFile(file){
  if(!file)return;
  const st=document.getElementById("upload-status");
  const sm=document.getElementById("upload-summary");
  const pv=document.getElementById("upload-preview");
  sm.innerHTML="";pv.innerHTML="";
  st.innerHTML='<div class="up-prog"><div class="up-prog-top"><span>Uploading…</span>'+
    '<span id="up-pct">0%</span></div><div class="up-track"><div class="up-fill" id="up-fill"></div></div>'+
    '<div class="up-eta" id="up-eta"></div></div>';
  const fd=new FormData();fd.append("file",file);
  const xhr=new XMLHttpRequest();
  const startT=Date.now();
  xhr.open("POST",API+"/api/upload-excel");
  xhr.setRequestHeader("Authorization","Bearer "+TOKEN);
  xhr.upload.onprogress=function(e){
    if(!e.lengthComputable)return;
    const pct=Math.round(e.loaded*100/e.total);
    const pe=document.getElementById("up-pct"),pf=document.getElementById("up-fill"),pet=document.getElementById("up-eta");
    if(pe)pe.textContent=pct+"%";
    if(pf)pf.style.width=pct+"%";
    const elapsed=(Date.now()-startT)/1000;
    const speed=e.loaded/Math.max(elapsed,0.001);
    const eta=speed>0?(e.total-e.loaded)/speed:0;
    if(pet)pet.textContent=pct<100?("~"+fmtEta(eta)+" remaining · "+fmtBytes(e.loaded)+" / "+fmtBytes(e.total)):"Processing file on server…";
  };
  xhr.onload=function(){
    let d={};try{d=JSON.parse(xhr.responseText);}catch(e){}
    if(xhr.status<200||xhr.status>=300){st.innerHTML='<div style="color:var(--danger)">'+(d.detail||"Upload failed")+'</div>';return;}
    st.innerHTML='<div style="color:var(--success);font-weight:600">'+(d.message||"Uploaded")+'</div>'+
      '<div style="margin-top:12px"><button class="btn btn-success btn-sm" onclick="runNow()">Run Check Now</button></div>';
    if(d.parse_error){sm.innerHTML='<div style="color:var(--danger);font-size:13px">Preview error: '+d.parse_error+'</div>';}
    else{renderUploadSummary(d);renderUploadPreview(d.preview||[]);}
    showToast("Excel uploaded — "+(d.unique||0)+" new students");
  };
  xhr.onerror=function(){st.innerHTML='<div style="color:var(--danger)">Upload error — try again</div>';};
  xhr.send(fd);
}
function fmtBytes(b){if(b<1024)return b+" B";if(b<1048576)return (b/1024).toFixed(1)+" KB";return (b/1048576).toFixed(1)+" MB";}
function fmtEta(s){s=Math.ceil(s);if(s<1)return "0s";if(s<60)return s+"s";const m=Math.floor(s/60),ss=s%60;return m+"m "+ss+"s";}
function renderUploadSummary(d){
  const sm=document.getElementById("upload-summary");
  sm.innerHTML='<div class="up-stats">'+
    '<div class="up-stat"><div class="us-lbl">Total in list</div><div class="us-val">'+(d.total||0)+'</div></div>'+
    '<div class="up-stat dup"><div class="us-lbl">Already in system</div><div class="us-val">'+(d.duplicates||0)+'</div></div>'+
    '<div class="up-stat new"><div class="us-lbl">New students</div><div class="us-val">'+(d.unique||0)+'</div></div>'+
    '</div>'+
    (d.duplicates>0?'<div style="font-size:12px;color:var(--muted);margin-top:8px">'+
      d.duplicates+' student(s) already tracked (matched by reference / email / name+phone) — '+
      'these will not be added again, only their status will be updated. '+(d.unique||0)+' new student(s) will be added.</div>':
      '<div style="font-size:12px;color:var(--success);margin-top:8px">Sabhi '+(d.unique||0)+' students new hain.</div>');
}
function renderUploadPreview(rows){
  const pv=document.getElementById("upload-preview");
  if(!rows.length){pv.innerHTML="";return;}
  const head='<tr><th>#</th><th>Name</th><th>Reference</th><th>Email</th><th>Class</th><th>Session</th><th>Status</th></tr>';
  const body=rows.map((s,i)=>'<tr'+(s.dup?' style="background:var(--dup-bg)"':'')+'>'+
    '<td style="color:var(--muted)">'+(i+1)+'</td>'+
    '<td>'+(s.student_name||"—")+'</td>'+
    '<td><span class="ref-tag">'+(s.reference_no||s.enrollment_no||"—")+'</span></td>'+
    '<td style="font-size:12px">'+(s.email||"—")+'</td>'+
    '<td>'+(s.class_level||"—")+'</td>'+
    '<td style="font-size:12px">'+(s.session||"—")+'</td>'+
    '<td>'+(s.dup?'<span class="dup-tag">Duplicate</span>':'<span class="new-tag">New</span>')+'</td></tr>').join("");
  pv.innerHTML='<div class="prev-head">Preview — '+rows.length+' rows (scroll to see all)</div>'+
    '<div class="prev-box"><table>'+head+body+'</table></div>';
}
async function downloadExcel(){
  const r=await fetch(API+"/api/download-excel",{headers:{"Authorization":"Bearer "+TOKEN}});
  if(!r.ok){showToast("No Excel found. Run a check first.");return;}
  const blob=await r.blob();const url=URL.createObjectURL(blob);
  const a=document.createElement("a");a.href=url;a.download="nios_status_updated.xlsx";a.click();
  URL.revokeObjectURL(url);
}
async function downloadSample(type){
  try{
    const r=await fetch(API+"/api/sample-sheet?type="+type,{headers:{"Authorization":"Bearer "+TOKEN}});
    if(!r.ok){showToast("Sample download failed");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download="MVS_sample_"+type+".xlsx";a.click();
    URL.revokeObjectURL(url);
    showToast("Sample sheet downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
function fval(id){const e=document.getElementById(id);return e?e.value:"";}
function dateRange(prefix){
  const preset=fval(prefix+"-datepreset");
  const now=new Date();
  let from="",to="";
  if(preset==="today"){const s=new Date(now);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="yesterday"){const s=new Date(now);s.setDate(s.getDate()-1);s.setHours(0,0,0,0);
    const e=new Date(s);e.setHours(23,59,59,0);from=fmtDT(s);to=fmtDT(e);}
  else if(preset==="7d"){const s=new Date(now);s.setDate(s.getDate()-6);s.setHours(0,0,0,0);from=fmtDT(s);to=fmtDT(now);}
  else if(preset==="custom"){from=dtLocalToStr(fval(prefix+"-from"));to=dtLocalToStr(fval(prefix+"-to"));}
  return {from,to};
}
function onDatePreset(prefix,reload){
  const custom=fval(prefix+"-datepreset")==="custom";
  const row=document.getElementById(prefix+"-daterow");
  if(row)row.style.display=custom?"flex":"none";
  if(!custom&&reload)reload();
}
async function exportStudents(view){
  let search="",status="",session="",cls="",from="",to="";
  if(view==="confirmed"){search=fval("c-search");status=fval("c-status");session=fval("c-session");cls=fval("c-class");const dr=dateRange("c");from=dr.from;to=dr.to;}
  else if(view==="required"){search=fval("r-search");status=fval("r-status");session=fval("r-session");}
  else{search=fval("s-search");status=fval("s-status");session=fval("s-session");cls=fval("s-class");const dr=dateRange("s");from=dr.from;to=dr.to;}
  const q=new URLSearchParams({view:view,search:search,status_filter:status,session_filter:session,
    class_filter:cls,date_from:from,date_to:to});
  try{
    showToast("Preparing Excel...");
    const r=await fetch(API+"/api/export-students?"+q.toString(),{headers:{Authorization:"Bearer "+TOKEN}});
    if(!r.ok){showToast("Export failed");return;}
    const blob=await r.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;
    a.download="nios_"+(view==="normal"?"active":view)+"_students.xlsx";a.click();
    URL.revokeObjectURL(url);
    showToast("Excel downloaded");
  }catch(e){showToast("Error: "+e.message);}
}
async function runNow(){
  const btn=document.getElementById("run-now-btn");
  if(btn&&btn.dataset.busy==="1")return;
  if(btn){btn.dataset.busy="1";btn.style.opacity="0.6";btn.style.pointerEvents="none";}
  try{
    const r=await api("/api/run-now","POST");
    showToast(r.message+" — running in the background");
    const box=document.getElementById("run-progress");
    if(box){box.style.display="block";document.getElementById("pb-pct").textContent="0%";
      document.getElementById("pb-fill").style.width="0%";
      document.getElementById("pb-sub").textContent="Starting…";
      document.getElementById("pb-label").textContent="Checking students…";}
    startProgressPoll();
  }catch(e){showToast("Error: "+e.message);}
  finally{ setTimeout(()=>{if(btn){btn.dataset.busy="";btn.style.opacity="";btn.style.pointerEvents="";}},4000); }
}

async function loadIntervals(){
  try{const r=await api("/api/intervals");
    document.getElementById("iv-regular").value=r.regular;
    document.getElementById("iv-public").value=r.public;}catch(e){}
}
async function saveIntervals(){
  const regular=parseInt(document.getElementById("iv-regular").value);
  const pub=parseInt(document.getElementById("iv-public").value);
  try{const r=await api("/api/intervals","POST",{regular:regular,public:pub});
    document.getElementById("iv-status").innerHTML='<span style="color:var(--success)">'+r.message+'</span>';
    showToast("Intervals saved!");}
  catch(e){document.getElementById("iv-status").innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function loadWa(){
  try{const r=await api("/api/wa-settings");
    document.getElementById("wa-enabled").checked=r.enabled;
    const cfg=document.getElementById("wa-config");
    if(!r.configured){
      cfg.innerHTML='<span style="color:var(--danger)">&#10007; AISENSY_API_KEY is not set in Railway environment variables</span>';
    }else{
      const c=r.campaigns||{};
      const row=(lbl,v)=>'<div style="margin:2px 0">'+lbl+': '+
        (v?'<b style="color:var(--success)">'+v+'</b>':'<span style="color:var(--warn)">not set</span>')+'</div>';
      cfg.innerHTML='<div style="color:var(--success);margin-bottom:6px">&#10003; API key configured</div>'+
        row("On Demand",c.ondemand)+row("Stream 2",c.stream2)+row("Public",c.public)+row("SYC",c.syc);
    }
  }catch(e){}
}
async function saveWa(){
  const en=document.getElementById("wa-enabled").checked;
  try{await api("/api/wa-settings","POST",{enabled:en});
    showToast(en?"WhatsApp auto-send enabled":"WhatsApp auto-send disabled");}
  catch(e){showToast("Error: "+e.message);}
}
async function testWa(){
  const num=document.getElementById("wa-num").value.trim();
  const group=document.getElementById("wa-group").value;
  const s=document.getElementById("wa-status");
  if(!num){s.innerHTML='<span style="color:var(--danger)">Please enter a number</span>';return;}
  s.innerHTML='Sending...';
  try{const r=await api("/api/wa-test","POST",{number:num,group:group});
    if(r.ok)s.innerHTML='<span style="color:var(--success)">&#10003; Test message sent. Please check your WhatsApp.</span>';
    else s.innerHTML='<span style="color:var(--danger)">&#10007; '+(r.info||"failed")+'</span>';}
  catch(e){s.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

function showToast(msg){
  const t=document.getElementById("toast");t.textContent=msg;t.classList.add("show");
  setTimeout(()=>t.classList.remove("show"),3500);
}

async function testLogin(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Logging in (solving captcha, ~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob});
    const d=await api("/api/debug-login?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    const ok=d.logged_in_guess;
    st.innerHTML=ok?'<span style="color:var(--success)">Login successful! See the links below.</span>'
      :'<span style="color:var(--warn)">Login may have failed (or the page is different). Details below.</span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function inspectDoc(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  const kind=document.getElementById("dbg-kind").value;
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Inspecting the '+kind+' page (~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob,kind:kind});
    const d=await api("/api/debug-doc?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    st.innerHTML='<span style="color:var(--success)">Inspection complete — structure below</span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function findAddr(){
  const ref=document.getElementById("dbg-ref").value.trim();
  const dob=document.getElementById("dbg-dob").value.trim();
  if(!ref||!dob){showToast("Please enter both Reference No and DOB");return;}
  const st=document.getElementById("dbg-status");
  const pre=document.getElementById("dbg-result");
  st.innerHTML='<span style="color:var(--muted)">Finding the Regional Centre address from the ID card (~15 sec)...</span>';
  pre.style.display="none";
  try{
    const q=new URLSearchParams({ref:ref,dob:dob});
    const d=await api("/api/debug-idcard?"+q.toString());
    if(d.error){st.innerHTML='<span style="color:var(--danger)">'+d.error+'</span>';return;}
    st.innerHTML='<span style="color:var(--success)">Extracted address: <b>'+(d.extracted_address||"(blank — please share the text below)")+'</b></span>';
    pre.style.display="block";
    pre.textContent=JSON.stringify(d,null,2);
  }catch(e){st.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}

async function resetData(){
  if(!confirm("Are you sure? All students, statuses, history and the sheet will be DELETED. This cannot be undone."))return;
  if(!confirm("Final confirmation — do you really want to clear all data?"))return;
  const s=document.getElementById("reset-status");
  s.innerHTML="Clearing...";
  try{const r=await api("/api/reset-data","POST",{});
    s.innerHTML='<span style="color:var(--success)">&#10003; '+r.message+' You can now upload a new sheet.</span>';
    showToast("All data cleared");}
  catch(e){s.innerHTML='<span style="color:var(--danger)">'+e.message+'</span>';}
}
</script>
</body>
</html>
"""

def create_token(username):
    expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != PORTAL_USER:
            raise HTTPException(status_code=401, detail="Invalid token")
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Token expired or invalid")

scheduler = BackgroundScheduler()

def _last_run_time(group_type):
    """Most recent completed run for this group (or a manual 'all' run)."""
    conn = get_db()
    row = conn.execute(
        "SELECT run_at FROM run_logs WHERE group_type IN (?, 'all') AND status='completed' "
        "ORDER BY id DESC LIMIT 1", (group_type,)).fetchone()
    conn.close()
    if row and row["run_at"]:
        try:
            return datetime.strptime(row["run_at"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    return None

def reschedule_jobs():
    """Schedule both jobs. Next run = last_run + interval. If overdue, run shortly."""
    reg = int(get_setting("interval_regular", "6"))
    pub = int(get_setting("interval_public", "12"))
    now = datetime.now()
    for jid, grp, hours in [("job_regular", "regular", reg), ("job_public", "public", pub)]:
        last = _last_run_time(grp)
        if last:
            nxt = last + timedelta(hours=hours)
            if nxt <= now:
                nxt = now + timedelta(seconds=20)      # overdue -> run shortly
        else:
            nxt = now + timedelta(hours=hours)          # no history -> wait one interval
        scheduler.add_job(lambda g=grp: run_status_check(g),
                          trigger=IntervalTrigger(hours=hours),
                          id=jid, replace_existing=True, next_run_time=nxt)
        logger.info(f"{jid}: every {hours}h | last_run={last} | next_run={nxt}")

@app.on_event("startup")
async def startup():
    init_db()
    reschedule_jobs()
    scheduler.start()
    logger.info("Scheduler started")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)

@app.get("/", response_class=HTMLResponse)
async def serve_portal():
    return PORTAL_HTML

@app.get("/health")
async def health():
    return {"status": "ok", "captcha_key_set": bool(os.environ.get("CAPTCHA_API_KEY", ""))}

@app.get("/mvs-test")
async def mvs_test(key: str = ""):
    """Browser test for the MVS connection (READ-ONLY — kuch likhta nahi).
    Open: https://<your-tracker-url>/mvs-test?key=YOUR_TRACKER_KEY"""
    if not mvs_sync:
        return {"ok": False, "error": "mvs_sync.py not found in tracker repo"}
    if not getattr(mvs_sync, "MVS_TRACKER_KEY", "") or key != mvs_sync.MVS_TRACKER_KEY:
        return {"ok": False, "error": "Wrong or missing ?key=  (use your TRACKER_KEY value)"}
    try:
        by_session, total = {}, 0
        for sess in mvs_sync.SESSIONS:
            rows = mvs_sync.fetch_students_for_tracker(sess)
            total += len(rows)
            by_session[sess] = {
                "count": len(rows),
                "sample": [
                    {"name": r["student_name"], "ref": r["reference_no"],
                     "enrol": r["enrollment_no"], "dob": r["dob"], "mobile": r["mobile"]}
                    for r in rows[:2]
                ],
            }
        return {"ok": True, "mvs_mode_on": mvs_sync.enabled(),
                "total_students": total, "by_session": by_session}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "hint": "Check MVS_API_URL (/exec), MVS_TRACKER_KEY, and that Apps Script New Version is deployed."}

@app.post("/api/login")
async def login(body: dict):
    if body.get("username") != PORTAL_USER or body.get("password") != PORTAL_PASS:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": create_token(body.get("username")), "username": body.get("username")}

@app.get("/api/dashboard")
async def dashboard(user=Depends(verify_token)):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM student_status").fetchone()[0]
    runs = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT 10").fetchall()
    dist = conn.execute("SELECT current_status, COUNT(*) as cnt FROM student_status GROUP BY current_status").fetchall()

    # Counts by status
    def count_status(s):
        return conn.execute("SELECT COUNT(*) FROM student_status WHERE current_status=?", (s,)).fetchone()[0]
    confirmed_cnt = count_status("Admission Confirmed")
    verified_cnt  = count_status("Verified")
    docreq_cnt    = count_status("Document Required")
    docverif_cnt  = count_status("Documents Verification In Progress")
    syc_cnt = conn.execute(
        "SELECT COUNT(*) FROM student_status WHERE (session LIKE '%syc%' OR current_status='SYC')"
    ).fetchone()[0]

    # Changes today
    today = datetime.now().strftime("%Y-%m-%d")
    changes_today = conn.execute(
        "SELECT COUNT(*) FROM status_history WHERE changed_at LIKE ?", (f"{today}%",)).fetchone()[0]

    # Notifications: document required students (name + ref)
    notifs = conn.execute(
        "SELECT student_name, reference_no, remark FROM student_status WHERE current_status='Document Required' ORDER BY last_changed DESC LIMIT 50"
    ).fetchall()

    # Session-wise totals
    sess_counts = conn.execute(
        "SELECT session, COUNT(*) as cnt FROM student_status WHERE session != '' GROUP BY session ORDER BY cnt DESC"
    ).fetchall()

    conn.close()
    jr = scheduler.get_job("job_regular")
    next_run = str(jr.next_run_time) if jr and jr.next_run_time else "Not scheduled"
    return {
        "total_students": total, "next_run": next_run,
        "status_distribution": [dict(r) for r in dist],
        "recent_runs": [dict(r) for r in runs],
        "counts": {
            "confirmed": confirmed_cnt, "verified": verified_cnt,
            "document_required": docreq_cnt, "doc_verification": docverif_cnt,
            "changes_today": changes_today, "syc": syc_cnt,
        },
        "notifications": [dict(n) for n in notifs],
        "session_counts": [dict(s) for s in sess_counts],
    }

def _build_student_where(view, search, status_filter, session_filter,
                         class_filter="", date_from="", date_to=""):
    """Shared WHERE builder so the table and its Excel export stay perfectly in sync.
    NULL-safe so students with missing status/date are never silently hidden."""
    wc, params = [], []
    if view == "confirmed":
        wc.append("is_confirmed = 1")
    elif view == "required":
        wc.append("current_status = 'Document Required'")
    else:  # normal = active students, exclude confirmed and SYC (NULL-safe)
        wc.append("COALESCE(is_confirmed,0) = 0")
        wc.append("COALESCE(current_status,'') != 'SYC'")
        wc.append("(session IS NULL OR session NOT LIKE '%syc%')")
    if search:
        wc.append("(reference_no LIKE ? OR student_name LIKE ? OR email LIKE ?)")
        params += [f"%{search}%", f"%{search}%", f"%{search}%"]
    if status_filter:
        wc.append("current_status = ?"); params.append(status_filter)
    if session_filter:
        wc.append("session = ?"); params.append(session_filter)
    if class_filter:                       # "10" matches 10/10TH, "12" matches 12/12TH
        wc.append("class_level LIKE ?"); params.append(f"{class_filter}%")
    # Date/time filter on when the status last changed; fall back to last_checked so a
    # student with an empty last_changed is never dropped. Accepts either a full
    # "YYYY-MM-DD HH:MM:SS" (custom range with time) or a plain "YYYY-MM-DD".
    def _dtval(v, end):
        v = (v or "").strip()
        if not v:
            return ""
        return v if len(v) > 10 else (v + (" 23:59:59" if end else " 00:00:00"))
    _dt = "COALESCE(NULLIF(last_changed,''), last_checked)"
    df, dtv = _dtval(date_from, False), _dtval(date_to, True)
    if df:
        wc.append(f"{_dt} >= ?"); params.append(df)
    if dtv:
        wc.append(f"{_dt} <= ?"); params.append(dtv)
    return (("WHERE " + " AND ".join(wc)) if wc else ""), params

@app.get("/api/students")
async def get_students(page: int=1, per_page: int=50, search: str="",
                       status_filter: str="", session_filter: str="",
                       class_filter: str="", date_from: str="", date_to: str="",
                       view: str="normal", user=Depends(verify_token)):
    conn = get_db()
    offset = (page - 1) * per_page
    where, params = _build_student_where(view, search, status_filter, session_filter,
                                         class_filter, date_from, date_to)
    total = conn.execute(f"SELECT COUNT(*) FROM student_status {where}", params).fetchone()[0]
    students = conn.execute(
        f"SELECT * FROM student_status {where} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, offset]).fetchall()
    sessions = conn.execute("SELECT DISTINCT session FROM student_status WHERE session != ''").fetchall()
    conn.close()
    return {"students": [dict(s) for s in students], "total": total, "page": page,
            "per_page": per_page, "pages": max(1, (total+per_page-1)//per_page),
            "sessions": [s["session"] for s in sessions]}

@app.get("/api/export-students")
async def export_students(view: str="normal", search: str="", status_filter: str="",
                         session_filter: str="", class_filter: str="",
                         date_from: str="", date_to: str="", user=Depends(verify_token)):
    """Export the CURRENTLY FILTERED list (active / confirmed / required) to .xlsx.
    Honours the exact same filters as the on-screen table (search, status, session,
    class 10/12, and date range)."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    conn = get_db()
    where, params = _build_student_where(view, search, status_filter, session_filter,
                                         class_filter, date_from, date_to)
    rows = conn.execute(f"SELECT * FROM student_status {where} ORDER BY student_name", params).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Students"
    headers = ["#", "Reference No", "Student Name", "Mobile", "Class", "Email",
               "Session", "Status", "Remark", "Last Checked"]
    ws.append(headers)
    for i, r in enumerate(rows, 1):
        ws.append([i, r["reference_no"], r["student_name"], r["mobile"], r["class_level"],
                   r["email"], r["session"], r["current_status"], r["remark"], r["last_checked"]])
    hdr_fill = PatternFill("solid", fgColor="4F46E5")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
    for idx, w in enumerate([5, 18, 26, 14, 8, 28, 22, 30, 44, 20], 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    label = {"confirmed": "confirmed", "required": "document_required"}.get(view, "active")
    fname = f"nios_{label}_students.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.get("/api/history")
async def get_history(page: int = 1, per_page: int = 10, from_dt: str = "", to_dt: str = "",
                     status: str = "", user=Depends(verify_token)):
    conn = get_db()
    wc, params = [], []
    if from_dt:
        wc.append("changed_at >= ?"); params.append(from_dt)
    if to_dt:
        wc.append("changed_at <= ?"); params.append(to_dt)
    if status:
        wc.append("new_status = ?"); params.append(status)
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    total = conn.execute(f"SELECT COUNT(*) FROM status_history {where}", params).fetchone()[0]
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    offset = (page - 1) * per_page
    rows = conn.execute(f"SELECT * FROM status_history {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                        params + [per_page, offset]).fetchall()
    conn.close()
    pages = max(1, (total + per_page - 1) // per_page)
    return {"items": [dict(x) for x in rows], "total": total,
            "page": page, "pages": pages, "per_page": per_page}

@app.get("/api/export-history")
async def export_history(from_dt: str = "", to_dt: str = "", status: str = "", user=Depends(verify_token)):
    """Export the filtered Change History to .xlsx."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    conn = get_db()
    wc, params = [], []
    if from_dt:
        wc.append("changed_at >= ?"); params.append(from_dt)
    if to_dt:
        wc.append("changed_at <= ?"); params.append(to_dt)
    if status:
        wc.append("new_status = ?"); params.append(status)
    where = ("WHERE " + " AND ".join(wc)) if wc else ""
    rows = conn.execute(f"SELECT * FROM status_history {where} ORDER BY id DESC", params).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Change History"
    headers = ["#", "Reference No", "Student Name", "Old Status", "New Status", "Changed At"]
    ws.append(headers)
    for i, r in enumerate(rows, 1):
        ws.append([i, r["reference_no"], r["student_name"], r["old_status"],
                   r["new_status"], r["changed_at"]])
    fill = PatternFill("solid", fgColor="4F46E5")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF"); cell.fill = fill
    for idx, w in enumerate([5, 18, 26, 28, 28, 20], 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=nios_change_history.xlsx"})

@app.get("/api/run-logs")
async def get_run_logs(limit: int=50, user=Depends(verify_token)):
    conn = get_db()
    l = conn.execute("SELECT * FROM run_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(x) for x in l]

@app.post("/api/run-now")
async def run_now(background_tasks: BackgroundTasks, user=Depends(verify_token)):
    background_tasks.add_task(run_status_check, "all")
    return {"message": "Run triggered for all students!"}

@app.post("/api/cancel-run")
async def cancel_run(run_id: int = Form(...), user=Depends(verify_token)):
    conn = get_db()
    row = conn.execute("SELECT status FROM run_logs WHERE id=?", (run_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")
    if row["status"] != "running":
        conn.close()
        return {"message": f"Run already {row['status']}", "status": row["status"]}
    conn.execute("UPDATE run_logs SET status='cancelled' WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return {"message": "Run cancelled", "status": "cancelled"}

@app.get("/api/progress")
async def run_progress(user=Depends(verify_token)):
    """Live progress of the currently running check (for the progress bar)."""
    conn = get_db()
    row = conn.execute("SELECT id, group_type, run_at, progress_current, progress_total, "
                       "progress_changed, progress_same "
                       "FROM run_logs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {"running": False}
    cur = row["progress_current"] or 0
    tot = row["progress_total"] or 0
    pct = int(cur * 100 / tot) if tot else 0
    return {"running": True, "id": row["id"], "group_type": row["group_type"],
            "run_at": row["run_at"], "current": cur, "total": tot, "percent": pct,
            "changed": row["progress_changed"] or 0, "same": row["progress_same"] or 0,
            "remaining": max(0, tot - cur)}

GROUP_LABELS = {"regular": "On Demand + Stream 2", "public": "Public (April / October)"}

@app.get("/api/next-runs")
async def next_runs(user=Depends(verify_token)):
    """Seconds remaining until the next automatic run of each group."""
    now = datetime.now(timezone.utc)
    out = []
    for grp, jid in [("regular", "job_regular"), ("public", "job_public")]:
        job = scheduler.get_job(jid)
        secs = None
        if job and job.next_run_time:
            nrt = job.next_run_time
            if nrt.tzinfo is None:
                delta = (nrt - datetime.now()).total_seconds()
            else:
                delta = (nrt - now).total_seconds()
            secs = max(0, int(delta))
        out.append({"group": grp, "label": GROUP_LABELS.get(grp, grp), "seconds": secs})
    return {"runs": out}

@app.post("/api/upload-excel")
async def upload_excel(file: UploadFile = File(...), user=Depends(verify_token)):
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Only .xlsx files allowed")
    async with aiofiles.open(EXCEL_PATH, "wb") as f:
        content = await file.read()
        await f.write(content)

    # Parse the uploaded sheet to report counts + a preview. A student is a
    # "duplicate" if it already exists in the system (matched by reference OR
    # email OR name+mobile) OR appears more than once within this file. Only the
    # genuinely NEW students are counted as new.
    resp = {"message": f"Excel uploaded ({len(content)} bytes)", "filename": file.filename,
            "total": 0, "duplicates": 0, "unique": 0, "preview": []}
    try:
        from excel_handler import read_students_from_excel

        def _norm(v):
            return str(v or "").strip().lower()

        # Build sets of keys already in the system
        conn = get_db()
        existing_ref, existing_email, existing_nm, existing_enroll = set(), set(), set(), set()
        for r in conn.execute("SELECT reference_no, email, student_name, mobile, enrollment_no FROM student_status").fetchall():
            if r["reference_no"]:
                existing_ref.add(_norm(r["reference_no"]))
            if r["email"]:
                existing_email.add(_norm(r["email"]))
            if r["enrollment_no"]:
                existing_enroll.add(_norm(r["enrollment_no"]))
            if r["student_name"] and r["mobile"]:
                existing_nm.add(_norm(r["student_name"]) + "|" + _norm(r["mobile"]))
        conn.close()

        students = read_students_from_excel(EXCEL_PATH)
        seen_ref, seen_email, seen_nm, seen_enroll = set(), set(), set(), set()
        preview = []
        dups = 0
        for s in students:
            ref = _norm(s.get("reference_no"))
            email = _norm(s.get("email"))
            enroll = _norm(s.get("enrollment_no"))
            nm = (_norm(s.get("student_name")) + "|" + _norm(s.get("mobile"))) \
                if s.get("student_name") and s.get("mobile") else ""
            # duplicate if seen earlier in file OR already present in the DB
            is_dup = (
                (ref and (ref in seen_ref or ref in existing_ref)) or
                (email and (email in seen_email or email in existing_email)) or
                (enroll and (enroll in seen_enroll or enroll in existing_enroll)) or
                (nm and (nm in seen_nm or nm in existing_nm))
            )
            if ref: seen_ref.add(ref)
            if email: seen_email.add(email)
            if enroll: seen_enroll.add(enroll)
            if nm: seen_nm.add(nm)
            if is_dup:
                dups += 1
            preview.append({
                "student_name": s.get("student_name", ""),
                "reference_no": s.get("reference_no", ""),
                "enrollment_no": s.get("enrollment_no", ""),
                "email": s.get("email", ""),
                "class_level": s.get("class_level", ""),
                "session": s.get("session", ""),
                "dob": s.get("dob", ""),
                "mobile": s.get("mobile", ""),
                "dup": is_dup,
            })
        total = len(students)
        resp.update({"total": total, "duplicates": dups, "unique": total - dups,
                     "preview": preview[:2000]})
    except Exception as e:
        resp["parse_error"] = str(e)[:200]
    return resp

@app.get("/api/download-excel")
async def download_excel(user=Depends(verify_token)):
    if not os.path.exists(EXCEL_PATH):
        raise HTTPException(status_code=404, detail="Excel file not found")
    return FileResponse(EXCEL_PATH,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="nios_status_updated.xlsx")

@app.get("/api/sample-sheet")
async def sample_sheet(type: str = "regular", user=Depends(verify_token)):
    """Generate a ready-to-fill sample Excel so counsellors always know the format."""
    import io, openpyxl
    from openpyxl.utils import get_column_letter
    from fastapi import Response
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Students"
    headers = ["STUDENT NAME", "MOBILE NO", "CLASS", "REFERENCE NUMBER", "Enrol No", "Email",
               "Date of Birth", "ADMISSION SESSION", "ADMISSION STATUS", "REMARKS",
               "DOWNLOAD ID CARD", "DOWNLOAD APPLICATION FORM", "HALL TICKET"]
    ws.append(headers)
    if type == "syc":
        rows = [
            ["AYUSH KUMAR", "9876543210", "12TH", "", "220004253089", "", "19-06-2006", "SYC"],
            ["PETER RANA", "7428240153", "12TH", "", "50258253204", "peter@example.com", "17-05-2001", "SYC"],
        ]
    else:
        rows = [
            ["SABBA NOOR", "6205148930", "12TH", "D1026300062", "", "", "05-02-2010", "On Demand"],
            ["DEVRAJ JAT", "7737485139", "10TH", "B0926200020", "", "", "01-07-2004", "Stream 2"],
            ["SANA PARWEEN", "9523534252", "12TH", "A1026300040", "", "sana@example.com", "28-02-2009", "April"],
        ]
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(h) + 2)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fn = "MVS_sample_syc.xlsx" if type == "syc" else "MVS_sample_regular.xlsx"
    return Response(content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'})

@app.get("/api/intervals")
async def get_intervals(user=Depends(verify_token)):
    return {"regular": int(get_setting("interval_regular", "6")),
            "public": int(get_setting("interval_public", "12"))}

@app.post("/api/intervals")
async def set_intervals(body: dict, user=Depends(verify_token)):
    reg = int(body.get("regular", 6))
    pub = int(body.get("public", 12))
    if not (1 <= reg <= 72) or not (1 <= pub <= 72):
        raise HTTPException(status_code=400, detail="Hours must be 1-72")
    set_setting("interval_regular", reg)
    set_setting("interval_public", pub)
    reschedule_jobs()
    return {"message": f"Regular: {reg}h, Public: {pub}h", "regular": reg, "public": pub}

@app.get("/api/debug-login")
async def debug_login_endpoint(ref: str, dob: str, action: str = "", user=Depends(verify_token)):
    """Phase 2 debug: login with a confirmed student & discover download links."""
    try:
        from nios_login import debug_login
        result = debug_login(ref, dob, action or None)
        return result
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/download-doc")
async def download_doc(ref: str, dob: str, kind: str, user=Depends(verify_token)):
    """Login as the student and return their document (PDF or print-ready HTML)."""
    from fastapi import Response
    from nios_login import fetch_document
    content, ctype, filename = fetch_document(ref, dob, kind)
    if content is None:
        raise HTTPException(status_code=404, detail=ctype)
    # PDF -> attachment download; HTML -> inline (open in tab to print)
    if "pdf" in ctype:
        disp = f'attachment; filename="{filename}"'
    else:
        disp = "inline"
    return Response(content=content, media_type=ctype, headers={"Content-Disposition": disp})

@app.get("/api/syc")
async def get_syc(page: int = 1, per_page: int = 20, search: str = "", user=Depends(verify_token)):
    """List SYC students (session contains SYC). No status check is done for these."""
    conn = get_db()
    where = "WHERE (session LIKE '%syc%' OR current_status='SYC')"
    params = []
    if search:
        where += " AND (student_name LIKE ? OR enrollment_no LIKE ? OR mobile LIKE ? OR reference_no LIKE ?)"
        like = f"%{search}%"
        params += [like, like, like, like]
    total = conn.execute(f"SELECT COUNT(*) c FROM student_status {where}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM student_status {where} ORDER BY student_name LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page]).fetchall()
    conn.close()
    return {"students": [dict(r) for r in rows], "total": total, "page": page,
            "pages": max(1, (total + per_page - 1) // per_page)}

@app.get("/api/syc-doc")
async def syc_doc(row_key: str, kind: str = "hall_ticket", user=Depends(verify_token)):
    """Fetch a SYC student's document (enrollment-login aware) for the counsellor."""
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        raise HTTPException(status_code=404, detail=err)
    return _serve_doc(*res)

@app.post("/api/syc-delete")
async def syc_delete(row_key: str, user=Depends(verify_token)):
    """Delete a SINGLE SYC student only. Guarded so it can never touch a
    non-SYC (On Demand / Stream 2 / Public) student."""
    conn = get_db()
    row = conn.execute("SELECT session, current_status FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Student not found")
    is_syc = ("syc" in (row["session"] or "").lower()) or (row["current_status"] == "SYC")
    if not is_syc:
        conn.close()
        raise HTTPException(status_code=400, detail="Not a SYC student — refused")
    conn.execute("DELETE FROM student_status WHERE row_key=?", (row_key,))
    conn.execute("DELETE FROM short_links WHERE row_key=?", (row_key,))
    conn.commit()
    conn.close()
    return {"ok": True}
    """Inspect a document page's structure (Phase 2 debug)."""
    try:
        from nios_login import debug_doc
        return debug_doc(ref, dob, kind)
    except Exception as e:
        return {"error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp (AiSensy) — settings, test, manual resend
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/wa-settings")
async def wa_settings_get(user=Depends(verify_token)):
    import whatsapp
    return {
        "enabled": get_setting("wa_enabled", "0") == "1",
        "configured": whatsapp.is_configured(),
        "campaigns": {
            "ondemand": whatsapp.campaign_for("ondemand"),
            "stream2": whatsapp.campaign_for("stream2"),
            "public": whatsapp.campaign_for("public"),
            "syc": whatsapp.campaign_for("syc"),
        },
    }

@app.post("/api/wa-settings")
async def wa_settings_set(body: dict, user=Depends(verify_token)):
    set_setting("wa_enabled", "1" if body.get("enabled") else "0")
    return {"message": "saved", "enabled": bool(body.get("enabled"))}

@app.post("/api/wa-test")
async def wa_test(body: dict, user=Depends(verify_token)):
    """Send a test message of a chosen template group to any number."""
    import whatsapp
    number = body.get("number", "")
    group = body.get("group", "ondemand")
    name = body.get("name", "") or "Test Student"
    ok, info = whatsapp.send_test(number, name, group)
    return {"ok": ok, "info": info}

@app.post("/api/wa-resend")
async def wa_resend(body: dict, user=Depends(verify_token)):
    """Manually (re)send documents to one student by row_key (session-aware)."""
    import whatsapp
    row_key = body.get("row_key", "")
    conn = get_db()
    row = conn.execute("SELECT row_key, student_name, mobile, session, reference_no, dob "
                       "FROM student_status WHERE row_key=?", (row_key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="student not found")
    ok, info = whatsapp.send_for_student(dict(row))
    if ok:
        conn.execute("UPDATE student_status SET whatsapp_sent=1, whatsapp_info=? WHERE row_key=?",
                     (str(info)[:180], row_key))
    else:
        conn.execute("UPDATE student_status SET whatsapp_info=? WHERE row_key=?",
                     (str(info)[:180], row_key))
    conn.commit()
    conn.close()
    return {"ok": ok, "info": info}

@app.get("/api/debug-idcard")
async def debug_idcard(ref: str, dob: str, user=Depends(verify_token)):
    """Show the ID card's visible text + best-effort Regional Centre address
    (used to finalise the Stream 2 address parser)."""
    try:
        from nios_login import debug_idcard_text
        return debug_idcard_text(ref, dob)
    except Exception as e:
        return {"error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
# Danger zone — wipe all student data for a fresh upload (keeps settings)
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/reset-data")
async def reset_data(body: dict, user=Depends(verify_token)):
    conn = get_db()
    conn.execute("DELETE FROM student_status")
    conn.execute("DELETE FROM status_history")
    conn.execute("DELETE FROM run_logs")
    try:
        conn.execute("DELETE FROM short_links")
    except Exception:
        pass
    conn.commit()
    conn.close()
    removed = False
    try:
        if os.path.exists(EXCEL_PATH):
            os.remove(EXCEL_PATH)
            removed = True
    except Exception:
        pass
    logger.info("All student data cleared via reset endpoint")
    return {"message": "All data has been cleared.", "excel_removed": removed}

# ─────────────────────────────────────────────────────────────────────────────
# Public document links (student opens WITHOUT portal login; token-signed)
# ─────────────────────────────────────────────────────────────────────────────
def _allowed_kinds(session):
    s = (session or "").lower()
    if "stream 2" in s or "stream2" in s or "stream-2" in s:
        return [("id_card", "ID Card"), ("app_form", "Application Form")]
    if "on demand" in s or "ondemand" in s or "on-demand" in s:
        return [("id_card", "ID Card"), ("app_form", "Application Form"), ("hall_ticket", "Hall Ticket")]
    if "april" in s or "october" in s or "public" in s:
        return [("id_card", "ID Card")]
    return [("id_card", "ID Card"), ("app_form", "Application Form"), ("hall_ticket", "Hall Ticket")]

DOC_PAGE_TPL = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Your NIOS Documents — MVS Foundation</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{background:linear-gradient(135deg,#4F46E5,#7C3AED);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:18px}
.card{background:#fff;border-radius:20px;max-width:440px;width:100%;padding:30px 26px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.logo{width:58px;height:58px;border-radius:14px;background:linear-gradient(135deg,#4F46E5,#7C3AED);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px;margin:0 auto 14px}
h1{font-size:19px;text-align:center;color:#0F172A}
.sub{text-align:center;color:#64748B;font-size:13px;margin:6px 0 22px}
.name{text-align:center;font-weight:700;color:#4F46E5;font-size:16px;margin-bottom:2px}
.btns{display:flex;flex-direction:column;gap:12px;margin-top:8px}
.docbtn{display:flex;align-items:center;gap:12px;padding:15px 18px;border:2px solid #E2E8F0;border-radius:14px;
  color:#0F172A;font-weight:600;font-size:15px;cursor:pointer;background:#F8FAFC;transition:.15s;text-decoration:none}
.docbtn:hover{border-color:#4F46E5;background:#EEF2FF}
.docbtn .ico{width:34px;height:34px;border-radius:9px;background:#4F46E5;color:#fff;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:17px}
.note{margin-top:18px;background:#FEF9C3;border:1px solid #FDE68A;border-radius:12px;padding:11px 14px;font-size:12.5px;color:#854D0E}
.foot{text-align:center;color:#94A3B8;font-size:11.5px;margin-top:18px}
.busy{opacity:.55;pointer-events:none}
</style></head><body>
<div class="card">
  <img src="/logo.png" alt="MVS Foundation" style="width:60px;height:60px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 14px">
  <h1>Your NIOS Documents</h1>
  <div class="sub">Admission Confirmed</div>
  <div class="name">__NAME__</div>
  <div class="sub" style="margin-top:0">Ref: __REF__</div>
  <div class="btns">__BUTTONS__</div>
  <div class="note">&#128161; It may take a few seconds to open each document (securely fetched from NIOS). Once it opens, tap <b>"Save as PDF / Print"</b> at the top to save it.</div>
  <div class="foot">MVS Foundation &middot; NIOS Open Schooling</div>
</div>
<script>
function openDoc(el,url){ if(el.dataset.b==='1')return; el.dataset.b='1';
  var t=el.querySelector('.lbl'); var o=t.textContent; t.textContent='Opening...';
  el.classList.add('busy'); window.open(url,'_blank');
  setTimeout(function(){t.textContent=o;el.classList.remove('busy');el.dataset.b='';},16000); }
</script>
</body></html>"""

@app.get("/d/{token}", response_class=HTMLResponse)
async def public_doc_page(token: str):
    from links import verify_doc_token
    row_key = verify_doc_token(token)
    if not row_key:
        return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>Invalid or broken link</h3>", status_code=404)
    conn = get_db()
    row = conn.execute("SELECT student_name, session, reference_no FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>Student not found</h3>", status_code=404)
    btns = ""
    for kind, label in _allowed_kinds(row["session"]):
        url = f"/d/{token}/{kind}"
        btns += (f'<a class="docbtn" onclick="openDoc(this,\'{url}\')">'
                 f'<span class="ico">&#128196;</span><span class="lbl">{label}</span></a>')
    html = (DOC_PAGE_TPL
            .replace("__NAME__", (row["student_name"] or "Student"))
            .replace("__REF__", (row["reference_no"] or "—"))
            .replace("__BUTTONS__", btns))
    return HTMLResponse(html)

@app.get("/d/{token}/{kind}")
async def public_doc_file(token: str, kind: str):
    from fastapi import Response
    from links import verify_doc_token
    from nios_login import fetch_document
    row_key = verify_doc_token(token)
    if not row_key:
        raise HTTPException(status_code=404, detail="invalid link")
    conn = get_db()
    row = conn.execute("SELECT reference_no, dob FROM student_status WHERE row_key=?",
                       (row_key,)).fetchone()
    conn.close()
    if not row or not row["reference_no"]:
        raise HTTPException(status_code=404, detail="student not found")
    content, ctype, filename = fetch_document(row["reference_no"], row["dob"], kind)
    if content is None:
        raise HTTPException(status_code=404, detail=ctype)
    disp = f'attachment; filename="{filename}"' if "pdf" in ctype else "inline"
    return Response(content=content, media_type=ctype, headers={"Content-Disposition": disp})

DOC_LABELS = {"id_card": "ID Card", "app_form": "Application / Registration Form",
              "hall_ticket": "Hall Ticket"}

LOADING_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Preparing your document — MVS Foundation</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
body{background:linear-gradient(135deg,#4F46E5,#7C3AED);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.box{background:#fff;border-radius:20px;max-width:430px;width:100%;padding:40px 30px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.spinner{width:52px;height:52px;border:5px solid #EEF2FF;border-top-color:#4F46E5;border-radius:50%;margin:0 auto 22px;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
h1{font-size:19px;color:#0F172A;margin-bottom:8px}
.doc{color:#4F46E5;font-weight:700}
p{color:#64748B;font-size:14px;line-height:1.6;margin-top:6px}
.warn{margin-top:22px;background:#FEF3C7;border:1px solid #FDE68A;border-radius:12px;padding:12px 14px;color:#92400E;font-size:13px;font-weight:600}
.err{display:none;margin-top:18px;color:#DC2626;font-size:14px}
.retry{display:none;margin-top:14px;padding:11px 26px;border:none;border-radius:10px;background:#4F46E5;color:#fff;font-weight:600;font-size:15px;cursor:pointer}
</style></head><body>
<div class="box">
  <img src="/logo.png" alt="MVS Foundation" style="width:60px;height:60px;border-radius:50%;object-fit:cover;display:block;margin:0 auto 14px">
  <div class="spinner" id="sp"></div>
  <h1>Preparing your <span class="doc">__LABEL__</span></h1>
  <p>Please wait a few seconds while we securely fetch your document from NIOS.</p>
  <div class="warn">&#9203; Please do not refresh this page or press the back button.</div>
  <div class="err" id="err">Sorry, the document could not be loaded. Please try again.</div>
  <button class="retry" id="retry" onclick="loadDoc()">Try Again</button>
</div>
<script>
function loadDoc(){
  document.getElementById('err').style.display='none';
  document.getElementById('retry').style.display='none';
  document.getElementById('sp').style.display='block';
  fetch('__PREP__').then(function(r){ return r.json(); })
  .then(function(d){
    if(d && d.ok){ window.location.replace('__VIEW__'); }
    else { throw new Error((d && d.error) || 'failed'); }
  }).catch(function(e){
    document.getElementById('sp').style.display='none';
    document.getElementById('err').style.display='block';
    document.getElementById('retry').style.display='inline-block';
  });
}
loadDoc();
</script></body></html>"""

# Short-lived in-memory cache so the document is fetched from NIOS once (during the
# loader's "prepare" step) and then served as a REAL page navigation at "view".
# Real navigation (not document.write) is what makes the browser's Save-as-PDF /
# Print work reliably on every device, including phones.
import time as _time
_DOC_CACHE = {}
_DOC_TTL = 600  # 10 minutes

def _cache_put(key, tup):
    _DOC_CACHE[key] = (_time.time(),) + tuple(tup)
    if len(_DOC_CACHE) > 150:
        now = _time.time()
        for k in [k for k, v in list(_DOC_CACHE.items()) if now - v[0] > _DOC_TTL]:
            _DOC_CACHE.pop(k, None)

def _cache_get(key):
    v = _DOC_CACHE.get(key)
    if not v:
        return None
    if _time.time() - v[0] > _DOC_TTL:
        _DOC_CACHE.pop(key, None)
        return None
    return v[1], v[2], v[3]

def _fetch_doc_for(row_key, kind):
    """Return ((content, ctype, filename), None) or (None, error_message).
    Never raises — a NIOS/network hiccup becomes a clean error so the student
    sees a 'Try Again' instead of a server crash."""
    try:
        from nios_login import fetch_document
        conn = get_db()
        row = conn.execute("SELECT reference_no, enrollment_no, dob FROM student_status WHERE row_key=?",
                           (row_key,)).fetchone()
        conn.close()
        ref = (row["reference_no"] if row else "") or ""
        enroll = (row["enrollment_no"] if row else "") or ""
        if not row or (not ref and not enroll):
            return None, "student not found"
        content, ctype, filename = fetch_document(ref, row["dob"], kind, enrollment_no=enroll)
        if content is None:
            return None, (ctype or "could not load document")
        return (content, ctype, filename), None
    except Exception as e:
        logger.warning(f"doc fetch failed for {kind}: {e}")
        return None, "could not load document"

def _serve_doc(content, ctype, filename):
    from fastapi import Response
    # inline -> the browser renders it as a normal page so Print / Save-as-PDF works
    return Response(content=content, media_type=ctype,
                    headers={"Content-Disposition": "inline"})

def _loader_html(kind, prep_url, view_url):
    label = DOC_LABELS.get(kind, "Document")
    return (LOADING_PAGE.replace("__LABEL__", label)
            .replace("__PREP__", prep_url).replace("__VIEW__", view_url))

def _invalid_link_html():
    return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:60px'>"
                        "This link is invalid or has expired.</h3>", status_code=404)

# ── Long signed links: /doc/{token} ──
@app.get("/doc/{token}", response_class=HTMLResponse)
async def public_single_doc(token: str):
    from links import verify_doc_link
    row_key, kind = verify_doc_link(token)
    if not row_key:
        return _invalid_link_html()
    return HTMLResponse(_loader_html(kind, f"/doc/{token}/prepare", f"/doc/{token}/view"))

@app.get("/doc/{token}/prepare")
async def public_doc_prepare(token: str):
    from links import verify_doc_link
    row_key, kind = verify_doc_link(token)
    if not row_key:
        return {"ok": False, "error": "invalid link"}
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        return {"ok": False, "error": err}
    _cache_put("doc:" + token, res)
    return {"ok": True}

@app.get("/doc/{token}/view")
async def public_doc_view(token: str):
    cached = _cache_get("doc:" + token)
    if not cached:
        from links import verify_doc_link
        row_key, kind = verify_doc_link(token)
        if not row_key:
            raise HTTPException(status_code=404, detail="invalid link")
        res, err = _fetch_doc_for(row_key, kind)
        if err:
            raise HTTPException(status_code=404, detail=err)
        cached = res
    return _serve_doc(*cached)

# ── Short links: /s/{code} ──
@app.get("/s/{code}", response_class=HTMLResponse)
async def short_doc(code: str):
    from shortlinks import resolve_short
    row_key, kind = resolve_short(code)
    if not row_key:
        return _invalid_link_html()
    return HTMLResponse(_loader_html(kind, f"/s/{code}/prepare", f"/s/{code}/view"))

@app.get("/s/{code}/prepare")
async def short_doc_prepare(code: str):
    from shortlinks import resolve_short
    row_key, kind = resolve_short(code)
    if not row_key:
        return {"ok": False, "error": "invalid link"}
    res, err = _fetch_doc_for(row_key, kind)
    if err:
        return {"ok": False, "error": err}
    _cache_put("s:" + code, res)
    return {"ok": True}

@app.get("/s/{code}/view")
async def short_doc_view(code: str):
    cached = _cache_get("s:" + code)
    if not cached:
        from shortlinks import resolve_short
        row_key, kind = resolve_short(code)
        if not row_key:
            raise HTTPException(status_code=404, detail="invalid link")
        res, err = _fetch_doc_for(row_key, kind)
        if err:
            raise HTTPException(status_code=404, detail=err)
        cached = res
    return _serve_doc(*cached)

@app.get("/logo.png")
async def logo_png():
    """MVS Foundation logo (embedded, served for portal + student pages)."""
    import base64
    from fastapi import Response
    try:
        from assets import LOGO_B64
        return Response(content=base64.b64decode(LOGO_B64), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        raise HTTPException(status_code=404, detail="logo not found")
