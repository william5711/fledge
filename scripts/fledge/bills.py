"""Recurring and one-time bills. Paying one saves a purchase."""
from .budget import current_budget, following_due, purchase_view
from .common import UserError, find, fmt, new_id, parse_date, parse_money, today


def bill_view(bill):
    return {
        "id": bill["id"],
        "name": bill["name"],
        "amount": fmt(bill["amount_cents"]),
        "frequency": bill["frequency"],
        "next_due": bill["next_due"],
        "active": bill["active"],
    }


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
