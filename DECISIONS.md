# Engineering Decisions

## 1. Stack Selection

Framework: Python + FastAPI
I chose FastAPI because I have used it before and I have a good understanding of it. It also gives great features like automatic /docs swagger UI, which made testing the endpoints very easy.

Database: SQLite (WAL Mode)
Because this is a hackathon project, I used a lightweight database which is SQLite so external setup time is avoided.
Setting PRAGMA journal_mode=WAL; allows reading and writing to happen smoothly without database lock errors. It makes the app very easy to run locally or deploy to Render without needing a separate database server.

AI Model: Groq API (llama-3.3-70b-versatile)
I used Groq because it has free models, runs fast, and is simple to set up. It gets the job done quickly for extracting JSON sentiment and priority levels from passenger feedback.

## 2. Code vs AI Boundary

I decided to keep all real business rules in code and only use the AI for reading feedback text.

What I kept in Code:
- Seat map state (available, held, booked).
- Double-booking prevention using SQLite transactions (BEGIN IMMEDIATE).
- 15-minute seat hold timer.
- Refund calculations (100%, 75%, 40%, 0%).
- Blocking repeat cancellations from giving double refunds.
- Cleaning email IDs and phone numbers locally before sending text to AI.

What I gave to AI:
- Reading post-trip passenger comments.
- Figuring out sentiment (POSITIVE, NEUTRAL, NEGATIVE, STRONGLY_NEGATIVE).
- Rating urgency into 4 levels (CRITICAL, HIGH, MEDIUM, LOW).
- Writing a 1-sentence summary of the complaint.

I did this because money, refunds, and seat inventory must be 100% exact and consistent. AI can make mistakes, so AI should only read feedback text, not handle payments or seat locking.

## 3. Hold Time and Refund Rules

15-Minute Seat Hold:
15 minutes gives the passenger enough time to select seats and enter payment details without holding seats blocked for too long if they leave.

4-Tier Refund Policy:
I used the category method for refunding instead of a formula-based refunding because it is simpler for the user to understand how much refund they will get at what time.
- More than 48 hours before departure: 100% Full Refund.
- 24 to 48 hours before departure: 75% Refund.
- 6 to 24 hours before departure: 40% Refund.
- Less than 6 hours before departure: 0% Refund.

Payment & Refunds:
For payment I have assumed it is online and I have simulated it for the purpose of this project.
For refunds, the backend takes the payment info from the database to process the refund.

Abuse Control Policy:
If a user cancels 3 or more times in the last-minute window (<6 hours), they get blocked (is_restricted = 1) from placing new holds or bookings.
I only applied this penalty to <6h cancellations because before 6 hours we can still re-sell the seat to someone else, so it blocks fewer real users by accident.

## 4. Live Deployment

I have deployed the app on Render. You can access the web app on https://scalebus.onrender.com/ and use it for testing.

## 5. Local Run Instructions

If you want to run it locally, you can run these commands on a Windows machine:

Step 1: Clone the repo
```bash
git clone https://github.com/mohammedpithapur/scalebus.git
cd scaletechbus
```

Step 2: Create virtual environment
```powershell
python -m venv venv
.\venv\Scripts\activate
```

Step 3: Install dependencies
```bash
pip install -r requirements.txt
```

Step 4: Set up local .env file
```powershell
Set-Content -Path .env -Value "GROQ_API_KEY=your_groq_api_key_here`nHOLD_GRACE_PERIOD_SECONDS=900"
```

Step 5: Start the server
```bash
python main.py
```

## 6. Engineering Trade-Offs

- SQLite vs Postgres: I used SQLite so anyone can clone and run the project immediately without setting up a Postgres server. In a massive production system with multiple servers, I would switch to Postgres or Redis locks.
- Direct DB Queries vs Redis: I fetched seat maps directly from SQLite because the database is small and fast. In a real company like RedBus, seat maps would be cached in Redis for faster loading.

## 7. Future Improvements for Production

If I were launching this for millions of users:
1. Add Redis for caching seat maps and holding seats with TTL timers.
2. Add a background queue (like Celery) to send alert notifications for CRITICAL safety complaints.
3. Add JWT login and API rate limiting to prevent spam.
