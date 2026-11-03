"""Recurring chores."""
from datetime import timedelta

from .common import UserError, find, new_id, parse_date, today


# Title and days between repeats.
STARTER_CHORES = [
    ("Take out the trash", 3),
    ("Wipe down the kitchen", 7),
    ("Do laundry", 7),
    ("Change your sheets", 14),
    ("Clean the bathroom", 14),
    ("Clean out the fridge", 14),
    ("Check that the smoke alarm works", 30),
]


def chore_view(chore):
    return {
        "id": chore["id"],
        "title": chore["title"],
        "every_days": chore["every_days"],
        "next_due": chore["next_due"],
        "last_done": chore["last_done"],
    }


def add_chore(store, title, every_days, first_due):
    for chore in store["chores"]:
        if chore["title"].casefold() == title.casefold():
            return None
    chore = {
        "id": new_id(store, "c"),
        "title": title,
        "every_days": every_days,
        "next_due": first_due.isoformat(),
        "last_done": None,
    }
    store["chores"].append(chore)
    return chore


def cmd_chore_add(store, args):
    if args.every_days < 1:
        raise UserError("--every-days must be at least 1.")
    first_due = parse_date(args.first_due) if args.first_due else today()
    chore = add_chore(store, args.title, args.every_days, first_due)
    if chore is None:
        raise UserError(f"You already have a chore called {args.title}.")
    return {"status": "saved", "chore": chore_view(chore)}


def cmd_chore_starter(store, args):
    first_due = parse_date(args.first_due) if args.first_due else today()
    added = [add_chore(store, title, every, first_due) for title, every in STARTER_CHORES]
    return {"status": "saved", "added": [chore_view(c) for c in added if c]}


def cmd_chore_done(store, args):
    chore = find(store["chores"], args.id, "chore")
    done_on = parse_date(args.date) if args.date else today()
    chore["last_done"] = done_on.isoformat()
    chore["next_due"] = (done_on + timedelta(days=chore["every_days"])).isoformat()
    return {"status": "saved", "chore": chore_view(chore)}


def cmd_chore_skip(store, args):
    chore = find(store["chores"], args.id, "chore")
    chore["next_due"] = (today() + timedelta(days=chore["every_days"])).isoformat()
    return {"status": "saved", "chore": chore_view(chore)}


def cmd_chore_remove(store, args):
    chore = find(store["chores"], args.id, "chore")
    store["chores"].remove(chore)
    return {"status": "deleted", "chore": chore_view(chore)}


def cmd_chore_list(store, args):
    return {"chores": [chore_view(c) for c in sorted(store["chores"], key=lambda c: c["next_due"])]}
