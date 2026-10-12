"""API & Experience layer (#2): FastAPI app - authentication, streaming chat (SSE), approvals, live feed, audit."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import agents, auth, db, external, llm, tools  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
FEED: list[dict] = []          # ephemeral live-event buffer (the transactions themselves are persisted)
RATE: dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
_feed_id = 0
METRICS = dict(requests=0, guardrail_blocks=0, total_ms=0, ft_ms=0, ft_n=0, est_tokens=0, agents=defaultdict(int))


async def _simulate_live_transactions():
    """Simulates a real-time transaction stream, scored by the fraud engine as events arrive."""
    global _feed_id
    rng = random.Random()
    while True:
        await asyncio.sleep(6)
        try:
            await asyncio.to_thread(_one_live_txn, rng)
        except Exception:
            pass


def _one_live_txn(rng: random.Random) -> None:
    global _feed_id
    cid = rng.choice(["C001", "C002", "C003"])
    acct = db.q1("SELECT * FROM accounts WHERE customer_id=? ORDER BY account_id LIMIT 1", (cid,))
    risky = rng.random() < 0.18
    if risky:
        m, cat, amt, ctry = rng.choice([("CryptoXchange Ltd", "Crypto", 70000, "SG"), ("LuxeWatch Dubai", "Luxury", 55000, "AE"),
                                       ("Unknown Merchant", "Other", 15000, "RO")])
    else:
        m, cat, lo, hi = rng.choice(db.MERCHANTS)
        amt, ctry = rng.uniform(lo, hi), "IN"
    status = "HELD" if risky else "SETTLED"      # risky card payments are held for review, balance untouched
    with db.transaction():
        if not risky:
            if db.x("UPDATE accounts SET balance=balance-? WHERE account_id=? AND balance>=?", (amt, acct["account_id"], amt)) != 1:
                return
        t = db.add_txn(cid, acct["account_id"], m, cat, amt, channel="ONLINE" if risky else "UPI", country=ctry, status=status)
    hist = db.q("SELECT * FROM transactions WHERE customer_id=? ORDER BY ts DESC LIMIT 200", (cid,))
    s = tools.score_txn(t, hist)
    _feed_id += 1
    FEED.append(dict(id=_feed_id, customer_id=cid, txn=t, risk_score=s["risk_score"], risk_level=s["risk_level"], flags=s["flags"]))
    del FEED[:-100]


@asynccontextmanager
async def lifespan(_app):
    db.conn()                       # create + seed database on first start
    if not llm.available():
        print("\n[BankMind] No LLM configured - running with built-in fallback replies. Set ANTHROPIC_API_KEY (or the "
              "OpenAI-compatible settings) in .env for AI-generated answers.\n")
    task = asyncio.create_task(_simulate_live_transactions())
    yield
    task.cancel()


app = FastAPI(title="BankMind - Multi-Agent Banking", version="2.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# ----------------------------------------------------------------------------- auth plumbing
def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    try:
        return auth.verify_token(authorization[7:])
    except auth.AuthError as e:
        raise HTTPException(401, str(e))


def rate_limit(username: str, limit: int = 20) -> None:
    q, now = RATE[username], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Rate limit exceeded. Try again in a minute.")
    q.append(now)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class ApprovalIn(BaseModel):
    approve: bool
    password: str = ""


# ----------------------------------------------------------------------------- public
@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "llm" if llm.available() else "offline", **llm.info(),
            "approval_threshold": tools.APPROVAL_THRESHOLD}


@app.post("/api/auth/login")
def login(body: LoginIn):
    try:
        return auth.login(body.username, body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))


# ----------------------------------------------------------------------------- authenticated
@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)):
    c = db.get_customer(user["customer_id"])
    return {"username": user["username"], "customer_id": c["customer_id"], "name": c["name"]}


@app.get("/api/overview")
def overview(user: dict = Depends(current_user)):
    c = db.get_customer(user["customer_id"])
    return {k: c[k] for k in ("customer_id", "name", "segment", "kyc", "credit_score", "accounts", "cards", "loans")}


@app.get("/api/history")
def history(user: dict = Depends(current_user)):
    rows = db.q("SELECT role,text,ts FROM chat_messages WHERE customer_id=? ORDER BY id DESC LIMIT 30", (user["customer_id"],))
    return rows[::-1]


def _record(done: dict, reply: str) -> None:
    M = METRICS
    M["requests"] += 1
    M["total_ms"] += done["latency_ms"]
    M["est_tokens"] += max(1, len(reply) // 4)
    if done["intent"] == "blocked":
        M["guardrail_blocks"] += 1
    if done.get("first_token_ms") is not None:
        M["ft_ms"] += done["first_token_ms"]; M["ft_n"] += 1
    for a in done["agents"]:
        M["agents"][a] += 1


def _stream_events(cid: str, message: str):
    hist = db.q("SELECT role,text FROM chat_messages WHERE customer_id=? ORDER BY id DESC LIMIT 8", (cid,))[::-1]
    reply: list[str] = []
    for ev in agents.run(message, cid, hist):
        if ev["type"] == "token":
            reply.append(ev["text"])
        elif ev["type"] == "done":
            _record(ev, "".join(reply))
        yield ev
    now = dt.datetime.now().isoformat(timespec="seconds")
    db.x("INSERT INTO chat_messages(customer_id,role,text,ts) VALUES(?,?,?,?)", (cid, "customer", message, now))
    db.x("INSERT INTO chat_messages(customer_id,role,text,ts) VALUES(?,?,?,?)", (cid, "assistant", "".join(reply), now))


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn, user: dict = Depends(current_user)):
    rate_limit(user["username"])

    def sse():
        try:
            for ev in _stream_events(user["customer_id"], body.message):
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:  # never hang the browser
            yield f"data: {json.dumps({'type': 'error', 'text': f'Internal error: {type(e).__name__}'})}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/chat")
def chat(body: ChatIn, user: dict = Depends(current_user)):
    """Non-streaming variant (same pipeline) - handy for scripts and tests."""
    rate_limit(user["username"])
    out = dict(reply="", trace=[], approvals=[], agents=[], mode="", intent="", latency_ms=0)
    for ev in _stream_events(user["customer_id"], body.message):
        t = ev["type"]
        if t == "token":
            out["reply"] += ev["text"]
        elif t == "approval":
            out["approvals"].append(ev)
        elif t == "done":
            out.update({k: ev[k] for k in ("agents", "mode", "intent", "latency_ms", "first_token_ms")})
        else:
            out["trace"].append(ev)
    return out


@app.post("/api/approvals/{approval_id}")
def approval(approval_id: str, body: ApprovalIn, user: dict = Depends(current_user)):
    if body.approve and not auth.check_password(user["username"], body.password):   # step-up authentication
        raise HTTPException(403, "Password incorrect. Re-enter your password to approve this transfer.")
    try:
        return tools.decide_approval(approval_id, body.approve, user["customer_id"])
    except tools.ToolError as e:
        raise HTTPException(400, str(e))


@app.post("/api/reset")
def reset(user: dict = Depends(current_user)):
    db.x("DELETE FROM chat_messages WHERE customer_id=?", (user["customer_id"],))
    return {"ok": True}


@app.get("/api/audit")
def audit(limit: int = 40, user: dict = Depends(current_user)):
    return db.q("SELECT ts,agent,tool,customer_id,ok,latency_ms,result_preview,trace_id FROM audit_log WHERE customer_id=? "
                "ORDER BY id DESC LIMIT ?", (user["customer_id"], min(limit, 200)))


@app.get("/api/feed")
def feed(since: int = 0, user: dict = Depends(current_user)):
    ev = [e for e in FEED if e["id"] > since and e["customer_id"] == user["customer_id"]]
    return {"events": ev, "last_id": FEED[-1]["id"] if FEED else 0}


@app.get("/api/metrics")
def metrics(user: dict = Depends(current_user)):
    M = METRICS
    t = db.q1("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS e FROM audit_log WHERE agent!='human'")
    return dict(requests=M["requests"], guardrail_blocks=M["guardrail_blocks"], est_output_tokens=M["est_tokens"],
                avg_latency_ms=round(M["total_ms"] / M["requests"]) if M["requests"] else 0,
                avg_first_token_ms=round(M["ft_ms"] / M["ft_n"]) if M["ft_n"] else 0,
                agent_usage=dict(M["agents"]), tool_calls=t["n"], tool_errors=t["e"])


@app.get("/api/external/status")
def external_status(user: dict = Depends(current_user)):
    return external.status()


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
