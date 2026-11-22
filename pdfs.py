"""PDF generation (invoice, company report, customer report) with ReportLab."""
from io import BytesIO

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import core

BRAND = colors.HexColor("#0f766e")
INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#64748b")
LINE = colors.HexColor("#e2e8f0")
SOFT = colors.HexColor("#f1f5f9")

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Heading1"], fontSize=18, textColor=INK, spaceAfter=2)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontSize=11.5, textColor=BRAND, spaceBefore=10, spaceAfter=4)
P = ParagraphStyle("p", parent=_ss["Normal"], fontSize=9, textColor=INK, leading=12)
SM = ParagraphStyle("sm", parent=P, fontSize=8, textColor=MUTED)


def money(v):
    return core.inr(v).replace("₹", "Rs. ")


def _frame(title, subtitle):
    def draw(canvas, doc):
        canvas.saveState()
        w, h = A4
        canvas.setFillColor(BRAND)
        canvas.rect(0, h - 22 * mm, w, 22 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 15)
        canvas.drawString(15 * mm, h - 13 * mm, "AutoCare")
        canvas.setFont("Helvetica", 9)
        canvas.drawString(15 * mm, h - 18 * mm, "Smart Vehicle Service Centre")
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawRightString(w - 15 * mm, h - 13 * mm, title)
        canvas.setFont("Helvetica", 9)
        canvas.drawRightString(w - 15 * mm, h - 18 * mm, subtitle)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(15 * mm, 9 * mm, f"Generated {core.now():%d %b %Y, %H:%M}")
        canvas.drawRightString(w - 15 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()
    return draw


def _build(title, subtitle, story):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=30 * mm, bottomMargin=16 * mm, title=title, author="AutoCare")
    fr = _frame(title, subtitle)
    doc.build(story, onFirstPage=fr, onLaterPages=fr)
    buf.seek(0)
    return buf


def _table(data, widths, align_right=(), header=True, zebra=True):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LINEBELOW", (0, 0), (-1, -1), 0.3, LINE)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BRAND), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                  ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    if zebra and len(data) > 2:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]))
    for c in align_right:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _kpi_row(items):
    cells = [[Paragraph(f'<font size="7" color="#64748b">{l}</font><br/><font size="12"><b>{v}</b></font>', P)
              for l, v in items]]
    t = Table(cells, colWidths=[(180 * mm) / len(items)] * len(items))
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("TOPPADDING", (0, 0), (-1, -1), 7),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    return t


def _bars(labels, values, color=BRAND, w=180 * mm, h=125):
    d = Drawing(w, h)
    c = VerticalBarChart()
    c.x, c.y, c.width, c.height = 38, 22, w - 50, h - 34
    c.data = [list(values) or [0]]
    c.categoryAxis.categoryNames = list(labels)
    c.categoryAxis.labels.fontSize = 6 if len(labels) > 14 else 7
    c.valueAxis.labels.fontSize = 6.5
    c.valueAxis.valueMin = 0
    mx = max(values) if values and max(values) > 0 else 1
    c.valueAxis.valueMax = mx * 1.15
    c.valueAxis.labelTextFormat = lambda v: f"{v:,.0f}"
    c.bars[0].fillColor = color
    c.bars.strokeColor = None
    c.barWidth = 6
    c.groupSpacing = 4
    c.valueAxis.strokeColor = LINE
    c.categoryAxis.strokeColor = LINE
    d.add(c)
    return d


# ------------------------------------------------------------------ invoice
def invoice_pdf(db, bid):
    b = db.execute("""SELECT b.*, c.name cname, c.phone cphone, c.email cemail, v.reg_no, v.make, v.model, v.odometer vodo,
                             s.full_name tech
                      FROM bookings b JOIN customers c ON c.id=b.customer_id JOIN vehicles v ON v.id=b.vehicle_id
                      LEFT JOIN staff s ON s.id=b.technician_id WHERE b.id=?""", (bid,)).fetchone()
    items = db.execute("SELECT * FROM booking_items WHERE booking_id=? ORDER BY id", (bid,)).fetchall()
    title = "TAX INVOICE" if b["status"] == "Delivered" else "ESTIMATE"
    story = [Paragraph(f"{title} · {b['ref']}", H1),
             Paragraph(f"Date: {core.parse_date(b['booking_date']):%d %b %Y} &nbsp;|&nbsp; Slot: {b['time_slot']} "
                       f"&nbsp;|&nbsp; Status: <b>{b['status']}</b>", SM), Spacer(1, 8)]
    info = [[Paragraph(f"<b>BILLED TO</b><br/>{b['cname']}<br/>+91 {b['cphone']}<br/>{b['cemail'] or ''}", P),
             Paragraph(f"<b>VEHICLE</b><br/>{b['reg_no']}<br/>{(b['make'] or '')} {b['model']}<br/>"
                       f"Odometer: {(b['odometer_in'] or b['vodo'] or 0):,} km", P),
             Paragraph(f"<b>SERVICE DETAILS</b><br/>Technician: {b['tech'] or '-'}<br/>"
                       f"Payment: {b['payment_status']}{' (' + b['payment_method'] + ')' if b['payment_method'] else ''}<br/>"
                       f"Source: {b['source'].title()}", P)]]
    t = Table(info, colWidths=[60 * mm] * 3)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [t, Spacer(1, 10)]
    rows = [["#", "Description", "Qty", "Rate", "Amount"]]
    for i, it in enumerate(items, 1):
        rows.append([str(i), it["name"], str(it["qty"]), money(it["price"]), money(it["price"] * it["qty"])])
    story.append(_table(rows, [10 * mm, 95 * mm, 15 * mm, 30 * mm, 30 * mm], align_right=(2, 3, 4)))
    half = b["tax"] / 2
    tot = [["Subtotal", money(b["subtotal"])]]
    if b["discount"]:
        tot.append([f"Loyalty discount ({b['discount_pct']:.0f}%)", "- " + money(b["discount"])])
    tot += [[f"CGST ({core.GST * 50:.0f}%)", money(half)], [f"SGST ({core.GST * 50:.0f}%)", money(half)],
            ["TOTAL", money(b["total"])]]
    tt = Table(tot, colWidths=[110 * mm, 40 * mm], hAlign="RIGHT")
    tt.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                            ("LINEABOVE", (0, -1), (-1, -1), 0.8, INK), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                            ("FONTSIZE", (0, -1), (-1, -1), 11), ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [Spacer(1, 6), tt, Spacer(1, 18)]
    if b["status"] != "Delivered":
        story.append(Paragraph("This is an estimate. The final amount is confirmed when your vehicle is delivered.", SM))
    story.append(Paragraph("Thank you for choosing AutoCare. This is a computer generated document.", SM))
    return _build(title, b["ref"], story)


# ------------------------------------------------------------------ company report
def company_report_pdf(r):
    k = r["kpi"]
    story = [Paragraph(f"{r['kind'].title()}ly Business Report", H1),
             Paragraph(f"Period: <b>{r['label']}</b> ({r['start']:%d %b %Y} to {r['end']:%d %b %Y})", SM), Spacer(1, 8),
             _kpi_row([("Bookings", k["bookings"]), ("Delivered", k["delivered"]), ("Cancelled", k["cancelled"]),
                       ("Revenue", money(k["revenue"])), ("Collected", money(k["collected"]))]), Spacer(1, 4),
             _kpi_row([("Pending payments", money(k["pending"])), ("Avg. ticket", money(k["avg_ticket"])),
                       ("GST collected", money(k["gst"])), ("Avg. rating", f"{k['rating']} / 5" if k["rating"] else "-"),
                       ("New customers", k["new_customers"])]),
             Paragraph("Revenue trend", H2), _bars(r["series"]["labels"], r["series"]["revenue"]),
             Paragraph("Bookings trend", H2), _bars(r["series"]["labels"], r["series"]["bookings"], colors.HexColor("#2563eb"))]
    if r["services"]:
        story += [Paragraph("Top services", H2),
                  _table([["Service", "Jobs", "Revenue"]] + [[s["name"], s["n"], money(s["rev"])] for s in r["services"]],
                         [100 * mm, 30 * mm, 50 * mm], align_right=(1, 2))]
    if r["techs"]:
        story += [Paragraph("Technician performance", H2),
                  _table([["Technician", "Jobs", "Revenue", "Avg. rating"]] +
                         [[t["full_name"], t["jobs"], money(t["rev"]), f"{t['rating']:.1f}" if t["rating"] else "-"]
                          for t in r["techs"]], [70 * mm, 30 * mm, 50 * mm, 30 * mm], align_right=(1, 2, 3))]
    if r["customers"]:
        story += [Paragraph("Top customers", H2),
                  _table([["Customer", "Phone", "Visits", "Spend"]] +
                         [[c["name"], "+91 " + c["phone"], c["n"], money(c["spend"])] for c in r["customers"]],
                         [70 * mm, 40 * mm, 20 * mm, 50 * mm], align_right=(2, 3))]
    if r["payments"]:
        story += [Paragraph("Payment methods", H2),
                  _table([["Method", "Invoices", "Amount"]] + [[p["method"], p["n"], money(p["amt"])] for p in r["payments"]],
                         [100 * mm, 30 * mm, 50 * mm], align_right=(1, 2))]
    if r["bookings"]:
        story += [Paragraph(f"Booking register ({len(r['bookings'])})", H2),
                  _table([["Booking", "Date", "Customer", "Vehicle", "Status", "Amount"]] +
                         [[b["ref"], f"{core.parse_date(b['booking_date']):%d %b}", b["cname"][:20], b["reg_no"],
                           b["status"], money(b["total"])] for b in r["bookings"]],
                         [26 * mm, 18 * mm, 45 * mm, 30 * mm, 33 * mm, 28 * mm], align_right=(5,))]
    return _build("BUSINESS REPORT", r["label"], story)


# ------------------------------------------------------------------ customer report
def customer_report_pdf(r, customer):
    k, lo = r["kpi"], r["loyalty"]
    story = [Paragraph(f"Service Report · {customer['name']}", H1),
             Paragraph(f"+91 {customer['phone']} &nbsp;|&nbsp; Period: <b>{r['label']}</b> &nbsp;|&nbsp; "
                       f"Loyalty tier: <b>{lo['tier']}</b> ({lo['points']:,} points)", SM), Spacer(1, 8),
             _kpi_row([("Visits", k["visits"]), ("Completed", k["delivered"]), ("Total spend", money(k["revenue"])),
                       ("You saved", money(k["discounts"])), ("Avg. per visit", money(k["avg_ticket"]))]),
             Paragraph("Spending", H2), _bars(r["series"]["labels"], r["series"]["revenue"])]
    if r["by_vehicle"]:
        story += [Paragraph("By vehicle", H2),
                  _table([["Vehicle", "Make / model", "Visits", "Spend"]] +
                         [[v["reg_no"], f"{v['make'] or ''} {v['model']}", v["n"], money(v["spend"])] for v in r["by_vehicle"]],
                         [40 * mm, 70 * mm, 25 * mm, 45 * mm], align_right=(2, 3))]
    if r["by_service"]:
        story += [Paragraph("By service", H2),
                  _table([["Service", "Times", "Amount"]] + [[s["name"], s["n"], money(s["rev"])] for s in r["by_service"]],
                         [100 * mm, 30 * mm, 50 * mm], align_right=(1, 2))]
    if r["vehicles"]:
        story += [Paragraph("Maintenance status", H2),
                  _table([["Vehicle", "Last service", "Next due", "Status"]] +
                         [[v["reg_no"], f"{core.parse_date(v['last_date']):%d %b %Y}" if v["last_date"] else "-",
                           f"{core.parse_date(v['next_due_date']):%d %b %Y} / {v['next_due_km']:,} km" if v["next_due_date"] else "-",
                           v["due"]["label"]] for v in r["vehicles"]], [35 * mm, 35 * mm, 75 * mm, 35 * mm])]
    if r["bookings"]:
        story += [Paragraph("Service history", H2),
                  _table([["Booking", "Date", "Vehicle", "Services", "Status", "Amount"]] +
                         [[b["ref"], f"{core.parse_date(b['booking_date']):%d %b %y}", b["reg_no"],
                           Paragraph((b["services"] or "")[:60], SM), b["status"], money(b["total"])]
                          for b in r["bookings"]], [26 * mm, 20 * mm, 28 * mm, 54 * mm, 28 * mm, 24 * mm], align_right=(5,))]
    return _build("SERVICE REPORT", r["label"], story)
