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
    now = utc_now_iso()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, trip_id, seats_json FROM holds 
        WHERE status = 'ACTIVE' AND expires_at < ?
    """, (now,))
    expired_holds = cursor.fetchall()
    
    for hold in expired_holds:
        hold_id = hold["id"]
        trip_id = hold["trip_id"]
        seats = json.loads(hold["seats_json"])
        
        cursor.execute("UPDATE holds SET status = 'EXPIRED' WHERE id = ?", (hold_id,))
        for seat_num in seats:
            cursor.execute("""
                UPDATE seats SET status = 'available', updated_at = ? 
                WHERE trip_id = ? AND seat_number = ? AND status = 'held'
            """, (now, trip_id, seat_num))

def get_trip_seat_map(trip_id: str):
    with get_db() as conn:
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM trips WHERE id = ?", (trip_id,))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=404, detail=f"Trip '{trip_id}' not found.")
            
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
    if not seat_numbers:
        raise HTTPException(status_code=400, detail="At least one seat must be selected for hold.")
        
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        cursor.execute("SELECT is_restricted, late_cancellation_count FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
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
            
        cursor.execute("SELECT price_per_seat FROM trips WHERE id = ?", (trip_id,))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=404, detail=f"Trip '{trip_id}' not found.")
            
        price_per_seat = trip["price_per_seat"]
        
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
        
        for seat in seat_numbers:
            cursor.execute(
                "UPDATE seats SET status = 'held', updated_at = ? WHERE trip_id = ? AND seat_number = ?",
                (now_iso, trip_id, seat)
            )
            
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

def confirm_booking(
    hold_id: str = None, 
    user_id: str = None, 
    payment_reference: str = None,
    trip_id: str = None,
    seat_numbers: list[str] = None
):
    if not hold_id and (not trip_id or not seat_numbers):
        raise HTTPException(status_code=400, detail="Must provide either 'hold_id' OR ('trip_id' and 'seat_numbers').")
        
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        expire_outdated_holds(conn)
        cursor = conn.cursor()
        
        cursor.execute("SELECT is_restricted FROM users WHERE id = ?", (user_id,))
        usr = cursor.fetchone()
        if not usr:
            cursor.execute(
                "INSERT INTO users (id, name, email, phone, late_cancellation_count, is_restricted) VALUES (?, ?, ?, ?, 0, 0)",
                (user_id, f"Passenger {user_id}", f"{user_id.lower()}@example.com", "+15550000000")
            )
        elif usr["is_restricted"]:
            raise HTTPException(
                status_code=403,
                detail="PASSENGER_RESTRICTED: User has exceeded late cancellation limits and is temporarily restricted."
            )

        if hold_id:
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
                cursor.execute("UPDATE holds SET status = 'EXPIRED' WHERE id = ?", (hold_id,))
                seats = json.loads(hold["seats_json"])
                for s in seats:
                    cursor.execute("UPDATE seats SET status = 'available', updated_at = ? WHERE trip_id = ? AND seat_number = ? AND status = 'held'", (now_iso, hold["trip_id"], s))
                raise HTTPException(status_code=400, detail="Hold expired before payment completion.")
                
            booking_id = f"BOOK-{uuid.uuid4().hex[:8].upper()}"
            seats = json.loads(hold["seats_json"])
            
            for s in seats:
                cursor.execute("UPDATE seats SET status = 'booked', updated_at = ? WHERE trip_id = ? AND seat_number = ?", (now_iso, hold["trip_id"], s))
                
            cursor.execute("UPDATE holds SET status = 'CONSUMED' WHERE id = ?", (hold_id,))
            
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

        else:
            cursor.execute("SELECT price_per_seat FROM trips WHERE id = ?", (trip_id,))
            trip = cursor.fetchone()
            if not trip:
                raise HTTPException(status_code=404, detail=f"Trip '{trip_id}' not found.")
                
            price_per_seat = trip["price_per_seat"]
            
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
                    detail=f"Cannot book seats: The following seat(s) are already held or booked: {', '.join(unavailable)}"
                )
                
            now_dt = datetime.now(timezone.utc)
            now_iso = now_dt.isoformat()
            total_price = price_per_seat * len(seat_numbers)
            booking_id = f"BOOK-{uuid.uuid4().hex[:8].upper()}"
            internal_hold_id = f"HOLD-AUTO-{uuid.uuid4().hex[:6].upper()}"
            
            cursor.execute("""
                INSERT INTO holds (id, trip_id, user_id, seats_json, status, total_price, expires_at, created_at)
                VALUES (?, ?, ?, ?, 'CONSUMED', ?, ?, ?)
            """, (internal_hold_id, trip_id, user_id, json.dumps(seat_numbers), total_price, now_iso, now_iso))
            
            for seat in seat_numbers:
                cursor.execute(
                    "UPDATE seats SET status = 'booked', updated_at = ? WHERE trip_id = ? AND seat_number = ?",
                    (now_iso, trip_id, seat)
                )
                
            cursor.execute("""
                INSERT INTO bookings (id, hold_id, trip_id, user_id, seats_json, total_amount, payment_ref, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?)
            """, (booking_id, internal_hold_id, trip_id, user_id, json.dumps(seat_numbers), total_price, payment_reference, now_iso))
            
            return {
                "booking_id": booking_id,
                "hold_id": internal_hold_id,
                "trip_id": trip_id,
                "user_id": user_id,
                "seats": seat_numbers,
                "total_amount": total_price,
                "status": "CONFIRMED",
                "payment_reference": payment_reference,
                "created_at": now_iso
            }

def cancel_booking(booking_id: str, reason: str = None):
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        booking = cursor.fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found.")
            
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        
        if booking["status"] == "CANCELLED":
            cursor.execute("SELECT departure_time FROM trips WHERE id = ?", (booking["trip_id"],))
            trip = cursor.fetchone()
            dep_dt = datetime.fromisoformat(trip["departure_time"])
            hours_diff = max(0.0, (dep_dt - datetime.fromisoformat(booking["cancelled_at"])).total_seconds() / 3600.0)
            
            cursor.execute("SELECT is_restricted FROM users WHERE id = ?", (booking["user_id"],))
            usr = cursor.fetchone()
            is_restricted = bool(usr["is_restricted"]) if usr else False
            
            pct = 100 if hours_diff > 24 else (50 if hours_diff >= 6 else 0)
            
            if booking["refund_amount"] > 0:
                repeat_msg = f"Notice: You have already cancelled this booking. A refund of ₹{booking['refund_amount']} ({pct}% refund) will be credited to your original payment method within 3 to 5 business days."
            else:
                repeat_msg = "Notice: You have already cancelled this booking. No refund (0% refund) was applicable due to late cancellation (<6 hours to departure)."
                
            return {
                "booking_id": booking_id,
                "status": "CANCELLED",
                "refund_amount": booking["refund_amount"],
                "refund_percentage": pct,
                "time_to_departure_hours": round(hours_diff, 2),
                "message": repeat_msg,
                "user_restricted": is_restricted
            }
            
        cursor.execute("SELECT departure_time FROM trips WHERE id = ?", (booking["trip_id"],))
        trip = cursor.fetchone()
        if not trip:
            raise HTTPException(status_code=500, detail="Associated trip record missing.")
            
        dep_dt = datetime.fromisoformat(trip["departure_time"])
        time_diff = dep_dt - now_dt
        hours_to_departure = time_diff.total_seconds() / 3600.0
        
        if hours_to_departure > 24.0:
            refund_pct = 100
        elif hours_to_departure >= 6.0:
            refund_pct = 50
        else:
            refund_pct = 0
            
        refund_amount = round((booking["total_amount"] * refund_pct) / 100.0, 2)
        
        cursor.execute("""
            UPDATE bookings 
            SET status = 'CANCELLED', refund_amount = ?, cancelled_at = ?
            WHERE id = ?
        """, (refund_amount, now_iso, booking_id))
        
        seats = json.loads(booking["seats_json"])
        for s in seats:
            cursor.execute("""
                UPDATE seats SET status = 'available', updated_at = ? 
                WHERE trip_id = ? AND seat_number = ?
            """, (now_iso, booking["trip_id"], s))
            
        is_restricted = False
        user_id = booking["user_id"]
        if refund_pct == 0:
            cursor.execute("""
                UPDATE users 
                SET late_cancellation_count = late_cancellation_count + 1 
                WHERE id = ?
            """, (user_id,))
            
            cursor.execute("SELECT late_cancellation_count FROM users WHERE id = ?", (user_id,))
            u_row = cursor.fetchone()
            if u_row and u_row["late_cancellation_count"] >= 3:
                cursor.execute("UPDATE users SET is_restricted = 1 WHERE id = ?", (user_id,))
                is_restricted = True
                
        if refund_amount > 0:
            message_str = f"Booking cancelled successfully! {refund_pct}% refund of ₹{refund_amount} will be credited to your original payment method within 3 to 5 business days."
        else:
            message_str = f"Booking cancelled. 0% refund applies as cancellation occurred within 6 hours of departure ({round(hours_to_departure, 1)} hours remaining)."
            
        if is_restricted:
            message_str += " NOTICE: User account restricted due to 3 or more late cancellations (<6h)."
            
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
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[EMAIL REDACTED]', text)
    text = re.sub(r'(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', '[PHONE REDACTED]', text)
    text = re.sub(r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[CARD REDACTED]', text)
    text = re.sub(r'\b\d{12,19}\b', '[CARD REDACTED]', text)
    return text

def analyze_feedback_llm(sanitized_text: str) -> dict:
    gemini_key = os.getenv("GEMINI_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    
    prompt = f"""
You are a customer feedback analyzer for a bus booking platform (RedBus).
Analyze the following passenger post-trip feedback text and output a JSON object with EXACTLY these keys:
- "sentiment": string (MUST be one of: "POSITIVE", "NEUTRAL", "NEGATIVE", "STRONGLY_NEGATIVE")
- "summary": string (1 concise sentence summarizing the feedback)
- "category_tags": list of strings (e.g. ["AC", "DRIVER_BEHAVIOR", "PUNCTUALITY", "CLEANLINESS", "SAFETY", "SEATING", "GENERAL"])
- "priority_level": string (MUST be one of 4 levels: "CRITICAL" for safety hazards/reckless driver/accidents, "HIGH" for AC breakdown/extreme delays >2h/missed bus, "MEDIUM" for cleanliness/minor delays <1h/comfort, "LOW" for general/positive feedback)
- "priority_score": integer (4 for CRITICAL, 3 for HIGH, 2 for MEDIUM, 1 for LOW)
- "urgent_followup": boolean (true if priority_level is "CRITICAL" or "HIGH"; false otherwise)

Passenger Feedback:
"{sanitized_text}"

Return ONLY raw JSON, with no markdown formatting or backticks.
"""

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
            print(f"[Groq API Exception]: {e}")

    if gemini_key and gemini_key != "your_gemini_api_key_here":
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            res = httpx.post(url, json=payload, timeout=10.0)
            if res.status_code == 200:
                data = res.json()
                raw_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                raw_out = re.sub(r"^```json\s*", "", raw_out)
                raw_out = re.sub(r"```$", "", raw_out).strip()
                return json.loads(raw_out)
        except Exception as e:
            print(f"[Gemini API Exception]: {e}")

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
            print(f"[OpenRouter API Exception]: {e}")

    lower = sanitized_text.lower()
    critical_keywords = ["reckless", "unsafe", "accident", "dangerous", "threat", "fight", "drunk"]
    high_keywords = ["angry", "horrible", "terrible", "worst", "breakdown", "scam", "ac not working", "ac broken"]
    medium_keywords = ["late", "delayed", "dirty", "uncomfortable", "smelly", "bad"]
    
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

    if any(k in lower for k in critical_keywords):
        return {
            "sentiment": "STRONGLY_NEGATIVE",
            "summary": "Critical safety complaint submitted by passenger.",
            "category_tags": category_tags,
            "priority_level": "CRITICAL",
            "priority_score": 4,
            "urgent_followup": True
        }
    elif any(k in lower for k in high_keywords):
        return {
            "sentiment": "STRONGLY_NEGATIVE",
            "summary": "High priority service quality failure reported.",
            "category_tags": category_tags,
            "priority_level": "HIGH",
            "priority_score": 3,
            "urgent_followup": True
        }
    elif any(k in lower for k in medium_keywords):
        return {
            "sentiment": "NEGATIVE",
            "summary": "Moderate passenger dissatisfaction reported.",
            "category_tags": category_tags,
            "priority_level": "MEDIUM",
            "priority_score": 2,
            "urgent_followup": False
        }
    else:
        return {
            "sentiment": "POSITIVE",
            "summary": "Passenger provided general commentary or positive feedback.",
            "category_tags": category_tags,
            "priority_level": "LOW",
            "priority_score": 1,
            "urgent_followup": False
        }

def process_feedback(booking_id: str, raw_feedback_text: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, status FROM bookings WHERE id = ?", (booking_id,))
        booking = cursor.fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail=f"Booking '{booking_id}' not found.")
            
        sanitized_text = sanitize_pii(raw_feedback_text)
        analysis = analyze_feedback_llm(sanitized_text)
        
        fb_id = f"FB-{uuid.uuid4().hex[:8].upper()}"
        now_iso = utc_now_iso()
        
        p_level = analysis.get("priority_level", "LOW")
        p_score = analysis.get("priority_score", 1)
        urgent_flag = analysis.get("urgent_followup", p_level in ["CRITICAL", "HIGH"])
        urgent_int = 1 if urgent_flag else 0
        
        cursor.execute("""
            INSERT INTO feedback (id, booking_id, raw_text, sanitized_text, sentiment, summary, category_tags_json, priority_level, priority_score, urgent_followup, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fb_id,
            booking_id,
            raw_feedback_text,
            sanitized_text,
            analysis.get("sentiment", "NEUTRAL"),
            analysis.get("summary", "Post-trip feedback"),
            json.dumps(analysis.get("category_tags", ["GENERAL"])),
            p_level,
            p_score,
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
            "priority_level": p_level,
            "priority_score": p_score,
            "urgent_followup": bool(urgent_flag),
            "created_at": now_iso
        }

def get_urgent_feedbacks(min_priority_level: str = None):
    """Retrieves feedback entries enriched with passenger profile contact details from the database."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        if min_priority_level:
            cursor.execute("""
                SELECT f.*, b.trip_id, b.user_id, u.name as user_name, u.email as user_email, u.phone as user_phone
                FROM feedback f
                JOIN bookings b ON f.booking_id = b.id
                LEFT JOIN users u ON b.user_id = u.id
                WHERE f.priority_level = ?
                ORDER BY f.priority_score DESC, f.created_at DESC
            """, (min_priority_level.upper(),))
        else:
            cursor.execute("""
                SELECT f.*, b.trip_id, b.user_id, u.name as user_name, u.email as user_email, u.phone as user_phone
                FROM feedback f
                JOIN bookings b ON f.booking_id = b.id
                LEFT JOIN users u ON b.user_id = u.id
                WHERE f.urgent_followup = 1 OR f.priority_score >= 3
                ORDER BY f.priority_score DESC, f.created_at DESC
            """)
        rows = cursor.fetchall()
        
        result = []
        for r in rows:
            result.append({
                "feedback_id": r["id"],
                "booking_id": r["booking_id"],
                "trip_id": r["trip_id"],
                "user_id": r["user_id"],
                "passenger_name": r["user_name"] or r["user_id"],
                "passenger_email": r["user_email"] or f"{r['user_id'].lower()}@example.com",
                "passenger_phone": r["user_phone"] or "+15550000000",
                "sanitized_text": r["sanitized_text"],
                "sentiment": r["sentiment"],
                "summary": r["summary"],
                "category_tags": json.loads(r["category_tags_json"]),
                "priority_level": r["priority_level"] or "HIGH",
                "priority_score": r["priority_score"] or 3,
                "urgent_followup": bool(r["urgent_followup"]),
                "created_at": r["created_at"]
            })
        return result
