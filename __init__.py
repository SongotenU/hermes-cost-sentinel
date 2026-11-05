"""cost-sentinel — live cost ledger + daily budget guarddog.

``post_api_request`` / ``api_request_error`` / ``post_auxiliary_call`` append
priced rows to a profile-scoped SQLite ledger; ``pre_llm_call`` can stop a turn
when the UTC-day budget is spent (hard mode only). ``/cost`` runs the human
surface; a ``cost_report`` tool exposes the same numbers to the agent.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import cost_sentinel as cs

logger = logging.getLogger(__name__)

_MENU = (
    "cost-sentinel usage:\n"
    "  /cost               — today + 7-day summary and per-model breakdown\n"
    "  /cost set <usd>     — set the daily limit (0 = unlimited)\n"
    "  /cost block         — hard-stop turns once the limit is reached\n"
    "  /cost warn          — report only, never stop a turn (default)\n"
    "  /cost off           — disable limit checking entirely\n"
    "  /cost scope fleet   — one budget shared by ALL profiles (default)\n"
    "  /cost scope profile — budget counts this profile's calls only\n"
    "  /cost resume        — clear a triggered hard gate (returns to warn)\n"
    "  /cost log           — the 20 most recent priced API calls\n"
    "  /cost reset         — delete today's rows"
)


def _on_post_api_request(**kwargs: Any) -> None:
    usage = kwargs.get("usage")
    model = kwargs.get("model") or ""
    provider = kwargs.get("provider") or ""
    base_url = kwargs.get("base_url") or ""
    cost, status, source, label = cs.estimate_cost(model, usage, provider=provider, base_url=base_url)
    cs.record_api_call(
        "main", ts=time.time(),
        session_id=kwargs.get("session_id"), turn_id=kwargs.get("turn_id"),
        api_request_id=kwargs.get("api_request_id"), platform=kwargs.get("platform"),
        model=model, provider=provider, api_mode=kwargs.get("api_mode"),
        api_call_count=kwargs.get("api_call_count"), api_duration=kwargs.get("api_duration"),
        finish_reason=kwargs.get("finish_reason"),
        usage=usage,
        input_tokens=(usage or {}).get("input_tokens") if isinstance(usage, dict) else 0,
        output_tokens=(usage or {}).get("output_tokens") if isinstance(usage, dict) else 0,
        cache_read_tokens=(usage or {}).get("cache_read_tokens") if isinstance(usage, dict) else 0,
        cache_write_tokens=(usage or {}).get("cache_write_tokens") if isinstance(usage, dict) else 0,
        reasoning_tokens=(usage or {}).get("reasoning_tokens") if isinstance(usage, dict) else 0,
        total_tokens=(usage or {}).get("total_tokens") if isinstance(usage, dict) else 0,
        cost_usd=cost, cost_status=status, cost_source=source, cost_label=label)


def _on_post_auxiliary_call(**kwargs: Any) -> None:
    if kwargs.get("error"):
        usage = None
    else:
        usage = kwargs.get("usage")
    cost, status, source, label = cs.estimate_cost(
        kwargs.get("model") or "", usage,
        provider=kwargs.get("provider") or "", base_url=kwargs.get("base_url") or "")
    cs.record_api_call("aux", ts=time.time(),
                       session_id=kwargs.get("session_id"), turn_id=kwargs.get("turn_id"),
                       api_request_id=kwargs.get("api_request_id"),
                       model=kwargs.get("model") or "", provider=kwargs.get("provider") or "",
                       usage=usage,
                       input_tokens=(usage or {}).get("input_tokens") if isinstance(usage, dict) else 0,
                       output_tokens=(usage or {}).get("output_tokens") if isinstance(usage, dict) else 0,
                       cache_read_tokens=(usage or {}).get("cache_read_tokens") if isinstance(usage, dict) else 0,
                       cache_write_tokens=(usage or {}).get("cache_write_tokens") if isinstance(usage, dict) else 0,
                       reasoning_tokens=(usage or {}).get("reasoning_tokens") if isinstance(usage, dict) else 0,
                       total_tokens=(usage or {}).get("total_tokens") if isinstance(usage, dict) else 0,
                       cost_usd=cost, cost_status=status, cost_source=source, cost_label=label,
                       error_type=(kwargs.get("error") or {}).get("type", "") if isinstance(kwargs.get("error"), dict) else str(kwargs.get("error") or ""))


def _on_api_request_error(**kwargs: Any) -> None:
    err = kwargs.get("error") or {}
    cs.record_api_call("error", ts=time.time(),
                       session_id=kwargs.get("session_id"), turn_id=kwargs.get("turn_id"),
                       api_request_id=kwargs.get("api_request_id"),
                       platform=kwargs.get("platform"), model=kwargs.get("model") or "",
                       provider=kwargs.get("provider") or "",
                       api_mode=kwargs.get("api_mode"),
                       error_type=str(err.get("type", "") if isinstance(err, dict) else err),
                       error_message=str(err.get("message", "") if isinstance(err, dict) else err))


def _on_pre_llm_call(**kwargs: Any) -> Any:
    block = cs.check_budget()
    if block:
        return {"action": "block", "message": block}
    return None


def _handle_cost(raw_args: str) -> str:
    """``/cost`` slash command handler."""
    args = (raw_args or "").strip().split()
    cmd = args[0].lower() if args else ""
    if cmd == "set":
        if len(args) != 2:
            return "usage: /cost set <usd>  (e.g. /cost set 20)"
        try:
            value = float(args[1])
        except ValueError:
            return f"not a dollar amount: {args[1]!r}"
        if value < 0:
            return "the limit is non-negative; 0 disables it"
        value = round(value, 6)  # keep sub-cent precision (0.000001 survives)
        settings = cs.update_settings(daily_limit_usd=str(value))
        return (f"daily limit: {'unlimited' if value == 0 else f'${value:.4f}'} "
                f"(enforce: {settings['enforce']}, scope: {settings['scope']})")
    if cmd == "block":
        settings = cs.update_settings(enforce="block")
        return f"hard gate ON — new turns stop after ${settings['daily_limit_usd']}/day (enforce)."
    if cmd == "warn":
        cs.update_settings(enforce="warn")
        return "warn mode — cost is tracked but turns are never stopped."
    if cmd == "off":
        cs.update_settings(enforce="off")
        return "limit checking disabled (ledger keeps recording)."
    if cmd == "scope":
        if len(args) != 2 or args[1].lower() not in ("fleet", "profile"):
            return "usage: /cost scope fleet|profile"
        settings = cs.update_settings(scope=args[1].lower())
        return (f"budget scope: {settings['scope']} — "
                + ("EVERY profile of this install shares the daily limit."
                   if settings["scope"] == "fleet"
                   else "only this profile's calls count toward the limit."))
    if cmd == "resume":
        cs.clear_block()
        return "hard gate cleared; enforce mode is back to warn."
    if cmd == "log":
        rows = cs.summary_payload(days=2)["recent"][:20]
        if not rows:
            return "no recorded API calls in the last two days."
        out = ["ts | model | provider | in/out/cache | cost"]
        for row in rows:
            out.append(
                f"{time.strftime('%H:%M:%S', time.gmtime(row['ts']))} | {row['model'][:32]:32s} | "
                f"{row['provider'][:10]:10s} | {row['input_tokens']}/{row['output_tokens']}"
                f"/{row['cache_read_tokens']} | {row['cost_label'] or 'unknown'}")
        return "\n".join(out)
    if cmd == "reset":
        cs.reset_today()
        return "today's ledger rows deleted."
    if cmd in ("", "status", "?"):
        payload = cs.summary_payload(days=7)
        limit = payload["daily_limit_usd"]
        spent = payload["today_spend_usd"]
        bar = _bar(spent, limit)
        fleet = payload.get("fleet_today_spend_usd")
        lines = [
            f"Today ({payload['today']}) — ${spent:.4f}{f' of ${limit:.2f} ({payload['scope']})' if limit else ''} "
            f"this profile · fleet total ${fleet:.4f}" if fleet is not None
            else f"Today ({payload['today']}) — ${spent:.4f}{f' of ${limit:.2f} ({payload['scope']})' if limit else ''} ",
            bar,
            "",
            "USD per model (7d):",
        ]
        if payload["by_model"]:
            for entry in payload["by_model"][:8]:
                lines.append(f"  ${entry['cost_usd']:.4f}  {entry['model']} "
                             f"({entry['provider'] or 'none'}, {entry['rows']} calls)")
        else:
            lines.append("  (no priced calls yet)")
        lines += ["", "Per day (7d):"]
        for entry in payload["days"]:
            lines.append(f"  {entry['day']}  ${entry['cost_usd']:.4f} "
                         f"({entry['rows']} calls, {entry['total_tokens']} toks)")
        lines += ["", _MENU.splitlines()[5].strip()]
        return "\n".join(lines)
    return _MENU


def _bar(spent: float, limit: float) -> str:
    if limit <= 0:
        return "limit: unlimited"
    ratio = max(0.0, min(1.0, spent / limit))
    filled = int(ratio * 20)
    return f"[{'#' * filled}{'.' * (20 - filled)}] {ratio * 100:.0f}%"


def _cost_report_tool(args: dict[str, Any]) -> str:
    """Agent-facing tool: read the ledger without touching shell/state files."""
    days = args.get("days") if isinstance(args.get("days"), int) else 7
    include_errors = bool(args.get("include_errors"))
    return cs.as_json(cs.summary_payload(days=max(1, min(90, days)),
                                         include_errors=include_errors))


_COST_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "days": {
            "type": "integer",
            "description": "Look-back window in days (1–90, default 7).",
        },
        "include_errors": {
            "type": "boolean",
            "description": "Include failed API attempts in the recent-calls list.",
        },
    },
    "additionalProperties": False,
}


def register(ctx: Any) -> None:
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("post_auxiliary_call", _on_post_auxiliary_call)
    ctx.register_hook("api_request_error", _on_api_request_error)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command("cost", handler=_handle_cost,
                         description="Cost ledger and daily budget guarddog status.",
                         args_hint="[set <usd>|block|warn|off|resume|log|reset]")
    ctx.register_tool(
        name="cost_report", toolset="cost", schema=_COST_REPORT_SCHEMA,
        handler=_cost_report_tool,
        description="Daily LLM spend and per-model cost breakdown recorded by the "
                    "cost-sentinel plugin. Read-only.",
        emoji="💸")
