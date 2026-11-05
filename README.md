# cost-sentinel

Live cost ledger + daily budget guarddog for Hermes Agent — **per profile or fleet-wide**.

**What it does**

- Records every `post_api_request` / `api_request_error` / `post_auxiliary_call` into
  TWO SQLite ledgers:
  - a per-profile one at `<profile home>/plugin-data/cost-sentinel/data.db`, and
  - a shared **fleet** ledger at `<default hermes root>/plugin-data/cost-sentinel/fleet.db`
    (via the official `get_default_hermes_root()` shared-root API) — every profile process
    of the install writes there, so the daily spend number covers the whole fleet.
- Token buckets come straight from the host payload (no parsing); USD is priced with
  Hermes' own pricing engine (`agent.usage_pricing.estimate_usage_cost`), the same engine
  the host shows — recorded cost status covers `actual / estimated / included / unknown`
  and its source.
- Turning the ledger into a **budget**:
  - `/cost set <usd>` — daily limit, stored in the shared fleet DB so every profile sees
    the same ceiling (`0` = unlimited).
  - `/cost scope fleet` (default) — the limit ceils **all profiles combined**;
    `/cost scope profile` — only this profile's calls count.
  - `/cost block` — `pre_llm_call` returns `{"action": "block"}` so each over-budget turn
    ends with the gate message and no inference request (the documented turn-scoped hook
    semantics; every turn re-checks, `/cost resume` returns to warn mode).
  - `/cost warn` (default) — record and report only, never stop a turn.
- `/cost` also gives a status summary, per-day and per-model breakdowns, the 20 most
  recent priced calls, and `/cost reset` deletes today's rows in both ledgers.
- `cost_report` tool: the agent answers "how much did today cost?" from the ledger
  directly, with a `scope`-aware view.
- Desktop half (`desktop/plugin.js`): right pane with a spend ring on the *gate* number
  (fleet or profile per scope), per-model and per-day breakdowns, live updates pushed as
  `plugin.cost-sentinel.cost.updated`, plus a status-bar chip while a limit is set.
  Served by this package's `dashboard/plugin_api.py`.

**Disclosure (rule 13)**

- Writes: the two SQLite ledgers above only. Setting rows live in the fleet DB.
- Network: none of its own — pricing happens through Hermes' pricing engine, which may
  fetch model price books the way the host itself does (e.g. OpenRouter's public models
  API), cached by the host.
- No MCP, no shell, no credentials (`requires_env` empty), no telemetry. Nothing is sent
  anywhere.
- Reads: the desktop half REST namespace reads the two ledgers' aggregates only
  (`/summary`, `/budget`) — never prompt, response or tool-argument text (content lives
  only inside hook payloads and is not persisted; per-call rows store tokens, cost,
  model/provider ids, session/turn/request ids, platform and error type/message).

**Install / test**

```bash
hermes plugins validate /path/to/cost-sentinel --install-deps
hermes plugins doctor /path/to/cost-sentinel
# enable per profile: config.yaml → plugins.enabled: ["cost-sentinel"]
python tests/run_functional.py        # profile-level checks (scratch HERMES_HOME)
python tests/run_fleet_body.py        # fleet-rollup checks via the same driver
```

Skill shipping: `cost-sentinel:cost-sentinel` (skills/cost-sentinel/SKILL.md).
