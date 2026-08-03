import json
import re
import uuid
import os
import httpx
from datetime import datetime, timezone, timedelta
from database import get_db, utc_now_iso
from fastapi import HTTPException

HOLD_GRACE_PERIOD_SECONDS = int(os.getenv("HOLD_GRACE_PERIOD_SECONDS", 300))

def expire_outdated_holds(conn):
    """
    Scans for holds whose expires_at < current_time and status is 'ACTIVE'.
    Marks them as EXPIRED and releases the corresponding seats back to 'available'.
    """
    now = utc_now_iso()
    cursor = conn.cursor()
    
    # Find expired active holds
    cursor.execute("""
        SELECT id, trip_id, seats_json FROM holds 
        WHERE status = 'ACTIVE' AND expires_at < ?
    """, (now,))
    expired_holds = cursor.fetchall()
    
    for hold in expired_holds:
        hold_id = hold["id"]
        trip_id = hold["trip_id"]
        seats = json.loads(hold["seats_json"])
        
        # Update hold status to EXPIRED
        cursor.execute("UPDATE holds SET status = 'EXPIRED' WHERE id = ?", (hold_id,))
        
        # Release seats to 'available' if they are currently marked 'held'
        for seat_num in seats:
            cursor.execute("""
                UPDATE seats SET status = 'available', updated_at = ? 
                WHERE trip_id = ? AND seat_number = ? AND status = 'held'
            """, (now, trip_id, seat_num))

def get_trip_seat_map(trip_id: str):
    """
    Returns the current seat map for a trip, auto-expiring old holds first.
    """
    with get_db() as conn:
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM trips WHERE id = ?", (trip_id,))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=44, detail=f"Trip '{trip_id}' not found.")
            
        cursor.execute("SELECT seat_number, status FROM seats WHERE trip_id = ? ORDER BY seat_number", (trip_id,))
        rows = cursor.fetchall()
        
        seats = [{"seat_number": row["seat_number"], "status": row["status"]} for row in rows]
        
        available_count = sum(1 for s in seats if s["status"] == "available")
        held_count = sum(1 for s in seats if s["status"] == "held")
        booked_count = sum(1 for s in seats if s["status"] == "booked")
        
        return {
            "trip_id": trip_id,
            "total_seats": len(seats),
            "available_count": available_count,
            "held_count": held_count,
            "booked_count": booked_count,
            "seats": seats
        }

def create_seat_hold(trip_id: str, seat_numbers: list[str], user_id: str):
    """
    Places a temporary hold on specified seats for a user using atomic transactions.
    """
    if not seat_numbers:
        raise HTTPException(status_code=400, detail="At least one seat must be selected for hold.")
        
    with get_db() as conn:
        # Atomic lock via IMMEDIATE transaction
        conn.execute("BEGIN IMMEDIATE;")
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        # Verify user exists & restriction status
        cursor.execute("SELECT is_restricted, late_cancellation_count FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            # Auto-create user for demo if not present
            cursor.execute(
                "INSERT INTO users (id, name, email, phone, late_cancellation_count, is_restricted) VALUES (?, ?, ?, ?, 0, 0)",
                (user_id, f"Passenger {user_id}", f"{user_id.lower()}@example.com", "+15550000000")
            )
            is_restricted = 0
        else:
            is_restricted = user["is_restricted"]
            
        if is_restricted:
            raise HTTPException(
                status_code=403, 
                detail="PASSENGER_RESTRICTED: User has exceeded late cancellation limits and is temporarily restricted from placing new holds/bookings."
            )
            
        # Verify trip exists
        cursor.execute("SELECT price_per_seat FROM trips WHERE id = ?", (trip_id,))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=404, detail=f"Trip '{trip_id}' not found.")
            
        price_per_seat = trip["price_per_seat"]
        
        # Check seat availability
        placeholders = ",".join("?" for _ in seat_numbers)
        cursor.execute(
            f"SELECT seat_number, status FROM seats WHERE trip_id = ? AND seat_number IN ({placeholders})",
            [trip_id] + list(seat_numbers)
        )
        found_seats = cursor.fetchall()
        found_dict = {s["seat_number"]: s["status"] for s in found_seats}
        
        unavailable = []
        for seat in seat_numbers:
            status = found_dict.get(seat)
            if status is None:
                raise HTTPException(status_code=404, detail=f"Seat '{seat}' does not exist on trip '{trip_id}'.")
            if status != "available":
                unavailable.append(f"{seat} ({status})")
                
        if unavailable:
            raise HTTPException(
                status_code=409, 
                detail=f"Cannot hold seats: The following seat(s) are already held or booked: {', '.join(unavailable)}"
            )
            
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=HOLD_GRACE_PERIOD_SECONDS)
        now_iso = now_dt.isoformat()
        expires_iso = expires_dt.isoformat()
        
        hold_id = f"HOLD-{uuid.uuid4().hex[:8].upper()}"
        total_price = price_per_seat * len(seat_numbers)
        
        # Update seat statuses to 'held'
        for seat in seat_numbers:
            cursor.execute(
                "UPDATE seats SET status = 'held', updated_at = ? WHERE trip_id = ? AND seat_number = ?",
                (now_iso, trip_id, seat)
            )
            
        # Create hold record
        cursor.execute("""
            INSERT INTO holds (id, trip_id, user_id, seats_json, status, total_price, expires_at, created_at)
            VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
        """, (hold_id, trip_id, user_id, json.dumps(seat_numbers), total_price, expires_iso, now_iso))
        
        return {
            "hold_id": hold_id,
            "trip_id": trip_id,
            "user_id": user_id,
            "seats": seat_numbers,
            "status": "ACTIVE",
            "total_price": total_price,
            "expires_at": expires_iso
        }

def confirm_booking(hold_id: str, user_id: str, payment_reference: str):
    """
    Converts an active hold into a confirmed booking upon payment.
    Idempotent: Re-submitting for an already confirmed hold returns the existing booking.
    """
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        # Check if booking already exists for this hold (Idempotency)
        cursor.execute("SELECT * FROM bookings WHERE hold_id = ?", (hold_id,))
        existing_booking = cursor.fetchone()
        if existing_booking:
            return {
                "booking_id": existing_booking["id"],
                "hold_id": existing_booking["hold_id"],
                "trip_id": existing_booking["trip_id"],
                "user_id": existing_booking["user_id"],
                "seats": json.loads(existing_booking["seats_json"]),
                "total_amount": existing_booking["total_amount"],
                "status": existing_booking["status"],
                "payment_reference": existing_booking["payment_ref"],
                "created_at": existing_booking["created_at"]
            }
            
        # Fetch hold record
        cursor.execute("SELECT * FROM holds WHERE id = ?", (hold_id,))
        hold = cursor.fetchone()
        if not hold:
            raise HTTPException(status_code=404, detail=f"Hold '{hold_id}' not found.")
            
        if hold["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Hold belongs to a different passenger.")
            
        if hold["status"] == "EXPIRED":
            raise HTTPException(status_code=400, detail="Hold has expired. Please place a new hold on available seats.")
            
        if hold["status"] == "CONSUMED":
            raise HTTPException(status_code=400, detail="Hold has already been consumed.")
            
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        expires_dt = datetime.fromisoformat(hold["expires_at"])
        
        if now_dt > expires_dt:
            # Mark hold expired
            cursor.execute("UPDATE holds SET status = 'EXPIRED' WHERE id = ?", (hold_id,))
            seats = json.loads(hold["seats_json"])
            for s in seats:
                cursor.execute("UPDATE seats SET status = 'available', updated_at = ? WHERE trip_id = ? AND seat_number = ? AND status = 'held'", (now_iso, hold["trip_id"], s))
            raise HTTPException(status_code=400, detail="Hold expired before payment completion.")
            
        # Confirm booking
        booking_id = f"BOOK-{uuid.uuid4().hex[:8].upper()}"
        seats = json.loads(hold["seats_json"])
        
        # Mark seats as 'booked'
        for s in seats:
            cursor.execute("UPDATE seats SET status = 'booked', updated_at = ? WHERE trip_id = ? AND seat_number = ?", (now_iso, hold["trip_id"], s))
            
        # Update hold status to CONSUMED
        cursor.execute("UPDATE holds SET status = 'CONSUMED' WHERE id = ?", (hold_id,))
        
        # Create booking record
        cursor.execute("""
            INSERT INTO bookings (id, hold_id, trip_id, user_id, seats_json, total_amount, payment_ref, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?)
        """, (booking_id, hold_id, hold["trip_id"], user_id, hold["seats_json"], hold["total_price"], payment_reference, now_iso))
        
        return {
            "booking_id": booking_id,
            "hold_id": hold_id,
            "trip_id": hold["trip_id"],
            "user_id": user_id,
            "seats": seats,
            "total_amount": hold["total_price"],
            "status": "CONFIRMED",
            "payment_reference": payment_reference,
            "created_at": now_iso
        }

def cancel_booking(booking_id: str, reason: str = None):
    """
    Cancels a booking, calculates refund based on time remaining to departure,
    and enforces idempotency (multiple cancellation requests return the exact same refund).
    Also updates user's late cancellation count if cancelled inside zero-refund window (<6h).
    """
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        booking = cursor.fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found.")
            
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        
        # Idempotency check: If already cancelled, return existing cancellation details
        if booking["status"] == "CANCELLED":
            cursor.execute("SELECT departure_time FROM trips WHERE id = ?", (booking["trip_id"],))
            trip = cursor.fetchone()
            dep_dt = datetime.fromisoformat(trip["departure_time"])
            hours_diff = max(0.0, (dep_dt - datetime.fromisoformat(booking["cancelled_at"])).total_seconds() / 3600.0)
            
            cursor.execute("SELECT is_restricted FROM users WHERE id = ?", (booking["user_id"],))
            usr = cursor.fetchone()
            is_restricted = bool(usr["is_restricted"]) if usr else False
            
            pct = 100 if hours_diff > 24 else (50 if hours_diff >= 6 else 0)
            
            return {
                "booking_id": booking_id,
                "status": "CANCELLED",
                "refund_amount": booking["refund_amount"],
                "refund_percentage": pct,
                "time_to_departure_hours": round(hours_diff, 2),
                "message": f"Booking was previously cancelled on {booking['cancelled_at']}. Refund of ₹{booking['refund_amount']} processed.",
                "user_restricted": is_restricted
            }
            
        # Get trip departure time
        cursor.execute("SELECT departure_time FROM trips WHERE id = ?", (booking["trip_id"],))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=500, detail="Associated trip record missing.")
            
        dep_dt = datetime.fromisoformat(trip["departure_time"])
        time_diff = dep_dt - now_dt
        hours_to_departure = time_diff.total_seconds() / 3600.0
        
        # Refund Policy Rules:
        # > 24 hours: 100% refund
        # 6h - 24h: 50% refund
        # < 6h: 0% refund
        if hours_to_departure > 24.0:
            refund_pct = 100
        elif hours_to_departure >= 6.0:
            refund_pct = 50
        else:
            refund_pct = 0
            
        refund_amount = round((booking["total_amount"] * refund_pct) / 100.0, 2)
        
        # Update booking record
        cursor.execute("""
            UPDATE bookings 
            SET status = 'CANCELLED', refund_amount = ?, cancelled_at = ?
            WHERE id = ?
        """, (refund_amount, now_iso, booking_id))
        
        # Free seats back to 'available'
        seats = json.loads(booking["seats_json"])
        for s in seats:
            cursor.execute("""
                UPDATE seats SET status = 'available', updated_at = ? 
                WHERE trip_id = ? AND seat_number = ?
            """, (now_iso, booking["trip_id"], s))
            
        # If cancelled in zero-refund window (< 6 hours), increment late cancellation count
        is_restricted = False
        user_id = booking["user_id"]
        if refund_pct == 0:
            cursor.execute("""
                UPDATE users 
                SET late_cancellation_count = late_cancellation_count + 1 
                WHERE id = ?
            """, (user_id,))
            
            # Check if count reached 3+ threshold
            cursor.execute("SELECT late_cancellation_count FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            if u_row and u_row["late_cancellation_count"] >= 3:
                cursor.execute("UPDATE users SET is_restricted = 1 WHERE id = ?", (user_id,))
                is_restricted = True
                
        message_str = f"Booking cancelled successfully. {refund_pct}% refund (₹{refund_amount}) applied based on cancellation window ({round(hours_to_departure, 1)} hours to departure)."
        if is_restricted:
            message_str += " NOTICE: User has been flagged as RESTRICTED due to 3 or more late cancellations (<6h)."
            
        return {
            "booking_id": booking_id,
            "status": "CANCELLED",
            "refund_amount": refund_amount,
            "refund_percentage": refund_pct,
            "time_to_departure_hours": round(hours_to_departure, 2),
            "message": message_str,
            "user_restricted": is_restricted
        }

def sanitize_pii(text: str) -> str:
    """
    Strips Personally Identifiable Information (PII) before sending text to the AI model.
    Redacts Emails, Phone numbers, and Credit Card / Payment info.
    """
    # Scrub Emails
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[EMAIL REDACTED]', text)
    # Scrub Phone numbers
    text = re.sub(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[PHONE REDACTED]', text)
    # Scrub Credit Card numbers (13-19 digits)
    text = re.sub(r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[CARD REDACTED]', text)
    text = re.sub(r'\b\d{12,19}\b', '[CARD REDACTED]', text)
    return text

def analyze_feedback_llm(sanitized_text: str) -> dict:
    """
    Sends sanitized feedback to LLM (Gemini, Groq, or OpenRouter) for sentiment & urgency analysis.
    Includes a fallback classifier if API key is not present in environment.
    """
    gemini_key = os.getenv("GEMINI_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    
    prompt = f"""
You are a customer feedback analyzer for a bus booking platform (RedBus).
Analyze the following passenger post-trip feedback text and output a JSON object with EXACTLY these keys:
- "sentiment": string (MUST be one of: "POSITIVE", "NEUTRAL", "NEGATIVE", "STRONGLY_NEGATIVE")
- "summary": string (1 concise sentence summarizing the feedback)
- "category_tags": list of strings (e.g. ["AC", "DRIVER_BEHAVIOR", "PUNCTUALITY", "CLEANLINESS", "SAFETY", "SEATING", "GENERAL"])
- "urgent_followup": boolean (true if strongly negative, angry, safety hazard, driver reckless, or severe complaint; false otherwise)

Passenger Feedback:
"{sanitized_text}"

Return ONLY raw JSON, with no markdown formatting or backticks.
"""

    # 1. Try Gemini API if key exists
    if gemini_key and gemini_key != "your_gemini_api_key_here":
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}]
            }
            res = httpx.post(url, json=payload, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                raw_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                raw_out = re.sub(r"^```json\s*", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()
                return json.loads(raw_out)
        except Exception as e:
            print(f"[Gemini API Call Exception]: {e}")
            
    # 2. Try Groq API if key exists
    if groq_key and groq_key != "your_groq_api_key_here":
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            res = httpx.post(url, json=payload, headers=headers, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                raw_out = data["choices"][0]["message"]["content"].strip()
                raw_out = re.sub(r"^```json\s*", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()
                return json.loads(raw_out)
        except Exception as e:
            print(f"[Groq API Call Exception]: {e}")

    # 3. Try OpenRouter API if key exists
    if openrouter_key and openrouter_key != "your_openrouter_api_key_here":
        try:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"}
            payload = {
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            res = httpx.post(url, json=payload, headers=headers, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                raw_out = data["choices"][0]["message"]["content"].strip()
                raw_out = re.sub(r"^```json\s*", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()
                return json.loads(raw_out)
        except Exception as e:
            print(f"[OpenRouter API Call Exception]: {e}")

    # 4. Fallback Rule-Based Engine (Guarantees zero breaking errors when running without API key)
    lower = sanitized_text.lower()
    strongly_neg_keywords = ["angry", "horrible", "terrible", "worst", "unsafe", "accident", "reckless", "rude", "dangerous", "breakdown", "scam"]
    neg_keywords = ["late", "delayed", "dirty", "uncomfortable", "ac not working", "cold", "hot", "smelly", "bad"]
    pos_keywords = ["great", "good", "excellent", "comfortable", "clean", "on time", "awesome", "loved", "nice"]
    
    category_tags = []
    if "ac" in lower or "air conditioning" in lower or "cold" in lower or "hot" in lower:
        category_tags.append("AC")
    if "driver" in lower or "driving" in lower or "speed" in lower:
        category_tags.append("DRIVER_BEHAVIOR")
    if "late" in lower or "delay" in lower or "time" in lower:
        category_tags.append("PUNCTUALITY")
    if "clean" in lower or "dirty" in lower or "smell" in lower:
        category_tags.append("CLEANLINESS")
    if not category_tags:
        category_tags.append("GENERAL")

    if any(k in lower for k in strongly_neg_keywords):
        return {
            "sentiment": "STRONGLY_NEGATIVE",
            "summary": "Passenger submitted strongly negative feedback regarding trip experience.",
            "category_tags": category_tags,
            "urgent_followup": True
        }
    elif any(k in lower for k in neg_keywords):
        return {
            "sentiment": "NEGATIVE",
            "summary": "Passenger reported unsatisfactory service quality.",
            "category_tags": category_tags,
            "urgent_followup": False
        }
    elif any(k in lower for k in pos_keywords):
        return {
            "sentiment": "POSITIVE",
            "summary": "Passenger reported a pleasant and satisfactory journey.",
            "category_tags": category_tags,
            "urgent_followup": False
        }
    else:
        return {
            "sentiment": "NEUTRAL",
            "summary": "Passenger provided general post-trip commentary.",
            "category_tags": category_tags,
            "urgent_followup": False
        }

def process_feedback(booking_id: str, raw_feedback_text: str):
    """
    Submits post-trip feedback, redacts PII locally, performs LLM analysis,
    and stores the record in database.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, status FROM bookings WHERE id = ?", (booking_id,))
        booking = cursor.fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found.")
            
        # PII Scrubbing
        sanitized_text = sanitize_pii(raw_feedback_text)
        
        # LLM Analysis
        analysis = analyze_feedback_llm(sanitized_text)
        
        fb_id = f"FB-{uuid.uuid4().hex[:8].upper()}"
        now_iso = utc_now_iso()
        
        urgent_int = 1 if analysis.get("urgent_followup") else 0
        
        cursor.execute("""
            INSERT INTO feedback (id, booking_id, raw_text, sanitized_text, sentiment, summary, category_tags_json, urgent_followup, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fb_id,
            booking_id,
            raw_feedback_text,
            sanitized_text,
            analysis.get("sentiment", "NEUTRAL"),
            analysis.get("summary", "Post-trip feedback"),
            json.dumps(analysis.get("category_tags", ["GENERAL"])),
            urgent_int,
            now_iso
        ))
        
        return {
            "feedback_id": fb_id,
            "booking_id": booking_id,
            "sanitized_text": sanitized_text,
            "sentiment": analysis.get("sentiment", "NEUTRAL"),
            "summary": analysis.get("summary", "Post-trip feedback"),
            "category_tags": analysis.get("category_tags", ["GENERAL"]),
            "urgent_followup": bool(analysis.get("urgent_followup")),
            "created_at": now_iso
        }
