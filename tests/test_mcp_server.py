import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "scripts" / "mcp_server.py"
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_server  # noqa: E402

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name) / "data.json"
        self.env = dict(os.environ, FLEDGE_DATA=str(self.data), FLEDGE_TODAY="2026-11-10")


class ProtocolTest(ServerTestCase):
    def talk(self, *messages):
        """Send raw lines through a real server process and return its replies."""
        lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
        done = subprocess.run(
            [sys.executable, str(SERVER)], input="\n".join(lines) + "\n",
            capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return [json.loads(line) for line in done.stdout.splitlines()]

    def call(self, tool_name, **arguments):
        params = {"name": tool_name, "arguments": arguments}
        return self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})[0]["result"]

    def call_json(self, tool_name, **arguments):
        result = self.call(tool_name, **arguments)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def test_initialize_accepts_known_versions_and_offers_its_own_otherwise(self):
        for requested, expected in (("2024-11-05", "2024-11-05"), ("1999-01-01", mcp_server.PROTOCOL_VERSIONS[0])):
            reply = self.talk({
                "jsonrpc": "2.0", "id": 7, "method": "initialize",
                "params": {"protocolVersion": requested, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            })[0]
            self.assertEqual(reply["id"], 7)
            self.assertEqual(reply["result"]["protocolVersion"], expected)
            self.assertEqual(reply["result"]["serverInfo"]["name"], "fledge")
            self.assertIn("tools", reply["result"]["capabilities"])

    def test_notifications_get_no_reply_and_stdout_stays_clean(self):
        replies = self.talk(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            "",
        )
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 1, "result": {}}])

    def test_bad_messages_get_errors_and_the_server_keeps_going(self):
        replies = self.talk(
            "{not json",
            {"jsonrpc": "2.0", "id": 1},
            [1, 2],
            {"jsonrpc": "2.0", "id": 2, "method": "nope"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": "bad"},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
        )
        codes = [r.get("error", {}).get("code") for r in replies]
        self.assertEqual(codes, [-32700, -32600, -32600, -32601, -32602, -32602, None])
        self.assertEqual(replies[-1]["id"], 5)

    def test_every_tracker_command_is_a_described_tool(self):
        tools = self.talk({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
        self.assertEqual(len(tools), 50)
        self.assertEqual({t["name"] for t in tools}, set(mcp_server.TOOL_DESCRIPTIONS))
        for tool in tools:
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
            for name, spec in tool["inputSchema"]["properties"].items():
                self.assertIn(spec["type"], ("string", "integer", "boolean"), name)
            for name in tool["inputSchema"].get("required", []):
                self.assertIn(name, tool["inputSchema"]["properties"])

    def test_schema_details(self):
        tools = mcp_server.build_tools()
        add = tools["purchase_add"].schema()
        self.assertEqual(sorted(add["required"]), ["amount", "item"])
        self.assertEqual(add["properties"]["status"]["enum"], ["planned", "bought"])
        self.assertEqual(add["properties"]["new"]["type"], "boolean")
        self.assertEqual(tools["week"].schema()["properties"]["weeks"]["type"], "integer")
        self.assertIn("start_date", tools["schedule_add"].schema()["properties"])
        self.assertEqual(tools["purchase_delete"].describe()["annotations"]["destructiveHint"], True)
        self.assertEqual(tools["briefing"].describe()["annotations"]["readOnlyHint"], True)
        self.assertEqual(tools["purchase_add"].describe()["annotations"]["readOnlyHint"], False)

    def test_tools_share_data_and_math_with_the_command_line(self):
        self.call_json("budget_set", monthly="1200")
        self.call_json("bill_add", name="Rent", amount="650", due="2026-11-15")
        saved = self.call_json("purchase_add", item="Groceries", amount=23.40, category="groceries")
        self.assertEqual(saved["budget"]["left_to_spend"], "$526.60")
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "tracker.py"), "budget", "summary"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(json.loads(done.stdout)["position"]["left_to_spend"], "$526.60")
        self.assertEqual(self.call_json("afford", amount="600")["verdict"], "no")

    def test_values_are_data_and_never_treated_as_options(self):
        for value in ("--status=planned", "-h", "$(touch /tmp/fledge-should-not-exist)"):
            saved = self.call_json("purchase_add", item=value, amount="1", new=True)
            self.assertEqual(saved["purchase"]["item"], value)
            self.assertEqual(saved["purchase"]["status"], "bought")

    def test_tracker_decisions_reach_the_client(self):
        self.call_json("purchase_add", item="Desk lamp", amount="40", status="planned")
        self.assertEqual(self.call_json("purchase_add", item="lamp", amount="35")["status"], "needs_decision")

    def test_mistakes_are_tool_errors_a_model_can_fix(self):
        cases = [
            ("purchase_add", {"item": "x"}, "Missing arguments: amount"),
            ("purchase_add", {"item": "x", "amount": "abc"}, "not a valid amount"),
            ("purchase_add", {"item": "x", "amount": "1", "status": "maybe"}, "status must be one of"),
            ("purchase_add", {"item": "x", "amount": "1", "new": "yes"}, "new must be true or false"),
            ("purchase_add", {"item": "x", "amount": "1", "color": "red"}, "Unknown arguments: color"),
            ("purchase_add", {"item": ["x"], "amount": "1"}, "item must be text"),
            ("week", {"weeks": "two"}, "weeks must be a whole number"),
            ("week", {"weeks": True}, "weeks must be a whole number"),
            ("purchase_buy", {"id": "p99"}, "No purchase with id p99"),
        ]
        for name, arguments, expected in cases:
            with self.subTest(name=name, expected=expected):
                result = self.call(name, **arguments)
                self.assertTrue(result["isError"])
                self.assertIn(expected, result["content"][0]["text"])
        self.assertFalse(self.data.exists())

    def test_import_requires_rows_so_it_never_reads_the_protocol_stream(self):
        replies = self.talk(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "agenda_import", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("rows", replies[0]["result"]["content"][0]["text"])
        self.assertEqual(replies[1]["id"], 2)

        rows = json.dumps([{"title": "Midterm", "date": "2027-02-18", "type": "exam"}])
        imported = self.call_json("agenda_import", course="BIO 101", rows=rows)
        self.assertEqual(imported["added"][0]["type"], "exam")
        self.assertEqual(self.call("agenda_import", rows="not json")["isError"], True)

    def test_corrupt_data_is_reported_and_left_alone(self):
        self.data.write_text("{oops")
        result = self.call("briefing")
        self.assertTrue(result["isError"])
        self.assertEqual(self.data.read_text(), "{oops")

    def test_command_line_import_still_reads_stdin(self):
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "tracker.py"), "agenda", "import"],
            input=json.dumps([{"title": "Essay", "date": "2026-12-02"}]),
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(len(json.loads(done.stdout)["added"]), 1)


@unittest.skipIf(ClientSession is None, "The official MCP SDK needs Python 3.10+ and isn't installed.")
class OfficialClientTest(ServerTestCase):
    """Connects with the official MCP Python client instead of hand-written messages."""

    async def session_flow(self):
        parameters = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=self.env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                started = await session.initialize()
                self.assertEqual(started.server_info.name, "fledge")
                self.assertTrue(started.instructions)

                tools = await session.list_tools()
                self.assertEqual(len(tools.tools), 50)

                await session.call_tool("budget_set", {"monthly": "1200"})
                saved = await session.call_tool(
                    "purchase_add", {"item": "Groceries", "amount": "23.40", "category": "groceries"}
                )
                self.assertFalse(saved.is_error)
                self.assertEqual(json.loads(saved.content[0].text)["budget"]["left_to_spend"], "$1,176.60")

                failed = await session.call_tool("purchase_buy", {"id": "p99"})
                self.assertTrue(failed.is_error)
                await session.send_ping()

    def test_full_session(self):
        asyncio.run(asyncio.wait_for(self.session_flow(), timeout=60))


if __name__ == "__main__":
    unittest.main()
