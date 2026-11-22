"""Company (staff) portal routes (/staff/...)."""
import csv
import io
import secrets
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, send_file, url_for

import core
import pdfs
import reports
from core import current_staff, staff_required
from db import get_db

bp = Blueprint("staff", __name__, url_prefix="/staff")
ADV = ("admin", "advisor")


def _booking(db, bid):
    b = db.execute("""SELECT b.*, v.reg_no, v.make, v.model, v.odometer vodo, c.name cname, c.phone cphone, c.email cemail,
                             s.full_name tech
                      FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id JOIN customers c ON c.id=b.customer_id
                      LEFT JOIN staff s ON s.id=b.technician_id WHERE b.id=?""", (bid,)).fetchone()
    if not b:
        abort(404)
    return dict(b)


# ------------------------------------------------------------------ dashboard
@bp.route("")
@staff_required()
def dashboard():
    db = get_db()
    t, n = core.today(), core.now()
    ts_ = t.isoformat()
    ms = t.replace(day=1)
    pm_end = ms - timedelta(days=1)
    pm_start = pm_end.replace(day=1)
    pm_same = min(pm_start + timedelta(days=t.day - 1), pm_end)
    cur = reports._kpis(db, ms, t)
    prev = reports._kpis(db, pm_start, pm_same)
    q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    unpaid = db.execute("""SELECT COUNT(*) n, COALESCE(SUM(total),0) amt FROM bookings
                           WHERE status='Delivered' AND payment_status!='Paid'""").fetchone()
    kp = dict(
        today=q("SELECT COUNT(*) FROM bookings WHERE booking_date=? AND status!='Cancelled'", ts_),
        in_workshop=q("SELECT COUNT(*) FROM bookings WHERE status IN ('Vehicle Received','Inspection','In Service','Quality Check')"),
        ready=q("SELECT COUNT(*) FROM bookings WHERE status='Ready for Delivery'"),
        revenue=cur["revenue"], rev_delta=reports._delta(cur["revenue"], prev["revenue"]),
        unpaid_n=unpaid["n"], unpaid_amt=unpaid["amt"],
        rating=db.execute("SELECT AVG(rating) FROM bookings WHERE rating IS NOT NULL AND booking_date>=?",
                          ((t - timedelta(days=90)).isoformat(),)).fetchone()[0],
        customers=q("SELECT COUNT(*) FROM customers"),
    )
    # 30-day revenue
    days = [(t - timedelta(days=i)) for i in range(29, -1, -1)]
    buckets = [(d, d, f"{d:%d %b}") for d in days]
    series = reports._series(db, buckets, days[0], t)
    status_split = [dict(r) for r in db.execute(
        "SELECT status, COUNT(*) n FROM bookings WHERE booking_date BETWEEN ? AND ? GROUP BY status", (ms.isoformat(), t.isoformat()))]
    # today's schedule by slot
    rows = db.execute(f"""{reports.BOOKING_ROWS} WHERE b.booking_date=? AND b.status!='Cancelled'
                          ORDER BY b.time_slot, b.id""", (ts_,)).fetchall()
    schedule = {s: [] for s in core.SLOTS}
    for r in rows:
        schedule.setdefault(r["time_slot"], []).append(dict(r))
    hm = n.strftime("%H:%M")
    # attention list
    attention = []
    late = db.execute("""SELECT b.id, b.ref, b.time_slot, v.reg_no FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
                         WHERE b.booking_date=? AND b.status='Booked' AND b.time_slot<?""", (ts_, hm)).fetchall()
    for r in late:
        attention.append(dict(icon="clock", tone="warn", text=f"{r['reg_no']} hasn't arrived for the {r['time_slot']} slot",
                              link=url_for("staff.booking", bid=r["id"])))
    for r in db.execute("""SELECT b.id, b.ref, b.total, v.reg_no FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
                           WHERE b.status='Delivered' AND b.payment_status!='Paid' ORDER BY b.booking_date DESC LIMIT 4"""):
        attention.append(dict(icon="card", tone="danger", text=f"Payment pending · {r['ref']} · {core.inr(r['total'])}",
                              link=url_for("staff.booking", bid=r["id"])))
    for r in db.execute("""SELECT b.id, b.ref, b.rating, c.name FROM bookings b JOIN customers c ON c.id=b.customer_id
                           WHERE b.rating<=2 AND b.booking_date>=? ORDER BY b.booking_date DESC LIMIT 3""",
                        ((t - timedelta(days=30)).isoformat(),)):
        attention.append(dict(icon="star", tone="danger", text=f"Low rating ({r['rating']}★) from {r['name']} · {r['ref']}",
                              link=url_for("staff.booking", bid=r["id"])))
    overdue = [v for v in core.vehicle_overview(db) if v["due"]["state"] == "overdue"]
    if overdue:
        attention.append(dict(icon="alert", tone="warn", text=f"{len(overdue)} vehicle(s) overdue for service",
                              link=url_for("staff.reminders")))
    top_services = [dict(r) for r in db.execute(
        """SELECT bi.name, SUM(bi.qty) n, SUM(bi.price*bi.qty) rev FROM booking_items bi JOIN bookings b ON b.id=bi.booking_id
           WHERE b.booking_date BETWEEN ? AND ? AND b.status='Delivered' GROUP BY bi.name ORDER BY rev DESC LIMIT 6""",
        (ms.isoformat(), t.isoformat()))]
    tech_load = [dict(r) for r in db.execute(
        """SELECT s.full_name, SUM(b.status IN ('Vehicle Received','Inspection','In Service','Quality Check','Ready for Delivery')) active,
                  SUM(b.booking_date=? AND b.status='Delivered') done_today
           FROM staff s LEFT JOIN bookings b ON b.technician_id=s.id WHERE s.role='technician' AND s.active=1
           GROUP BY s.id""", (ts_,))]
    recent = db.execute(f"{reports.BOOKING_ROWS} ORDER BY b.id DESC LIMIT 8").fetchall()
    return render_template("s_dashboard.html", kp=kp, series=series, status_split=status_split, schedule=schedule,
                           hm=hm, attention=attention, top_services=top_services, tech_load=tech_load, recent=recent)


# ------------------------------------------------------------------ workshop board
@bp.route("/board")
@staff_required()
def board():
    db = get_db()
    t = core.today().isoformat()
    cols = []
    for st in core.STAGES[:6]:
        cond = "AND b.booking_date<=?" if st == "Booked" else ""
        args = [st] + ([t] if st == "Booked" else [])
        rows = db.execute(f"""SELECT b.id, b.ref, b.booking_date, b.time_slot, b.status, b.pickup, v.reg_no, v.make, v.model,
                                     c.name cname, s.full_name tech,
                                     (SELECT GROUP_CONCAT(name, ', ') FROM booking_items WHERE booking_id=b.id) services
                              FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id JOIN customers c ON c.id=b.customer_id
                              LEFT JOIN staff s ON s.id=b.technician_id
                              WHERE b.status=? {cond} ORDER BY b.booking_date, b.time_slot""", args).fetchall()
        cols.append(dict(status=st, cards=[dict(r) for r in rows]))
    done = db.execute(f"{reports.BOOKING_ROWS} WHERE b.status='Delivered' AND b.booking_date=? ORDER BY b.id DESC", (t,)).fetchall()
    return render_template("s_board.html", cols=cols, done=done)


@bp.route("/board/advance/<int:bid>", methods=["POST"])
@staff_required()
def advance(bid):
    db = get_db()
    b = _booking(db, bid)
    if b["status"] in core.STAGES[:-1]:
        nxt = core.STAGES[core.STAGES.index(b["status"]) + 1]
        core.set_status(db, bid, nxt, staff_id=current_staff()["id"])
        flash(f"{b['reg_no']} moved to {nxt}.", "success")
    return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/staff") else url_for("staff.board"))


# ------------------------------------------------------------------ bookings
VIEWS = [("all", "All"), ("today", "Today"), ("upcoming", "Upcoming"), ("workshop", "In workshop"),
         ("ready", "Ready"), ("unpaid", "Unpaid"), ("cancelled", "Cancelled")]


@bp.route("/bookings")
@staff_required()
def bookings():
    db = get_db()
    view = request.args.get("view", "all")
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status", "")
    d1, d2 = core.parse_date(request.args.get("from")), core.parse_date(request.args.get("to"))
    t = core.today().isoformat()
    where, args = ["1=1"], []
    cond = {"today": ("b.booking_date=? AND b.status!='Cancelled'", [t]),
            "upcoming": ("b.booking_date>? AND b.status='Booked'", [t]),
            "workshop": ("b.status IN ('Vehicle Received','Inspection','In Service','Quality Check')", []),
            "ready": ("b.status='Ready for Delivery'", []),
            "unpaid": ("b.status='Delivered' AND b.payment_status!='Paid'", []),
            "cancelled": ("b.status='Cancelled'", [])}.get(view)
    if cond:
        where.append(cond[0])
        args += cond[1]
    if q:
        like = f"%{q}%"
        where.append("(b.ref LIKE ? OR c.name LIKE ? OR c.phone LIKE ? OR v.reg_no LIKE ?)")
        args += [like] * 4
    if status in core.ALL_STATUSES:
        where.append("b.status=?")
        args.append(status)
    if d1:
        where.append("b.booking_date>=?")
        args.append(d1.isoformat())
    if d2:
        where.append("b.booking_date<=?")
        args.append(d2.isoformat())
    w = " AND ".join(where)
    total = db.execute(f"""SELECT COUNT(*) FROM bookings b JOIN customers c ON c.id=b.customer_id
                           JOIN vehicles v ON v.id=b.vehicle_id WHERE {w}""", args).fetchone()[0]
    page = max(core.to_int(request.args.get("page"), 1) or 1, 1)
    per = 20
    pages = max((total + per - 1) // per, 1)
    page = min(page, pages)
    rows = db.execute(f"{reports.BOOKING_ROWS} WHERE {w} ORDER BY b.booking_date DESC, b.time_slot DESC, b.id DESC LIMIT ? OFFSET ?",
                      (*args, per, (page - 1) * per)).fetchall()
    return render_template("s_bookings.html", rows=rows, view=view, views=VIEWS, q=q, status=status, total=total,
                           page=page, pages=pages, d1=request.args.get("from", ""), d2=request.args.get("to", ""))


@bp.route("/bookings/new", methods=["GET", "POST"])
@staff_required(*ADV)
def new_booking():
    db = get_db()
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY base_price DESC").fetchall()
    if request.method == "POST":
        f = request.form
        phone = core.norm_phone(f.get("phone"))
        reg = core.norm_reg(f.get("reg_no"))
        name = " ".join((f.get("name") or "").split())
        model = " ".join((f.get("model") or "").split())
        valid = {s["id"] for s in services}
        sids = [i for i in (core.to_int(x) for x in f.getlist("services")) if i in valid]
        date_s, slot = f.get("booking_date", ""), f.get("time_slot", "")
        errors = []
        if not phone:
            errors.append("Enter a valid 10-digit customer mobile number.")
        if not reg:
            errors.append("Enter a valid vehicle registration number.")
        if not sids:
            errors.append("Select at least one service.")
        cust = db.execute("SELECT * FROM customers WHERE phone=?", (phone,)).fetchone() if phone else None
        if phone and not cust and len(name) < 2:
            errors.append("New customer: enter the customer's name.")
        veh = db.execute("SELECT * FROM vehicles WHERE reg_no=?", (reg,)).fetchone() if reg else None
        if veh and cust and veh["customer_id"] != cust["id"]:
            errors.append("That vehicle belongs to a different customer.")
        if veh and not cust:
            errors.append("That vehicle is registered to another customer. Use that customer's phone number.")
        if reg and not veh and not model:
            errors.append("New vehicle: enter the model.")
        if not errors:
            errors += core.validate_slot(db, date_s, slot, veh["id"] if veh else 0)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("s_new_booking.html", services=services, form=f, selected=set(f.getlist("services")))
        n = core.ts()
        if not cust:
            cid = db.execute("INSERT INTO customers(name, phone, email, phone_verified, created_at) VALUES (?,?,?,0,?)",
                             (name, phone, (f.get("email") or "").strip() or None, n)).lastrowid
        else:
            cid = cust["id"]
        odo = core.to_int(f.get("odometer"), 0, 0, 2_000_000)
        if not veh:
            vid = db.execute("""INSERT INTO vehicles(customer_id, reg_no, make, model, odometer, created_at) VALUES (?,?,?,?,?,?)""",
                             (cid, reg, (f.get("make") or "").strip(), model, odo, n)).lastrowid
        else:
            vid = veh["id"]
        me = current_staff()
        bid = core.create_booking(db, cid, vid, sids, date_s, slot, odo, False, (f.get("notes") or "").strip()[:300],
                                  source="walk-in", staff_id=me["id"])
        if f.get("checkin"):
            core.set_status(db, bid, "Vehicle Received", staff_id=me["id"])
        flash("Walk-in booking created.", "success")
        return redirect(url_for("staff.booking", bid=bid))
    return render_template("s_new_booking.html", services=services, selected=set(),
                           form={"phone": request.args.get("phone", ""), "booking_date": core.today().isoformat()})


@bp.route("/bookings/<int:bid>")
@staff_required()
def booking(bid):
    db = get_db()
    b = _booking(db, bid)
    items = db.execute("SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (bid,)).fetchall()
    hist = db.execute("""SELECT h.*, s.full_name staff FROM status_history h LEFT JOIN staff s ON s.id=h.staff_id
                         WHERE h.booking_id=? ORDER BY h.id DESC""", (bid,)).fetchall()
    techs = db.execute("SELECT id, full_name, role FROM staff WHERE active=1 AND role IN ('technician','advisor') ORDER BY role DESC, full_name").fetchall()
    lo = core.loyalty(db, b["customer_id"])
    return render_template("s_booking.html", b=b, items=items, hist=hist, techs=techs, times=core.track_times(db, bid), lo=lo,
                           services=db.execute("SELECT * FROM services WHERE active=1 ORDER BY name").fetchall())


@bp.route("/bookings/<int:bid>/status", methods=["POST"])
@staff_required()
def set_status(bid):
    db = get_db()
    b = _booking(db, bid)
    status = request.form.get("status", "")
    if status not in core.ALL_STATUSES:
        flash("Choose a valid status.", "error")
    elif status == b["status"]:
        flash("The booking is already in that status.", "error")
    else:
        core.set_status(db, bid, status, (request.form.get("note") or "").strip()[:200], current_staff()["id"])
        flash(f"Status updated to {status}. The customer has been notified.", "success")
    return redirect(url_for("staff.booking", bid=bid))


@bp.route("/bookings/<int:bid>/assign", methods=["POST"])
@staff_required(*ADV)
def assign(bid):
    db = get_db()
    _booking(db, bid)
    tid = core.to_int(request.form.get("technician_id"))
    if tid and not db.execute("SELECT 1 FROM staff WHERE id=? AND active=1", (tid,)).fetchone():
        abort(400)
    db.execute("UPDATE bookings SET technician_id=?, updated_at=? WHERE id=?", (tid, core.ts(), bid))
    flash("Technician assigned.", "success")
    return redirect(url_for("staff.booking", bid=bid))


@bp.route("/bookings/<int:bid>/items", methods=["POST"])
@staff_required(*ADV)
def add_item(bid):
    db = get_db()
    b = _booking(db, bid)
    if b["payment_status"] == "Paid" or b["status"] == "Cancelled":
        flash("Items can't be changed on a paid or cancelled booking.", "error")
        return redirect(url_for("staff.booking", bid=bid))
    sid = core.to_int(request.form.get("service_id"))
    if sid:
        s = db.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
        if not s:
            abort(400)
        name, price = s["name"], s["base_price"]
    else:
        name = " ".join((request.form.get("name") or "").split())[:80]
        price = core.to_float(request.form.get("price"), None, 0)
        if not name or price is None:
            flash("Enter an item name and a price (0 or more).", "error")
            return redirect(url_for("staff.booking", bid=bid))
    qty = core.to_int(request.form.get("qty"), 1, 1, 99)
    db.execute("INSERT INTO booking_items(booking_id, service_id, name, price, qty) VALUES (?,?,?,?,?)", (bid, sid, name, price, qty))
    core.recalc_booking(db, bid)
    flash("Item added and total updated.", "success")
    return redirect(url_for("staff.booking", bid=bid))


@bp.route("/bookings/<int:bid>/items/<int:iid>/delete", methods=["POST"])
@staff_required(*ADV)
def delete_item(bid, iid):
    db = get_db()
    b = _booking(db, bid)
    if b["payment_status"] == "Paid" or b["status"] == "Cancelled":
        flash("Items can't be changed on a paid or cancelled booking.", "error")
    else:
        db.execute("DELETE FROM booking_items WHERE id=? AND booking_id=?", (iid, bid))
        core.recalc_booking(db, bid)
        flash("Item removed.", "success")
    return redirect(url_for("staff.booking", bid=bid))


@bp.route("/bookings/<int:bid>/payment", methods=["POST"])
@staff_required(*ADV)
def payment(bid):
    db = get_db()
    b = _booking(db, bid)
    status = request.form.get("payment_status")
    method = request.form.get("payment_method")
    if status not in ("Paid", "Unpaid") or (status == "Paid" and method not in core.PAY_METHODS):
        flash("Choose a payment status and, for paid invoices, a payment method.", "error")
    elif b["status"] == "Cancelled":
        flash("A cancelled booking can't be billed.", "error")
    else:
        db.execute("UPDATE bookings SET payment_status=?, payment_method=?, paid_at=?, updated_at=? WHERE id=?",
                   (status, method if status == "Paid" else None, core.ts() if status == "Paid" else None, core.ts(), bid))
        if status == "Paid":
            core.notify(db, b["customer_id"], f"Payment received · {b['ref']}",
                        f"We received {core.inr(b['total'])} via {method}. Thank you!", f"/app/bookings/{bid}")
        flash("Payment updated.", "success")
    return redirect(url_for("staff.booking", bid=bid))


@bp.route("/bookings/<int:bid>/invoice.pdf")
@staff_required()
def invoice(bid):
    db = get_db()
    b = _booking(db, bid)
    return send_file(pdfs.invoice_pdf(db, bid), mimetype="application/pdf", download_name=f"{b['ref']}_invoice.pdf")


# ------------------------------------------------------------------ customers
@bp.route("/customers")
@staff_required(*ADV)
def customers():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    like = f"%{q}%"
    rows = db.execute("""SELECT c.*, (SELECT COUNT(*) FROM vehicles WHERE customer_id=c.id) vehicles,
                                (SELECT COUNT(*) FROM bookings WHERE customer_id=c.id AND status='Delivered') visits,
                                (SELECT COALESCE(SUM(total),0) FROM bookings WHERE customer_id=c.id AND status='Delivered') spend,
                                (SELECT MAX(booking_date) FROM bookings WHERE customer_id=c.id AND status!='Cancelled') last_visit
                         FROM customers c WHERE (?='' OR c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ?
                              OR EXISTS (SELECT 1 FROM vehicles v WHERE v.customer_id=c.id AND v.reg_no LIKE ?))
                         ORDER BY spend DESC, c.name""", (q, like, like, like, like)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tier"] = next(t for t in reversed(core.TIERS) if d["spend"] >= t[1])[0]
        out.append(d)
    return render_template("s_customers.html", rows=out, q=q)


@bp.route("/customers/<int:cid>")
@staff_required(*ADV)
def customer(cid):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    kind = request.args.get("p", "all")
    if kind not in ("week", "month", "year", "all"):
        kind = "all"
    r = reports.customer_report(db, cid, kind, reports.parse_anchor(request.args.get("d")))
    return render_template("s_customer.html", c=c, r=r, kind=kind)


@bp.route("/customers/<int:cid>/report.pdf")
@staff_required(*ADV)
def customer_pdf(cid):
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    kind = request.args.get("p", "all")
    if kind not in ("week", "month", "year", "all"):
        kind = "all"
    r = reports.customer_report(db, cid, kind, reports.parse_anchor(request.args.get("d")))
    return send_file(pdfs.customer_report_pdf(r, c), mimetype="application/pdf",
                     download_name=f"{c['name'].replace(' ', '_')}_report.pdf")


# ------------------------------------------------------------------ reminders
@bp.route("/reminders")
@staff_required(*ADV)
def reminders():
    db = get_db()
    state = request.args.get("state", "")
    rows = [v for v in core.vehicle_overview(db) if v["due"]["state"] in ("overdue", "soon")]
    rows.sort(key=lambda v: (v["due"]["state"] != "overdue", v["next_due_date"] or ""))
    counts = dict(overdue=sum(v["due"]["state"] == "overdue" for v in rows), soon=sum(v["due"]["state"] == "soon" for v in rows))
    if state in ("overdue", "soon"):
        rows = [v for v in rows if v["due"]["state"] == state]
    return render_template("s_reminders.html", rows=rows, counts=counts, state=state)


@bp.route("/reminders/send/<int:vid>", methods=["POST"])
@staff_required(*ADV)
def send_reminder(vid):
    db = get_db()
    v = db.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
    if not v:
        abort(404)
    last = core.parse_ts(v["last_reminded"])
    if last and (core.now() - last) < timedelta(days=3):
        flash("A reminder was already sent in the last 3 days.", "error")
    else:
        info = core.vehicle_overview(db, vehicle_id=vid)[0]["due"]
        core.notify(db, v["customer_id"], f"Service reminder · {v['reg_no']}", f"{info['text']}. Book a slot at your convenience.",
                    f"/app/book?vehicle={vid}")
        cust = db.execute("SELECT phone FROM customers WHERE id=?", (v["customer_id"],)).fetchone()
        core.send_sms(cust["phone"], f"AutoCare: your vehicle {v['reg_no']} is due for service. Book at your convenience.")
        db.execute("UPDATE vehicles SET last_reminded=? WHERE id=?", (core.ts(), vid))
        flash(f"Reminder sent for {v['reg_no']}.", "success")
    return redirect(url_for("staff.reminders"))


# ------------------------------------------------------------------ services catalogue
@bp.route("/services", methods=["GET", "POST"])
@staff_required(*ADV)
def services():
    db = get_db()
    edit = core.to_int(request.args.get("edit"))
    if request.method == "POST":
        f = request.form
        sid = core.to_int(f.get("id"))
        name = " ".join((f.get("name") or "").split())[:60]
        price = core.to_float(f.get("base_price"), None, 0)
        dur = core.to_int(f.get("duration_min"), None, 5, 1440)
        ikm = core.to_int(f.get("interval_km"), 0, 0, 200000)
        imo = core.to_int(f.get("interval_months"), 0, 0, 60)
        if not name or price is None or dur is None:
            flash("Enter a name, a price and a duration in minutes.", "error")
        elif db.execute("SELECT 1 FROM services WHERE name=? AND id!=?", (name, sid or 0)).fetchone():
            flash("A service with that name already exists.", "error")
        else:
            vals = (name, (f.get("description") or "").strip()[:160], price, dur, ikm, imo)
            if sid:
                db.execute("""UPDATE services SET name=?, description=?, base_price=?, duration_min=?, interval_km=?,
                              interval_months=?, active=? WHERE id=?""", (*vals, 1 if f.get("active") else 0, sid))
                flash("Service updated. Existing bookings keep their original prices.", "success")
            else:
                db.execute("""INSERT INTO services(name, description, base_price, duration_min, interval_km, interval_months)
                              VALUES (?,?,?,?,?,?)""", vals)
                flash("Service added to the catalogue.", "success")
            return redirect(url_for("staff.services"))
        edit = sid
    rows = db.execute("""SELECT s.*, (SELECT COUNT(*) FROM booking_items WHERE service_id=s.id) used
                         FROM services s ORDER BY active DESC, name""").fetchall()
    cur = db.execute("SELECT * FROM services WHERE id=?", (edit,)).fetchone() if edit else None
    return render_template("s_services.html", rows=rows, cur=cur)


# ------------------------------------------------------------------ team (admin)
@bp.route("/team", methods=["GET", "POST"])
@staff_required("admin")
def team():
    db = get_db()
    if request.method == "POST":
        f = request.form
        username = (f.get("username") or "").strip().lower()
        name = " ".join((f.get("full_name") or "").split())
        role, pw = f.get("role"), f.get("password") or ""
        if not (3 <= len(username) <= 20) or not username.isalnum() or len(name) < 2 or role not in core.ROLES or len(pw) < 6:
            flash("Username (3-20 letters/numbers), name, role and a password of at least 6 characters are required.", "error")
        elif db.execute("SELECT 1 FROM staff WHERE username=?", (username,)).fetchone():
            flash("That username is taken.", "error")
        else:
            db.execute("INSERT INTO staff(username, full_name, role, password_hash, created_at) VALUES (?,?,?,?,?)",
                       (username, name, role, core.hash_password(pw), core.ts()))
            flash(f"{name} added as {role}.", "success")
            return redirect(url_for("staff.team"))
    rows = db.execute("""SELECT s.*, (SELECT COUNT(*) FROM bookings WHERE technician_id=s.id AND status='Delivered') jobs
                         FROM staff s ORDER BY active DESC, role, full_name""").fetchall()
    return render_template("s_team.html", rows=rows)


@bp.route("/team/<int:sid>/toggle", methods=["POST"])
@staff_required("admin")
def team_toggle(sid):
    db = get_db()
    if sid == current_staff()["id"]:
        flash("You can't deactivate your own account.", "error")
    else:
        db.execute("UPDATE staff SET active=1-active WHERE id=?", (sid,))
        flash("Account updated.", "success")
    return redirect(url_for("staff.team"))


@bp.route("/team/<int:sid>/reset", methods=["POST"])
@staff_required("admin")
def team_reset(sid):
    db = get_db()
    s = db.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    pw = secrets.token_urlsafe(6)
    db.execute("UPDATE staff SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE id=?", (core.hash_password(pw), sid))
    flash(f"New password for {s['username']}: {pw} (share it securely; it won't be shown again).", "success")
    return redirect(url_for("staff.team"))


# ------------------------------------------------------------------ company reports
def _company_args():
    kind = request.args.get("p", "month")
    if kind not in reports.KINDS:
        kind = "month"
    return kind, reports.parse_anchor(request.args.get("d"))


@bp.route("/reports")
@staff_required(*ADV)
def report():
    kind, anchor = _company_args()
    return render_template("s_reports.html", r=reports.company_report(get_db(), kind, anchor), kind=kind)


@bp.route("/reports.pdf")
@staff_required(*ADV)
def report_pdf():
    kind, anchor = _company_args()
    r = reports.company_report(get_db(), kind, anchor)
    return send_file(pdfs.company_report_pdf(r), mimetype="application/pdf",
                     download_name=f"business_report_{kind}_{r['start']:%Y%m%d}.pdf")


@bp.route("/reports.csv")
@staff_required(*ADV)
def report_csv():
    kind, anchor = _company_args()
    r = reports.company_report(get_db(), kind, anchor)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Booking ID", "Date", "Slot", "Customer", "Phone", "Vehicle", "Model", "Services", "Status", "Payment",
                "Method", "Total (INR)", "Rating"])
    for b in r["bookings"]:
        w.writerow([b["ref"], b["booking_date"], b["time_slot"], b["cname"], b["cphone"], b["reg_no"],
                    f"{b['make'] or ''} {b['model']}".strip(), b["services"], b["status"], b["payment_status"],
                    b["payment_method"] or "", f"{b['total']:.2f}", b["rating"] or ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=bookings_{kind}_{r['start']:%Y%m%d}.csv"})


# ------------------------------------------------------------------ profile
@bp.route("/profile", methods=["GET", "POST"])
@staff_required()
def profile():
    db, me = get_db(), current_staff()
    if request.method == "POST":
        cur, new, again = request.form.get("current", ""), request.form.get("new", ""), request.form.get("again", "")
        if not core.verify_password(me["password_hash"], cur):
            flash("Your current password is incorrect.", "error")
        elif len(new) < 6 or new != again:
            flash("The new password must be at least 6 characters and match the confirmation.", "error")
        else:
            db.execute("UPDATE staff SET password_hash=? WHERE id=?", (core.hash_password(new), me["id"]))
            flash("Password changed.", "success")
            return redirect(url_for("staff.profile"))
    stats = db.execute("""SELECT COUNT(*) jobs, COALESCE(SUM(total),0) rev FROM bookings
                          WHERE technician_id=? AND status='Delivered'""", (me["id"],)).fetchone()
    return render_template("s_profile.html", stats=stats)
