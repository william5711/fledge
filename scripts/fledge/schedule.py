"""Weekly classes and work shifts, and spotting busy days."""
from datetime import timedelta

from .agenda import agenda_view
from .common import UserError, find, new_id, parse_date, parse_time, today


BUSY_LOOKAHEAD_DAYS = 14
BUSY_GRADED_ITEMS = 2
BUSY_SCHEDULED_HOURS = 6
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def schedule_minutes(block):
    start_hour, start_minute = map(int, block["start"].split(":"))
    end_hour, end_minute = map(int, block["end"].split(":"))
    # An end time before the start means an overnight shift.
    return ((end_hour - start_hour) * 60 + end_minute - start_minute) % (24 * 60)


def schedule_view(block):
    return {
        "id": block["id"],
        "kind": block["kind"],
        "title": block["title"],
        "days": block["days"],
        "start": block["start"],
        "end": block["end"],
        "from": block["from"],
        "until": block["until"],
    }


def scheduled_on(store, day):
    iso = day.isoformat()
    blocks = [
        b for b in store["schedule"]
        if WEEKDAYS[day.weekday()] in b["days"]
        and (not b["from"] or b["from"] <= iso)
        and (not b["until"] or iso <= b["until"])
    ]
    return sorted(blocks, key=lambda b: b["start"])


def day_load(store, day):
    iso = day.isoformat()
    blocks = scheduled_on(store, day)
    entries = [e for e in store["agenda"] if e["date"] == iso and not e["done"]]
    deadlines = [e for e in entries if e["kind"] == "deadline"]
    graded = [e for e in deadlines if e.get("type") != "admin"]
    hours = round(sum(schedule_minutes(b) for b in blocks) / 60, 1)

    reason = None
    if len(graded) >= BUSY_GRADED_ITEMS:
        reason = f"{len(graded)} graded items due"
    elif graded and hours >= BUSY_SCHEDULED_HOURS:
        reason = f"{graded[0]['title']} plus {hours:g} hours of classes and work"
    return {
        "date": iso,
        "weekday": WEEKDAYS[day.weekday()],
        "classes_and_shifts": [schedule_view(b) for b in blocks],
        "deadlines": [agenda_view(e) for e in deadlines],
        "events": [agenda_view(e) for e in entries if e["kind"] == "event"],
        "scheduled_hours": hours,
        "busy_reason": reason,
    }


def cmd_schedule_add(store, args):
    days = [part.strip().lower()[:3] for part in args.days.split(",") if part.strip()]
    unknown = [part for part in days if part not in WEEKDAYS]
    if unknown or not days:
        raise UserError(f"Use days from {', '.join(WEEKDAYS)}.")
    if parse_time(args.start) == parse_time(args.end):
        raise UserError("The start and end times can't match.")
    first = parse_date(args.start_date).isoformat() if args.start_date else None
    last = parse_date(args.until).isoformat() if args.until else None
    if first and last and last < first:
        raise UserError("The end date is before the start date.")
    block = {
        "id": new_id(store, "w"),
        "kind": args.kind,
        "title": args.title,
        "days": days,
        "start": args.start,
        "end": args.end,
        "from": first,
        "until": last,
    }
    store["schedule"].append(block)
    return {"status": "saved", "block": schedule_view(block)}


def cmd_schedule_remove(store, args):
    block = find(store["schedule"], args.id, "schedule block")
    store["schedule"].remove(block)
    return {"status": "deleted", "block": schedule_view(block)}


def cmd_schedule_list(store, args):
    return {"schedule": [schedule_view(b) for b in store["schedule"]]}


def cmd_week(store, args):
    if args.weeks < 1:
        raise UserError("--weeks must be at least 1.")
    day = parse_date(args.date) if args.date else today()
    monday = day - timedelta(days=day.weekday())
    weeks = []
    for number in range(args.weeks):
        start = monday + timedelta(weeks=number)
        days = [day_load(store, start + timedelta(days=offset)) for offset in range(7)]
        all_deadlines = [d for load in days for d in load["deadlines"]]
        work_minutes = sum(
            schedule_minutes(b)
            for offset in range(7)
            for b in scheduled_on(store, start + timedelta(days=offset))
            if b["kind"] == "work"
        )
        weeks.append({
            "week_of": start.isoformat(),
            "scheduled_hours": round(sum(load["scheduled_hours"] for load in days), 1),
            "work_hours": round(work_minutes / 60, 1),
            "exams": sum(1 for d in all_deadlines if d["type"] == "exam"),
            "deadlines": len(all_deadlines),
            "busy_days": [
                {"date": load["date"], "reason": load["busy_reason"]}
                for load in days if load["busy_reason"]
            ],
            "days": days,
        })
    return {"weeks": weeks}
