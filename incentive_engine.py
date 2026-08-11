# =============================================================================
# DYNAMIC INCENTIVE ENGINE — pure calculation (no DB) — spec Part 76-120
# -----------------------------------------------------------------------------
# Generic, fully configurable. Views is just the FIRST metric — the SAME engine
# handles any measurable metric. Admin defines the rule; this computes the reward.
# All amounts in INR (integers). Never over-pays: every result is capped.
# =============================================================================

CALC_TYPES = ("fixed", "per_unit", "tiered", "percentage", "threshold_bonus", "milestone")

# Metrics that can be read automatically from portal activity (month_activity keys).
# Others (views, watch_time, custom) come from a controlled manual/analytics value.
AUTO_METRICS = {
    "video_uploads": "videos_made",
    "live_sessions": "live_sessions",
    "shorts_uploads": "shorts_made",
    "test_completion": "tests_created",
    "dpp_completion": "dpp_uploaded",
    "doubts_resolved": "doubts_resolved",
    "task_completion": "tasks_on_time",
    "notes_uploaded": "notes_uploaded",
    "classes_conducted": "classes_conducted",
}
# Metrics with no automatic portal source — need a recorded value (admin/analytics).
MANUAL_METRICS = {"video_views", "video_watch_time", "shorts_views", "video_published",
                  "video_approvals", "video_completion", "live_attendance",
                  "content_submission", "content_approval", "student_engagement", "custom"}

ALL_METRICS = sorted(set(AUTO_METRICS.keys()) | MANUAL_METRICS)


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except Exception:
        return d


def compute_incentive(calc_type, metric_value, params):
    """Ek rule ke liye reward compute karo.
    params keys (jitne relevant hon): min_threshold, unit_size, unit_reward,
    tiers (list of {min,max,reward}), percentage, target_value, target_reward, max_reward.
    Returns dict: {calculated, capped, qualifying, explain}."""
    mv = max(0.0, _f(metric_value))
    p = params or {}
    min_th = _f(p.get("min_threshold"), 0)
    max_reward = _f(p.get("max_reward"), 0)   # 0 / None => no per-rule cap
    calc = (calc_type or "").lower()

    calculated = 0.0
    qualifying = 0
    explain = ""

    if mv < min_th:
        return {"calculated": 0, "capped": 0, "qualifying": 0,
                "explain": "Below minimum threshold (%g < %g)" % (mv, min_th)}

    if calc == "fixed":
        # flat amount jab metric threshold (default 1) tak pahunche
        need = max(1.0, min_th)
        if mv >= need:
            calculated = _f(p.get("target_reward"))
            explain = "Fixed reward %g (metric %g >= %g)" % (calculated, mv, need)

    elif calc == "per_unit":
        size = _f(p.get("unit_size"), 1) or 1
        rate = _f(p.get("unit_reward"))
        qualifying = int(mv // size)
        calculated = qualifying * rate
        explain = "%g / %g = %d unit(s) x %g = %g" % (mv, size, qualifying, rate, calculated)

    elif calc == "tiered":
        tiers = p.get("tiers") or []
        chosen = None
        for t in tiers:
            lo = _f(t.get("min"), 0)
            hi = t.get("max", None)
            hi = _f(hi, float("inf")) if hi not in (None, "", "inf") else float("inf")
            if mv >= lo and mv <= hi:
                chosen = t
                break
        if chosen is None and tiers:
            # highest tier ka reward agar sab max se upar (top tier open-ended na ho)
            top = max(tiers, key=lambda t: _f(t.get("min"), 0))
            if mv >= _f(top.get("min"), 0):
                chosen = top
        if chosen:
            calculated = _f(chosen.get("reward"))
            explain = "Tier matched -> %g" % calculated
        else:
            explain = "No tier matched"

    elif calc == "percentage":
        pct = _f(p.get("percentage"))
        calculated = mv * pct / 100.0
        explain = "%g%% of %g = %g" % (pct, mv, calculated)

    elif calc == "threshold_bonus":
        tv = _f(p.get("target_value"))
        if mv >= tv:
            calculated = _f(p.get("target_reward"))
            explain = "Reached %g -> bonus %g" % (tv, calculated)
        else:
            explain = "Threshold %g not reached (%g)" % (tv, mv)

    elif calc == "milestone":
        tv = _f(p.get("target_value"))
        if mv >= tv:
            calculated = _f(p.get("target_reward"))
            explain = "Milestone %g reached -> %g (one-time)" % (tv, calculated)
        else:
            explain = "Milestone %g not reached (%g)" % (tv, mv)
    else:
        explain = "Unknown calculation type"

    calculated = max(0.0, calculated)
    capped = calculated
    if max_reward and max_reward > 0 and capped > max_reward:
        capped = max_reward
        explain += " | capped at %g" % max_reward

    return {"calculated": int(round(calculated)), "capped": int(round(capped)),
            "qualifying": qualifying, "explain": explain}


def apply_global_cap(total, global_cap):
    """Sab rules ka sum global monthly cap se upar na jaye."""
    if global_cap and global_cap > 0 and total > global_cap:
        return int(global_cap)
    return int(total)


def apply_stacking(rule_rewards, mode):
    """rule_rewards = list of (rule_id, reward). mode: allow | none | highest.
    Returns filtered list of (rule_id, reward)."""
    mode = (mode or "allow").lower()
    if not rule_rewards:
        return []
    if mode == "highest":
        best = max(rule_rewards, key=lambda x: x[1])
        return [best] if best[1] > 0 else []
    if mode in ("none", "no"):
        # sirf pehla non-zero (deterministic): actually "no stacking" = ek hi rule.
        nz = [r for r in rule_rewards if r[1] > 0]
        return [max(nz, key=lambda x: x[1])] if nz else []
    return rule_rewards  # allow
