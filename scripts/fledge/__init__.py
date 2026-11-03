"""Data store and calculations for the fledge skill.

Every command prints JSON. Money is stored as whole cents so totals never pick
up floating point errors. Data lives in a SQLite database at ~/.fledge/fledge.db,
or at the path set by FLEDGE_DATA.

Modules:
    common     parsing, formatting, and errors shared by every command
    storage    the SQLite schema, migrations, and reading and writing records
    budget     monthly and move-in budget math, budget commands, and afford
    purchases  logging, buying, refunding, and editing purchases
    bills      recurring and one-time bills
    movein     the move-in checklist and fund
    agenda     events, reminders, and deadlines
    schedule   classes, work shifts, and busy days
    shared     roommate expenses and balances
    kitchen    the grocery list and kitchen inventory
    chores     recurring chores
    reports    the daily briefing, weekly review, and export
    cli        the command parser and the transaction each command runs in
"""
