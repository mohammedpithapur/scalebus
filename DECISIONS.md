# Engineering Design Decisions

I built this bus seat booking and cancellation engine to tackle two main challenges: strict, conflict-free seat allocation under high concurrency, and automated passenger feedback triage using LLMs without exposing sensitive user data.

Here is a breakdown of the architectural choices, trade-offs, and design boundaries made during development.

---

## 1. Stack Selection & Rationale

- **Framework**: Python 3.14 + FastAPI + Pydantic v2
  FastAPI was chosen for its async I/O performance, automatic Pydantic request/response validation, and zero-configuration OpenAPI (`/docs`) generation. This made testing and verifying all required endpoints straightforward.

- **Database**: SQLite in Write-Ahead Logging (WAL) Mode (`PRAGMA journal_mode=WAL;`)
  SQLite WAL mode allows concurrent read queries while a write transaction is executing. It eliminates external database setup overhead while supporting full ACID transactions. For local testing and production deployment on single-instance web services (like Render), it provides persistence with zero ops management.

- **AI Feedback Model**: Groq API (`llama-3.3-70b-versatile`) with multi-provider fallback
  Groq's Llama 3.3 70B model delivers sub-500ms inference times for structured JSON extraction. The service layer is decoupled so it can fall back to Gemini or OpenRouter if needed.

---

## 2. Drawing the Boundary: Code vs. LLM

A key design rule followed in this project: **Never let an AI model manage financial calculations, inventory state, or business rules.**

### What Is Handled Purely in Deterministic Code:
- **Seat Inventory & State Transitions**: Tracking seat states (`available`, `held`, `booked`).
- **Concurrency & Double-Booking Prevention**: Using SQLite `BEGIN IMMEDIATE` transactions to acquire write locks before checking seat availability. If two users attempt to book seat `A1` at the exact same millisecond, the second transaction is blocked until the first finishes, correctly returning HTTP 409 Conflict.
- **Hold Expiration Math**: 5-minute hold grace period evaluated lazily on read/write operations.
- **Refund Policy Enforcement**: Exact percentage refund math based on departure hours (`>24h` = 100%, `6-24h` = 50%, `<6h` = 0%).
- **Cancellation Idempotency**: Storing refund amounts so repeat cancellation calls return identical refund values without double-refunding.
- **Local PII Scrubbing**: Running regex sanitization locally to strip email addresses, phone numbers, and payment cards *before* any text reaches the LLM.

### What Is Delegated to the LLM:
- **Passenger Review Understanding**: Categorizing post-trip feedback into sentiment (`POSITIVE`, `NEUTRAL`, `NEGATIVE`, `STRONGLY_NEGATIVE`), tag categories (`AC`, `DRIVER_BEHAVIOR`, `PUNCTUALITY`, `SAFETY`), 1-sentence summaries, and 4 priority levels (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`).

### Why This Separation Matters:
LLMs are probabilistic. Asking an LLM "How much refund should this customer get?" introduces risks like prompt injection or unpredictable output. Code handles money and seats; LLMs handle natural language comprehension.

---

## 3. Hold Grace Period & Refund Policies

- **5-Minute Seat Hold Grace Period**:
  Five minutes gives a passenger sufficient time to select seats and enter payment details without hoarding inventory on busy routes. If payment isn't confirmed within 300 seconds, the hold expires automatically.

- **Time-Window Refund Policy**:
  - `> 24 hours to departure`: **100% Refund**. High likelihood of reselling the seat.
  - `6 to 24 hours to departure`: **50% Refund**. Partial cost coverage for short-notice cancellation.
  - `< 6 hours to departure`: **0% Refund**. High risk of empty seat on departure.

- **Abuse Restriction Policy**:
  Passengers who accumulate 3 or more cancellations in the zero-refund window (<6h) are flagged with `is_restricted = 1`, blocking future holds/bookings (`HTTP 403 Forbidden`).

---

## 4. Engineering Trade-Offs

1. **SQLite WAL vs PostgreSQL**:
   SQLite WAL was selected to make the project instantly runnable everywhere without setting up Postgres containers. In a multi-region distributed cluster, PostgreSQL with row-level locks (`SELECT ... FOR UPDATE`) or Redis distributed locking (`Redlock`) would be used instead.
2. **Synchronous DB Access vs Async Connection Pool**:
   Given SQLite's file-level locking nature, synchronous connection contexts per request avoided thread starvation while keeping transaction boundaries simple and safe.

---

## 5. What I Would Add for Production at RedBus Scale

If taking this service to production for millions of daily bookings:
1. **Redis Caching & Distributed Locks**: Offloading seat map reads to Redis and using Redis keys with TTL for 5-minute seat holds.
2. **Background Task Queue**: Using Celery/RQ for processing background hold expirations and sending PagerDuty alerts for `CRITICAL` passenger feedback.
3. **Database Migrations**: Alembic for tracking DB schema versioning.
4. **JWT Authentication & Rate Limiting**: Securing user endpoints with OAuth2/JWT tokens and rate-limiting IP addresses.
