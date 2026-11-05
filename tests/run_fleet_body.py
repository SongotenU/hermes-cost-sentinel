"""Fleet-rollup functional checks (run by tests/run_functional.py driver, needs
HERMES_HOME set to a scratch profile). Extends the base body with the two-ledger
behaviour: shared fleet DB, shared settings, scope-aware gate."""
import sys, json, time, os, tempfile, sqlite3
from pathlib import Path
plugin_dir = Path(sys.argv[1]).parent.parent  # argv[1] = tests/run_fleet_body.py → package root
import importlib.util
spec2 = importlib.util.spec_from_file_location("cost_sentinel_plugin", plugin_dir / "__init__.py",
                                               submodule_search_locations=[str(plugin_dir)])
init = importlib.util.module_from_spec(spec2)
sys.modules["cost_sentinel_plugin"] = init
spec2.loader.exec_module(init)
cs = init.cs  # single identity

calls = []
class Ctx:
    def register_hook(self, name, cb, **k): calls.append((name, cb))
    def register_command(self, name, **k): calls.append(("command/" + name, k.get("handler")))
    def register_tool(self, **k): calls.append(("tool/" + k["name"], k["handler"]))
    def register_skill(self, *a, **k): return None
init.register(Ctx())

hookmap = dict(calls)
post = hookmap["post_api_request"]; pre = hookmap["pre_llm_call"]
cmd = hookmap["command/cost"]; tool = hookmap["tool/cost_report"]

print("fleet db:", cs.fleet_db_path())
home = Path(os.environ["HERMES_HOME"])
from hermes_constants import get_default_hermes_root
expected_fleet = get_default_hermes_root() / "plugin-data" / "cost-sentinel" / "fleet.db"
assert cs.fleet_db_path() == expected_fleet, (cs.fleet_db_path(), expected_fleet)
from plugins.plugin_storage import plugin_data_dir
assert cs._db_path() != cs.fleet_db_path(), "profile ledger and fleet ledger must be separate"
print("layout check: profile ledger separate from the shared fleet ledger: OK")

def priced_call(session, model="gpt-4o"):
    post(session_id=session, turn_id="t-" + session, api_request_id="r-" + session, platform="cli",
         model=model, provider="openai", base_url="https://api.openai.com/v1", api_mode="chat_completions",
         api_call_count=1, api_duration=1.0, finish_reason="stop",
         usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 500,
                "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
                "prompt_tokens": 1700, "total_tokens": 2000})   # ~$0.0066

# profile A records one priced call; own ledger == fleet ledger with one profile
priced_call("A")
d = json.loads(tool({"days": 1}))
assert abs(d["today_spend_usd"] - 0.0066) < 0.001, d["today_spend_usd"]
assert abs(d["fleet_today_spend_usd"] - 0.0066) < 0.001
print("profile A: own ~$0.0066, fleet ~$0.0066 — aggregation OK")

# shared settings: limit $0.01 sits BETWEEN one profile's day (~$0.0066) and
# the two-profile fleet day (~$0.0132) — the scope flip must change the verdict.
print(cmd("set 0.01"))
priced_call("A2")   # A reaches ~$0.0132 → fleet over, profile A over
print(cmd("block"))
d = json.loads(tool({"days": 1}))
assert d["scope"] == "fleet" and d["over_budget"] is True
gate = pre(session_id="A3", user_message="x")
assert isinstance(gate, dict) and "ALL profiles" in gate["message"], gate
print("fleet gate blocks profile A (fleet $0.0132 > $0.01): OK")

# scope=profile: gate counts ONLY this profile's day; a fresh second profile is clean
print(cmd("scope profile"))
old_hh = os.environ["HERMES_HOME"]
os.environ["HERMES_HOME"] = str(home.parent / "worker-sim")  # sibling profile, same install root
priced_call("B")
d2 = json.loads(tool({"days": 1}))
assert d2["fleet_today_spend_usd"] > 0.01, d2["fleet_today_spend_usd"]
assert pre(session_id="B2", user_message="x") is None, "profile scope: B under its own limit must not block"
print("profile scope: B's gate returns None (B own $0.0066 < $0.01) despite fleet over: OK")

# scope back to fleet: B's next turn blocks (shared limit)
os.environ["HERMES_HOME"] = old_hh
print(cmd("scope fleet"))
gate_both = pre(session_id="B3", user_message="x")
assert isinstance(gate_both, dict) and "ALL profiles" in gate_both["message"], gate_both
print("fleet scope re-enabled: blocks BOTH over the shared $0.01: OK")

print(cmd("resume"))
assert pre(session_id="B2", user_message="x") is None
d = json.loads(tool({"days": 1}))
assert d["enforce"] == "warn"
print("resume clears the gate (warn mode): OK")

print(cmd("reset"))
d = json.loads(tool({"days": 1}))
assert d["today_spend_usd"] == 0 and d["fleet_today_spend_usd"] == 0
con = sqlite3.connect(cs.fleet_db_path())
assert con.execute("select count(*) from api_calls").fetchone()[0] == 0
print("reset clears profile + fleet ledgers: OK")
print("ALL FLEET-ROLLUP CHECKS PASSED")
