# =============================================================================
# TEACHER PERFORMANCE MANAGEMENT SYSTEM — PHASE 2: SCORING ENGINE
# -----------------------------------------------------------------------------
# 8-component Performance Score (out of 100), REAL data se, month-scoped,
# workload-normalized, multi-subject weighted, overachievement-capped,
# config-driven (perf_config). Koi fake data nahi. Jis category ka data hi nahi
# (N/A) uska weight baaki components me redistribute hota hai — taaki teacher ko
# na-applicable category ke liye penalty na mile (spec Part 14 & 15).
#
# Design: pure `score_from_metrics(metrics, cfg, team)` (bina DB — testable) +
# `gather_metrics(db, tp, month)` (real DB data) + `compute(db, month)` (sab
# teachers, ranked). PM = Admin (koi alag role nahi).
# =============================================================================
from datetime import datetime, date, timedelta
import perf_config as _pc

COMPONENTS = ["teaching", "content", "targets", "student_support",
              "tests", "task_discipline", "consistency", "video_initiative"]


# ---------------------------------------------------------------- month helpers
def month_bounds(month=""):
    """'YYYY-MM' -> (dt0, dt1) [start inclusive, next-month start exclusive].
    Khaali -> current IST month."""
    try:
        from models import ist_today
        today = ist_today()
    except Exception:
        today = date.today()
    if month and len(month) >= 7:
        y, m = int(month[:4]), int(month[5:7])
    else:
        y, m = today.year, today.month
    dt0 = datetime(y, m, 1)
    dt1 = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return dt0, dt1, "%04d-%02d" % (y, m)


# ---------------------------------------------------------------- overachievement cap
def cap_pct(pct, cfg):
    oc = cfg.get("overachievement", {})
    base = float(oc.get("base", 100))
    c1 = float(oc.get("cap_101_120", 105))
    c2 = float(oc.get("cap_over_120", 110))
    if pct <= base:
        return round(pct, 1)
    if pct <= 120:
        return round(min(pct, c1), 1)
    return round(min(pct, c2), 1)


def _ratio_pct(done, target, cfg, allow_over=True):
    """done/target %. target 0 -> None (N/A). Cap applied when allow_over."""
    if not target or target <= 0:
        return None
    pct = 100.0 * done / target
    return cap_pct(pct, cfg) if allow_over else round(min(100.0, pct), 1)


# ---------------------------------------------------------------- PURE SCORING CORE
def score_from_metrics(m, cfg, team=None):
    """m = ek teacher ke real metrics (gather_metrics se). cfg = perf_config.
    team = {'avg_workload_units': x} eligibility ke liye. Returns full breakdown.
    Har component ka raw% (0..110) ya None (N/A). Overall = applicable components
    ka weighted average (N/A ka weight redistribute)."""
    w = cfg.get("component_weights", {})
    raw = {}   # component -> % or None

    # 1) TEACHING — reported scheduled classes ka on-time (delayed = aadha)
    ot, dl = m.get("lect_ontime", 0), m.get("lect_delayed", 0)
    done = ot + dl
    raw["teaching"] = round(100.0 * (ot + 0.5 * dl) / done, 1) if done > 0 else None

    # 2) CONTENT — video + short + project (per-video) production vs target
    craw = _blend_target(m.get("content_items", []), cfg)
    raw["content"] = craw

    # 3) TARGETS — SAARE monthly targets ka achievement (videos/shorts/live/tests/dpp/custom)
    traw = _blend_target(m.get("target_items", []), cfg)
    raw["targets"] = traw

    # 4) STUDENT SUPPORT — doubts resolved ratio (pending penalty). Koi doubt nahi -> N/A
    rec, res = m.get("doubts_received", 0), m.get("doubts_resolved", 0)
    raw["student_support"] = round(100.0 * res / rec, 1) if rec > 0 else None

    # 5) TESTS — tests done vs target (agar target/assigned ho)
    raw["tests"] = _ratio_pct(m.get("tests_done", 0), m.get("tests_target", 0), cfg) \
        if m.get("tests_target", 0) else (100.0 if m.get("tests_done", 0) > 0 else None)

    # 6) TASK DISCIPLINE — task timeliness (not_completed = 0 credit)
    to, td, tn = m.get("task_ontime", 0), m.get("task_delayed", 0), m.get("task_not_completed", 0)
    tot = to + td + tn
    raw["task_discipline"] = round(100.0 * (to + 0.5 * td) / tot, 1) if tot > 0 else None

    # 7) CONSISTENCY — on-time ratio + present-day regularity (proxy). Koi activity nahi -> N/A
    craw2 = _consistency_pct(m)
    raw["consistency"] = craw2

    # 8) VIDEO INITIATIVE — reward-based, capped. Koi approved proposal nahi -> N/A (bonus, penalty nahi)
    raw["video_initiative"] = _initiative_pct(m, cfg)

    # ---- component scores (raw%/100 * weight), N/A -> excluded ----
    comp = {}
    applic_w = 0.0
    weighted_sum = 0.0
    for c in COMPONENTS:
        wt = float(w.get(c, 0))
        pv = raw.get(c)
        if pv is None:
            comp[c] = {"raw": None, "weight": wt, "score": None, "na": True}
        else:
            sc = round(min(pv, 110) / 100.0 * wt, 2)
            comp[c] = {"raw": pv, "weight": wt, "score": sc, "na": False}
            applic_w += wt
            weighted_sum += (min(pv, 110) / 100.0) * wt
    overall = round(100.0 * weighted_sum / applic_w, 1) if applic_w > 0 else 0.0

    # ---- eligibility (min workload) ----
    my_units = m.get("workload_units_assigned", 0)
    limited = False
    if team and team.get("avg_workload_units"):
        thr = float(cfg.get("min_workload_fraction", 0.6)) * team["avg_workload_units"]
        limited = my_units < thr

    # ---- workload level (team-avg ke % me) ----
    wl_level, wl_pct = _workload_level(my_units, team, cfg)

    return {
        "overall": overall,
        "components": comp,
        "raw": raw,
        "workload_units_assigned": round(my_units, 1),
        "workload_units_completed": round(m.get("workload_units_completed", 0), 1),
        "workload_level": wl_level,
        "workload_pct": wl_pct,
        "limited_workload": limited,
        "video_initiative": {
            "proposed": m.get("prop_proposed", 0),
            "approved": m.get("prop_approved", 0),
            "in_production": m.get("prop_in_production", 0),
            "published": m.get("prop_published", 0),
            "reward_points": m.get("reward_points", 0),
        },
        "consistency_streak": m.get("streak_days", 0),
    }


def _blend_target(items, cfg):
    """items = [{'done':n,'target':t,'units':u}] -> workload-weighted capped % ya None.
    Sirf target>0 waale items count hote hain (jinka target set hai)."""
    tw = dn = 0.0
    for it in items or []:
        t = float(it.get("target", 0) or 0)
        if t <= 0:
            continue
        u = float(it.get("units", 1) or 1)
        pct = cap_pct(100.0 * float(it.get("done", 0)) / t, cfg)
        tw += u
        dn += (pct / 100.0) * u
    if tw <= 0:
        # koi target set nahi -> agar kuch banaya hai to 100 (non-penalizing), warna N/A
        any_done = any(float(it.get("done", 0) or 0) > 0 for it in (items or []))
        return 100.0 if any_done else None
    return round(100.0 * dn / tw, 1)


def _consistency_pct(m):
    parts = []
    tot = m.get("task_ontime", 0) + m.get("task_delayed", 0) + m.get("task_not_completed", 0)
    if tot > 0:
        parts.append(100.0 * m.get("task_ontime", 0) / tot)
    lect = m.get("lect_ontime", 0) + m.get("lect_delayed", 0)
    if lect > 0:
        parts.append(100.0 * m.get("lect_ontime", 0) / lect)
    # present-day regularity (present / working-day-ish target ~ 24)
    pres = m.get("present_days", 0)
    if pres > 0:
        parts.append(min(100.0, 100.0 * pres / 24.0))
    if not parts:
        return None
    return round(sum(parts) / len(parts), 1)


def _initiative_pct(m, cfg):
    """Reward points ko % me — quality/approval-based (raw count nahi). 10 reward pts = 100%.
    Koi approved proposal nahi -> None (bonus, penalty nahi)."""
    if m.get("prop_approved", 0) <= 0 and m.get("reward_points", 0) <= 0:
        return None
    pts = float(m.get("reward_points", 0))
    return round(min(100.0, pts / 10.0 * 100.0), 1)


def _workload_level(units, team, cfg):
    lv = cfg.get("workload_levels", {})
    if not team or not team.get("avg_workload_units"):
        return "Standard", 100
    pct = round(100.0 * units / team["avg_workload_units"]) if team["avg_workload_units"] else 100
    if pct >= lv.get("very_high_above", 150):
        return "Very High", pct
    if pct >= lv.get("high_above", 120):
        return "High", pct
    if pct < lv.get("low_below", 60):
        return "Low", pct
    return "Standard", pct


# ---------------------------------------------------------------- REAL DATA GATHER
def gather_metrics(db, tp, dt0, dt1, cfg):
    """Ek teacher ke month-scoped REAL metrics. Sab defensive (missing data = 0/None)."""
    from models import (User, Lecture, TimetableEntry, Doubt, DoubtStatus,
                        DppPack, Exam, VideoTask, TeacherAttendance)
    from sqlalchemy.orm import defer
    aw = cfg.get("activity_workload", {})
    m = {}

    # ---- Lectures (teaching / on-time) ----
    ot = dl = 0
    try:
        lecs = db.query(Lecture).options(defer(Lecture.pdf_b64), defer(Lecture.dpp_b64)).filter(
            Lecture.teacher_id == tp.id,
            Lecture.lecture_date >= dt0.date(), Lecture.lecture_date < dt1.date()).all()
        for l in lecs:
            te = db.query(TimetableEntry).filter(TimetableEntry.id == l.timetable_entry_id).first() \
                if getattr(l, "timetable_entry_id", None) else None
            if not te or not te.entry_date:
                continue
            cd = l.lecture_date or (te.completed_at.date() if te.completed_at else None)
            if cd and cd > te.entry_date:
                dl += 1
            else:
                ot += 1
    except Exception:
        pass
    m["lect_ontime"], m["lect_delayed"] = ot, dl

    # ---- DPP (content/target) ----
    try:
        dpp_done = db.query(DppPack).filter(DppPack.teacher_id == tp.id,
                                            DppPack.created_at >= dt0,
                                            DppPack.created_at < dt1).count()
    except Exception:
        dpp_done = 0
    m["dpp_done"] = dpp_done

    # ---- Tests ----
    try:
        tests_done = db.query(Exam).filter(Exam.teacher_id == tp.id, Exam.is_active == True,
                                           Exam.created_at >= dt0, Exam.created_at < dt1).count()
    except Exception:
        tests_done = 0
    m["tests_done"] = tests_done

    # ---- Video targets (videos/shorts/live/tests) via existing REAL engine ----
    vt = {"videos": {"assigned": 0, "done": 0, "target": 0},
          "shorts": {"assigned": 0, "done": 0, "target": 0},
          "live":   {"assigned": 0, "done": 0, "target": 0},
          "tests":  {"assigned": 0, "done": 0, "target": 0}}
    proj_completed = 0
    try:
        from video_tasks import _vt_targets_for
        tg = _vt_targets_for(db, tp, dt0, dt1)
        for r in tg.get("rows", []):
            k = r.get("key")
            if k in vt:
                vt[k] = {"assigned": r.get("assigned", 0), "done": r.get("done", 0),
                         "target": r.get("target", 0)}
        m["_by_subject"] = tg.get("by_subject", [])
    except Exception:
        m["_by_subject"] = []
    # tests target/done: existing config target + real Exam count as done
    m["tests_target"] = vt["tests"]["target"]
    # projects completed (all chapters done) — per-video already counted in vt.videos
    try:
        from models import VideoTaskChapter
        projs = db.query(VideoTask).filter(VideoTask.teacher_id == tp.id,
                                           VideoTask.kind.in_(["one_shot", "rapid_revision", "project"]),
                                           VideoTask.created_at >= dt0, VideoTask.created_at < dt1).all()
        for p in projs:
            chs = db.query(VideoTaskChapter).filter(VideoTaskChapter.task_id == p.id).all()
            if chs and all((c.link or "").strip() or c.submitted_at for c in chs):
                proj_completed += 1
    except Exception:
        pass
    m["projects_completed"] = proj_completed

    # ---- Doubts (student support) ----
    try:
        rec = db.query(Doubt).filter(Doubt.teacher_id == tp.id,
                                     Doubt.created_at >= dt0, Doubt.created_at < dt1).count()
        pend = db.query(Doubt).filter(Doubt.teacher_id == tp.id, Doubt.status == DoubtStatus.pending,
                                      Doubt.created_at >= dt0, Doubt.created_at < dt1).count()
    except Exception:
        rec = pend = 0
    m["doubts_received"], m["doubts_resolved"] = rec, max(0, rec - pend)

    # ---- Tasks timeliness (on-time / delayed / not_completed) + proposals + rewards ----
    to = tdl = tnc = 0
    p_proposed = p_approved = p_inprod = p_published = 0
    reward = 0
    rw = cfg.get("video_rewards", {})
    try:
        tasks = db.query(VideoTask).filter(VideoTask.teacher_id == tp.id,
                                           VideoTask.created_at >= dt0, VideoTask.created_at < dt1).all()
        for t in tasks:
            if t.submitted_at and t.on_time is True:
                to += 1
            elif t.submitted_at and t.on_time is False:
                tdl += 1
            if getattr(t, "status", "") == "not_completed":
                tnc += 1
            # proposals (teacher-proposed)
            if str(getattr(t, "proposed_by", "") or "").lower() not in ("admin", "system", ""):
                p_proposed += 1
                if (t.proposal_ok or "") == "approved":
                    p_approved += 1
                    reward += float(rw.get("approved", 1))
                    st = getattr(t, "status", "")
                    if st in ("editing_soon", "editing_done"):
                        p_inprod += 1
                        reward += float(rw.get("in_production", 2))
                    if st == "uploaded":
                        p_published += 1
                        reward += float(rw.get("in_production", 2)) + float(rw.get("published", 2))
    except Exception:
        pass
    m["task_ontime"], m["task_delayed"], m["task_not_completed"] = to, tdl, tnc
    m["prop_proposed"], m["prop_approved"] = p_proposed, p_approved
    m["prop_in_production"], m["prop_published"] = p_inprod, p_published
    m["reward_points"] = round(reward, 1)

    # ---- Attendance (consistency) ----
    try:
        att = db.query(TeacherAttendance).filter(TeacherAttendance.teacher_id == tp.id,
                                                 TeacherAttendance.att_date >= dt0.date(),
                                                 TeacherAttendance.att_date < dt1.date()).all()
        m["present_days"] = len({a.att_date for a in att if a.punch_in})
    except Exception:
        m["present_days"] = 0
    m["streak_days"] = m["present_days"]   # proxy; real streak Phase-later

    # ---- content & target items (for _blend_target) with workload units ----
    m["content_items"] = [
        {"done": vt["videos"]["done"], "target": vt["videos"]["target"] or vt["videos"]["assigned"],
         "units": aw.get("one_shot", 5) if proj_completed else aw.get("long_video", 2)},
        {"done": vt["shorts"]["done"], "target": vt["shorts"]["target"] or vt["shorts"]["assigned"],
         "units": aw.get("short", 1)},
    ]
    # explicit per-teacher targets (agar admin ne set kiye) -> targets component me use
    _ttg = (get_teacher_targets(db, tp.id).get("targets", {}) or {})
    def _T(k, fallback):
        v = _ttg.get(k)
        return v if (v is not None and v != "") else fallback
    m["target_items"] = [
        {"done": vt["videos"]["done"], "target": _T("videos", vt["videos"]["target"]), "units": aw.get("long_video", 2)},
        {"done": vt["shorts"]["done"], "target": _T("shorts", vt["shorts"]["target"]), "units": aw.get("short", 1)},
        {"done": vt["live"]["done"],   "target": _T("live", vt["live"]["target"]),   "units": aw.get("youtube_live", 4)},
        {"done": tests_done,           "target": _T("tests", vt["tests"]["target"]), "units": aw.get("weekly_test", 3)},
        {"done": dpp_done,             "target": _T("dpp", m.get("dpp_target", 0)),   "units": aw.get("dpp", 1)},
        {"done": ot + dl,              "target": _T("classes", 0),                    "units": aw.get("class", 1)},
    ]

    # ---- workload units (assigned vs completed) for normalization + eligibility ----
    def _u(key, n):
        return aw.get(key, 1) * n
    assigned_units = (_u("long_video", vt["videos"]["assigned"]) + _u("short", vt["shorts"]["assigned"])
                      + _u("youtube_live", vt["live"]["assigned"]) + _u("weekly_test", vt["tests"]["target"])
                      + _u("dpp", dpp_done) + _u("class", ot + dl) + _u("doubt", rec)
                      + _u("task", len_or0(m, "task_ontime", "task_delayed", "task_not_completed")))
    completed_units = (_u("long_video", vt["videos"]["done"]) + _u("short", vt["shorts"]["done"])
                       + _u("youtube_live", vt["live"]["done"]) + _u("weekly_test", tests_done)
                       + _u("dpp", dpp_done) + _u("class", ot) + _u("doubt", m["doubts_resolved"])
                       + _u("task", to))
    m["workload_units_assigned"] = assigned_units
    m["workload_units_completed"] = completed_units
    return m


def len_or0(m, *keys):
    return sum(m.get(k, 0) for k in keys)


# ---------------------------------------------------------------- TOP-LEVEL COMPUTE
def _score_all(db, month, cfg):
    """Ek month ke saare teachers ki ranked rows (bina rank_change) + team avg."""
    from models import TeacherProfile, User
    dt0, dt1, mkey = month_bounds(month)
    metricmap = {}
    for tp in db.query(TeacherProfile).all():
        u = db.query(User).filter(User.id == tp.user_id).first()
        if not u:
            continue
        metricmap[tp.id] = (tp, u, gather_metrics(db, tp, dt0, dt1, cfg))
    units = [mm[2].get("workload_units_assigned", 0) for mm in metricmap.values()]
    avg_units = (sum(units) / len(units)) if units else 0
    team = {"avg_workload_units": avg_units}
    rows = []
    for tid, (tp, u, m) in metricmap.items():
        sc = score_from_metrics(m, cfg, team)
        rows.append({
            "teacher_id": tp.id, "name": u.name or "", "user_id": u.user_id or "",
            "photo": getattr(u, "photo_b64", None) or "",
            "subjects": tp.subjects or [], "batch": tp.batch or "",
            "score": sc["overall"], "components": sc["components"],
            "workload_level": sc["workload_level"], "workload_pct": sc["workload_pct"],
            "limited_workload": sc["limited_workload"],
            "video_initiative": sc["video_initiative"],
            "consistency_streak": sc["consistency_streak"],
        })
    rows.sort(key=lambda r: (-r["score"], (r["name"] or "").lower()))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows, round(avg_units, 1), mkey


def _prev_month_key(month):
    dt0, _, _ = month_bounds(month)
    py, pm = (dt0.year - 1, 12) if dt0.month == 1 else (dt0.year, dt0.month - 1)
    return "%04d-%02d" % (py, pm)


# ------------- PHASE 8: monthly history snapshots (freeze) -------------
def _snap_key(mkey):
    return "perf_snap_" + mkey


def _cur_month_key():
    _, _, mk = month_bounds("")
    return mk


def _load_snapshot(db, mkey):
    from models import AppSetting
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _snap_key(mkey)).first()
        if row and row.value:
            return _json.loads(row.value)
    except Exception:
        pass
    return None


def _save_snapshot(db, mkey, results):
    from models import AppSetting
    payload = {"month": mkey, "frozen_at": _ist(datetime.utcnow()) or "", "results": results}
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _snap_key(mkey)).first()
        if not row:
            db.add(AppSetting(key=_snap_key(mkey), value=_json.dumps(payload)))
        else:
            row.value = _json.dumps(payload)
        db.commit()
    except Exception:
        pass
    return payload


def _apply_rank_change(db, rows, prev_key):
    psnap = _load_snapshot(db, prev_key)
    prm = {r["teacher_id"]: r["rank"] for r in (psnap.get("results", []) if psnap else [])}
    for r in rows:
        pr = prm.get(r["teacher_id"])
        r["prev_rank"] = pr
        r["rank_change"] = (pr - r["rank"]) if pr else None   # + = upar chadha, None = NEW


def compute(db, month=""):
    """Month performance, ranked + rank_change. PAST months FROZEN (snapshot) rehte hain —
    weights badalne par purani ranking nahi badalti (spec Part 28). Current month live."""
    cfg = _pc.get_perf_config(db)
    _, _, mkey = month_bounds(month)
    prev_key = _prev_month_key(month)
    cur = _cur_month_key()

    if mkey != cur:
        # --- past month: agar snapshot hai to frozen return; warna live compute + auto-freeze ---
        snap = _load_snapshot(db, mkey)
        if snap and snap.get("results"):
            rows = [dict(r) for r in snap["results"]]
            _apply_rank_change(db, rows, prev_key)
            return {"month": mkey, "prev_month": prev_key, "frozen": True,
                    "frozen_at": snap.get("frozen_at", ""),
                    "weights": cfg.get("component_weights", {}),
                    "team_avg_workload_units": 0, "results": rows}
        rows, avg, _ = _score_all(db, month, cfg)
        _save_snapshot(db, mkey, rows)          # pehli baar access -> freeze
        _apply_rank_change(db, rows, prev_key)
        return {"month": mkey, "prev_month": prev_key, "frozen": True, "auto_frozen": True,
                "weights": cfg.get("component_weights", {}),
                "team_avg_workload_units": avg, "results": rows}

    # --- current month: live ---
    rows, avg, _ = _score_all(db, month, cfg)
    # previous (just-ended) month ko lazily freeze karo taaki rank-change accurate rahe
    if not _load_snapshot(db, prev_key):
        try:
            prows, _, _ = _score_all(db, prev_key, cfg)
            _save_snapshot(db, prev_key, prows)
        except Exception:
            pass
    _apply_rank_change(db, rows, prev_key)
    return {"month": mkey, "prev_month": prev_key, "frozen": False,
            "weights": cfg.get("component_weights", {}),
            "team_avg_workload_units": avg, "results": rows}


def freeze_month(db, month=""):
    """Admin: is month ke current scores ko snapshot me freeze/re-freeze kar do."""
    cfg = _pc.get_perf_config(db)
    rows, avg, mkey = _score_all(db, month, cfg)
    snap = _save_snapshot(db, mkey, rows)
    return {"ok": True, "month": mkey, "frozen_at": snap.get("frozen_at", ""), "count": len(rows)}


def history_months(db, back=8):
    """Current + pichhle N months ki list, konse frozen hain (UI dropdown ke liye)."""
    cur = _cur_month_key()
    y, m = int(cur[:4]), int(cur[5:7])
    out = []
    for i in range(back + 1):
        yy, mm = y, m - i
        while mm <= 0:
            mm += 12; yy -= 1
        mk = "%04d-%02d" % (yy, mm)
        snap = _load_snapshot(db, mk)
        out.append({"month": mk, "frozen": bool(snap), "current": (mk == cur),
                    "frozen_at": (snap.get("frozen_at", "") if snap else "")})
    return {"current": cur, "months": out}


def _areas(components):
    """Strong (top raw%) + improve (lowest applicable raw%) component keys."""
    applic = [(c, v["raw"]) for c, v in components.items()
              if not v.get("na") and v.get("raw") is not None]
    if not applic:
        return [], []
    hi = sorted(applic, key=lambda x: -x[1])
    lo = sorted(applic, key=lambda x: x[1])
    strong = [c for c, v in hi[:2] if v >= 70]
    improve = [c for c, v in lo[:3] if v < 85][:3]
    return strong, improve


def compute_one(db, tp, month=""):
    """Ek teacher ka full breakdown + next-rank gap + strong/improve (Why am I this rank)."""
    cfg = _pc.get_perf_config(db)
    dt0, dt1, mkey = month_bounds(month)
    board = compute(db, month)
    rows = board["results"]
    me = next((r for r in rows if r["teacher_id"] == tp.id), None)
    m = gather_metrics(db, tp, dt0, dt1, cfg)
    detail = score_from_metrics(m, cfg, {"avg_workload_units": board["team_avg_workload_units"]})
    nxt = None
    if me and me.get("rank", 1) > 1:
        ab = next((r for r in rows if r["rank"] == me["rank"] - 1), None)
        if ab:
            nxt = {"rank": ab["rank"], "name": ab["name"], "score": ab["score"],
                   "gap": round(ab["score"] - me["score"], 1)}
    strong, improve = _areas(detail["components"])
    return {"month": mkey, "me": me, "breakdown": detail, "metrics": m,
            "next": nxt, "strong": strong, "improve": improve,
            "total": len(rows), "weights": cfg.get("component_weights", {})}


# =============================================================================
# PHASE 3: MULTI-SUBJECT MONTHLY TARGETS (spec Part 5)
# Per-subject, per-activity real done/assigned + overall + vs-previous-month.
# =============================================================================
def _subject_targets(db, tp, dt0, dt1):
    """Ek teacher ke month ki per-subject activity breakdown (REAL data)."""
    from models import VideoTask, VideoTaskChapter, Exam, DppPack
    subs = list(tp.subjects or [])
    out = {}
    for s in subs:
        out[s] = {"videos": {"done": 0, "assigned": 0}, "shorts": {"done": 0, "assigned": 0},
                  "live": {"done": 0, "assigned": 0}, "tests": {"done": 0, "assigned": 0},
                  "dpp": {"done": 0, "assigned": 0},
                  "proposals": {"done": 0, "assigned": 0}}
    def _bucket(s):
        return out.setdefault(s, {"videos": {"done": 0, "assigned": 0}, "shorts": {"done": 0, "assigned": 0},
                                  "live": {"done": 0, "assigned": 0}, "tests": {"done": 0, "assigned": 0},
                                  "dpp": {"done": 0, "assigned": 0}, "proposals": {"done": 0, "assigned": 0}})

    # ---- Video tasks (videos/shorts/live + projects per-video) ----
    try:
        tasks = db.query(VideoTask).filter(
            VideoTask.teacher_id == tp.id,
            VideoTask.created_at >= dt0, VideoTask.created_at < dt1,
            VideoTask.proposal_ok != "pending", VideoTask.status != "rejected").all()
        for t in tasks:
            s = t.subject or "General"
            b = _bucket(s)
            kind = (t.kind or "normal")
            # proposals (teacher-proposed) count bhi (subject-wise)
            if str(getattr(t, "proposed_by", "") or "").lower() not in ("admin", "system", ""):
                b["proposals"]["assigned"] += 1
                if (t.proposal_ok or "") == "approved":
                    b["proposals"]["done"] += 1
            if kind in ("one_shot", "rapid_revision", "project"):
                chs = db.query(VideoTaskChapter).filter(VideoTaskChapter.task_id == t.id).all()
                if not chs:
                    b["videos"]["assigned"] += 1
                    continue
                for ch in chs:
                    b["videos"]["assigned"] += 1
                    has = bool((ch.link or "").strip()) or bool(ch.submitted_at)
                    if has and (getattr(ch, "vintage", "") or "") == "new":
                        b["videos"]["done"] += 1
                continue
            vt = (t.video_type or "").lower()
            if "short" in vt:
                cat = "shorts"
            elif "live" in vt or (getattr(t, "streaming", "") or "") == "live":
                cat = "live"
            else:
                cat = "videos"
            b[cat]["assigned"] += 1
            if bool(t.submitted_at) or t.status == "uploaded":
                b[cat]["done"] += 1
    except Exception:
        pass

    # ---- Tests (Exam per subject) ----
    try:
        for e in db.query(Exam).filter(Exam.teacher_id == tp.id, Exam.is_active == True,
                                       Exam.created_at >= dt0, Exam.created_at < dt1).all():
            b = _bucket(e.subject or "General")
            b["tests"]["done"] += 1
            b["tests"]["assigned"] += 1
    except Exception:
        pass

    # ---- DPP (DppPack per subject) ----
    try:
        for d in db.query(DppPack).filter(DppPack.teacher_id == tp.id,
                                          DppPack.created_at >= dt0, DppPack.created_at < dt1).all():
            b = _bucket(d.subject or "General")
            b["dpp"]["done"] += 1
            b["dpp"]["assigned"] += 1
    except Exception:
        pass
    return out


_ACT_LABELS = [("videos", "Videos"), ("shorts", "Shorts"), ("live", "YouTube Live"),
               ("tests", "Weekly Tests"), ("dpp", "DPP"), ("proposals", "Video Proposals")]
# in activities, in ka % subject-completion me count hota hai (real assigned/target waale):
_PCT_KEYS = ("videos", "shorts", "live")


def _subject_pct_rows(bucket, cfg):
    """Ek subject ke activity rows + subject-level done/total/pct."""
    rows = []
    tot_done = tot_total = 0
    for key, label in _ACT_LABELS:
        b = bucket.get(key, {"done": 0, "assigned": 0})
        done, assigned = b["done"], b["assigned"]
        rows.append({"key": key, "label": label, "done": done, "total": assigned,
                     "count_only": key in ("tests", "dpp", "proposals")})
        if key in _PCT_KEYS:
            tot_done += done
            tot_total += assigned
    pct = cap_pct(100.0 * tot_done / tot_total, cfg) if tot_total > 0 else (100.0 if tot_done > 0 else 0.0)
    return rows, tot_done, tot_total, round(pct, 1)


def monthly_targets(db, tp, month=""):
    """Multi-subject monthly performance (spec Part 5): overall + per-subject expandable
    breakdown + vs previous month. REAL data only."""
    cfg = _pc.get_perf_config(db)
    dt0, dt1, mkey = month_bounds(month)

    def _overall_for(d0, d1):
        by = _subject_targets(db, tp, d0, d1)
        subs = []
        odone = ototal = 0
        for s in sorted(by.keys()):
            rows, sd, st, pct = _subject_pct_rows(by[s], cfg)
            subs.append({"subject": s, "pct": pct, "done": sd, "total": st, "activities": rows})
            odone += sd
            ototal += st
        opct = cap_pct(100.0 * odone / ototal, cfg) if ototal > 0 else (100.0 if odone > 0 else 0.0)
        return subs, odone, ototal, round(opct, 1)

    subs, odone, ototal, opct = _overall_for(dt0, dt1)

    # previous month delta
    py, pm = (dt0.year - 1, 12) if dt0.month == 1 else (dt0.year, dt0.month - 1)
    from datetime import datetime as _dt
    pd0 = _dt(py, pm, 1)
    pd1 = dt0
    _, _, _, prev_pct = _overall_for(pd0, pd1)
    delta = round(opct - prev_pct, 1)

    return {"month": mkey, "prev_month": "%04d-%02d" % (py, pm),
            "overall_pct": opct, "overall_done": odone, "overall_total": ototal,
            "prev_pct": prev_pct, "delta_vs_prev": delta,
            "multi": len(subs) > 1, "subjects": subs}


# =============================================================================
# PHASE 4: VIDEO PROPOSAL — lifecycle + reward history + duplicate detect
# Sab DERIVED (idempotent) — koi reward double count nahi, koi nayi table nahi.
# PM = Admin. Reward points perf_config.video_rewards se.
# =============================================================================
import json as _json
import re as _re


def _norm_title(t):
    return _re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip()


def _tokens(t):
    return set(w for w in _norm_title(t).split() if len(w) > 2)


def _similar(a, b):
    """0..1 title similarity (token Jaccard)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 1.0 if _norm_title(a) == _norm_title(b) else 0.0
    inter = len(ta & tb)
    return inter / float(len(ta | tb))


def _hist_when(task, *stages):
    """status_history JSON se pehla matching stage ka time (string) — warna fallbacks."""
    try:
        hist = _json.loads(task.status_history or "[]")
        for h in hist:
            if str(h.get("s", "")) in stages:
                return h.get("at") or ""
    except Exception:
        pass
    return None


def _proposal_stage(t):
    """Lifecycle stage (derived): proposed/rejected/approved/in_production/published."""
    pok = (t.proposal_ok or "")
    if pok == "pending":
        return "proposed"
    if pok == "rejected":
        return "rejected"
    st = (t.status or "")
    if st == "uploaded" or (getattr(t, "youtube_url", "") or ""):
        return "published"
    if st in ("editing_soon", "editing_done"):
        return "in_production"
    # approved (task assigned/submitted etc.)
    return "approved"


def video_contribution(db, tp, month=""):
    """Teacher ki video-initiative dashboard (Part 12) + reward history (Part 30) +
    proposals list with lifecycle + duplicate flags. All-time (proposals span months)."""
    from models import VideoTask
    cfg = _pc.get_perf_config(db)
    rw = cfg.get("video_rewards", {})
    r_appr = float(rw.get("approved", 1))
    r_prod = float(rw.get("in_production", 2))
    r_pub = float(rw.get("published", 2))

    # teacher ke apne proposals (proposed_by teacher)
    q = db.query(VideoTask).filter(VideoTask.teacher_id == tp.id)
    tasks = [t for t in q.all()
             if str(getattr(t, "proposed_by", "") or "").lower() not in ("admin", "system", "")]
    tasks.sort(key=lambda t: (getattr(t, "created_at", None) or datetime.min))

    # duplicate detection (apne proposals me similar titles)
    dup_ids = set()
    for i in range(len(tasks)):
        for j in range(i):
            if _similar(tasks[i].title, tasks[j].title) >= 0.75:
                dup_ids.add(tasks[i].id)
                break

    counts = {"proposed": 0, "rejected": 0, "approved": 0, "in_production": 0, "published": 0}
    reward_total = 0.0
    reward_history = []
    plist = []
    top_idea = None
    for t in tasks:
        stage = _proposal_stage(t)
        counts[stage] = counts.get(stage, 0) + 1
        # cumulative reward + history (idempotent — stage-based)
        pts = 0.0
        if stage in ("approved", "in_production", "published"):
            pts += r_appr
            reward_history.append({"date": _hist_when(t, "assigned") or _ist(t.created_at),
                                   "reason": "Video Idea Approved", "proposal": t.title,
                                   "points": r_appr, "approved_by": "Production Manager"})
        if stage in ("in_production", "published"):
            pts += r_prod
            reward_history.append({"date": _hist_when(t, "editing_soon", "editing_done") or _ist(t.updated_at),
                                   "reason": "Moved to Production", "proposal": t.title,
                                   "points": r_prod, "approved_by": "Production Manager"})
        if stage == "published":
            pts += r_pub
            reward_history.append({"date": _hist_when(t, "uploaded") or _ist(t.submitted_at) or _ist(t.updated_at),
                                   "reason": "Published", "proposal": t.title,
                                   "points": r_pub, "approved_by": "Production Manager"})
        reward_total += pts
        if pts > 0 and (top_idea is None or pts > top_idea["points"]):
            top_idea = {"title": t.title, "points": pts, "stage": stage}
        plist.append({
            "id": t.id, "title": t.title, "subject": t.subject or "",
            "video_type": t.video_type or "", "stage": stage,
            "reward": round(pts, 1), "duplicate": t.id in dup_ids,
            "date": _ist(t.created_at), "reference": (t.reference or "")[:200],
            "rejected_reason": (t.review_remarks or "") if stage == "rejected" else "",
        })

    proposed_total = len(tasks)
    approved_ct = counts["approved"] + counts["in_production"] + counts["published"]
    appr_rate = round(100.0 * approved_ct / proposed_total, 1) if proposed_total else 0.0
    pub_rate = round(100.0 * counts["published"] / proposed_total, 1) if proposed_total else 0.0
    reward_history.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    plist.sort(key=lambda p: (p.get("date") or ""), reverse=True)
    return {
        "teacher_id": tp.id,
        "counts": {"proposed": proposed_total, "approved": approved_ct,
                   "in_production": counts["in_production"], "published": counts["published"],
                   "rejected": counts["rejected"]},
        "approval_rate": appr_rate, "publication_rate": pub_rate,
        "reward_points": round(reward_total, 1),
        "top_idea": top_idea, "duplicates": len(dup_ids),
        "reward_history": reward_history, "proposals": plist,
    }


def _ist(dt):
    try:
        from models import ist_iso
        return ist_iso(dt)
    except Exception:
        return dt.isoformat() if dt else None


# =============================================================================
# PER-TEACHER TARGETS (individual) — 2 modes: AUTO (timetable se) ya MANUAL.
# Store: AppSetting perf_ttgt_<tid> = {mode, targets:{classes,dpp,videos,shorts,live,tests}}.
# Engine ka "targets" component in explicit targets ko use karta hai (set hon to).
# =============================================================================
def _ttgt_key(tid):
    return "perf_ttgt_%s" % tid


def get_teacher_targets(db, tid):
    from models import AppSetting
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _ttgt_key(tid)).first()
        if row and row.value:
            return _json.loads(row.value)
    except Exception:
        pass
    return {}


def save_teacher_targets(db, tid, targets, mode="manual"):
    from models import AppSetting
    clean = {}
    for k, v in (targets or {}).items():
        try:
            clean[k] = max(0, int(float(v or 0)))
        except Exception:
            clean[k] = 0
    payload = {"mode": (mode or "manual"), "targets": clean, "at": _ist(datetime.utcnow()) or ""}
    try:
        row = db.query(AppSetting).filter(AppSetting.key == _ttgt_key(tid)).first()
        if not row:
            db.add(AppSetting(key=_ttgt_key(tid), value=_json.dumps(payload)))
        else:
            row.value = _json.dumps(payload)
        db.commit()
    except Exception:
        pass
    return payload


def auto_targets_from_timetable(db, tp, month=""):
    """BULLETPROOF: teacher ki is month ki timetable se targets nikaalo.
    - Entry teacher ki hai agar teacher_id == tp.id YA subject teacher ke subjects me (dono).
    - Mock/Test Series -> TESTS. PYQ solution / Revision / Doubt / chapter parts -> CLASSES.
    - Chapters (DPP min) = distinct real syllabus chapters (pyq/revision/test chhod ke).
    - Sabhi assigned subjects consider hote hain."""
    from models import TimetableEntry
    from sqlalchemy import or_
    dt0, dt1, _ = month_bounds(month)
    subs = [s for s in (tp.subjects or []) if s]
    classes = 0
    tests_tt = 0
    chset = set()
    try:
        conds = [TimetableEntry.teacher_id == tp.id]
        if subs:
            conds.append(TimetableEntry.subject.in_(subs))
        rows = db.query(TimetableEntry).filter(
            TimetableEntry.entry_date >= dt0.date(),
            TimetableEntry.entry_date < dt1.date(),
            or_(*conds)).all()
        for e in rows:
            txt = ((e.chapter or "") + " " + (e.part or "") + " " +
                   (getattr(e, "topic_covered", "") or "")).lower()
            # Mock / Test Series / Test -> tests (class nahi)
            if ("mock" in txt) or ("test" in txt) or ("series" in txt):
                tests_tt += 1
                continue
            # baaki sab (chapter part / PYQ solution / revision / doubt) -> class
            classes += 1
            ch = (e.chapter or "").strip()
            low = ch.lower()
            # real chapter hi DPP-chapter count me (pyq/revision/doubt nahi)
            if ch and not any(w in low for w in ("pyq", "revision", "doubt", "mock", "test", "series")):
                chset.add(((e.subject or "").strip().lower(), low))
    except Exception:
        pass
    chapters = len(chset)
    vids = shorts = live = tests_v = 0
    try:
        tg = _vt_targets_for(db, tp, dt0, dt1)
        for r in tg.get("rows", []):
            k = r.get("key")
            n = r.get("assigned", 0) or r.get("target", 0)
            if k == "videos":
                vids = n
            elif k == "shorts":
                shorts = n
            elif k == "live":
                live = n
            elif k == "tests":
                tests_v = r.get("target", 0) or r.get("assigned", 0)
    except Exception:
        pass
    tests = tests_tt or tests_v   # timetable ke mock/test priority
    dpp_min = chapters
    dpp_max = max(classes, chapters)
    return {"classes": classes, "chapters": chapters,
            "dpp": (dpp_min or classes), "dpp_min": dpp_min, "dpp_max": dpp_max,
            "videos": vids, "shorts": shorts, "live": live, "tests": tests}


# =============================================================================
# TEACHER ACTIVITY LOG (timetable timing change / delete / reschedule request)
# =============================================================================
_ACT_LABEL = {"timing_change": "Class timing changed", "class_edit": "Class edited",
              "class_delete": "Class/part deleted", "reschedule_request": "Reschedule requested"}


def teacher_activity_rows(db, tid, limit=30):
    from models import TeacherActivity
    lim = max(1, min(int(limit or 30), 100))
    rows = db.query(TeacherActivity).filter(TeacherActivity.teacher_id == tid) \
        .order_by(TeacherActivity.created_at.desc()).limit(lim).all()
    counts = {}
    out = []
    for a in rows:
        counts[a.action] = counts.get(a.action, 0) + 1
        out.append({"action": a.action, "label": _ACT_LABEL.get(a.action, a.action),
                    "subject": a.subject or "", "chapter": a.chapter or "",
                    "detail": a.detail or "", "at": _ist(a.created_at)})
    try:
        total = db.query(TeacherActivity).filter(TeacherActivity.teacher_id == tid).count()
    except Exception:
        total = len(out)
    return {"teacher_id": tid, "total": total, "counts": counts, "activity": out}
