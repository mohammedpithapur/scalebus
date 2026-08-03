# RedBus Bus Seat Booking & Cancellation Engine

A robust, concurrent-safe backend engine for bus seat inventory management, temporary grace-period holds, time-window refund math, idempotency, passenger risk control, and AI-driven feedback sentiment analysis.

---

## Live Deployment & Interactive Docs
- 🚌 **Live Web App & Dashboard**: [https://scalebus.onrender.com/](https://scalebus.onrender.com/)
- 📖 **Interactive OpenAPI Docs**: [https://scalebus.onrender.com/docs](https://scalebus.onrender.com/docs)

---

## AI Tools & Provider Integration
- **Antigravity AI Agent**: System architecture design, code generation, test suite crafting, and documentation.
- **Live LLM Integration (Groq API / `llama-3.3-70b-versatile`)**: Real-time sentiment analysis, tag extraction, 4-tier priority classification (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`), and urgent feedback triage.

---

## Features & Highlights

1. **Real-Time Seat Map (`GET /trips/{id}/seats`)**: Returns seat status (`available`, `held`, `booked`) with automatic hold expiration on access.
2. **Atomic Seat Hold (`POST /seats/hold`)**: 15-minute (900s) hold grace period. Uses SQLite `BEGIN IMMEDIATE` transactions to prevent concurrent double-booking.
3. **Idempotent Booking Confirmation (`POST /bookings`)**: Converts active holds into confirmed bookings OR supports direct booking. Repeat calls return existing booking details.
4. **4-Tier Refund Engine (`POST /bookings/{id}/cancel`)**:
   - `> 48 hours` to departure: **100% Full Refund**
   - `24h to 48h` to departure: **75% Refund**
   - `6h to 24h` to departure: **40% Refund**
   - `< 6 hours` to departure: **0% Zero Refund**
   - **Idempotent**: Multiple cancellation requests return identical refund amounts without double crediting.
5. **Abuse Prevention Policy**: Passengers with 3+ cancellations inside the zero-refund window (<6h) are automatically restricted (`PASSENGER_RESTRICTED`).
6. **AI Feedback & Sentiment Engine (`POST /bookings/{id}/feedback`)**:
   - Local **PII Sanitization**: Redacts emails, phone numbers, and payment details before calling the LLM.
   - **LLM Sentiment Classification**: Categorizes feedback (`POSITIVE`, `NEUTRAL`, `NEGATIVE`, `STRONGLY_NEGATIVE`), generates concise summary, extracts category tags, and rates urgency into 4 priority levels.

---

## Getting Started

### 1. Installation & Environment Setup
Install required dependencies:
```bash
pip install -r requirements.txt
```

Your `.env` file should contain your Groq API key and 15-minute grace period:
```env
GROQ_API_KEY=your_groq_api_key_here
HOLD_GRACE_PERIOD_SECONDS=900
```

---

## How to Run the Server Locally

Start the FastAPI application with Uvicorn:
```bash
python -m uvicorn main:app --reload --port 8000
```
Or directly:
```bash
python main.py
```

### Interactive API Documentation (Swagger UI)
Once running, open your browser and navigate to:
👉 **[http://localhost:8000/docs](http://localhost:8000/docs)**

---

## How to Run Automated Tests

Execute the comprehensive test suite verifying holds, expiry, concurrency, refund math, double cancellation idempotency, PII scrubbing, and live Groq AI sentiment analysis:
```bash
python -m pytest test_app.py -p no:cacheprovider -v
```

---

## API Endpoints Reference & Example Requests

### 1. Get Seat Map
```bash
curl -X GET "http://localhost:8000/trips/TRIP-101/seats"
```

### 2. Place Temporary Seat Hold
```bash
curl -X POST "http://localhost:8000/seats/hold" \
     -H "Content-Type: application/json" \
     -d '{
       "trip_id": "TRIP-101",
       "seat_numbers": ["A1", "A2"],
       "user_id": "USER-101"
     }'
```

### 3. Confirm Booking
```bash
curl -X POST "http://localhost:8000/bookings" \
     -H "Content-Type: application/json" \
     -d '{
       "hold_id": "HOLD-XXXXXX",
       "user_id": "USER-101",
       "payment_reference": "PAY-TXN-123456"
     }'
```

### 4. Cancel Booking & Calculate Refund
```bash
curl -X POST "http://localhost:8000/bookings/BOOK-XXXXXX/cancel" \
     -H "Content-Type: application/json" \
     -d '{
       "reason": "Plans changed"
     }'
```

### 5. Submit Post-Trip AI Feedback
```bash
curl -X POST "http://localhost:8000/bookings/BOOK-XXXXXX/feedback" \
     -H "Content-Type: application/json" \
     -d '{
       "feedback_text": "The driver was driving recklessly and angry! Call me at +15551112222 or email john@example.com."
     }'
```
