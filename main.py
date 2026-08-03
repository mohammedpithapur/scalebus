import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from database import init_db, get_db
from seed import seed_database
from models import (
    TripSeatMapResponse,
    HoldSeatsRequest,
    HoldSeatsResponse,
    ConfirmBookingRequest,
    ConfirmBookingResponse,
    CancelBookingRequest,
    CancelBookingResponse,
    SubmitFeedbackRequest,
    FeedbackResponse
)
from services import (
    get_trip_seat_map,
    create_seat_hold,
    confirm_booking,
    cancel_booking,
    process_feedback,
    get_urgent_feedbacks
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_database()
    yield

app = FastAPI(
    title="RedBus Seat Booking & Cancellation Engine",
    description="High-reliability engine for bus seat inventory, concurrent holds, time-window refund math, and AI-driven passenger feedback analysis.",
    version="1.0.0",
    lifespan=lifespan
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", tags=["Dashboard"])
def root():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "status": "online",
        "service": "Bus Seat Booking & Cancellation Engine",
        "documentation": "/docs"
    }

@app.get("/trips", tags=["Trips"])
def list_trips(date: Optional[str] = Query(None, description="Filter trips by travel date YYYY-MM-DD")):
    """List available trips in the system, optionally filtered by travel date (YYYY-MM-DD)."""
    with get_db() as conn:
        cursor = conn.cursor()
        if date:
            cursor.execute("SELECT * FROM trips WHERE departure_time LIKE ? ORDER BY departure_time ASC", (f"{date}%",))
        else:
            cursor.execute("SELECT * FROM trips ORDER BY departure_time ASC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

# API 1: GET /trips/{id}/seats
@app.get(
    "/trips/{id}/seats",
    response_model=TripSeatMapResponse,
    tags=["Seats"],
    summary="Get seat map for a trip"
)
def get_seats(id: str = Path(..., description="Trip ID (e.g. TRIP-101)")):
    return get_trip_seat_map(id)

# API 2: POST /seats/hold
@app.post(
    "/seats/hold",
    response_model=HoldSeatsResponse,
    tags=["Seats"],
    summary="Place a temporary hold on seats"
)
def place_hold(request: HoldSeatsRequest):
    return create_seat_hold(
        trip_id=request.trip_id,
        seat_numbers=request.seat_numbers,
        user_id=request.user_id
    )

# API 3: POST /bookings
@app.post(
    "/bookings",
    response_model=ConfirmBookingResponse,
    tags=["Bookings"],
    summary="Confirm booking from an active hold OR direct book"
)
def create_booking(request: ConfirmBookingRequest):
    return confirm_booking(
        hold_id=request.hold_id,
        user_id=request.user_id,
        payment_reference=request.payment_reference,
        trip_id=request.trip_id,
        seat_numbers=request.seat_numbers
    )

# API 4: POST /bookings/{id}/cancel
@app.post(
    "/bookings/{id}/cancel",
    response_model=CancelBookingResponse,
    tags=["Bookings"],
    summary="Cancel booking and process refund"
)
def cancel(
    id: str = Path(..., description="Booking ID (e.g. BOOK-12345678)"),
    request: CancelBookingRequest = CancelBookingRequest()
):
    return cancel_booking(booking_id=id, reason=request.reason)

# API 5: POST /bookings/{id}/feedback
@app.post(
    "/bookings/{id}/feedback",
    response_model=FeedbackResponse,
    tags=["Feedback & AI"],
    summary="Submit post-trip feedback for AI analysis"
)
def submit_feedback(
    request: SubmitFeedbackRequest,
    id: str = Path(..., description="Booking ID")
):
    return process_feedback(booking_id=id, raw_feedback_text=request.feedback_text)

@app.get("/feedback/urgent", tags=["Feedback & AI"], summary="List feedback categorized by priority levels")
def list_urgent_feedback(
    level: Optional[str] = Query(None, description="Filter by priority level: CRITICAL, HIGH, MEDIUM, LOW")
):
    return get_urgent_feedbacks(min_priority_level=level)

@app.get("/users/{user_id}", tags=["Users"])
def get_user_profile(user_id: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")
        return dict(user)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
