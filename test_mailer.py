"""
Tests for mailer.py. NOTHING is emailed: every test talks to a fake SMTP server
running on 127.0.0.1 inside this process, which just records what it receives.

    python3 -m unittest -v test_mailer
"""
import base64
import csv
import email
import email.policy
import json
import smtplib
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import mailer

REAL_CSV = mailer.default_csv()


# --------------------------------------------------------------------------- fake SMTP server

class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, password="secret", reject=(), drop_after=None, hangup_on_ehlo=False):
        super().__init__(("127.0.0.1", 0), FakeSMTPHandler)
        self.hangup_on_ehlo = hangup_on_ehlo
        self.password = password
        self.reject = {r.lower() for r in reject}
        self.drop_after = drop_after  # close the connection after N messages (once)
        self.messages = []  # (mail_from, [rcpts], parsed message)
        self.logins = []
        self.connections = 0
        self.lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def all_rcpts(self):
        return [r for _, rcpts, _ in self.messages for r in rcpts]


class FakeSMTPHandler(socketserver.StreamRequestHandler):
    def w(self, line):
        self.wfile.write((line + "\r\n").encode())

    def handle(self):
        srv: FakeSMTP = self.server
        with srv.lock:
            srv.connections += 1
        self.w("220 fake.local ESMTP")
        mail_from, rcpts, authed, count = None, [], False, 0
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            up = line.upper()
            if up.startswith("EHLO") and srv.hangup_on_ehlo:
                return
            if up.startswith("EHLO"):
                self.wfile.write(b"250-fake.local\r\n250-8BITMIME\r\n250 AUTH PLAIN\r\n")
            elif up.startswith("HELO"):
                self.w("250 fake.local")
            elif up.startswith("AUTH PLAIN"):
                parts = line.split(" ", 2)
                creds = base64.b64decode(parts[2]).split(b"\0") if len(parts) == 3 else []
                user, pw = (creds[1].decode(), creds[2].decode()) if len(creds) == 3 else ("", "")
                if pw == srv.password:
                    authed = True
                    srv.logins.append(user)
                    self.w("235 2.7.0 Authentication successful")
                else:
                    self.w("535 5.7.8 Username and Password not accepted")
            elif up.startswith("MAIL FROM:"):
                if srv.password and not authed:
                    self.w("530 5.7.0 Authentication required")
                    continue
                mail_from = line[10:].strip().strip("<>").split(">")[0]
                rcpts = []
                self.w("250 OK")
            elif up.startswith("RCPT TO:"):
                addr = line[8:].strip().strip("<>").split(">")[0]
                if addr.lower() in srv.reject:
                    self.w("550 5.1.1 No such user")
                else:
                    rcpts.append(addr)
                    self.w("250 OK")
            elif up == "DATA":
                self.w("354 End data with <CR><LF>.<CR><LF>")
                buf = []
                while True:
                    dl = self.rfile.readline()
                    if dl in (b".\r\n", b".\n", b""):
                        break
                    buf.append(dl[1:] if dl.startswith(b"..") else dl)
                msg = email.message_from_bytes(b"".join(buf), policy=email.policy.default)
                with srv.lock:
                    srv.messages.append((mail_from, list(rcpts), msg))
                self.w("250 OK queued")
                count += 1
                if srv.drop_after and count >= srv.drop_after:
                    srv.drop_after = None
                    return  # hang up without QUIT, like a server timing out
            elif up == "RSET":
                rcpts = []
                self.w("250 OK")
            elif up == "NOOP":
                self.w("250 OK")
            elif up == "QUIT":
                self.w("221 Bye")
                return
            else:
                self.w("502 Command not implemented")


# --------------------------------------------------------------------------- helpers

class AppHarness:
    def __init__(self, csv_path: Path):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = mailer.make_server(csv_path, "127.0.0.1", 0, Path(self.tmp.name) / "logs")
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.token = self.httpd.app.token
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, path, body, token=True):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"X-Mailer-Token": self.token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def wait_job(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            _, r = self.post("/api/job", {})
            if r["job"] and r["job"]["status"] != "running":
                return r["job"]
            time.sleep(0.05)
        raise AssertionError("job did not finish")


def smtp_settings(port, password="secret"):
    return {"host": "127.0.0.1", "port": port, "security": "none", "username": "club@example.org",
            "password": password, "from_name": "LNU AI Society", "from_email": "club@example.org",
            "reply_to": "board@example.org"}


MESSAGE = {"subject": "Workshop for {first_name}", "body": "Hi {first_name},\n\nSee you there, {name} ({programme}).\n— Åsa",
           "is_html": False, "attachments": []}


def write_csv(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Full name", "LNU Student email\n(Used for invitations)", "Familiarity", "Programme"])
        w.writerows(rows)


# --------------------------------------------------------------------------- tests

class TestCSV(unittest.TestCase):
    def test_real_member_list(self):
        self.assertIsNotNone(REAL_CSV, "member CSV not found next to mailer.py")
        d = mailer.load_recipients(REAL_CSV)
        with open(REAL_CSV, newline="", encoding="utf-8-sig") as f:
            raw = [r[2].strip().lower() for r in list(csv.reader(f))[1:]]
        self.assertEqual(d["total_rows"], len(raw))
        self.assertEqual(len(d["recipients"]), len(set(raw)))
        self.assertEqual(len(d["recipients"]) + len(d["duplicates"]) + len(d["invalid"]) + d["blank"], len(raw))
        emails = [r["email"].lower() for r in d["recipients"]]
        self.assertEqual(len(emails), len(set(emails)), "recipients must be unique")
        self.assertTrue(all(r["name"] for r in d["recipients"]))
        self.assertTrue(any("student.inu.se" in r["warning"] for r in d["recipients"]), "typo domain should be flagged")

    def test_dedupe_invalid_blank(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "m.csv"
            write_csv(p, [["1", "Anna  Berg ", "anna@student.lnu.se", "x", "SE"],
                          ["2", "Anna B", "ANNA@student.lnu.se ", "x", "SE"],
                          ["3", "Bad", "not-an-email", "x", "SE"],
                          ["4", "Blank", "", "x", "SE"],
                          ["5", "", "noname@student.lnu.se", "x", ""]])
            d = mailer.load_recipients(p)
            self.assertEqual([r["email"] for r in d["recipients"]], ["anna@student.lnu.se", "noname@student.lnu.se"])
            self.assertEqual(d["recipients"][0]["name"], "Anna Berg")
            self.assertEqual(len(d["duplicates"]), 1)
            self.assertEqual(len(d["invalid"]), 1)
            self.assertEqual(d["blank"], 1)


class TestMessage(unittest.TestCase):
    S = mailer.clean_settings(smtp_settings(25))

    def test_placeholders_and_unicode(self):
        r = {"email": "isaac@student.lnu.se", "name": "Isaac Benítez Santos", "first_name": "Isaac", "programme": "Software"}
        m = mailer.build_message(self.S, mailer.clean_message(MESSAGE), r)
        parsed = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
        self.assertEqual(parsed["Subject"], "Workshop for Isaac")
        self.assertEqual(parsed["To"].addresses[0].addr_spec, "isaac@student.lnu.se")
        self.assertEqual(parsed["To"].addresses[0].display_name, "Isaac Benítez Santos")
        self.assertEqual(parsed["Reply-To"], "board@example.org")
        body = parsed.get_body(("plain",)).get_content()
        self.assertIn("Hi Isaac,", body)
        self.assertIn("Isaac Benítez Santos (Software)", body)
        self.assertIn("Åsa", body)

    def test_missing_name_fallback(self):
        m = mailer.build_message(self.S, mailer.clean_message(MESSAGE), {"email": "x@y.se"})
        self.assertIn("Hi there,", m.get_body(("plain",)).get_content())

    def test_html_escapes_values_and_has_text_part(self):
        msg = dict(MESSAGE, is_html=True, body="<p>Hi <b>{name}</b></p><p><a href='https://x.se'>Join</a></p>")
        r = {"email": "a@b.se", "name": "<script>", "first_name": "x", "programme": ""}
        m = mailer.build_message(self.S, mailer.clean_message(msg), r)
        self.assertIn("&lt;script&gt;", m.get_body(("html",)).get_content())
        text = m.get_body(("plain",)).get_content()
        self.assertIn("Hi <script>", text)
        self.assertIn("Join (https://x.se)", text)

    def test_literal_braces_left_alone(self):
        msg = dict(MESSAGE, body="CSS {color:red} and {unknown} stay; {first_name} changes")
        m = mailer.build_message(self.S, mailer.clean_message(msg), {"email": "a@b.se", "first_name": "Bo"})
        self.assertIn("CSS {color:red} and {unknown} stay; Bo changes", m.get_body(("plain",)).get_content())

    def test_validation(self):
        with self.assertRaises(mailer.SettingsError):
            mailer.clean_settings(dict(smtp_settings(25), from_email="", username="nope"))
        with self.assertRaises(mailer.SettingsError):
            mailer.clean_settings(dict(smtp_settings(25), port="abc"))
        with self.assertRaises(mailer.SettingsError):
            mailer.clean_settings(dict(smtp_settings(25), password=""))
        with self.assertRaises(mailer.SettingsError):
            mailer.clean_message(dict(MESSAGE, subject=" "))
        with self.assertRaises(mailer.SettingsError):
            mailer.clean_message(dict(MESSAGE, body=""))

    def test_port_security_mismatch_is_corrected(self):
        s = mailer.clean_settings(dict(smtp_settings(465), host="smtp.gmail.com", security="starttls",
                                       password="abcd efgh ijkl mnop"))
        self.assertEqual((s["port"], s["security"], s["password"]), (465, "ssl", "abcdefghijklmnop"))
        s = mailer.clean_settings(dict(smtp_settings(587), security="ssl", password="keep my spaces"))
        self.assertEqual((s["security"], s["password"]), ("starttls", "keep my spaces"))

    def test_hangup_gives_clear_message(self):
        srv = FakeSMTP(hangup_on_ehlo=True)
        try:
            s = mailer.clean_settings(dict(smtp_settings(srv.port), security="starttls"))
            with self.assertRaises(smtplib.SMTPServerDisconnected) as cm:
                mailer.smtp_connect(s, timeout=5)
            msg = mailer.friendly_smtp_error(cm.exception)
            self.assertIn(f"connecting to 127.0.0.1:{srv.port}", msg)
            self.assertIn("587 + STARTTLS", msg)
        finally:
            srv.shutdown(); srv.server_close()

    def test_parse_email_list(self):
        self.assertEqual(mailer.parse_email_list("a@b.se, c@d.se;\n<e@f.se>  A@B.se"), ["a@b.se", "c@d.se", "e@f.se"])


class TestApp(unittest.TestCase):
    """End-to-end through the HTTP API, with the real member CSV, against the fake SMTP server."""

    def setUp(self):
        self.smtp = FakeSMTP()
        self.app = AppHarness(REAL_CSV)
        self.members = {r["email"].lower() for r in self.app.httpd.app.data["recipients"]}
        self.n = len(self.members)

    def tearDown(self):
        self.app.close()
        self.smtp.shutdown()
        self.smtp.server_close()

    def test_page_and_state(self):
        with urllib.request.urlopen(self.app.base + "/", timeout=10) as r:
            page = r.read().decode()
        self.assertIn("LNU AIS Mailer", page)
        self.assertIn(self.app.token, page)
        code, s = self.app.post("/api/state", {})
        self.assertEqual(code, 200)
        self.assertEqual(len(s["recipients"]), self.n)

    def test_token_required(self):
        code, r = self.app.post("/api/send-test", {"test_emails": "a@b.se"}, token=False)
        self.assertEqual(code, 403)
        self.assertEqual(self.smtp.connections, 0)

    def test_connection_ok_and_bad_password(self):
        code, r = self.app.post("/api/test-connection", {"settings": smtp_settings(self.smtp.port)})
        self.assertEqual(code, 200, r)
        code, r = self.app.post("/api/test-connection", {"settings": smtp_settings(self.smtp.port, "wrong")})
        self.assertEqual(code, 400)
        self.assertIn("Login failed", r["error"])
        self.assertEqual(self.smtp.messages, [], "testing the connection must not send anything")

    def test_send_test_only_goes_to_test_addresses(self):
        member = sorted(self.members)[0]
        code, j = self.app.post("/api/send-test", {
            "settings": smtp_settings(self.smtp.port), "message": MESSAGE, "test_prefix": True,
            "test_emails": "tester1@example.com,\n tester2@example.com"})
        self.assertEqual(code, 200, j)
        job = self.app.wait_job()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["sent"], 2)
        self.assertEqual(sorted(self.smtp.all_rcpts()), ["tester1@example.com", "tester2@example.com"])
        self.assertFalse(set(r.lower() for r in self.smtp.all_rcpts()) & self.members, "a member was emailed!")
        for _, rcpts, msg in self.smtp.messages:
            self.assertEqual(len(rcpts), 1)
            self.assertTrue(msg["Subject"].startswith("[TEST] Workshop for Test"))
            self.assertEqual(msg["To"].addresses[0].addr_spec, rcpts[0])
            self.assertEqual(msg["From"].addresses[0].addr_spec, "club@example.org")
        self.assertTrue(Path(job["log_file"]).exists())
        # A member address used as a test address gets only that one email, with their real name.
        self.smtp.messages.clear()
        self.app.post("/api/send-test", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                         "test_emails": member})
        self.app.wait_job()
        self.assertEqual(self.smtp.all_rcpts(), [member])

    def test_send_test_validation(self):
        for emails, needle in [("", "at least one"), ("nope", "Not valid"),
                               (",".join(f"t{i}@x.se" for i in range(26)), "at most")]:
            code, r = self.app.post("/api/send-test", {"settings": smtp_settings(self.smtp.port),
                                                       "message": MESSAGE, "test_emails": emails})
            self.assertEqual(code, 400)
            self.assertIn(needle, r["error"])
        self.assertEqual(self.smtp.connections, 0)

    def test_bulk_requires_exact_confirmation(self):
        for confirm in ["", "SEND", f"SEND {self.n + 1}", "yes"]:
            code, r = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                       "confirm": confirm, "delay": 0})
            self.assertEqual(code, 400)
            self.assertIn(f"SEND {self.n}", r["error"])
        self.assertEqual(self.smtp.connections, 0)

    def test_bulk_dry_run_sends_nothing(self):
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "confirm": f"send {self.n}", "delay": 0, "dry_run": True})
        self.assertEqual(code, 200, j)
        job = self.app.wait_job()
        self.assertEqual((job["status"], job["sent"], job["failed"]), ("done", self.n, []))
        self.assertEqual(self.smtp.connections, 0)

    def test_bulk_sends_one_email_to_each_member(self):
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "confirm": f"SEND {self.n}", "delay": 0})
        self.assertEqual(code, 200, j)
        job = self.app.wait_job()
        self.assertEqual(job["status"], "done", job["log"][-5:])
        self.assertEqual(job["sent"], self.n)
        rcpts = [r.lower() for r in self.smtp.all_rcpts()]
        self.assertEqual(len(rcpts), self.n)
        self.assertEqual(set(rcpts), self.members, "every member exactly once, nobody else")
        self.assertEqual(self.smtp.connections, 1, "one connection is reused")
        by_email = {r["email"].lower(): r for r in self.app.httpd.app.data["recipients"]}
        for _, rc, msg in self.smtp.messages:
            self.assertEqual(len(rc), 1, "each email must have exactly one recipient")
            self.assertIsNone(msg["Cc"]); self.assertIsNone(msg["Bcc"])
            m = by_email[rc[0].lower()]
            self.assertEqual(msg["To"].addresses[0].addr_spec.lower(), rc[0].lower())
            self.assertEqual(msg["Subject"], f"Workshop for {m['first_name']}")
            self.assertIn(f"Hi {m['first_name']},", msg.get_body(("plain",)).get_content())
        with open(job["log_file"], newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), self.n)
        self.assertTrue(all(r["status"] == "sent" for r in rows))

    def test_bulk_exclusions_and_retry_subset(self):
        allm = sorted(self.members)
        exclude, only = allm[:5], allm[5:8]
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "exclude": exclude, "confirm": f"SEND {self.n - 5}", "delay": 0})
        self.assertEqual(code, 200, j)
        self.app.wait_job()
        self.assertEqual(set(r.lower() for r in self.smtp.all_rcpts()), set(allm[5:]))
        self.smtp.messages.clear()
        # "only" (used by the retry button) can never add addresses outside the CSV
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "only": only + ["outsider@evil.example"], "confirm": "SEND 3",
                                                   "delay": 0})
        self.assertEqual(code, 200, j)
        self.app.wait_job()
        self.assertEqual(sorted(r.lower() for r in self.smtp.all_rcpts()), only)

    def test_bulk_rejected_recipient_and_reconnect(self):
        allm = sorted(self.members)
        self.smtp.reject = {allm[3]}
        self.smtp.drop_after = 10
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "confirm": f"SEND {self.n}", "delay": 0})
        self.assertEqual(code, 200, j)
        job = self.app.wait_job()
        self.assertEqual(job["status"], "done", job["log"][-5:])
        self.assertEqual(job["sent"], self.n - 1)
        self.assertEqual([f["email"].lower() for f in job["failed"]], [allm[3]])
        self.assertEqual(len(self.smtp.all_rcpts()), self.n - 1)
        self.assertEqual(self.smtp.connections, 2, "should reconnect once after the server hung up")

    def test_bulk_bad_password_aborts_cleanly(self):
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port, "wrong"),
                                                   "message": MESSAGE, "confirm": f"SEND {self.n}", "delay": 0})
        job = self.app.wait_job()
        self.assertEqual(job["status"], "aborted")
        self.assertIn("Login failed", job["error"])
        self.assertEqual(len(job["remaining"]), self.n)
        self.assertEqual(self.smtp.messages, [])

    def test_stop_and_one_job_at_a_time(self):
        code, j = self.app.post("/api/send-bulk", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "confirm": f"SEND {self.n}", "delay": 0.3})
        self.assertEqual(code, 200)
        time.sleep(0.5)
        code, r = self.app.post("/api/send-test", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                   "test_emails": "t@x.se"})
        self.assertEqual(code, 409)
        self.app.post("/api/stop", {})
        job = self.app.wait_job()
        self.assertEqual(job["status"], "stopped")
        self.assertLess(job["sent"], self.n)
        self.assertEqual(job["sent"] + len(job["remaining"]), self.n)
        self.assertNotIn("t@x.se", self.smtp.all_rcpts())

    def test_attachment(self):
        att = {"filename": "poster.pdf", "content_type": "application/pdf",
               "data_b64": base64.b64encode(b"%PDF-1.4 fake").decode()}
        self.app.post("/api/send-test", {"settings": smtp_settings(self.smtp.port),
                                         "message": dict(MESSAGE, attachments=[att]), "test_emails": "t@x.se"})
        self.app.wait_job()
        msg = self.smtp.messages[0][2]
        parts = list(msg.iter_attachments())
        self.assertEqual(parts[0].get_filename(), "poster.pdf")
        self.assertEqual(parts[0].get_content(), b"%PDF-1.4 fake")

    def test_preview(self):
        code, r = self.app.post("/api/preview", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                 "mode": "bulk"})
        self.assertEqual(code, 200, r)
        first = self.app.httpd.app.data["recipients"][0]
        self.assertIn(first["email"], r["headers"]["To"])
        self.assertIn(f"Hi {first['first_name']},", r["text"])
        code, r = self.app.post("/api/preview", {"settings": smtp_settings(self.smtp.port), "message": MESSAGE,
                                                 "mode": "test", "test_emails": "me@x.se", "test_prefix": True})
        self.assertEqual(r["headers"]["Subject"], "[TEST] Workshop for Test")
        self.assertEqual(self.smtp.connections, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
