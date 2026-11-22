"""First-run data: admin accounts, service catalogue and (optionally) realistic demo data."""
import os
import random
from datetime import datetime, timedelta

import core

SERVICES = [
    ("General Service", "Complete periodic service with 40-point check", 2500, 180, 10000, 6),
    ("Oil & Filter Change", "Engine oil and oil filter replacement", 1200, 45, 7500, 6),
    ("Brake Service", "Pad inspection, cleaning and adjustment", 3000, 90, 20000, 12),
    ("Tyre Rotation & Alignment", "Rotation and 3D wheel alignment", 1800, 60, 10000, 6),
    ("Wheel Balancing", "Computerised wheel balancing", 800, 30, 10000, 6),
    ("Battery Replacement", "Battery health test and replacement", 4500, 30, 40000, 24),
    ("AC Service", "AC gas top-up, filter and vent clean", 2200, 90, 20000, 12),
    ("Full Inspection", "Comprehensive 80-point inspection report", 1500, 60, 10000, 6),
    ("Suspension Check", "Shocks, bushes and linkages check", 2800, 90, 30000, 12),
    ("Car Wash & Detailing", "Foam wash, interior vacuum and polish", 1000, 60, 0, 0),
]

STAFF = [
    ("admin", "Admin", "admin", "admin123"),
    ("advisor", "Lakshmi Devi", "advisor", "advisor123"),
    ("tech1", "Ravi Kumar", "technician", "tech123"),
    ("tech2", "Suresh Babu", "technician", "tech123"),
]

CUSTOMERS = [
    ("Arjun Kumar", "9876500001", "arjun.k@example.com"), ("Priya Natarajan", "9876500002", "priya.n@example.com"),
    ("Karthik Subramanian", "9876500003", "karthik.s@example.com"), ("Meena Lakshmi", "9876500004", "meena.l@example.com"),
    ("Rahul Verma", "9876500005", "rahul.v@example.com"), ("Divya Shankar", "9876500006", "divya.s@example.com"),
    ("Mohammed Faizal", "9876500007", "faizal.m@example.com"), ("Sneha Reddy", "9876500008", "sneha.r@example.com"),
    ("Vignesh Raja", "9876500009", "vignesh.r@example.com"), ("Anitha Balu", "9876500010", "anitha.b@example.com"),
]

# owner index, reg no, make, model, year, fuel, current odometer
VEHICLES = [
    (0, "TN09CK4521", "Hyundai", "Creta", 2021, "Petrol", 38200), (0, "TN09BX7788", "Maruti", "Swift", 2018, "Petrol", 71500),
    (1, "TN10AD3345", "Honda", "City", 2020, "Petrol", 46100), (2, "TN07BZ9012", "Toyota", "Innova Crysta", 2019, "Diesel", 92400),
    (3, "TN22DM6654", "Maruti", "Baleno", 2021, "Petrol", 29800), (4, "TN01AY2201", "Kia", "Seltos", 2022, "Diesel", 41000),
    (5, "TN11BQ4410", "Mahindra", "XUV700", 2023, "Diesel", 22800), (6, "TN37CE8890", "Hyundai", "i20", 2019, "Petrol", 63400),
    (7, "TN45AK1357", "Skoda", "Slavia", 2023, "Petrol", 15900), (8, "TN06BT7712", "Maruti", "Ertiga", 2020, "Petrol", 58800),
    (9, "TN14DA9087", "Tata", "Harrier", 2021, "Diesel", 52300),
]
# vehicles whose last visit is long ago -> show "overdue" / "due soon" in the demo (index: days since last visit)
STALE = {3: 235, 7: 218, 9: 247, 5: 172}

COMBOS = [["General Service"], ["Oil & Filter Change"], ["General Service", "Wheel Balancing"], ["Brake Service"],
          ["Tyre Rotation & Alignment", "Wheel Balancing"], ["AC Service"], ["Full Inspection"], ["Battery Replacement"],
          ["Oil & Filter Change", "Full Inspection"], ["General Service", "Car Wash & Detailing"], ["Suspension Check"]]
COMMENTS = ["Quick and transparent service.", "Loved the live status updates!", "Pickup was on time. Great work.",
            "Fair pricing and clean workshop.", "Took a bit longer than expected.", "Very professional team."]


def seed_base(conn):
    n = core.ts()
    for u, name, role, pw in STAFF:
        conn.execute("INSERT INTO staff(username, full_name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                     (u, name, role, core.hash_password(pw), n))
    for s in SERVICES:
        conn.execute("""INSERT INTO services(name, description, base_price, duration_min, interval_km, interval_months)
                        VALUES (?,?,?,?,?,?)""", s)
    conn.commit()


def _open_day(d):
    return d + timedelta(days=1) if d.weekday() == 6 else d


def seed_demo(conn):
    rnd = random.Random(11)
    t0 = core.today()
    now = core.now()
    svc = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM services")}
    techs = [r["id"] for r in conn.execute("SELECT id FROM staff WHERE role='technician'")]
    advisor = conn.execute("SELECT id FROM staff WHERE role='advisor'").fetchone()["id"]

    cids = []
    for name, phone, email in CUSTOMERS:
        created = core.ts(datetime.combine(t0 - timedelta(days=rnd.randint(320, 430)), datetime.min.time()))
        cur = conn.execute("""INSERT INTO customers(name, phone, email, phone_verified, created_at, last_login)
                              VALUES (?,?,?,1,?,?)""", (name, phone, email, created, core.ts(now - timedelta(days=rnd.randint(0, 20)))))
        cids.append(cur.lastrowid)
    vids = []
    for owner, reg, make, model, year, fuel, odo in VEHICLES:
        cur = conn.execute("""INSERT INTO vehicles(customer_id, reg_no, make, model, year, fuel, odometer, created_at)
                              VALUES (?,?,?,?,?,?,?,?)""", (cids[owner], reg, make, model, year, fuel, odo,
                                                           core.ts(now - timedelta(days=400))))
        vids.append(cur.lastrowid)

    slot_use = {}

    def pick_slot(d):
        free = [s for s in core.SLOTS if slot_use.get((d, s), 0) < core.BAYS]
        s = rnd.choice(free)
        slot_use[(d, s)] = slot_use.get((d, s), 0) + 1
        return s

    # ---- historic visits
    visits = []
    for vi, v in enumerate(VEHICLES):
        newest = t0 - timedelta(days=STALE.get(vi, 5))
        d = t0 - timedelta(days=rnd.randint(380, 420))
        while True:
            d += timedelta(days=rnd.randint(38, 85))
            if d > newest:
                break
            visits.append((_open_day(d), vi))
        if STALE.get(vi):
            visits.append((_open_day(newest), vi))
    visits.sort()

    def insert_booking(d, vi, combo, status, slot, tech, source="online", created_dt=None, odometer=None):
        owner = cids[VEHICLES[vi][0]]
        lo = core.loyalty(conn, owner)
        created = created_dt or datetime.combine(d - timedelta(days=rnd.randint(1, 6)), datetime.min.time()) + timedelta(hours=rnd.randint(8, 20), minutes=rnd.randint(0, 59))
        items = [svc[n] for n in combo]
        sub = sum(i["base_price"] for i in items)
        p = core.price(sub, lo["discount"])
        cur = conn.execute(
            """INSERT INTO bookings(ref, customer_id, vehicle_id, booking_date, time_slot, status, pickup, odometer_in,
                                    technician_id, discount_pct, subtotal, discount, tax, total, source, created_at, updated_at)
               VALUES ('TMP',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, vids[vi], d.isoformat(), slot, status, int(rnd.random() < .25), odometer, tech, lo["discount"],
             p["subtotal"], p["discount"], p["tax"], p["total"], source, core.ts(created), core.ts(created)))
        bid = cur.lastrowid
        conn.execute("UPDATE bookings SET ref=? WHERE id=?", (f"AC{d:%y%m}{bid:04d}", bid))
        for i in items:
            conn.execute("INSERT INTO booking_items(booking_id, service_id, name, price) VALUES (?,?,?,?)",
                         (bid, i["id"], i["name"], i["base_price"]))
        return bid, created, items, p

    def history(bid, created, d, slot, upto, tech_note=True):
        h, m = map(int, slot.split(":"))
        t = datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m + rnd.randint(0, 8))
        steps = [("Booked", created, "Appointment confirmed")]
        offsets = [0, 18, 45, 140, 175, 205]
        for st, off in zip(core.STAGES[1:], offsets):
            steps.append((st, t + timedelta(minutes=off + rnd.randint(0, 10)), core.STAGE_TEXT[st]))
        for st, when, note in steps[: core.STAGES.index(upto) + 1]:
            conn.execute("INSERT INTO status_history(booking_id, status, note, created_at) VALUES (?,?,?,?)",
                         (bid, st, note, core.ts(when)))
        return steps[core.STAGES.index(upto)][1]

    for d, vi in visits:
        combo = rnd.choice(COMBOS)
        slot = pick_slot(d)
        odo = VEHICLES[vi][6] - int((t0 - d).days * rnd.uniform(24, 42))
        cancelled = rnd.random() < 0.06
        tech = rnd.choice(techs)
        status = "Cancelled" if cancelled else "Delivered"
        bid, created, items, p = insert_booking(d, vi, combo, status, slot, tech, rnd.choice(["online", "online", "walk-in"]),
                                                odometer=max(odo, 1000))
        if cancelled:
            history(bid, created, d, slot, "Booked")
            conn.execute("INSERT INTO status_history(booking_id, status, note, created_at) VALUES (?,?,?,?)",
                         (bid, "Cancelled", "Cancelled by customer", core.ts(created + timedelta(hours=5))))
            continue
        done = history(bid, created, d, slot, "Delivered")
        paid = d < t0 - timedelta(days=4) or rnd.random() < .7
        method = rnd.choices(core.PAY_METHODS, [45, 25, 20, 10])[0] if paid else None
        rating = rnd.choices([5, 4, 3, 2, 1], [50, 30, 12, 5, 3])[0] if rnd.random() < .75 else None
        conn.execute("""UPDATE bookings SET payment_status=?, payment_method=?, paid_at=?, rating=?, feedback=?, updated_at=?
                        WHERE id=?""", ("Paid" if paid else "Unpaid", method, core.ts(done) if paid else None, rating,
                                        rnd.choice(COMMENTS) if rating and rnd.random() < .6 else None, core.ts(done), bid))
        ik = min([i["interval_km"] for i in items if i["interval_km"]] or [10000])
        im = min([i["interval_months"] for i in items if i["interval_months"]] or [6])
        conn.execute("""INSERT INTO maintenance(vehicle_id, booking_id, service_date, odometer, services, cost, next_due_date,
                                                next_due_km, source) VALUES (?,?,?,?,?,?,?,?, 'workshop')""",
                     (vids[vi], bid, d.isoformat(), max(odo, 1000), ", ".join(i["name"] for i in items), p["total"],
                      core.add_months(d, im).isoformat(), max(odo, 1000) + ik))

    # ---- today's workshop floor + upcoming bookings
    today_plan = [(0, "Vehicle Received", "09:00"), (2, "Inspection", "09:00"), (4, "In Service", "10:00"),
                  (8, "In Service", "10:00"), (1, "Quality Check", "11:00"), (6, "Ready for Delivery", "11:00")]
    for vi, status, slot in today_plan:
        slot_use[(t0, slot)] = slot_use.get((t0, slot), 0) + 1
        combo = rnd.choice(COMBOS)
        bid, created, _, _ = insert_booking(t0, vi, combo, status, slot, rnd.choice(techs), created_dt=now - timedelta(days=2),
                                            odometer=VEHICLES[vi][6])
        history(bid, created, t0, slot, status)
    upcoming = [(10, "Booked", 0, "14:00"), (3, "Booked", 1, "10:00"), (7, "Booked", 2, "11:00"),
                (9, "Booked", 4, "15:00"), (5, "Booked", 0, "16:00")]
    for vi, status, plus, slot in upcoming:
        d = _open_day(t0 + timedelta(days=plus))
        if (d, slot) in slot_use and slot_use[(d, slot)] >= core.BAYS:
            slot = pick_slot(d)
        slot_use[(d, slot)] = slot_use.get((d, slot), 0) + 1
        bid, created, _, _ = insert_booking(d, vi, rnd.choice(COMBOS), status, slot, None, created_dt=now - timedelta(hours=rnd.randint(2, 40)),
                                            odometer=VEHICLES[vi][6])
        history(bid, created, d, slot, "Booked")

    # ---- a few notifications
    for cid in cids:
        conn.execute("INSERT INTO notifications(customer_id, title, body, created_at) VALUES (?,?,?,?)",
                     (cid, "Welcome to AutoCare", "Book services, track your vehicle live and download invoices here.",
                      core.ts(now - timedelta(days=rnd.randint(5, 60)))))
    conn.commit()


def ensure_seeded(conn):
    if conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
        seed_base(conn)
        if os.environ.get("AUTOCARE_DEMO", "1") == "1":
            seed_demo(conn)


if __name__ == "__main__":
    import db
    c = db.init_db()
    ensure_seeded(c)
    print("Database ready:", db.DB_PATH)
