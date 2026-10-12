"""Knowledge / RAG layer (#7). A tiny BM25-style retriever over bank policy documents.

In production this would be a vector DB + BM25 hybrid with a reranker; here we keep the same
interface (`search(query, k)`) so the Credit agent can ground its answers in policy text.
"""
from __future__ import annotations

import math
import re
from collections import Counter

POLICIES = [
    dict(id="POL-CREDIT-01", title="Personal loan eligibility",
         text="Personal loans require KYC status VERIFIED, minimum monthly income of 25000 and credit score of 650 or above. "
              "Applicants with score 650 to 699 are priced at 14.5 percent, 700 to 749 at 12 percent, 750 and above at 10.5 percent."),
    dict(id="POL-CREDIT-02", title="Debt burden (FOIR) limit",
         text="Total fixed obligations to income ratio FOIR including the proposed EMI must not exceed 50 percent of net monthly income. "
              "Cases above 50 percent are declined or must reduce amount or extend tenure."),
    dict(id="POL-CREDIT-03", title="Loan amount caps",
         text="Maximum unsecured personal loan is 20 times net monthly income subject to an absolute cap of 2500000 rupees. "
              "Tenure may range from 12 to 72 months."),
    dict(id="POL-FRAUD-01", title="Suspicious transaction handling",
         text="Transactions scoring 70 or above are high risk. Customer card must be offered for immediate blocking and the case "
              "escalated to the fraud operations team. Foreign online transactions at night with high value are high risk indicators."),
    dict(id="POL-PAY-01", title="Fund transfer approvals",
         text="Transfers above 25000 rupees require explicit human approval before execution. Daily transfer limit is 200000 rupees. "
              "Transfers to beneficiaries not registered are rejected."),
    dict(id="POL-AML-01", title="AML screening of beneficiaries",
         text="Every beneficiary is screened against OFAC, UN, RBI caution and PEP watch-lists before payment. Flagged "
              "beneficiaries are rejected and referred to compliance. If screening is unavailable payments fail closed."),
    dict(id="POL-KYC-01", title="KYC requirements",
         text="Customers with KYC status PENDING cannot receive new credit products. Complete video KYC or visit a branch with "
              "identity and address proof to become VERIFIED."),
]


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def search(query: str, k: int = 3) -> list[dict]:
    docs = [(_tok(p["title"] + " " + p["text"]), p) for p in POLICIES]
    avgdl = sum(len(d) for d, _ in docs) / len(docs)
    df = Counter(t for d, _ in docs for t in set(d))
    n = len(docs)
    q = _tok(query)
    scored = []
    for toks, p in docs:
        tf = Counter(toks)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(toks) / avgdl))
        scored.append((s, p))
    scored.sort(key=lambda x: -x[0])
    return [dict(id=p["id"], title=p["title"], text=p["text"], score=round(s, 2)) for s, p in scored[:k] if s > 0]
