import os
from fastapi import FastAPI, HTTPException, Path
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
    process_feedback
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB and seed data on startup
    seed_database()
    yield

app = FastAPI(
    title="RedBus Seat Booking & Cancellation Engine",
    description="High-reliability engine for bus seat inventory, concurrent holds, time-window refund math, and AI-driven passenger feedback analysis.",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/", tags=["Health"])
def root():
    return {
        "status": "online",
        "service": "Bus Seat Booking & Cancellation Engine",
        "documentation": "/docs",
        "endpoints": [
            "GET /trips",
            "GET /trips/{id}/seats",
            "POST /seats/hold",
            "POST /bookings",
            "POST /bookings/{id}/cancel",
            "POST /bookings/{id}/feedback"
        ]
    }

@app.get("/trips", tags=["Trips"])
def list_trips():
    """List all available trips in the system."""
    with get_db() as conn:
        cursor = conn.cursor()
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
    """
    Returns the real-time seat map for a trip.
    Auto-expires any seat holds past their grace period before returning status.
    """
    return get_trip_seat_map(id)

# API 2: POST /seats/hold
@app.post(
    "/seats/hold",
    response_model=HoldSeatsResponse,
    tags=["Seats"],
    summary="Place a temporary hold on seats"
)
def place_hold(request: HoldSeatsRequest):
    """
    Places a temporary hold (grace period: 5 minutes) on selected seats.
    Uses atomic transactions to prevent concurrent double-holding of the same seat.
    Blocks passengers flagged with high cancellation risk (3+ late cancellations).
    """
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
    summary="Confirm booking from an active hold"
)
def create_booking(request: ConfirmBookingRequest):
    """
    Confirms a booking from a valid, active hold upon payment.
    Converts 'held' seats into 'booked' seats.
    Re-submitting with the same hold_id is idempotent and returns the existing booking.
    """
    return confirm_booking(
        hold_id=request.hold_id,
        user_id=request.user_id,
        payment_reference=request.payment_reference
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
    """
    Cancels a booking, releases seats back to 'available', and calculates refund:
    - > 24 hours before departure: 100% refund
    - 6h - 24h before departure: 50% refund
    - < 6 hours before departure: 0% refund
    
    Idempotent: Multiple cancellation calls on the same booking return the exact same refund.
    Cancelling < 6 hours before departure increments late cancellation counter; 3+ late cancellations restrict user.
    """
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
    """
    Submits post-trip passenger feedback.
    Strips PII (email, phone, credit card) before sending text to LLM.
    LLM analyzes sentiment, generates summary, extracts category tags,
    and sets 'urgent_followup' flag if feedback requires immediate customer service action.
    """
    return process_feedback(booking_id=id, raw_feedback_text=request.feedback_text)

@app.get("/users/{user_id}", tags=["Users"])
def get_user_profile(user_id: str):
    """View passenger profile, late cancellation count, and restriction status."""
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
