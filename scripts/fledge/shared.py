"""Roommate expenses. The budget counts only the student's own share."""
from .budget import current_budget
from .common import UserError, find, fmt, new_id, parse_date, parse_money, today
from .purchases import planned_match_decision


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
    if shares.get("me") and not args.ledger_only:
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
