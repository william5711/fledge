"""The grocery list and the kitchen inventory it feeds."""
from datetime import timedelta

from .budget import current_budget, purchase_view
from .common import find, fmt, new_id, parse_date, parse_money, today
from .purchases import planned_match_decision

USE_SOON_DAYS = 3


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
