#!/usr/bin/env python3
"""
LNU AIS Mailer - send an email to everyone on the member list (CSV).

Pure Python standard library: no installs needed.

    python3 mailer.py                      # opens http://127.0.0.1:8765 in your browser
    python3 mailer.py --csv other.csv      # use a different member list
    python3 mailer.py --port 9000 --no-browser

The page has two separate send actions:
  * "Send test"  -> goes ONLY to the addresses you type into the test box.
  * "Send to all" -> goes to the members in the CSV (one individual email each,
    nobody sees anyone else's address). It requires you to type a confirmation.
"""
from __future__ import annotations

import argparse
import base64
import csv
import difflib
import html
import json
import mimetypes
import re
import secrets
import smtplib
import ssl
import sys
import threading
import time
import uuid
import webbrowser
from collections import Counter
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)
PLACEHOLDER_RE = re.compile(r"\{(name|first_name|email|programme)\}")
MAX_TEST_RECIPIENTS = 25
MAX_BODY_BYTES = 30 * 1024 * 1024  # request size limit (attachments are base64 in JSON)
MAX_CONSECUTIVE_FAILURES = 3  # abort a run if the server keeps refusing (e.g. quota hit)


def is_valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match(addr or ""))


def parse_email_list(text: str) -> list[str]:
    """Split a free-form list of addresses (commas, semicolons, spaces, newlines)."""
    out, seen = [], set()
    for part in re.split(r"[\s,;]+", text or ""):
        part = part.strip().strip("<>").strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


# --------------------------------------------------------------------------- CSV

def _find_col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        hl = h.lower()
        if any(n in hl for n in needles):
            return i
    return None


def load_recipients(path: Path) -> dict:
    """Read the member CSV. Returns unique valid recipients plus a report of skipped rows."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"{path.name} is empty")
    header = rows[0]
    email_i = _find_col(header, "email", "e-mail")
    if email_i is None:
        raise ValueError(f"No email column found in {path.name}. Header: {header}")
    name_i = _find_col(header, "full name", "name")
    prog_i = _find_col(header, "programme", "program")

    def cell(row, i):
        return " ".join(row[i].split()) if i is not None and i < len(row) else ""

    recipients, invalid, duplicates, blank = [], [], [], 0
    seen: dict[str, int] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        email = cell(row, email_i).replace(" ", "")
        name = cell(row, name_i)
        if not email:
            blank += 1
            continue
        if not is_valid_email(email):
            invalid.append({"line": line_no, "email": email, "name": name})
            continue
        key = email.lower()
        if key in seen:
            duplicates.append({"line": line_no, "email": email, "name": name, "first_line": seen[key]})
            continue
        seen[key] = line_no
        recipients.append({
            "email": email,
            "name": name,
            "first_name": name.split()[0] if name else "",
            "programme": cell(row, prog_i),
            "line": line_no,
            "warning": "",
        })

    # Flag domains that look like a typo of the most common domain (e.g. student.inu.se).
    domains = Counter(r["email"].split("@")[1].lower() for r in recipients)
    if domains:
        main_domain = domains.most_common(1)[0][0]
        for r in recipients:
            d = r["email"].split("@")[1].lower()
            if d != main_domain and difflib.SequenceMatcher(None, d, main_domain).ratio() >= 0.85:
                r["warning"] = f"Possible typo: '{d}' (did they mean '{main_domain}'?)"

    return {
        "csv_path": str(path),
        "csv_name": path.name,
        "total_rows": len(rows) - 1,
        "recipients": recipients,
        "invalid": invalid,
        "duplicates": duplicates,
        "blank": blank,
        "domains": domains.most_common(),
    }


# --------------------------------------------------------------------------- message building

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "hr"}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.hrefs: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK:
            self.parts.append("\n\n")
        elif tag == "li":
            self.parts.append("\n- ")
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n\n")
        elif tag == "a" and self.hrefs:
            href = self.hrefs.pop()
            if href and not href.startswith(("#", "mailto:")):
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))


def html_to_text(markup: str) -> str:
    p = _TextExtractor()
    p.feed(markup)
    p.close()
    lines = [ln.strip() for ln in "".join(p.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def placeholder_values(r: dict) -> dict:
    return {
        "name": r.get("name") or "there",
        "first_name": r.get("first_name") or "there",
        "email": r.get("email", ""),
        "programme": r.get("programme", ""),
    }


def render(template: str, r: dict, escape_html: bool = False) -> str:
    vals = placeholder_values(r)

    def sub(m):
        v = vals.get(m.group(1), "")
        return html.escape(v) if escape_html else v

    return PLACEHOLDER_RE.sub(sub, template or "")


def _address(name: str, email: str) -> Address:
    local, _, domain = email.rpartition("@")
    return Address(display_name=name or "", username=local, domain=domain)


class SettingsError(ValueError):
    pass


def clean_settings(s: dict, need_server: bool = True) -> dict:
    s = dict(s or {})
    out = {
        "host": (s.get("host") or "").strip(),
        "security": (s.get("security") or "starttls").strip().lower(),
        "username": (s.get("username") or "").strip(),
        "password": s.get("password") or "",
        "from_name": (s.get("from_name") or "").strip(),
        "from_email": (s.get("from_email") or "").strip() or (s.get("username") or "").strip(),
        "reply_to": (s.get("reply_to") or "").strip(),
    }
    try:
        out["port"] = int(s.get("port") or 587)
    except (TypeError, ValueError):
        raise SettingsError("Port must be a number (usually 587 or 465).")
    if not 1 <= out["port"] <= 65535:
        raise SettingsError("Port must be between 1 and 65535.")
    if out["security"] not in ("starttls", "ssl", "none"):
        raise SettingsError("Security must be STARTTLS, SSL/TLS or None.")
    # Port 465 always means SSL/TLS from the start; 587 always means STARTTLS. A mismatch makes the
    # server hang up ("Connection unexpectedly closed"), so fix it instead of failing.
    if out["port"] == 465 and out["security"] == "starttls":
        out["security"] = "ssl"
    elif out["port"] == 587 and out["security"] == "ssl":
        out["security"] = "starttls"
    if out["host"].lower().endswith(("gmail.com", "googlemail.com")):
        out["password"] = out["password"].replace(" ", "")  # Google shows app passwords as "abcd efgh ijkl mnop"
    if not is_valid_email(out["from_email"]):
        raise SettingsError("Enter a valid 'From' email address (your email).")
    if out["reply_to"] and not is_valid_email(out["reply_to"]):
        raise SettingsError("Reply-To is not a valid email address.")
    if need_server:
        if not out["host"]:
            raise SettingsError("Enter the SMTP server (e.g. smtp.gmail.com).")
        if out["username"] and not out["password"]:
            raise SettingsError("Enter your password / app password.")
    return out


def clean_message(m: dict) -> dict:
    m = dict(m or {})
    out = {
        "subject": (m.get("subject") or "").strip(),
        "body": m.get("body") or "",
        "is_html": bool(m.get("is_html")),
        "attachments": [],
    }
    if not out["subject"]:
        raise SettingsError("The subject is empty.")
    if not out["body"].strip():
        raise SettingsError("The message body is empty.")
    for a in m.get("attachments") or []:
        try:
            data = base64.b64decode(a["data_b64"], validate=True)
        except Exception:
            raise SettingsError(f"Attachment {a.get('filename')!r} could not be read.")
        filename = Path(a.get("filename") or "attachment").name
        ctype = a.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if "/" not in ctype:
            ctype = "application/octet-stream"
        out["attachments"].append({"filename": filename, "content_type": ctype, "data": data})
    return out


def build_message(settings: dict, message: dict, r: dict, test_prefix: str = "") -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = test_prefix + render(message["subject"], r)
    msg["From"] = _address(settings["from_name"], settings["from_email"])
    msg["To"] = _address(r.get("name", ""), r["email"])
    if settings.get("reply_to"):
        msg["Reply-To"] = settings["reply_to"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=settings["from_email"].split("@")[1])

    if message["is_html"]:
        body_html = render(message["body"], r, escape_html=True)
        msg.set_content(html_to_text(body_html), cte="quoted-printable")
        msg.add_alternative(body_html, subtype="html", cte="quoted-printable")
    else:
        msg.set_content(render(message["body"], r), cte="quoted-printable")

    for a in message["attachments"]:
        maintype, subtype = a["content_type"].split("/", 1)
        msg.add_attachment(a["data"], maintype=maintype, subtype=subtype, filename=a["filename"])
    return msg


# --------------------------------------------------------------------------- SMTP

# Errors that mean "couldn't talk to the server on this port/security", as opposed to a login problem.
_TRANSPORT_ERRORS = (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ssl.SSLError,
                     TimeoutError, ConnectionError, smtplib.SMTPNotSupportedError)


def _smtp_connect_once(host: str, port: int, security: str, settings: dict, timeout: float) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    server = None
    stage = f"connecting to {host}:{port}"
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.ehlo()
            if security == "starttls":
                stage = "starting encryption (STARTTLS)"
                server.starttls(context=ctx)
                server.ehlo()
        if settings["username"]:
            stage = f"logging in as {settings['username']}"
            server.login(settings["username"], settings["password"])
        return server
    except Exception as e:
        e.smtp_stage = stage  # type: ignore[attr-defined]
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        raise


def smtp_connect(settings: dict, timeout: float = 30) -> smtplib.SMTP:
    """Connect + log in. If the chosen port/security can't connect, try the other standard setup
    (587 STARTTLS / 465 SSL). On success with a fallback, `settings` is updated in place."""
    host, port, security = settings["host"], settings["port"], settings["security"]
    try:
        return _smtp_connect_once(host, port, security, settings, timeout)
    except _TRANSPORT_ERRORS as first:
        if security == "none" or str(getattr(first, "smtp_stage", "")).startswith("logging in"):
            raise
        for alt_port, alt_sec in ((587, "starttls"), (465, "ssl")):
            if (alt_port, alt_sec) == (port, security):
                continue
            try:
                server = _smtp_connect_once(host, alt_port, alt_sec, settings, min(timeout, 20))
            except _TRANSPORT_ERRORS:
                continue
            # (a login error on the alternative propagates: it proves the alternative connects)
            settings["port"], settings["security"] = alt_port, alt_sec
            settings["fallback_note"] = (f"Port {port} with {security.upper()} didn't work, so it switched to "
                                         f"port {alt_port} with {'SSL/TLS' if alt_sec == 'ssl' else 'STARTTLS'}.")
            return server
        raise first


def friendly_smtp_error(e: Exception) -> str:
    stage = getattr(e, "smtp_stage", "")
    msg = _friendly_smtp_error(e)
    return f"Failed while {stage}: {msg}" if stage and not isinstance(e, smtplib.SMTPAuthenticationError) else msg


def _friendly_smtp_error(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPServerDisconnected):
        return ("the server hung up. This almost always means Port and Security don't match "
                "(use 587 + STARTTLS, or 465 + SSL/TLS), or your network/antivirus/VPN blocks email ports "
                "(try another network, e.g. a phone hotspot).")
    if isinstance(e, smtplib.SMTPAuthenticationError):
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        return ("Login failed - wrong username or password. For Gmail you must use an App Password "
                "(Google Account > Security > 2-Step Verification > App passwords), not your normal password. "
                f"Server said: {e.smtp_code} {detail}")
    if isinstance(e, smtplib.SMTPNotSupportedError):
        if "AUTH" in str(e):
            return ("this server only allows login over an encrypted connection - set Security to STARTTLS "
                    "(port 587) or SSL/TLS (port 465).")
        return f"the server doesn't support this (try a different Security/Port combination): {e}"
    if isinstance(e, smtplib.SMTPResponseException):
        detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
        return f"Server said: {e.smtp_code} {detail}"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "Recipient refused: " + "; ".join(
            f"{k}: {v[0]} {v[1].decode(errors='replace') if isinstance(v[1], bytes) else v[1]}"
            for k, v in e.recipients.items())
    if isinstance(e, ssl.SSLError):
        return f"SSL/TLS error - check the Security setting matches the port (587 = STARTTLS, 465 = SSL/TLS): {e}"
    if isinstance(e, (TimeoutError,)):
        return "Connection timed out - check the server name and port."
    if isinstance(e, ConnectionRefusedError):
        return "Connection refused - check the server name and port."
    if isinstance(e, OSError) and getattr(e, "errno", None) is None and "Name or service" in str(e):
        return f"Unknown server name: {e}"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str, recipients: list[dict], dry_run: bool, delay: float, subject: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind  # "test" or "bulk"
        self.recipients = recipients
        self.dry_run = dry_run
        self.delay = max(0.0, delay)
        self.subject = subject
        self.status = "running"  # running | done | stopped | aborted
        self.error = ""
        self.sent: list[str] = []
        self.failed: list[dict] = []
        self.next_index = 0
        self.log: list[str] = []
        self.log_file = ""
        self.started = time.time()
        self.finished: float | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def add_log(self, line: str):
        with self.lock:
            self.log.append(f"[{datetime.now():%H:%M:%S}] {line}")
            if len(self.log) > 2000:
                self.log = self.log[-2000:]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "dry_run": self.dry_run,
                "status": self.status,
                "error": self.error,
                "total": len(self.recipients),
                "sent": len(self.sent),
                "failed": list(self.failed),
                "remaining": [r["email"] for r in self.recipients[self.next_index:]] if self.status != "running" else [],
                "log": list(self.log[-400:]),
                "log_file": self.log_file,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


def run_job(job: Job, settings: dict, message: dict, log_dir: Path):
    test_prefix = "[TEST] " if (job.kind == "test" and message.get("test_prefix")) else ""
    server = None
    log_dir.mkdir(parents=True, exist_ok=True)
    tag = ("dryrun-" if job.dry_run else "") + job.kind
    log_path = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{tag}-{job.id}.csv"
    job.log_file = str(log_path)

    def connect():
        job.add_log(f"Connecting to {settings['host']}:{settings['port']} ({settings['security']})...")
        s = smtp_connect(settings)
        if settings.pop("fallback_note", ""):
            job.add_log(f"Switched to port {settings['port']} ({settings['security']}).")
        job.add_log("Connected and logged in." if settings["username"] else "Connected.")
        return s

    with open(log_path, "w", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        w.writerow(["time", "run", "email", "name", "status", "detail", "subject"])

        def record(r, status, detail=""):
            w.writerow([datetime.now().isoformat(timespec="seconds"), tag, r["email"], r.get("name", ""),
                        status, detail, test_prefix + render(message["subject"], r)])
            lf.flush()

        try:
            if not job.dry_run:
                server = connect()
            else:
                job.add_log("DRY RUN - nothing will actually be sent.")
            consecutive_failures = 0
            for i, r in enumerate(job.recipients):
                if job.stop_event.is_set():
                    job.status = "stopped"
                    job.add_log("Stopped by you.")
                    break
                with job.lock:
                    job.next_index = i
                label = f"({i + 1}/{len(job.recipients)}) {r['email']}"
                try:
                    msg = build_message(settings, message, r, test_prefix)
                    if not job.dry_run:
                        try:
                            # to_addrs is explicit: each message goes to exactly this one person.
                            server.send_message(msg, from_addr=settings["from_email"], to_addrs=[r["email"]])
                        except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError) as e:
                            job.add_log(f"Connection dropped ({type(e).__name__}); reconnecting...")
                            try:
                                server.close()
                            except Exception:
                                pass
                            server = connect()
                            server.send_message(msg, from_addr=settings["from_email"], to_addrs=[r["email"]])
                except smtplib.SMTPAuthenticationError:
                    raise
                except smtplib.SMTPSenderRefused as e:
                    raise RuntimeError(f"The server refused the From address: {friendly_smtp_error(e)}")
                except Exception as e:
                    err = friendly_smtp_error(e)
                    with job.lock:
                        job.failed.append({"email": r["email"], "error": err})
                    record(r, "FAILED", err)
                    job.add_log(f"FAILED {label}: {err}")
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        with job.lock:
                            job.next_index = i + 1
                        raise RuntimeError(f"{MAX_CONSECUTIVE_FAILURES} failures in a row - stopping so you can "
                                           "check what's wrong (you may have hit a sending limit).")
                else:
                    consecutive_failures = 0
                    with job.lock:
                        job.sent.append(r["email"])
                    record(r, "dry-run" if job.dry_run else "sent")
                    job.add_log(("Would send " if job.dry_run else "Sent ") + label)
                with job.lock:
                    job.next_index = i + 1
                if i < len(job.recipients) - 1 and job.delay and not job.dry_run:
                    job.stop_event.wait(job.delay)
            else:
                job.status = "done"
        except Exception as e:
            job.status = "aborted"
            job.error = friendly_smtp_error(e) if not isinstance(e, RuntimeError) else str(e)
            job.add_log("ABORTED: " + job.error)
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
            job.finished = time.time()
            with job.lock:
                summary = (f"Finished: {len(job.sent)} {'would be sent' if job.dry_run else 'sent'}, "
                           f"{len(job.failed)} failed, {len(job.recipients) - job.next_index} not attempted.")
            job.add_log(summary)
            job.add_log(f"Log saved to {log_path}")


# --------------------------------------------------------------------------- app + HTTP

class App:
    def __init__(self, csv_path: Path, log_dir: Path):
        self.csv_path = csv_path
        self.log_dir = log_dir
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.data = load_recipients(csv_path)

    def reload(self):
        self.data = load_recipients(self.csv_path)

    def job_running(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start_job(self, job: Job, settings: dict, message: dict):
        self.job = job
        t = threading.Thread(target=run_job, args=(job, settings, message, self.log_dir), daemon=True)
        t.start()

    def bulk_targets(self, exclude: list[str], only: list[str] | None) -> list[dict]:
        excl = {e.lower() for e in exclude or []}
        targets = [r for r in self.data["recipients"] if r["email"].lower() not in excl]
        if only is not None:
            only_set = {e.lower() for e in only}
            targets = [r for r in targets if r["email"].lower() in only_set]
        return targets


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Handler(BaseHTTPRequestHandler):
    server_version = "LNUAISMailer/1.0"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = INDEX_HTML.replace("__TOKEN__", self.app.token)
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if self.headers.get("X-Mailer-Token") != self.app.token:
                raise ApiError("Bad or missing session token - reload the page.", 403)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ApiError("Expected JSON.", 415)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                raise ApiError("Request too large (attachments over ~20 MB?).", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            route = {
                "/api/state": self.api_state,
                "/api/reload": self.api_reload,
                "/api/preview": self.api_preview,
                "/api/test-connection": self.api_test_connection,
                "/api/send-test": self.api_send_test,
                "/api/send-bulk": self.api_send_bulk,
                "/api/job": self.api_job,
                "/api/stop": self.api_stop,
            }.get(path)
            if route is None:
                raise ApiError("Not found", 404)
            self._json(route(payload))
        except ApiError as e:
            self._json({"error": str(e)}, e.code)
        except SettingsError as e:
            self._json({"error": str(e)}, 400)
        except json.JSONDecodeError:
            self._json({"error": "Invalid JSON"}, 400)
        except Exception as e:  # pragma: no cover - last-resort safety net
            self._json({"error": f"Unexpected error: {type(e).__name__}: {e}"}, 500)

    # ---- endpoints

    def api_state(self, p):
        d = self.app.data
        return {
            "csv_name": d["csv_name"], "csv_path": d["csv_path"], "total_rows": d["total_rows"],
            "recipients": [{k: r[k] for k in ("email", "name", "programme", "line", "warning")}
                           for r in d["recipients"]],
            "invalid": d["invalid"], "duplicates": d["duplicates"], "blank": d["blank"],
            "domains": d["domains"],
            "job": self.app.job.snapshot() if self.app.job else None,
        }

    def api_reload(self, p):
        if self.app.job_running():
            raise ApiError("Can't reload while sending.", 409)
        try:
            self.app.reload()
        except Exception as e:
            raise ApiError(f"Could not read CSV: {e}")
        return self.api_state(p)

    def api_preview(self, p):
        settings = clean_settings(p.get("settings"), need_server=False)
        message = clean_message(p.get("message"))
        if p.get("mode") == "test":
            emails = parse_email_list(p.get("test_emails", ""))
            r = self._test_recipient(emails[0]) if emails and is_valid_email(emails[0]) else \
                {"email": "test@example.com", "name": "Test Recipient", "first_name": "Test", "programme": ""}
            prefix = "[TEST] " if p.get("test_prefix") else ""
        else:
            targets = self.app.bulk_targets(p.get("exclude") or [], p.get("only"))
            if not targets:
                raise ApiError("No recipients selected.")
            want = (p.get("email") or "").lower()
            r = next((t for t in targets if t["email"].lower() == want), targets[0])
            prefix = ""
        msg = build_message(settings, message, r, prefix)
        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        return {
            "headers": {k: str(msg[k]) for k in ("From", "To", "Reply-To", "Subject") if msg[k]},
            "text": text_part.get_content() if text_part else "",
            "html": html_part.get_content() if html_part else "",
            "attachments": [a["filename"] for a in message["attachments"]],
            "size_kb": round(len(msg.as_bytes()) / 1024, 1),
        }

    def api_test_connection(self, p):
        settings = clean_settings(p.get("settings"))
        try:
            s = smtp_connect(settings, timeout=20)
            s.quit()
        except Exception as e:
            raise ApiError(friendly_smtp_error(e))
        note = settings.get("fallback_note", "")
        return {"ok": True, "port": settings["port"], "security": settings["security"],
                "message": (note + " " if note else "") + f"Connected to {settings['host']}:{settings['port']}"
                           + (f" and logged in as {settings['username']}." if settings["username"] else ".")
                           + " Nothing was sent."}

    def _test_recipient(self, email: str) -> dict:
        # If the test address happens to be a member, use their real name for placeholders.
        m = next((r for r in self.app.data["recipients"] if r["email"].lower() == email.lower()), None)
        if m:
            return dict(m)
        return {"email": email, "name": "Test Recipient", "first_name": "Test", "programme": "Test Programme"}

    def api_send_test(self, p):
        with self.app.lock:
            if self.app.job_running():
                raise ApiError("Another send is still running.", 409)
            emails = parse_email_list(p.get("test_emails", ""))
            if not emails:
                raise ApiError("Enter at least one test email address.")
            bad = [e for e in emails if not is_valid_email(e)]
            if bad:
                raise ApiError("Not valid email address(es): " + ", ".join(bad))
            if len(emails) > MAX_TEST_RECIPIENTS:
                raise ApiError(f"A test can go to at most {MAX_TEST_RECIPIENTS} addresses.")
            dry_run = bool(p.get("dry_run"))
            settings = clean_settings(p.get("settings"), need_server=not dry_run)
            message = clean_message(p.get("message"))
            message["test_prefix"] = bool(p.get("test_prefix"))
            # Recipients come ONLY from the test box - never from the CSV list.
            targets = [self._test_recipient(e) for e in emails]
            job = Job("test", targets, dry_run=dry_run, delay=0.5, subject=message["subject"])
            job.add_log(f"TEST send to {len(targets)} address(es) only: {', '.join(emails)}")
            self.app.start_job(job, settings, message)
            return job.snapshot()

    def api_send_bulk(self, p):
        with self.app.lock:
            if self.app.job_running():
                raise ApiError("Another send is still running.", 409)
            only = p.get("only")
            if only is not None and not isinstance(only, list):
                raise ApiError("Bad 'only' list.")
            targets = self.app.bulk_targets(p.get("exclude") or [], only)
            if not targets:
                raise ApiError("No recipients selected.")
            expected = f"SEND {len(targets)}"
            if (p.get("confirm") or "").strip().upper() != expected:
                raise ApiError(f"To confirm, type exactly: {expected}")
            dry_run = bool(p.get("dry_run"))
            settings = clean_settings(p.get("settings"), need_server=not dry_run)
            message = clean_message(p.get("message"))
            try:
                delay = float(p.get("delay", 2))
            except (TypeError, ValueError):
                raise ApiError("Delay must be a number of seconds.")
            if not 0 <= delay <= 120:
                raise ApiError("Delay must be between 0 and 120 seconds.")
            job = Job("bulk", targets, dry_run=dry_run, delay=delay, subject=message["subject"])
            job.add_log(f"{'DRY RUN of ' if dry_run else ''}send to {len(targets)} member(s) from "
                        f"{self.app.data['csv_name']}, {delay:g}s between emails.")
            self.app.start_job(job, settings, message)
            return job.snapshot()

    def api_job(self, p):
        return {"job": self.app.job.snapshot() if self.app.job else None}

    def api_stop(self, p):
        if self.app.job_running():
            self.app.job.stop_event.set()
            self.app.job.add_log("Stop requested - finishing the current email...")
        return {"job": self.app.job.snapshot() if self.app.job else None}


def make_server(csv_path: Path, host="127.0.0.1", port=8765, log_dir: Path | None = None) -> ThreadingHTTPServer:
    app = App(csv_path, log_dir or (HERE / "sent_logs"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    return httpd


def default_csv() -> Path | None:
    csvs = sorted(p for p in HERE.glob("*.csv"))
    if not csvs:
        return None
    members = [p for p in csvs if "member" in p.name.lower()]
    return (members or csvs)[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="LNU AIS Mailer - email the member list.")
    ap.add_argument("--csv", type=Path, help="member list CSV (default: the member CSV next to this script)")
    ap.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1 = this computer only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log-dir", type=Path, default=HERE / "sent_logs")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args(argv)

    csv_path = args.csv or default_csv()
    if not csv_path or not csv_path.exists():
        sys.exit("No CSV found. Pass one with --csv path/to/list.csv")
    try:
        httpd = make_server(csv_path.resolve(), args.host, args.port, args.log_dir)
    except OSError as e:
        sys.exit(f"Could not start on port {args.port}: {e}. Try --port 9000")
    d = httpd.app.data  # type: ignore[attr-defined]
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    print(f"Loaded {len(d['recipients'])} unique recipients from {csv_path.name} "
          f"({len(d['duplicates'])} duplicates, {len(d['invalid'])} invalid skipped).")
    print(f"Open {url}  (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- page

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LNU AIS Mailer</title>
<style>
:root{
  --bg:#f5f6f8; --card:#fff; --ink:#1d2330; --muted:#636b7a; --line:#dfe3ea;
  --accent:#2457d6; --accent-ink:#fff; --test:#0f8a5f; --danger:#c62f2f; --warn-bg:#fff6dd; --warn:#8a6100;
  --ok-bg:#e6f6ee; --err-bg:#fdecec; --input:#fff; --code:#f0f2f5;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#14171c; --card:#1d2128; --ink:#e7eaf0; --muted:#9aa3b2; --line:#2e3440;
    --accent:#5b8cff; --test:#2fbf86; --danger:#ff6b6b; --warn-bg:#3a3015; --warn:#f0c560;
    --ok-bg:#16332a; --err-bg:#3b1d1d; --input:#15181d; --code:#262b33; color-scheme:dark; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header h1{margin:0;font-size:19px}
header .sub{color:var(--muted)}
main{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:18px;padding:18px 24px;max-width:1500px;margin:0 auto}
@media (max-width:1000px){main{grid-template-columns:1fr;padding:14px 16px}}
.col{display:flex;flex-direction:column;gap:18px;min-width:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card h2{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px}
.card h2 .n{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;background:var(--accent);color:var(--accent-ink);font-size:12px}
.card.test h2 .n{background:var(--test)} .card.draft h2 .n{background:#7a4cc2} .card.bulk h2 .n{background:var(--danger)}
.card .hint{color:var(--muted);margin:0 0 12px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 14px}
.grid .full{grid-column:1/-1}
@media (max-width:560px){.grid{grid-template-columns:1fr}}
label{display:block;font-weight:600;font-size:12.5px;margin-bottom:4px}
label .opt{font-weight:400;color:var(--muted)}
input[type=text],input[type=email],input[type=password],input[type=number],select,textarea{
  width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:7px;background:var(--input);color:var(--ink);font:inherit}
textarea{resize:vertical;min-height:90px}
#body{min-height:220px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:transparent}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.inline{display:flex;align-items:center;gap:6px;font-weight:400;margin:0}
button{font:inherit;font-weight:600;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 14px;cursor:pointer}
button:hover{border-color:var(--muted)}
button:disabled{opacity:.5;cursor:not-allowed}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.go-test{background:var(--test);border-color:var(--test);color:#fff}
button.go-bulk{background:var(--danger);border-color:var(--danger);color:#fff}
button.small{padding:4px 9px;font-size:12.5px}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chip{font:12px ui-monospace,Menlo,Consolas,monospace;background:var(--code);border:1px solid var(--line);border-radius:999px;padding:2px 9px;cursor:pointer;color:var(--ink)}
.msg{margin-top:10px;padding:8px 11px;border-radius:7px;font-size:13px;white-space:pre-wrap;word-break:break-word}
.msg.ok{background:var(--ok-bg)} .msg.err{background:var(--err-bg)} .msg.warn{background:var(--warn-bg);color:var(--warn)}
.stats{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 10px}
.stat b{font-size:20px;display:block;line-height:1.1}
.stat span{color:var(--muted);font-size:12px}
.tablewrap{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:var(--card);z-index:1;font-size:12px;color:var(--muted)}
tr.off td{opacity:.45;text-decoration:line-through}
td.email{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;overflow-wrap:anywhere}
.badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:5px;background:var(--warn-bg);color:var(--warn);margin-top:2px}
details{margin-top:10px} summary{cursor:pointer;color:var(--muted)}
.progress{height:10px;background:var(--code);border-radius:99px;overflow:hidden;margin:8px 0}
.progress div{height:100%;background:var(--accent);width:0;transition:width .3s}
.log{background:var(--code);border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;max-height:260px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.preview-head{font-size:13px;border:1px solid var(--line);border-radius:8px 8px 0 0;padding:8px 11px;background:var(--code)}
.preview-head div{word-break:break-word} .preview-head b{display:inline-block;min-width:70px;color:var(--muted);font-weight:600}
.preview-body{border:1px solid var(--line);border-top:0;border-radius:0 0 8px 8px;padding:12px;white-space:pre-wrap;word-break:break-word;max-height:420px;overflow:auto}
iframe.preview-body{width:100%;height:420px;background:#fff;padding:0}
.tabs{display:flex;gap:6px;margin:8px 0}
.mode-banner{padding:8px 11px;border-radius:7px;background:var(--warn-bg);color:var(--warn);margin-bottom:10px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12.5px}
.files{margin-top:6px;font-size:13px;color:var(--muted)}
[hidden]{display:none!important}
</style>
</head>
<body>
<header>
  <h1>LNU AIS Mailer</h1>
  <span class="sub" id="csvInfo">Loading member list…</span>
</header>
<main>
<div class="col">

  <section class="card">
    <h2><span class="n">1</span> Your email account (sender)</h2>
    <p class="hint">Emails are sent from your account over SMTP. Your password is never saved; it's only used while this app is running.
      <b>Gmail:</b> turn on 2-Step Verification, then create an <i>App password</i> (myaccount.google.com/apppasswords) and paste it below.</p>
    <div class="grid">
      <div class="full">
        <label for="preset">Email provider</label>
        <select id="preset">
          <option value="gmail">Gmail / Google Workspace (smtp.gmail.com)</option>
          <option value="outlook">Outlook.com / Hotmail (smtp-mail.outlook.com)</option>
          <option value="office365">Microsoft 365 / school account (smtp.office365.com)</option>
          <option value="custom">Other / custom SMTP server</option>
        </select>
      </div>
      <div><label for="username">Your email (login)</label><input id="username" type="email" placeholder="you@gmail.com" autocomplete="username"></div>
      <div><label for="password">Password / App password</label><input id="password" type="password" placeholder="16-character app password" autocomplete="current-password"></div>
      <div><label for="fromName">From name <span class="opt">(shown to recipients)</span></label><input id="fromName" type="text" placeholder="LNU AI Society"></div>
      <div><label for="fromEmail">From email <span class="opt">(blank = your email)</span></label><input id="fromEmail" type="email" placeholder="same as login"></div>
      <div class="full"><label for="replyTo">Reply-To <span class="opt">(optional - where replies go)</span></label><input id="replyTo" type="email" placeholder="optional"></div>
    </div>
    <details id="advanced">
      <summary>Server settings (filled in by the provider choice)</summary>
      <div class="grid" style="margin-top:10px">
        <div><label for="host">SMTP server</label><input id="host" type="text"></div>
        <div><label for="port">Port</label><input id="port" type="number" min="1" max="65535"></div>
        <div class="full"><label for="security">Security</label>
          <select id="security"><option value="starttls">STARTTLS (port 587)</option><option value="ssl">SSL/TLS (port 465)</option><option value="none">None (local testing only)</option></select></div>
      </div>
    </details>
    <div class="row" style="margin-top:12px"><button id="btnConn">Test connection</button><span class="hint" style="margin:0">Logs in only, sends nothing.</span></div>
    <div id="connMsg" class="msg" hidden></div>
  </section>

  <section class="card">
    <h2><span class="n">2</span> Write the email</h2>
    <p class="hint">Click a placeholder to insert it - it's replaced per person. If a name is missing, <code>{first_name}</code> becomes “there”.</p>
    <label for="subject">Subject</label>
    <input id="subject" type="text" placeholder="e.g. AI Society workshop this Thursday">
    <div style="margin-top:10px" class="row">
      <label class="inline"><input type="radio" name="fmt" value="text" checked> Plain text</label>
      <label class="inline"><input type="radio" name="fmt" value="html"> HTML</label>
    </div>
    <label for="body" style="margin-top:8px">Message</label>
    <textarea id="body" placeholder="Hi {first_name},&#10;&#10;We're hosting ...&#10;&#10;Best,&#10;LNU AI Society"></textarea>
    <div class="chips" id="chips">
      <span class="chip" data-ph="{first_name}">{first_name}</span>
      <span class="chip" data-ph="{name}">{name}</span>
      <span class="chip" data-ph="{email}">{email}</span>
      <span class="chip" data-ph="{programme}">{programme}</span>
    </div>
    <div style="margin-top:12px">
      <label for="files">Attachments <span class="opt">(optional, keep under ~15 MB total)</span></label>
      <input id="files" type="file" multiple>
      <div class="files" id="fileList"></div>
    </div>
  </section>

  <section class="card test">
    <h2><span class="n">3</span> Send a test</h2>
    <p class="hint">Goes <b>only</b> to the addresses you type here - never to the member list. Separate several with commas or new lines.</p>
    <label for="testEmails">Test recipient(s)</label>
    <textarea id="testEmails" style="min-height:60px" placeholder="you@gmail.com, friend@student.lnu.se"></textarea>
    <div class="row" style="margin-top:8px">
      <label class="inline"><input type="checkbox" id="testPrefix" checked> Put <code>[TEST]</code> in front of the subject</label>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="btnPreviewTest">Preview</button>
      <button id="btnTest" class="go-test">Send test email</button>
    </div>
    <div id="testMsg" class="msg" hidden></div>
  </section>

  <section class="card bulk">
    <h2><span class="n">4</span> Send to the member list</h2>
    <p class="hint">Each person gets their own individual email (nobody sees anyone else's address). Send a test first!</p>
    <div id="onlyBanner" class="mode-banner" hidden></div>
    <div class="grid">
      <div><label for="delay">Seconds between emails</label><input id="delay" type="number" min="0" max="120" step="0.5" value="2"></div>
      <div style="align-self:end"><label class="inline"><input type="checkbox" id="dryRun"> Dry run (simulate, send nothing)</label></div>
      <div class="full"><label for="confirm">Type <code id="confirmWord">SEND 0</code> to confirm</label><input id="confirm" type="text" autocomplete="off" spellcheck="false"></div>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="btnPreviewBulk">Preview first email</button>
      <button id="btnBulk" class="go-bulk" disabled>Send to 0 members</button>
    </div>
    <div id="bulkMsg" class="msg" hidden></div>
  </section>

  <section class="card draft">
    <h2><span class="n">5</span> Or: open a ready-made draft in your email (no password needed)</h2>
    <p class="hint">Opens Gmail / Outlook / your email app with <b>all selected members already in BCC</b> and your subject and message filled in. You check it and press Send yourself.
      Everyone gets the same email, so <code>{first_name}</code>/<code>{name}</code> become “everyone”, and attachments have to be added in the draft.</p>
    <div class="grid">
      <div><label for="draftApp">Open in</label>
        <select id="draftApp">
          <option value="gmail">Gmail (in the browser)</option>
          <option value="o365">Outlook - LNU / Microsoft 365 account</option>
          <option value="outlookcom">Outlook.com / Hotmail (personal)</option>
          <option value="mailto">My computer's email app (Thunderbird, Apple Mail, Outlook…)</option>
        </select></div>
      <div><label for="draftTo">To <span class="opt">(your own address - members go in BCC)</span></label><input id="draftTo" type="email" placeholder="you@gmail.com"></div>
    </div>
    <div class="row" style="margin-top:12px"><button id="btnDraft" class="primary">Prepare draft</button><span class="hint" style="margin:0" id="draftCount"></span></div>
    <div id="draftMsg" class="msg" hidden></div>
    <div id="draftBtns" class="row" style="margin-top:10px"></div>
    <details>
      <summary>Or copy the addresses and paste them into BCC yourself</summary>
      <div class="row" style="margin-top:8px"><button class="small" id="btnCopy">Copy selected addresses</button><span id="copyMsg" class="hint" style="margin:0"></span></div>
      <textarea id="copyBox" readonly style="margin-top:8px;min-height:70px"></textarea>
    </details>
  </section>
</div>

<div class="col">
  <section class="card" id="jobCard" hidden>
    <h2 id="jobTitle">Progress</h2>
    <div class="progress"><div id="bar"></div></div>
    <div class="stats">
      <div class="stat"><b id="jSent">0</b><span id="jSentLbl">sent</span></div>
      <div class="stat"><b id="jFailed">0</b><span>failed</span></div>
      <div class="stat"><b id="jTotal">0</b><span>total</span></div>
      <div class="stat"><b id="jStatus">-</b><span>status</span></div>
    </div>
    <div class="row" style="margin-bottom:8px">
      <button id="btnStop" class="small">Stop</button>
      <button id="btnResume" class="small" hidden>Retry failed + unsent only</button>
    </div>
    <div class="log" id="log"></div>
  </section>

  <section class="card" id="previewCard" hidden>
    <h2>Preview <button class="small" id="btnClosePreview" style="margin-left:auto">Close</button></h2>
    <div class="tabs" id="previewTabs" hidden>
      <button class="small" data-tab="html">HTML</button><button class="small" data-tab="text">Plain-text version</button>
    </div>
    <div class="preview-head" id="previewHead"></div>
    <div class="preview-body" id="previewText"></div>
    <iframe class="preview-body" id="previewHtml" sandbox="" hidden title="HTML preview"></iframe>
  </section>

  <section class="card">
    <h2>Members <span class="hint" style="margin:0;font-weight:400" id="selCount"></span></h2>
    <div class="stats" id="csvStats"></div>
    <div id="csvWarn"></div>
    <div class="row" style="margin-bottom:8px">
      <input id="search" type="text" placeholder="Search name, email, programme…" style="flex:1;min-width:160px">
      <button class="small" id="btnAll">Select all</button>
      <button class="small" id="btnNone">Select none</button>
      <button class="small" id="btnReload">Reload CSV</button>
    </div>
    <div class="tablewrap">
      <table><thead><tr><th style="width:30px"></th><th>Name</th><th>Email</th><th>Programme</th></tr></thead><tbody id="tbody"></tbody></table>
    </div>
    <details>
      <summary>Skipped rows (duplicates / invalid)</summary>
      <div id="skipped" style="margin-top:8px;font-size:13px"></div>
    </details>
  </section>
</div>
</main>

<script>
const TOKEN = "__TOKEN__";
const PRESETS = {
  gmail:{host:"smtp.gmail.com",port:587,security:"starttls"},
  outlook:{host:"smtp-mail.outlook.com",port:587,security:"starttls"},
  office365:{host:"smtp.office365.com",port:587,security:"starttls"},
  custom:null
};
const $ = id => document.getElementById(id);
let members = [], excluded = new Set(), onlyList = null, attachments = [], pollTimer = null, lastJobId = null, lastJob = null;

// ---------- persistence of non-secret fields (password is never stored)
const KEEP = ["preset","username","fromName","fromEmail","replyTo","host","port","security","subject","body","testEmails","delay","draftApp","draftTo"];
function save(){ try{ const o={}; KEEP.forEach(k=>o[k]=$(k).value); o.fmt=fmt(); o.testPrefix=$("testPrefix").checked;
  localStorage.setItem("lnuais-mailer", JSON.stringify(o)); }catch(e){} }
function restore(){ try{ const o=JSON.parse(localStorage.getItem("lnuais-mailer")||"null"); if(!o) return false;
  KEEP.forEach(k=>{ if(o[k]!==undefined && o[k]!==null) $(k).value=o[k]; });
  if(o.fmt) document.querySelector(`input[name=fmt][value=${o.fmt}]`).checked=true;
  if(o.testPrefix!==undefined) $("testPrefix").checked=o.testPrefix; return true; }catch(e){ return false; } }

function applyPreset(){ const p=PRESETS[$("preset").value]; if(p){ $("host").value=p.host; $("port").value=p.port; $("security").value=p.security; }
  else $("advanced").open=true; }
$("preset").addEventListener("change", ()=>{ applyPreset(); save(); });
$("security").addEventListener("change", ()=>{ const v=$("security").value; if(v==="ssl"&&$("port").value=="587") $("port").value=465; if(v==="starttls"&&$("port").value=="465") $("port").value=587; save(); });
$("port").addEventListener("input", ()=>{ const p=$("port").value; if(p==="465") $("security").value="ssl"; else if(p==="587" && $("security").value==="ssl") $("security").value="starttls"; save(); });
KEEP.forEach(k=>$(k).addEventListener("input", save));
document.querySelectorAll("input[name=fmt]").forEach(r=>r.addEventListener("change", save));
$("testPrefix").addEventListener("change", save);

function fmt(){ return document.querySelector("input[name=fmt]:checked").value; }

// ---------- API
async function api(path, body){
  const res = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","X-Mailer-Token":TOKEN}, body:JSON.stringify(body||{})});
  let data; try{ data = await res.json(); }catch(e){ data = {error:"Bad response from the app ("+res.status+")"}; }
  if(!res.ok) throw new Error(data.error || ("HTTP "+res.status));
  return data;
}
function settings(){ return {host:$("host").value, port:$("port").value, security:$("security").value, username:$("username").value,
  password:$("password").value, from_name:$("fromName").value, from_email:$("fromEmail").value, reply_to:$("replyTo").value}; }
function message(){ return {subject:$("subject").value, body:$("body").value, is_html:fmt()==="html", attachments}; }
function show(id, text, kind){ const el=$(id); el.textContent=text; el.className="msg "+kind; el.hidden=!text; }

// ---------- placeholders
document.querySelectorAll(".chip").forEach(c=>c.addEventListener("click", ()=>{
  const ta=$("body"), ph=c.dataset.ph, s=ta.selectionStart, e=ta.selectionEnd;
  ta.value = ta.value.slice(0,s)+ph+ta.value.slice(e); ta.focus(); ta.selectionStart=ta.selectionEnd=s+ph.length; save();
}));

// ---------- attachments
$("files").addEventListener("change", async ()=>{
  attachments = [];
  const files=[...$("files").files]; let total=0;
  for(const f of files){
    total += f.size;
    const b64 = await new Promise((ok,bad)=>{ const r=new FileReader(); r.onload=()=>ok(String(r.result).split(",")[1]||""); r.onerror=bad; r.readAsDataURL(f); });
    attachments.push({filename:f.name, content_type:f.type||"", data_b64:b64});
  }
  $("fileList").textContent = files.length ? files.map(f=>`${f.name} (${(f.size/1024).toFixed(0)} KB)`).join(", ") + (total>15e6?"  - WARNING: over 15 MB, many mail servers will reject this":"") : "";
});

// ---------- members table
function selected(){ return members.filter(m=>!excluded.has(m.email.toLowerCase()) && (!onlyList || onlyList.has(m.email.toLowerCase()))); }
function esc(s){ return String(s??"").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function renderTable(){
  const q=$("search").value.trim().toLowerCase();
  const rows = members.filter(m=>!q || (m.name+" "+m.email+" "+m.programme).toLowerCase().includes(q));
  $("tbody").innerHTML = rows.map(m=>{ const k=m.email.toLowerCase(), on=!excluded.has(k);
    return `<tr class="${on?"":"off"}"><td><input type="checkbox" data-e="${esc(k)}" ${on?"checked":""} aria-label="include ${esc(m.email)}"></td>
      <td>${esc(m.name)||"<i>(no name)</i>"}</td><td class="email">${esc(m.email)}${m.warning?`<br><span class="badge">⚠ ${esc(m.warning)}</span>`:""}</td><td>${esc(m.programme)}</td></tr>`; }).join("")
    || `<tr><td colspan="4" class="hint">No matches.</td></tr>`;
  updateCounts();
}
$("tbody").addEventListener("change", e=>{ const k=e.target.dataset.e; if(!k) return; e.target.checked?excluded.delete(k):excluded.add(k); e.target.closest("tr").className=e.target.checked?"":"off"; updateCounts(); });
$("search").addEventListener("input", renderTable);
$("btnAll").onclick=()=>{ excluded.clear(); renderTable(); };
$("btnNone").onclick=()=>{ members.forEach(m=>excluded.add(m.email.toLowerCase())); renderTable(); };

function updateCounts(){
  const n = selected().length;
  $("selCount").textContent = `- ${n} of ${members.length} selected`;
  $("draftCount").textContent = `${n} member${n===1?"":"s"} will go in BCC`;
  $("confirmWord").textContent = "SEND "+n;
  $("btnBulk").textContent = ($("dryRun").checked?"Dry run for ":"Send to ")+n+" member"+(n===1?"":"s");
  checkConfirm();
  $("onlyBanner").hidden = !onlyList;
  if(onlyList) $("onlyBanner").innerHTML = `Retry mode: only the ${n} failed/unsent member(s) from the last run. <button class="small" id="btnCancelOnly">Cancel - back to full list</button>`;
  const c=$("btnCancelOnly"); if(c) c.onclick=()=>{ onlyList=null; updateCounts(); };
}
function checkConfirm(){ const n=selected().length; $("btnBulk").disabled = !(n>0 && $("confirm").value.trim().toUpperCase()==="SEND "+n) || jobRunning(); }
$("confirm").addEventListener("input", checkConfirm);
$("dryRun").addEventListener("change", updateCounts);

function applyState(s){
  members = s.recipients;
  const known = new Set(members.map(m=>m.email.toLowerCase()));
  excluded = new Set([...excluded].filter(e=>known.has(e)));
  $("csvInfo").textContent = `${s.csv_name} - ${members.length} unique members`;
  const warnN = members.filter(m=>m.warning).length;
  $("csvStats").innerHTML = `<div class="stat"><b>${s.total_rows}</b><span>rows in CSV</span></div>
    <div class="stat"><b>${members.length}</b><span>unique valid emails</span></div>
    <div class="stat"><b>${s.duplicates.length}</b><span>duplicates skipped</span></div>
    <div class="stat"><b>${s.invalid.length+s.blank}</b><span>invalid/blank skipped</span></div>`;
  $("csvWarn").innerHTML = warnN ? `<div class="msg warn" style="margin:0 0 10px">⚠ ${warnN} address(es) look like typos (marked below). Untick them, or fix the CSV and press “Reload CSV”.</div>` : "";
  $("skipped").innerHTML = (s.duplicates.map(d=>`Line ${d.line}: <code>${esc(d.email)}</code> ${esc(d.name)} - duplicate of line ${d.first_line}`)
    .concat(s.invalid.map(d=>`Line ${d.line}: <code>${esc(d.email)}</code> ${esc(d.name)} - not a valid email`)).join("<br>")) || "None.";
  renderTable();
  if(s.job) renderJob(s.job, true);
}
$("btnReload").onclick=async()=>{ try{ applyState(await api("/api/reload")); }catch(e){ alertBox(e.message); } };
function alertBox(t){ show("bulkMsg", t, "err"); }

// ---------- ready-made drafts (mail client does the sending)
window.DRAFT_URL_LIMIT = {gmail:7500, o365:7500, outlookcom:7500, mailto:/Windows/.test(navigator.userAgent)?1900:7500};
function draftText(){
  let body = $("body").value;
  if(fmt()==="html"){
    body = body.replace(/<br\s*\/?>/gi,"\n").replace(/<\/(p|div|h[1-6]|li|tr)>/gi,"\n\n").replace(/<li[^>]*>/gi,"- ");
    const doc = new DOMParser().parseFromString(body,"text/html");
    doc.querySelectorAll("a[href]").forEach(a=>{ if(/^https?:/.test(a.getAttribute("href"))) a.textContent += " ("+a.getAttribute("href")+")"; });
    body = (doc.body.textContent||"").replace(/[ \t]+\n/g,"\n").replace(/\n{3,}/g,"\n\n").trim();
  }
  const fill = t => t.replace(/\{(first_name|name)\}/g,"everyone").replace(/\{(email|programme)\}/g,"");
  return {subject: fill($("subject").value.trim()), body: fill(body), hadPlaceholders: /\{(first_name|name|email|programme)\}/.test($("subject").value+body)};
}
function draftURL(app, to, bcc, subject, body){
  const e = encodeURIComponent;
  if(app==="gmail"){
    let u = "https://mail.google.com/mail/?view=cm&fs=1";
    if(to) u += "&to="+e(to)+"&authuser="+e(to);
    return u + "&bcc="+e(bcc.join(","))+"&su="+e(subject)+(body!==null?"&body="+e(body):"");
  }
  if(app==="o365"||app==="outlookcom"){
    const base = app==="o365" ? "https://outlook.office.com/mail/deeplink/compose" : "https://outlook.live.com/mail/0/deeplink/compose";
    return base+"?"+(to?"to="+e(to)+"&":"")+"bcc="+e(bcc.join(";"))+"&subject="+e(subject)+(body!==null?"&body="+e(body):"");
  }
  return "mailto:"+e(to||"")+"?bcc="+e(bcc.join(","))+"&subject="+e(subject)+(body!==null?"&body="+e(body.replace(/\r?\n/g,"\r\n")):"");
}
// Split recipients into as few drafts as possible while keeping each link short enough to work.
function buildDrafts(app, to, emails, subject, body){
  const limit = DRAFT_URL_LIMIT[app];
  let includeBody = draftURL(app,to,[],subject,body).length < limit*0.7;
  const b = includeBody ? body : null, drafts=[]; let cur=[];
  for(const em of emails){
    if(cur.length && draftURL(app,to,cur.concat(em),subject,b).length > limit){ drafts.push(cur); cur=[]; }
    cur.push(em);
  }
  if(cur.length) drafts.push(cur);
  return {drafts: drafts.map(d=>({emails:d, url:draftURL(app,to,d,subject,b)})), includeBody};
}
$("btnDraft").onclick=()=>{
  $("draftBtns").innerHTML=""; show("draftMsg","","");
  const emails = selected().map(m=>m.email), app=$("draftApp").value, to=$("draftTo").value.trim();
  const {subject, body, hadPlaceholders} = draftText();
  if(!emails.length) return show("draftMsg","No members selected.","err");
  if(!subject) return show("draftMsg","Write a subject first (section 2).","err");
  if(to && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) return show("draftMsg","The To address doesn't look like an email.","err");
  const {drafts, includeBody} = buildDrafts(app, to, emails, subject, body);
  const notes = [];
  notes.push(drafts.length===1 ? `1 draft with all ${emails.length} members in BCC.` :
    `${drafts.length} drafts (the list is too long for one link) - open and send each one.`);
  if(!includeBody) notes.push("Your message is too long to put in a link: the button copies it, then paste it into the draft (Ctrl+V).");
  if(hadPlaceholders) notes.push("Placeholders were replaced: {first_name}/{name} → “everyone”, {email}/{programme} removed.");
  if(attachments.length) notes.push("Attachments can't be added automatically - attach them in the draft.");
  if(app==="gmail" && !to) notes.push("Tip: put your Gmail address in To so the draft opens in the right Google account.");
  show("draftMsg", notes.join("\n"), includeBody?"ok":"warn");
  drafts.forEach((d,i)=>{
    const btn=document.createElement("button");
    const from = emails.indexOf(d.emails[0])+1, last = from+d.emails.length-1;
    btn.className = "go-test"; btn.dataset.url = d.url;
    btn.textContent = drafts.length===1 ? `Open draft (${d.emails.length} in BCC)` : `Open draft ${i+1} (members ${from}–${last})`;
    btn.onclick = async()=>{
      if(!includeBody){ try{ await navigator.clipboard.writeText(body); }catch(e){} }
      if(app==="mailto") window.location.href = d.url; else window.open(d.url, "_blank", "noopener");
      btn.textContent = "✓ Opened - " + btn.textContent.replace(/^✓ Opened - /,"");
    };
    $("draftBtns").appendChild(btn);
  });
};
$("draftApp").addEventListener("change", ()=>{ $("draftBtns").innerHTML=""; save(); });

$("btnCopy").onclick=async()=>{ const txt=selected().map(m=>m.email).join(", "); $("copyBox").value=txt;
  try{ await navigator.clipboard.writeText(txt); $("copyMsg").textContent=`Copied ${selected().length} addresses.`; }
  catch(e){ $("copyBox").select(); $("copyMsg").textContent="Select the text below and copy it (Ctrl+C)."; } };

// ---------- connection test
$("btnConn").onclick=async()=>{ const b=$("btnConn"); b.disabled=true; show("connMsg","Connecting…","warn");
  try{ const r=await api("/api/test-connection",{settings:settings()});
    if(r.port){ $("port").value=r.port; $("security").value=r.security; save(); }
    show("connMsg","✓ "+r.message,"ok"); }
  catch(e){ show("connMsg",e.message,"err"); } finally{ b.disabled=false; } };

// ---------- preview
async function preview(mode){
  const target = mode==="test"?"testMsg":"bulkMsg"; show(target,"","");
  try{
    const r = await api("/api/preview",{mode, settings:settings(), message:message(), test_emails:$("testEmails").value,
      test_prefix:$("testPrefix").checked, exclude:[...excluded], only:onlyList?[...onlyList]:null});
    $("previewCard").hidden=false;
    $("previewHead").innerHTML = Object.entries(r.headers).map(([k,v])=>`<div><b>${esc(k)}</b> ${esc(v)}</div>`).join("")
      + (r.attachments.length?`<div><b>Attached</b> ${esc(r.attachments.join(", "))}</div>`:"") + `<div><b>Size</b> ${r.size_kb} KB</div>`;
    $("previewText").textContent = r.text;
    $("previewHtml").srcdoc = r.html || "";
    $("previewTabs").hidden = !r.html;
    setTab(r.html?"html":"text");
    $("previewCard").scrollIntoView({behavior:"smooth", block:"start"});
  }catch(e){ show(target,e.message,"err"); }
}
function setTab(t){ $("previewHtml").hidden = t!=="html"; $("previewText").hidden = t==="html"; }
$("previewTabs").addEventListener("click", e=>{ if(e.target.dataset.tab) setTab(e.target.dataset.tab); });
$("btnPreviewTest").onclick=()=>preview("test");
$("btnPreviewBulk").onclick=()=>preview("bulk");
$("btnClosePreview").onclick=()=>{ $("previewCard").hidden=true; };

// ---------- sending
$("btnTest").onclick=async()=>{
  show("testMsg","","");
  try{ const j=await api("/api/send-test",{settings:settings(), message:message(), test_emails:$("testEmails").value, test_prefix:$("testPrefix").checked});
    show("testMsg","Sending test… see progress on the right.","warn"); renderJob(j); startPolling(); }
  catch(e){ show("testMsg",e.message,"err"); }
};
$("btnBulk").onclick=async()=>{
  show("bulkMsg","","");
  try{ const j=await api("/api/send-bulk",{settings:settings(), message:message(), exclude:[...excluded], only:onlyList?[...onlyList]:null,
      confirm:$("confirm").value, delay:$("delay").value, dry_run:$("dryRun").checked});
    $("confirm").value=""; checkConfirm(); show("bulkMsg", j.dry_run?"Dry run started.":"Sending… see progress on the right. Keep this window and the terminal open.","warn");
    renderJob(j); startPolling(); }
  catch(e){ show("bulkMsg",e.message,"err"); }
};
$("btnStop").onclick=async()=>{ try{ const r=await api("/api/stop"); if(r.job) renderJob(r.job); }catch(e){} };
$("btnResume").onclick=()=>{ if(!lastJob) return; onlyList = new Set(lastJob.failed.map(f=>f.email.toLowerCase()).concat(lastJob.remaining.map(e=>e.toLowerCase())));
  excluded.clear(); renderTable(); $("confirm").focus(); $("confirm").scrollIntoView({behavior:"smooth",block:"center"}); };

function jobRunning(){ return lastJob && lastJob.status==="running"; }
function renderJob(j, quiet){
  lastJob=j; lastJobId=j.id; $("jobCard").hidden=false;
  const title = (j.dry_run?"Dry run: ":"") + (j.kind==="test"?"Test send":"Send to members");
  $("jobTitle").textContent = title;
  const done = j.sent + j.failed.length;
  $("bar").style.width = (j.total? (100*done/j.total):0)+"%";
  $("jSent").textContent=j.sent; $("jSentLbl").textContent = j.dry_run?"would send":"sent";
  $("jFailed").textContent=j.failed.length; $("jTotal").textContent=j.total;
  $("jStatus").textContent = {running:"running…",done:"done ✓",stopped:"stopped",aborted:"aborted"}[j.status]||j.status;
  const lg=$("log"), atBottom = lg.scrollTop+lg.clientHeight >= lg.scrollHeight-20;
  lg.textContent = j.log.join("\n"); if(atBottom) lg.scrollTop=lg.scrollHeight;
  $("btnStop").hidden = j.status!=="running";
  $("btnResume").hidden = !(j.kind==="bulk" && !j.dry_run && j.status!=="running" && (j.failed.length || j.remaining.length));
  ["btnTest","btnConn"].forEach(id=>$(id).disabled = j.status==="running");
  checkConfirm();
  if(j.status!=="running"){
    const target = j.kind==="test"?"testMsg":"bulkMsg";
    if(!quiet){
      if(j.status==="done" && !j.failed.length) show(target, j.dry_run?`Dry run OK - ${j.sent} email(s) would be sent.`:`✓ Sent ${j.sent} email(s).`, "ok");
      else show(target, (j.error?j.error+"\n":"") + `${j.sent} ${j.dry_run?"would be sent":"sent"}, ${j.failed.length} failed, ${j.remaining.length} not attempted.`
        + (j.failed.length?"\nFailed: "+j.failed.map(f=>f.email+" ("+f.error+")").join("\n"):""), "err");
      if(j.kind==="bulk" && j.status==="done" && !j.dry_run && onlyList){ onlyList=null; updateCounts(); }
    }
  }
}
function startPolling(){ clearInterval(pollTimer); pollTimer=setInterval(async()=>{
  try{ const r=await api("/api/job"); if(r.job){ renderJob(r.job); if(r.job.status!=="running") clearInterval(pollTimer); } }catch(e){} }, 700); }

// ---------- boot
if(!restore()) applyPreset(); else if(!$("host").value) applyPreset();
if(!$("draftTo").value) $("draftTo").value = $("fromEmail").value || $("username").value;
if($("port").value==="465") $("security").value="ssl"; else if($("port").value==="587" && $("security").value==="ssl") $("security").value="starttls";
api("/api/state").then(s=>{ applyState(s); if(s.job && s.job.status==="running") startPolling(); })
  .catch(e=>{ $("csvInfo").textContent="Could not load members: "+e.message; });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
