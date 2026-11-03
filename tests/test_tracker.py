import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TRACKER = Path(__file__).resolve().parent.parent / "scripts" / "tracker.py"


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name) / "nested" / "fledge.db"
        self.today = "2026-11-10"

    def run_cli(self, *args, expect_error=False, stdin=None):
        env = dict(os.environ, FLEDGE_DATA=str(self.data), FLEDGE_TODAY=self.today)
        done = subprocess.run(
            [sys.executable, str(TRACKER), *args], capture_output=True, text=True, env=env, input=stdin
        )
        self.assertEqual(done.returncode, 1 if expect_error else 0, done.stdout + done.stderr)
        return json.loads(done.stdout)

    def add_bought(self, item, amount, *extra):
        return self.run_cli("purchase", "add", "--item", item, "--amount", amount, *extra)

    def test_data_survives_between_runs_and_is_private(self):
        self.add_bought("Coffee", "4.50")
        self.assertEqual(self.run_cli("purchase", "list")["purchases"][0]["item"], "Coffee")
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o600)

    def test_cents_add_up_exactly(self):
        self.run_cli("budget", "set", "--monthly", "1")
        for _ in range(3):
            self.add_bought("Gum", "0.10", "--new")
        position = self.run_cli("budget", "summary")["position"]
        self.assertEqual(position["spent"], "$0.30")
        self.assertEqual(position["left_to_spend"], "$0.70")

    def test_amount_formats(self):
        saved = self.add_bought("Laptop", "$1,200.50")
        self.assertEqual(saved["purchase"]["amount"], "$1,200.50")
        for bad in ("-5", "0", "NaN", "Infinity", "abc", "0.001"):
            self.run_cli("purchase", "add", "--item", "x", "--amount", bad, expect_error=True)

    def test_buying_a_planned_item_does_not_count_twice(self):
        self.run_cli("budget", "set", "--monthly", "500")
        plan = self.add_bought("Desk lamp", "40", "--status", "planned")["purchase"]
        self.assertEqual(self.run_cli("budget", "summary")["position"]["left_to_spend"], "$460.00")

        attempt = self.add_bought("lamp", "35")
        self.assertEqual(attempt["status"], "needs_decision")
        self.assertEqual(attempt["matches"][0]["id"], plan["id"])
        self.assertEqual(len(self.run_cli("purchase", "list")["purchases"]), 1)

        self.run_cli("purchase", "buy", plan["id"], "--amount", "35")
        position = self.run_cli("budget", "summary")["position"]
        self.assertEqual(position["planned"], "$0.00")
        self.assertEqual(position["spent"], "$35.00")
        self.assertEqual(position["left_to_spend"], "$465.00")

    def test_new_flag_saves_a_separate_item(self):
        self.add_bought("Desk lamp", "40", "--status", "planned")
        self.assertEqual(self.add_bought("lamp", "35", "--new")["status"], "saved")

    def test_refunds(self):
        self.run_cli("budget", "set", "--monthly", "100")
        item = self.add_bought("Shoes", "60")["purchase"]["id"]
        self.run_cli("purchase", "refund", item, "--amount", "20")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$40.00")
        self.run_cli("purchase", "refund", item, "--amount", "41", expect_error=True)
        result = self.run_cli("purchase", "refund", item)
        self.assertEqual(result["purchase"]["status"], "refunded")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$0.00")
        self.run_cli("purchase", "refund", item, expect_error=True)

    def test_paying_a_bill_moves_it_from_due_to_spent(self):
        self.run_cli("budget", "set", "--monthly", "1000")
        bill = self.run_cli(
            "bill", "add", "--name", "Rent", "--amount", "600", "--due", "2026-11-15"
        )["bill"]["id"]
        self.assertEqual(self.run_cli("budget", "summary")["position"]["left_to_spend"], "$400.00")
        result = self.run_cli("bill", "pay", bill)
        self.assertEqual(result["bill"]["next_due"], "2026-12-15")
        position = self.run_cli("budget", "summary")["position"]
        self.assertEqual(position["bills_still_due"], "$0.00")
        self.assertEqual(position["spent"], "$600.00")
        self.assertEqual(position["left_to_spend"], "$400.00")

    def test_deleting_a_payment_makes_the_bill_due_again(self):
        bill = self.run_cli(
            "bill", "add", "--name", "Phone", "--amount", "30", "--due", "2026-11-15", "--frequency", "once"
        )["bill"]["id"]
        payment = self.run_cli("bill", "pay", bill)["payment"]["id"]
        self.assertEqual(self.run_cli("bill", "list")["bills"], [])
        self.run_cli("purchase", "delete", payment)
        restored = self.run_cli("bill", "list")["bills"][0]
        self.assertEqual(restored["next_due"], "2026-11-15")
        self.assertTrue(restored["active"])

    def test_month_end_due_days_do_not_drift(self):
        self.today = "2027-01-10"
        bill = self.run_cli(
            "bill", "add", "--name", "Rent", "--amount", "5", "--due", "2027-01-31"
        )["bill"]["id"]
        self.assertEqual(self.run_cli("bill", "pay", bill)["bill"]["next_due"], "2027-02-28")
        self.assertEqual(self.run_cli("bill", "pay", bill)["bill"]["next_due"], "2027-03-31")

    def test_overdue_bills_count_this_month_but_not_next_month(self):
        self.run_cli("budget", "set", "--monthly", "1000")
        self.run_cli("bill", "add", "--name", "Rent", "--amount", "100", "--due", "2026-10-15")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["bills_still_due"], "$200.00")
        future = self.run_cli("budget", "summary", "--month", "2026-12")["position"]
        self.assertEqual(future["bills_still_due"], "$100.00")

    def test_afford_verdicts(self):
        self.run_cli("budget", "set", "--monthly", "1000")
        self.run_cli("bill", "add", "--name", "Rent", "--amount", "600", "--due", "2026-11-20")
        self.add_bought("Food", "100")
        self.assertEqual(self.run_cli("afford", "--amount", "50")["verdict"], "yes")
        tight = self.run_cli("afford", "--amount", "250")
        self.assertEqual(tight["verdict"], "tight")
        self.assertEqual(tight["left_after"], "$50.00")
        self.assertEqual(tight["bills_still_due"][0]["name"], "Rent")
        self.assertEqual(self.run_cli("afford", "--amount", "301")["verdict"], "no")
        self.assertEqual(self.run_cli("afford", "--amount", "300")["left_after"], "$0.00")

    def test_afford_does_not_count_the_checked_plan_twice(self):
        self.run_cli("budget", "set", "--monthly", "100")
        plan = self.add_bought("Jacket", "80", "--status", "planned")["purchase"]["id"]
        checked = self.run_cli("afford", "--amount", "80", "--purchase-id", plan)
        self.assertEqual(checked["left_after"], "$20.00")

    def test_afford_needs_a_budget(self):
        self.assertIn("budget", self.run_cli("afford", "--amount", "5", expect_error=True)["error"])

    def test_movein_costs_stay_out_of_the_monthly_budget(self):
        self.run_cli("budget", "set", "--monthly", "500", "--movein-fund", "300")
        self.run_cli("movein", "init", "--date", "2027-01-15", "--housing", "dorm")
        task = self.run_cli("movein", "add", "--title", "Buy mini fridge", "--cost", "120")["task"]["id"]
        self.assertEqual(self.run_cli("budget", "summary")["position"]["left_to_spend"], "$500.00")
        status = self.run_cli("movein", "status")
        self.assertEqual(status["money"]["left_in_fund"], "$180.00")

        self.run_cli("movein", "done", task, expect_error=True)
        self.run_cli("movein", "done", task, "--actual", "100")
        money = self.run_cli("movein", "status")["money"]
        self.assertEqual(money["spent"], "$100.00")
        self.assertEqual(money["left_in_fund"], "$200.00")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$0.00")

        self.assertEqual(self.run_cli("afford", "--amount", "250", "--pool", "movein")["verdict"], "no")

    def test_buying_a_movein_purchase_finishes_its_task(self):
        self.run_cli("movein", "init", "--date", "2027-01-15", "--housing", "dorm")
        task = self.run_cli("movein", "add", "--title", "Buy fan", "--cost", "25")["task"]["id"]
        match = self.add_bought("fan", "20")["matches"][0]["id"]
        self.run_cli("purchase", "buy", match, "--amount", "20")
        tasks = {t["id"]: t for t in self.run_cli("movein", "status")["tasks"]}
        self.assertTrue(tasks[task]["done"])

    def test_late_movein_plan_clamps_past_due_dates_to_today(self):
        status = self.run_cli("movein", "init", "--date", "2026-11-20", "--housing", "apartment")
        self.assertGreaterEqual(min(t["due"] for t in status["tasks"]), self.today)
        self.run_cli("movein", "init", "--date", "2026-12-20", "--housing", "dorm", expect_error=True)

    def test_reschedule_moves_open_tasks_and_their_costs(self):
        self.run_cli("movein", "init", "--date", "2027-03-01", "--housing", "dorm")
        task = self.run_cli(
            "movein", "add", "--title", "Buy rug", "--due", "2027-02-20", "--cost", "30"
        )["task"]["id"]
        status = self.run_cli("movein", "reschedule", "--date", "2027-03-11")
        moved = next(t for t in status["tasks"] if t["id"] == task)
        self.assertEqual(moved["due"], "2027-03-02")
        self.assertEqual(self.run_cli("purchase", "list", "--status", "planned")["purchases"][0]["date"], "2027-03-02")

    def test_briefing_collects_what_matters_today(self):
        self.run_cli("budget", "set", "--monthly", "1000")
        self.run_cli("agenda", "add", "--kind", "event", "--title", "Chem", "--date", "2026-11-10", "--time", "14:00")
        self.run_cli("agenda", "add", "--kind", "event", "--title", "Bio", "--date", "2026-11-10", "--time", "09:00")
        self.run_cli("agenda", "add", "--kind", "event", "--title", "Quiz", "--date", "2026-11-11")
        self.run_cli("agenda", "add", "--kind", "event", "--title", "Old", "--date", "2026-11-01")
        self.run_cli("agenda", "add", "--kind", "reminder", "--title", "Call landlord", "--date", "2026-11-08")
        done = self.run_cli("agenda", "add", "--kind", "reminder", "--title", "Finished", "--date", "2026-11-08")
        self.run_cli("agenda", "done", done["entry"]["id"])
        self.run_cli("bill", "add", "--name", "Phone", "--amount", "30", "--due", "2026-11-14")
        self.run_cli("bill", "add", "--name", "Gym", "--amount", "20", "--due", "2026-11-05")
        self.run_cli("bill", "add", "--name", "Rent", "--amount", "400", "--due", "2026-11-28")

        brief = self.run_cli("briefing")
        self.assertEqual([e["title"] for e in brief["events_today"]], ["Bio", "Chem"])
        self.assertEqual([e["title"] for e in brief["events_tomorrow"]], ["Quiz"])
        self.assertEqual([e["title"] for e in brief["reminders_overdue"]], ["Call landlord"])
        self.assertEqual([b["name"] for b in brief["bills_due_this_week"]], ["Phone"])
        self.assertEqual([b["name"] for b in brief["bills_overdue"]], ["Gym"])
        self.assertEqual(brief["budget"]["left_to_spend"], "$550.00")
        self.assertEqual(brief["budget"]["days_left_in_month"], 21)
        self.assertEqual(brief["budget"]["per_day"], "$26.19")
        self.assertEqual(brief["setup_needed"], [])

    def test_briefing_on_an_empty_store_asks_for_setup(self):
        brief = self.run_cli("briefing")
        self.assertEqual(brief["setup_needed"], ["monthly budget", "bills"])
        self.assertIsNone(brief["move_in"])
        self.assertFalse(self.data.exists())

    def test_corrupt_file_is_never_overwritten(self):
        self.data.parent.mkdir(parents=True)
        self.data.write_text("{oops")
        self.run_cli("purchase", "add", "--item", "x", "--amount", "1", expect_error=True)
        self.assertEqual(self.data.read_text(), "{oops")

    def test_failed_command_saves_nothing(self):
        self.add_bought("Coffee", "3")
        before = self.run_cli("export")
        self.run_cli("purchase", "edit", "p1", "--item", "Changed", "--amount", "bad", expect_error=True)
        self.assertEqual(self.run_cli("export"), before)

    def test_old_data_files_keep_their_records(self):
        self.data.parent.mkdir(parents=True)
        old_file = self.data.with_name("data.json")
        old_file.write_text(json.dumps({
            "version": 1,
            "settings": {"monthly_budget_cents": 50000, "movein_fund_cents": None},
            "next_id": {"p": 2, "b": 1, "m": 1, "a": 2},
            "purchases": [{
                "id": "p1", "item": "Coffee", "amount_cents": 500, "category": "dining",
                "status": "bought", "date": "2026-11-09",
            }],
            "bills": [],
            "movein": {"date": None, "housing": None, "tasks": []},
            "agenda": [{
                "id": "a1", "kind": "event", "title": "Lab", "date": "2026-11-10",
                "time": None, "done": False,
            }],
        }))
        self.assertEqual(self.run_cli("budget", "summary")["position"]["left_to_spend"], "$495.00")
        self.assertEqual(self.run_cli("briefing")["events_today"][0]["title"], "Lab")
        self.assertEqual(self.run_cli("chore", "starter")["added"][0]["id"], "c1")
        self.assertEqual(self.add_bought("Tea", "2")["purchase"]["id"], "p2")
        self.assertIn("Coffee", old_file.read_text())

    def test_syllabus_import_skips_duplicates_and_rejects_bad_rows(self):
        rows = json.dumps([
            {"title": "Midterm", "date": "2026-11-20", "type": "exam"},
            {"title": "Essay 1", "date": "2026-11-12"},
        ])
        first = self.run_cli("agenda", "import", "--course", "BIO 101", stdin=rows)
        self.assertEqual(len(first["added"]), 2)
        second = self.run_cli("agenda", "import", "--course", "bio 101", stdin=rows)
        self.assertEqual((len(second["added"]), len(second["already_saved"])), (0, 2))
        other_course = self.run_cli("agenda", "import", "--course", "CHEM", stdin=rows)
        self.assertEqual(len(other_course["added"]), 2)

        bad = json.dumps([{"title": "Fine", "date": "2026-12-01"}, {"title": "Broken", "date": "soon"}])
        self.run_cli("agenda", "import", stdin=bad, expect_error=True)
        self.run_cli("agenda", "import", stdin="not json", expect_error=True)
        titles = [e["title"] for e in self.run_cli("agenda", "list")["entries"]]
        self.assertNotIn("Fine", titles)

    def test_week_flags_busy_days_and_counts_hours(self):
        self.run_cli(
            "schedule", "add", "--kind", "class", "--title", "Bio", "--days", "mon,wed",
            "--start", "09:00", "--end", "10:30", "--until", "2026-12-10",
        )
        self.run_cli(
            "schedule", "add", "--kind", "work", "--title", "Cafe", "--days", "Thursday",
            "--start", "12:00", "--end", "18:00",
        )
        self.run_cli(
            "schedule", "add", "--kind", "work", "--title", "Night", "--days", "fri",
            "--start", "22:00", "--end", "02:00",
        )
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Exam", "--date", "2026-11-12", "--type", "exam")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "A1", "--date", "2026-11-14")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "A2", "--date", "2026-11-14")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Add/drop", "--date", "2026-11-13", "--type", "admin")

        week = self.run_cli("week")["weeks"][0]
        self.assertEqual(week["week_of"], "2026-11-09")
        self.assertEqual(week["scheduled_hours"], 13.0)
        self.assertEqual(week["work_hours"], 10.0)
        self.assertEqual(week["exams"], 1)
        self.assertEqual(
            [d["date"] for d in week["busy_days"]], ["2026-11-12", "2026-11-14"]
        )
        later = self.run_cli("week", "--date", "2026-12-14")["weeks"][0]
        self.assertEqual(later["scheduled_hours"], 10.0)
        self.run_cli("schedule", "add", "--kind", "work", "--title", "x", "--days", "someday",
                     "--start", "09:00", "--end", "10:00", expect_error=True)

    def test_budget_counts_only_your_share_of_a_shared_expense(self):
        self.run_cli("budget", "set", "--monthly", "500")
        result = self.run_cli(
            "shared", "add", "--item", "Internet", "--amount", "60", "--paid-by", "me", "--split", "me,Alex,Sam"
        )
        self.assertEqual(result["balances"]["owed_to_you"], "$40.00")
        self.assertEqual(result["balances"]["you_owe"], "$0.00")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$20.00")

        settled = self.run_cli("shared", "settle", "--from", "alex", "--to", "me", "--amount", "20")
        self.assertEqual(settled["balances"]["owed_to_you"], "$20.00")
        self.assertEqual(settled["balances"]["who_pays_whom"], [{"from": "Sam", "to": "me", "amount": "$20.00"}])
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$20.00")

    def test_when_a_roommate_pays_you_owe_your_share(self):
        self.run_cli("budget", "set", "--monthly", "500")
        result = self.run_cli(
            "shared", "add", "--item", "Pizza", "--amount", "30", "--paid-by", "Alex", "--shares", "me=10,Alex=20"
        )
        self.assertEqual(result["balances"]["you_owe"], "$10.00")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$10.00")

    def test_shared_split_keeps_every_cent(self):
        result = self.run_cli("shared", "add", "--item", "Soap", "--amount", "10", "--paid-by", "me", "--split", "me,A,B")
        self.assertEqual(result["expense"]["shares"], {"me": "$3.34", "A": "$3.33", "B": "$3.33"})
        self.run_cli("shared", "add", "--item", "Mop", "--amount", "10", "--paid-by", "me",
                     "--shares", "me=5,A=4", expect_error=True)
        self.run_cli("shared", "add", "--item", "Mop", "--amount", "10", "--paid-by", "me",
                     "--split", "me,me", expect_error=True)
        self.run_cli("shared", "settle", "--from", "Typo", "--to", "me", "--amount", "1", expect_error=True)

    def test_deleting_a_shared_expense_removes_your_share(self):
        expense = self.run_cli("shared", "add", "--item", "Soap", "--amount", "10", "--paid-by", "me", "--split", "me,A")
        purchase_id = self.run_cli("purchase", "list")["purchases"][0]["id"]
        self.run_cli("purchase", "delete", purchase_id, expect_error=True)
        self.run_cli("purchase", "refund", purchase_id, expect_error=True)
        result = self.run_cli("shared", "delete", expense["expense"]["id"])
        self.assertEqual(result["balances"]["owed_to_you"], "$0.00")
        self.assertEqual(self.run_cli("purchase", "list")["purchases"], [])

    def test_a_paid_bill_can_be_split_without_counting_twice(self):
        self.run_cli("budget", "set", "--monthly", "500")
        bill = self.run_cli("bill", "add", "--name", "Internet", "--amount", "30", "--due", "2026-11-15")["bill"]["id"]
        self.run_cli("bill", "pay", bill)
        result = self.run_cli(
            "shared", "add", "--item", "Internet", "--amount", "60", "--paid-by", "me",
            "--split", "me,Alex", "--ledger-only",
        )
        self.assertEqual(result["balances"]["owed_to_you"], "$30.00")
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$30.00")

    def test_shared_expense_checks_planned_items(self):
        self.add_bought("Couch", "200", "--status", "planned")
        attempt = self.run_cli("shared", "add", "--item", "couch", "--amount", "200", "--paid-by", "me", "--split", "me,A")
        self.assertEqual(attempt["status"], "needs_decision")
        self.assertEqual(self.run_cli("shared", "status")["expenses"], [])

    def test_grocery_trip_logs_one_purchase_and_stocks_the_kitchen(self):
        self.run_cli("budget", "set", "--monthly", "500")
        self.run_cli("grocery", "add", "--item", "Eggs", "--estimate", "4")
        self.run_cli("grocery", "add", "--item", "eggs", "--quantity", "12")
        rice = self.run_cli("grocery", "add", "--item", "Rice", "--estimate", "3.50")
        self.assertEqual(rice["list"]["estimated_total"], "$7.50")
        self.assertEqual(len(rice["list"]["items"]), 2)

        trip = self.run_cli("grocery", "trip", "--total", "6.25", "--ids", "g1")
        self.assertEqual([k["item"] for k in trip["moved_to_kitchen"]], ["Eggs"])
        self.assertEqual([g["item"] for g in trip["still_on_list"]], ["Rice"])
        self.assertEqual(self.run_cli("budget", "summary")["position"]["spent"], "$6.25")

    def test_kitchen_flags_food_to_use_soon(self):
        self.run_cli("kitchen", "add", "--item", "Milk", "--expires", "2026-11-12")
        self.run_cli("kitchen", "add", "--item", "Yogurt", "--expires", "2026-11-09")
        self.run_cli("kitchen", "add", "--item", "Rice")
        self.run_cli("kitchen", "add", "--item", "Cheese", "--expires", "2026-12-20")
        self.run_cli("kitchen", "notes", "vegetarian, peanut allergy")
        listed = self.run_cli("kitchen", "list")
        self.assertEqual(listed["food_notes"], "vegetarian, peanut allergy")
        flags = {k["item"]: k["flag"] for k in listed["items"]}
        self.assertEqual(flags, {"Milk": "use_soon", "Yogurt": "expired", "Rice": None, "Cheese": None})
        soon = [k["item"] for k in self.run_cli("briefing")["kitchen_use_soon"]]
        self.assertEqual(soon, ["Yogurt", "Milk"])

    def test_chores_repeat_from_when_they_were_done(self):
        chore = self.run_cli("chore", "add", "--title", "Trash", "--every-days", "7", "--first-due", "2026-11-05")["chore"]["id"]
        self.run_cli("chore", "add", "--title", "trash", "--every-days", "7", expect_error=True)
        self.run_cli("chore", "add", "--title", "Bad", "--every-days", "0", expect_error=True)
        self.assertEqual(self.run_cli("briefing")["chores_due"][0]["title"], "Trash")
        done = self.run_cli("chore", "done", chore)["chore"]
        self.assertEqual(done["next_due"], "2026-11-17")
        self.assertEqual(self.run_cli("briefing")["chores_due"], [])
        skipped = self.run_cli("chore", "skip", chore)["chore"]
        self.assertEqual(skipped["last_done"], "2026-11-10")

    def test_starter_chores_can_be_added_twice_safely(self):
        first = self.run_cli("chore", "starter")["added"]
        self.assertGreater(len(first), 0)
        self.assertEqual(self.run_cli("chore", "starter")["added"], [])

    def test_briefing_includes_new_sections(self):
        self.run_cli("schedule", "add", "--kind", "class", "--title", "Bio", "--days", "tue",
                     "--start", "09:00", "--end", "10:00")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Essay", "--date", "2026-11-13")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Missed", "--date", "2026-11-08")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Old", "--date", "2026-10-01")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Past exam", "--date", "2026-11-08", "--type", "exam")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "E1", "--date", "2026-11-20")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "E2", "--date", "2026-11-20")
        brief = self.run_cli("briefing")
        self.assertEqual(brief["classes_and_shifts_today"][0]["title"], "Bio")
        self.assertEqual([d["title"] for d in brief["deadlines_this_week"]], ["Essay"])
        self.assertEqual([d["title"] for d in brief["deadlines_missed"]], ["Missed"])
        self.assertEqual(brief["busy_days_ahead"][0]["date"], "2026-11-20")
        self.assertEqual(brief["events_today"], [])
        self.assertEqual(brief["reminders_overdue"], [])

    def test_weekly_review(self):
        self.run_cli("budget", "set", "--monthly", "1000")
        bill = self.run_cli("bill", "add", "--name", "Rent", "--amount", "600", "--due", "2026-11-09")["bill"]["id"]
        self.run_cli("bill", "add", "--name", "Phone", "--amount", "100", "--due", "2026-11-15")
        self.run_cli("bill", "pay", bill)
        self.add_bought("Dinner", "90", "--category", "dining", "--date", "2026-11-08")
        self.add_bought("Groceries", "50", "--category", "groceries", "--date", "2026-11-09")
        self.add_bought("Shoes", "40", "--category", "clothes", "--date", "2026-11-02")
        self.add_bought("Old", "5", "--date", "2026-10-30")
        review = self.run_cli("review")
        spending = review["spending"]
        self.assertEqual(spending["total_excluding_bills"], "$140.00")
        self.assertEqual(spending["previous_week_excluding_bills"], "$45.00")
        self.assertEqual(spending["bills_paid"], "$600.00")
        self.assertEqual(spending["by_category"][0]["category"], "dining")
        self.assertEqual(review["pace"]["status"], "ahead_of_pace")
        self.assertEqual(review["pace"]["available_after_bills"], "$300.00")
        self.assertEqual(review["pace"]["expected_by_today"], "$100.00")
        self.assertEqual([b["name"] for b in review["next_week"]["bills_due"]], ["Phone"])

    def test_review_on_an_empty_store(self):
        review = self.run_cli("review")
        self.assertIsNone(review["pace"])
        self.assertEqual(review["spending"]["total_excluding_bills"], "$0.00")

    def fill_every_table(self):
        self.run_cli("budget", "set", "--monthly", "1000", "--movein-fund", "500")
        bill = self.run_cli("bill", "add", "--name", "Rent", "--amount", "600", "--due", "2026-11-09")["bill"]["id"]
        self.run_cli("bill", "add", "--name", "Tuition", "--amount", "50", "--due", "2026-12-01", "--frequency", "once")
        self.run_cli("bill", "pay", bill)
        refunded = self.add_bought("Shoes", "60", "--category", "clothes")["purchase"]["id"]
        self.run_cli("purchase", "refund", refunded, "--amount", "20.50")
        self.add_bought("Lamp", "40", "--status", "planned")
        self.run_cli("movein", "init", "--date", "2027-01-15", "--housing", "apartment")
        task = self.run_cli("movein", "add", "--title", "Buy fan", "--cost", "25")["task"]["id"]
        self.run_cli("movein", "done", task, "--actual", "22")
        self.run_cli("agenda", "add", "--kind", "deadline", "--title", "Midterm", "--date", "2026-12-09",
                     "--time", "09:00", "--type", "exam", "--course", "BIO 101")
        self.run_cli("agenda", "add", "--kind", "reminder", "--title", "Call", "--date", "2026-11-09")
        self.run_cli("schedule", "add", "--kind", "class", "--title", "Bio", "--days", "wed,mon",
                     "--start", "09:00", "--end", "10:00", "--from", "2026-09-01", "--until", "2026-12-10")
        self.run_cli("shared", "add", "--item", "Soap", "--amount", "10", "--paid-by", "me", "--split", "Sam,me,Alex")
        self.run_cli("shared", "settle", "--from", "Alex", "--to", "me", "--amount", "1")
        self.run_cli("grocery", "add", "--item", "Eggs", "--quantity", "12", "--estimate", "4")
        self.run_cli("kitchen", "add", "--item", "Milk", "--expires", "2026-11-12")
        self.run_cli("kitchen", "notes", "peanut allergy")
        chore = self.run_cli("chore", "add", "--title", "Trash", "--every-days", "3")["chore"]["id"]
        self.run_cli("chore", "done", chore)

    def test_every_table_survives_a_round_trip_and_json_import(self):
        self.fill_every_table()
        exported = self.run_cli("export")
        self.assertEqual(exported["purchases"][1]["refunded_cents"], 2050)
        self.assertEqual(exported["schedule"][0]["days"], ["wed", "mon"])
        self.assertIs(exported["bills"][0]["active"], True)
        self.assertEqual(list(exported["shared"]["expenses"][0]["shares"]), ["Sam", "me", "Alex"])
        self.assertEqual(exported["shared"]["settlements"][0]["from"], "Alex")

        # Earlier versions saved this same shape as JSON, so it must import exactly.
        other = Path(self.folder.name) / "other"
        other.mkdir()
        (other / "data.json").write_text(json.dumps(exported))
        self.data = other / "fledge.db"
        self.assertEqual(self.run_cli("export"), exported)

    def test_bad_old_json_is_reported_and_kept(self):
        self.data.parent.mkdir(parents=True)
        old_file = self.data.with_name("data.json")
        old_file.write_text("{oops")
        self.assertIn("data.json", self.run_cli("budget", "summary", expect_error=True)["error"])
        self.assertEqual(old_file.read_text(), "{oops")

    def test_parallel_writes_keep_every_change(self):
        # Without a write lock, processes would overwrite each other's purchases and ids.
        env = dict(os.environ, FLEDGE_DATA=str(self.data), FLEDGE_TODAY=self.today)
        command = [sys.executable, str(TRACKER), "purchase", "add", "--new", "--amount", "1", "--item"]
        processes = [
            subprocess.Popen(command + [f"Item {n}"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for n in range(20)
        ]
        for process in processes:
            _, errors = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, errors)
        purchases = self.run_cli("export")["purchases"]
        self.assertEqual(len(purchases), 20)
        self.assertEqual(len({p["id"] for p in purchases}), 20)
        self.assertEqual({p["item"] for p in purchases}, {f"Item {n}" for n in range(20)})

    def test_database_rejects_invalid_rows(self):
        self.add_bought("Coffee", "3")
        connection = sqlite3.connect(str(self.data), isolation_level=None)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA foreign_keys = ON")
        insert = "INSERT INTO purchases (id, item, amount_cents, category, status, date, bill_id) VALUES (?, ?, ?, ?, ?, ?, ?)"
        bad_rows = [
            ("x1", "Bad", -5, "other", "bought", "2026-11-10", None),
            ("x2", "Bad", 5, "other", "maybe", "2026-11-10", None),
            ("x3", "Bad", 5, "other", "bought", None, None),
            ("x4", "Bad", 5, "other", "bought", "2026-11-10", "no-such-bill"),
        ]
        for row in bad_rows:
            with self.subTest(row=row[0]), self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, row)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE purchases SET refunded_cents = 999999")

    def test_newer_database_is_refused(self):
        self.add_bought("Coffee", "3")
        connection = sqlite3.connect(str(self.data))
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        self.assertIn("newer version", self.run_cli("export", expect_error=True)["error"])

    def test_new_migrations_upgrade_an_existing_file_once(self):
        self.add_bought("Coffee", "3")
        sys.path.insert(0, str(TRACKER.parent))
        self.addCleanup(sys.path.pop, 0)
        from fledge import cli, storage
        extra = [["CREATE TABLE notes (id TEXT PRIMARY KEY)"]]
        args = cli.build_parser().parse_args(["export"])
        with mock.patch.dict(os.environ, {"FLEDGE_DATA": str(self.data)}):
            with mock.patch.object(storage, "MIGRATIONS", storage.MIGRATIONS + extra):
                for _ in range(2):
                    self.assertEqual(cli.execute(args)["purchases"][0]["item"], "Coffee")
        connection = sqlite3.connect(str(self.data))
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        self.assertIn("notes", tables)

    def test_share_of_zero_cents_saves_no_purchase(self):
        result = self.run_cli("shared", "add", "--item", "Gum", "--amount", "0.01", "--paid-by", "Alex", "--split", "Alex,me")
        self.assertEqual(result["expense"]["shares"], {"Alex": "$0.01", "me": "$0.00"})
        self.assertEqual(self.run_cli("purchase", "list")["purchases"], [])

    def test_trends_group_by_month_with_changes(self):
        self.add_bought("Old", "100", "--date", "2026-01-15")
        self.add_bought("Rent", "200", "--category", "home", "--date", "2026-09-05")
        self.add_bought("Food", "50", "--category", "food", "--date", "2026-09-06")
        self.add_bought("Food", "30", "--category", "food", "--date", "2026-11-02", "--new")
        shoes = self.add_bought("Shoes", "40", "--category", "clothes", "--date", "2026-11-03")["purchase"]["id"]
        self.run_cli("purchase", "refund", shoes, "--amount", "10")
        self.add_bought("Later", "99", "--date", "2026-12-01")
        self.add_bought("Plan", "77", "--status", "planned", "--date", "2026-11-04")
        self.run_cli("movein", "init", "--date", "2027-01-15", "--housing", "dorm")
        task = self.run_cli("movein", "add", "--title", "Buy fan", "--cost", "25")["task"]["id"]
        self.run_cli("movein", "done", task, "--actual", "22")

        months = self.run_cli("budget", "trends", "--months", "3")["months"]
        self.assertEqual([m["month"] for m in months], ["2026-09", "2026-11"])
        self.assertEqual(months[0]["total"], "$250.00")
        self.assertIsNone(months[0]["change"])
        self.assertEqual(months[1]["total"], "$60.00")
        self.assertEqual(months[1]["previous_month_with_spending"], "2026-09")
        self.assertEqual(months[1]["change"], "-$190.00")
        self.assertEqual(months[1]["by_category"], [
            {"category": "clothes", "spent": "$30.00"}, {"category": "food", "spent": "$30.00"},
        ])
        self.run_cli("budget", "trends", "--months", "0", expect_error=True)

    def test_trends_cross_a_year_and_handle_no_file(self):
        self.assertEqual(self.run_cli("budget", "trends")["months"], [])
        self.assertFalse(self.data.exists())
        self.add_bought("Old", "10", "--date", "2025-12-15")
        months = self.run_cli("budget", "trends", "--months", "12")["months"]
        self.assertEqual([m["month"] for m in months], ["2025-12"])


if __name__ == "__main__":
    unittest.main()
