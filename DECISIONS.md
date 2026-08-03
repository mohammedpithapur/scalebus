# Architecture & Engineering Decisions (DECISIONS.md)

## 1. Technology Stack Choice
- **Framework**: Python 3.14 + FastAPI + Pydantic v2.
  - **Reasoning**: FastAPI provides asynchronous request handling, automatic schema validation with Pydantic, and instant interactive OpenAPI documentation (`/docs`). This makes testing all 5 required endpoints seamless.
- **Database**: SQLite with Write-Ahead Logging (WAL) mode (`PRAGMA journal_mode=WAL;`) and foreign keys enabled.
  - **Reasoning**: SQLite in WAL mode allows concurrent readers and writers without lock contention. For a hackathon context, it offers zero setup overhead, zero external dependencies, and easy reproducibility while supporting full ACID transactions.
- **AI Integration**: Multi-provider LLM Client supporting Google Gemini (`GEMINI_API_KEY`), Groq (`GROQ_API_KEY`), and OpenRouter (`OPENROUTER_API_KEY`), with a local rule-based fallback for environments without an active API key.

---

## 2. Solution Structure & Boundary (Code vs. LLM)

### What is Kept in Code (Deterministic Engine):
- **Seat Map & Inventory Management**: Tracking real-time statuses (`available`, `held`, `booked`).
- **Concurrency & Race Condition Prevention**: SQLite `BEGIN IMMEDIATE` transactions to atomically lock seat inventory during hold creation and booking confirmation, guaranteeing that two passengers can never claim the same seat.
- **Grace Period Enforcement**: Lazy evaluation on read/hold + background status cleanup.
- **Time-Window Refund Math**: Precise calculation based on hours remaining until trip departure (`>24h` = 100%, `6-24h` = 50%, `<6h` = 0%).
- **Idempotency**: Storing refund amounts and transaction states so repeat requests return identical results without double-crediting.
- **PII Scrubbing**: Local Regex sanitization removing email addresses, phone numbers, and payment details before any text is dispatched to external AI models.

### What is Delegated to the LLM (Probabilistic AI):
- **Post-Trip Feedback Analysis**:
  1. **Sentiment Classification**: `POSITIVE`, `NEUTRAL`, `NEGATIVE`, `STRONGLY_NEGATIVE`.
  2. **Categorization Tags**: E.g., `AC`, `DRIVER_BEHAVIOR`, `PUNCTUALITY`, `CLEANLINESS`, `SAFETY`.
  3. **Urgency Flagging**: Identifying angry passengers, safety issues, or severe driver complaints that require immediate customer support escalation (`urgent_followup: true`).
  4. **Feedback Summarization**: 1-sentence executive summary.

### Why We Drew the Line Here:
- Financial transactions, seat reservations, and refund algorithms must be 100% deterministic, audit-friendly, and repeatable. Letting an LLM decide refund math or seat availability introduces unpredictability and security vulnerabilities (e.g., prompt injection). 
- Conversely, understanding natural language passenger feedback and assessing emotional urgency is where LLMs excel beyond rigid keyword patterns.

---

## 3. Grace Period & Refund Windows

### Seat Hold Grace Period: 5 Minutes (300 Seconds)
- **Reasoning**: 5 minutes gives passengers adequate time to review passenger details and complete payment gateway authentication. A longer period (e.g., 15 minutes) risks inventory hoarding during high-demand peak hours, while a shorter period (e.g., 2 minutes) leads to high payment drop-off rates.

### Refund Time Windows:
- **> 24 Hours before departure**: **100% Full Refund**
  - *Reasoning*: The bus operator has sufficient lead time to re-list and resell the cancelled seat to another traveler.
- **6 Hours to 24 Hours before departure**: **50% Half Refund**
  - *Reasoning*: Partial compensation covering operational costs while incentivizing early cancellations so seats can still be re-allocated.
- **< 6 Hours before departure**: **0% Zero Refund**
  - *Reasoning*: Extremely short notice makes it highly unlikely for the operator to fill the empty seat before departure, representing direct lost revenue.

### Late Cancellation Risk Policy:
- Passengers who accumulate **3 or more cancellations within the 0% refund window (<6h)** are automatically flagged with `is_restricted = 1`.
- **Policy Enforcement**: Restricted passengers are blocked (`403 Forbidden`) from placing new seat holds or bookings, protecting bus operators against speculative booking abuse.

---

## 4. Trade-Offs & What Was Deliberately Skipped

- **SQLite vs. PostgreSQL**: SQLite WAL was chosen for zero-dependency portability. In a high-throughput multi-node production setup, PostgreSQL with `SELECT ... FOR UPDATE` row locks or Redis distributed locks (`Redlock`) would be preferred.
- **In-Memory Cache vs. Direct DB Queries**: Seat maps are fetched directly from SQLite. A production architecture would use Redis cache for high-read seat map endpoints.
- **Payment Gateway Integration**: Payment reference is accepted as an incoming token rather than triggering a live Stripe/Razorpay SDK flow.

---

## 5. Production Readiness Roadmap

Before deploying this engine to a high-scale production environment (e.g., RedBus scale), we would add:
1. **Distributed Locking & Caching**: Redis for atomic seat hold keys with auto-TTL, offloading read traffic from the primary database.
2. **Asynchronous Background Sweep**: Celery / Redis Queue (RQ) for sweeping expired holds and dispatching urgent feedback notifications via Slack/PagerDuty.
3. **Database Migrations**: Alembic for managing database schema versioning.
4. **Rate Limiting & Authentication**: JWT authentication middleware and API rate limiting per IP/user.
5. **Observability**: Prometheus metrics (track hold expiration rates, cancellation percentages, AI latency) and OpenTelemetry tracing.
