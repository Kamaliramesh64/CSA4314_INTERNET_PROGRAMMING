"""AutoCare: Smart Vehicle Service Booking and Maintenance Tracking System (Flask + SQLite)."""
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from flask import (Flask, flash, jsonify, redirect, render_template, request, send_file, session, url_for)

import core
import db as dbm
from db import get_db
from icons import icon

BASE = Path(__file__).parent


def _secret():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = BASE / ".secret_key"
    try:
        if f.exists():
            return f.read_text().strip()
        key = secrets.token_hex(32)
        f.write_text(key)
        return key
    except OSError:
        return "autocare-fallback-key"


app = Flask(__name__)
app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=1 << 20)
app.teardown_appcontext(dbm.close_db)
app.before_request(core.csrf_protect)
app.after_request(dbm.commit_or_rollback)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.endpoint not in ("static", "service_worker") and "text/html" in resp.content_type:
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ template helpers
def static_v(filename):
    p = BASE / "static" / filename
    v = int(p.stat().st_mtime) if p.exists() else 0
    return f"{url_for('static', filename=filename)}?v={v}"


def _d(s, fmt):
    d = core.parse_date(s)
    return d.strftime(fmt) if d else (s or "")


app.jinja_env.globals.update(
    icon=icon, csrf_token=core.csrf_token, static_v=static_v, STAGES=core.STAGES, STAGE_ICON=core.STAGE_ICON,
    STAGE_TEXT=core.STAGE_TEXT, SLOTS=core.SLOTS, PAY_METHODS=core.PAY_METHODS, ALL_STATUSES=core.ALL_STATUSES,
    TIERS=core.TIERS, SHOW_DEMO=core.SHOW_DEMO_OTP)
app.jinja_env.filters.update(
    money=core.inr,
    money2=lambda v: core.inr(v, 2),
    dmy=lambda s: _d(s, "%d %b %Y"),
    dm=lambda s: _d(s, "%d %b"),
    weekday=lambda s: _d(s, "%a, %d %b"),
    dt=lambda s: core.parse_ts(s).strftime("%d %b, %I:%M %p") if core.parse_ts(s) else (s or ""),
    tm=lambda s: core.parse_ts(s).strftime("%I:%M %p") if core.parse_ts(s) else (s or ""),
    initials=lambda n: "".join(w[0] for w in (n or "?").split()[:2]).upper(),
    mask=core.mask_phone,
    slug=lambda s: re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-"),
)


@app.context_processor
def inject():
    me, st = core.current_customer(), core.current_staff()
    unread = 0
    if me:
        unread = get_db().execute("SELECT COUNT(*) FROM notifications WHERE customer_id=? AND is_read=0",
                                  (me["id"],)).fetchone()[0]
    return dict(me=me, staff_user=st, unread=unread, today=core.today(), endpoint=request.endpoint or "")


# ------------------------------------------------------------------ public pages
@app.route("/")
def landing():
    if core.current_customer():
        return redirect(url_for("cust.dashboard"))
    if core.current_staff():
        return redirect(url_for("staff.dashboard"))
    db = get_db()
    stats = dict(
        services=db.execute("SELECT COUNT(*) FROM bookings WHERE status='Delivered'").fetchone()[0],
        customers=db.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        rating=db.execute("SELECT AVG(rating) FROM bookings WHERE rating IS NOT NULL").fetchone()[0])
    return render_template("landing.html", stats=stats)


@app.route("/track", methods=["GET", "POST"])
def track():
    """Public tracking: Booking ID + registered phone number, no login needed."""
    result = None
    form = request.form if request.method == "POST" else {"ref": request.args.get("ref", "")}
    if request.method == "POST":
        db = get_db()
        ref = (request.form.get("ref") or "").strip().upper()
        phone = core.norm_phone(request.form.get("phone"))
        b = db.execute("""SELECT b.*, v.reg_no, v.make, v.model, c.phone cphone, c.name cname FROM bookings b
                          JOIN vehicles v ON v.id=b.vehicle_id JOIN customers c ON c.id=b.customer_id
                          WHERE b.ref=?""", (ref,)).fetchone()
        if b and phone and b["cphone"] == phone:
            result = dict(b=b, times=core.track_times(db, b["id"]),
                          items=db.execute("SELECT * FROM booking_items WHERE booking_id=?", (b["id"],)).fetchall())
        else:
            flash("No booking found for that Booking ID and mobile number.", "error")
    return render_template("track.html", result=result, form=form)


@app.route("/api/slots")
def api_slots():
    return jsonify(core.slot_availability(get_db(), request.args.get("date", ""), request.args.get("exclude", type=int)))


@app.route("/manifest.webmanifest")
def manifest():
    return send_file(BASE / "static" / "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    resp = send_file(BASE / "static" / "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/offline")
def offline():
    return render_template("offline.html")


# ------------------------------------------------------------------ customer authentication (phone + OTP)
def _start_otp(db, phone, purpose):
    code, err, kind = core.issue_otp(db, phone, purpose)
    if err and kind != "cooldown":
        flash(err, "error")
        return redirect(url_for("login" if purpose == "login" else "register"))
    if err:
        flash(err, "error")
    session["otp_phone"], session["otp_purpose"] = phone, purpose
    if code and core.SHOW_DEMO_OTP:
        session["demo_otp"] = code
    elif code:
        session.pop("demo_otp", None)
    session["otp_sent"] = core.ts()
    return redirect(url_for("verify"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if core.current_customer():
        return redirect(url_for("cust.dashboard"))
    if request.method == "POST":
        phone = core.norm_phone(request.form.get("phone"))
        if not phone:
            flash("Enter a valid 10-digit mobile number.", "error")
            return render_template("login.html", form=request.form)
        db = get_db()
        if not db.execute("SELECT 1 FROM customers WHERE phone=?", (phone,)).fetchone():
            flash("This number isn't registered yet. Create an account to continue.", "error")
            return redirect(url_for("register", phone=phone))
        nxt = core.safe_next(request.args.get("next"))
        if nxt:
            session["next"] = nxt
        return _start_otp(db, phone, "login")
    return render_template("login.html", form={})


@app.route("/register", methods=["GET", "POST"])
def register():
    if core.current_customer():
        return redirect(url_for("cust.dashboard"))
    if request.method != "POST":
        return render_template("register.html", form={"phone": request.args.get("phone", "")})
    db = get_db()
    f = request.form
    name = " ".join((f.get("name") or "").split())
    phone = core.norm_phone(f.get("phone"))
    email = (f.get("email") or "").strip().lower()
    reg = core.norm_reg(f.get("reg_no"))
    model = " ".join((f.get("model") or "").split())
    make = " ".join((f.get("make") or "").split())
    odo = core.to_int(f.get("odometer"), 0, lo=0, hi=2_000_000)
    errors = []
    if len(name) < 2:
        errors.append("Enter your full name.")
    if not phone:
        errors.append("Enter a valid 10-digit mobile number (starting with 6-9).")
    if email and not core.EMAIL_RE.match(email):
        errors.append("Enter a valid email address or leave it blank.")
    if not reg:
        errors.append("Enter a valid vehicle registration number, e.g. TN45AB1234.")
    if not model:
        errors.append("Enter your vehicle model.")
    if not f.get("terms"):
        errors.append("Please accept the terms and consent to receive service updates.")
    if phone and db.execute("SELECT 1 FROM customers WHERE phone=?", (phone,)).fetchone():
        errors.append("This mobile number is already registered. Please log in instead.")
    if reg and db.execute("SELECT 1 FROM vehicles WHERE reg_no=?", (reg,)).fetchone():
        errors.append("This vehicle is already registered with another account. Please contact the workshop.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("register.html", form=f)
    session["pending_reg"] = dict(name=name, phone=phone, email=email, reg=reg, model=model, make=make,
                                  odometer=odo, year=core.to_int(f.get("year"), None, 1980, 2100), fuel=f.get("fuel"))
    return _start_otp(db, phone, "register")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    phone, purpose = session.get("otp_phone"), session.get("otp_purpose")
    if not phone:
        return redirect(url_for("login"))
    db = get_db()
    if request.method == "POST":
        ok, msg = core.check_otp(db, phone, (request.form.get("otp") or "").strip())
        if not ok:
            db.commit()  # persist the attempt counter
            flash(msg, "error")
            return render_template("verify.html", phone=phone, purpose=purpose, demo_otp=session.get("demo_otp"))
        if purpose == "register":
            pr = session.get("pending_reg")
            if not pr or pr["phone"] != phone:
                flash("Your sign-up session expired. Please start again.", "error")
                return redirect(url_for("register"))
            n = core.ts()
            cur = db.execute("INSERT INTO customers(name, phone, email, phone_verified, created_at) VALUES (?,?,?,1,?)",
                             (pr["name"], phone, pr["email"] or None, n))
            cid = cur.lastrowid
            db.execute("""INSERT INTO vehicles(customer_id, reg_no, make, model, year, fuel, odometer, created_at)
                          VALUES (?,?,?,?,?,?,?,?)""",
                       (cid, pr["reg"], pr["make"], pr["model"], pr["year"], pr["fuel"], pr["odometer"], n))
            core.notify(db, cid, "Welcome to AutoCare",
                        "Your account is ready. Book your first service whenever you like.", "/app/book")
        else:
            row = db.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
            cid = row["id"]
        nxt = core.safe_next(session.get("next"))
        core.login_customer(cid)
        flash("You're logged in. Welcome!", "success")
        return redirect(nxt or url_for("cust.dashboard"))
    return render_template("verify.html", phone=phone, purpose=purpose, demo_otp=session.get("demo_otp"))


@app.route("/verify/resend", methods=["POST"])
def resend_otp():
    phone, purpose = session.get("otp_phone"), session.get("otp_purpose")
    if not phone:
        return redirect(url_for("login"))
    return _start_otp(get_db(), phone, purpose)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("landing"))


# ------------------------------------------------------------------ staff authentication
@app.route("/staff/login", methods=["GET", "POST"])
def staff_login():
    if core.current_staff():
        return redirect(url_for("staff.dashboard"))
    if request.method == "POST":
        db = get_db()
        username = (request.form.get("username") or "").strip().lower()
        row = db.execute("SELECT * FROM staff WHERE lower(username)=? AND active=1", (username,)).fetchone()
        if row and row["locked_until"] and core.parse_ts(row["locked_until"]) > core.now():
            flash("This account is temporarily locked after too many failed attempts. Try again in a few minutes.", "error")
        elif row and core.verify_password(row["password_hash"], request.form.get("password") or ""):
            db.execute("UPDATE staff SET failed_attempts=0, locked_until=NULL WHERE id=?", (row["id"],))
            nxt = core.safe_next(request.args.get("next"))
            core.login_staff(row["id"])
            return redirect(nxt or url_for("staff.dashboard"))
        else:
            if row:
                fails = row["failed_attempts"] + 1
                lock = core.ts(core.now() + timedelta(minutes=10)) if fails >= 5 else None
                db.execute("UPDATE staff SET failed_attempts=?, locked_until=? WHERE id=?",
                           (0 if lock else fails, lock, row["id"]))
                db.commit()
            flash("Invalid username or password.", "error")
    return render_template("staff_login.html")


@app.route("/staff/logout", methods=["POST"])
def staff_logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("staff_login"))


# ------------------------------------------------------------------ errors
@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def handle_error(e):
    code = getattr(e, "code", 500)
    msgs = {400: ("Something went wrong", getattr(e, "description", "Bad request.")),
            403: ("Access denied", "You don't have permission to open this page."),
            404: ("Page not found", "The page you're looking for doesn't exist or was moved."),
            500: ("Server error", "Something broke on our side. Please try again.")}
    title, text = msgs.get(code, msgs[500])
    return render_template("error.html", code=code, title=title, text=text), code


from customer import bp as customer_bp  # noqa: E402
from staff import bp as staff_bp  # noqa: E402

app.register_blueprint(customer_bp)
app.register_blueprint(staff_bp)


def bootstrap():
    import seed
    conn = dbm.init_db()
    seed.ensure_seeded(conn)
    conn.close()


bootstrap()

if __name__ == "__main__":
    app.run(debug=True, port=5055)
