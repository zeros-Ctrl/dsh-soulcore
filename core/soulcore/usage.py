"""Persistent, content-free accounting for actual model requests.

Prices are supplied by the operator in currency units per million tokens.  The
ledger keeps missing provider usage and missing prices visible; a partial known
cost is never presented as a complete bill.  Reasoning tokens are normally a
subset of output tokens, not an additional output charge.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import logging
import os
import sqlite3
import threading
import time


_purpose = ContextVar("soulcore_usage_purpose", default=None)
log = logging.getLogger("soulcore.usage")


def current_purpose(default: str = "online_reply") -> str:
    return _purpose.get() or default


def record_request(ledger, **metadata):
    """Accounting failure must not discard a successfully generated reply."""
    if ledger is None:
        return None
    try:
        return ledger.record(**metadata)
    except Exception as ex:
        if hasattr(ledger, "recording_errors"):
            ledger.recording_errors += 1
        log.warning("用量账本写入失败（%s），模型结果继续正常处理", type(ex).__name__)
        return None


@contextmanager
def usage_scope(purpose: str):
    """Label calls in this thread/task without leaking labels across callers."""
    token = _purpose.set(str(purpose)[:80] or "unspecified")
    try:
        yield
    finally:
        _purpose.reset(token)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _first_count(*values):
    return next((n for value in values if (n := _count(value)) is not None), None)


def normalize_usage(usage, operation="chat", reasoning_in_output=True) -> dict:
    """Normalize OpenAI/DeepSeek usage; do not estimate tokens from characters."""
    u = usage if isinstance(usage, dict) else {}
    inp_details = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    out_details = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
    inp_details = inp_details if isinstance(inp_details, dict) else {}
    out_details = out_details if isinstance(out_details, dict) else {}
    inp = _first_count(u.get("prompt_tokens"), u.get("input_tokens"))
    cached = _first_count(u.get("prompt_cache_hit_tokens"), inp_details.get("cached_tokens"))
    uncached = _first_count(u.get("prompt_cache_miss_tokens"))
    if inp is None and cached is not None and uncached is not None:
        inp = cached + uncached
    if operation == "embedding" and inp is None:
        inp = _count(u.get("total_tokens"))
    if uncached is None and inp is not None and cached is not None and cached <= inp:
        uncached = inp - cached
    output = _first_count(u.get("completion_tokens"), u.get("output_tokens"))
    reasoning = _first_count(out_details.get("reasoning_tokens"), u.get("reasoning_tokens"))
    if operation == "embedding":
        output, reasoning = 0, 0   # embeddings do not generate completion tokens
    billed_output = output
    if not reasoning_in_output:
        billed_output = output + reasoning if output is not None and reasoning is not None else None
    issues = []
    if inp is not None and cached is not None and cached > inp:
        issues.append("cache_exceeds_input")
    if inp is not None and cached is not None and uncached is not None and cached + uncached != inp:
        issues.append("cache_split_mismatch")
    if reasoning_in_output and output is not None and reasoning is not None and reasoning > output:
        issues.append("reasoning_exceeds_output")
    reported = any(value is not None for value in (inp, cached, uncached))
    if operation != "embedding":
        reported = reported or output is not None or reasoning is not None
    required_known = inp is not None and output is not None
    status = "invalid" if issues else "reported" if reported and required_known else "partial" if reported else "unavailable"
    return {"usage_status": status, "input_tokens": inp, "cached_input_tokens": cached,
            "uncached_input_tokens": uncached, "output_tokens": output,
            "reasoning_tokens": reasoning, "billable_output_tokens": billed_output,
            "reasoning_in_output": int(bool(reasoning_in_output)), "usage_issues": ",".join(issues)}


def _rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _cost(normalized: dict, price: dict, currency: str):
    """Return known portion, completeness and a reason for incomplete pricing."""
    if not price or not currency:
        return 0.0, False, "unconfigured"
    if normalized["usage_status"] == "invalid":
        return 0.0, False, "invalid_usage"
    inp, output = normalized["input_tokens"], normalized["output_tokens"]
    cached = normalized["cached_input_tokens"]
    uncached = normalized["uncached_input_tokens"]
    reasoning = normalized["reasoning_tokens"]
    input_rate = _rate(price.get("input_per_million"))
    cache_rate = _rate(price.get("cached_input_per_million")) if "cached_input_per_million" in price else input_rate
    output_rate = _rate(price.get("output_per_million"))
    reasoning_rate = _rate(price.get("reasoning_per_million")) if "reasoning_per_million" in price else output_rate
    known, complete = 0.0, True
    if inp == 0:
        pass
    elif inp is None:
        complete = False
    elif input_rate is not None and cache_rate == input_rate:
        known += inp * input_rate / 1_000_000
    elif cached is not None and uncached is not None:
        for count, rate in ((cached, cache_rate), (uncached, input_rate)):
            if count and rate is None:
                complete = False
            elif rate is not None:
                known += count * rate / 1_000_000
    else:
        complete = False   # a differential cache tariff requires an actual split
    if output == 0 and (normalized["reasoning_in_output"] or reasoning == 0):
        pass
    elif output is None:
        complete = False
    elif normalized["reasoning_in_output"] and reasoning_rate == output_rate and output_rate is not None:
        known += output * output_rate / 1_000_000
    elif reasoning is not None:
        ordinary = output - reasoning if normalized["reasoning_in_output"] else output
        for count, rate in ((ordinary, output_rate), (reasoning, reasoning_rate)):
            if count and rate is None:
                complete = False
            elif rate is not None:
                known += count * rate / 1_000_000
    else:
        complete = False
    reason = "complete" if complete else "missing_usage" if normalized["usage_status"] == "unavailable" else "partial"
    return known, complete, reason


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
  provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
  operation TEXT NOT NULL, success INTEGER NOT NULL,
  status_code INTEGER, error_type TEXT NOT NULL DEFAULT '', elapsed_ms REAL NOT NULL,
  usage_status TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER,
  uncached_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
  billable_output_tokens INTEGER, reasoning_in_output INTEGER NOT NULL,
  usage_issues TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT '',
  known_cost REAL NOT NULL DEFAULT 0, cost_complete INTEGER NOT NULL DEFAULT 0,
  price_status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_requests_created ON requests(created);
CREATE INDEX IF NOT EXISTS ix_requests_purpose_model ON requests(purpose, provider, model);
"""


_AGGREGATE = """
COUNT(*) AS calls, COALESCE(SUM(success),0) AS successful_calls,
COALESCE(SUM(1-success),0) AS failed_calls,
COALESCE(SUM(usage_status IN ('reported','partial','invalid')),0) AS reported_usage_calls,
COALESCE(SUM(usage_status='unavailable'),0) AS missing_usage_calls,
COALESCE(SUM(usage_status='partial'),0) AS partial_usage_calls,
COALESCE(SUM(usage_status='invalid'),0) AS invalid_usage_calls,
COALESCE(SUM(input_tokens IS NULL),0) AS unknown_input_calls,
COALESCE(SUM(output_tokens IS NULL),0) AS unknown_output_calls,
COALESCE(SUM(input_tokens IS NOT NULL AND cached_input_tokens IS NULL),0) AS unknown_cache_split_calls,
SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS cached_input_tokens,
SUM(uncached_input_tokens) AS uncached_input_tokens, SUM(output_tokens) AS output_tokens,
SUM(reasoning_tokens) AS reasoning_tokens, SUM(billable_output_tokens) AS billable_output_tokens,
COALESCE(SUM(cost_complete),0) AS completely_priced_calls,
COALESCE(SUM(1-cost_complete),0) AS incompletely_priced_calls,
COALESCE(SUM(price_status='unconfigured'),0) AS unconfigured_price_calls,
COALESCE(SUM(elapsed_ms),0) AS elapsed_ms
"""


class UsageLedger:
    """Append-only usage rows in a separate SQLite database, safe across threads."""
    def __init__(self, data_dir: str, cfg: dict | None = None, clock=time.time):
        self.cfg = cfg or {}
        self.clock = clock
        self.enabled = bool(self.cfg.get("enabled", True))
        self.lock = threading.RLock()
        self.recording_errors = 0
        self.db = None
        if self.enabled:
            os.makedirs(data_dir, exist_ok=True)
            self.db = sqlite3.connect(os.path.join(data_dir, "usage.sqlite3"), timeout=10,
                                      check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA busy_timeout=10000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(_SCHEMA)
            self.db.commit()

    def record(self, *, provider: str, model: str, operation="chat", success=True,
               usage=None, elapsed_ms=0.0, status_code=None, error_type="", purpose=None):
        if not self.enabled:
            return None
        provider, model = str(provider)[:120], str(model)[:240]
        prices = self.cfg.get("prices") or {}
        prices = prices if isinstance(prices, dict) else {}
        price = prices.get(f"{provider}:{model}", prices.get(model, {}))
        price = price if isinstance(price, dict) else {}
        currency = str(price.get("currency") or self.cfg.get("currency") or "")[:16]
        normalized = normalize_usage(usage, operation, price.get("reasoning_in_output", True))
        known_cost, complete, price_status = _cost(normalized, price, currency)
        values = {"created": self.clock(), "provider": provider, "model": model,
                  "purpose": str(purpose or current_purpose("embedding" if operation == "embedding" else "online_reply"))[:80],
                  "operation": str(operation)[:40], "success": int(bool(success)),
                  "status_code": _count(status_code), "error_type": str(error_type)[:80],
                  "elapsed_ms": max(0.0, float(elapsed_ms)) if math.isfinite(float(elapsed_ms)) else 0.0,
                  **normalized, "currency": currency, "known_cost": known_cost,
                  "cost_complete": int(complete), "price_status": price_status}
        with self.lock:
            if self.db is None:
                return None
            fields = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            try:
                cur = self.db.execute(f"INSERT INTO requests({fields}) VALUES({placeholders})", tuple(values.values()))
                self.db.commit()
                return cur.lastrowid
            except Exception:
                self.db.rollback()
                raise

    def snapshot(self, since: float | None = None) -> dict:
        with self.lock:
            if self.db is None:
                return {"enabled": False, "totals": {"calls": 0}, "by_purpose": {}, "by_model": {},
                        "known_cost_by_currency": {}, "cost_complete": True}
            where, params = (" WHERE created>=?", (float(since),)) if since is not None else ("", ())
            totals = dict(self.db.execute("SELECT " + _AGGREGATE + " FROM requests" + where, params).fetchone())
            by_purpose = {r["purpose"]: {k: r[k] for k in totals}
                          for r in self.db.execute("SELECT purpose," + _AGGREGATE + " FROM requests" + where + " GROUP BY purpose", params)}
            by_model = {}
            for r in self.db.execute("SELECT provider,model," + _AGGREGATE + " FROM requests" + where + " GROUP BY provider,model", params):
                by_model[f"{r['provider']}:{r['model']}"] = {"provider": r["provider"], "model": r["model"], **{k: r[k] for k in totals}}
            for group in (by_purpose, by_model):
                for item in group.values():
                    item["known_cost_by_currency"] = {}
                    item["cost_complete"] = item["incompletely_priced_calls"] == 0
            for r in self.db.execute("SELECT purpose,currency,SUM(known_cost) AS cost FROM requests" + where + " GROUP BY purpose,currency", params):
                if r["currency"]:
                    by_purpose[r["purpose"]]["known_cost_by_currency"][r["currency"]] = r["cost"]
            for r in self.db.execute("SELECT provider,model,currency,SUM(known_cost) AS cost FROM requests" + where + " GROUP BY provider,model,currency", params):
                if r["currency"]:
                    by_model[f"{r['provider']}:{r['model']}"]["known_cost_by_currency"][r["currency"]] = r["cost"]
            currencies = self.db.execute("SELECT currency,SUM(known_cost) AS cost FROM requests" + where + " GROUP BY currency", params)
            costs = {r["currency"]: r["cost"] for r in currencies if r["currency"]}
            return {"enabled": self.enabled, "totals": totals, "by_purpose": by_purpose,
                    "by_model": by_model, "known_cost_by_currency": costs,
                    "cost_complete": totals["incompletely_priced_calls"] == 0,
                    "recording_errors": self.recording_errors,
                    "note": "Costs include only configured prices and reported usage; reasoning is not added twice."}

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
