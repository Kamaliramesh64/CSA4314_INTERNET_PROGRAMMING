"""Customer portal routes (/app/...)."""
from datetime import timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

import core
import pdfs
import reports
from core import customer_required, current_customer
from db import get_db

bp = Blueprint("cust", __name__, url_prefix="/app")


def _my_booking(db, bid):
    b = db.execute("""SELECT b.*, v.reg_no, v.make, v.model, v.odometer vodo, s.full_name tech
                      FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
                      LEFT JOIN staff s ON s.id=b.technician_id
                      WHERE b.id=? AND b.customer_id=?""", (bid, current_customer()["id"])).fetchone()
    if not b:
        abort(404)
    return dict(b)


def _my_vehicle(db, vid):
    v = db.execute("SELECT * FROM vehicles WHERE id=? AND customer_id=?", (vid, current_customer()["id"])).fetchone()
    if not v:
        abort(404)
    return v


@bp.route("")
@customer_required
def dashboard():
    db, me = get_db(), current_customer()
    cid = me["id"]
    hour = core.now().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
    vehicles = core.vehicle_overview(db, cid)
    active = []
    for r in db.execute("""SELECT b.*, v.reg_no, v.make, v.model FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
                           WHERE b.customer_id=? AND b.status NOT IN ('Delivered','Cancelled')
                           ORDER BY b.booking_date, b.time_slot""", (cid,)):
        d = dict(r)
        d["times"] = core.track_times(db, d["id"])
        d["pct"] = int(core.STAGES.index(d["status"]) / (len(core.STAGES) - 1) * 100)
        d["items"] = [i["name"] for i in db.execute("SELECT name FROM booking_items WHERE booking_id=?", (d["id"],))]
        active.append(d)
    nxt = next((a for a in active if a["status"] == "Booked"), None)
    nxt_days = (core.parse_date(nxt["booking_date"]) - core.today()).days if nxt else None
    recent = db.execute("""SELECT b.*, v.reg_no FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
                           WHERE b.customer_id=? ORDER BY b.booking_date DESC, b.id DESC LIMIT 5""", (cid,)).fetchall()
    # last 6 months spend
    p = reports.period("all", core.today())
    series = reports._series(db, p["buckets"][-6:], p["buckets"][-6][0], p["end"], "customer_id=?", (cid,))
    stats = db.execute("""SELECT COUNT(*) n, COALESCE(SUM(total),0) spend FROM bookings
                          WHERE customer_id=? AND status='Delivered'""", (cid,)).fetchone()
    notes = db.execute("SELECT * FROM notifications WHERE customer_id=? ORDER BY id DESC LIMIT 5", (cid,)).fetchall()
    return render_template("c_dashboard.html", greeting=greeting, vehicles=vehicles, active=active, nxt=nxt,
                           nxt_days=nxt_days, recent=recent, series=series, lo=core.loyalty(db, cid),
                           stats=stats, notes=notes,
                           due_count=sum(1 for v in vehicles if v["due"]["state"] in ("soon", "overdue")))


# ------------------------------------------------------------------ vehicles
@bp.route("/vehicles")
@customer_required
def vehicles():
    return render_template("c_vehicles.html", vehicles=core.vehicle_overview(get_db(), current_customer()["id"]))


def _vehicle_form(v=None):
    db, me = get_db(), current_customer()
    if request.method == "POST":
        f = request.form
        reg = core.norm_reg(f.get("reg_no"))
        model = " ".join((f.get("model") or "").split())
        errors = []
        if not reg:
            errors.append("Enter a valid registration number, e.g. TN45AB1234.")
        if not model:
            errors.append("Enter the vehicle model.")
        odo = core.to_int(f.get("odometer"), None, 0, 2_000_000)
        if odo is None:
            errors.append("Enter the current odometer reading in km.")
        if reg and db.execute("SELECT 1 FROM vehicles WHERE reg_no=? AND id!=?", (reg, v["id"] if v else 0)).fetchone():
            errors.append("This registration number is already registered.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("c_vehicle_form.html", v=v, form=f)
        vals = (reg, (f.get("make") or "").strip(), model, core.to_int(f.get("year"), None, 1980, 2100),
                f.get("fuel") or None, odo)
        if v:
            db.execute("UPDATE vehicles SET reg_no=?, make=?, model=?, year=?, fuel=?, odometer=? WHERE id=?", (*vals, v["id"]))
            flash("Vehicle updated.", "success")
            return redirect(url_for("cust.vehicle", vid=v["id"]))
        cur = db.execute("""INSERT INTO vehicles(reg_no, make, model, year, fuel, odometer, customer_id, created_at)
                            VALUES (?,?,?,?,?,?,?,?)""", (*vals, me["id"], core.ts()))
        flash("Vehicle added to your garage.", "success")
        return redirect(url_for("cust.vehicle", vid=cur.lastrowid))
    return render_template("c_vehicle_form.html", v=v, form=dict(v) if v else {})


@bp.route("/vehicles/add", methods=["GET", "POST"])
@customer_required
def vehicle_add():
    return _vehicle_form()


@bp.route("/vehicles/<int:vid>/edit", methods=["GET", "POST"])
@customer_required
def vehicle_edit(vid):
    return _vehicle_form(_my_vehicle(get_db(), vid))


@bp.route("/vehicles/<int:vid>")
@customer_required
def vehicle(vid):
    db = get_db()
    _my_vehicle(db, vid)
    v = core.vehicle_overview(db, vehicle_id=vid)[0]
    history = db.execute("SELECT * FROM maintenance WHERE vehicle_id=? ORDER BY service_date DESC, id DESC", (vid,)).fetchall()
    chrono = list(reversed(history))
    bookings = db.execute("SELECT * FROM bookings WHERE vehicle_id=? ORDER BY booking_date DESC LIMIT 8", (vid,)).fetchall()
    chart = dict(labels=[core.parse_date(h["service_date"]).strftime("%d %b %y") for h in chrono],
                 cost=[h["cost"] for h in chrono], odo=[h["odometer"] for h in chrono])
    return render_template("c_vehicle.html", v=v, history=history, bookings=bookings, chart=chart)


@bp.route("/vehicles/<int:vid>/record", methods=["POST"])
@customer_required
def vehicle_record(vid):
    db = get_db()
    _my_vehicle(db, vid)
    f = request.form
    d = core.parse_date(f.get("service_date"))
    odo = core.to_int(f.get("odometer"), None, 0, 2_000_000)
    cost = core.to_float(f.get("cost"), 0, 0)
    ikm = core.to_int(f.get("interval_km"), None, 1, 200000)
    imo = core.to_int(f.get("interval_months"), None, 1, 60)
    names = " ".join((f.get("services") or "").split())
    if not d or d > core.today() or odo is None or not names or not ikm or not imo:
        flash("Check the date (not in the future), odometer, services and service interval.", "error")
        return redirect(url_for("cust.vehicle", vid=vid))
    db.execute("""INSERT INTO maintenance(vehicle_id, service_date, odometer, services, cost, next_due_date, next_due_km, source)
                  VALUES (?,?,?,?,?,?,?, 'customer')""",
               (vid, d.isoformat(), odo, names, cost, core.add_months(d, imo).isoformat(), odo + ikm))
    db.execute("UPDATE vehicles SET odometer=MAX(odometer, ?) WHERE id=?", (odo, vid))
    flash("Past service added. Your next due date is updated.", "success")
    return redirect(url_for("cust.vehicle", vid=vid))


@bp.route("/vehicles/<int:vid>/delete", methods=["POST"])
@customer_required
def vehicle_delete(vid):
    db = get_db()
    _my_vehicle(db, vid)
    if db.execute("SELECT 1 FROM bookings WHERE vehicle_id=?", (vid,)).fetchone():
        flash("This vehicle has bookings, so it can't be removed.", "error")
        return redirect(url_for("cust.vehicle", vid=vid))
    db.execute("DELETE FROM vehicles WHERE id=?", (vid,))
    flash("Vehicle removed.", "success")
    return redirect(url_for("cust.vehicles"))


# ------------------------------------------------------------------ bookings
@bp.route("/book", methods=["GET", "POST"])
@customer_required
def book():
    db, me = get_db(), current_customer()
    vehicles = db.execute("SELECT * FROM vehicles WHERE customer_id=? ORDER BY id", (me["id"],)).fetchall()
    if not vehicles:
        flash("Add a vehicle to your garage before booking.", "error")
        return redirect(url_for("cust.vehicle_add"))
    services = db.execute("SELECT * FROM services WHERE active=1 ORDER BY base_price DESC").fetchall()
    lo = core.loyalty(db, me["id"])
    form = request.form if request.method == "POST" else {
        "vehicle_id": request.args.get("vehicle", ""), "service": request.args.get("service", "")}
    if request.method == "POST":
        f = request.form
        vid = core.to_int(f.get("vehicle_id"))
        veh = next((v for v in vehicles if v["id"] == vid), None)
        valid_ids = {s["id"] for s in services}
        sids = [i for i in (core.to_int(x) for x in f.getlist("services")) if i in valid_ids]
        date_s, slot = f.get("booking_date", ""), f.get("time_slot", "")
        errors = []
        if not veh:
            errors.append("Select one of your vehicles.")
        if not sids:
            errors.append("Select at least one service.")
        if veh:
            errors += core.validate_slot(db, date_s, slot, veh["id"])
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            odo = core.to_int(f.get("odometer"), veh["odometer"], 0, 2_000_000)
            bid = core.create_booking(db, me["id"], veh["id"], sids, date_s, slot, odo, f.get("pickup") == "1",
                                      (f.get("notes") or "").strip()[:300])
            flash("Booking confirmed! You can track it live from here.", "success")
            return redirect(url_for("cust.booking", bid=bid))
    selected = set(request.form.getlist("services")) if request.method == "POST" else {
        request.args.get("service", "")}
    return render_template("c_book.html", vehicles=vehicles, services=services, lo=lo, form=form,
                           selected=selected, max_date=(core.today() + timedelta(days=core.BOOKING_HORIZON_DAYS)).isoformat())


@bp.route("/bookings")
@customer_required
def bookings():
    db, cid = get_db(), current_customer()["id"]
    tab = request.args.get("tab", "all")
    cond = {"active": "AND b.status NOT IN ('Delivered','Cancelled')", "done": "AND b.status='Delivered'",
            "cancelled": "AND b.status='Cancelled'"}.get(tab, "")
    rows = db.execute(f"""SELECT b.*, v.reg_no, v.make, v.model,
                          (SELECT GROUP_CONCAT(name, ', ') FROM booking_items WHERE booking_id=b.id) services
                          FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id WHERE b.customer_id=? {cond}
                          ORDER BY b.booking_date DESC, b.id DESC""", (cid,)).fetchall()
    return render_template("c_bookings.html", rows=rows, tab=tab)


@bp.route("/bookings/<int:bid>")
@customer_required
def booking(bid):
    db = get_db()
    b = _my_booking(db, bid)
    hist = db.execute("SELECT * FROM status_history WHERE booking_id=? ORDER BY id DESC", (bid,)).fetchall()
    items = db.execute("SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (bid,)).fetchall()
    return render_template("c_booking.html", b=b, hist=hist, items=items, times=core.track_times(db, bid),
                           pct=int(core.STAGES.index(b["status"]) / (len(core.STAGES) - 1) * 100)
                           if b["status"] in core.STAGES else 0)


@bp.route("/api/booking/<int:bid>/status")
@customer_required
def api_status(bid):
    b = _my_booking(get_db(), bid)
    return jsonify(status=b["status"], updated=b["updated_at"])


@bp.route("/bookings/<int:bid>/reschedule", methods=["GET", "POST"])
@customer_required
def reschedule(bid):
    db = get_db()
    b = _my_booking(db, bid)
    if b["status"] != "Booked":
        flash("Only bookings that haven't started can be rescheduled.", "error")
        return redirect(url_for("cust.booking", bid=bid))
    if request.method == "POST":
        date_s, slot = request.form.get("booking_date", ""), request.form.get("time_slot", "")
        errors = core.validate_slot(db, date_s, slot, b["vehicle_id"], exclude=bid)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db.execute("UPDATE bookings SET booking_date=?, time_slot=?, updated_at=? WHERE id=?", (date_s, slot, core.ts(), bid))
            core.add_history(db, bid, "Booked", f"Rescheduled to {date_s} at {slot}")
            flash("Booking rescheduled.", "success")
            return redirect(url_for("cust.booking", bid=bid))
    return render_template("c_reschedule.html", b=b, max_date=(core.today() + timedelta(days=core.BOOKING_HORIZON_DAYS)).isoformat())


@bp.route("/bookings/<int:bid>/cancel", methods=["POST"])
@customer_required
def cancel(bid):
    db = get_db()
    b = _my_booking(db, bid)
    if b["status"] != "Booked":
        flash("This booking is already in progress and can't be cancelled online. Please call the workshop.", "error")
    else:
        core.set_status(db, bid, "Cancelled", "Cancelled by customer")
        flash("Booking cancelled. The slot is free again.", "success")
    return redirect(url_for("cust.booking", bid=bid))


@bp.route("/bookings/<int:bid>/feedback", methods=["POST"])
@customer_required
def feedback(bid):
    db = get_db()
    b = _my_booking(db, bid)
    rating = core.to_int(request.form.get("rating"), None, 1, 5)
    if b["status"] != "Delivered" or b["rating"] or not rating:
        flash("Feedback can be given once, after delivery, with a 1-5 rating.", "error")
    else:
        db.execute("UPDATE bookings SET rating=?, feedback=? WHERE id=?",
                   (rating, (request.form.get("feedback") or "").strip()[:300] or None, bid))
        flash("Thanks for your feedback!", "success")
    return redirect(url_for("cust.booking", bid=bid))


@bp.route("/bookings/<int:bid>/invoice.pdf")
@customer_required
def invoice(bid):
    db = get_db()
    b = _my_booking(db, bid)
    return send_file(pdfs.invoice_pdf(db, bid), mimetype="application/pdf", download_name=f"{b['ref']}_invoice.pdf")


# ------------------------------------------------------------------ reports
def _report_args():
    kind = request.args.get("p", "month")
    if kind not in ("week", "month", "year", "all"):
        kind = "month"
    return kind, reports.parse_anchor(request.args.get("d"))


@bp.route("/reports")
@customer_required
def reports_page():
    db, me = get_db(), current_customer()
    kind, anchor = _report_args()
    return render_template("c_reports.html", r=reports.customer_report(db, me["id"], kind, anchor), kind=kind)


@bp.route("/reports.pdf")
@customer_required
def reports_pdf():
    db, me = get_db(), current_customer()
    kind, anchor = _report_args()
    r = reports.customer_report(db, me["id"], kind, anchor)
    return send_file(pdfs.customer_report_pdf(r, me), mimetype="application/pdf",
                     download_name=f"service_report_{kind}_{core.today():%Y%m%d}.pdf")


# ------------------------------------------------------------------ notifications & profile
@bp.route("/notifications")
@customer_required
def notifications():
    db, cid = get_db(), current_customer()["id"]
    rows = db.execute("SELECT * FROM notifications WHERE customer_id=? ORDER BY id DESC LIMIT 60", (cid,)).fetchall()
    db.execute("UPDATE notifications SET is_read=1 WHERE customer_id=?", (cid,))
    return render_template("c_notifications.html", rows=rows)


@bp.route("/profile", methods=["GET", "POST"])
@customer_required
def profile():
    db, me = get_db(), current_customer()
    if request.method == "POST":
        name = " ".join((request.form.get("name") or "").split())
        email = (request.form.get("email") or "").strip().lower()
        if len(name) < 2 or (email and not core.EMAIL_RE.match(email)):
            flash("Enter your name and a valid email (or leave email blank).", "error")
        else:
            db.execute("UPDATE customers SET name=?, email=?, address=? WHERE id=?",
                       (name, email or None, (request.form.get("address") or "").strip()[:200] or None, me["id"]))
            flash("Profile updated.", "success")
            return redirect(url_for("cust.profile"))
    return render_template("c_profile.html", lo=core.loyalty(db, me["id"]))
