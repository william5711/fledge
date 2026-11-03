"""The SQLite schema, migrations, and moving records between the database and a store.

Commands work on a store: a dict of lists that mirrors the tables. execute()
in cli.py reads it, runs one command, and writes back only the rows that changed.
"""
import json
import os
import sqlite3
from pathlib import Path

from .common import UserError


# Each migration runs once, in order, inside the transaction that opens the file.
# To change the schema, add a new list to the end and never edit an old one.
MIGRATIONS = [
    [
        """CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            monthly_budget_cents INTEGER CHECK (monthly_budget_cents > 0),
            movein_fund_cents INTEGER CHECK (movein_fund_cents > 0),
            food_notes TEXT,
            movein_date TEXT,
            movein_housing TEXT CHECK (movein_housing IN ('dorm', 'apartment', 'house-share'))
        )""",
        "INSERT INTO settings (id) VALUES (1)",
        "CREATE TABLE counters (prefix TEXT PRIMARY KEY, next_value INTEGER NOT NULL CHECK (next_value >= 1))",
        """CREATE TABLE bills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            frequency TEXT NOT NULL CHECK (frequency IN ('once', 'weekly', 'monthly', 'yearly')),
            next_due TEXT NOT NULL,
            due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
            active INTEGER NOT NULL CHECK (active IN (0, 1))
        )""",
        """CREATE TABLE movein_tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            due TEXT,
            done INTEGER NOT NULL CHECK (done IN (0, 1)),
            category TEXT,
            purchase_id TEXT REFERENCES purchases (id) DEFERRABLE INITIALLY DEFERRED
        )""",
        """CREATE TABLE shared_expenses (
            id TEXT PRIMARY KEY,
            item TEXT NOT NULL,
            total_cents INTEGER NOT NULL CHECK (total_cents > 0),
            paid_by TEXT NOT NULL,
            date TEXT NOT NULL,
            purchase_id TEXT REFERENCES purchases (id) DEFERRABLE INITIALLY DEFERRED
        )""",
        """CREATE TABLE shared_shares (
            expense_id TEXT NOT NULL REFERENCES shared_expenses (id) ON DELETE CASCADE,
            person TEXT NOT NULL,
            cents INTEGER NOT NULL CHECK (cents >= 0),
            PRIMARY KEY (expense_id, person)
        )""",
        """CREATE TABLE settlements (
            id TEXT PRIMARY KEY,
            payer TEXT NOT NULL,
            receiver TEXT NOT NULL,
            cents INTEGER NOT NULL CHECK (cents > 0),
            date TEXT NOT NULL,
            CHECK (payer <> receiver)
        )""",
        """CREATE TABLE purchases (
            id TEXT PRIMARY KEY,
            item TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            refunded_cents INTEGER NOT NULL DEFAULT 0,
            category TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('planned', 'bought', 'refunded')),
            date TEXT,
            bill_id TEXT REFERENCES bills (id) DEFERRABLE INITIALLY DEFERRED,
            bill_paid_due TEXT,
            movein_id TEXT REFERENCES movein_tasks (id) DEFERRABLE INITIALLY DEFERRED,
            shared_id TEXT REFERENCES shared_expenses (id) DEFERRABLE INITIALLY DEFERRED,
            CHECK (refunded_cents BETWEEN 0 AND amount_cents),
            CHECK (status = 'planned' OR date IS NOT NULL)
        )""",
        """CREATE TABLE agenda (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('event', 'reminder', 'deadline')),
            title TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT,
            done INTEGER NOT NULL CHECK (done IN (0, 1)),
            course TEXT,
            type TEXT CHECK (type IN ('exam', 'quiz', 'assignment', 'project', 'admin'))
        )""",
        """CREATE TABLE schedule (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('class', 'work')),
            title TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            first_date TEXT,
            last_date TEXT
        )""",
        """CREATE TABLE schedule_days (
            block_id TEXT NOT NULL REFERENCES schedule (id) ON DELETE CASCADE,
            day TEXT NOT NULL CHECK (day IN ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')),
            PRIMARY KEY (block_id, day)
        )""",
        """CREATE TABLE groceries (
            id TEXT PRIMARY KEY,
            item TEXT NOT NULL,
            quantity TEXT,
            estimate_cents INTEGER CHECK (estimate_cents > 0)
        )""",
        "CREATE TABLE kitchen (id TEXT PRIMARY KEY, item TEXT NOT NULL, quantity TEXT, expires TEXT)",
        """CREATE TABLE chores (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            every_days INTEGER NOT NULL CHECK (every_days >= 1),
            next_due TEXT NOT NULL,
            last_done TEXT
        )""",
        "CREATE INDEX purchases_by_date ON purchases (date, category)",
        "CREATE INDEX agenda_by_date ON agenda (date)",
    ],
]

# Maps each table to where its rows live in the store the commands work with.
STORE_PATHS = {
    "bills": ("bills",),
    "movein_tasks": ("movein", "tasks"),
    "shared_expenses": ("shared", "expenses"),
    "settlements": ("shared", "settlements"),
    "purchases": ("purchases",),
    "agenda": ("agenda",),
    "schedule": ("schedule",),
    "groceries": ("groceries",),
    "kitchen": ("kitchen",),
    "chores": ("chores",),
}
# Store key to column name, for each table.
TABLE_COLUMNS = {
    "bills": {k: k for k in ("id", "name", "amount_cents", "frequency", "next_due", "due_day", "active")},
    "movein_tasks": {k: k for k in ("id", "title", "due", "done", "category", "purchase_id")},
    "shared_expenses": {k: k for k in ("id", "item", "total_cents", "paid_by", "date", "purchase_id")},
    "settlements": {"id": "id", "from": "payer", "to": "receiver", "cents": "cents", "date": "date"},
    "purchases": {k: k for k in (
        "id", "item", "amount_cents", "refunded_cents", "category", "status", "date",
        "bill_id", "bill_paid_due", "movein_id", "shared_id",
    )},
    "agenda": {k: k for k in ("id", "kind", "title", "date", "time", "done", "course", "type")},
    "schedule": {
        "id": "id", "kind": "kind", "title": "title", "start": "start_time",
        "end": "end_time", "from": "first_date", "until": "last_date",
    },
    "groceries": {k: k for k in ("id", "item", "quantity", "estimate_cents")},
    "kitchen": {k: k for k in ("id", "item", "quantity", "expires")},
    "chores": {k: k for k in ("id", "title", "every_days", "next_due", "last_done")},
}
BOOLEAN_COLUMNS = {"bills": {"active"}, "movein_tasks": {"done"}, "agenda": {"done"}}


def data_path():
    configured = os.environ.get("FLEDGE_DATA")
    return Path(configured or "~/.fledge/fledge.db").expanduser()


def empty_store():
    return {
        "version": 3,
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


def open_database(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Budgets are private, so only the owner can read the file.
        os.close(os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600))
    # isolation_level=None means this code controls each transaction itself.
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def database_version(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0]


def migrate(connection):
    version = database_version(connection)
    if version > len(MIGRATIONS):
        raise UserError("This data file came from a newer version of Fledge. Update Fledge to use it.")
    for number in range(version, len(MIGRATIONS)):
        for statement in MIGRATIONS[number]:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {number + 1}")
    if version == 0:
        import_old_json(connection)


def import_old_json(connection):
    """Bring records over from the JSON file that earlier versions used."""
    old_file = data_path().with_name("data.json")
    if old_file == data_path() or not old_file.exists():
        return
    try:
        store = json.loads(old_file.read_text(encoding="utf-8"))
    except ValueError:
        raise UserError(f"{old_file} could not be imported because it isn't valid JSON. It was left unchanged.")
    if not isinstance(store, dict):
        raise UserError(f"{old_file} could not be imported because it isn't a tracker file. It was left unchanged.")
    write_store(connection, upgrade_store(store), empty_store())


def read_rows(connection, table):
    columns = TABLE_COLUMNS[table]
    names = ", ".join(columns.values())
    booleans = BOOLEAN_COLUMNS.get(table, set())
    rows = []
    for values in connection.execute(f"SELECT {names} FROM {table} ORDER BY rowid"):
        row = dict(zip(columns, values))
        for key in booleans:
            row[key] = bool(row[key])
        rows.append(row)
    return rows


def rows_in_store(store, table):
    value = store
    for key in STORE_PATHS[table]:
        value = value[key]
    return value


def read_store(connection):
    store = empty_store()
    settings = connection.execute(
        "SELECT monthly_budget_cents, movein_fund_cents, food_notes, movein_date, movein_housing FROM settings"
    ).fetchone()
    store["settings"] = {
        "monthly_budget_cents": settings[0], "movein_fund_cents": settings[1], "food_notes": settings[2],
    }
    store["movein"]["date"], store["movein"]["housing"] = settings[3], settings[4]
    store["next_id"].update(connection.execute("SELECT prefix, next_value FROM counters"))
    for table in STORE_PATHS:
        rows = rows_in_store(store, table)
        rows.extend(read_rows(connection, table))
    days = {}
    for block_id, day in connection.execute("SELECT block_id, day FROM schedule_days ORDER BY rowid"):
        days.setdefault(block_id, []).append(day)
    for block in store["schedule"]:
        block["days"] = days.get(block["id"], [])
    shares = {}
    for expense_id, person, cents in connection.execute(
        "SELECT expense_id, person, cents FROM shared_shares ORDER BY rowid"
    ):
        shares.setdefault(expense_id, {})[person] = cents
    for expense in store["shared"]["expenses"]:
        expense["shares"] = shares.get(expense["id"], {})
    return store


def write_child_rows(connection, table, row):
    if table == "schedule":
        connection.execute("DELETE FROM schedule_days WHERE block_id = ?", (row["id"],))
        connection.executemany(
            "INSERT INTO schedule_days (block_id, day) VALUES (?, ?)", [(row["id"], day) for day in row["days"]]
        )
    elif table == "shared_expenses":
        connection.execute("DELETE FROM shared_shares WHERE expense_id = ?", (row["id"],))
        connection.executemany(
            "INSERT INTO shared_shares (expense_id, person, cents) VALUES (?, ?, ?)",
            [(row["id"], person, cents) for person, cents in row["shares"].items()],
        )


def write_table(connection, table, old_rows, new_rows):
    """Write only the rows a command changed."""
    columns = TABLE_COLUMNS[table]
    names = list(columns.values())
    updates = ", ".join(f"{name} = excluded.{name}" for name in names if name != "id")
    upsert = (
        f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    old_by_id = {row["id"]: row for row in old_rows}
    for row_id in set(old_by_id) - {row["id"] for row in new_rows}:
        connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    for row in new_rows:
        if old_by_id.get(row["id"]) != row:
            values = [row.get("refunded_cents") or 0 if key == "refunded_cents" else row.get(key) for key in columns]
            connection.execute(upsert, values)
            write_child_rows(connection, table, row)


def write_store(connection, store, original):
    settings = store["settings"]
    connection.execute(
        "UPDATE settings SET monthly_budget_cents = ?, movein_fund_cents = ?, food_notes = ?, "
        "movein_date = ?, movein_housing = ?",
        (
            settings["monthly_budget_cents"], settings["movein_fund_cents"], settings["food_notes"],
            store["movein"]["date"], store["movein"]["housing"],
        ),
    )
    connection.executemany(
        "INSERT INTO counters (prefix, next_value) VALUES (?, ?) "
        "ON CONFLICT (prefix) DO UPDATE SET next_value = excluded.next_value",
        list(store["next_id"].items()),
    )
    for table in STORE_PATHS:
        write_table(connection, table, rows_in_store(original, table), rows_in_store(store, table))
