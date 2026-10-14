#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# coast_stats.py
#
# Reads a Coast CSV export (the NEW platform layout) and writes coast_stats.json
# into the office-board web root, for the full-screen "Coast Operations" panel.
#
# Zero tokens — pure counting/date math, no AI calls.
#
# Manual for now: drop a fresh export at CSV_PATH, then run this (or let cron
# regenerate it every few minutes). The board reads the JSON, not the CSV.
#
# Cron suggestion:  */10 * * * *  python3 /home/user/coast_stats.py >> /home/user/coast_stats.log 2>&1
# ─────────────────────────────────────────────────────────────────────────────

import csv, json, os
from datetime import datetime, timezone
from collections import defaultdict

CSV_PATH = "/mnt/c/CoastExport/coast_new.csv"          # drop the new-platform export here
OUT_PATH = "/home/user/weather/coast_stats.json"      # board's :8080 web root

OPEN_STATUSES = {"Open", "In Progress", "Scheduled", "Pending Review"}
STATUS_ORDER = ["Open", "In Progress", "Scheduled", "Pending Review", "Complete"]

def _order_statuses(counts):
    out = [[s, counts[s]] for s in STATUS_ORDER if counts.get(s)]
    for s, c in counts.items():
        if s not in STATUS_ORDER:
            out.append([s, c])
    return out

def parse_dt(x):
    x = (x or "").strip()
    try:
        return datetime.fromisoformat(x)   # keeps the -07:00 offset (tz-aware)
    except Exception:
        return None

def g(row, *names):
    """First non-empty value among the given column names."""
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""

def build():
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    now = datetime.now(timezone.utc)
    def age_days(dt):
        d = parse_dt(dt)
        return (now - d).days if d else None

    open_count = complete_count = 0
    close_deltas = []
    zones = defaultdict(int)
    status_counts = defaultdict(int)
    tenants = {}          # name -> {open, total, oldest_open_days}
    oldest_open = None    # (days, row)

    for r in rows:
        status = (r.get("Work Order Status") or "").strip()
        is_open = status in OPEN_STATUSES
        is_done = status == "Complete"
        if is_open: open_count += 1
        if is_done: complete_count += 1
        if status: status_counts[status] += 1

        # avg days to close (Updated - Created) on completed tickets
        if is_done:
            c, u = parse_dt(r.get("Created On")), parse_dt(r.get("Updated On"))
            if c and u:
                close_deltas.append((u - c).total_seconds() / 86400)

        # zone breakdown (skip blanks)
        z = g(r, "Zone")
        if z:
            zones[z] += 1

        # tenant leaderboard (Tenant, fallback Tenants (New))
        t = g(r, "Tenant", "Tenants (New)")
        if t:
            rec = tenants.setdefault(t, {"open": 0, "total": 0, "oldest_open_days": 0})
            rec["total"] += 1
            if is_open:
                rec["open"] += 1
                a = age_days(r.get("Created On"))
                if a is not None and a > rec["oldest_open_days"]:
                    rec["oldest_open_days"] = a

        # oldest open ticket overall
        if is_open:
            a = age_days(r.get("Created On"))
            if a is not None and (oldest_open is None or a > oldest_open[0]):
                oldest_open = (a, r)

    # rank tenants: most open first, then most total, then name
    ranked = sorted(
        ({"name": k, **v} for k, v in tenants.items()),
        key=lambda x: (-x["open"], -x["total"], x["name"])
    )

    total_valid = open_count + complete_count
    stats = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "csv_age_min": int((datetime.now().timestamp() - os.path.getmtime(CSV_PATH)) / 60),
        "total_tickets": len(rows),
        "open": open_count,
        "complete": complete_count,
        "completion_pct": round(complete_count / total_valid * 100) if total_valid else 0,
        "avg_days_close": round(sum(close_deltas) / len(close_deltas), 1) if close_deltas else None,
        "zones": sorted(zones.items(), key=lambda x: -x[1]),
        "statuses": _order_statuses(status_counts),
        "tenants": ranked[:6],
        "oldest_open": None,
    }
    if oldest_open:
        days, r = oldest_open
        stats["oldest_open"] = {
            "days": days,
            "id": g(r, "Work Order ID", "Card ID Number"),
            "tenant": g(r, "Tenant", "Tenants (New)"),
            "issue": g(r, "Title", "Notes", "Issue") or f"WO #{g(r,'Work Order ID')}",
        }
    return stats

if __name__ == "__main__":
    stats = build()
    with open(OUT_PATH, "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))
