"""Monthly and move-in budget math, the budget commands, and 'can I afford this?'."""
import calendar
from datetime import date, timedelta

from .common import UserError, find, fmt, month_end, month_key, month_start, parse_date, parse_money, parse_month, today

LOW_BUFFER_PERCENT = 10


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


def cmd_budget_trends(store, args):
    if args.months < 1:
        raise UserError("--months must be at least 1.")
    if args.connection is None:
        return {"months": []}
    this_month = today().replace(day=1)
    year, month = this_month.year, this_month.month - (args.months - 1)
    while month < 1:
        year, month = year - 1, month + 12
    period = (f"{year:04d}-{month:02d}-01", month_end(month_key(this_month)).isoformat())
    spending = """FROM purchases
        WHERE status <> 'planned' AND movein_id IS NULL AND date BETWEEN ? AND ?"""
    categories = {}
    for row_month, category, cents in args.connection.execute(
        f"""SELECT substr(date, 1, 7), category, SUM(amount_cents - refunded_cents) {spending}
        GROUP BY 1, 2 ORDER BY 1, 3 DESC, 2""",
        period,
    ):
        categories.setdefault(row_month, []).append({"category": category, "spent": fmt(cents)})
    totals = args.connection.execute(
        f"""WITH monthly AS (
            SELECT substr(date, 1, 7) AS month, SUM(amount_cents - refunded_cents) AS total {spending}
            GROUP BY 1
        )
        SELECT month, total, LAG(month) OVER (ORDER BY month), total - LAG(total) OVER (ORDER BY month)
        FROM monthly ORDER BY month""",
        period,
    ).fetchall()
    return {
        "months": [
            {
                "month": row_month,
                "total": fmt(total),
                "previous_month_with_spending": previous,
                "change": fmt(change),
                "by_category": categories[row_month],
            }
            for row_month, total, previous, change in totals
        ]
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


def current_budget(store, when):
    month = when[:7] if when else month_key(today())
    return position_view(monthly_position(store, month))


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
