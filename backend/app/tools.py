"""Agent Tool / MCP layer (#5): banking APIs behind a governed registry, backed by SQLite and external providers.

Controls: tool registry, authorization (customer_id injected from the authenticated session, never from the LLM),
schema validation, permission boundary per agent, transaction approval (human-in-the-loop), persistent audit log.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from collections import defaultdict
from typing import Any, Callable

from . import db, external, knowledge
from .db import inr

APPROVAL_THRESHOLD = float(os.getenv("TRANSFER_APPROVAL_THRESHOLD", "25000"))
DAILY_LIMIT = 200000.0


class ToolError(Exception):
    pass


class Tool:
    def __init__(self, fn: Callable, name: str, description: str, props: dict, required: list[str], agents: list[str]):
        self.fn, self.name, self.description = fn, name, description
        self.props, self.required, self.agents = props, required, agents

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.props, "required": self.required}}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, props: dict | None = None, required: list[str] | None = None,
         agents: list[str] | None = None):
    def deco(fn):
        REGISTRY[name] = Tool(fn, name, description, props or {}, required or [], agents or [])
        return fn
    return deco


def schemas_for(agent: str) -> list[dict]:
    return [t.schema() for t in REGISTRY.values() if agent in t.agents]


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def _validate(t: Tool, args: dict) -> dict:
    for r in t.required:
        if r not in args:
            raise ToolError(f"missing required argument '{r}'")
    clean = {}
    for k, v in args.items():
        if k not in t.props:
            continue  # drop unknown args (e.g. an injected customer_id)
        want = t.props[k]["type"]
        if want in ("integer", "number") and isinstance(v, str):
            try:
                v = float(v) if want == "number" else int(float(v))
            except ValueError:
                raise ToolError(f"argument '{k}' must be {want}")
        if not isinstance(v, _TYPES[want]):
            raise ToolError(f"argument '{k}' must be {want}")
        clean[k] = v
    return clean


def call(agent: str, name: str, args: dict, ctx: dict) -> Any:
    """Single governed entry point used by every agent."""
    started, ok = time.perf_counter(), False
    try:
        t = REGISTRY.get(name)
        if not t:
            raise ToolError(f"unknown tool '{name}'")
        if agent not in t.agents:
            raise ToolError(f"permission boundary: agent '{agent}' may not call '{name}'")
        result = t.fn(ctx=ctx, **_validate(t, args or {}))
        ok = True
    except (ToolError, external.ExternalServiceError) as e:
        result = {"error": str(e)}
    except Exception as e:  # never leak stack traces to the LLM
        result = {"error": f"tool failure: {type(e).__name__}"}
    ms = round((time.perf_counter() - started) * 1000, 1)
    preview = str(result)[:160]
    db.log_audit(agent, name, ctx["customer_id"], args, ok, ms, preview, ctx.get("trace_id"))
    ctx.setdefault("trace", []).append(dict(type="tool", agent=agent, tool=name, args=args, ok=ok, latency_ms=ms,
                                            result_preview=preview))
    return result


# --------------------------------------------------------------------------- helpers
def _cust(ctx) -> dict:
    return db.get_customer(ctx["customer_id"])


def _txns(cid: str, days: int) -> list[dict]:
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    return db.q("SELECT * FROM transactions WHERE customer_id=? AND ts>=? ORDER BY ts", (cid, cutoff))


def emi(principal: float, annual_rate: float, months: int) -> float:
    r = annual_rate / 12 / 100
    return principal / months if r == 0 else principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


# --------------------------------------------------------------------------- Customer / Banking
@tool("get_customer_profile", "Get the customer's profile: name, segment, income, KYC status, credit score.",
      agents=["account", "credit", "advisory"])
def get_customer_profile(ctx):
    c = _cust(ctx)
    return {k: c[k] for k in ("customer_id", "name", "city", "segment", "monthly_income", "kyc", "credit_score")}


@tool("get_accounts", "List the customer's accounts with balances, cards and active loans (Customer 360).",
      agents=["account", "payments", "advisory"])
def get_accounts(ctx):
    c = _cust(ctx)
    return {"accounts": c["accounts"], "cards": c["cards"], "loans": c["loans"]}


@tool("get_transactions", "Fetch recent transactions, newest first.",
      {"days": {"type": "integer", "description": "Look-back window in days (default 30)"},
       "limit": {"type": "integer", "description": "Max rows (default 10)"}}, agents=["account", "fraud"])
def get_transactions(ctx, days: int = 30, limit: int = 10):
    rows = sorted(_txns(ctx["customer_id"], days), key=lambda t: t["ts"], reverse=True)[:max(1, min(limit, 50))]
    return {"count": len(rows), "transactions": rows}


@tool("spending_summary", "Summarise debit spending by category over a period and show the top category.",
      {"days": {"type": "integer", "description": "Look-back window in days (default 30)"}}, agents=["account", "advisory"])
def spending_summary(ctx, days: int = 30):
    by_cat: dict[str, float] = defaultdict(float)
    income = 0.0
    for t in _txns(ctx["customer_id"], days):
        if t["type"] == "debit" and t["status"] != "HELD":
            by_cat[t["category"]] += t["amount"]
        elif t["type"] == "credit":
            income += t["amount"]
    total = sum(by_cat.values())
    ranked = sorted(by_cat.items(), key=lambda x: -x[1])
    return {"days": days, "total_spent": round(total, 2), "income_received": round(income, 2),
            "by_category": [{"category": k, "amount": round(v, 2), "share_pct": round(100 * v / total, 1) if total else 0}
                            for k, v in ranked]}


# --------------------------------------------------------------------------- Risk / Fraud
def score_txn(t: dict, history: list[dict]) -> dict:
    flags, score = [], 0
    debits = [h["amount"] for h in history if h["type"] == "debit" and h["txn_id"] != t["txn_id"]]
    avg = sum(debits) / len(debits) if debits else 1000
    if t["amount"] > 5 * avg:
        score += 30; flags.append(f"amount is {t['amount'] / avg:.0f}x the customer's average spend")
    elif t["amount"] > 3 * avg:
        score += 15; flags.append("amount well above typical spend")
    if t["country"] != "IN":
        score += 30; flags.append(f"foreign transaction ({t['country']})")
    hour = int(t["ts"][11:13])
    if hour < 5:
        score += 20; flags.append(f"unusual hour ({hour:02d}:00)")
    if t["category"] in ("Crypto", "Luxury", "Other"):
        score += 15; flags.append(f"high-risk category ({t['category']})")
    tt = dt.datetime.fromisoformat(t["ts"])
    near = [h for h in history if abs((dt.datetime.fromisoformat(h["ts"]) - tt).total_seconds()) < 3600]
    if len(near) >= 3:
        score += 15; flags.append("velocity: 3+ transactions within an hour")
    score = min(score, 100)
    return dict(txn_id=t["txn_id"], merchant=t["merchant"], amount=t["amount"], ts=t["ts"], risk_score=score,
                risk_level="HIGH" if score >= 70 else "MEDIUM" if score >= 40 else "LOW", flags=flags)


@tool("scan_recent_transactions", "Run fraud/anomaly scoring over recent transactions and return those that look risky.",
      {"days": {"type": "integer", "description": "Look-back window in days (default 7)"}}, agents=["fraud"])
def scan_recent_transactions(ctx, days: int = 7):
    hist = _txns(ctx["customer_id"], 45)
    window = [t for t in _txns(ctx["customer_id"], days) if t["type"] == "debit"]
    scored = [score_txn(t, hist) for t in window]
    risky = sorted([s for s in scored if s["risk_score"] >= 40], key=lambda s: -s["risk_score"])
    return {"scanned": len(scored), "risky_count": len(risky), "risky": risky[:8]}


@tool("score_transaction", "Score a single transaction by id for fraud risk.",
      {"txn_id": {"type": "string"}}, ["txn_id"], agents=["fraud"])
def score_transaction(ctx, txn_id: str):
    t = db.q1("SELECT * FROM transactions WHERE txn_id=? AND customer_id=?", (txn_id, ctx["customer_id"]))
    if not t:
        raise ToolError("transaction not found for this customer")
    return score_txn(t, _txns(ctx["customer_id"], 45))


@tool("block_card", "Immediately block a customer's card to stop further fraud. Use only when the customer asks or risk is HIGH.",
      {"card_id": {"type": "string", "description": "Card id, e.g. CARD-02. Omit to block the first active card."},
       "reason": {"type": "string"}}, agents=["fraud"])
def block_card(ctx, card_id: str | None = None, reason: str = "customer request"):
    card = next((c for c in _cust(ctx)["cards"] if (card_id is None or c["card_id"] == card_id) and c["status"] == "ACTIVE"), None)
    if not card:
        raise ToolError("no active card found to block")
    db.x("UPDATE cards SET status='BLOCKED' WHERE card_id=? AND customer_id=?", (card["card_id"], ctx["customer_id"]))
    return {"card_id": card["card_id"], "last4": card["last4"], "status": "BLOCKED",
            "fraud_case": f"CASE-{uuid.uuid4().hex[:6].upper()}", "reason": reason}


# --------------------------------------------------------------------------- Credit (uses external services)
@tool("calculate_emi", "Compute monthly EMI for a loan.",
      {"principal": {"type": "number"}, "annual_rate_pct": {"type": "number"}, "months": {"type": "integer"}},
      ["principal", "annual_rate_pct", "months"], agents=["credit"])
def calculate_emi(ctx, principal: float, annual_rate_pct: float, months: int):
    e = emi(principal, annual_rate_pct, months)
    return {"emi": round(e, 2), "total_payable": round(e * months, 2), "total_interest": round(e * months - principal, 2)}


@tool("fetch_credit_report", "Fetch the customer's live credit report from the external credit bureau.", agents=["credit"])
def fetch_credit_report(ctx):
    return external.call("credit_bureau", "get_report", external.bureau_report, _cust(ctx)["pan"])


@tool("run_kyc_check",
      "Run KYC verification: validates the customer's PAN with the government ID service and screens the name against AML "
      "watch-lists. Marks KYC as VERIFIED when everything is clear.", agents=["credit"])
def run_kyc_check(ctx):
    c = _cust(ctx)
    if c["kyc"] == "VERIFIED":
        return {"status": "ALREADY_VERIFIED"}
    gov = external.call("gov_id", "verify_pan", external.verify_pan, c["pan"], c["name"])
    aml = external.call("kyc_aml", "screen", external.aml_screen, c["name"])
    if gov["valid"] and gov["name_match"] and aml["status"] == "CLEAR":
        db.x("UPDATE customers SET kyc='VERIFIED' WHERE customer_id=?", (c["customer_id"],))
        return {"status": "VERIFIED", "pan_status": gov["status"], "aml": aml["status"], "lists_checked": aml["lists_checked"]}
    return {"status": "FAILED", "pan_valid": gov["valid"], "name_match": gov["name_match"], "aml": aml["status"],
            "detail": aml["match"] or "identity details did not match; visit a branch"}


@tool("check_loan_eligibility",
      "Run the eligibility engine for a personal loan: KYC, live bureau score, FOIR and amount caps.",
      {"amount": {"type": "number", "description": "Requested loan amount in INR"},
       "tenure_months": {"type": "integer", "description": "Tenure in months (12-72)"}},
      ["amount", "tenure_months"], agents=["credit"])
def check_loan_eligibility(ctx, amount: float, tenure_months: int):
    c = _cust(ctx)
    score, source, notes = c["credit_score"], "internal record", []
    try:
        rep = external.call("credit_bureau", "get_report", external.bureau_report, c["pan"])
        score, source = rep["score"], "credit bureau"
        if rep["delinquencies"]:
            notes.append(f"{rep['delinquencies']} past delinquency on bureau report")
    except external.ExternalServiceError as e:
        notes.append(f"bureau unavailable ({e}); used internal score")
    reasons, ok = [], True
    if c["kyc"] != "VERIFIED":
        ok = False; reasons.append("KYC is PENDING - complete KYC before applying (POL-KYC-01)")
    if c["monthly_income"] < 25000:
        ok = False; reasons.append("income below 25,000 minimum")
    if score < 650:
        ok = False; reasons.append(f"credit score {score} is below the 650 minimum (POL-CREDIT-01)")
    rate = 10.5 if score >= 750 else 12.0 if score >= 700 else 14.5
    tenure = max(12, min(72, tenure_months))
    cap = min(20 * c["monthly_income"], 2_500_000)
    if amount > cap:
        ok = False; reasons.append(f"amount exceeds your cap of {inr(cap)} (POL-CREDIT-03)")
    new_emi = emi(amount, rate, tenure)
    existing = sum(l["emi"] for l in c["loans"])
    foir = (existing + new_emi) / c["monthly_income"] * 100
    if foir > 50:
        ok = False; reasons.append(f"FOIR {foir:.0f}% exceeds the 50% limit (POL-CREDIT-02)")
    headroom = max(0.0, 0.5 * c["monthly_income"] - existing)
    r = rate / 12 / 100
    max_amt = min(headroom * ((1 + r) ** tenure - 1) / (r * (1 + r) ** tenure) if headroom else 0, cap)
    decision = "APPROVED" if ok else ("COUNTER_OFFER" if max_amt >= 50000 and c["kyc"] == "VERIFIED" and score >= 650 else "DECLINED")
    return {"decision": decision, "requested_amount": amount, "tenure_months": tenure, "offered_rate_pct": rate,
            "estimated_emi": round(new_emi, 2), "existing_emi": existing, "foir_pct": round(foir, 1),
            "credit_score": score, "credit_score_source": source, "kyc": c["kyc"],
            "reasons": reasons or ["meets all policy criteria"], "notes": notes,
            "max_eligible_amount_for_tenure": round(max_amt, -3)}


@tool("search_policy", "Search the bank's credit / fraud / payment / KYC policy documents (RAG).",
      {"query": {"type": "string"}}, ["query"], agents=["credit", "fraud", "payments"])
def search_policy(ctx, query: str):
    return {"results": knowledge.search(query)}


# --------------------------------------------------------------------------- Advisory
@tool("get_market_data", "Get live market quotes: USD/INR, EUR/INR, gold, Nifty 50, Sensex (external market data feed).",
      agents=["advisory"])
def get_market_data(ctx):
    return external.call("market_data", "quotes", external.market_quotes)


# --------------------------------------------------------------------------- Payments
def _today_out(cid: str) -> float:
    r = db.q1("SELECT COALESCE(SUM(amount),0) AS s FROM transactions WHERE customer_id=? AND channel='TRANSFER' "
              "AND type='debit' AND ts>=?", (cid, dt.date.today().isoformat()))
    return r["s"]


def execute_transfer(req: dict) -> dict:
    """Send via the external payment network FIRST, then debit atomically. Used for instant and approved transfers."""
    acct = db.q1("SELECT * FROM accounts WHERE account_id=? AND customer_id=?", (req["from_account_id"], req["customer_id"]))
    if not acct or acct["balance"] < req["amount"]:
        return {"status": "FAILED", "reason": "insufficient funds"}
    rail = "UPI" if req["amount"] <= 100000 else "IMPS"
    try:
        net = external.call("payment_network", "submit", external.payment_submit, rail, req["amount"], req["beneficiary"])
    except external.ExternalServiceError as e:
        return {"status": "FAILED", "reason": f"payment network unavailable - no money was debited ({e})"}
    with db.transaction():
        if db.x("UPDATE accounts SET balance=balance-? WHERE account_id=? AND balance>=?",
                (req["amount"], acct["account_id"], req["amount"])) != 1:
            return {"status": "FAILED", "reason": "insufficient funds"}
        t = db.add_txn(req["customer_id"], acct["account_id"], f"Transfer to {req['beneficiary']}", "Transfer",
                       req["amount"], channel="TRANSFER", ref=net["utr"])
    bal = db.q1("SELECT balance FROM accounts WHERE account_id=?", (acct["account_id"],))["balance"]
    return {"status": "EXECUTED", "txn_id": t["txn_id"], "new_balance": bal, "rail": net["rail"], "utr": net["utr"]}


@tool("initiate_transfer",
      "Transfer money to a registered beneficiary. Beneficiaries are AML-screened; transfers above the approval threshold "
      "are held for human approval; executed transfers go out over UPI/IMPS.",
      {"from_account_id": {"type": "string", "description": "Source account id, e.g. A-1001"},
       "beneficiary": {"type": "string", "description": "Registered beneficiary name"},
       "amount": {"type": "number", "description": "Amount in INR"}},
      ["beneficiary", "amount"], agents=["payments"])
def initiate_transfer(ctx, beneficiary: str, amount: float, from_account_id: str | None = None):
    c = _cust(ctx)
    if amount <= 0:
        raise ToolError("amount must be positive")
    match = next((b for b in c["beneficiaries"] if beneficiary.lower() in b.lower() or b.lower() in beneficiary.lower()), None)
    if not match:
        return {"status": "REJECTED", "reason": f"'{beneficiary}' is not a registered beneficiary",
                "registered_beneficiaries": c["beneficiaries"]}
    acct = next((a for a in c["accounts"] if a["account_id"] == from_account_id), c["accounts"][0])
    if acct["balance"] < amount:
        return {"status": "REJECTED", "reason": f"insufficient balance ({inr(acct['balance'])} available)"}
    if _today_out(c["customer_id"]) + amount > DAILY_LIMIT:
        return {"status": "REJECTED", "reason": f"daily transfer limit of {inr(DAILY_LIMIT)} exceeded"}
    try:  # external compliance screening - fail closed
        aml = external.call("kyc_aml", "screen", external.aml_screen, match)
    except external.ExternalServiceError as e:
        return {"status": "REJECTED", "reason": f"compliance screening unavailable, please retry shortly ({e})"}
    if aml["status"] != "CLEAR":
        return {"status": "REJECTED", "reason": f"beneficiary flagged by AML screening ({aml['match']}); referred to compliance"}
    req = dict(approval_id=f"APR-{uuid.uuid4().hex[:6].upper()}", customer_id=c["customer_id"],
               from_account_id=acct["account_id"], beneficiary=match, amount=amount)
    if amount > APPROVAL_THRESHOLD:
        db.x("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,NULL)",
             (req["approval_id"], req["customer_id"], req["from_account_id"], match, amount, "PENDING",
              dt.datetime.now().isoformat(timespec="seconds")))
        ctx.setdefault("approvals", []).append(req)
        return {"status": "PENDING_APPROVAL", "approval_id": req["approval_id"], "beneficiary": match, "amount": amount,
                "note": f"amount exceeds {inr(APPROVAL_THRESHOLD)}; waiting for customer's explicit approval"}
    return execute_transfer(req) | {"beneficiary": match, "amount": amount}


def decide_approval(approval_id: str, approve: bool, customer_id: str) -> dict:
    req = db.q1("SELECT * FROM approvals WHERE approval_id=? AND customer_id=?", (approval_id, customer_id))
    if not req or req["status"] != "PENDING":
        raise ToolError("approval not found or already decided")
    now = dt.datetime.now().isoformat(timespec="seconds")
    # claim the approval atomically so it can only ever execute once
    if db.x("UPDATE approvals SET status=?, decided_ts=? WHERE approval_id=? AND status='PENDING'",
            ("APPROVED" if approve else "REJECTED", now, approval_id)) != 1:
        raise ToolError("approval not found or already decided")
    if not approve:
        db.log_audit("human", "transaction_approval", customer_id, {"approval_id": approval_id, "approve": False}, True, 0, "rejected")
        return {"status": "REJECTED", "approval_id": approval_id}
    res = execute_transfer(req)
    db.log_audit("human", "transaction_approval", customer_id, {"approval_id": approval_id, "approve": True},
                 res["status"] == "EXECUTED", 0, str(res)[:160])
    return res | {"approval_id": approval_id, "beneficiary": req["beneficiary"], "amount": req["amount"]}
