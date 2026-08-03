import sqlite3
import os
import contextlib
from datetime import datetime, timezone

DB_NAME = os.getenv("DATABASE_URL", "bus_booking.db")

def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

@contextlib.contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                bus_number TEXT NOT NULL,
                source TEXT NOT NULL,
                destination TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                price_per_seat REAL NOT NULL
            );
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id TEXT NOT NULL,
                seat_number TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('available', 'held', 'booked')),
                updated_at TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id),
                UNIQUE(trip_id, seat_number)
            );
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS holds (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                seats_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'EXPIRED', 'CONSUMED')),
                total_price REAL NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                hold_id TEXT,
                trip_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                seats_json TEXT NOT NULL,
                total_amount REAL NOT NULL,
                payment_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('CONFIRMED', 'CANCELLED')),
                refund_amount REAL DEFAULT 0.0,
                cancelled_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                late_cancellation_count INTEGER DEFAULT 0,
                is_restricted INTEGER DEFAULT 0
            );
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                booking_id TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                sanitized_text TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                summary TEXT NOT NULL,
                category_tags_json TEXT NOT NULL,
                priority_level TEXT DEFAULT 'LOW',
                priority_score INTEGER DEFAULT 1,
                urgent_followup INTEGER NOT NULL CHECK(urgent_followup IN (0, 1)),
                created_at TEXT NOT NULL,
                FOREIGN KEY (booking_id) REFERENCES bookings(id)
            );
        """)
        
        # Add columns if upgrading existing table
        try:
            cursor.execute("ALTER TABLE feedback ADD COLUMN priority_level TEXT DEFAULT 'LOW';")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE feedback ADD COLUMN priority_score INTEGER DEFAULT 1;")
        except Exception:
            pass
            
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_seats_trip ON seats(trip_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_holds_status_exp ON holds(status, expires_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id);")

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
