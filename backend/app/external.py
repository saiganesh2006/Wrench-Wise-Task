"""Layer 8 - External Services.

Five simulated third-party providers, each reached ONLY through `call()`, which adds what a real integration needs:
  * simulated network latency and random transient failures (EXTERNAL_LATENCY_MS, EXTERNAL_FAILURE_RATE)
  * retries with a circuit breaker (opens after 3 consecutive failures for 20 s)
  * business errors (e.g. "no bureau record") are NOT retried
  * every call is persisted to `external_calls` for observability (/api/external/status)

To go live, replace the provider functions below with real HTTP clients - nothing else changes.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import threading
import time

from . import db


class ExternalServiceError(Exception):
    """Provider returned a business error, or the service is unavailable."""


SERVICES = {
    "credit_bureau": "Credit Bureau",
    "kyc_aml": "KYC / AML Screening",
    "gov_id": "Government ID (PAN)",
    "payment_network": "Payment Network (UPI / IMPS)",
    "market_data": "Market Data Feed",
}
_state = {k: dict(calls=0, failures=0, consecutive=0, open_until=0.0, total_ms=0.0) for k in SERVICES}
_lock = threading.Lock()


def _latency() -> float:
    return float(os.getenv("EXTERNAL_LATENCY_MS", "150")) / 1000


def _fail_rate() -> float:
    return float(os.getenv("EXTERNAL_FAILURE_RATE", "0.04"))


def _log(service, operation, ok, ms, attempts, detail="") -> None:
    db.x("INSERT INTO external_calls(ts,service,operation,ok,latency_ms,attempts,detail) VALUES(?,?,?,?,?,?,?)",
         (dt.datetime.now().isoformat(timespec="seconds"), service, operation, int(ok), round(ms, 1), attempts, detail[:200]))


def call(service: str, operation: str, fn, *args, retries: int = 2, **kwargs):
    st, t0, attempts = _state[service], time.perf_counter(), 0
    if st["open_until"] > time.time():
        _log(service, operation, False, 0, 0, "circuit open")
        raise ExternalServiceError(f"{SERVICES[service]} is temporarily unavailable (circuit breaker open)")
    while attempts <= retries:
        attempts += 1
        try:
            if _latency():
                time.sleep(_latency() * random.uniform(0.6, 1.4))
            if random.random() < _fail_rate():
                raise TimeoutError("upstream timeout")
            res = fn(*args, **kwargs)
            ms = (time.perf_counter() - t0) * 1000
            with _lock:
                st["calls"] += 1; st["total_ms"] += ms; st["consecutive"] = 0
            _log(service, operation, True, ms, attempts)
            return res
        except ExternalServiceError as e:  # business error: service is healthy, do not retry
            ms = (time.perf_counter() - t0) * 1000
            with _lock:
                st["calls"] += 1; st["total_ms"] += ms; st["consecutive"] = 0
            _log(service, operation, False, ms, attempts, str(e))
            raise
        except (TimeoutError, ConnectionError):
            with _lock:
                st["failures"] += 1; st["consecutive"] += 1
                if st["consecutive"] >= 3:
                    st["open_until"] = time.time() + 20
                    break
    ms = (time.perf_counter() - t0) * 1000
    _log(service, operation, False, ms, attempts, "no response")
    raise ExternalServiceError(f"{SERVICES[service]} did not respond after {attempts} attempt(s)")


def status() -> dict:
    now, out = time.time(), []
    for k, label in SERVICES.items():
        s = _state[k]
        out.append(dict(service=k, label=label, state="OPEN" if s["open_until"] > now else "HEALTHY", calls=s["calls"],
                        failures=s["failures"], avg_latency_ms=round(s["total_ms"] / s["calls"], 1) if s["calls"] else 0))
    recent = db.q("SELECT ts,service,operation,ok,latency_ms,attempts,detail FROM external_calls ORDER BY id DESC LIMIT 12")
    return dict(services=out, recent=recent)


def reset_breakers() -> None:
    for s in _state.values():
        s.update(consecutive=0, open_until=0.0)


# ============================================================================ simulated providers
PAN_REGISTRY = {"AKPSH4821Q": "Aarav Sharma", "BNRPN9034L": "Priya Nair", "CMHPM1156K": "Rohan Mehta"}
BUREAU = {
    "AKPSH4821Q": dict(score=784, active_accounts=3, enquiries_6m=1, delinquencies=0, utilization_pct=22),
    "BNRPN9034L": dict(score=691, active_accounts=2, enquiries_6m=3, delinquencies=0, utilization_pct=48),
    "CMHPM1156K": dict(score=671, active_accounts=4, enquiries_6m=2, delinquencies=1, utilization_pct=61),
}
SANCTIONED = ["blocked entity", "viktor sanction", "shell holdings"]


def bureau_report(pan: str) -> dict:
    r = BUREAU.get(pan)
    if not r:
        raise ExternalServiceError("no credit bureau record found for this PAN")
    return dict(bureau="CreditBureau (simulated)", pan_masked=pan[:2] + "XXXXX" + pan[-2:], **r)


def verify_pan(pan: str, name: str) -> dict:
    if not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", pan or ""):
        raise ExternalServiceError("PAN format invalid")
    holder = PAN_REGISTRY.get(pan)
    if not holder:
        return dict(valid=False, name_match=False, status="NOT_FOUND")
    return dict(valid=True, name_match=holder.lower() == name.lower(), status="ACTIVE")


def aml_screen(name: str) -> dict:
    lists = ["OFAC", "UN Consolidated", "RBI Caution List", "PEP"]
    hit = next((s for s in SANCTIONED if s in name.lower()), None)
    return dict(status="REVIEW" if hit else "CLEAR", lists_checked=lists,
                match=f"name matches watch-list entry '{hit}'" if hit else None)


def payment_submit(rail: str, amount: float, beneficiary: str) -> dict:
    if amount <= 0:
        raise ExternalServiceError("invalid amount")
    utr = f"{rail}{dt.datetime.now():%y%m%d}{random.randint(10**7, 10**8 - 1)}"
    return dict(rail=rail, utr=utr, status="SUCCESS", settlement="instant")


_BASE = {"USD/INR": 83.4, "EUR/INR": 90.6, "GOLD (10g, INR)": 71200.0, "NIFTY 50": 22450.0, "SENSEX": 73900.0}


def market_quotes() -> dict:
    bucket = int(time.time() // 30)
    quotes = []
    for sym, base in _BASE.items():
        drift = (random.Random(f"{sym}{bucket}").random() - 0.5) * 0.012
        quotes.append(dict(symbol=sym, price=round(base * (1 + drift), 2), change_pct=round(drift * 100, 2)))
    return dict(as_of=dt.datetime.now().isoformat(timespec="seconds"), quotes=quotes, note="simulated feed")
