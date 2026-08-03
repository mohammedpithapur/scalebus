# Hackathon Live Evaluation & Defense Guide

Use this reference to confidently explain your technical architecture, decisions, and code implementation to hackathon evaluators.

---

## 1. Executive Summary (The 30-Second Elevator Pitch)
> "We built a production-grade Bus Seat Booking and Cancellation engine in Python with FastAPI and SQLite WAL mode. The system solves three core challenges:
> 1. **Atomic Seat Holds**: Prevents double-booking using database-level `BEGIN IMMEDIATE` transaction locks during 5-minute grace period holds.
> 2. **Time-Window Refund Math**: Calculates exact refund percentages (100% >24h, 50% 6-24h, 0% <6h) with strict idempotency to prevent double-refunding.
> 3. **AI Passenger Feedback Analysis**: Uses Groq LLM API (`llama-3.3-70b-versatile`) for sentiment classification, tag extraction, and urgent follow-up flagging, while enforcing local Regex PII redaction so user emails, phone numbers, and payment cards are never sent to external AI."

---

## 2. Anticipated Evaluator Questions & Your Answers

### Q1: "How do you prevent two passengers from booking the exact same seat at the same time?"
- **Answer**: 
  - "We enforce concurrency safety at the database level rather than application memory.
  - When `/seats/hold` or `/bookings` is called, we start a `BEGIN IMMEDIATE` transaction in SQLite (which has Write-Ahead Logging enabled).
  - Inside the lock, we re-verify that all requested seats are currently in `available` state.
  - If two users request seat `A1` at the exact same millisecond, the first transaction acquires the write lock and changes `A1` to `held`. The second transaction fails the availability check and returns `409 Conflict` (Seat already held or booked)."

---

### Q2: "Why did you choose 5 minutes for the hold grace period, and how do you handle hold expiration?"
- **Answer**: 
  - **Grace Period**: "5 minutes (300 seconds) is optimal — it gives passengers enough time to enter payment details without locking up high-demand bus inventory indefinitely."
  - **Expiration Mechanism**: "We use a dual strategy:
    1. **Lazy expiration on read/write**: Every time `/trips/{id}/seats` or `/seats/hold` is called, our `expire_outdated_holds()` query automatically identifies holds where `expires_at < current_time` and converts those seats back to `available`.
    2. **Background sweep**: In production, an asynchronous worker (e.g. Celery or Redis TTL) handles background sweeps."

---

### Q3: "What line did you draw between what code decides versus what the LLM decides?"
- **Answer**: 
  - **Code (Deterministic)**: "Seat availability, price calculation, transaction locks, refund percentages, idempotency, and PII sanitization are 100% written in code. Financial math and seat inventory must be audit-friendly, predictable, and immune to prompt injection."
  - **LLM (Probabilistic AI)**: "The LLM only analyzes freeform post-trip passenger feedback to detect sentiment (`POSITIVE`, `NEUTRAL`, `NEGATIVE`, `STRONGLY_NEGATIVE`), summarize issues, extract category tags (`AC`, `DRIVER_BEHAVIOR`, `PUNCTUALITY`), and flag urgent complaints needing support follow-up."

---

### Q4: "How do you protect passenger privacy (PII) before sending data to the AI?"
- **Answer**: 
  - "Before any feedback string reaches the LLM API, our `sanitize_pii()` function runs local regular expressions to redact:
    - Email addresses (`[EMAIL REDACTED]`)
    - Phone numbers (`[PHONE REDACTED]`)
    - Credit card / Account numbers (`[CARD REDACTED]`)
  - Only the sanitized text is sent over HTTP to the Groq/Gemini API, fulfilling strict privacy rules."

---

### Q5: "How does your system handle repeat cancellation requests or user abuse?"
- **Answer**: 
  - **Repeat Cancellations (Idempotency)**: "If `/bookings/{id}/cancel` is called twice for the same booking ID, the system checks `status == CANCELLED` and returns the exact refund amount previously calculated without deducting or crediting funds twice."
  - **Passenger Risk Policy**: "Passengers who cancel 3 or more times within the zero-refund window (<6 hours to departure) have their user profile marked as `is_restricted = 1`. Any subsequent attempt by a restricted user to place a seat hold returns `403 Forbidden`."

---

## 3. How to Demonstrate live to Judges

1. **Open Swagger Docs**: Go to `http://localhost:8000/docs` in your browser.
2. **Show Seat Map**: Call `GET /trips/TRIP-101/seats` to show 20 available seats.
3. **Place Hold**: Call `POST /seats/hold` with seats `["A1", "A2"]`. Show status becomes `held`.
4. **Show Conflict**: Call `POST /seats/hold` with seat `["A2"]` from another user. Show `409 Conflict`.
5. **Confirm Booking**: Call `POST /bookings` using the hold ID. Show status becomes `CONFIRMED`.
6. **Cancel Booking**: Call `POST /bookings/{id}/cancel` on `TRIP-101` (>24h). Show 100% refund. Re-call to demonstrate idempotency.
7. **AI Feedback**: Call `POST /bookings/{id}/feedback` with a string containing a phone number, email, and angry complaint about a reckless driver. Show PII redaction and `urgent_followup: true` from the live Groq API!
