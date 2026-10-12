# Submission checklist – AI/ML Intern Technical Assignment (Wrench Wise)

## A. Task requirements ("at a minimum, demonstrate")
| # | Requirement from the brief | Status | Evidence |
|---|---|---|---|
| 1 | Supervisor/Orchestrator agent to understand and route requests | ✅ | `agents.py` → `route_llm`, `route_rules`, `run`; visible in the **Agent trace** panel |
| 2 | At least 3 specialised agents | ✅ (5) | Customer & Banking, Risk & Fraud, Credit & Loan, Payments, Financial Advisory |
| 3 | Simulated banking APIs/tools (customer, transaction, loan info) | ✅ | `tools.py` (≈15 tools) over SQLite; external providers in `external.py` |
| 4 | Natural-language interaction using an LLM/AI model | ✅ | `llm.py` – Claude or any OpenAI-compatible model, streaming + tool use *(needs an API key or local model in `.env`)* |
| 5 | 3–5 working banking use cases | ✅ | 1 Account & spending · 2 Fraud detection/card block · 3 Loan eligibility + KYC · 4 Transfers with approval · 5 Market & saving advice |
| 6 | Simple, user-friendly interface | ✅ | Login + chat + overview + live feed + agent trace |

## B. Deliverables to submit
| # | Deliverable | What to do |
|---|---|---|
| 1 | Source code / GitHub repository | `git init && git add . && git commit -m "BankMind"` → push to a **public repo** (`.env` and `data/` are git-ignored) |
| 2 | README with setup and execution instructions | ✅ `README.md` (one-command: `./run.sh` or `run.bat`) |
| 3 | Short demo video (5–10 min) | ⏳ **You record it** – follow `docs/DEMO_SCRIPT.md` (≈7 min). Set a real LLM key first so replies are AI-generated |
| 4 | Brief explanation of architecture and approach | ✅ `docs/ARCHITECTURE.md` (diagram, flow, decisions, limitations) |

**Deadline:** the email says *before EOD of 12 Sep 2026* but was sent on 9 Oct 2026 – **confirm the intended date with HR** (it is most likely 12 Oct 2026).

## C. Evaluation criteria → where to look
| Criterion | Evidence |
|---|---|
| Problem-solving approach | `docs/ARCHITECTURE.md` §5 design decisions |
| AI/ML understanding | tool-use loop with streaming, intent routing, guardrails, RAG-lite policy retrieval, deterministic fraud scoring |
| Agent architecture | Supervisor + least-privilege specialists, permission boundary, human-in-the-loop |
| Implementation quality | 26 automated tests (`python -m pytest -q tests`), typed schemas, error handling, graceful degradation |
| Working solution | `./run.sh` → http://localhost:8000 |

## D. Pre-submission steps (5 minutes)
1. Put a real key in `.env` (or run Ollama) → send "Can I get a loan of 8 lakh for 2 years?" and confirm the reply streams in.
2. `python -m pytest -q tests` → all green.
3. Sign in as each demo user once; run the demo script; record the video.
4. Deploy with `docs/DEPLOY.md` to get a public URL. Push to GitHub; paste the live link, repo link, video link and `docs/ARCHITECTURE.md` into the reply email.

## E. Likely technical-discussion questions
* **Why a supervisor?** Specialisation, least-privilege tools, smaller prompts, testability; routing can use a cheap model.
* **How do you stop prompt injection / data leaks?** Input guardrails, tools never take `customer_id` from the model, tool results treated as data, permission boundary, per-user token.
* **Why not let the LLM decide loan approval?** Regulated, must be deterministic and auditable; LLM explains the result.
* **What if the LLM or a provider is down?** Backup routing/logic; circuit breaker; bureau fallback; payments never debit without network success; AML fails closed.
* **How would you scale it?** Postgres, stateless API replicas, queue for tool calls, real MCP gateway, vector DB + reranker, token budgets, evaluation harness.
* **How do you evaluate agents?** Unit tests for tools; routing accuracy on a labelled prompt set; LLM-as-judge for answer quality; trace review.
