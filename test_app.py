import pytest
from fastapi.testclient import TestClient
from main import app
from seed import seed_database
from database import get_db, utc_now_iso
import json
import concurrent.futures

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_db():
    """Reset database state before each test run."""
    seed_database(reset_for_tests=True)

def test_1_get_seat_map():
    """Verify seat map retrieval and initial counts."""
    response = client.get("/trips/TRIP-101/seats")
    assert response.status_code == 200
    data = response.json()
    assert data["trip_id"] == "TRIP-101"
    assert data["total_seats"] == 20
    assert data["available_count"] == 20
    assert data["held_count"] == 0
    assert data["booked_count"] == 0
    assert len(data["seats"]) == 20

def test_2_hold_seats_success_and_conflict():
    """Verify successful seat hold and conflict when another user attempts to hold same seat."""
    payload = {
        "trip_id": "TRIP-101",
        "seat_numbers": ["A1", "A2"],
        "user_id": "USER-101"
    }
    res = client.post("/seats/hold", json=payload)
    assert res.status_code == 200
    hold_data = res.json()
    assert hold_data["status"] == "ACTIVE"
    assert hold_data["seats"] == ["A1", "A2"]
    assert hold_data["total_price"] == 2400.0

    payload_conflict = {
        "trip_id": "TRIP-101",
        "seat_numbers": ["A2", "A3"],
        "user_id": "USER-102"
    }
    res_conf = client.post("/seats/hold", json=payload_conflict)
    assert res_conf.status_code == 409
    assert "already held or booked" in res_conf.json()["detail"]

def test_3_confirm_booking_from_hold_and_idempotency():
    """Verify hold conversion to confirmed booking and idempotent re-submitting."""
    hold_res = client.post("/seats/hold", json={
        "trip_id": "TRIP-101",
        "seat_numbers": ["B1"],
        "user_id": "USER-101"
    })
    hold_id = hold_res.json()["hold_id"]

    book_payload = {
        "hold_id": hold_id,
        "user_id": "USER-101",
        "payment_reference": "PAY-TXN-998877"
    }
    book_res = client.post("/bookings", json=book_payload)
    assert book_res.status_code == 200
    b_data = book_res.json()
    assert b_data["status"] == "CONFIRMED"
    assert b_data["seats"] == ["B1"]

    re_book_res = client.post("/bookings", json=book_payload)
    assert re_book_res.status_code == 200
    assert re_book_res.json()["booking_id"] == b_data["booking_id"]

def test_4_direct_booking_without_prior_hold():
    """Verify direct booking in one step without a prior explicit hold_id."""
    direct_payload = {
        "trip_id": "TRIP-101",
        "seat_numbers": ["B2", "B3"],
        "user_id": "USER-101",
        "payment_reference": "PAY-DIRECT-001"
    }
    res = client.post("/bookings", json=direct_payload)
    assert res.status_code == 200
    b_data = res.json()
    assert b_data["status"] == "CONFIRMED"
    assert b_data["seats"] == ["B2", "B3"]
    assert b_data["total_amount"] == 2400.0

def test_5_cancellation_refund_windows_and_idempotency():
    """Verify 100%, 50%, and 0% refund windows and idempotency on double cancellation."""
    h1 = client.post("/seats/hold", json={"trip_id": "TRIP-101", "seat_numbers": ["C1"], "user_id": "USER-101"}).json()
    b1 = client.post("/bookings", json={"hold_id": h1["hold_id"], "user_id": "USER-101", "payment_reference": "PAY-1"}).json()
    
    c1_res = client.post(f"/bookings/{b1['booking_id']}/cancel")
    assert c1_res.status_code == 200
    c1_data = c1_res.json()
    assert c1_data["refund_percentage"] == 100
    assert c1_data["refund_amount"] == 1200.0

    c1_re = client.post(f"/bookings/{b1['booking_id']}/cancel")
    assert c1_re.status_code == 200
    assert c1_re.json()["refund_amount"] == 1200.0

    h2 = client.post("/seats/hold", json={"trip_id": "TRIP-102", "seat_numbers": ["C2"], "user_id": "USER-101"}).json()
    b2 = client.post("/bookings", json={"hold_id": h2["hold_id"], "user_id": "USER-101", "payment_reference": "PAY-2"}).json()
    
    c2_res = client.post(f"/bookings/{b2['booking_id']}/cancel")
    assert c2_res.status_code == 200
    assert c2_res.json()["refund_percentage"] == 50
    assert c2_res.json()["refund_amount"] == 250.0

    h3 = client.post("/seats/hold", json={"trip_id": "TRIP-103", "seat_numbers": ["C3"], "user_id": "USER-101"}).json()
    b3 = client.post("/bookings", json={"hold_id": h3["hold_id"], "user_id": "USER-101", "payment_reference": "PAY-3"}).json()
    
    c3_res = client.post(f"/bookings/{b3['booking_id']}/cancel")
    assert c3_res.status_code == 200
    assert c3_res.json()["refund_percentage"] == 0
    assert c3_res.json()["refund_amount"] == 0.0

def test_6_user_late_cancellation_restriction_policy():
    """Verify that 3 late cancellations (<6h) triggers restriction on the user."""
    h = client.post("/seats/hold", json={"trip_id": "TRIP-103", "seat_numbers": ["D1"], "user_id": "USER-999"}).json()
    b = client.post("/bookings", json={"hold_id": h["hold_id"], "user_id": "USER-999", "payment_reference": "PAY-99"}).json()
    
    c_res = client.post(f"/bookings/{b['booking_id']}/cancel")
    assert c_res.status_code == 200
    assert c_res.json()["user_restricted"] is True

    hold_rejected = client.post("/seats/hold", json={"trip_id": "TRIP-101", "seat_numbers": ["D2"], "user_id": "USER-999"})
    assert hold_rejected.status_code == 403
    assert "PASSENGER_RESTRICTED" in hold_rejected.json()["detail"]

def test_7_feedback_pii_sanitization_and_urgent_flag():
    """Verify PII redaction and 4 priority levels classification for feedback."""
    h = client.post("/seats/hold", json={"trip_id": "TRIP-101", "seat_numbers": ["A5"], "user_id": "USER-101"}).json()
    b = client.post("/bookings", json={"hold_id": h["hold_id"], "user_id": "USER-101", "payment_reference": "PAY-A5"}).json()

    raw_text = "The driver was driving reckless and angry! Contact me at john@example.com or +15551112222, card 4111-2222-3333-4444."
    fb_res = client.post(f"/bookings/{b['booking_id']}/feedback", json={"feedback_text": raw_text})
    assert fb_res.status_code == 200
    fb_data = fb_res.json()

    sanitized = fb_data["sanitized_text"]
    assert "john@example.com" not in sanitized
    assert "+15551112222" not in sanitized
    assert "4111-2222-3333-4444" not in sanitized
    assert "[EMAIL REDACTED]" in sanitized
    assert "[PHONE REDACTED]" in sanitized
    assert "[CARD REDACTED]" in sanitized

    assert fb_data["urgent_followup"] is True
    assert fb_data["priority_level"] in ["CRITICAL", "HIGH"]
    assert fb_data["priority_score"] in [3, 4]

def test_8_concurrent_race_condition_protection():
    """
    Simulates 10 concurrent threads attempting to hold/book the exact same seat ('A1') simultaneously.
    Verifies that EXACTLY 1 thread succeeds (200 OK) and 9 threads fail cleanly (409 Conflict).
    """
    def try_hold_seat(user_index):
        return client.post("/seats/hold", json={
            "trip_id": "TRIP-104",
            "seat_numbers": ["A1"],
            "user_id": f"USER-CONCURRENT-{user_index}"
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(try_hold_seat, i) for i in range(10)]
        results = [f.result() for f in futures]

    successes = [r for r in results if r.status_code == 200]
    conflicts = [r for r in results if r.status_code == 409]

    # Exactly ONE passenger gets the hold
    assert len(successes) == 1
    # Exactly NINE passengers get conflict error
    assert len(conflicts) == 9
