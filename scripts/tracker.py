#!/usr/bin/env python3
"""Data store and calculations for the fledge skill.

Every command prints JSON. Money is stored as whole cents so totals never pick
up floating point errors. Data lives in ~/.fledge/data.json, or in
the path set by FLEDGE_DATA.
"""
import argparse
import calendar
import json
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

STOP_WORDS = {"a", "an", "the", "my", "new", "some", "for"}
LOW_BUFFER_PERCENT = 10
SOON_DAYS = 7
USE_SOON_DAYS = 3
BUSY_LOOKAHEAD_DAYS = 14
BUSY_GRADED_ITEMS = 2
BUSY_SCHEDULED_HOURS = 6
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEADLINE_TYPES = ["exam", "quiz", "assignment", "project", "admin"]
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

# offset is days relative to move-in day. Costs are left blank on purpose:
# prices vary too much by city to present a guess as a real estimate.
COMMON_TASKS = [
    (-60, "Set a move-in budget and savings target", None),
    (-45, "Confirm move-in date, time, and how to get keys", None),
    (-30, "Check renters insurance (a family policy may already cover you)", None),
    (-21, "Update your address with school, bank, and phone plan", None),
    (-14, "Refill prescriptions and pack health insurance details", None),
    (-14, "Buy bedding and towels", "home"),
    (-14, "Buy a first aid kit and basic toolkit", "home"),
    (-7, "Buy cleaning supplies and laundry basics", "home"),
    (-3, "Pack important documents separately from your other boxes", None),
    (0, "Photograph the condition of your room on move-in day", None),
    (7, "Find the nearest urgent care and pharmacy", None),
]
APARTMENT_TASKS = [
    (-45, "Read the lease: rent due date, deposit terms, and move-out rules", None),
    (-30, "Set up electricity, gas, water, and internet", None),
    (-14, "Buy basic kitchen items", "home"),
    (0, "Find the water shutoff and circuit breaker", None),
    (1, "Add rent and utility due dates as bills", None),
]
DORM_TASKS = [
    (-30, "Read the housing guide for what is allowed in your room", None),
    (-21, "Coordinate shared items with your roommate", None),
    (-14, "Check the dorm's bed size before buying bedding", None),
]
HOUSING_TASKS = {
    "apartment": APARTMENT_TASKS,
    "house-share": APARTMENT_TASKS,
    "dorm": DORM_TASKS,
}


class UserError(Exception):
    pass


# --- storage -----------------------------------------------------------------


def data_path():
    configured = os.environ.get("FLEDGE_DATA")
    return Path(configured or "~/.fledge/data.json").expanduser()


def empty_store():
    return {
        "version": 2,
        "settings": {"monthly_budget_cents": None, "movein_fund_cents": None, "food_notes": None},
        "next_id": {"p": 1, "b": 1, "m": 1, "a": 1, "w": 1, "s": 1, "t": 1, "g": 1, "k": 1, "c": 1},
        "purchases": [],
        "bills": [],
        "movein": {"date": None, "housing": None, "tasks": []},
        "agenda": [],
        "schedule": [],
        "shared": {"expenses": [], "settlements": []},
        "groceries": [],
        "kitchen": [],
        "chores": [],
    }


def upgrade_store(store):
    # Older data files keep their records and gain any newer sections.
    defaults = empty_store()
    for key, value in defaults.items():
        store.setdefault(key, value)
    for section in ("settings", "next_id"):
        for key, value in defaults[section].items():
            store[section].setdefault(key, value)
    store["version"] = defaults["version"]
    return store


def load_store():
    path = data_path()
    if not path.exists():
        return empty_store()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise UserError(f"{path} is not valid JSON. It was left unchanged.")
    if not isinstance(store, dict):
        raise UserError(f"{path} is not a tracker data file. It was left unchanged.")
    return upgrade_store(store)


def save_store(store):
    path = data_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Write to a temp file first so a crash can't leave a half-written budget.
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            json.dump(store, temp_file, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def new_id(store, prefix):
    number = store["next_id"][prefix]
    store["next_id"][prefix] = number + 1
    return f"{prefix}{number}"


def find(items, item_id, label):
    for item in items:
        if item["id"] == item_id:
            return item
    raise UserError(f"No {label} with id {item_id}.")


# --- parsing and formatting --------------------------------------------------


def parse_money(text):
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise UserError(f"'{text}' is not a valid amount.")
    if not amount.is_finite() or amount <= 0:
        raise UserError("Amounts must be greater than zero.")
    cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents == 0:
        raise UserError("Amounts must be at least one cent.")
    return cents


def fmt(cents):
    if cents is None:
        return None
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{part:02d}"


def parse_date(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UserError(f"'{text}' is not a date. Use YYYY-MM-DD.")


def parse_time(text):
    if text is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise UserError(f"'{text}' is not a time. Use 24-hour HH:MM.")
    return text


def today():
    override = os.environ.get("FLEDGE_TODAY")
    return parse_date(override) if override else date.today()


def month_key(day):
    return day.strftime("%Y-%m")


def parse_month(text):
    if text is None:
        return month_key(today())
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise UserError(f"'{text}' is not a month. Use YYYY-MM.")
    return text


def month_end(month):
    year, number = int(month[:4]), int(month[5:])
    return date(year, number, calendar.monthrange(year, number)[1])


def month_start(month):
    return date(int(month[:4]), int(month[5:]), 1)


def words(text):
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP_WORDS


def similar(first, second):
    first_words, second_words = words(first), words(second)
    if not first_words or not second_words:
        return False
    return first_words <= second_words or second_words <= first_words


# --- views -------------------------------------------------------------------


def net_cents(purchase):
    return purchase["amount_cents"] - purchase.get("refunded_cents", 0)


def purchase_view(purchase):
    view = {
        "id": purchase["id"],
        "item": purchase["item"],
        "amount": fmt(purchase["amount_cents"]),
        "category": purchase["category"],
        "status": purchase["status"],
        "date": purchase["date"],
    }
    if purchase.get("refunded_cents"):
        view["refunded"] = fmt(purchase["refunded_cents"])
    if purchase.get("movein_id"):
        view["movein_task"] = purchase["movein_id"]
    if purchase.get("bill_id"):
        view["bill"] = purchase["bill_id"]
    return view


def bill_view(bill):
    return {
        "id": bill["id"],
        "name": bill["name"],
        "amount": fmt(bill["amount_cents"]),
        "frequency": bill["frequency"],
        "next_due": bill["next_due"],
        "active": bill["active"],
    }


def task_view(store, task):
    view = {
        "id": task["id"],
        "title": task["title"],
        "due": task["due"],
        "done": task["done"],
        "category": task.get("category"),
        "cost": None,
    }
    if task.get("purchase_id"):
        view["cost"] = fmt(net_cents(find(store["purchases"], task["purchase_id"], "purchase")))
    return view


# --- budget calculations -----------------------------------------------------


def following_due(bill, due):
    if bill["frequency"] == "once":
        return None
    if bill["frequency"] == "weekly":
        return due + timedelta(days=7)
    if bill["frequency"] == "monthly":
        year, month = (due.year + 1, 1) if due.month == 12 else (due.year, due.month + 1)
    else:
        year, month = due.year + 1, due.month
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(bill["due_day"], last_day))


def bill_occurrences(bill, end):
    if not bill["active"]:
        return
    due = parse_date(bill["next_due"])
    while due is not None and due <= end:
        yield due
        due = following_due(bill, due)


def monthly_position(store, month, exclude_purchase_id=None):
    current = month_key(today())
    start = month_start(month)
    end = month_end(month)

    spent = sum(
        net_cents(p)
        for p in store["purchases"]
        if p["status"] != "planned"
        and not p.get("movein_id")
        and p["date"][:7] == month
    )

    bills_remaining = 0
    planned = 0
    if month >= current:
        # Overdue bills and past-dated plans are still owed, so they count
        # against the current month. A future month only counts its own items.
        for bill in store["bills"]:
            for due in bill_occurrences(bill, end):
                if month == current or due >= start:
                    bills_remaining += bill["amount_cents"]
        for p in store["purchases"]:
            if p["status"] != "planned" or p.get("movein_id") or p["id"] == exclude_purchase_id:
                continue
            if p["date"] is None:
                planned += p["amount_cents"] if month == current else 0
            elif p["date"][:7] == month or (month == current and p["date"][:7] < month):
                planned += p["amount_cents"]

    budget = store["settings"]["monthly_budget_cents"]
    remaining = None if budget is None else budget - spent - bills_remaining - planned
    return {
        "month": month,
        "budget_cents": budget,
        "spent_cents": spent,
        "bills_remaining_cents": bills_remaining,
        "planned_cents": planned,
        "remaining_cents": remaining,
    }


def position_view(position):
    return {
        "month": position["month"],
        "budget": fmt(position["budget_cents"]),
        "spent": fmt(position["spent_cents"]),
        "bills_still_due": fmt(position["bills_remaining_cents"]),
        "planned": fmt(position["planned_cents"]),
        "left_to_spend": fmt(position["remaining_cents"]),
    }


def movein_position(store, exclude_purchase_id=None):
    linked = [p for p in store["purchases"] if p.get("movein_id")]
    spent = sum(net_cents(p) for p in linked if p["status"] != "planned")
    planned = sum(
        p["amount_cents"]
        for p in linked
        if p["status"] == "planned" and p["id"] != exclude_purchase_id
    )
    fund = store["settings"]["movein_fund_cents"]
    return {
        "fund_cents": fund,
        "spent_cents": spent,
        "planned_cents": planned,
        "remaining_cents": None if fund is None else fund - spent - planned,
    }


def verdict(after_cents, pool_cents):
    if after_cents < 0:
        return "no"
    if after_cents * 100 < pool_cents * LOW_BUFFER_PERCENT:
        return "tight"
    return "yes"


# --- budget and purchases ----------------------------------------------------


def cmd_budget_set(store, args):
    if args.monthly is None and args.movein_fund is None:
        raise UserError("Provide --monthly, --movein-fund, or both.")
    if args.monthly is not None:
        store["settings"]["monthly_budget_cents"] = parse_money(args.monthly)
    if args.movein_fund is not None:
        store["settings"]["movein_fund_cents"] = parse_money(args.movein_fund)
    return {
        "status": "saved",
        "monthly_budget": fmt(store["settings"]["monthly_budget_cents"]),
        "movein_fund": fmt(store["settings"]["movein_fund_cents"]),
    }


def cmd_budget_summary(store, args):
    month = parse_month(args.month)
    categories = {}
    items = []
    for p in store["purchases"]:
        if p["status"] != "planned" and not p.get("movein_id") and p["date"][:7] == month:
            categories[p["category"]] = categories.get(p["category"], 0) + net_cents(p)
            items.append(purchase_view(p))
    ranked = sorted(categories.items(), key=lambda pair: -pair[1])
    return {
        "position": position_view(monthly_position(store, month)),
        "by_category": [{"category": name, "spent": fmt(cents)} for name, cents in ranked],
        "purchases": sorted(items, key=lambda view: view["date"]),
    }


def planned_match_decision(store, item):
    matches = [
        purchase_view(p)
        for p in store["purchases"]
        if p["status"] == "planned" and similar(p["item"], item)
    ]
    if not matches:
        return None
    return {
        "status": "needs_decision",
        "message": "This looks like an existing plan. Nothing was saved.",
        "matches": matches,
        "options": "Use 'purchase buy ID' for the same item, or repeat with --new.",
    }


def block_shared(purchase):
    if purchase.get("shared_id"):
        raise UserError(
            f"{purchase['id']} is your share of expense {purchase['shared_id']}. "
            "Use 'shared delete' and log it again."
        )


def cmd_purchase_add(store, args):
    cents = parse_money(args.amount)
    when = parse_date(args.date).isoformat() if args.date else None
    if args.status == "bought" and when is None:
        when = today().isoformat()
    if not args.new:
        decision = planned_match_decision(store, args.item)
        if decision:
            return decision
    purchase = {
        "id": new_id(store, "p"),
        "item": args.item,
        "amount_cents": cents,
        "category": args.category.strip().lower(),
        "status": args.status,
        "date": when,
    }
    store["purchases"].append(purchase)
    return {"status": "saved", "purchase": purchase_view(purchase), "budget": current_budget(store, when)}


def current_budget(store, when):
    month = when[:7] if when else month_key(today())
    return position_view(monthly_position(store, month))


def mark_bought(store, purchase, amount, when):
    if purchase["status"] != "planned":
        raise UserError(f"{purchase['id']} is already {purchase['status']}.")
    if amount is not None:
        purchase["amount_cents"] = parse_money(amount)
    purchase["status"] = "bought"
    purchase["date"] = parse_date(when).isoformat() if when else today().isoformat()
    if purchase.get("movein_id"):
        task = find(store["movein"]["tasks"], purchase["movein_id"], "move-in task")
        task["done"] = True


def cmd_purchase_buy(store, args):
    purchase = find(store["purchases"], args.id, "purchase")
    mark_bought(store, purchase, args.amount, args.date)
    return {"status": "saved", "purchase": purchase_view(purchase), "budget": current_budget(store, purchase["date"])}


def cmd_purchase_refund(store, args):
    purchase = find(store["purchases"], args.id, "purchase")
    block_shared(purchase)
    if purchase["status"] != "bought":
        raise UserError(f"Only bought items can be refunded. {purchase['id']} is {purchase['status']}.")
    available = net_cents(purchase)
    cents = parse_money(args.amount) if args.amount else available
    if cents > available:
        raise UserError(f"The most that can be refunded is {fmt(available)}.")
    purchase["refunded_cents"] = purchase.get("refunded_cents", 0) + cents
    if net_cents(purchase) == 0:
        purchase["status"] = "refunded"
    return {"status": "saved", "purchase": purchase_view(purchase), "budget": current_budget(store, purchase["date"])}


def cmd_purchase_edit(store, args):
    purchase = find(store["purchases"], args.id, "purchase")
    if args.amount or args.date:
        block_shared(purchase)
    if args.item:
        purchase["item"] = args.item
    if args.category:
        purchase["category"] = args.category.strip().lower()
    if args.date:
        purchase["date"] = parse_date(args.date).isoformat()
    if args.amount:
        cents = parse_money(args.amount)
        if cents < purchase.get("refunded_cents", 0):
            raise UserError("The amount can't be lower than what was already refunded.")
        purchase["amount_cents"] = cents
    return {"status": "saved", "purchase": purchase_view(purchase), "budget": current_budget(store, purchase["date"])}


def cmd_purchase_delete(store, args):
    purchase = find(store["purchases"], args.id, "purchase")
    block_shared(purchase)
    store["purchases"].remove(purchase)
    if purchase.get("movein_id"):
        task = find(store["movein"]["tasks"], purchase["movein_id"], "move-in task")
        task["purchase_id"] = None
        task["done"] = False
    if purchase.get("bill_id"):
        # Deleting a payment means the bill is owed again.
        for bill in store["bills"]:
            if bill["id"] == purchase["bill_id"]:
                bill["active"] = True
                bill["next_due"] = min(bill["next_due"], purchase["bill_paid_due"])
    return {"status": "deleted", "purchase": purchase_view(purchase), "budget": current_budget(store, purchase["date"])}


def cmd_purchase_list(store, args):
    month = parse_month(args.month) if args.month else None
    results = []
    for p in store["purchases"]:
        if args.status and p["status"] != args.status:
            continue
        if month and (p["date"] or "")[:7] != month:
            continue
        results.append(purchase_view(p))
    return {"purchases": results}


def cmd_afford(store, args):
    cents = parse_money(args.amount)
    day = parse_date(args.date) if args.date else today()
    month = month_key(day)
    if month < month_key(today()):
        raise UserError("Affordability checks only work for this month or a future month.")
    exclude = None
    pool = args.pool
    if args.purchase_id:
        purchase = find(store["purchases"], args.purchase_id, "purchase")
        if purchase["status"] != "planned":
            raise UserError(f"{purchase['id']} is already {purchase['status']}.")
        exclude = purchase["id"]
        if purchase.get("movein_id"):
            pool = "movein"

    if pool == "movein":
        position = movein_position(store, exclude)
        if position["fund_cents"] is None:
            raise UserError("No move-in fund is set. Use 'budget set --movein-fund'.")
        after = position["remaining_cents"] - cents
        return {
            "pool": "move-in fund",
            "amount": fmt(cents),
            "verdict": verdict(after, position["fund_cents"]),
            "left_before": fmt(position["remaining_cents"]),
            "left_after": fmt(after),
            "fund": fmt(position["fund_cents"]),
            "already_spent": fmt(position["spent_cents"]),
            "other_planned": fmt(position["planned_cents"]),
        }

    position = monthly_position(store, month, exclude)
    if position["budget_cents"] is None:
        raise UserError("No monthly budget is set. Use 'budget set --monthly'.")
    after = position["remaining_cents"] - cents
    end = month_end(month)
    upcoming = sorted(
        ((due, bill) for bill in store["bills"] for due in bill_occurrences(bill, end)),
        key=lambda pair: pair[0],
    )
    return {
        "pool": "monthly budget",
        "amount": fmt(cents),
        "verdict": verdict(after, position["budget_cents"]),
        "left_before": fmt(position["remaining_cents"]),
        "left_after": fmt(after),
        "position": position_view(position),
        "bills_still_due": [
            {"name": bill["name"], "amount": fmt(bill["amount_cents"]), "due": due.isoformat()}
            for due, bill in upcoming
            if month == month_key(today()) or due >= month_start(month)
        ],
    }


# --- bills -------------------------------------------------------------------


def cmd_bill_add(store, args):
    due = parse_date(args.due)
    bill = {
        "id": new_id(store, "b"),
        "name": args.name,
        "amount_cents": parse_money(args.amount),
        "frequency": args.frequency,
        "next_due": due.isoformat(),
        "due_day": due.day,
        "active": True,
    }
    store["bills"].append(bill)
    return {"status": "saved", "bill": bill_view(bill)}


def cmd_bill_edit(store, args):
    bill = find(store["bills"], args.id, "bill")
    if args.name:
        bill["name"] = args.name
    if args.amount:
        bill["amount_cents"] = parse_money(args.amount)
    if args.due:
        due = parse_date(args.due)
        bill["next_due"] = due.isoformat()
        bill["due_day"] = due.day
    return {"status": "saved", "bill": bill_view(bill)}


def cmd_bill_pay(store, args):
    bill = find(store["bills"], args.id, "bill")
    if not bill["active"]:
        raise UserError(f"{bill['name']} has no payment due.")
    paid_on = parse_date(args.date) if args.date else today()
    due = parse_date(bill["next_due"])
    purchase = {
        "id": new_id(store, "p"),
        "item": bill["name"],
        "amount_cents": parse_money(args.amount) if args.amount else bill["amount_cents"],
        "category": "bills",
        "status": "bought",
        "date": paid_on.isoformat(),
        "bill_id": bill["id"],
        "bill_paid_due": bill["next_due"],
    }
    store["purchases"].append(purchase)
    upcoming = following_due(bill, due)
    if upcoming is None:
        bill["active"] = False
    else:
        bill["next_due"] = upcoming.isoformat()
    return {
        "status": "saved",
        "payment": purchase_view(purchase),
        "bill": bill_view(bill),
        "budget": current_budget(store, purchase["date"]),
    }


def cmd_bill_remove(store, args):
    bill = find(store["bills"], args.id, "bill")
    bill["active"] = False
    return {"status": "stopped", "bill": bill_view(bill)}


def cmd_bill_list(store, args):
    bills = [b for b in store["bills"] if b["active"] or args.all]
    return {"bills": [bill_view(b) for b in sorted(bills, key=lambda b: b["next_due"])]}


# --- move-in planner ---------------------------------------------------------


def add_task(store, title, due, category=None):
    task = {
        "id": new_id(store, "m"),
        "title": title,
        "due": due,
        "done": False,
        "category": category,
        "purchase_id": None,
    }
    store["movein"]["tasks"].append(task)
    return task


def link_cost(store, task, cents, category):
    purchase = {
        "id": new_id(store, "p"),
        "item": task["title"],
        "amount_cents": cents,
        "category": (category or task.get("category") or "home").strip().lower(),
        "status": "planned",
        "date": task["due"],
        "movein_id": task["id"],
    }
    store["purchases"].append(purchase)
    task["purchase_id"] = purchase["id"]
    return purchase


def cmd_movein_init(store, args):
    if store["movein"]["date"]:
        raise UserError("A move-in plan already exists. Use 'movein reschedule' to change the date.")
    move_date = parse_date(args.date)
    store["movein"]["date"] = move_date.isoformat()
    store["movein"]["housing"] = args.housing
    for offset, title, category in COMMON_TASKS + HOUSING_TASKS[args.housing]:
        # A student planning late still sees every task, just due right away.
        due = max(move_date + timedelta(days=offset), today())
        add_task(store, title, due.isoformat(), category)
    store["movein"]["tasks"].sort(key=lambda task: task["due"])
    return cmd_movein_status(store, args)


def cmd_movein_status(store, args):
    plan = store["movein"]
    if not plan["date"]:
        return {"plan": None, "message": "No move-in plan yet."}
    tasks = sorted(plan["tasks"], key=lambda task: task["due"] or "9999")
    position = movein_position(store)
    unpriced = [
        t["id"] for t in tasks if t.get("category") and not t["purchase_id"] and not t["done"]
    ]
    return {
        "move_date": plan["date"],
        "housing": plan["housing"],
        "days_until_move": (parse_date(plan["date"]) - today()).days,
        "tasks_done": sum(1 for t in tasks if t["done"]),
        "tasks_total": len(tasks),
        "money": {
            "fund": fmt(position["fund_cents"]),
            "spent": fmt(position["spent_cents"]),
            "planned": fmt(position["planned_cents"]),
            "projected_total": fmt(position["spent_cents"] + position["planned_cents"]),
            "left_in_fund": fmt(position["remaining_cents"]),
            "purchases_without_a_price": unpriced,
        },
        "tasks": [task_view(store, t) for t in tasks],
    }


def require_plan(store):
    if not store["movein"]["date"]:
        raise UserError("No move-in plan yet. Use 'movein init' first.")


def cmd_movein_add(store, args):
    require_plan(store)
    due = parse_date(args.due).isoformat() if args.due else None
    task = add_task(store, args.title, due, args.category)
    if args.cost:
        link_cost(store, task, parse_money(args.cost), args.category)
    return {"status": "saved", "task": task_view(store, task)}


def cmd_movein_cost(store, args):
    task = find(store["movein"]["tasks"], args.id, "move-in task")
    cents = parse_money(args.amount)
    if task["purchase_id"]:
        purchase = find(store["purchases"], task["purchase_id"], "purchase")
        if purchase["status"] != "planned":
            raise UserError(f"{task['id']} was already bought. Edit purchase {purchase['id']} instead.")
        purchase["amount_cents"] = cents
    else:
        link_cost(store, task, cents, args.category)
    return {"status": "saved", "task": task_view(store, task)}


def cmd_movein_done(store, args):
    task = find(store["movein"]["tasks"], args.id, "move-in task")
    if task["done"]:
        raise UserError(f"{task['id']} is already done.")
    if task["purchase_id"]:
        if not args.actual:
            raise UserError("This task has a planned cost. Provide --actual with what was really paid.")
        purchase = find(store["purchases"], task["purchase_id"], "purchase")
        mark_bought(store, purchase, args.actual, args.date)
    elif args.actual:
        purchase = link_cost(store, task, parse_money(args.actual), args.category)
        mark_bought(store, purchase, None, args.date)
    task["done"] = True
    return {"status": "saved", "task": task_view(store, task)}


def cmd_movein_remove(store, args):
    task = find(store["movein"]["tasks"], args.id, "move-in task")
    if task["purchase_id"]:
        purchase = find(store["purchases"], task["purchase_id"], "purchase")
        if purchase["status"] != "planned":
            raise UserError(f"{task['id']} has a recorded purchase ({purchase['id']}), so it was kept.")
        store["purchases"].remove(purchase)
    store["movein"]["tasks"].remove(task)
    return {"status": "deleted", "task": task["id"]}


def cmd_movein_reschedule(store, args):
    require_plan(store)
    new_date = parse_date(args.date)
    shift = new_date - parse_date(store["movein"]["date"])
    store["movein"]["date"] = new_date.isoformat()
    for task in store["movein"]["tasks"]:
        if task["done"] or not task["due"]:
            continue
        due = max(parse_date(task["due"]) + shift, today())
        task["due"] = due.isoformat()
        if task["purchase_id"]:
            find(store["purchases"], task["purchase_id"], "purchase")["date"] = task["due"]
    return cmd_movein_status(store, args)


# --- agenda and briefing -----------------------------------------------------


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
        rows = json.load(sys.stdin)
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


# --- classes, shifts, and workload -------------------------------------------


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


# --- roommate expenses -------------------------------------------------------


def known_people(store):
    names = set()
    for expense in store["shared"]["expenses"]:
        names.add(expense["paid_by"])
        names.update(expense["shares"])
    for settlement in store["shared"]["settlements"]:
        names.update((settlement["from"], settlement["to"]))
    return names


def person_name(store, text):
    name = text.strip()
    if not name:
        raise UserError("A name can't be blank.")
    if name.casefold() == "me":
        return "me"
    for known in known_people(store):
        if known.casefold() == name.casefold():
            return known
    return name


def split_total(store, total, split, shares):
    if bool(split) == bool(shares):
        raise UserError("Provide either --split or --shares.")
    result = {}
    if split:
        names = [person_name(store, part) for part in split.split(",") if part.strip()]
        if not names:
            raise UserError("--split needs at least one name.")
        # Hand out leftover cents one at a time so the shares add up exactly.
        each, extra = divmod(total, len(names))
        for index, name in enumerate(names):
            if name in result:
                raise UserError(f"{name} is listed twice.")
            result[name] = each + (1 if index < extra else 0)
        return result
    for part in shares.split(","):
        name, separator, amount = part.partition("=")
        if not separator:
            raise UserError("Write --shares like me=20,Alex=40.")
        name = person_name(store, name)
        if name in result:
            raise UserError(f"{name} is listed twice.")
        result[name] = parse_money(amount)
    if sum(result.values()) != total:
        raise UserError(
            f"The shares add up to {fmt(sum(result.values()))}, but the total is {fmt(total)}."
        )
    return result


def net_balances(store):
    net = {}
    for expense in store["shared"]["expenses"]:
        net[expense["paid_by"]] = net.get(expense["paid_by"], 0) + expense["total_cents"]
        for name, cents in expense["shares"].items():
            net[name] = net.get(name, 0) - cents
    for settlement in store["shared"]["settlements"]:
        net[settlement["from"]] = net.get(settlement["from"], 0) + settlement["cents"]
        net[settlement["to"]] = net.get(settlement["to"], 0) - settlement["cents"]
    return net


def simplified_debts(net):
    creditors = sorted(([n, c] for n, c in net.items() if c > 0), key=lambda p: (-p[1], p[0]))
    debtors = sorted(([n, -c] for n, c in net.items() if c < 0), key=lambda p: (-p[1], p[0]))
    debts = []
    while creditors and debtors:
        creditor, debtor = creditors[0], debtors[0]
        paid = min(creditor[1], debtor[1])
        debts.append({"from": debtor[0], "to": creditor[0], "amount": fmt(paid)})
        creditor[1] -= paid
        debtor[1] -= paid
        if creditor[1] == 0:
            creditors.pop(0)
        if debtor[1] == 0:
            debtors.pop(0)
    return debts


def balances_view(store):
    net = net_balances(store)
    mine = net.get("me", 0)
    return {
        "you_owe": fmt(max(-mine, 0)),
        "owed_to_you": fmt(max(mine, 0)),
        "who_pays_whom": simplified_debts(net),
    }


def expense_view(expense):
    return {
        "id": expense["id"],
        "item": expense["item"],
        "total": fmt(expense["total_cents"]),
        "paid_by": expense["paid_by"],
        "shares": {name: fmt(cents) for name, cents in expense["shares"].items()},
        "date": expense["date"],
    }


def cmd_shared_add(store, args):
    if not args.new and not args.ledger_only:
        decision = planned_match_decision(store, args.item)
        if decision:
            decision["options"] = "Delete the plan with 'purchase delete ID' and repeat with --new."
            return decision
    total = parse_money(args.amount)
    when = parse_date(args.date).isoformat() if args.date else today().isoformat()
    paid_by = person_name(store, args.paid_by)
    shares = split_total(store, total, args.split, args.shares)
    expense = {
        "id": new_id(store, "s"),
        "item": args.item,
        "total_cents": total,
        "paid_by": paid_by,
        "shares": shares,
        "date": when,
        "purchase_id": None,
    }
    # The budget counts only the student's own share, even when they fronted
    # the whole amount. What roommates owe back shows up in the balances.
    if "me" in shares and not args.ledger_only:
        purchase = {
            "id": new_id(store, "p"),
            "item": f"{args.item} (your share)",
            "amount_cents": shares["me"],
            "category": args.category.strip().lower(),
            "status": "bought",
            "date": when,
            "shared_id": expense["id"],
        }
        store["purchases"].append(purchase)
        expense["purchase_id"] = purchase["id"]
    store["shared"]["expenses"].append(expense)
    return {
        "status": "saved",
        "expense": expense_view(expense),
        "balances": balances_view(store),
        "budget": current_budget(store, when),
    }


def cmd_shared_settle(store, args):
    payer = person_name(store, args.payer)
    receiver = person_name(store, args.receiver)
    if payer == receiver:
        raise UserError("The payer and receiver must be different people.")
    known = known_people(store) | {"me"}
    for name in (payer, receiver):
        if name not in known:
            raise UserError(f"No shared expenses include {name}. Check the spelling.")
    settlement = {
        "id": new_id(store, "t"),
        "from": payer,
        "to": receiver,
        "cents": parse_money(args.amount),
        "date": parse_date(args.date).isoformat() if args.date else today().isoformat(),
    }
    store["shared"]["settlements"].append(settlement)
    return {"status": "saved", "settlement_id": settlement["id"], "balances": balances_view(store)}


def cmd_shared_delete(store, args):
    shared = store["shared"]
    if args.id.startswith("t"):
        shared["settlements"].remove(find(shared["settlements"], args.id, "settlement"))
        return {"status": "deleted", "balances": balances_view(store)}
    expense = find(shared["expenses"], args.id, "shared expense")
    shared["expenses"].remove(expense)
    if expense["purchase_id"]:
        store["purchases"].remove(find(store["purchases"], expense["purchase_id"], "purchase"))
    return {"status": "deleted", "balances": balances_view(store)}


def cmd_shared_status(store, args):
    return {
        "balances": balances_view(store),
        "expenses": [expense_view(e) for e in store["shared"]["expenses"]],
        "settlements": [
            {"id": s["id"], "from": s["from"], "to": s["to"], "amount": fmt(s["cents"]), "date": s["date"]}
            for s in store["shared"]["settlements"]
        ],
    }


# --- groceries and kitchen ---------------------------------------------------


def grocery_view(item):
    return {
        "id": item["id"],
        "item": item["item"],
        "quantity": item["quantity"],
        "estimate": fmt(item["estimate_cents"]),
    }


def kitchen_flag(item, day):
    if not item["expires"]:
        return None
    if item["expires"] < day.isoformat():
        return "expired"
    if item["expires"] <= (day + timedelta(days=USE_SOON_DAYS)).isoformat():
        return "use_soon"
    return None


def kitchen_view(item, day):
    return {
        "id": item["id"],
        "item": item["item"],
        "quantity": item["quantity"],
        "expires": item["expires"],
        "flag": kitchen_flag(item, day),
    }


def kitchen_sorted(store):
    return sorted(store["kitchen"], key=lambda k: (k["expires"] or "9999", k["item"].casefold()))


def stock_kitchen(store, name, quantity, expires):
    for item in store["kitchen"]:
        if item["item"].casefold() == name.casefold():
            item["quantity"] = quantity or item["quantity"]
            item["expires"] = expires or item["expires"]
            return item
    item = {"id": new_id(store, "k"), "item": name, "quantity": quantity, "expires": expires}
    store["kitchen"].append(item)
    return item


def grocery_list_view(store):
    return {
        "items": [grocery_view(g) for g in store["groceries"]],
        "estimated_total": fmt(sum(g["estimate_cents"] or 0 for g in store["groceries"])),
        "items_without_estimate": sum(1 for g in store["groceries"] if g["estimate_cents"] is None),
    }


def cmd_grocery_add(store, args):
    estimate = parse_money(args.estimate) if args.estimate else None
    for item in store["groceries"]:
        if item["item"].casefold() == args.item.casefold():
            item["quantity"] = args.quantity or item["quantity"]
            item["estimate_cents"] = estimate if estimate is not None else item["estimate_cents"]
            return {"status": "updated", "item": grocery_view(item), "list": grocery_list_view(store)}
    item = {
        "id": new_id(store, "g"),
        "item": args.item,
        "quantity": args.quantity,
        "estimate_cents": estimate,
    }
    store["groceries"].append(item)
    return {"status": "saved", "item": grocery_view(item), "list": grocery_list_view(store)}


def cmd_grocery_remove(store, args):
    item = find(store["groceries"], args.id, "grocery item")
    store["groceries"].remove(item)
    return {"status": "deleted", "list": grocery_list_view(store)}


def cmd_grocery_list(store, args):
    return grocery_list_view(store)


def cmd_grocery_trip(store, args):
    if not args.new:
        decision = planned_match_decision(store, "Groceries")
        if decision:
            decision["options"] = "Delete the plan with 'purchase delete ID' and repeat with --new."
            return decision
    total = parse_money(args.total)
    when = parse_date(args.date).isoformat() if args.date else today().isoformat()
    if args.ids:
        ids = [part.strip() for part in args.ids.split(",") if part.strip()]
        bought = [find(store["groceries"], item_id, "grocery item") for item_id in ids]
    else:
        bought = list(store["groceries"])
    purchase = {
        "id": new_id(store, "p"),
        "item": "Groceries",
        "amount_cents": total,
        "category": "groceries",
        "status": "bought",
        "date": when,
    }
    store["purchases"].append(purchase)
    stocked = []
    for item in bought:
        store["groceries"].remove(item)
        stocked.append(stock_kitchen(store, item["item"], item["quantity"], None))
    day = today()
    return {
        "status": "saved",
        "purchase": purchase_view(purchase),
        "moved_to_kitchen": [kitchen_view(k, day) for k in stocked],
        "still_on_list": [grocery_view(g) for g in store["groceries"]],
        "budget": current_budget(store, when),
    }


def cmd_kitchen_add(store, args):
    expires = parse_date(args.expires).isoformat() if args.expires else None
    item = stock_kitchen(store, args.item, args.quantity, expires)
    return {"status": "saved", "item": kitchen_view(item, today())}


def cmd_kitchen_remove(store, args):
    item = find(store["kitchen"], args.id, "kitchen item")
    store["kitchen"].remove(item)
    return {"status": "deleted", "item": kitchen_view(item, today())}


def cmd_kitchen_list(store, args):
    day = today()
    return {
        "food_notes": store["settings"]["food_notes"],
        "items": [kitchen_view(k, day) for k in kitchen_sorted(store)],
    }


def cmd_kitchen_notes(store, args):
    store["settings"]["food_notes"] = args.notes.strip() or None
    return {"status": "saved", "food_notes": store["settings"]["food_notes"]}


# --- chores ------------------------------------------------------------------


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


# --- weekly review -----------------------------------------------------------


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


# --- command line ------------------------------------------------------------


def add_command(group, name, handler, writes=True):
    command = group.add_parser(name)
    command.set_defaults(handler=handler, writes=writes)
    return command


def build_parser():
    parser = argparse.ArgumentParser(description="Fledge tracker")
    top = parser.add_subparsers(dest="area", required=True)

    budget = top.add_parser("budget").add_subparsers(dest="action", required=True)
    command = add_command(budget, "set", cmd_budget_set)
    command.add_argument("--monthly")
    command.add_argument("--movein-fund")
    command = add_command(budget, "summary", cmd_budget_summary, writes=False)
    command.add_argument("--month")

    purchase = top.add_parser("purchase").add_subparsers(dest="action", required=True)
    command = add_command(purchase, "add", cmd_purchase_add)
    command.add_argument("--item", required=True)
    command.add_argument("--amount", required=True)
    command.add_argument("--category", default="other")
    command.add_argument("--status", choices=["planned", "bought"], default="bought")
    command.add_argument("--date")
    command.add_argument("--new", action="store_true", help="Save even if it resembles a planned item")
    command = add_command(purchase, "buy", cmd_purchase_buy)
    command.add_argument("id")
    command.add_argument("--amount")
    command.add_argument("--date")
    command = add_command(purchase, "refund", cmd_purchase_refund)
    command.add_argument("id")
    command.add_argument("--amount")
    command = add_command(purchase, "edit", cmd_purchase_edit)
    command.add_argument("id")
    command.add_argument("--item")
    command.add_argument("--amount")
    command.add_argument("--category")
    command.add_argument("--date")
    command = add_command(purchase, "delete", cmd_purchase_delete)
    command.add_argument("id")
    command = add_command(purchase, "list", cmd_purchase_list, writes=False)
    command.add_argument("--status", choices=["planned", "bought", "refunded"])
    command.add_argument("--month")

    command = top.add_parser("afford")
    command.set_defaults(handler=cmd_afford, writes=False)
    command.add_argument("--amount", required=True)
    command.add_argument("--date")
    command.add_argument("--pool", choices=["monthly", "movein"], default="monthly")
    command.add_argument("--purchase-id", help="Planned purchase being checked, so it isn't counted twice")

    bill = top.add_parser("bill").add_subparsers(dest="action", required=True)
    command = add_command(bill, "add", cmd_bill_add)
    command.add_argument("--name", required=True)
    command.add_argument("--amount", required=True)
    command.add_argument("--due", required=True)
    command.add_argument("--frequency", choices=["once", "weekly", "monthly", "yearly"], default="monthly")
    command = add_command(bill, "edit", cmd_bill_edit)
    command.add_argument("id")
    command.add_argument("--name")
    command.add_argument("--amount")
    command.add_argument("--due")
    command = add_command(bill, "pay", cmd_bill_pay)
    command.add_argument("id")
    command.add_argument("--amount")
    command.add_argument("--date")
    command = add_command(bill, "remove", cmd_bill_remove)
    command.add_argument("id")
    command = add_command(bill, "list", cmd_bill_list, writes=False)
    command.add_argument("--all", action="store_true")

    movein = top.add_parser("movein").add_subparsers(dest="action", required=True)
    command = add_command(movein, "init", cmd_movein_init)
    command.add_argument("--date", required=True)
    command.add_argument("--housing", choices=sorted(HOUSING_TASKS), required=True)
    add_command(movein, "status", cmd_movein_status, writes=False)
    command = add_command(movein, "add", cmd_movein_add)
    command.add_argument("--title", required=True)
    command.add_argument("--due")
    command.add_argument("--cost")
    command.add_argument("--category")
    command = add_command(movein, "cost", cmd_movein_cost)
    command.add_argument("id")
    command.add_argument("amount")
    command.add_argument("--category")
    command = add_command(movein, "done", cmd_movein_done)
    command.add_argument("id")
    command.add_argument("--actual")
    command.add_argument("--date")
    command.add_argument("--category")
    command = add_command(movein, "remove", cmd_movein_remove)
    command.add_argument("id")
    command = add_command(movein, "reschedule", cmd_movein_reschedule)
    command.add_argument("--date", required=True)

    agenda = top.add_parser("agenda").add_subparsers(dest="action", required=True)
    command = add_command(agenda, "add", cmd_agenda_add)
    command.add_argument("--kind", choices=["event", "reminder", "deadline"], required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--date", required=True)
    command.add_argument("--time")
    command.add_argument("--course")
    command.add_argument("--type", choices=DEADLINE_TYPES, help="For deadlines only")
    command = add_command(agenda, "import", cmd_agenda_import)
    command.add_argument("--course")
    command = add_command(agenda, "done", cmd_agenda_done)
    command.add_argument("id")
    command = add_command(agenda, "remove", cmd_agenda_remove)
    command.add_argument("id")
    command = add_command(agenda, "list", cmd_agenda_list, writes=False)
    command.add_argument("--all", action="store_true")
    command.add_argument("--kind", choices=["event", "reminder", "deadline"])
    command.add_argument("--course")

    schedule = top.add_parser("schedule").add_subparsers(dest="action", required=True)
    command = add_command(schedule, "add", cmd_schedule_add)
    command.add_argument("--kind", choices=["class", "work"], required=True)
    command.add_argument("--title", required=True)
    command.add_argument("--days", required=True, help="Comma-separated, like mon,wed")
    command.add_argument("--start", required=True)
    command.add_argument("--end", required=True)
    command.add_argument("--from", dest="start_date")
    command.add_argument("--until")
    command = add_command(schedule, "remove", cmd_schedule_remove)
    command.add_argument("id")
    add_command(schedule, "list", cmd_schedule_list, writes=False)

    command = top.add_parser("week")
    command.set_defaults(handler=cmd_week, writes=False)
    command.add_argument("--date")
    command.add_argument("--weeks", type=int, default=1)

    shared = top.add_parser("shared").add_subparsers(dest="action", required=True)
    command = add_command(shared, "add", cmd_shared_add)
    command.add_argument("--item", required=True)
    command.add_argument("--amount", required=True)
    command.add_argument("--paid-by", required=True)
    command.add_argument("--split", help="Even split, like me,Alex,Sam")
    command.add_argument("--shares", help="Exact amounts, like me=20,Alex=40")
    command.add_argument("--category", default="other")
    command.add_argument("--date")
    command.add_argument("--new", action="store_true", help="Save even if it resembles a planned item")
    command.add_argument("--ledger-only", action="store_true", help="Your share is already in the budget")
    command = add_command(shared, "settle", cmd_shared_settle)
    command.add_argument("--from", dest="payer", required=True)
    command.add_argument("--to", dest="receiver", required=True)
    command.add_argument("--amount", required=True)
    command.add_argument("--date")
    command = add_command(shared, "delete", cmd_shared_delete)
    command.add_argument("id")
    add_command(shared, "status", cmd_shared_status, writes=False)

    grocery = top.add_parser("grocery").add_subparsers(dest="action", required=True)
    command = add_command(grocery, "add", cmd_grocery_add)
    command.add_argument("--item", required=True)
    command.add_argument("--quantity")
    command.add_argument("--estimate")
    command = add_command(grocery, "remove", cmd_grocery_remove)
    command.add_argument("id")
    add_command(grocery, "list", cmd_grocery_list, writes=False)
    command = add_command(grocery, "trip", cmd_grocery_trip)
    command.add_argument("--total", required=True)
    command.add_argument("--ids", help="Items bought, like g1,g2. Leave out if all were bought.")
    command.add_argument("--date")
    command.add_argument("--new", action="store_true", help="Save even if groceries were planned")

    kitchen = top.add_parser("kitchen").add_subparsers(dest="action", required=True)
    command = add_command(kitchen, "add", cmd_kitchen_add)
    command.add_argument("--item", required=True)
    command.add_argument("--quantity")
    command.add_argument("--expires")
    command = add_command(kitchen, "remove", cmd_kitchen_remove)
    command.add_argument("id")
    add_command(kitchen, "list", cmd_kitchen_list, writes=False)
    command = add_command(kitchen, "notes", cmd_kitchen_notes)
    command.add_argument("notes")

    chore = top.add_parser("chore").add_subparsers(dest="action", required=True)
    command = add_command(chore, "add", cmd_chore_add)
    command.add_argument("--title", required=True)
    command.add_argument("--every-days", type=int, required=True)
    command.add_argument("--first-due")
    command = add_command(chore, "starter", cmd_chore_starter)
    command.add_argument("--first-due")
    command = add_command(chore, "done", cmd_chore_done)
    command.add_argument("id")
    command.add_argument("--date")
    command = add_command(chore, "skip", cmd_chore_skip)
    command.add_argument("id")
    command = add_command(chore, "remove", cmd_chore_remove)
    command.add_argument("id")
    add_command(chore, "list", cmd_chore_list, writes=False)

    command = top.add_parser("review")
    command.set_defaults(handler=cmd_review, writes=False)
    command.add_argument("--date")

    command = top.add_parser("briefing")
    command.set_defaults(handler=cmd_briefing, writes=False)
    command.add_argument("--date")
    return parser


def main():
    args = build_parser().parse_args()
    try:
        store = load_store()
        result = args.handler(store, args)
        if args.writes:
            save_store(store)
    except UserError as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
