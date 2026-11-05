"""Functional test: cost-sentinel against the fork's own venv — no mocks of Hermes core.

Runs with the hermes-agent venv so `agent.usage_pricing` and `plugins.plugin_storage`
are imported the same way the runtime loads them. HERMES_HOME points at a scratch
profile so the real profile DB isn't touched.
"""
import subprocess
import tempfile
import os
from pathlib import Path

REPO = Path.home() / ".hermes/hermes-agent"
PLUGIN = Path.home() / "Downloads/Hermes/cost-sentinel"

SCRIPT = '''
import sys, time, json, types
from pathlib import Path
plugin_dir = Path(sys.argv[1])
sys.path.insert(0, str(plugin_dir.parent))          # so package import name matches dir name
pkgname = plugin_dir.name.replace("-", "_")         # keep filename-friendly import
loader = importlib.machinery if False else None
import importlib.util
spec = importlib.util.spec_from_file_location("cost_sentinel", plugin_dir / "cost_sentinel.py")
cs = importlib.util.module_from_spec(spec); spec.loader.exec_module(cs)

# import the register() module the same way: as a package named cost_sentinel_plugin
spec2 = importlib.util.spec_from_file_location("cost_sentinel_plugin_init", plugin_dir / "__init__.py",
                                               submodule_search_locations=[str(plugin_dir)])
init = importlib.util.module_from_spec(spec2)
import importlib.machinery
sys.modules["cost_sentinel_plugin_init"] = init
spec2.loader.exec_module(init)

import hermes_constants
# scratch settings audit
calls = []
class Ctx:
    def register_hook(self, name, cb, **k): calls.append((name, cb)); return None
    def register_command(self, name, **k): calls.append(("command/" + name, k.get("handler"))); return None
    def register_tool(self, **k): calls.append(("tool/" + k["name"], k["handler"])); return None
    def register_skill(self, *a, **k): return None
init.register(Ctx())

hookmap = {n: cb for n, cb in calls}
post = hookmap["post_api_request"]; pre = hookmap["pre_llm_call"]; aux = hookmap["post_auxiliary_call"]; err = hookmap["api_request_error"]
cmd = hookmap["command/cost"]; tool = hookmap["tool/cost_report"]

# 1) priced call gpt-4o usage from the real payload shape
post(session_id="s1", turn_id="t1", api_request_id="r1", platform="cli",
     model="gpt-4o", provider="openai", base_url="https://api.openai.com/v1", api_mode="chat_completions",
     api_call_count=1, api_duration=2.5, finish_reason="stop",
     usage={"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 500,
            "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
            "prompt_tokens": 1700, "total_tokens": 2000})
s = tool({"days": 1})
d = json.loads(s)
assert d["today_spend_usd"] > 0, f"cost should be >0: {s}"
gpt4o_row = [r for r in d["by_model"] if r["model"] == "gpt-4o"][0]
print("gpt-4o row:", gpt4o_row["cost_usd"], "status", [r["cost_status"] for r in d["recent"]][0])

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
print("\\n".join(txt.splitlines()[:6]))

# 5) budget gate: set small limit in WARN → no block; BLOCK → block message
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
print("ALL FUNCTIONAL CHECKS PASSED")
'''

env = dict(os.environ)
env["HERMES_HOME"] = tempfile.mkdtemp(prefix="cost-sentinel-test-")
env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"  # test harness: keep the wrapper from re-exec-ing this process
# hermes venv_sync relaunches any un-managed -c invocation; the child re-runs the
# whole script body and writes a duplicate ledger row. Flag it as already
# relaunched, exactly as the managed runtime does.
guard = "import sys; sys._hermes_relaunched = True; exec(open(sys.argv[1]).read())"
cmd = [str(REPO / ".venv/bin/python"), "-c", guard,
       str(PLUGIN / "tests" / "run_functional_body.py")]
r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
print("STDOUT:\n", r.stdout)
if r.returncode != 0:
    print("STDERR:\n", r.stderr[-3000:])
print("exit:", r.returncode)
