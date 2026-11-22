"""SQLite database layer for AutoCare."""
import os
import sqlite3
from pathlib import Path

from flask import g

# New file name on purpose: the earlier prototype used service.db with a different schema.
DB_PATH = Path(os.environ.get("AUTOCARE_DB") or Path(__file__).with_name("autocare.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    email TEXT,
    address TEXT,
    phone_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','advisor','technician')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS otps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    reg_no TEXT NOT NULL UNIQUE,
    make TEXT,
    model TEXT NOT NULL,
    year INTEGER,
    fuel TEXT,
    odometer INTEGER NOT NULL DEFAULT 0,
    last_reminded TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    base_price REAL NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 60,
    interval_km INTEGER NOT NULL DEFAULT 0,
    interval_months INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id),
    booking_date TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Booked',
    notes TEXT,
    pickup INTEGER NOT NULL DEFAULT 0,
    odometer_in INTEGER,
    technician_id INTEGER REFERENCES staff(id),
    discount_pct REAL NOT NULL DEFAULT 0,
    subtotal REAL NOT NULL DEFAULT 0,
    discount REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    payment_status TEXT NOT NULL DEFAULT 'Unpaid',
    payment_method TEXT,
    paid_at TEXT,
    rating INTEGER,
    feedback TEXT,
    source TEXT NOT NULL DEFAULT 'online',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    service_id INTEGER REFERENCES services(id),
    name TEXT NOT NULL,
    price REAL NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    note TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    booking_id INTEGER REFERENCES bookings(id) ON DELETE SET NULL,
    service_date TEXT NOT NULL,
    odometer INTEGER NOT NULL,
    services TEXT NOT NULL,
    cost REAL NOT NULL DEFAULT 0,
    next_due_date TEXT,
    next_due_km INTEGER,
    source TEXT NOT NULL DEFAULT 'workshop'
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_bookings_date ON bookings(booking_date);
CREATE INDEX IF NOT EXISTS ix_bookings_customer ON bookings(customer_id);
CREATE INDEX IF NOT EXISTS ix_bookings_status ON bookings(status);
CREATE INDEX IF NOT EXISTS ix_history_booking ON status_history(booking_id);
CREATE INDEX IF NOT EXISTS ix_maint_vehicle ON maintenance(vehicle_id);
CREATE INDEX IF NOT EXISTS ix_notif_customer ON notifications(customer_id, is_read);
CREATE INDEX IF NOT EXISTS ix_otps_phone ON otps(phone);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def commit_or_rollback(response):
    """Commit at the end of a successful request, roll back on errors."""
    conn = g.get("db")
    if conn is not None:
        if response.status_code < 400:
            conn.commit()
        else:
            conn.rollback()
    return response


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        if exc is not None:
            conn.rollback()
        conn.close()
