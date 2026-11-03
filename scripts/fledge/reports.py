"""The daily briefing, the weekly review, and the full export."""
from datetime import timedelta

from .agenda import agenda_view
from .bills import bill_view
from .budget import monthly_position, net_cents, position_view, purchase_view
from .chores import chore_view
from .common import fmt, month_end, month_key, parse_date, today
from .kitchen import kitchen_flag, kitchen_sorted, kitchen_view
from .movein import task_view
from .schedule import BUSY_LOOKAHEAD_DAYS, day_load, schedule_view, scheduled_on
from .shared import balances_view

SOON_DAYS = 7


def cmd_export(store, args):
    return store


def cmd_briefing(store, args):
    day = parse_date(args.date) if args.date else today()
    soon = day + timedelta(days=SOON_DAYS)
    iso = day.isoformat()
    tomorrow = (day + timedelta(days=1)).isoformat()

    def by_time(entry):
        return entry["time"] or ""

    open_agenda = [e for e in store["agenda"] if not e["done"]]
    events = [e for e in open_agenda if e["kind"] == "event"]
    reminders = [e for e in open_agenda if e["kind"] == "reminder"]

    bills = [b for b in store["bills"] if b["active"]]
    planned = [
        p for p in store["purchases"]
        if p["status"] == "planned" and not p.get("movein_id") and p["date"]
    ]

    month = month_key(day)
    position = monthly_position(store, month)
    days_left = (month_end(month) - day).days + 1
    budget = position_view(position)
    budget["days_left_in_month"] = days_left
    if position["remaining_cents"] is not None:
        budget["per_day"] = fmt(max(position["remaining_cents"], 0) // days_left)

    movein = None
    plan = store["movein"]
    if plan["date"]:
        open_tasks = [t for t in plan["tasks"] if not t["done"] and t["due"]]
        days_until = (parse_date(plan["date"]) - day).days
        if open_tasks or days_until >= 0:
            movein = {
                "move_date": plan["date"],
                "days_until_move": days_until,
                "overdue": [task_view(store, t) for t in open_tasks if t["due"] < iso],
                "due_this_week": [
                    task_view(store, t) for t in open_tasks if iso <= t["due"] <= soon.isoformat()
                ],
            }

    deadlines = [e for e in open_agenda if e["kind"] == "deadline"]
    recent = (day - timedelta(days=SOON_DAYS)).isoformat()
    busy_days = []
    for offset in range(1, BUSY_LOOKAHEAD_DAYS + 1):
        load = day_load(store, day + timedelta(days=offset))
        if load["busy_reason"]:
            busy_days.append({"date": load["date"], "reason": load["busy_reason"]})

    setup_needed = []
    if store["settings"]["monthly_budget_cents"] is None:
        setup_needed.append("monthly budget")
    if not store["bills"]:
        setup_needed.append("bills")

    return {
        "date": iso,
        "events_today": [agenda_view(e) for e in sorted((e for e in events if e["date"] == iso), key=by_time)],
        "events_tomorrow": [agenda_view(e) for e in sorted((e for e in events if e["date"] == tomorrow), key=by_time)],
        "reminders_due": [agenda_view(e) for e in sorted((e for e in reminders if e["date"] == iso), key=by_time)],
        "reminders_overdue": [agenda_view(e) for e in reminders if e["date"] < iso],
        "bills_overdue": [bill_view(b) for b in bills if b["next_due"] < iso],
        "bills_due_this_week": [
            bill_view(b) for b in sorted(bills, key=lambda b: b["next_due"])
            if iso <= b["next_due"] <= soon.isoformat()
        ],
        "planned_purchases_this_week": [
            purchase_view(p) for p in planned if iso <= p["date"] <= soon.isoformat()
        ],
        "budget": budget,
        "move_in": movein,
        "classes_and_shifts_today": [schedule_view(s) for s in scheduled_on(store, day)],
        "deadlines_this_week": [
            agenda_view(e) for e in sorted(deadlines, key=lambda e: (e["date"], e["time"] or ""))
            if iso <= e["date"] <= soon.isoformat()
        ],
        # Exams can't be late, and old unfinished items shouldn't nag forever.
        "deadlines_missed": [
            agenda_view(e) for e in deadlines
            if recent <= e["date"] < iso and e.get("type") not in ("exam", "quiz")
        ],
        "busy_days_ahead": busy_days,
        "chores_due": [chore_view(c) for c in store["chores"] if c["next_due"] <= iso],
        "kitchen_use_soon": [
            kitchen_view(k, day) for k in kitchen_sorted(store) if kitchen_flag(k, day)
        ],
        "roommates": balances_view(store),
        "setup_needed": setup_needed,
    }


def spending_between(store, first, last):
    categories = {}
    bills = 0
    for p in store["purchases"]:
        if p["status"] == "planned" or p.get("movein_id") or not first <= p["date"] <= last:
            continue
        if p.get("bill_id"):
            bills += net_cents(p)
        else:
            categories[p["category"]] = categories.get(p["category"], 0) + net_cents(p)
    return categories, bills


def spending_pace(store, day):
    """Compare non-bill spending with how far through the month it is."""
    month = month_key(day)
    position = monthly_position(store, month)
    if position["budget_cents"] is None:
        return None
    bills_paid = sum(
        net_cents(p) for p in store["purchases"]
        if p.get("bill_id") and p["status"] != "planned" and p["date"][:7] == month
    )
    available = position["budget_cents"] - bills_paid - position["bills_remaining_cents"]
    spent = position["spent_cents"] - bills_paid
    expected = max(available, 0) * day.day // month_end(month).day
    if available <= 0:
        status = "bills_use_the_whole_budget"
    elif spent > expected:
        status = "ahead_of_pace"
    else:
        status = "on_pace"
    return {
        "status": status,
        "available_after_bills": fmt(available),
        "spent_so_far": fmt(spent),
        "expected_by_today": fmt(expected),
    }


def cmd_review(store, args):
    day = parse_date(args.date) if args.date else today()
    week_start = (day - timedelta(days=6)).isoformat()
    previous_start = (day - timedelta(days=13)).isoformat()
    previous_end = (day - timedelta(days=7)).isoformat()
    iso = day.isoformat()
    soon = (day + timedelta(days=SOON_DAYS)).isoformat()

    categories, bills_paid = spending_between(store, week_start, iso)
    previous_categories, _ = spending_between(store, previous_start, previous_end)
    ranked = sorted(categories.items(), key=lambda pair: -pair[1])

    next_week = [day_load(store, day + timedelta(days=offset)) for offset in range(1, SOON_DAYS + 1)]
    plan = store["movein"]
    movein = None
    if plan["date"] and any(not t["done"] for t in plan["tasks"]):
        movein = {
            "move_date": plan["date"],
            "days_until_move": (parse_date(plan["date"]) - day).days,
            "tasks_done": sum(1 for t in plan["tasks"] if t["done"]),
            "tasks_total": len(plan["tasks"]),
        }
    return {
        "week": {"from": week_start, "to": iso},
        "spending": {
            "total_excluding_bills": fmt(sum(categories.values())),
            "previous_week_excluding_bills": fmt(sum(previous_categories.values())),
            "bills_paid": fmt(bills_paid),
            "by_category": [{"category": name, "spent": fmt(cents)} for name, cents in ranked],
        },
        "month": position_view(monthly_position(store, month_key(day))),
        "pace": spending_pace(store, day),
        "next_week": {
            "bills_due": [
                bill_view(b) for b in sorted(store["bills"], key=lambda b: b["next_due"])
                if b["active"] and b["next_due"] <= soon
            ],
            "deadlines": [d for load in next_week for d in load["deadlines"]],
            "scheduled_hours": round(sum(load["scheduled_hours"] for load in next_week), 1),
            "busy_days": [
                {"date": load["date"], "reason": load["busy_reason"]}
                for load in next_week if load["busy_reason"]
            ],
        },
        "chores": {
            "done_this_week": [
                c["title"] for c in store["chores"]
                if c["last_done"] and week_start <= c["last_done"] <= iso
            ],
            "overdue": [c["title"] for c in store["chores"] if c["next_due"] < iso],
        },
        "kitchen_use_soon": [
            kitchen_view(k, day) for k in kitchen_sorted(store) if kitchen_flag(k, day)
        ],
        "roommates": balances_view(store),
        "move_in": movein,
    }
