"""Doubt resolution metrics — shared helpers.

- Resolution time (wall-clock) per resolved doubt.
- On-time / late with FORWARD-ONLY SLA: har doubt par resolve ke waqt ka SLA snapshot
  (`sla_snapshot`) use hota hai; admin baad me SLA badle to purane doubts ka status nahi badlta.
- Office-hours-adjusted response time (sirf average ke liye): office hours ke bahar ka
  waqt average me count nahi hota. Late-resolved isse alag hai (wall-clock).

Ye sab teacher + admin ko dikhta hai; students ko NAHI.
"""
import json as _json
from datetime import datetime, timedelta

OFFICE_DEFAULT = {"enabled": True, "start": 9, "end": 20}
# DB timestamps naive-UTC me store hote hain (ist_iso bhi UTC maan ke IST deta hai).
# Office hours IST me hain (9 AM - 8 PM IST), isliye office-calc se pehle +5:30 shift.
_IST = timedelta(hours=5, minutes=30)


def sla_hours(db):
    try:
        import perf_config as _pc
        v = float(_pc.get_perf_config(db).get("doubt_sla_hours", 15) or 15)
        return v if v > 0 else 15.0
    except Exception:
        return 15.0


def office_cfg(db):
    from models import AppSetting
    cfg = dict(OFFICE_DEFAULT)
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "doubt_office_cfg").first()
        if row and row.value:
            saved = _json.loads(row.value)
            for k in ("enabled", "start", "end"):
                if k in saved:
                    cfg[k] = saved[k]
    except Exception:
        pass
    try:
        cfg["start"] = max(0, min(23, int(cfg["start"])))
    except Exception:
        cfg["start"] = 9
    try:
        cfg["end"] = max(1, min(24, int(cfg["end"])))
    except Exception:
        cfg["end"] = 20
    if cfg["end"] <= cfg["start"]:
        cfg["end"] = min(24, cfg["start"] + 1)
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def save_office_cfg(db, patch):
    from models import AppSetting
    cfg = office_cfg(db)
    if "enabled" in patch:
        cfg["enabled"] = bool(patch["enabled"])
    if "start" in patch and patch["start"] is not None:
        cfg["start"] = max(0, min(23, int(patch["start"])))
    if "end" in patch and patch["end"] is not None:
        cfg["end"] = max(1, min(24, int(patch["end"])))
    if cfg["end"] <= cfg["start"]:
        raise ValueError("Closing time must be after opening time.")
    row = db.query(AppSetting).filter(AppSetting.key == "doubt_office_cfg").first()
    if not row:
        row = AppSetting(key="doubt_office_cfg")
        db.add(row)
    row.value = _json.dumps(cfg)
    db.commit()
    return cfg


def office_minutes(start_dt, end_dt, oh):
    """Minutes between start_dt and end_dt that fall inside daily office window [start,end).
    oh disabled -> plain wall-clock minutes."""
    if not start_dt or not end_dt or end_dt <= start_dt:
        return 0
    if not oh.get("enabled", True):
        return int((end_dt - start_dt).total_seconds() // 60)
    os_h = int(oh["start"])
    oe_h = int(oh["end"])
    total = 0.0
    cur = start_dt
    guard = 0
    while cur < end_dt and guard < 800:
        guard += 1
        day = cur.date()
        win_start = datetime(day.year, day.month, day.day, os_h, 0, 0)
        if oe_h >= 24:
            win_end = datetime(day.year, day.month, day.day) + timedelta(days=1)
        else:
            win_end = datetime(day.year, day.month, day.day, oe_h, 0, 0)
        lo = max(cur, win_start)
        hi = min(end_dt, win_end)
        if hi > lo:
            total += (hi - lo).total_seconds()
        cur = datetime(day.year, day.month, day.day) + timedelta(days=1)
    return int(total // 60)


def fmt_dur(minutes):
    if minutes is None:
        return ""
    minutes = int(round(minutes))
    if minutes < 0:
        minutes = 0
    if minutes < 60:
        return "%dm" % minutes
    h = minutes // 60
    m = minutes % 60
    if h < 24:
        return ("%dh %dm" % (h, m)) if m else ("%dh" % h)
    d = h // 24
    hh = h % 24
    return ("%dd %dh" % (d, hh)) if hh else ("%dd" % d)


def metrics_for(created_at, resolved_at, sla_snap, sla_now, oh):
    """Resolved doubt ke metrics. sla_snap None -> sla_now use karo."""
    if not created_at or not resolved_at:
        return None
    mins = int((resolved_at - created_at).total_seconds() // 60)
    if mins < 0:
        mins = 0
    sla = sla_snap if (sla_snap is not None and sla_snap > 0) else sla_now
    limit_min = sla * 60.0
    late = mins > limit_min
    late_min = int(mins - limit_min) if late else 0
    om = office_minutes(created_at + _IST, resolved_at + _IST, oh)
    return {
        "minutes": mins, "late": late, "late_minutes": late_min,
        "office_minutes": om, "sla_hours": sla,
        "label": fmt_dur(mins), "late_label": fmt_dur(late_min) if late else "",
        "office_label": fmt_dur(om),
    }
