---
name: cost-sentinel
description: "Use when the user asks about LLM spend, token cost, or daily budget; /cost for-status."
---

## Cost Sentinel (agent instructions)

- The plugin keeps TWO ledgers: a per-profile one and a shared FLEET ledger at
  `<default hermes root>/plugin-data/cost-sentinel/fleet.db` (same schema) that every
  profile of this install writes to; both are priced with the host pricing engine.
  Days are UTC. Never edit either DB by hand — the `cost_report` tool reads the active
  view.
- The gate default scope is `fleet`: `/cost set 5` + `/cost block` means EVERY profile
  of this install shares one $5/day ceiling, and `pre_llm_call` stops any profile's
  turn over it. `/cost scope profile` returns to per-profile accounting.
- Read numbers with the `cost_report` tool — never parse `$HOME` files by hand for this.
- `/cost status` is the human-facing one-liner; run it for the user on request
  (slash interpreter runs it, the handler returns text).
- Budget settings live in the fleet DB (`/cost set <usd>` + `/cost block|warn|off`
  + `/cost scope fleet|profile`); settings are shared by all profiles.
- The Desktop half shows the same numbers from `/api/plugins/cost-sentinel/*`.
