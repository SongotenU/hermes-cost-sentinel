"""cost_sentinel — ledger, pricing and budget logic for the cost-sentinel plugin.

One SQLite database (``plugin_db("cost-sentinel")``, WAL) holds every observed
API request plus a settings row-set for the daily budget. Counters are keyed by
UTC day. Cost comes from Hermes' own pricing engine (:func:`agent.usage_pricing.
estimate_usage_cost`) so plugin numbers can never drift from what the host shows.

Everything is side-effect-light: hook callbacks only ever append rows and check
a cached counter, never block the agent loop (fail-open, per the observer
contract).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_PLUGIN_NAME = "cost-sentinel"
_PLUGIN_NAME_RAW = "cost-sentinel"  # wire id for broadcast_plugin_event (no dots)
_DB_LOCK = threading.Lock()  # sqlite connects are opened per-call; lock only guards init

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,               -- main | aux | error
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    api_request_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    api_mode TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    api_duration REAL NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,                    -- NULL when cost unknown
    cost_status TEXT NOT NULL DEFAULT '',
    cost_source TEXT NOT NULL DEFAULT '',
    cost_label TEXT NOT NULL DEFAULT '',
    finish_reason TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_api_calls_day ON api_calls(day);
CREATE INDEX IF NOT EXISTS idx_api_calls_session ON api_calls(session_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SETTINGS_DEFAULTS: Dict[str, str] = {
    "daily_limit_usd": "0",      # 0 = no limit
    "enforce": "warn",           # warn | block | off
    "scope": "fleet",            # fleet = every profile of this Hermes install shares one daily budget
}

_DAY_RE = None  # not needed further; day keys are YYYY-MM-DD strings


def _db_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(_PLUGIN_NAME) / "data.db"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or _db_path()), check_same_thread=False, timeout=10)
    # WAL + long busy timeout: the fleet DB is written concurrently by every profile
    # process of this install (each is its own gateway/CLI process).
    with contextlib.suppress(sqlite3.DatabaseError):
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=10000")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA)


def fleet_db_path() -> Path:
    """Shared cross-profile ledger, keyed to the install root (never the profile).

    ``get_default_hermes_root()`` is the official shared-root API: ``~/.hermes``
    for profile layouts (``HERMES_HOME=~/.hermes/profiles/<name>``) and the
    custom root itself otherwise — so every profile process of this install
    lands on the same file. Disclosed in README + catalog (rule 13).
    """
    from hermes_constants import get_default_hermes_root
    path = get_default_hermes_root() / "plugin-data" / _PLUGIN_NAME / "fleet.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


# --- settings ---------------------------------------------------------------

def _get_settings(con: sqlite3.Connection) -> Dict[str, str]:
    s = dict(_SETTINGS_DEFAULTS)
    for key, value in con.execute("SELECT key, value FROM settings"):
        s[key] = value
    return s


def _set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    con.commit()


def read_settings() -> Dict[str, str]:
    """Budget settings live in the FLEET db — one shared budget for the install."""
    with _DB_LOCK, contextlib.closing(_connect(fleet_db_path())) as con:
        _ensure_schema(con)
        return _get_settings(con)


def update_settings(**updates: str) -> Dict[str, str]:
    with _DB_LOCK, contextlib.closing(_connect(fleet_db_path())) as con:
        _ensure_schema(con)
        for key, value in updates.items():
            if key in _SETTINGS_DEFAULTS:
                _set_setting(con, key, value)
        return _get_settings(con)


# --- cost estimation --------------------------------------------------------

def _canonical_from_payload(usage: Optional[dict[str, Any]]) -> Optional["CanonicalUsage"]:
    """Build a :class:`agent.usage_pricing.CanonicalUsage` from a hook usage payload.

    The ``post_api_request`` payload is already the normalized bucket dict
    (``_usage_summary_for_api_request_hook``), so its keys map 1:1 onto
    CanonicalUsage fields.
    """
    if not isinstance(usage, dict):
        return None
    try:
        from agent.usage_pricing import CanonicalUsage
        return CanonicalUsage(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            request_count=int(usage.get("request_count") or 1) or 1,
        )
    except (ImportError, TypeError, ValueError):
        return None


def estimate_cost(model: str, usage: Optional[dict[str, Any]], *,
                  provider: str = "", base_url: str = "") -> Tuple[Optional[float], str, str, str]:
    """USD cost for one request via the host pricing engine.

    Returns ``(amount_usd_or_None, status, source, label)``; ``None`` means the
    route has no pricing data (tokens are still logged).
    """
    canonical = _canonical_from_payload(usage)
    if canonical is None:
        return None, "", "", ""
    if canonical.total_tokens == 0:
        return 0.0, "included", "none", "included"
    try:
        from agent.usage_pricing import estimate_usage_cost
        result = estimate_usage_cost(
            model or "", canonical, provider=provider,
            base_url=base_url or None)
    except Exception as exc:  # pricing must never take a call down
        logger.debug("cost-sentinel pricing unavailable for %s: %s", model, exc)
        return None, "", "", ""
    amount = result.amount_usd
    if amount is not None:
        try:
            amount = float(Decimal(amount).quantize(Decimal("0.000001")))
        except (InvalidOperation, ValueError):
            amount = None
    return amount, (result.status or ""), (result.source or ""), (result.label or "")


# --- ledger writes (hook side) ----------------------------------------------

def record_api_call(kind: str, *, ts: Optional[float] = None, **fields: Any) -> None:
    """Insert one observed request into the profile ledger AND the shared fleet ledger.

    Never raises (observer callbacks fail open).
    """
    ts = ts or time.time()
    tokens_keys = ("input_tokens", "output_tokens", "cache_read_tokens",
                   "cache_write_tokens", "reasoning_tokens", "total_tokens")
    intify = {k: int(fields.pop(k, 0) or 0) for k in tokens_keys}
    # backward-compat: legacy buckets used prompt_tokens for input-side tokens
    if fields.pop("prompt_tokens", None):
        pass  # normalized payload already carries the split fields
    try:
        assigned = float(fields.pop("cost_usd"))
    except (KeyError, TypeError, ValueError):
        assigned = None
    row = {
        "ts": ts, "day": _day_key(ts), "kind": kind,
        "session_id": str(fields.get("session_id") or ""),
        "turn_id": str(fields.get("turn_id") or ""),
        "api_request_id": str(fields.get("api_request_id") or ""),
        "platform": str(fields.get("platform") or ""),
        "model": str(fields.get("model") or ""),
        "provider": str(fields.get("provider") or ""),
        "api_mode": str(fields.get("api_mode") or ""),
        "api_call_count": int(fields.get("api_call_count") or 0),
        "api_duration": float(fields.get("api_duration") or 0),
        "finish_reason": str(fields.get("finish_reason") or ""),
        "error_type": str(fields.get("error_type") or ""),
        "error_message": str(fields.get("error_message") or "")[:400],
        "cost_usd": assigned,
        "cost_status": str(fields.get("cost_status") or ""),
        "cost_source": str(fields.get("cost_source") or ""),
        "cost_label": str(fields.get("cost_label") or ""),
        **intify,
    }
    sql_cols = ", ".join(row)
    sql_marks = ", ".join("?" for _ in row)
    values = tuple(row.values())
    for path in (_db_path(), fleet_db_path()):
        try:
            with _DB_LOCK, contextlib.closing(_connect(path)) as con:
                _ensure_schema(con)
                con.execute(f"INSERT INTO api_calls({sql_cols}) VALUES({sql_marks})", values)
                con.commit()
        except Exception as exc:
            logger.debug("cost-sentinel write to %s failed: %s", path.name, exc)
    _broadcast(row)


def _broadcast(row: Dict[str, Any]) -> None:
    """Live update for the desktop pane (fire-and-forget, viewer-side optional)."""
    with contextlib.suppress(Exception):
        # broadcast_plugin_event raises ValueError only for malformed ids; ours are static.
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(
            _PLUGIN_NAME_RAW, "cost.updated",
            {"day": row["day"], "cost_usd": row["cost_usd"], "model": row["model"]})


def day_spend(day_key: str, *, db: str = "profile") -> Tuple[float, int, int]:
    """(sum of known-cost rows, count of known rows, count of unknown-cost rows)."""
    with _DB_LOCK, contextlib.closing(
            _connect(fleet_db_path() if db == "fleet" else _db_path())) as con:
        _ensure_schema(con)
        known = con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM api_calls "
            "WHERE day = ? AND cost_usd IS NOT NULL AND kind != 'error'", (day_key,)).fetchone()
        unknown = con.execute(
            "SELECT COUNT(*) FROM api_calls WHERE day = ? AND cost_usd IS NULL AND kind != 'error'",
            (day_key,)).fetchone()
        return float(known[0] or 0), int(known[1] or 0), int(unknown[0] or 0)


def _gate_db(settings: Dict[str, str]) -> str:
    """'fleet' in default (profile) mode; 'profile' when scope is 'profile'.
    UnknownScope values fall back to fleet so a typo cannot widen the budget."""
    return "profile" if settings.get("scope", "fleet") == "profile" else "fleet"


def summary_payload(days: int = 7, include_errors: bool = False) -> Dict[str, Any]:
    """Payload shared by the /cost command and the desktop REST pane."""
    today = _day_key(time.time())
    total, known_rows, unknown_rows = day_spend(today)
    fleet_total, fleet_rows, fleet_unknown = day_spend(today, db="fleet")
    settings = read_settings()
    window_start = time.time() - max(1, days) * 86400
    with _DB_LOCK, contextlib.closing(_connect()) as con:
        _ensure_schema(con)
        per_day = con.execute(
            "SELECT day, SUM(cost_usd), COUNT(*), SUM(total_tokens) FROM api_calls "
            "WHERE day >= ? AND kind != 'error' GROUP BY day ORDER BY day",
            (_day_key(window_start),)).fetchall()
        per_model = con.execute(
            "SELECT model, provider, SUM(cost_usd), COUNT(*), SUM(total_tokens) FROM api_calls "
            "WHERE ts >= ? AND kind != 'error' GROUP BY model, provider ORDER BY SUM(cost_usd) DESC",
            (window_start,)).fetchall()
        recent_rows: List[dict[str, Any]] = []
        kind_clause = "" if include_errors else " AND kind != 'error'"
        for row in con.execute(
                "SELECT ts, kind, model, provider, platform, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, reasoning_tokens, total_tokens, "
                "cost_usd, cost_status, cost_label, session_id, turn_id, error_type "
                f"FROM api_calls WHERE ts >= ?{kind_clause} ORDER BY ts DESC LIMIT 40",
                (window_start,)):
            recent_rows.append(dict(zip(
                ("ts", "kind", "model", "provider", "platform", "input_tokens", "output_tokens",
                 "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens",
                 "cost_usd", "cost_status", "cost_label", "session_id", "turn_id", "error_type"),
                row)))
    limit = _parse_limit(settings.get("daily_limit_usd", "0"))
    enforce = settings.get("enforce", "warn")
    scope = "profile" if settings.get("scope", "fleet") == "profile" else "fleet"
    gate_spend = fleet_total if scope == "fleet" else total
    remaining = None if limit <= 0 else max(0.0, limit - gate_spend)
    return {
        "today": today,
        "scope": scope,
        "today_spend_usd": round(total, 4),
        "today_known_rows": known_rows,
        "today_unknown_rows": unknown_rows,
        "fleet_today_spend_usd": round(fleet_total, 4),
        "fleet_today_rows": fleet_rows,
        "fleet_today_unknown_rows": fleet_unknown,
        "daily_limit_usd": limit,
        "enforce": enforce,
        "remaining_usd": None if remaining is None else round(remaining, 4),
        "over_budget": bool(limit > 0 and gate_spend >= limit),
        "days": [{"day": d, "cost_usd": round(float(c or 0), 4), "rows": int(n or 0),
                  "total_tokens": int(t or 0)} for d, c, n, t in per_day],
        "by_model": [{"model": m, "provider": p, "cost_usd": round(float(c or 0), 4),
                      "rows": int(n or 0), "total_tokens": int(t or 0)}
                     for m, p, c, n, t in per_model],
        "recent": recent_rows,
        "generated_at": time.time(),
        "tz_note": "days are UTC",
    }


def _parse_limit(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def check_budget() -> Optional[str]:
    """Hard-gate decision for ``pre_llm_call``.

    Returns a block message when the budget is exceeded and enforce mode is
    ``block``; ``None`` otherwise (warn/off are non-blocking).
    """
    settings = read_settings()
    if settings.get("enforce") != "block":
        return None
    limit = _parse_limit(settings.get("daily_limit_usd", "0"))
    if limit <= 0:
        return None
    spend, _rows, _unknown = day_spend(_day_key(time.time()), db=_gate_db(settings))
    if spend < limit:
        return None
    scope_note = ("across ALL profiles of this Hermes install"
                  if _gate_db(settings) == "fleet" else "in this profile")
    return (
        "⏳ COST SENTINEL — daily budget reached.\n"
        f"Today's tokens cost ${spend:.4f} of the ${limit:.2f} daily limit {scope_note}, "
        "so this turn was stopped before inference.\n"
        "Raise the ceiling with `/cost set <usd>` (from any profile) or run "
        "`/cost resume` to clear the gate and return to warn mode."
    )


def clear_block() -> None:
    """/cost resume — one-turn escape hatch: switch back to warn (gate off)."""
    update_settings(enforce="warn")


def reset_today() -> None:
    """Wipe today's rows in BOTH ledgers (testing / false start), never other days."""
    today = _day_key(time.time())
    for path in (_db_path(), fleet_db_path()):
        with _DB_LOCK, contextlib.closing(_connect(path)) as con:
            _ensure_schema(con)
            con.execute("DELETE FROM api_calls WHERE day = ?", (today,))
            con.commit()


def as_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
