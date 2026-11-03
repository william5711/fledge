"""Events, reminders, and deadlines, including deadlines imported from a syllabus."""
import json
import sys

from .common import UserError, find, new_id, parse_date, parse_time

DEADLINE_TYPES = ["exam", "quiz", "assignment", "project", "admin"]


def agenda_view(entry):
    return {
        "id": entry["id"],
        "kind": entry["kind"],
        "title": entry["title"],
        "date": entry["date"],
        "time": entry["time"],
        "done": entry["done"],
        "course": entry.get("course"),
        "type": entry.get("type"),
    }


def add_agenda_entry(store, kind, title, when, clock, course, deadline_type):
    """Return the saved entry and whether it was new.

    Pasting the same syllabus twice shouldn't double every deadline.
    """
    iso = parse_date(when).isoformat()
    for entry in store["agenda"]:
        if (
            entry["kind"] == kind
            and entry["date"] == iso
            and entry["title"].casefold() == title.casefold()
            and (entry.get("course") or "").casefold() == (course or "").casefold()
        ):
            return entry, False
    entry = {
        "id": new_id(store, "a"),
        "kind": kind,
        "title": title,
        "date": iso,
        "time": parse_time(clock),
        "done": False,
        "course": course,
        "type": (deadline_type or "assignment") if kind == "deadline" else None,
    }
    store["agenda"].append(entry)
    return entry, True


def cmd_agenda_add(store, args):
    entry, created = add_agenda_entry(
        store, args.kind, args.title, args.date, args.time, args.course, args.type
    )
    return {"status": "saved" if created else "duplicate", "entry": agenda_view(entry)}


def cmd_agenda_import(store, args):
    try:
        rows = json.loads(args.rows) if args.rows is not None else json.load(sys.stdin)
    except ValueError:
        raise UserError("The import must be a JSON list.")
    if not isinstance(rows, list):
        raise UserError("The import must be a JSON list.")
    added, skipped = [], []
    for row in rows:
        if not isinstance(row, dict) or not row.get("title") or not row.get("date"):
            raise UserError("Each import row needs a title and a date. Nothing was saved.")
        deadline_type = row.get("type", "assignment")
        if deadline_type not in DEADLINE_TYPES:
            raise UserError(f"'{deadline_type}' is not one of {', '.join(DEADLINE_TYPES)}.")
        entry, created = add_agenda_entry(
            store, "deadline", row["title"], row["date"], row.get("time"), args.course, deadline_type
        )
        (added if created else skipped).append(agenda_view(entry))
    return {"status": "saved", "added": added, "already_saved": skipped}


def cmd_agenda_done(store, args):
    entry = find(store["agenda"], args.id, "agenda entry")
    entry["done"] = True
    return {"status": "saved", "entry": agenda_view(entry)}


def cmd_agenda_remove(store, args):
    entry = find(store["agenda"], args.id, "agenda entry")
    store["agenda"].remove(entry)
    return {"status": "deleted", "entry": agenda_view(entry)}


def cmd_agenda_list(store, args):
    entries = [
        e for e in store["agenda"]
        if (args.all or not e["done"])
        and (not args.kind or e["kind"] == args.kind)
        and (not args.course or (e.get("course") or "").casefold() == args.course.casefold())
    ]
    return {"entries": [agenda_view(e) for e in sorted(entries, key=lambda e: (e["date"], e["time"] or ""))]}
