from pydantic import BaseModel, Field
from typing import List, Optional

class SeatStatus(BaseModel):
    seat_number: str
    status: str  # "available", "held", "booked"

class TripSeatMapResponse(BaseModel):
    trip_id: str
    total_seats: int
    available_count: int
    held_count: int
    booked_count: int
    seats: List[SeatStatus]

class HoldSeatsRequest(BaseModel):
    trip_id: str
    seat_numbers: List[str]
    user_id: str

class HoldSeatsResponse(BaseModel):
    hold_id: str
    trip_id: str
    user_id: str
    seats: List[str]
    status: str
    total_price: float
    expires_at: str

class ConfirmBookingRequest(BaseModel):
    hold_id: str
    user_id: str
    payment_reference: str

class ConfirmBookingResponse(BaseModel):
    booking_id: str
    hold_id: str
    trip_id: str
    user_id: str
    seats: List[str]
    total_amount: float
    status: str
    payment_reference: str
    created_at: str

class CancelBookingRequest(BaseModel):
    reason: Optional[str] = None

class CancelBookingResponse(BaseModel):
    booking_id: str
    status: str
    refund_amount: float
    refund_percentage: int
    time_to_departure_hours: float
    message: str
    user_restricted: bool

class SubmitFeedbackRequest(BaseModel):
    feedback_text: str = Field(..., min_length=3, description="Passenger feedback after the trip")

class FeedbackResponse(BaseModel):
    feedback_id: str
    booking_id: str
    sanitized_text: str
    sentiment: str  # "POSITIVE", "NEUTRAL", "NEGATIVE", "STRONGLY_NEGATIVE"
    summary: str
    category_tags: List[str]
    urgent_followup: bool
    created_at: str
