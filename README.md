# AutoCare: Smart Vehicle Service Booking and Maintenance Tracking System

Flask + SQLite + HTML/CSS/JS. Two portals: **Customer** (/login) and **Company** (/staff/login).

## Run on Windows
Double-click `start.bat` (installs Flask and reportlab, opens http://127.0.0.1:5055).
Or: `python -m pip install -r requirements.txt` then `python app.py`.
You do not need to re-run anything else; the database `autocare.db` is created and seeded automatically on first start.

## Logins (demo)
| Portal | Login |
|---|---|
| Customer | phone 9876500001 to 9876500010, OTP is shown on screen (demo mode) |
| Admin | admin / admin123 |
| Advisor | advisor / advisor123 |
| Technician | tech1 / tech123 |

New customers register with: name, mobile, vehicle number, model, consent and OTP verification.

## Features
- Customer: OTP login, garage, multi-service booking with slot capacity, live 7-stage tracking, reschedule/cancel, invoice PDF, feedback, reminders, loyalty tiers, personal reports (weekly/monthly/yearly/all time) + PDF.
- Company: control-room dashboard, kanban workshop board, bookings, walk-ins, technician assignment, billing/payment, customers with individual reports, service reminders, services and prices, team and roles, business reports (weekly/monthly/yearly) with PDF and CSV.
- Public tracking at /track using booking ID + phone.
- Dark mode, mobile layout, installable PWA.

## Database (SQLite, `autocare.db`)
customers, vehicles, services, bookings, booking_items, status_history, maintenance, notifications, otps, staff.
customers 1-N vehicles; customers 1-N bookings; bookings 1-N booking_items; bookings 1-N status_history; vehicles 1-N maintenance.

## Deploy on PythonAnywhere
1. Files tab: upload the zip to `/home/Kamali/mysite`, then in a Bash console: `unzip -o vehicle_service.zip` (move files so `app.py` sits directly in mysite).
2. Bash: `pip install --user flask reportlab`
3. Web tab: edit the WSGI file with the contents of `wsgi_pythonanywhere.py`, then press Reload.

## Before real use
Change demo passwords, set `AUTOCARE_SHOW_OTP=0`, connect an SMS gateway in `core.send_sms()`, set `SECRET_KEY`.
