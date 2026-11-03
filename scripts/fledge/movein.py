"""The move-in checklist. Its costs come from a separate fund, not the monthly budget."""
from datetime import timedelta

from .budget import movein_position, net_cents
from .common import UserError, find, fmt, new_id, parse_date, parse_money, today
from .purchases import mark_bought


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
