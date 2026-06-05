"""Alert rule evaluation."""

import logging
import time

logger = logging.getLogger(__name__)

_FIRED: dict[int, float] = {}  # rule_id -> last fire timestamp


def evaluate_alerts() -> list[dict]:
    """Check all enabled rules against current metrics. Returns newly-fired alerts."""
    from src.monitoring.metrics import _latest_resources
    from src.monitoring.storage import get_alert_rules, save_alert_event

    resources = _latest_resources
    rules = get_alert_rules()
    now = time.time()
    fired: list[dict] = []

    for rule in rules:
        if not rule.get("enabled", 1):
            continue

        value = _get_metric(rule["metric"], resources)
        if value is None:
            continue

        triggered = False
        if rule["operator"] == "gt":
            triggered = value > rule["threshold"]
        elif rule["operator"] == "lt":
            triggered = value < rule["threshold"]

        if triggered:
            last = _FIRED.get(rule["id"], 0)
            # Only fire once per 5 minutes per rule
            if now - last > 300:
                _FIRED[rule["id"]] = now
                evt = {
                    "ts": now,
                    "rule_id": rule["id"],
                    "label": rule["label"],
                    "metric": rule["metric"],
                    "value": round(value, 1),
                    "operator": rule["operator"],
                    "threshold": rule["threshold"],
                }
                save_alert_event(**evt)
                fired.append(evt)
        else:
            _FIRED.pop(rule["id"], None)

    return fired


def _get_metric(metric: str, resources: dict) -> float | None:
    """Extract a numeric metric value from the current resources snapshot."""
    if metric == "cpu":
        return resources.get("cpu")
    if metric == "mem_pct":
        total = resources.get("mem_total", 0)
        used = resources.get("mem_used", 0)
        if total:
            return used / total * 100
        return None
    if metric == "disk_pct":
        disks = resources.get("disks", [])
        totals = resources.get("disk_totals", {})
        pcts = []
        for d in disks:
            t = totals.get(d.get("mount", ""), 0)
            if t:
                pcts.append(d.get("used", 0) / t * 100)
        return max(pcts) if pcts else None
    if metric == "db_pool_pct":
        avail = resources.get("db_pool", {}).get("avail", 0)
        total = resources.get("db_pool", {}).get("max", 1) or 1
        return avail / total * 100
    return None