import sys, json, time
from pathlib import Path
plugin_dir = Path(sys.argv[1]).parent.parent  # argv[1] = tests/run_functional_body.py → package root
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
post = hookmap["post_api_request"]; pre = hookmap["pre_llm_call"]; aux = hookmap["post_auxiliary_call"]; err = hookmap["api_request_error"]
cmd = hookmap["command/cost"]; tool = hookmap["tool/cost_report"]

# 1) priced call gpt-4o usage from the real payload shape
post(session_id="s1", turn_id="t1", api_request_id="r1", platform="cli",
     model="gpt-4o", provider="openai", base_url="https://api.openai.com/v1", api_mode="chat_completions",
     api_call_count=1, api_duration=2.5, finish_reason="stop",
     usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 500,
            "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
            "prompt_tokens": 1700, "total_tokens": 2000})
d = json.loads(tool({"days": 1}))
assert d["today_spend_usd"] > 0, f"cost should be >0: {s}"
gpt4o_row = [r for r in d["by_model"] if r["model"] == "gpt-4o"][0]
print("gpt-4o row:", gpt4o_row["cost_usd"], "costs", [r["cost_status"] for r in d["recent"] if r["model"] == "gpt-4o"])
assert len([r for r in d["recent"] if r["session_id"] == "s1"]) == 1, f"duplicate row! {d['recent']}"

# 2) unpriced model → row kept with NULL cost, counted as unknown
post(session_id="s2", turn_id="t2", api_request_id="r2", platform="cli",
     model="totally-unknown-model-xyz", provider="", base_url="", api_mode="chat_completions",
     api_call_count=1, api_duration=1.0, finish_reason="stop",
     usage={"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": 0, "request_count": 1, "prompt_tokens": 10, "total_tokens": 20})
d = json.loads(tool({"days": 1}))
assert d["today_unknown_rows"] == 1, d
print("unknown-cost model logged with NULL cost: OK")

# 3) aux + error rows
aux(session_id="s1", turn_id="t1", api_request_id="r3", model="gpt-4o-mini", provider="openai",
    base_url="https://api.openai.com/v1", usage={"input_tokens": 200, "output_tokens": 50,
    "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
    "prompt_tokens": 200, "total_tokens": 250})
err(session_id="s1", turn_id="t1", api_request_id="r4", platform="cli", model="gpt-4o",
    provider="openai", error={"type": "rate_limit", "message": "429"})
print("aux + error rows recorded: OK")

# 4) /cost status text
txt = cmd("status")
assert "Today" in txt and "gpt-4o" in txt
print("--- /cost status head ---")
print("\n".join(txt.splitlines()[:6]))

# 5) budget gate: WARN no block, BLOCK blocks until /cost resume
print(cmd("set 0.000001"))
assert pre(session_id="s", user_message="x") is None, "warn mode must not block"
out = cmd("block")
block = pre(session_id="s", user_message="x")
assert isinstance(block, dict) and block.get("action") == "block", block
assert "budget reached" in block["message"]
print("block mode stops the turn with the gate message: OK")
print(cmd("resume"))
assert pre(session_id="s", user_message="x") is None, "resume must clear the gate"
print("resume clears the gate: OK")

# 6) enforce=off disables gate entirely
print(cmd("block")); print(cmd("off"))
assert pre(session_id="s", user_message="x") is None, "off mode must not block"
print("off disables the gate: OK")
print("ALL FUNCTIONAL CHECKS PASSED")
