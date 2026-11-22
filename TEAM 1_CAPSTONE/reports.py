"""Report calculations (weekly / monthly / yearly) for the company and for individual customers."""
import calendar
from datetime import date, timedelta

import core

KINDS = ("week", "month", "year")


def parse_anchor(s):
    return core.parse_date(s) or core.today()


def period(kind, anchor):
    """Return start/end/label, prev/next anchors and chart buckets for a period."""
    if kind == "week":
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=6)
        label = f"{start:%d %b} – {end:%d %b %Y}"
        prev, nxt = start - timedelta(days=7), start + timedelta(days=7)
        buckets = [(start + timedelta(days=i), start + timedelta(days=i), f"{start + timedelta(days=i):%a %d}")
                   for i in range(7)]
    elif kind == "year":
        start, end = date(anchor.year, 1, 1), date(anchor.year, 12, 31)
        label = str(anchor.year)
        prev, nxt = date(anchor.year - 1, 1, 1), date(anchor.year + 1, 1, 1)
        buckets = [(date(anchor.year, m, 1), date(anchor.year, m, calendar.monthrange(anchor.year, m)[1]),
                    calendar.month_abbr[m]) for m in range(1, 13)]
    elif kind == "all":
        end = core.today()
        start = date(2000, 1, 1)
        label = "All time"
        prev = nxt = None
        buckets = []
        y, m = end.year, end.month
        months = []
        for _ in range(12):
            months.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        for y, m in reversed(months):
            buckets.append((date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1]), f"{calendar.month_abbr[m]} {str(y)[2:]}"))
    else:  # month
        kind = "month"
        start = anchor.replace(day=1)
        end = anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])
        label = f"{start:%B %Y}"
        prev = (start - timedelta(days=1)).replace(day=1)
        nxt = end + timedelta(days=1)
        buckets = [(start + timedelta(days=i), start + timedelta(days=i), str(i + 1))
                   for i in range((end - start).days + 1)]
    if nxt and nxt > core.today():
        nxt = None
    return dict(kind=kind, start=start, end=end, label=label, prev=prev, next=nxt, buckets=buckets)


def _series(db, buckets, s, e, where="1=1", params=()):
    rows = db.execute(
        f"""SELECT booking_date d, COUNT(*) n,
                   COALESCE(SUM(CASE WHEN status='Delivered' THEN total END),0) rev
            FROM bookings WHERE {where} AND status!='Cancelled' AND booking_date BETWEEN ? AND ?
            GROUP BY booking_date""", (*params, s.isoformat(), e.isoformat())).fetchall()
    per = {r["d"]: (r["n"], r["rev"]) for r in rows}
    labels, counts, revs = [], [], []
    for bs, be, lab in buckets:
        n, rev, d = 0, 0.0, bs
        while d <= be:
            x = per.get(d.isoformat())
            if x:
                n += x[0]
                rev += x[1]
            d += timedelta(days=1)
        labels.append(lab)
        counts.append(n)
        revs.append(round(rev, 2))
    return dict(labels=labels, bookings=counts, revenue=revs)


def _kpis(db, s, e, where="1=1", params=()):
    r = db.execute(
        f"""SELECT COUNT(*) total,
                   COALESCE(SUM(status='Delivered'),0) delivered,
                   COALESCE(SUM(status='Cancelled'),0) cancelled,
                   COALESCE(SUM(CASE WHEN status='Delivered' THEN total END),0) revenue,
                   COALESCE(SUM(CASE WHEN status='Delivered' AND payment_status='Paid' THEN total END),0) collected,
                   COALESCE(SUM(CASE WHEN status='Delivered' THEN tax END),0) gst,
                   COALESCE(SUM(CASE WHEN status='Delivered' THEN discount END),0) discounts,
                   AVG(rating) rating, COUNT(rating) rated
            FROM bookings WHERE {where} AND booking_date BETWEEN ? AND ?""",
        (*params, s.isoformat(), e.isoformat())).fetchone()
    k = dict(r)
    k["bookings"] = k["total"] - k["cancelled"]
    k["pending"] = round(k["revenue"] - k["collected"], 2)
    k["avg_ticket"] = round(k["revenue"] / k["delivered"], 2) if k["delivered"] else 0
    k["rating"] = round(k["rating"], 2) if k["rating"] else None
    k["cancel_rate"] = round(k["cancelled"] * 100 / k["total"]) if k["total"] else 0
    return k


def _delta(cur, prev):
    if not prev:
        return None
    return round((cur - prev) * 100 / prev)


BOOKING_ROWS = """SELECT b.id, b.ref, b.booking_date, b.time_slot, b.status, b.total, b.payment_status, b.payment_method,
                         b.rating, c.name cname, c.phone cphone, v.reg_no, v.make, v.model,
                         (SELECT GROUP_CONCAT(name, ', ') FROM booking_items WHERE booking_id=b.id) services
                  FROM bookings b JOIN customers c ON c.id=b.customer_id JOIN vehicles v ON v.id=b.vehicle_id"""


def company_report(db, kind, anchor):
    p = period(kind, anchor)
    s, e = p["start"], p["end"]
    # previous period of the same length for comparison
    span = (e - s).days + 1
    if p["kind"] == "month":
        ps = (s - timedelta(days=1)).replace(day=1)
        pe = s - timedelta(days=1)
    elif p["kind"] == "year":
        ps, pe = date(s.year - 1, 1, 1), date(s.year - 1, 12, 31)
    else:
        ps, pe = s - timedelta(days=span), s - timedelta(days=1)
    k, pk = _kpis(db, s, e), _kpis(db, ps, pe)
    k["new_customers"] = db.execute("SELECT COUNT(*) FROM customers WHERE substr(created_at,1,10) BETWEEN ? AND ?",
                                    (s.isoformat(), e.isoformat())).fetchone()[0]
    rng = (s.isoformat(), e.isoformat())
    return dict(
        p=p, kind=p["kind"], label=p["label"], start=s, end=e, kpi=k, prev_kpi=pk,
        delta=dict(bookings=_delta(k["bookings"], pk["bookings"]), revenue=_delta(k["revenue"], pk["revenue"]),
                   delivered=_delta(k["delivered"], pk["delivered"]),
                   avg_ticket=_delta(k["avg_ticket"], pk["avg_ticket"])),
        series=_series(db, p["buckets"], s, e),
        status=[dict(r) for r in db.execute(
            "SELECT status, COUNT(*) n FROM bookings WHERE booking_date BETWEEN ? AND ? GROUP BY status ORDER BY n DESC", rng)],
        services=[dict(r) for r in db.execute(
            """SELECT bi.name, SUM(bi.qty) n, SUM(bi.price*bi.qty) rev FROM booking_items bi
               JOIN bookings b ON b.id=bi.booking_id WHERE b.booking_date BETWEEN ? AND ? AND b.status='Delivered'
               GROUP BY bi.name ORDER BY rev DESC LIMIT 8""", rng)],
        techs=[dict(r) for r in db.execute(
            """SELECT s.full_name, COUNT(b.id) jobs, COALESCE(SUM(b.total),0) rev, AVG(b.rating) rating
               FROM bookings b JOIN staff s ON s.id=b.technician_id
               WHERE b.booking_date BETWEEN ? AND ? AND b.status='Delivered' GROUP BY s.id ORDER BY jobs DESC""", rng)],
        customers=[dict(r) for r in db.execute(
            """SELECT c.id, c.name, c.phone, COUNT(*) n, SUM(b.total) spend FROM bookings b
               JOIN customers c ON c.id=b.customer_id WHERE b.booking_date BETWEEN ? AND ? AND b.status='Delivered'
               GROUP BY c.id ORDER BY spend DESC LIMIT 6""", rng)],
        payments=[dict(r) for r in db.execute(
            """SELECT COALESCE(payment_method, 'Unpaid') method, COUNT(*) n, SUM(total) amt FROM bookings
               WHERE booking_date BETWEEN ? AND ? AND status='Delivered' GROUP BY method ORDER BY amt DESC""", rng)],
        bookings=[dict(r) for r in db.execute(
            BOOKING_ROWS + " WHERE b.booking_date BETWEEN ? AND ? ORDER BY b.booking_date DESC, b.time_slot", rng)],
    )


def customer_report(db, cid, kind, anchor):
    p = period(kind, anchor)
    s, e = p["start"], p["end"]
    rng = (cid, s.isoformat(), e.isoformat())
    k = _kpis(db, s, e, "customer_id=?", (cid,))
    visits = db.execute("""SELECT COUNT(*) FROM bookings WHERE customer_id=? AND booking_date BETWEEN ? AND ?
                           AND status!='Cancelled'""", rng).fetchone()[0]
    k["visits"] = visits
    return dict(
        p=p, kind=p["kind"], label=p["label"], start=s, end=e, kpi=k,
        loyalty=core.loyalty(db, cid),
        series=_series(db, p["buckets"], s, e, "customer_id=?", (cid,)),
        by_vehicle=[dict(r) for r in db.execute(
            """SELECT v.reg_no, v.make, v.model, COUNT(b.id) n, COALESCE(SUM(b.total),0) spend
               FROM bookings b JOIN vehicles v ON v.id=b.vehicle_id
               WHERE b.customer_id=? AND b.booking_date BETWEEN ? AND ? AND b.status='Delivered'
               GROUP BY v.id ORDER BY spend DESC""", rng)],
        by_service=[dict(r) for r in db.execute(
            """SELECT bi.name, SUM(bi.qty) n, SUM(bi.price*bi.qty) rev FROM booking_items bi
               JOIN bookings b ON b.id=bi.booking_id
               WHERE b.customer_id=? AND b.booking_date BETWEEN ? AND ? AND b.status='Delivered'
               GROUP BY bi.name ORDER BY rev DESC""", rng)],
        bookings=[dict(r) for r in db.execute(
            BOOKING_ROWS + " WHERE b.customer_id=? AND b.booking_date BETWEEN ? AND ? ORDER BY b.booking_date DESC, b.time_slot",
            rng)],
        vehicles=core.vehicle_overview(db, customer_id=cid),
    )
