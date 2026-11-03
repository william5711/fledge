"""The command parser, and the transaction each command runs in."""
import argparse
import copy
import json
import sqlite3

from . import storage
from .agenda import DEADLINE_TYPES, cmd_agenda_add, cmd_agenda_done, cmd_agenda_import, cmd_agenda_list, cmd_agenda_remove
from .bills import cmd_bill_add, cmd_bill_edit, cmd_bill_list, cmd_bill_pay, cmd_bill_remove
from .budget import cmd_afford, cmd_budget_set, cmd_budget_summary, cmd_budget_trends
from .chores import cmd_chore_add, cmd_chore_done, cmd_chore_list, cmd_chore_remove, cmd_chore_skip, cmd_chore_starter
from .common import UserError
from .kitchen import (
    cmd_grocery_add, cmd_grocery_list, cmd_grocery_remove, cmd_grocery_trip,
    cmd_kitchen_add, cmd_kitchen_list, cmd_kitchen_notes, cmd_kitchen_remove,
)
from .movein import (
    HOUSING_TASKS, cmd_movein_add, cmd_movein_cost, cmd_movein_done, cmd_movein_init,
    cmd_movein_remove, cmd_movein_reschedule, cmd_movein_status,
)
from .purchases import (
    cmd_purchase_add, cmd_purchase_buy, cmd_purchase_delete, cmd_purchase_edit, cmd_purchase_list, cmd_purchase_refund,
)
from .reports import cmd_briefing, cmd_export, cmd_review
from .schedule import cmd_schedule_add, cmd_schedule_list, cmd_schedule_remove, cmd_week
from .shared import cmd_shared_add, cmd_shared_delete, cmd_shared_settle, cmd_shared_status


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
    command = add_command(budget, "trends", cmd_budget_trends, writes=False)
    command.add_argument("--months", type=int, default=6)
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
    command.add_argument("--rows", help="JSON list of deadlines. Read from stdin if left out.")
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

    command = top.add_parser("export")
    command.set_defaults(handler=cmd_export, writes=False)

    command = top.add_parser("briefing")
    command.set_defaults(handler=cmd_briefing, writes=False)
    command.add_argument("--date")
    return parser


def execute(args):
    """Run one command. The command line and the MCP server both use this."""
    path = storage.data_path()
    if not args.writes and not path.exists() and not path.with_name("data.json").exists():
        args.connection = None  # A read with nothing to import shouldn't create a file.
        return args.handler(storage.empty_store(), args)
    connection = None
    try:
        connection = storage.open_database(path)
        needs_migration = storage.database_version(connection) < len(storage.MIGRATIONS)
        # Taking the write lock before reading means two processes can't both
        # read the same data and then overwrite each other's changes.
        connection.execute("BEGIN IMMEDIATE" if args.writes or needs_migration else "BEGIN")
        storage.migrate(connection)
        store = storage.read_store(connection)
        original = copy.deepcopy(store)
        args.connection = connection
        result = args.handler(store, args)
        if args.writes:
            storage.write_store(connection, store, original)
        connection.execute("COMMIT")
        return result
    except sqlite3.DatabaseError as error:
        raise UserError(f"{path} could not be used as a Fledge database: {error}. No changes were saved.")
    finally:
        if connection is not None:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()


def main():
    args = build_parser().parse_args()
    try:
        result = execute(args)
    except UserError as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0
