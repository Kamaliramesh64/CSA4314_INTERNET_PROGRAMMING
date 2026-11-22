import os, sys, tempfile, datetime, sqlite3, unittest
os.environ["AUTOCARE_DB"] = tempfile.mkdtemp() + "/t.db"
os.environ["AUTOCARE_SHOW_OTP"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as A


def post(c, url, data):
    with c.session_transaction() as s:
        t = s.get("csrf")
    return c.post(url, data=dict(data, csrf_token=t))


class T(unittest.TestCase):
    def customer(self, phone):
        c = A.app.test_client(); c.get("/login")
        post(c, "/login", {"phone": phone})
        with c.session_transaction() as s:
            otp = s["demo_otp"]
        r = post(c, "/verify", {"otp": otp})
        self.assertEqual(r.headers["Location"], "/app")
        return c

    def staff(self, u="admin", p="admin123"):
        c = A.app.test_client(); c.get("/staff/login")
        self.assertEqual(post(c, "/staff/login", {"username": u, "password": p}).status_code, 302)
        return c

    def test_public(self):
        for u in ["/", "/login", "/register", "/staff/login", "/track"]:
            self.assertEqual(A.app.test_client().get(u).status_code, 200)

    def test_wrong_otp_and_anon(self):
        c = A.app.test_client(); c.get("/login"); post(c, "/login", {"phone": "9876500002"})
        self.assertEqual(post(c, "/verify", {"otp": "000000"}).status_code, 200)
        self.assertEqual(A.app.test_client().get("/app").status_code, 302)

    def test_customer_booking_and_tracking(self):
        c = self.customer("9876500003")
        for u in ["/app", "/app/vehicles", "/app/book", "/app/bookings", "/app/reports?p=week", "/app/reports?p=year",
                  "/app/reports.pdf", "/app/notifications", "/app/profile"]:
            self.assertEqual(c.get(u).status_code, 200, u)
        db = sqlite3.connect(os.environ["AUTOCARE_DB"])
        vid = db.execute("select v.id from vehicles v join customers c on c.id=v.customer_id where c.phone='9876500003'").fetchone()[0]
        d = datetime.date.today() + datetime.timedelta(days=3)
        while d.weekday() == 6:
            d += datetime.timedelta(days=1)
        r = post(c, "/app/book", {"vehicle_id": vid, "services": [1], "booking_date": d.isoformat(), "time_slot": "16:00"})
        self.assertEqual(r.status_code, 302)
        ref = db.execute("select ref from bookings order by id desc limit 1").fetchone()[0]
        t = A.app.test_client(); t.get("/track")
        self.assertIn(b"Vehicle Received", post(t, "/track", {"ref": ref, "phone": "9876500003"}).data)

    def test_staff_and_roles(self):
        s = self.staff()
        for u in ["/staff", "/staff/board", "/staff/bookings", "/staff/customers", "/staff/customers/1", "/staff/reminders",
                  "/staff/services", "/staff/team", "/staff/reports", "/staff/reports?p=week", "/staff/reports?p=year",
                  "/staff/reports.pdf", "/staff/reports.csv", "/staff/customers/1/report.pdf"]:
            self.assertEqual(s.get(u).status_code, 200, u)
        t = self.staff("tech1", "tech123")
        self.assertEqual(t.get("/staff/board").status_code, 200)
        self.assertEqual(t.get("/staff/reports").status_code, 403)


if __name__ == "__main__":
    unittest.main()
