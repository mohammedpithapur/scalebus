from datetime import datetime, timezone, timedelta
from database import init_db, get_db

def seed_database(reset_for_tests: bool = False):
    """
    Initializes database tables and populates sample trips & users.
    Only wipes data if explicitly requested (e.g. during test runs).
    Preserves all existing bookings, holds, and feedback across server reloads!
    """
    init_db()
    
    now_dt = datetime.now(timezone.utc)
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Only clean transactional tables if running automated pytest suite
        if reset_for_tests:
            cursor.execute("DELETE FROM feedback;")
            cursor.execute("DELETE FROM bookings;")
            cursor.execute("DELETE FROM holds;")
        
        # Check if trips already exist; if so, keep existing data intact
        cursor.execute("SELECT COUNT(*) as count FROM trips")
        trip_count = cursor.fetchone()["count"]
        
        if trip_count == 0 or reset_for_tests:
            trips_data = [
                {
                    "id": "TRIP-101",
                    "bus_number": "KA-01-F-1234",
                    "source": "Bangalore",
                    "destination": "Goa",
                    "departure_time": (now_dt + timedelta(hours=30)).isoformat(),  # >24h (100% refund)
                    "price_per_seat": 1200.0
                },
                {
                    "id": "TRIP-102",
                    "bus_number": "MH-12-Q-5678",
                    "source": "Mumbai",
                    "destination": "Pune",
                    "departure_time": (now_dt + timedelta(hours=12)).isoformat(),  # 6-24h (50% refund)
                    "price_per_seat": 500.0
                },
                {
                    "id": "TRIP-103",
                    "bus_number": "DL-01-A-9999",
                    "source": "Delhi",
                    "destination": "Jaipur",
                    "departure_time": (now_dt + timedelta(hours=2)).isoformat(),   # <6h (0% refund)
                    "price_per_seat": 800.0
                },
                {
                    "id": "TRIP-104",
                    "bus_number": "TS-09-UB-4321",
                    "source": "Hyderabad",
                    "destination": "Chennai",
                    "departure_time": (now_dt + timedelta(hours=48)).isoformat(),  # >24h
                    "price_per_seat": 1500.0
                }
            ]
            
            seat_labels = [f"{row}{num}" for row in ["A", "B", "C", "D"] for num in range(1, 6)]  # 20 seats
            
            for t in trips_data:
                cursor.execute("""
                    INSERT OR REPLACE INTO trips (id, bus_number, source, destination, departure_time, price_per_seat)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (t["id"], t["bus_number"], t["source"], t["destination"], t["departure_time"], t["price_per_seat"]))
                
                for seat_num in seat_labels:
                    cursor.execute("""
                        INSERT OR REPLACE INTO seats (trip_id, seat_number, status, updated_at)
                        VALUES (?, ?, 'available', ?)
                    """, (t["id"], seat_num, now_dt.isoformat()))
                    
            users = [
                ("USER-101", "John Doe", "john@example.com", "+15551112222", 0, 0),
                ("USER-102", "Jane Smith", "jane@example.com", "+15553334444", 0, 0),
                ("USER-999", "Risk User", "risk@example.com", "+15559999999", 2, 0)
            ]
            
            for u in users:
                cursor.execute("""
                    INSERT OR REPLACE INTO users (id, name, email, phone, late_cancellation_count, is_restricted)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, u)
                
            print("Database initialized and seeded with trips & users.")
        else:
            print("Existing SQLite data preserved across server reload.")

if __name__ == "__main__":
    seed_database(reset_for_tests=True)
