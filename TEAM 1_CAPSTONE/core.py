"""Business rules, auth helpers and shared utilities for AutoCare."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db

# ---------------------------------------------------------------- configuration
TZ_OFFSET_MIN = int(os.environ.get("AUTOCARE_TZ_MIN", "330"))      # IST by default
SHOW_DEMO_OTP = os.environ.get("AUTOCARE_SHOW_OTP", "1") == "1"      # show OTP on screen (no SMS gateway)
OTP_TTL = 300            # seconds an OTP stays valid
OTP_COOLDOWN = 30        # seconds between OTP requests
OTP_MAX_PER_HOUR = 5
OTP_MAX_TRIES = 5

STAGES = ["Booked", "Vehicle Received", "Inspection", "In Service",
          "Quality Check", "Ready for Delivery", "Delivered"]
ALL_STATUSES = STAGES + ["Cancelled"]
WORKSHOP = STAGES[1:6]            # statuses where the car is physically in the workshop
OPEN_STATUSES = STAGES[:6]        # not finished
STAGE_ICON = {"Booked": "calendar", "Vehicle Received": "car", "Inspection": "inspect",
              "In Service": "wrench", "Quality Check": "shield",
              "Ready for Delivery": "flag", "Delivered": "check"}
STAGE_TEXT = {
    "Booked": "Your appointment is confirmed.",
    "Vehicle Received": "We have received your vehicle at the workshop.",
    "Inspection": "Our technician is inspecting your vehicle.",
    "In Service": "Your vehicle is being serviced.",
    "Quality Check": "Final quality check is in progress.",
    "Ready for Delivery": "Your vehicle is ready. Please collect it.",
    "Delivered": "Vehicle delivered. Thank you for choosing AutoCare!",
    "Cancelled": "This booking was cancelled.",
}

SLOTS = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
BAYS = 2                      # vehicles that can be handled per slot
BOOKING_HORIZON_DAYS = 60
GST = 0.18
TIERS = [("Bronze", 0, 0), ("Silver", 10000, 3), ("Gold", 30000, 5), ("Platinum", 75000, 10)]
PAY_METHODS = ["UPI", "Card", "Cash", "Net Banking"]
ROLES = ["admin", "advisor", "technician"]
VEHICLE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- time & formatting
def now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=TZ_OFFSET_MIN)


def today():
    return now().date()


def ts(dt=None):
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def parse_ts(s):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def inr(v, dec=0):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    neg = v < 0
    v = abs(v)
    whole, _, frac = f"{v:.{dec}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        whole = f"{head},{tail}"
    return f"{'-' if neg else ''}₹{whole}" + (f".{frac}" if frac else "")


def add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def norm_reg(s):
    reg = re.sub(r"[\s\-]", "", (s or "")).upper()
    return reg if VEHICLE_RE.match(reg) else None


def mask_phone(p):
    return f"+91 {p[:2]}•••••{p[-3:]}" if p and len(p) == 10 else p or ""


def to_int(v, default=None, lo=None, hi=None):
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


def to_float(v, default=None, lo=None):
    try:
        n = float(str(v).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    return n


# ---------------------------------------------------------------- CSRF & sessions
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired or the security token is missing. Go back, refresh the page and try again.")


def safe_next(url):
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


def current_customer():
    if "customer" not in g:
        g.customer = None
        cid = session.get("cid")
        if cid:
            g.customer = get_db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    return g.customer


def current_staff():
    if "staff" not in g:
        g.staff = None
        sid = session.get("sid")
        if sid:
            g.staff = get_db().execute("SELECT * FROM staff WHERE id=? AND active=1", (sid,)).fetchone()
    return g.staff


def login_customer(cid):
    session.clear()
    session["cid"] = cid
    session.permanent = True
    csrf_token()
    get_db().execute("UPDATE customers SET last_login=? WHERE id=?", (ts(), cid))


def login_staff(sid):
    session.clear()
    session["sid"] = sid
    session.permanent = True
    csrf_token()


def customer_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not current_customer():
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return f(*a, **k)
    return wrapper


def staff_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            st = current_staff()
            if not st:
                return redirect(url_for("staff_login", next=request.full_path.rstrip("?")))
            if roles and st["role"] not in roles:
                abort(403)
            return f(*a, **k)
        return wrapper
    return deco


def hash_password(pw):
    return generate_password_hash(pw, method="pbkdf2:sha256")


def verify_password(h, pw):
    return check_password_hash(h, pw)


# ---------------------------------------------------------------- OTP
def send_sms(phone, message):
    """Hook for a real SMS gateway (Twilio, MSG91, Fast2SMS...). Prints to the console in demo mode."""
    print(f"[SMS to +91{phone}] {message}")


def _otp_hash(phone, code):
    key = os.environ.get("SECRET_KEY", "autocare")
    return hashlib.sha256(f"{key}:{phone}:{code}".encode()).hexdigest()


def issue_otp(db, phone, purpose):
    """Create and send an OTP. Returns (code, error, kind)."""
    n = now()
    hour_ago = ts(n - timedelta(hours=1))
    recent = db.execute("SELECT COUNT(*) FROM otps WHERE phone=? AND created_at>?", (phone, hour_ago)).fetchone()[0]
    if recent >= OTP_MAX_PER_HOUR:
        return None, "Too many OTP requests for this number. Please try again in an hour.", "limit"
    last = db.execute("SELECT created_at FROM otps WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
    if last and OTP_COOLDOWN and (n - parse_ts(last["created_at"])).total_seconds() < OTP_COOLDOWN:
        return None, f"Please wait {OTP_COOLDOWN} seconds before requesting another OTP.", "cooldown"
    code = f"{secrets.randbelow(900000) + 100000}"
    db.execute("INSERT INTO otps(phone, code_hash, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
               (phone, _otp_hash(phone, code), purpose, ts(n), ts(n + timedelta(seconds=OTP_TTL))))
    send_sms(phone, f"{code} is your AutoCare verification code. Valid for {OTP_TTL // 60} minutes. Do not share it.")
    return code, None, None


def check_otp(db, phone, code):
    row = db.execute("SELECT * FROM otps WHERE phone=? AND used=0 ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
    if not row:
        return False, "No active OTP. Please request a new one."
    if parse_ts(row["expires_at"]) < now():
        return False, "This OTP has expired. Please request a new one."
    if row["attempts"] >= OTP_MAX_TRIES:
        return False, "Too many wrong attempts. Please request a new OTP."
    db.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (row["id"],))
    if not re.fullmatch(r"\d{6}", code or "") or not hmac.compare_digest(row["code_hash"], _otp_hash(phone, code)):
        left = OTP_MAX_TRIES - row["attempts"] - 1
        return False, f"Incorrect OTP. {max(left, 0)} attempt(s) left."
    db.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
    return True, None


# ---------------------------------------------------------------- loyalty & pricing
def loyalty(db, cid):
    spend = db.execute("SELECT COALESCE(SUM(total),0) FROM bookings WHERE customer_id=? AND status='Delivered'",
                       (cid,)).fetchone()[0]
    idx = max(i for i, t in enumerate(TIERS) if spend >= t[1])
    cur = TIERS[idx]
    nxt = TIERS[idx + 1] if idx + 1 < len(TIERS) else None
    progress = 100 if not nxt else int((spend - cur[1]) / (nxt[1] - cur[1]) * 100)
    return dict(spend=spend, points=int(spend // 100), tier=cur[0], discount=cur[2], idx=idx,
                next=nxt[0] if nxt else None, next_discount=nxt[2] if nxt else None,
                to_next=(nxt[1] - spend) if nxt else 0, progress=progress)


def price(subtotal, discount_pct):
    disc = round(subtotal * discount_pct / 100, 2)
    taxable = subtotal - disc
    tax = round(taxable * GST, 2)
    return dict(subtotal=round(subtotal, 2), discount=disc, tax=tax, total=round(taxable + tax, 2))


def recalc_booking(db, bid):
    b = db.execute("SELECT discount_pct FROM bookings WHERE id=?", (bid,)).fetchone()
    sub = db.execute("SELECT COALESCE(SUM(price*qty),0) FROM booking_items WHERE booking_id=?", (bid,)).fetchone()[0]
    p = price(sub, b["discount_pct"])
    db.execute("UPDATE bookings SET subtotal=?, discount=?, tax=?, total=?, updated_at=? WHERE id=?",
               (p["subtotal"], p["discount"], p["tax"], p["total"], ts(), bid))
    return p


# ---------------------------------------------------------------- slots
def slot_load(db, date_s, exclude=None):
    q = "SELECT time_slot, COUNT(*) n FROM bookings WHERE booking_date=? AND status!='Cancelled'"
    args = [date_s]
    if exclude:
        q += " AND id!=?"
        args.append(exclude)
    q += " GROUP BY time_slot"
    return {r["time_slot"]: r["n"] for r in db.execute(q, args)}


def slot_availability(db, date_s, exclude=None):
    d = parse_date(date_s)
    if not d:
        return dict(closed=True, message="Pick a date to see available slots.", slots={})
    if d < today():
        return dict(closed=True, message="That date is in the past.", slots={})
    if d > today() + timedelta(days=BOOKING_HORIZON_DAYS):
        return dict(closed=True, message=f"Bookings open up to {BOOKING_HORIZON_DAYS} days ahead.", slots={})
    if d.weekday() == 6:
        return dict(closed=True, message="The workshop is closed on Sundays.", slots={})
    load = slot_load(db, d.isoformat(), exclude)
    hm = now().strftime("%H:%M")
    slots = {s: dict(free=max(BAYS - load.get(s, 0), 0), total=BAYS, past=(d == today() and s <= hm))
             for s in SLOTS}
    return dict(closed=False, message="", slots=slots)


def validate_slot(db, date_s, slot, vehicle_id, exclude=None):
    av = slot_availability(db, date_s, exclude)
    if av["closed"]:
        return [av["message"]]
    if slot not in SLOTS:
        return ["Select a time slot."]
    info = av["slots"][slot]
    if info["past"]:
        return ["That time slot has already passed."]
    if info["free"] <= 0:
        return [f"The {slot} slot on {date_s} is full. Please choose another slot."]
    q = "SELECT 1 FROM bookings WHERE vehicle_id=? AND booking_date=? AND status!='Cancelled'"
    args = [vehicle_id, date_s]
    if exclude:
        q += " AND id!=?"
        args.append(exclude)
    if db.execute(q, args).fetchone():
        return ["This vehicle already has a booking on that date."]
    return []


# ---------------------------------------------------------------- bookings
def notify(db, customer_id, title, body="", link=None):
    db.execute("INSERT INTO notifications(customer_id, title, body, link, created_at) VALUES (?,?,?,?,?)",
               (customer_id, title, body, link, ts()))


def add_history(db, bid, status, note="", staff_id=None):
    db.execute("INSERT INTO status_history(booking_id, status, note, staff_id, created_at) VALUES (?,?,?,?,?)",
               (bid, status, note, staff_id, ts()))


def create_booking(db, customer_id, vehicle_id, service_ids, date_s, slot, odometer=None,
                   pickup=False, notes="", source="online", staff_id=None):
    n = ts()
    disc = loyalty(db, customer_id)["discount"]
    cur = db.execute(
        """INSERT INTO bookings(ref, customer_id, vehicle_id, booking_date, time_slot, status, notes, pickup,
                                odometer_in, discount_pct, source, created_at, updated_at)
           VALUES (?,?,?,?,?,'Booked',?,?,?,?,?,?,?)""",
        (f"TMP{secrets.token_hex(5)}", customer_id, vehicle_id, date_s, slot, notes, int(bool(pickup)),
         odometer, disc, source, n, n))
    bid = cur.lastrowid
    db.execute("UPDATE bookings SET ref=? WHERE id=?", (f"AC{today():%y%m}{bid:04d}", bid))
    for sid in service_ids:
        s = db.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
        db.execute("INSERT INTO booking_items(booking_id, service_id, name, price) VALUES (?,?,?,?)",
                   (bid, s["id"], s["name"], s["base_price"]))
    recalc_booking(db, bid)
    add_history(db, bid, "Booked", "Appointment confirmed" + (" (walk-in)" if source == "walk-in" else ""), staff_id)
    ref = db.execute("SELECT ref FROM bookings WHERE id=?", (bid,)).fetchone()["ref"]
    notify(db, customer_id, f"Booking confirmed · {ref}",
           f"{date_s} at {slot}. We'll keep you posted here.", f"/app/bookings/{bid}")
    return bid


def track_times(db, bid):
    rows = db.execute("SELECT status, MIN(created_at) t FROM status_history WHERE booking_id=? GROUP BY status", (bid,))
    return {r["status"]: r["t"] for r in rows}


def next_due_for(db, bid, service_date, odometer):
    rows = db.execute("""SELECT s.interval_km ik, s.interval_months im FROM booking_items bi
                         JOIN services s ON s.id=bi.service_id WHERE bi.booking_id=?""", (bid,)).fetchall()
    kms = [r["ik"] for r in rows if r["ik"]]
    mos = [r["im"] for r in rows if r["im"]]
    ik = min(kms) if kms else 10000
    im = min(mos) if mos else 6
    return add_months(service_date, im).isoformat(), (odometer or 0) + ik


def finalize_delivery(db, b):
    veh = db.execute("SELECT * FROM vehicles WHERE id=?", (b["vehicle_id"],)).fetchone()
    odo = b["odometer_in"] or veh["odometer"] or 0
    names = ", ".join(r["name"] for r in db.execute("SELECT name FROM booking_items WHERE booking_id=?", (b["id"],)))
    total = db.execute("SELECT total FROM bookings WHERE id=?", (b["id"],)).fetchone()["total"]
    due_date, due_km = next_due_for(db, b["id"], today(), odo)
    db.execute("DELETE FROM maintenance WHERE booking_id=?", (b["id"],))
    db.execute("""INSERT INTO maintenance(vehicle_id, booking_id, service_date, odometer, services, cost,
                                          next_due_date, next_due_km, source)
                  VALUES (?,?,?,?,?,?,?,?,'workshop')""",
               (b["vehicle_id"], b["id"], today().isoformat(), odo, names, total, due_date, due_km))
    db.execute("UPDATE vehicles SET odometer=MAX(odometer, ?) WHERE id=?", (odo, b["vehicle_id"]))
    return due_date, due_km


def set_status(db, bid, status, note="", staff_id=None):
    b = db.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    if not b or status not in ALL_STATUSES or b["status"] == status:
        return False
    db.execute("UPDATE bookings SET status=?, updated_at=? WHERE id=?", (status, ts(), bid))
    add_history(db, bid, status, note or STAGE_TEXT.get(status, ""), staff_id)
    if b["status"] == "Delivered":
        db.execute("DELETE FROM maintenance WHERE booking_id=?", (bid,))
    veh = db.execute("SELECT reg_no FROM vehicles WHERE id=?", (b["vehicle_id"],)).fetchone()
    text = STAGE_TEXT.get(status, "")
    if status == "Delivered":
        due_date, due_km = finalize_delivery(db, b)
        text += f" Next service due by {parse_date(due_date):%d %b %Y} or {due_km:,} km."
    notify(db, b["customer_id"], f"{b['ref']} · {status}", f"{veh['reg_no']}: {text}", f"/app/bookings/{bid}")
    return True


# ---------------------------------------------------------------- vehicle health
def due_info(last_date, last_km, due_date, due_km, current_km):
    if not due_date:
        return dict(state="none", label="No history", css="muted", color="--muted", pct=0,
                    days_left=None, km_left=None, text="No service history yet")
    dd = parse_date(due_date)
    ld = parse_date(last_date) or today()
    days_left = (dd - today()).days
    km_left = (due_km or 0) - (current_km or 0)
    span_d = max((dd - ld).days, 1)
    span_k = max((due_km or 0) - (last_km or 0), 1)
    pct = max((span_d - days_left) / span_d, ((current_km or 0) - (last_km or 0)) / span_k)
    pct = int(max(0.0, min(1.0, pct)) * 100)
    if days_left < 0 or km_left <= 0:
        parts = []
        if days_left < 0:
            parts.append(f"{-days_left} days late")
        if km_left <= 0:
            parts.append(f"{-km_left:,} km over")
        return dict(state="overdue", label="Overdue", css="danger", color="--danger", pct=100,
                    days_left=days_left, km_left=km_left, text="Overdue · " + " & ".join(parts))
    state, label, css, color = ("soon", "Due soon", "warn", "--warn") if (days_left <= 14 or km_left <= 500) \
        else ("ok", "On track", "ok", "--ok")
    return dict(state=state, label=label, css=css, color=color, pct=pct, days_left=days_left, km_left=km_left,
                text=f"Due in {days_left} days or {km_left:,} km")


def vehicle_overview(db, customer_id=None, vehicle_id=None):
    sql = """SELECT v.*, c.name AS owner, c.phone AS owner_phone,
                    m.service_date AS last_date, m.odometer AS last_km, m.next_due_date, m.next_due_km,
                    m.services AS last_services
             FROM vehicles v JOIN customers c ON c.id=v.customer_id
             LEFT JOIN maintenance m ON m.id=(SELECT id FROM maintenance WHERE vehicle_id=v.id
                                              ORDER BY service_date DESC, id DESC LIMIT 1)"""
    conds, args = [], []
    if customer_id:
        conds.append("v.customer_id=?")
        args.append(customer_id)
    if vehicle_id:
        conds.append("v.id=?")
        args.append(vehicle_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY v.id"
    out = []
    for r in db.execute(sql, args):
        d = dict(r)
        d["due"] = due_info(d["last_date"], d["last_km"], d["next_due_date"], d["next_due_km"], d["odometer"])
        out.append(d)
    return out
