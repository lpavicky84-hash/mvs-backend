# =============================================================================
# TEACHER PERFORMANCE MANAGEMENT SYSTEM — PHASE 1: CONFIG FOUNDATION
# -----------------------------------------------------------------------------
# Har scoring value yahin se aati hai (admin-configurable) — kahin bhi hard-code
# NAHI (spec Part 2 & Part 24). Ye config generic AppSetting key-value store me
# JSON ke roop me `perf_config` key ke andar save hoti hai — koi nayi DB table
# nahi, koi migration nahi, purana kuch touch nahi. Baad ke phases (scoring
# engine, leaderboard, targets, rewards) isi config ko consume karenge.
# =============================================================================
import json
import copy

# spec ki recommended default values — admin inhe portal se badal sakta hai.
PERF_DEFAULTS = {
    # ---- Part 2: main Performance Score components (out of 100) ----
    "component_weights": {
        "teaching": 20,           # Teaching & Class Delivery
        "content": 20,            # Content Production (videos/shorts/DPP etc.)
        "targets": 20,            # Monthly Target Achievement
        "student_support": 15,    # Doubt resolution
        "tests": 10,              # Tests & Assessment work
        "task_discipline": 5,     # Task timeliness/discipline
        "consistency": 5,         # Consistency
        "video_initiative": 5,    # Video ideas/proposals (capped)
    },
    # ---- Part 3: workload units per activity (effort-weighted, configurable) ----
    "activity_workload": {
        "class": 1,
        "dpp": 1,
        "short": 1,
        "long_video": 2,
        "one_shot": 5,
        "revision": 3,
        "youtube_live": 4,
        "weekly_test": 3,
        "doubt": 0.5,
        "task": 1,
        "video_proposal": 0.5,
    },
    # ---- Part 18: quality / quantity / timeliness split (jahaan quality data ho) ----
    "quality_split": {"target": 0.60, "quality": 0.25, "timeliness": 0.15},
    # ---- Part 19: overachievement caps (ek activity dominate na kare) ----
    "overachievement": {"base": 100, "cap_101_120": 105, "cap_over_120": 110},
    # ---- Part 20: minimum workload eligibility (expected monthly workload ka fraction) ----
    "min_workload_fraction": 0.60,
    # ---- Part 9: video initiative max points (teaching ko overpower na kare) ----
    "video_initiative_cap": 5,
    # ---- Part 8 & 30: video idea reward points (approval/production/published) ----
    "video_rewards": {
        "submitted": 0,
        "approved": 1,
        "in_production": 2,
        "published": 2,
        "high_impact": 2,
    },
    # ---- Part 17: consistency rules ----
    "consistency": {"max_points": 5, "streak_days_for_badge": 12},
    # ---- Part 22: workload level thresholds (team-average ke % me) ----
    "workload_levels": {
        "low_below": 60,          # < 60% team-avg  -> Low
        "high_above": 120,        # >= 120%         -> High
        "very_high_above": 150,   # >= 150%         -> Very High
    },
}


def _deep_merge(base, patch):
    """patch ke keys ko base me merge karo (nested dicts recursively). base mutate hota hai."""
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def get_perf_config(db):
    """Defaults + admin ke saved overrides ka merged config. Kuch save na ho to pure defaults."""
    from models import AppSetting
    cfg = copy.deepcopy(PERF_DEFAULTS)
    try:
        row = db.query(AppSetting).filter(AppSetting.key == "perf_config").first()
        if row and row.value:
            _deep_merge(cfg, json.loads(row.value))
    except Exception:
        pass
    return cfg


def _num(v, fallback):
    try:
        f = float(v)
        return f if f == f else fallback   # NaN guard
    except Exception:
        return fallback


def _validate(cfg):
    """Sirf numbers rakho, negative na hon. Component weights ka sum bhi lauta do (info)."""
    dfl = PERF_DEFAULTS
    for grp in ("component_weights", "activity_workload", "video_rewards",
                "workload_levels", "consistency", "overachievement", "quality_split"):
        if isinstance(cfg.get(grp), dict):
            for k in list(cfg[grp].keys()):
                cfg[grp][k] = max(0, _num(cfg[grp][k], dfl.get(grp, {}).get(k, 0)))
    cfg["min_workload_fraction"] = min(1.0, max(0.0, _num(cfg.get("min_workload_fraction"), 0.6)))
    cfg["video_initiative_cap"] = max(0, _num(cfg.get("video_initiative_cap"), 5))
    return cfg


def save_perf_config(db, patch):
    """Admin ke bheje overrides ko merge + validate karke save karo. Merged config return."""
    from models import AppSetting
    cfg = get_perf_config(db)
    _deep_merge(cfg, patch or {})
    cfg = _validate(cfg)
    row = db.query(AppSetting).filter(AppSetting.key == "perf_config").first()
    if not row:
        db.add(AppSetting(key="perf_config", value=json.dumps(cfg)))
    else:
        row.value = json.dumps(cfg)
    db.commit()
    return cfg


def component_weight_sum(cfg):
    """Part 2: components ka sum (ideally 100). UI is number ko dikha ke warn kar sakti hai."""
    try:
        return round(sum(float(v) for v in (cfg.get("component_weights") or {}).values()), 2)
    except Exception:
        return 0
