"""Parsing, formatting, and lookups that every command uses."""
import calendar
import os
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

STOP_WORDS = {"a", "an", "the", "my", "new", "some", "for"}


class UserError(Exception):
    pass


def new_id(store, prefix):
    number = store["next_id"][prefix]
    store["next_id"][prefix] = number + 1
    return f"{prefix}{number}"


def find(items, item_id, label):
    for item in items:
        if item["id"] == item_id:
            return item
    raise UserError(f"No {label} with id {item_id}.")


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
