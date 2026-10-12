# Architecture & Approach

## Goal
Deliver a *working MVP* of the reference architecture in 48h: keep the layered shape, implement the parts that make the
multi-agent behaviour real and safe, and stub the rest honestly.

## Request flow
```
Web UI ─▶ FastAPI (rate limit, validation)
        ─▶ AI Gateway guardrails (PII masking, prompt-injection / off-topic block)
        ─▶ Supervisor (LLM intent classification → 1-2 agents; rule-based fallback)
        ─▶ Specialist agent(s): LLM tool-use loop limited to that agent's tools
        ─▶ Tool layer (registry → permission boundary → schema validation → simulated API → audit log)
        ─▶ Supervisor aggregates → output guardrail → UI (reply + agent trace + approval cards)
```

## Mapping to the reference diagram
| Layer | Status | Implementation |
|---|---|---|
| 1 Channels | Simulated | Web chat UI |
| 2 API & Experience | Implemented (subset) | FastAPI, JWT auth + scrypt passwords + lockout, Pydantic validation, per-user rate limiting, SSE streaming, persisted chat memory |
| 3 AI Gateway | Implemented (subset) | PII detection/masking, prompt-injection & content policy, output redaction, model selection via env, human-in-the-loop |
| 4 Multi-Agent Orchestrator | Implemented | Supervisor + Customer/Banking, Risk/Fraud, Credit/Loan, Payments agents |
| 5 Agent Tool / MCP | Implemented | Tool registry, JSON schemas, authorization (customer id injected server-side), schema validation, permission boundary, transaction approval, audit logging |
| 6 Enterprise Data | Implemented | SQLite persistence (customers, accounts, transactions, approvals, audit, chat history); atomic debit transactions |
| 7 Knowledge / RAG | Implemented (lite) | BM25-style retriever over 6 policy docs; Credit agent cites policy IDs |
| 8 External services | Implemented (simulated providers) | `external.py`: credit bureau, KYC/AML, PAN API, UPI/IMPS network, market data; timeout/retry, circuit breaker, call log, status UI. Fail-closed on AML, graceful fallback on bureau |
| 9 Cross-cutting | Partial | Observability = per-request agent trace + latency; Audit = `/api/audit`; Evaluation = pytest suite |

## Key design decisions (be ready to explain these)
1. **Supervisor + specialists instead of one mega-prompt** – each agent has a small prompt and only the tools it needs,
   improving accuracy and limiting blast radius.
2. **LLMs reason, code decides** – eligibility (KYC, score, FOIR), fraud scoring and transfer limits are deterministic
   functions; the model orchestrates and explains. This makes money-moving decisions auditable and testable.
3. **Security by construction** – `customer_id` is never an LLM argument (injected from the session), so a prompt
   cannot read another customer's data; unknown args are dropped; agents cannot call tools outside their boundary.
4. **Human-in-the-loop** – transfers > ₹25,000 return `PENDING_APPROVAL`; money moves only after the user clicks Approve
   (approval is single-use, preventing double execution). The agent prompt forbids claiming success before `EXECUTED`.
5. **Graceful degradation** – LLM errors/timeouts fall back to offline logic with the same tools, and the trace says so.
6. **Explainability** – the UI shows routing rationale, each tool call with args, latency and result.
7. **Simulated real-time** – a background task streams transactions, scored by the same fraud engine, to the live feed.

## Limitations / next steps
Single-process SQLite (use Postgres to scale), no MFA/refresh tokens, external providers are simulated, simple retriever (no embeddings/reranker), no streaming of tokens,
no external bureau/KYC, evaluation limited to unit tests. Next: Postgres, OAuth2, pgvector hybrid search,
LLM-as-judge evals for routing accuracy, SSE token streaming.

## Streaming design
The orchestrator is a Python generator yielding events (guardrail, route, agent, tool, token, approval, done). FastAPI forwards them as SSE; the browser renders tokens as they arrive. Routing uses a small fast model; multi-agent answers stream sequentially (no extra synthesis pass) to minimise time-to-first-word. Providers (Anthropic / OpenAI-compatible) sit behind one neutral message format in `llm.py`.
