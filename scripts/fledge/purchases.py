"""Logging, buying, refunding, editing, and deleting purchases."""
from .budget import current_budget, net_cents, purchase_view
from .common import UserError, find, fmt, new_id, parse_date, parse_money, parse_month, similar, today


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
