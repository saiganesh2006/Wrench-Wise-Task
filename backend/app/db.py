"""Persistent storage (SQLite). Replaces the earlier in-memory store.

* Single shared connection guarded by an RLock (SQLite + threads), WAL mode, explicit transactions for money movement.
* On first start the database is created and seeded with demo customers, transactions and login users.
* Reset demo data:  python -m backend.app.db --reset
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

LOCK = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  customer_id TEXT NOT NULL, failed_attempts INTEGER DEFAULT 0, locked_until REAL DEFAULT 0, last_login TEXT);
CREATE TABLE IF NOT EXISTS customers(
  customer_id TEXT PRIMARY KEY, name TEXT, city TEXT, segment TEXT, age INTEGER, monthly_income REAL,
  credit_score INTEGER, kyc TEXT, phone TEXT, pan TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  account_id TEXT PRIMARY KEY, customer_id TEXT, type TEXT, number TEXT, balance REAL);
CREATE TABLE IF NOT EXISTS cards(
  card_id TEXT PRIMARY KEY, customer_id TEXT, type TEXT, last4 TEXT, status TEXT, credit_limit REAL);
CREATE TABLE IF NOT EXISTS loans(
  loan_id TEXT PRIMARY KEY, customer_id TEXT, type TEXT, outstanding REAL, emi REAL, rate REAL, remaining_months INTEGER);
CREATE TABLE IF NOT EXISTS beneficiaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS transactions(
  txn_id TEXT PRIMARY KEY, customer_id TEXT, account_id TEXT, ts TEXT, merchant TEXT, category TEXT, amount REAL,
  type TEXT, channel TEXT, country TEXT, status TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS idx_txn_cust_ts ON transactions(customer_id, ts);
CREATE TABLE IF NOT EXISTS approvals(
  approval_id TEXT PRIMARY KEY, customer_id TEXT, from_account_id TEXT, beneficiary TEXT, amount REAL, status TEXT,
  created_ts TEXT, decided_ts TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, tool TEXT, customer_id TEXT, args TEXT, ok INTEGER,
  latency_ms REAL, result_preview TEXT);
CREATE TABLE IF NOT EXISTS external_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, service TEXT, operation TEXT, ok INTEGER, latency_ms REAL,
  attempts INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, role TEXT, text TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


def _path() -> str:
    return os.getenv("BANKMIND_DB") or str(Path(__file__).resolve().parents[2] / "data" / "bankmind.db")


def conn() -> sqlite3.Connection:
    global _conn
    with LOCK:
        if _conn is None:
            p = _path()
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            if not _conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                seed()
        return _conn


def q(sql: str, args: tuple = ()) -> list[dict]:
    with LOCK:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql: str, args: tuple = ()) -> int:
    with LOCK:
        return conn().execute(sql, args).rowcount


@contextmanager
def transaction():
    with LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def kv_get(key: str) -> str | None:
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    return r["value"] if r else None


def kv_set(key: str, value: str) -> None:
    x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def inr(v: float) -> str:
    """Indian digit grouping: ₹5,00,000"""
    n = int(round(abs(v)))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if v < 0 else "") + "₹" + s


# ----------------------------------------------------------------------------- domain helpers
def get_customer(cid: str) -> dict | None:
    c = q1("SELECT * FROM customers WHERE customer_id=?", (cid,))
    if not c:
        return None
    c["accounts"] = q("SELECT account_id,type,number,balance FROM accounts WHERE customer_id=? ORDER BY account_id", (cid,))
    c["cards"] = q('SELECT card_id,type,last4,status,credit_limit AS "limit" FROM cards WHERE customer_id=?', (cid,))
    c["loans"] = q("SELECT loan_id,type,outstanding,emi,rate,remaining_months FROM loans WHERE customer_id=?", (cid,))
    c["beneficiaries"] = [r["name"] for r in q("SELECT name FROM beneficiaries WHERE customer_id=? ORDER BY id", (cid,))]
    return c


def add_txn(customer_id, account_id, merchant, category, amount, ttype="debit", channel="POS", country="IN",
            ts=None, status="SETTLED", ref=None) -> dict:
    t = dict(txn_id="T" + uuid.uuid4().hex[:8].upper(), customer_id=customer_id, account_id=account_id,
             ts=(ts or dt.datetime.now()).isoformat(timespec="seconds"), merchant=merchant, category=category,
             amount=round(float(amount), 2), type=ttype, channel=channel, country=country, status=status, ref=ref)
    x("INSERT INTO transactions VALUES(:txn_id,:customer_id,:account_id,:ts,:merchant,:category,:amount,:type,"
      ":channel,:country,:status,:ref)", t)
    return t


def log_audit(agent, tool, customer_id, args, ok, latency_ms, preview) -> None:
    x("INSERT INTO audit_log(ts,agent,tool,customer_id,args,ok,latency_ms,result_preview) VALUES(?,?,?,?,?,?,?,?)",
      (dt.datetime.now().isoformat(timespec="seconds"), agent, tool, customer_id, json.dumps(args, default=str),
       int(ok), latency_ms, preview))


# ----------------------------------------------------------------------------- seed data
MERCHANTS = [
    ("Swiggy", "Food", 180, 950), ("Zomato", "Food", 150, 800), ("BigBasket", "Groceries", 600, 3200),
    ("Amazon", "Shopping", 400, 6500), ("Uber", "Transport", 120, 650), ("Netflix", "Entertainment", 649, 649),
    ("Apollo Pharmacy", "Health", 200, 1800), ("Airtel", "Utilities", 399, 999),
    ("Reliance Digital", "Shopping", 1500, 12000), ("Starbucks", "Food", 250, 700),
    ("IRCTC", "Travel", 400, 3500), ("TNEB", "Utilities", 800, 2600),
]

DEMO_USERS = [  # username, password, customer_id
    ("aarav.sharma", "Aarav@2026", "C001"),
    ("priya.nair", "Priya@2026", "C002"),
    ("rohan.mehta", "Rohan@2026", "C003"),
]

_CUSTOMERS = [
    dict(customer_id="C001", name="Aarav Sharma", city="Chennai", segment="Salaried (IT)", age=34, monthly_income=95000,
         credit_score=782, kyc="VERIFIED", phone="98xxxxxx41", pan="AKPSH4821Q",
         accounts=[("A-1001", "Savings", "XXXX4821", 184250.75), ("A-1002", "Current", "XXXX7710", 42000.0)],
         cards=[("CARD-01", "Visa Signature Credit", "4421", "ACTIVE", 250000)],
         loans=[("L-1001", "Home Loan", 2400000, 28500, 8.6, 120)],
         beneficiaries=["Ravi Kumar", "Meera Sharma", "Landlord - S. Iyer"]),
    dict(customer_id="C002", name="Priya Nair", city="Bengaluru", segment="Salaried (Healthcare)", age=29,
         monthly_income=60000, credit_score=692, kyc="VERIFIED", phone="97xxxxxx08", pan="BNRPN9034L",
         accounts=[("A-2001", "Savings", "XXXX9034", 96400.20)],
         cards=[("CARD-02", "Mastercard Debit", "7788", "ACTIVE", 100000)],
         loans=[("L-2001", "Personal Loan", 180000, 9000, 13.5, 22)],
         beneficiaries=["Anita Nair", "Suresh Menon"]),
    dict(customer_id="C003", name="Rohan Mehta", city="Mumbai", segment="Self-employed", age=41, monthly_income=150000,
         credit_score=671, kyc="PENDING", phone="99xxxxxx73", pan="CMHPM1156K",
         accounts=[("A-3001", "Current", "XXXX1156", 512800.0)],
         cards=[("CARD-03", "RuPay Credit", "3302", "ACTIVE", 150000)],
         loans=[("L-3001", "Business Loan", 900000, 42000, 12.0, 30)],
         beneficiaries=["Vendor - Mehta Traders", "CA Office", "Blocked Entity Pvt Ltd"]),
]


def seed() -> None:
    from . import security  # local import: avoids import cycles
    rng = random.Random(42)
    now = dt.datetime.now()
    with transaction():
        for c in _CUSTOMERS:
            x("INSERT INTO customers VALUES(?,?,?,?,?,?,?,?,?,?)",
              (c["customer_id"], c["name"], c["city"], c["segment"], c["age"], c["monthly_income"], c["credit_score"],
               c["kyc"], c["phone"], c["pan"]))
            for a in c["accounts"]:
                x("INSERT INTO accounts VALUES(?,?,?,?,?)", (a[0], c["customer_id"], a[1], a[2], a[3]))
            for k in c["cards"]:
                x("INSERT INTO cards VALUES(?,?,?,?,?,?)", (k[0], c["customer_id"], k[1], k[2], k[3], k[4]))
            for l in c["loans"]:
                x("INSERT INTO loans VALUES(?,?,?,?,?,?,?)", (l[0], c["customer_id"], l[1], l[2], l[3], l[4], l[5]))
            for b in c["beneficiaries"]:
                x("INSERT INTO beneficiaries(customer_id,name) VALUES(?,?)", (c["customer_id"], b))
        for u, p, cid in DEMO_USERS:
            x("INSERT INTO users(username,password_hash,customer_id) VALUES(?,?,?)", (u, security.hash_password(p), cid))
        for c in _CUSTOMERS:
            acct = c["accounts"][0][0]
            for d in range(45, 1, -1):
                for _ in range(rng.choice([0, 1, 1, 2])):
                    m, cat, lo, hi = rng.choice(MERCHANTS)
                    ts = (now - dt.timedelta(days=d)).replace(hour=rng.randint(8, 21), minute=rng.randint(0, 59))
                    add_txn(c["customer_id"], acct, m, cat, rng.uniform(lo, hi), channel=rng.choice(["POS", "UPI", "ONLINE"]), ts=ts)
                if d % 30 == 1:
                    add_txn(c["customer_id"], acct, "Salary Credit", "Income", c["monthly_income"], "credit", "NEFT",
                            ts=now - dt.timedelta(days=d))
        y = now - dt.timedelta(hours=14)   # planted anomalies for the fraud demo (Priya)
        add_txn("C002", "A-2001", "CryptoXchange Ltd", "Crypto", 85000, channel="ONLINE", country="SG", ts=y.replace(hour=2, minute=14))
        add_txn("C002", "A-2001", "LuxeWatch Dubai", "Luxury", 62500, channel="ONLINE", country="AE", ts=y.replace(hour=2, minute=31))
        add_txn("C002", "A-2001", "Unknown Merchant 7731", "Other", 9999, channel="ONLINE", country="RO", ts=y.replace(hour=2, minute=40))


def reset_demo() -> None:
    """Delete the database file and re-seed. Use if demo data has become stale."""
    global _conn
    with LOCK:
        if _conn is not None:
            _conn.close()
            _conn = None
        for suffix in ("", "-wal", "-shm"):
            Path(_path() + suffix).unlink(missing_ok=True)
        conn()


if __name__ == "__main__":
    import sys
    if "--reset" in sys.argv:
        reset_demo()
        print("Database reset and re-seeded at", _path())
    else:
        conn()
        print("Database ready at", _path())
