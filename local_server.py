#!/usr/bin/env python3
"""
Krish and James Party V2 — RSVP Server
Run:  ADMIN_PASSWORD=yourpass python3 server.py
Open: http://localhost:8080
Admin: http://localhost:8080/admin
"""

import json
import sqlite3
import os
import hashlib
import secrets
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rsvps.db")
PORT = 8080
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Simple session store: token -> expiry timestamp
admin_sessions = {}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS guest_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
        invite_token TEXT UNIQUE
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rsvps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'going',
        approved INTEGER NOT NULL DEFAULT 0,
        instagram TEXT DEFAULT '',
        facebook TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS plus_ones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        added_by TEXT NOT NULL COLLATE NOCASE,
        name TEXT NOT NULL COLLATE NOCASE,
        phone TEXT DEFAULT '',
        invite_token TEXT UNIQUE,
        approved INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    try:
        conn.execute("ALTER TABLE plus_ones ADD COLUMN phone TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE plus_ones ADD COLUMN invite_token TEXT")
    except sqlite3.OperationalError:
        pass
    # Migrate: add columns if they don't exist
    try:
        conn.execute("ALTER TABLE rsvps ADD COLUMN instagram TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE rsvps ADD COLUMN facebook TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE rsvps ADD COLUMN phone TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE guest_list ADD COLUMN invite_token TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE guest_list ADD COLUMN instagram TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE guest_list ADD COLUMN facebook TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Migrate: add profile_pic column if missing
    try:
        conn.execute("ALTER TABLE rsvps ADD COLUMN profile_pic TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Backfill invite tokens for guests that don't have one
    guests_without_token = conn.execute("SELECT id FROM guest_list WHERE invite_token IS NULL").fetchall()
    for g in guests_without_token:
        conn.execute("UPDATE guest_list SET invite_token = ? WHERE id = ?", (secrets.token_urlsafe(12), g["id"]))
    # Backfill invite tokens for approved plus ones that don't have one
    po_without_token = conn.execute("SELECT id FROM plus_ones WHERE approved = 1 AND (invite_token IS NULL OR invite_token = '')").fetchall()
    for po in po_without_token:
        token = secrets.token_urlsafe(12)
        conn.execute("UPDATE plus_ones SET invite_token = ? WHERE id = ?", (token, po["id"]))
    conn.commit()
    return conn


def check_admin_session(cookie_header):
    if not cookie_header:
        return False
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith("admin_token="):
            token = part.split("=", 1)[1]
            expiry = admin_sessions.get(token)
            if expiry and expiry > time.time():
                return True
            admin_sessions.pop(token, None)
    return False


def json_response(handler, code, data):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(data).encode())


def html_response(handler, code, html):
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(html.encode())


def read_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    return json.loads(handler.rfile.read(length)) if length else {}


get_db().close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            invite_token = params.get("invite", [""])[0].strip()
            if invite_token:
                conn = get_db()
                guest = conn.execute("SELECT name FROM guest_list WHERE invite_token = ?", (invite_token,)).fetchone()
                if not guest:
                    guest = conn.execute("SELECT name FROM plus_ones WHERE invite_token = ? AND approved = 1", (invite_token,)).fetchone()
                conn.close()
                if guest:
                    # Inject JS to pre-fill name and auto-lock
                    prefill_script = f'<script>if(!localStorage.getItem(STORAGE_KEY)){{localStorage.setItem(STORAGE_KEY,{json.dumps(guest["name"])});location.href="/";}}</script>'
                    html = MAIN_HTML.replace('</body>', prefill_script + '</body>')
                    html_response(self, 200, html)
                    return
            html_response(self, 200, MAIN_HTML)
        elif self.path == "/api/rsvps":
            conn = get_db()
            rows = conn.execute(
                "SELECT name, status, instagram, facebook, profile_pic, updated_at FROM rsvps WHERE approved = 1 ORDER BY updated_at DESC"
            ).fetchall()
            # Include approved plus ones as "going" guests
            plus_ones = conn.execute(
                "SELECT name, created_at FROM plus_ones WHERE approved = 1 ORDER BY created_at DESC"
            ).fetchall()
            conn.close()
            guests = [{"name": r["name"], "status": r["status"], "instagram": r["instagram"] or "", "facebook": r["facebook"] or "", "profile_pic": r["profile_pic"] or "", "time": r["updated_at"]} for r in rows]
            rsvp_names_set = set(g["name"].lower() for g in guests)
            # Get invited guests who haven't RSVP'd yet
            rsvp_names = set(g["name"].lower() for g in guests)
            conn2 = get_db()
            gl_rows = conn2.execute("SELECT name FROM guest_list ORDER BY name").fetchall()
            conn2.close()
            invited = [{"name": r["name"], "status": "invited"} for r in gl_rows if r["name"].lower() not in rsvp_names]
            # Also add approved plus ones who haven't RSVP'd yet to invited
            invited_names = set(i["name"].lower() for i in invited)
            for p in plus_ones:
                if p["name"].lower() not in rsvp_names_set and p["name"].lower() not in invited_names:
                    invited.append({"name": p["name"], "status": "invited"})
            invited.sort(key=lambda x: x["name"].lower())
            going = [g for g in guests if g["status"] == "going"]
            maybe = [g for g in guests if g["status"] == "maybe"]
            cant = [g for g in guests if g["status"] == "cant_go"]
            data = {
                "guests": guests,
                "invited": invited,
                "going_count": len(going),
                "maybe_count": len(maybe),
                "cant_go_count": len(cant),
                "invited_count": len(invited),
                "total": len(guests),
            }
            json_response(self, 200, data)
        elif self.path == "/api/my-status":
            # Check status by name (passed as query param)
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            name = params.get("name", [""])[0].strip()
            if not name:
                json_response(self, 200, {"found": False})
                return
            conn = get_db()
            row = conn.execute("SELECT status, approved, profile_pic FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            conn.close()
            if row:
                json_response(self, 200, {"found": True, "status": row["status"], "approved": row["approved"], "profile_pic": row["profile_pic"] or ""})
            else:
                json_response(self, 200, {"found": False})
        elif self.path.startswith("/api/my-status?"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            name = params.get("name", [""])[0].strip()
            conn = get_db()
            row = conn.execute("SELECT status, approved, instagram, facebook, phone, profile_pic FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            gl_row = conn.execute("SELECT instagram, facebook FROM guest_list WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            host_ig = (gl_row["instagram"] if gl_row and gl_row["instagram"] else "")
            host_fb = (gl_row["facebook"] if gl_row and gl_row["facebook"] else "")
            conn.close()
            if row:
                json_response(self, 200, {"found": True, "status": row["status"], "approved": row["approved"], "instagram": row["instagram"] or "", "facebook": row["facebook"] or "", "phone": row["phone"] or "", "profile_pic": row["profile_pic"] or "", "host_instagram": host_ig, "host_facebook": host_fb})
            else:
                json_response(self, 200, {"found": False})
        elif self.path.startswith("/api/plus-ones?"):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(self.path).query)
            name = params.get("name", [""])[0].strip()
            if not name:
                json_response(self, 200, {"plus_ones": []})
                return
            conn = get_db()
            rows = conn.execute("SELECT id, name, phone, approved FROM plus_ones WHERE added_by = ? COLLATE NOCASE ORDER BY created_at DESC", (name,)).fetchall()
            conn.close()
            json_response(self, 200, {"plus_ones": [{"id": r["id"], "name": r["name"], "phone": r["phone"] or "", "approved": r["approved"] == 1, "denied": r["approved"] == -1} for r in rows]})
        elif self.path == "/api/announcements":
            conn = get_db()
            rows = [dict(r) for r in conn.execute("SELECT id, message, created_at FROM announcements ORDER BY created_at DESC").fetchall()]
            conn.close()
            json_response(self, 200, {"announcements": rows})
        elif self.path == "/admin":
            if check_admin_session(self.headers.get("Cookie")):
                html_response(self, 200, ADMIN_HTML)
            else:
                html_response(self, 200, ADMIN_LOGIN_HTML)
        elif self.path == "/api/admin/data":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            conn = get_db()
            guest_list = [dict(r) for r in conn.execute("SELECT id, name, invite_token, instagram, facebook FROM guest_list ORDER BY name COLLATE NOCASE").fetchall()]
            rsvps = [dict(r) for r in conn.execute("SELECT id, name, status, approved, instagram, facebook, phone, profile_pic, created_at, updated_at FROM rsvps ORDER BY created_at DESC").fetchall()]
            plus_ones = [dict(r) for r in conn.execute("SELECT id, added_by, name, phone, invite_token, approved, created_at FROM plus_ones ORDER BY added_by COLLATE NOCASE, created_at DESC").fetchall()]
            announcements = [dict(r) for r in conn.execute("SELECT id, message, created_at FROM announcements ORDER BY created_at DESC").fetchall()]
            conn.close()
            json_response(self, 200, {"guest_list": guest_list, "rsvps": rsvps, "plus_ones": plus_ones, "announcements": announcements})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rsvp":
            body = read_body(self)
            name = body.get("name", "").strip()
            status = body.get("status", "going")
            instagram = body.get("instagram", "").strip()
            facebook = body.get("facebook", "").strip()
            if not name:
                json_response(self, 400, {"error": "Name is required"})
                return
            if status not in ("going", "maybe", "cant_go"):
                json_response(self, 400, {"error": "Invalid status"})
                return

            conn = get_db()
            on_list = conn.execute("SELECT 1 FROM guest_list WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            is_approved_plus_one = conn.execute("SELECT 1 FROM plus_ones WHERE name = ? COLLATE NOCASE AND approved = 1", (name,)).fetchone()
            approved = 1 if (on_list or is_approved_plus_one) else 0

            existing = conn.execute("SELECT id FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if existing:
                conn.execute("UPDATE rsvps SET status = ?, instagram = ?, facebook = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, instagram, facebook, existing["id"]))
            else:
                conn.execute("INSERT INTO rsvps (name, status, approved, instagram, facebook) VALUES (?, ?, ?, ?, ?)", (name, status, approved, instagram, facebook))
            conn.commit()
            conn.close()

            json_response(self, 200 if existing else 201, {
                "ok": True,
                "approved": bool(approved),
                "updated": bool(existing),
                "status": status,
            })

        elif self.path == "/api/update-socials":
            body = read_body(self)
            name = body.get("name", "").strip()
            instagram = body.get("instagram", "").strip()
            facebook = body.get("facebook", "").strip()
            if not name:
                json_response(self, 400, {"error": "Name is required"})
                return
            conn = get_db()
            existing = conn.execute("SELECT id FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if not existing:
                json_response(self, 404, {"error": "RSVP not found"})
                conn.close()
                return
            conn.execute("UPDATE rsvps SET instagram = ?, facebook = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (instagram, facebook, existing["id"]))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/login":
            if not ADMIN_PASSWORD:
                json_response(self, 500, {"error": "ADMIN_PASSWORD env var not set"})
                return
            body = read_body(self)
            pw = body.get("password", "")
            if pw == ADMIN_PASSWORD:
                token = secrets.token_hex(32)
                admin_sessions[token] = time.time() + 86400  # 24h
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"admin_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
            else:
                json_response(self, 401, {"error": "Wrong password"})

        elif self.path == "/api/admin/guest-list":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            action = body.get("action")
            name = body.get("name", "").strip()
            if not name:
                json_response(self, 400, {"error": "Name is required"})
                return

            conn = get_db()
            if action == "add":
                try:
                    token = secrets.token_urlsafe(12)
                    conn.execute("INSERT INTO guest_list (name, invite_token) VALUES (?, ?)", (name, token))
                    # Auto-approve any existing pending RSVP for this name
                    conn.execute("UPDATE rsvps SET approved = 1 WHERE name = ? COLLATE NOCASE", (name,))
                    conn.commit()
                    json_response(self, 201, {"ok": True})
                except sqlite3.IntegrityError:
                    json_response(self, 409, {"error": "Name already on list"})
            elif action == "remove":
                conn.execute("DELETE FROM guest_list WHERE name = ? COLLATE NOCASE", (name,))
                conn.commit()
                json_response(self, 200, {"ok": True})
            else:
                json_response(self, 400, {"error": "Invalid action"})
            conn.close()

        elif self.path == "/api/admin/approve":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            rsvp_id = body.get("id")
            action = body.get("action")  # "approve" or "reject"
            if not rsvp_id or action not in ("approve", "reject"):
                json_response(self, 400, {"error": "Invalid request"})
                return

            conn = get_db()
            if action == "approve":
                conn.execute("UPDATE rsvps SET approved = 1 WHERE id = ?", (rsvp_id,))
                # Also add to guest list if not there
                row = conn.execute("SELECT name FROM rsvps WHERE id = ?", (rsvp_id,)).fetchone()
                if row:
                    try:
                        conn.execute("INSERT INTO guest_list (name) VALUES (?)", (row["name"],))
                    except sqlite3.IntegrityError:
                        pass
            elif action == "reject":
                conn.execute("UPDATE rsvps SET approved = -1 WHERE id = ?", (rsvp_id,))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/plus-one":
            body = read_body(self)
            added_by = body.get("added_by", "").strip()
            name = body.get("name", "").strip()
            phone = body.get("phone", "").strip()
            if not added_by or not name:
                json_response(self, 400, {"error": "Name is required"})
                return
            if not phone:
                json_response(self, 400, {"error": "Phone number is required for plus ones"})
                return
            conn = get_db()
            existing = conn.execute("SELECT id FROM plus_ones WHERE added_by = ? COLLATE NOCASE AND name = ? COLLATE NOCASE", (added_by, name)).fetchone()
            if existing:
                json_response(self, 409, {"error": f"{name} is already on your plus one list"})
                conn.close()
                return
            conn.execute("INSERT INTO plus_ones (added_by, name, phone) VALUES (?, ?, ?)", (added_by, name, phone))
            conn.commit()
            conn.close()
            json_response(self, 201, {"ok": True})

        elif self.path == "/api/plus-one/remove":
            body = read_body(self)
            plus_one_id = body.get("id")
            added_by = body.get("added_by", "").strip()
            if not plus_one_id or not added_by:
                json_response(self, 400, {"error": "Invalid request"})
                return
            conn = get_db()
            # Only allow removing your own plus ones that are still pending
            conn.execute("DELETE FROM plus_ones WHERE id = ? AND added_by = ? COLLATE NOCASE AND approved = 0", (plus_one_id, added_by))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/approve-plus-one":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            plus_one_id = body.get("id")
            action = body.get("action")
            if not plus_one_id or action not in ("approve", "reject"):
                json_response(self, 400, {"error": "Invalid request"})
                return
            conn = get_db()
            if action == "approve":
                token = secrets.token_urlsafe(12)
                conn.execute("UPDATE plus_ones SET approved = 1, invite_token = ? WHERE id = ?", (token, plus_one_id))
            elif action == "reject":
                conn.execute("UPDATE plus_ones SET approved = -1 WHERE id = ?", (plus_one_id,))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/delete-plus-one":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            plus_one_id = body.get("id")
            name = body.get("name", "").strip()
            conn = get_db()
            if plus_one_id:
                row = conn.execute("SELECT name FROM plus_ones WHERE id = ?", (plus_one_id,)).fetchone()
                if row:
                    name = row["name"]
                conn.execute("DELETE FROM plus_ones WHERE id = ?", (plus_one_id,))
            elif name:
                conn.execute("DELETE FROM plus_ones WHERE name = ? COLLATE NOCASE", (name,))
            else:
                conn.close()
                json_response(self, 400, {"error": "ID or name is required"})
                return
            if name:
                conn.execute("DELETE FROM rsvps WHERE name = ? COLLATE NOCASE", (name,))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/update-phone":
            body = read_body(self)
            name = body.get("name", "").strip()
            phone = body.get("phone", "").strip()
            if not name:
                json_response(self, 400, {"error": "Name is required"})
                return
            conn = get_db()
            existing = conn.execute("SELECT id FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if not existing:
                conn.close()
                json_response(self, 404, {"error": "RSVP not found"})
                return
            conn.execute("UPDATE rsvps SET phone = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (phone, existing["id"]))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/update-guest-socials":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            guest_id = body.get("id")
            instagram = body.get("instagram", "").strip()
            facebook = body.get("facebook", "").strip()
            phone = body.get("phone", "").strip()
            if not guest_id:
                json_response(self, 400, {"error": "Guest ID is required"})
                return
            conn = get_db()
            conn.execute("UPDATE guest_list SET instagram = ?, facebook = ? WHERE id = ?", (instagram, facebook, guest_id))
            guest = conn.execute("SELECT name FROM guest_list WHERE id = ?", (guest_id,)).fetchone()
            if guest:
                rsvp = conn.execute("SELECT id, instagram, facebook, phone FROM rsvps WHERE name = ? COLLATE NOCASE", (guest["name"],)).fetchone()
                if rsvp:
                    updates = []
                    params = []
                    if not rsvp["instagram"] and instagram:
                        updates.append("instagram = ?"); params.append(instagram)
                    if not rsvp["facebook"] and facebook:
                        updates.append("facebook = ?"); params.append(facebook)
                    if phone:
                        updates.append("phone = ?"); params.append(phone)
                    if updates:
                        params.append(rsvp["id"])
                        conn.execute("UPDATE rsvps SET " + ", ".join(updates) + " WHERE id = ?", params)
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/update-plusone-details":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            po_id = body.get("id")
            instagram = body.get("instagram", "").strip()
            facebook = body.get("facebook", "").strip()
            phone = body.get("phone", "").strip()
            if not po_id:
                json_response(self, 400, {"error": "Plus one ID is required"})
                return
            conn = get_db()
            if phone:
                conn.execute("UPDATE plus_ones SET phone = ? WHERE id = ?", (phone, po_id))
            po = conn.execute("SELECT name FROM plus_ones WHERE id = ?", (po_id,)).fetchone()
            if po:
                rsvp = conn.execute("SELECT id FROM rsvps WHERE name = ? COLLATE NOCASE", (po["name"],)).fetchone()
                if rsvp:
                    updates = []
                    params = []
                    if instagram:
                        updates.append("instagram = ?"); params.append(instagram)
                    if facebook:
                        updates.append("facebook = ?"); params.append(facebook)
                    if phone:
                        updates.append("phone = ?"); params.append(phone)
                    if updates:
                        params.append(rsvp["id"])
                        conn.execute("UPDATE rsvps SET " + ", ".join(updates) + " WHERE id = ?", params)
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/upload-photo":
            body = read_body(self)
            name = body.get("name", "").strip()
            photo = body.get("photo", "").strip()
            if not name:
                json_response(self, 400, {"error": "Name is required"})
                return
            if not photo:
                json_response(self, 400, {"error": "Photo is required"})
                return
            if len(photo) > 500 * 1024:
                json_response(self, 400, {"error": "Photo too large (max 500KB)"})
                return
            conn = get_db()
            existing = conn.execute("SELECT id FROM rsvps WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
            if not existing:
                conn.close()
                json_response(self, 404, {"error": "RSVP not found"})
                return
            conn.execute("UPDATE rsvps SET profile_pic = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (photo, existing["id"]))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        elif self.path == "/api/admin/send-alert":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            message = body.get("message", "").strip()
            if not message:
                json_response(self, 400, {"error": "Message is required"})
                return
            conn = get_db()
            recipients = [dict(r) for r in conn.execute("SELECT name, phone FROM rsvps WHERE phone IS NOT NULL AND phone != '' AND approved = 1").fetchall()]
            conn.close()
            print(f"[ALERT] Message: {message}")
            for r in recipients:
                print(f"  -> {r['name']}: {r['phone']}")
            json_response(self, 200, {"ok": True, "sent_to": len(recipients), "recipients": [{"name": r["name"], "phone": r["phone"]} for r in recipients]})

        elif self.path == "/api/admin/announcement":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            message = body.get("message", "").strip()
            if not message:
                json_response(self, 400, {"error": "Message is required"})
                return
            conn = get_db()
            conn.execute("INSERT INTO announcements (message) VALUES (?)", (message,))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if self.path == "/api/admin/announcement":
            if not check_admin_session(self.headers.get("Cookie")):
                json_response(self, 401, {"error": "Unauthorized"})
                return
            body = read_body(self)
            ann_id = body.get("id")
            if not ann_id:
                json_response(self, 400, {"error": "ID is required"})
                return
            conn = get_db()
            conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
            conn.commit()
            conn.close()
            json_response(self, 200, {"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


# ─── HTML Templates ──────────────────────────────────────────────────────────

MAIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Krish and James Party V2</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=DM+Serif+Display&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    background: #f5f3f0;
    color: #1a1a1a;
    min-height: 100vh;
  }

  .page-wrapper {
    max-width: 480px;
    margin: 0 auto;
    min-height: 100vh;
    background: #fff;
    box-shadow: 0 0 40px rgba(0,0,0,0.06);
  }

  .hero {
    position: relative;
    width: 100%;
    height: 280px;
    background: linear-gradient(135deg, #ff6b9d, #c44dff, #6e8efb, #4dc9f6);
    background-size: 300% 300%;
    animation: gradientShift 8s ease infinite;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }

  @keyframes gradientShift {
    0%, 100% { background-position: 0% 50%; }
    50% { background-position: 100% 50%; }
  }

  .hero::before {
    content: '';
    position: absolute;
    inset: 0;
    background:
      radial-gradient(circle at 20% 80%, rgba(255,255,255,0.3) 0%, transparent 50%),
      radial-gradient(circle at 80% 20%, rgba(255,255,255,0.2) 0%, transparent 40%);
  }

  .hero-content { position: relative; text-align: center; color: #fff; padding: 20px; z-index: 2; }
  .hero-emoji { font-size: 56px; margin-bottom: 12px; filter: drop-shadow(0 4px 12px rgba(0,0,0,0.15)); }
  .hero h1 { font-family: 'DM Serif Display', Georgia, serif; font-size: 32px; font-weight: 400; letter-spacing: -0.02em; line-height: 1.15; text-shadow: 0 2px 20px rgba(0,0,0,0.15); }
  .hero .hosted-by { margin-top: 10px; font-size: 14px; font-weight: 500; opacity: 0.9; letter-spacing: 0.03em; }

  /* Bubbles */
  .bubble {
    position: absolute;
    border-radius: 50%;
    pointer-events: none;
    z-index: 1;
    background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.95), rgba(255,255,255,0.15) 60%, rgba(200,220,255,0.1));
    box-shadow: inset 0 -4px 8px rgba(255,255,255,0.15), 0 0 6px rgba(255,255,255,0.2);
    animation: bubbleFly var(--duration) ease-out forwards;
  }
  .bubble::after {
    content: '';
    position: absolute;
    top: 18%;
    left: 22%;
    width: 30%;
    height: 20%;
    background: rgba(255,255,255,0.6);
    border-radius: 50%;
    transform: rotate(-30deg);
  }
  @keyframes bubbleFly {
    0% {
      opacity: 0.9;
      transform: translate(0, 0) scale(0.3);
    }
    20% {
      opacity: 0.85;
    }
    70% {
      opacity: 0.5;
    }
    100% {
      opacity: 0;
      transform: translate(var(--tx), var(--ty)) scale(var(--end-scale));
    }
  }

  .event-details { padding: 20px 24px; border-bottom: 1px solid #eee; display: flex; flex-direction: column; gap: 14px; }
  .detail-row { display: flex; align-items: flex-start; gap: 14px; }
  .detail-icon { width: 40px; height: 40px; border-radius: 12px; background: #f5f3f0; display: flex; align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0; }
  .detail-text { display: flex; flex-direction: column; justify-content: center; min-height: 40px; }
  .detail-label { font-size: 12px; font-weight: 600; color: #999; text-transform: uppercase; letter-spacing: 0.06em; }
  .detail-value { font-size: 15px; font-weight: 500; color: #1a1a1a; margin-top: 1px; }

  .byob-msg { font-size: 14px; color: #777; text-align: center; padding: 10px 0 2px; font-weight: 500; letter-spacing: 0.01em; }

  .tabs { display: flex; border-bottom: 1px solid #eee; position: sticky; top: 0; background: #fff; z-index: 10; }
  .tab-btn { flex: 1; padding: 16px; border: none; background: transparent; font-family: 'DM Sans', sans-serif; font-size: 15px; font-weight: 600; color: #999; cursor: pointer; position: relative; transition: color 0.2s; }
  .tab-btn.active { color: #1a1a1a; }
  .tab-btn.active::after { content: ''; position: absolute; bottom: 0; left: 24px; right: 24px; height: 3px; background: #1a1a1a; border-radius: 3px 3px 0 0; }
  .tab-btn:hover:not(.active) { color: #666; }

  .panel { padding: 24px; }
  .hidden { display: none !important; }

  /* RSVP Form */
  .rsvp-intro { text-align: center; margin-bottom: 28px; }
  .rsvp-intro h2 { font-family: 'DM Serif Display', Georgia, serif; font-size: 24px; font-weight: 400; margin-bottom: 8px; }
  .gradient-name {
    background: linear-gradient(135deg, #ff6b9d, #c44dff, #6e8efb, #4dc9f6);
    background-size: 300% 300%;
    animation: gradientShift 8s ease infinite;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    filter: drop-shadow(0 0 12px rgba(196, 77, 255, 0.4));
  }
  .rsvp-intro p { color: #777; font-size: 15px; line-height: 1.5; }

  .form-group { margin-bottom: 20px; }
  label { display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  input[type="text"] { width: 100%; padding: 16px 18px; font-family: 'DM Sans', sans-serif; font-size: 16px; border: 2px solid #e8e6e3; border-radius: 14px; background: #faf9f7; color: #1a1a1a; outline: none; transition: border-color 0.2s, box-shadow 0.2s; }
  input[type="text"]:focus { border-color: #c44dff; box-shadow: 0 0 0 4px rgba(196, 77, 255, 0.1); background: #fff; }
  input[type="text"]::placeholder { color: #bbb; }

  .returning-name { font-family: 'DM Serif Display', Georgia, serif; font-size: 28px; font-weight: 400; color: #1a1a1a; padding: 8px 0; }
  .returning-name-row { display: flex; align-items: center; gap: 16px; }
  .profile-photo-upload { width: 60px; height: 60px; border-radius: 50%; border: 2px dashed #ccc; display: flex; align-items: center; justify-content: center; cursor: pointer; overflow: hidden; flex-shrink: 0; transition: border-color 0.2s; }
  .profile-photo-upload:hover { border-color: #999; }
  .profile-photo-upload img { width: 100%; height: 100%; object-fit: cover; }

  /* Plus Ones */
  .add-plusone-form { display: flex; gap: 10px; margin-bottom: 20px; }
  .add-plusone-form input { flex: 1; padding: 14px 16px; font-family: 'DM Sans', sans-serif; font-size: 15px; border: 2px solid #e8e6e3; border-radius: 14px; background: #faf9f7; outline: none; transition: border-color 0.2s; }
  .add-plusone-form input:focus { border-color: #c44dff; background: #fff; }
  .add-plusone-form input::placeholder { color: #bbb; }
  .plusone-add-btn { padding: 14px 22px; font-family: 'DM Sans', sans-serif; font-size: 15px; font-weight: 700; border: none; border-radius: 14px; background: #1a1a1a; color: #fff; cursor: pointer; white-space: nowrap; transition: transform 0.15s; }
  .plusone-add-btn:active { transform: scale(0.97); }
  .plusone-item { display: flex; align-items: center; gap: 12px; padding: 14px 0; border-bottom: 1px solid #f0eee9; }
  .plusone-item:last-child { border-bottom: none; }
  .plusone-name { flex: 1; font-size: 15px; font-weight: 500; }
  .plusone-status { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 100px; }
  .plusone-status.pending { background: #fff8e6; color: #9a7b20; }
  .plusone-status.approved { background: #e8f8ef; color: #1a7a42; }
  .plusone-status.denied { background: #fde8e8; color: #c0392b; }
  .plusone-remove { background: none; border: none; color: #c0392b; font-size: 18px; cursor: pointer; padding: 4px 8px; font-weight: 700; opacity: 0.6; transition: opacity 0.2s; }
  .plusone-remove:hover { opacity: 1; }

  .rsvp-buttons { display: flex; gap: 10px; margin-bottom: 8px; }
  .rsvp-btn { flex: 1; padding: 16px 8px; font-family: 'DM Sans', sans-serif; font-size: 14px; font-weight: 700; border: none; border-radius: 14px; cursor: pointer; transition: transform 0.15s, box-shadow 0.2s; display: flex; align-items: center; justify-content: center; gap: 6px; }
  .rsvp-btn:active { transform: scale(0.97); }
  .rsvp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .rsvp-btn.going, .rsvp-btn.maybe-btn, .rsvp-btn.cant-go { background: #fff; color: #999; border: 2px solid #e8e6e3; }
  .rsvp-btn.going:hover:not(:disabled), .rsvp-btn.maybe-btn:hover:not(:disabled), .rsvp-btn.cant-go:hover:not(:disabled) { border-color: #ccc; color: #666; }
  .rsvp-btn.going.selected { background: #e8f8ef; color: #1a7a42; border-color: #27ae60; }
  .rsvp-btn.maybe-btn.selected { background: #fff8e6; color: #9a7b20; border-color: #f1c40f; }
  .rsvp-btn.cant-go.selected { background: #fde8e8; color: #c0392b; border-color: #e74c3c; }

  .status-msg { text-align: center; margin-top: 16px; padding: 14px 18px; border-radius: 12px; font-size: 14px; font-weight: 500; line-height: 1.5; }
  .status-msg.approved { background: #e8f8ef; color: #1a7a42; }
  .status-msg.pending { background: #fff8e6; color: #9a7b20; }
  .status-msg.denied { background: #fde8e8; color: #c0392b; }
  .status-msg.maybe-status { background: #fff8e6; color: #9a7b20; }
  .status-msg.cantgo-status { background: #fde8e8; color: #c0392b; }
  .status-msg.updated { background: #e8f0fe; color: #2a5db0; }

  .error-msg { color: #e74c3c; font-size: 14px; margin-top: 10px; text-align: center; }

  /* Social links section */
  .socials-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid #eee; }
  .socials-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .socials-header h3 { font-size: 15px; font-weight: 700; color: #1a1a1a; }
  .socials-prompt { font-size: 13px; color: #999; margin-bottom: 6px; }
  .socials-subtitle { font-size: 12px; color: #bbb; margin-bottom: 14px; font-style: italic; }
  .social-input-row { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .social-icon-label { width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .social-icon-label.ig { background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888); }
  .social-icon-label.fb { background: #1877f2; }
  .social-icon-label svg { width: 20px; height: 20px; fill: #fff; }
  .social-input { flex: 1; padding: 12px 14px; font-family: 'DM Sans', sans-serif; font-size: 14px; border: 2px solid #e8e6e3; border-radius: 12px; background: #faf9f7; color: #1a1a1a; outline: none; transition: border-color 0.2s; }
  .social-input:focus { border-color: #c44dff; background: #fff; }
  .social-input::placeholder { color: #bbb; }
  .save-socials-btn { width: 100%; padding: 12px; font-family: 'DM Sans', sans-serif; font-size: 14px; font-weight: 600; border: 2px solid #e8e6e3; border-radius: 12px; background: #fff; color: #555; cursor: pointer; transition: all 0.2s; margin-top: 4px; }
  .save-socials-btn:hover { border-color: #c44dff; color: #c44dff; }

  /* Social icons in guest list */
  .guest-socials { display: flex; gap: 6px; align-items: center; }
  .guest-social-link { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 8px; transition: transform 0.15s; text-decoration: none; }
  .guest-social-link:hover { transform: scale(1.1); }
  .guest-social-link.ig-link { background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888); }
  .guest-social-link.fb-link { background: #1877f2; }
  .guest-social-link svg { width: 16px; height: 16px; fill: #fff; }

  /* Guest List */
  .guest-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .guest-header h2 { font-family: 'DM Serif Display', Georgia, serif; font-size: 22px; font-weight: 400; }

  .count-pills { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }
  .count-pill { font-size: 12px; font-weight: 700; padding: 5px 12px; border-radius: 100px; }
  .count-pill.going-pill { background: #e8f8ef; color: #1a7a42; }
  .count-pill.maybe-pill { background: #fff8e6; color: #9a7b20; }
  .count-pill.cant-pill { background: #fde8e8; color: #c0392b; }

  .guest-section-label { font-size: 12px; font-weight: 700; color: #999; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 20px; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #f0eee9; scroll-margin-top: 60px; }
  .guest-section-label:first-child { margin-top: 0; }

  .guest-list { list-style: none; }
  .guest-list li { padding: 12px 0; border-bottom: 1px solid #f0eee9; display: flex; align-items: center; gap: 14px; font-size: 15px; font-weight: 500; }
  .guest-list li:last-child { border-bottom: none; }

  .avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; color: #fff; flex-shrink: 0; }
  .avatar-clickable { cursor: pointer; transition: transform 0.3s ease, box-shadow 0.3s ease; }
  .avatar-clickable:hover { transform: scale(1.1); }
  .avatar-clickable.expanded { transform: scale(2.5); box-shadow: 0 4px 20px rgba(0,0,0,0.3); z-index: 100; position: relative; }
  .guest-name { flex: 1; }
  .guest-badge { font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 100px; }
  .badge-you { color: #2a5db0; background: #e8f0fe; }
  .badge-going { color: #1a7a42; background: #e8f8ef; }
  .badge-maybe { color: #9a7b20; background: #fff8e6; }
  .badge-cant { color: #c0392b; background: #fde8e8; }

  .empty-state { text-align: center; padding: 48px 20px; }
  .empty-state .empty-emoji { font-size: 48px; margin-bottom: 12px; }
  .empty-state p { color: #999; font-size: 15px; }

  .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%) translateY(-100px); background: #1a1a1a; color: #fff; padding: 14px 24px; border-radius: 14px; font-size: 15px; font-weight: 600; box-shadow: 0 8px 32px rgba(0,0,0,0.2); z-index: 100; transition: transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1); display: flex; align-items: center; gap: 10px; max-width: 90vw; text-align: center; }
  .toast.show { transform: translateX(-50%) translateY(0); }

  /* Celebration particles */
  .particle {
    position: fixed;
    pointer-events: none;
    z-index: 200;
    font-size: 24px;
    will-change: transform, opacity;
  }
  .particle-confetti {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    animation: confettiFall var(--fall-duration) ease-in forwards;
  }
  @keyframes confettiFall {
    0% { opacity: 1; transform: translate(0, 0) rotate(0deg) scale(1); }
    100% { opacity: 0; transform: translate(var(--drift), 100vh) rotate(var(--spin)) scale(0.5); }
  }
  .particle-sad {
    animation: sadFall var(--fall-duration) ease-in forwards;
  }
  @keyframes sadFall {
    0% { opacity: 1; transform: translate(0, 0) scale(1); }
    100% { opacity: 0; transform: translate(var(--drift), 100vh) scale(0.3); }
  }
  .particle-maybe {
    animation: maybeFloat var(--float-duration) cubic-bezier(0.12, 0.4, 0.29, 1) forwards;
  }
  @keyframes maybeFloat {
    0% { opacity: 0; transform: translate(0, 0) scale(0.2); }
    10% { opacity: 1; transform: translate(calc(var(--dx) * 0.08), calc(var(--dy) * 0.08)) scale(1.2); }
    35% { opacity: 1; transform: translate(calc(var(--dx) * 0.35), calc(var(--dy) * 0.35)) scale(1.05); }
    65% { opacity: 0.8; transform: translate(calc(var(--dx) * 0.7), calc(var(--dy) * 0.7)) scale(0.9); }
    100% { opacity: 0; transform: translate(var(--dx), var(--dy)) scale(0.3); }
  }

  @media (max-width: 480px) {
    .page-wrapper { box-shadow: none; }
    .hero { height: 240px; }
    .hero h1 { font-size: 28px; }
    .hero-emoji { font-size: 48px; }
    .panel { padding: 20px 18px; }
    .rsvp-buttons { flex-direction: column; gap: 10px; }
  }
</style>
</head>
<body>
<div class="page-wrapper">
  <div class="hero" id="hero">
    <div class="hero-content">
      <div class="hero-emoji">&#127881;</div>
      <h1>Krish and James<br>Party V2</h1>
      <div class="hosted-by">Hosted by Krish & James</div>
    </div>
  </div>

  <div class="event-details">
    <div class="detail-row">
      <div class="detail-icon">&#128197;</div>
      <div class="detail-text"><span class="detail-label">Date</span><span class="detail-value">Saturday, May 2, 2026</span></div>
    </div>
    <div class="detail-row">
      <div class="detail-icon">&#128336;</div>
      <div class="detail-text"><span class="detail-label">Time</span><span class="detail-value">6:30 PM onwards</span></div>
    </div>
    <div class="detail-row">
      <div class="detail-icon">&#128205;</div>
      <div class="detail-text"><span class="detail-label">Location</span><span class="detail-value">50 Hordern St, Newtown</span></div>
    </div>
    <div class="byob-msg">&#127867; BYO drinks! Last time they disappeared faster than the gossip — so bring enough to share (or at least enough to survive) &#128514;</div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" id="tab-rsvp" onclick="showTab('rsvp')">RSVP</button>
    <button class="tab-btn" id="tab-plusones" onclick="showTab('plusones')">Plus Ones</button>
    <button class="tab-btn" id="tab-guests" onclick="showTab('guests')">Guest List</button>
  </div>

  <div class="panel" id="panel-rsvp">
    <div class="rsvp-intro" id="rsvp-intro">
      <h2>You're Invited!</h2>
      <p>Let us know if you can make it. No app download needed.</p>
    </div>
    <form id="rsvp-form" onsubmit="return false;">
      <div class="form-group" id="name-group">
        <label for="name-input">Your Name</label>
        <input type="text" id="name-input" placeholder="Enter your name" autocomplete="off" required>
      </div>
      <div class="rsvp-buttons">
        <button type="button" class="rsvp-btn going" onclick="submitRsvp('going')">Going &#127881;</button>
        <button type="button" class="rsvp-btn maybe-btn" onclick="submitRsvp('maybe')">Maybe &#129300;</button>
        <button type="button" class="rsvp-btn cant-go" onclick="submitRsvp('cant_go')">Can't Go</button>
      </div>
      <div class="error-msg" id="error-msg"></div>
      <div id="status-area"></div>
      <div class="socials-section" id="socials-section">
      <div class="socials-header"><h3>Link your socials</h3></div>
      <p class="socials-prompt">Copy &amp; paste your profile links so other guests can find you! (optional)</p>
      <p class="socials-subtitle">This may have already been filled out for you &#128064;</p>
      <div class="social-input-row">
        <div class="social-icon-label ig"><svg viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg></div>
        <input type="url" class="social-input" id="ig-input" placeholder="https://instagram.com/yourprofile" autocomplete="off">
      </div>
      <div class="social-input-row">
        <div class="social-icon-label fb"><svg viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385h-3.047v-3.47h3.047v-2.642c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953h-1.514c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385c5.737-.9 10.125-5.864 10.125-11.854z"/></svg></div>
        <input type="url" class="social-input" id="fb-input" placeholder="https://facebook.com/yourprofile" autocomplete="off">
      </div>
      <button class="save-socials-btn" onclick="saveSocials()">Save Socials</button>
      </div>
      <div id="announcements-section" style="display:none">
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
        <h3 style="font-size:18px;font-weight:700;margin-bottom:12px">&#128203; Details</h3>
        <div id="announcements-list"></div>
      </div>
    </form>
  </div>

  <div class="panel hidden" id="panel-plusones">
    <div class="rsvp-intro">
      <h2>Bring Friends</h2>
      <p>Add people you'd like to bring along. They'll be added once the host approves.</p>
    </div>
    <div id="plusone-login-prompt" class="empty-state">
      <div class="empty-emoji">&#128100;</div>
      <p>RSVP first to add plus ones.</p>
    </div>
    <div id="plusone-area" class="hidden">
      <div class="add-plusone-form" style="flex-wrap:wrap">
        <input type="text" id="plusone-name" placeholder="Friend's full name" autocomplete="off" style="flex:1;min-width:140px">
        <input type="tel" id="plusone-phone" placeholder="Phone number" autocomplete="off" style="flex:1;min-width:140px;padding:14px 16px;font-family:'DM Sans',sans-serif;font-size:15px;border:2px solid #e8e6e3;border-radius:14px;background:#faf9f7;outline:none">
        <button class="plusone-add-btn" onclick="addPlusOne()">Add</button>
      </div>
      <p style="font-size:12px;color:#aaa;margin:-8px 0 10px 4px">📲 We'll need their number so the host can send them an invite link</p>
      <div id="plusone-list"></div>
    </div>
  </div>

  <div class="panel hidden" id="panel-guests">
    <div class="guest-header">
      <h2>Who's Coming</h2>
    </div>
    <div class="count-pills" id="count-pills"></div>
    <div id="guest-list-container"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  // Ambient bubbles
  (function() {
    const hero = document.getElementById('hero');
    function spawnBubble() {
      const b = document.createElement('div');
      b.className = 'bubble';
      const size = 8 + Math.random() * 30;
      const heroRect = hero.getBoundingClientRect();
      // Start from random position along the bottom half
      const startX = Math.random() * heroRect.width;
      const startY = heroRect.height * 0.5 + Math.random() * heroRect.height * 0.5;
      // Float upward with gentle drift
      const tx = (Math.random() - 0.5) * 120;
      const ty = -(60 + Math.random() * 180);
      const duration = 2.5 + Math.random() * 3;
      const endScale = 1 + Math.random() * 1.2;
      b.style.cssText = `width:${size}px;height:${size}px;left:${startX}px;top:${startY}px;--tx:${tx}px;--ty:${ty}px;--end-scale:${endScale};--duration:${duration}s;`;
      hero.appendChild(b);
      setTimeout(() => b.remove(), duration * 1000);
    }
    setInterval(() => { spawnBubble(); if (Math.random() > 0.4) spawnBubble(); }, 400);
    for (let i = 0; i < 6; i++) setTimeout(spawnBubble, i * 150);
  })();

  const STORAGE_KEY = 'krish_james_party_v2_name';
  const LETTER_COLORS = {A:'#E74C3C',B:'#E67E22',C:'#F1C40F',D:'#2ECC71',E:'#1ABC9C',F:'#3498DB',G:'#9B59B6',H:'#E91E63',I:'#FF5722',J:'#FF9800',K:'#8BC34A',L:'#00BCD4',M:'#673AB7',N:'#F06292',O:'#D32F2F',P:'#FF7043',Q:'#CDDC39',R:'#26A69A',S:'#42A5F5',T:'#7E57C2',U:'#EC407A',V:'#FF8A65',W:'#66BB6A',X:'#FFD700',Y:'#29B6F6',Z:'#AB47BC'};
  const STATUS_LABELS = { going: 'Going', maybe: 'Maybe', cant_go: "Can't Go" };
  const STATUS_EMOJI = { going: '&#127881;', maybe: '&#129300;', cant_go: '&#128532;' };

  function getAvatarColor(name) {
    const letter = (name || '?').charAt(0).toUpperCase();
    return LETTER_COLORS[letter] || '#999';
  }
  function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  function showToast(msg, duration) {
    const t = document.getElementById('toast');
    t.innerHTML = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), duration || 2500);
  }

  function showTab(tab) {
    ['rsvp', 'plusones', 'guests'].forEach(t => {
      document.getElementById('tab-' + t).classList.toggle('active', tab === t);
      document.getElementById('panel-' + t).classList.toggle('hidden', tab !== t);
    });
    if (tab === 'guests') loadGuests();
    if (tab === 'rsvp') refreshMyStatus();
    if (tab === 'plusones') loadPlusOnes();
  }

  function updateSelectedButton(status) {
    document.querySelectorAll('.rsvp-btn').forEach(b => b.classList.remove('selected'));
    if (status === 'going') document.querySelector('.rsvp-btn.going').classList.add('selected');
    else if (status === 'maybe') document.querySelector('.rsvp-btn.maybe-btn').classList.add('selected');
    else if (status === 'cant_go') document.querySelector('.rsvp-btn.cant-go').classList.add('selected');
  }

  async function refreshMyStatus() {
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) return;
    const nameGroup = document.getElementById('name-group');
    const cameraSvg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#ccc" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path><circle cx="12" cy="13" r="4"></circle></svg>';
    nameGroup.innerHTML = '<div class="returning-name-row"><div class="profile-photo-upload" id="profile-photo-circle" onclick="document.getElementById(\'profile-photo-input\').click()">' + cameraSvg + '</div><div class="returning-name">' + escapeHtml(name) + '</div></div><input type="file" id="profile-photo-input" accept="image/*" capture style="display:none" onchange="handleProfilePhoto(this)">';
    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.id = 'name-input';
    hidden.value = name;
    nameGroup.appendChild(hidden);
    try {
      const res = await fetch('/api/my-status?name=' + encodeURIComponent(name));
      const data = await res.json();
      if (data.found) {
        if (data.profile_pic) {
          const circle = document.getElementById('profile-photo-circle');
          circle.innerHTML = '<img src="' + data.profile_pic + '" alt="Profile">';
          circle.style.borderStyle = 'solid';
          circle.style.borderColor = '#e8e6e3';
        }
        const area = document.getElementById('status-area');
        if (data.approved === 1) {
          const statusStyle = data.status === 'maybe' ? 'maybe-status' : data.status === 'cant_go' ? 'cantgo-status' : 'approved';
          const icon = data.status === 'cant_go' ? '&#128532;' : '&#10003;';
          area.innerHTML = '<div class="status-msg ' + statusStyle + '">' + icon + ' You\'re RSVP\'d as <strong>' + STATUS_LABELS[data.status] + '</strong>. You can change your status anytime.</div>';
        } else {
          area.innerHTML = '<div class="status-msg pending">&#9203; Your RSVP is awaiting host approval. We\'ll add you to the list once confirmed.</div>';
        }
        updateSelectedButton(data.status);
        const firstName = name.split(' ')[0];
        document.getElementById('rsvp-intro').querySelector('h2').innerHTML = 'Welcome back, <span class="gradient-name">' + escapeHtml(firstName) + '</span>!';
        document.getElementById('rsvp-intro').querySelector('p').textContent = '';
        // Show socials section and populate
        document.getElementById('socials-section').style.display = '';
        document.getElementById('ig-input').value = data.instagram || data.host_instagram || '';
        document.getElementById('fb-input').value = data.facebook || data.host_facebook || '';
        if (data.host_instagram) {
          const igInput = document.getElementById('ig-input');
          igInput.readOnly = true;
          igInput.style.opacity = '0.6';
          igInput.style.cursor = 'not-allowed';
          igInput.title = 'Pre-filled by the host';
        }
        if (data.host_facebook) {
          const fbInput = document.getElementById('fb-input');
          fbInput.readOnly = true;
          fbInput.style.opacity = '0.6';
          fbInput.style.cursor = 'not-allowed';
          fbInput.title = 'Pre-filled by the host';
        }
      }
      // Check if any plus ones have been responded to (approved or denied)
      const poRes = await fetch('/api/plus-ones?name=' + encodeURIComponent(name));
      const poData = await poRes.json();
      const responded = poData.plus_ones.filter(p => p.approved || p.denied);
      if (responded.length > 0) {
        const area = document.getElementById('status-area');
        area.innerHTML += '<div class="status-msg updated" style="margin-top:10px;cursor:pointer" onclick="showTab(\'plusones\')">&#128172; The host responded to your plus one request. <strong>View Plus Ones &rarr;</strong></div>';
      }
    } catch (e) {}
  }

  async function loadGuests() {
    try {
      const res = await fetch('/api/rsvps');
      const data = await res.json();
      const pills = document.getElementById('count-pills');
      const container = document.getElementById('guest-list-container');
      const myName = localStorage.getItem(STORAGE_KEY);

      pills.innerHTML = '';
      if (data.going_count > 0) pills.innerHTML += '<span class="count-pill going-pill" style="cursor:pointer" onclick="document.getElementById(\'section-going\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.going_count + ' going</span>';
      if (data.maybe_count > 0) pills.innerHTML += '<span class="count-pill maybe-pill" style="cursor:pointer" onclick="document.getElementById(\'section-maybe\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.maybe_count + ' maybe</span>';
      if (data.cant_go_count > 0) pills.innerHTML += '<span class="count-pill cant-pill" style="cursor:pointer" onclick="document.getElementById(\'section-cantgo\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.cant_go_count + " can't go</span>";
      if (data.invited_count > 0) pills.innerHTML += '<span class="count-pill" style="background:#f0eeeb;color:#999;cursor:pointer" onclick="document.getElementById(\'section-invited\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.invited_count + ' invited</span>';

      if (data.guests.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-emoji">&#128064;</div><p>No RSVPs yet.<br>Be the first to join!</p></div>';
        return;
      }

      const going = data.guests.filter(g => g.status === 'going');
      const maybe = data.guests.filter(g => g.status === 'maybe');
      const cant = data.guests.filter(g => g.status === 'cant_go');

      let html = '';
      if (going.length > 0) {
        html += '<div class="guest-section-label" id="section-going">Going &#127881;</div>';
        html += renderGuestList(going, myName);
      }
      if (maybe.length > 0) {
        html += '<div class="guest-section-label" id="section-maybe">Maybe &#129300;</div>';
        html += renderGuestList(maybe, myName);
      }
      if (cant.length > 0) {
        html += '<div class="guest-section-label" id="section-cantgo">Can\'t Go</div>';
        html += renderGuestList(cant, myName);
      }
      const invited = data.invited || [];
      if (invited.length > 0) {
        html += '<div class="guest-section-label" id="section-invited" style="color:#bbb">Invited &#9993;&#65039;</div>';
        html += '<ul class="guest-list">' + invited.map(g => {
          const color = getAvatarColor(g.name);
          const isMe = myName && g.name.toLowerCase() === myName.toLowerCase();
          return '<li style="opacity:0.5"><span class="avatar" style="background:' + color + '">' + escapeHtml(g.name.charAt(0).toUpperCase()) + '</span><span class="guest-name">' + escapeHtml(g.name) + '</span>' + (isMe ? '<span class="guest-badge badge-you">You</span>' : '') + '</li>';
        }).join('') + '</ul>';
      }
      container.innerHTML = html;
    } catch (e) {
      console.error('Failed to load guests', e);
    }
  }

  const IG_SVG = '<svg viewBox="0 0 24 24"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>';
  const FB_SVG = '<svg viewBox="0 0 24 24"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385h-3.047v-3.47h3.047v-2.642c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953h-1.514c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385c5.737-.9 10.125-5.864 10.125-11.854z"/></svg>';

  function renderGuestList(guests, myName) {
    return '<ul class="guest-list">' + guests.map(g => {
      const color = getAvatarColor(g.name);
      const isMe = myName && g.name.toLowerCase() === myName.toLowerCase();
      let socials = '';
      if (g.instagram || g.facebook) {
        socials = '<span class="guest-socials">';
        if (g.instagram) {
          const igUrl = g.instagram.startsWith('http') ? g.instagram : 'https://instagram.com/' + g.instagram;
          socials += '<a class="guest-social-link ig-link" href="' + escapeHtml(igUrl) + '" target="_blank" rel="noopener" title="Instagram">' + IG_SVG + '</a>';
        }
        if (g.facebook) {
          const fbUrl = g.facebook.startsWith('http') ? g.facebook : 'https://facebook.com/' + g.facebook;
          socials += '<a class="guest-social-link fb-link" href="' + escapeHtml(fbUrl) + '" target="_blank" rel="noopener" title="Facebook">' + FB_SVG + '</a>';
        }
        socials += '</span>';
      }
      const avatarHtml = g.profile_pic ? '<span class="avatar avatar-clickable" style="padding:0;overflow:hidden" onclick="expandAvatar(this)"><img src="' + g.profile_pic + '" style="width:100%;height:100%;object-fit:cover;border-radius:50%"></span>' : '<span class="avatar" style="background:' + color + '">' + escapeHtml(g.name.charAt(0).toUpperCase()) + '</span>';
      return '<li>' + avatarHtml + '<span class="guest-name">' + escapeHtml(g.name) + '</span>' + socials + (isMe ? '<span class="guest-badge badge-you">You</span>' : '') + '</li>';
    }).join('') + '</ul>';
  }

  function expandAvatar(el) {
    if (el.classList.contains('expanded')) {
      el.classList.remove('expanded');
    } else {
      document.querySelectorAll('.avatar-clickable.expanded').forEach(a => a.classList.remove('expanded'));
      el.classList.add('expanded');
    }
  }
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.avatar-clickable')) {
      document.querySelectorAll('.avatar-clickable.expanded').forEach(a => a.classList.remove('expanded'));
    }
  });

  async function handleProfilePhoto(input) {
    if (!input.files || !input.files[0]) return;
    const file = input.files[0];
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) return;
    const reader = new FileReader();
    reader.onload = function(e) {
      const img = new Image();
      img.onload = function() {
        const canvas = document.createElement('canvas');
        let w = img.width, h = img.height;
        const maxDim = 200;
        if (w > h) { if (w > maxDim) { h = h * maxDim / w; w = maxDim; } }
        else { if (h > maxDim) { w = w * maxDim / h; h = maxDim; } }
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        const base64 = canvas.toDataURL('image/jpeg', 0.7);
        fetch('/api/upload-photo', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name, photo: base64 })
        }).then(res => {
          if (res.ok) {
            const circle = document.getElementById('profile-photo-circle');
            circle.innerHTML = '<img src="' + base64 + '" alt="Profile">';
            circle.style.borderStyle = 'solid';
            circle.style.borderColor = '#e8e6e3';
            showToast('&#10003; Photo uploaded!');
          } else { showToast('Could not upload photo'); }
        }).catch(() => showToast('Something went wrong'));
      };
      img.src = e.target.result;
    };
    reader.readAsDataURL(file);
  }

  async function saveSocials() {
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) return;
    const instagram = document.getElementById('ig-input').value.trim();
    const facebook = document.getElementById('fb-input').value.trim();
    try {
      const res = await fetch('/api/update-socials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, instagram, facebook })
      });
      if (res.ok) showToast('&#10003; Socials saved!');
      else showToast('Could not save — RSVP first');
    } catch (e) { showToast('Something went wrong'); }
  }



  async function submitRsvp(status) {
    const input = document.getElementById('name-input');
    const errEl = document.getElementById('error-msg');
    const name = input.value.trim();
    errEl.textContent = '';

    if (!name) { errEl.textContent = 'Please enter your name'; return; }

    const buttons = document.querySelectorAll('.rsvp-btn');
    buttons.forEach(b => b.disabled = true);

    try {
      const res = await fetch('/api/rsvp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, status })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Something went wrong');

      localStorage.setItem(STORAGE_KEY, name);

      // Trigger celebration animation
      if (status === 'going') confettiShower();
      else if (status === 'cant_go') sadShower();
      else if (status === 'maybe') maybeExplosion();

      // Update selected button state
      updateSelectedButton(status);

      if (data.approved) {
        if (data.updated) {
          showToast('&#10003; Status updated to ' + STATUS_LABELS[status]);
        } else {
          showToast('&#127881; You\'re on the list!');
        }
      } else {
        showToast('&#9203; RSVP submitted — awaiting approval', 3500);
      }
      refreshMyStatus();
      // Show socials section after first RSVP
      document.getElementById('socials-section').style.display = '';
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      buttons.forEach(b => b.disabled = false);
    }
  }

  // Celebration animations
  const CONFETTI_COLORS = ['#ff6b9d','#c44dff','#6e8efb','#4dc9f6','#f39c12','#27ae60','#e74c3c','#fdcb6e','#00b894','#e84393'];

  function confettiShower() {
    const champagneEmojis = ['\u{1F37E}', '\u{1F942}', '\u{2728}', '\u{1F389}', '\u{1F38A}', '\u{2B50}'];
    // Wave 1: Big champagne burst
    for (let i = 0; i < 15; i++) {
      setTimeout(() => {
        const el = document.createElement('div');
        el.className = 'particle particle-sad';
        el.textContent = champagneEmojis[Math.floor(Math.random() * champagneEmojis.length)];
        el.style.left = Math.random() * 100 + 'vw';
        el.style.top = -30 + 'px';
        el.style.fontSize = (28 + Math.random() * 20) + 'px';
        const duration = 3 + Math.random() * 2;
        el.style.setProperty('--fall-duration', duration + 's');
        el.style.setProperty('--drift', (Math.random() - 0.5) * 120 + 'px');
        document.body.appendChild(el);
        setTimeout(() => el.remove(), duration * 1000);
      }, Math.random() * 500);
    }
    // Continuous confetti over 4 seconds — multiple waves
    function spawnConfettiBatch(count, delayBase) {
      for (let i = 0; i < count; i++) {
        setTimeout(() => {
          const el = document.createElement('div');
          el.className = 'particle particle-confetti';
          el.style.left = Math.random() * 100 + 'vw';
          el.style.top = -10 + 'px';
          el.style.background = CONFETTI_COLORS[Math.floor(Math.random() * CONFETTI_COLORS.length)];
          el.style.width = (6 + Math.random() * 10) + 'px';
          el.style.height = (6 + Math.random() * 10) + 'px';
          el.style.borderRadius = Math.random() > 0.5 ? '50%' : '2px';
          const duration = 2.5 + Math.random() * 2;
          el.style.setProperty('--fall-duration', duration + 's');
          el.style.setProperty('--drift', (Math.random() - 0.5) * 200 + 'px');
          el.style.setProperty('--spin', (Math.random() * 1080 - 540) + 'deg');
          document.body.appendChild(el);
          setTimeout(() => el.remove(), duration * 1000);
        }, delayBase + Math.random() * 800);
      }
    }
    spawnConfettiBatch(50, 0);
    spawnConfettiBatch(40, 1000);
    spawnConfettiBatch(35, 2000);
    spawnConfettiBatch(25, 3000);
    // Sprinkle more champagne throughout
    for (let i = 0; i < 12; i++) {
      setTimeout(() => {
        const el = document.createElement('div');
        el.className = 'particle particle-sad';
        el.textContent = champagneEmojis[Math.floor(Math.random() * champagneEmojis.length)];
        el.style.left = Math.random() * 100 + 'vw';
        el.style.top = -30 + 'px';
        el.style.fontSize = (24 + Math.random() * 16) + 'px';
        const duration = 3 + Math.random() * 2;
        el.style.setProperty('--fall-duration', duration + 's');
        el.style.setProperty('--drift', (Math.random() - 0.5) * 100 + 'px');
        document.body.appendChild(el);
        setTimeout(() => el.remove(), duration * 1000);
      }, 1500 + Math.random() * 2500);
    }
  }

  function sadShower() {
    const emojis = ['\u{1F622}', '\u{1F62D}', '\u{1F625}', '\u{1F62A}', '\u{1F614}', '\u{1F44E}', '\u{1F44E}', '\u{1F44E}'];
    for (let i = 0; i < 30; i++) {
      setTimeout(() => {
        const el = document.createElement('div');
        el.className = 'particle particle-sad';
        el.textContent = emojis[Math.floor(Math.random() * emojis.length)];
        el.style.left = Math.random() * 100 + 'vw';
        el.style.top = -30 + 'px';
        el.style.fontSize = (18 + Math.random() * 16) + 'px';
        const duration = 2 + Math.random() * 1.5;
        el.style.setProperty('--fall-duration', duration + 's');
        el.style.setProperty('--drift', (Math.random() - 0.5) * 100 + 'px');
        document.body.appendChild(el);
        setTimeout(() => el.remove(), duration * 1000);
      }, Math.random() * 800);
    }
  }

  function maybeExplosion() {
    const btn = document.querySelector('.rsvp-btn.maybe-btn');
    const rect = btn.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const emojis = ['\u{2753}', '\u{2753}', '\u{2753}', '\u{2754}', '\u{2754}', '\u{2754}', '\u{1F914}', '\u{1F615}'];
    // Slow-motion explosion waves
    function spawnWave(count, delayBase) {
      for (let i = 0; i < count; i++) {
        setTimeout(() => {
          const el = document.createElement('div');
          el.className = 'particle particle-maybe';
          el.textContent = emojis[Math.floor(Math.random() * emojis.length)];
          el.style.left = cx + 'px';
          el.style.top = cy + 'px';
          el.style.fontSize = (20 + Math.random() * 18) + 'px';
          const angle = Math.random() * Math.PI * 2;
          const dist = 80 + Math.random() * 180;
          const duration = 2.5 + Math.random() * 1.5;
          el.style.setProperty('--float-duration', duration + 's');
          el.style.setProperty('--dx', Math.cos(angle) * dist + 'px');
          el.style.setProperty('--dy', Math.sin(angle) * dist + 'px');
          document.body.appendChild(el);
          setTimeout(() => el.remove(), duration * 1000);
        }, delayBase + Math.random() * 500);
      }
    }
    spawnWave(5, 0);
    spawnWave(4, 600);
  }

  // Plus Ones
  async function loadPlusOnes() {
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) {
      document.getElementById('plusone-login-prompt').classList.remove('hidden');
      document.getElementById('plusone-area').classList.add('hidden');
      return;
    }
    document.getElementById('plusone-login-prompt').classList.add('hidden');
    document.getElementById('plusone-area').classList.remove('hidden');
    try {
      const res = await fetch('/api/plus-ones?name=' + encodeURIComponent(name));
      const data = await res.json();
      const list = document.getElementById('plusone-list');
      if (data.plus_ones.length === 0) {
        list.innerHTML = '<div class="empty-state" style="padding:24px 0"><p style="color:#999;font-size:14px">No plus ones yet. Add friends you\'d like to bring!</p></div>';
      } else {
        list.innerHTML = data.plus_ones.map(p => {
          const color = getAvatarColor(p.name);
          const statusClass = p.denied ? 'denied' : p.approved ? 'approved' : 'pending';
          const statusText = p.denied ? 'Denied' : p.approved ? 'Approved' : 'Pending';
          const removeBtn = (p.approved || p.denied) ? '' : '<button class="plusone-remove" onclick="removePlusOne(' + p.id + ')" title="Remove">&times;</button>';
          return '<div class="plusone-item"><span class="avatar" style="background:' + color + ';width:36px;height:36px;font-size:14px">' + escapeHtml(p.name.charAt(0).toUpperCase()) + '</span><span class="plusone-name">' + escapeHtml(p.name) + '</span><span class="plusone-status ' + statusClass + '">' + statusText + '</span>' + removeBtn + '</div>';
        }).join('');
      }
    } catch (e) { console.error('Failed to load plus ones', e); }
  }

  async function addPlusOne() {
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) return;
    const input = document.getElementById('plusone-name');
    const phoneInput = document.getElementById('plusone-phone');
    const friendName = input.value.trim();
    const friendPhone = phoneInput.value.trim();
    if (!friendName) { showToast('Please enter their name'); return; }
    const nameParts = friendName.split(/\s+/);
    if (nameParts.length < 2 || nameParts[nameParts.length - 1].length < 2) {
      showToast('Please enter their full name (first & last, at least 2 characters each)');
      return;
    }
    if (!friendPhone) { showToast('Phone number is required for plus ones'); return; }
    try {
      const res = await fetch('/api/plus-one', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ added_by: name, name: friendName, phone: friendPhone })
      });
      const data = await res.json();
      if (!res.ok) { showToast(data.error || 'Failed to add'); return; }
      input.value = '';
      phoneInput.value = '';
      showToast('&#10003; ' + escapeHtml(friendName) + ' added — pending approval');
      loadPlusOnes();
    } catch (e) { showToast('Something went wrong'); }
  }

  async function removePlusOne(id) {
    const name = localStorage.getItem(STORAGE_KEY);
    if (!name) return;
    try {
      await fetch('/api/plus-one/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, added_by: name })
      });
      loadPlusOnes();
    } catch (e) {}
  }

  // Enter key in plusone input
  document.getElementById('plusone-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); document.getElementById('plusone-phone').focus(); }
  });
  document.getElementById('plusone-phone').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addPlusOne(); }
  });

  async function loadAnnouncements() {
    try {
      const res = await fetch('/api/announcements');
      const data = await res.json();
      const list = document.getElementById('announcements-list');
      const section = document.getElementById('announcements-section');
      if (data.announcements && data.announcements.length > 0) {
        section.style.display = '';
        list.innerHTML = data.announcements.map(a => {
          const d = new Date(a.created_at);
          const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
          const h = d.getHours(); const m = d.getMinutes();
          const ampm = h >= 12 ? 'PM' : 'AM';
          const h12 = h % 12 || 12;
          const timeStr = months[d.getMonth()] + ' ' + d.getDate() + ' at ' + h12 + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
          return '<div style="background:#f9f8f6;border-radius:12px;padding:14px 16px;margin-bottom:10px"><div style="font-size:14px;line-height:1.5">' + escapeHtml(a.message) + '</div><div style="font-size:11px;color:#999;margin-top:6px">' + timeStr + '</div></div>';
        }).join('');
      } else {
        section.style.display = 'none';
      }
    } catch (e) { console.error('Failed to load announcements', e); }
  }

  // On load
  const savedName = localStorage.getItem(STORAGE_KEY);
  if (savedName) {
    document.getElementById('name-input').value = savedName;
    refreshMyStatus();
  }
  loadAnnouncements();
</script>
</body>
</html>
"""


ADMIN_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Login — Party V2</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'DM Sans', sans-serif; background: #f5f3f0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .login-card { background: #fff; border-radius: 20px; padding: 40px 32px; max-width: 400px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.08); text-align: center; }
  .login-card h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
  .login-card p { color: #777; font-size: 14px; margin-bottom: 28px; }
  input[type="password"] { width: 100%; padding: 16px 18px; font-family: 'DM Sans', sans-serif; font-size: 16px; border: 2px solid #e8e6e3; border-radius: 14px; background: #faf9f7; outline: none; margin-bottom: 16px; transition: border-color 0.2s; }
  input[type="password"]:focus { border-color: #c44dff; background: #fff; }
  .login-btn { width: 100%; padding: 16px; font-family: 'DM Sans', sans-serif; font-size: 16px; font-weight: 700; border: none; border-radius: 14px; background: #1a1a1a; color: #fff; cursor: pointer; transition: transform 0.15s; }
  .login-btn:active { transform: scale(0.97); }
  .error { color: #e74c3c; font-size: 14px; margin-top: 12px; }
</style>
</head>
<body>
<div class="login-card">
  <h1>Admin Login</h1>
  <p>Enter the admin password to manage your event.</p>
  <form onsubmit="login(event)">
    <input type="password" id="pw" placeholder="Password" autofocus required>
    <button type="submit" class="login-btn">Sign In</button>
    <div class="error" id="err"></div>
  </form>
</div>
<script>
  async function login(e) {
    e.preventDefault();
    const pw = document.getElementById('pw').value;
    const res = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw })
    });
    if (res.ok) {
      location.reload();
    } else {
      const d = await res.json();
      document.getElementById('err').textContent = d.error || 'Login failed';
    }
  }
</script>
</body>
</html>
"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin — Krish and James Party V2</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Serif+Display&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'DM Sans', sans-serif; background: #f5f3f0; color: #1a1a1a; min-height: 100vh; padding: 20px; }

  .admin-wrapper { max-width: 700px; margin: 0 auto; }
  .admin-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
  .admin-header h1 { font-family: 'DM Serif Display', Georgia, serif; font-size: 28px; font-weight: 400; }
  .admin-header a { font-size: 14px; font-weight: 600; color: #c44dff; text-decoration: none; }

  .card { background: #fff; border-radius: 16px; padding: 24px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); margin-bottom: 20px; }
  .card h2 { font-size: 18px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }

  .add-form { display: flex; gap: 10px; margin-bottom: 16px; }
  .add-form input { flex: 1; padding: 12px 16px; font-family: 'DM Sans', sans-serif; font-size: 15px; border: 2px solid #e8e6e3; border-radius: 12px; background: #faf9f7; outline: none; }
  .add-form input:focus { border-color: #c44dff; background: #fff; }
  .add-form button { padding: 12px 20px; font-family: 'DM Sans', sans-serif; font-size: 14px; font-weight: 700; border: none; border-radius: 12px; background: #1a1a1a; color: #fff; cursor: pointer; white-space: nowrap; }

  .chip-list { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .chip { display: flex; align-items: center; gap: 6px; background: #f0eee9; padding: 8px 14px; border-radius: 100px; font-size: 14px; font-weight: 500; }
  .chip .remove { background: none; border: none; color: #c0392b; font-size: 16px; cursor: pointer; padding: 0 2px; font-weight: 700; margin-left: auto; }

  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; font-size: 12px; font-weight: 700; color: #999; text-transform: uppercase; letter-spacing: 0.05em; padding: 8px 12px; border-bottom: 2px solid #eee; }
  td { padding: 12px; border-bottom: 1px solid #f0eee9; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }

  .status-badge { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 100px; display: inline-block; }
  .status-going { background: #e8f8ef; color: #1a7a42; }
  .status-maybe { background: #fff8e6; color: #9a7b20; }
  .status-cant_go { background: #fde8e8; color: #c0392b; }
  .status-pending { background: #f0eee9; color: #777; }
  .status-denied { background: #fde8e8; color: #c0392b; }

  .action-btn { padding: 6px 14px; font-family: 'DM Sans', sans-serif; font-size: 13px; font-weight: 700; border: none; border-radius: 8px; cursor: pointer; transition: transform 0.1s; }
  .action-btn:active { transform: scale(0.95); }
  .approve-btn { background: #e8f8ef; color: #1a7a42; }
  .reject-btn { background: #fde8e8; color: #c0392b; }

  .actions { display: flex; gap: 6px; }
  .empty-text { color: #999; font-size: 14px; padding: 20px 0; text-align: center; }

  .admin-tabs { display: flex; gap: 0; background: #fff; border-radius: 12px; overflow: hidden; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.04); }
  .admin-tab { flex: 1; padding: 14px 16px; border: none; background: transparent; font-family: 'DM Sans', sans-serif; font-size: 15px; font-weight: 600; color: #999; cursor: pointer; position: relative; transition: color 0.2s, background 0.2s; }
  .admin-tab.active { color: #1a1a1a; background: #f0eee9; }
  .admin-tab:hover:not(.active) { color: #666; }
  .admin-panel { display: none; }
  .admin-panel.active { display: block; }

  .invite-link-cell { display: flex; align-items: center; gap: 6px; }
  .invite-url { font-size: 11px; color: #777; word-break: break-all; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; background: #faf9f7; padding: 6px 8px; border-radius: 8px; border: 1px solid #e8e6e3; font-family: monospace; }
  .copy-btn { padding: 6px 14px; font-family: 'DM Sans', sans-serif; font-size: 12px; font-weight: 700; border: 2px solid #e8e6e3; border-radius: 8px; background: #fff; color: #555; cursor: pointer; transition: all 0.2s; white-space: nowrap; min-width: 64px; text-align: center; }
  .copy-btn:hover { border-color: #c44dff; color: #c44dff; }
  .copy-btn.copied { border-color: #27ae60; color: #27ae60; background: #e8f8ef; }
  .no-rsvp { color: #bbb; font-style: italic; }
  .count-pills { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; }

  .plusone-group { margin-bottom: 20px; padding: 16px; background: #faf9f7; border-radius: 12px; }
  .plusone-group-header { font-size: 15px; font-weight: 700; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .plusone-group-header .inviter { color: #c44dff; }
  .plusone-group-list { display: flex; flex-direction: column; gap: 8px; }
  .plusone-group-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: #fff; border-radius: 10px; }
  .plusone-group-item .po-name { flex: 1; font-size: 14px; font-weight: 500; }

  .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%) translateY(-100px); background: #1a1a1a; color: #fff; padding: 12px 20px; border-radius: 12px; font-size: 14px; font-weight: 600; box-shadow: 0 8px 32px rgba(0,0,0,0.2); z-index: 100; transition: transform 0.4s cubic-bezier(0.34, 1.56, 0.64, 1); }
  .toast.show { transform: translateX(-50%) translateY(0); }

  /* Guest list styles (shared with public site) */
  .guest-header { margin-bottom: 12px; }
  .guest-header h2 { font-family: 'DM Serif Display', Georgia, serif; font-size: 22px; font-weight: 400; }
  .count-pill { font-size: 12px; font-weight: 700; padding: 5px 12px; border-radius: 100px; }
  .count-pill.going-pill { background: #e8f8ef; color: #1a7a42; }
  .count-pill.maybe-pill { background: #fff8e6; color: #9a7b20; }
  .count-pill.cant-pill { background: #fde8e8; color: #c0392b; }
  .guest-section-label { font-size: 12px; font-weight: 700; color: #999; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 20px; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #f0eee9; scroll-margin-top: 60px; }
  .guest-section-label:first-child { margin-top: 0; }
  .guest-list { list-style: none; }
  .guest-list li { padding: 12px 0; border-bottom: 1px solid #f0eee9; display: flex; align-items: center; gap: 14px; font-size: 15px; font-weight: 500; }
  .guest-list li:last-child { border-bottom: none; }
  .avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; color: #fff; flex-shrink: 0; }
  .avatar-clickable { cursor: pointer; transition: transform 0.3s ease, box-shadow 0.3s ease; }
  .avatar-clickable:hover { transform: scale(1.1); }
  .avatar-clickable.expanded { transform: scale(2.5); box-shadow: 0 4px 20px rgba(0,0,0,0.3); z-index: 100; position: relative; }
  .guest-name { flex: 1; }
  .guest-socials { display: flex; gap: 6px; align-items: center; }
  .guest-social-link { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 8px; transition: transform 0.15s; text-decoration: none; }
  .guest-social-link:hover { transform: scale(1.1); }
  .guest-social-link.ig-link { background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888); }
  .guest-social-link.fb-link { background: #1877f2; }
  .guest-social-link svg { width: 16px; height: 16px; fill: #fff; }
  .guest-badge { font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 100px; }
  .badge-you { background: #f0eee9; color: #999; }

  @media (max-width: 600px) {
    body { padding: 12px; }
    .card { padding: 18px; }
    .add-form { flex-direction: column; }
    table { font-size: 13px; }
    th, td { padding: 8px 6px; }
    .invite-url { max-width: 120px; font-size: 11px; }
  }
</style>
</head>
<body>
<div class="admin-wrapper">
  <div class="admin-header">
    <h1>Event Admin</h1>
    <a href="/">&#8592; Back to event</a>
  </div>

  <div class="admin-tabs">
    <button class="admin-tab active" onclick="showAdminTab('manage')">Manage</button>
    <button class="admin-tab" onclick="showAdminTab('invitelinks')">Guest Invite Links</button>
    <button class="admin-tab" onclick="showAdminTab('plusonelinks')">Plus One Links</button>
    <button class="admin-tab" onclick="showAdminTab('guestlist')">Guest List</button>
  </div>

  <!-- MANAGE TAB -->
  <div class="admin-panel active" id="panel-manage">
    <!-- Guest List Management -->
    <div class="card">
      <h2>&#128203; Approved Guest List</h2>
      <div class="add-form">
        <input type="text" id="add-name" placeholder="Add a name to the guest list">
        <button onclick="addGuest()">Add</button>
      </div>
      <div class="chip-list" id="guest-chips"></div>
    </div>

    <!-- Plus Ones -->
    <div class="card" id="plusones-card">
      <h2>&#128101; Plus Ones</h2>
      <div id="plusones-area"></div>
    </div>

    <div class="card">
      <h3>&#128203; Party Updates</h3>
      <p style="color:#999;font-size:13px;margin-bottom:16px">Post updates that all guests will see on their RSVP page</p>
      <div style="display:flex;gap:8px;margin-bottom:16px">
        <textarea id="announcement-input" placeholder="e.g. It's raining — bring jackets! &#9748;&#65039;" style="flex:1;padding:10px 14px;font-size:14px;border:1px solid #e8e6e3;border-radius:12px;resize:vertical;min-height:60px;font-family:inherit"></textarea>
        <button onclick="postAnnouncement()" style="padding:10px 20px;background:#222;color:#fff;border:none;border-radius:12px;font-weight:600;cursor:pointer;white-space:nowrap;align-self:flex-start">Post</button>
      </div>
      <div id="admin-announcements-list"></div>
    </div>
  </div>

  <!-- GUEST INVITE LINKS TAB -->
  <div class="admin-panel" id="panel-invitelinks">
    <div class="card">
      <h2>&#128279; Guest Invite Links</h2>
      <p style="color:#777;font-size:14px;margin-bottom:16px;">Share unique invite links with each guest. Their name will be pre-filled when they open the link.</p>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th style="width:30px">#</th><th>Guest Name</th><th>RSVP Status</th><th>Invite Link</th></tr></thead>
          <tbody id="invite-table"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- PLUS ONE INVITE LINKS TAB -->
  <div class="admin-panel" id="panel-plusonelinks">
    <div class="card">
      <h2>&#128279; Plus One Invite Links</h2>
      <p style="color:#777;font-size:14px;margin-bottom:16px;">Share invite links with approved plus ones so they can see party details and update their RSVP.</p>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th style="width:30px">#</th><th>Guest Name</th><th>RSVP Status</th><th>Invite Link</th></tr></thead>
          <tbody id="plusone-invite-table"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- GUEST LIST TAB (read-only public view) -->
  <div class="admin-panel" id="panel-guestlist">
    <div class="guest-header">
      <h2>Who's Coming</h2>
    </div>
    <div class="count-pills" id="admin-count-pills"></div>
    <div id="admin-guest-list-container"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2000);
  }

  async function loadData() {
    const res = await fetch('/api/admin/data');
    if (res.status === 401) { location.reload(); return; }
    const data = await res.json();
    _adminData = data;

    // Guest list chips
    const chips = document.getElementById('guest-chips');
    const approvedPlusOnes = (data.plus_ones || []).filter(p => p.approved == 1);
    const guestNames = new Set(data.guest_list.map(g => g.name.toLowerCase()));
    const poNames = new Set(approvedPlusOnes.map(p => p.name.toLowerCase()));
    const approvedRsvpsNotListed = data.rsvps.filter(r => r.approved == 1 && !guestNames.has(r.name.toLowerCase()) && !poNames.has(r.name.toLowerCase()));
    const allApproved = [
      ...data.guest_list.map(g => ({ name: g.name, type: 'guest' })),
      ...approvedPlusOnes.map(p => ({ name: p.name, type: 'plusone' })),
      ...approvedRsvpsNotListed.map(r => ({ name: r.name, type: 'plusone' }))
    ];
    if (allApproved.length === 0) {
      chips.innerHTML = '<span class="empty-text">No guests added yet. Add names above.</span>';
    } else {
      chips.innerHTML = allApproved.map(g =>
        '<span class="chip' + (g.type === 'plusone' ? ' chip-plusone' : '') + '">' + esc(g.name) + (g.type === 'plusone' ? ' <span style="font-size:10px;color:#999">+1</span>' : '') + ' <button class="remove" data-name="' + esc(g.name).replace(/"/g, '&quot;') + '" data-type="' + g.type + '">&times;</button></span>'
      ).join('');
      chips.querySelectorAll('.remove[data-name]').forEach(btn => {
        btn.onclick = async () => {
          const gName = btn.getAttribute('data-name');
          if (btn.getAttribute('data-type') === 'plusone') {
            if (confirm('Remove "' + gName + '" from the approved list? This will also remove their RSVP.')) {
              await fetch('/api/admin/delete-plus-one', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name: gName }) });
              loadAdminData();
            }
          } else {
            removeGuest(gName);
          }
        };
      });
    }

    // Refresh invite table if visible
    renderInviteTable();

    // Plus ones grouped by inviter
    const plusOnes = data.plus_ones || [];
    const poArea = document.getElementById('plusones-area');
    if (plusOnes.length === 0) {
      poArea.innerHTML = '<p class="empty-text">No plus ones submitted yet</p>';
    } else {
      // Group by added_by
      const groups = {};
      plusOnes.forEach(p => {
        const key = p.added_by;
        if (!groups[key]) groups[key] = [];
        groups[key].push(p);
      });
      let html = '';
      Object.keys(groups).sort().forEach(inviter => {
        const items = groups[inviter];
        const names = items.map(p => esc(p.name)).join(', ');
        html += '<div class="plusone-group">';
        html += '<div class="plusone-group-header"><span class="inviter">' + esc(inviter) + '</span> is bringing:</div>';
        html += '<div class="plusone-group-list">';
        items.forEach(p => {
          const badge = p.approved === 1
            ? '<span class="status-badge status-going">Approved</span>'
            : p.approved === -1
            ? '<span class="status-badge status-denied">Denied</span>'
            : '<span class="status-badge status-pending">Pending</span>';
          let actions = '<div class="actions">';
          if (p.approved !== 1) actions += '<button class="action-btn approve-btn" onclick="approvePlusOne(' + p.id + ')">Approve</button>';
          if (p.approved !== -1) actions += '<button class="action-btn reject-btn" onclick="rejectPlusOne(' + p.id + ')">Deny</button>';
          actions += '</div>';
          const phoneInfo = p.phone ? '<span style="font-size:12px;color:#777;margin-left:4px">(' + esc(p.phone) + ')</span>' : '';
          const removeBtn = '<button onclick="adminDeletePlusOne(' + p.id + ')" style="background:none;border:none;color:#c0392b;font-size:18px;cursor:pointer;padding:0 4px;font-weight:700" title="Remove plus one">&times;</button>';
          html += '<div class="plusone-group-item"><span class="po-name">' + esc(p.name) + phoneInfo + '</span>' + badge + actions + removeBtn + '</div>';
        });
        html += '</div></div>';
      });
      poArea.innerHTML = html;
    }

    renderAnnouncements();
  }

  function statusLabel(s) {
    return { going: 'Going', maybe: 'Maybe', cant_go: "Can't Go" }[s] || s;
  }
  function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  async function addGuest() {
    const input = document.getElementById('add-name');
    const name = input.value.trim();
    if (!name) return;
    const res = await fetch('/api/admin/guest-list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'add', name })
    });
    const d = await res.json();
    if (res.ok) {
      input.value = '';
      showToast('Added ' + name);
      loadData();
    } else {
      showToast(d.error || 'Failed');
    }
  }

  async function removeGuest(name) {
    if (!confirm('Remove "' + name + '" from the guest list?')) return;
    await fetch('/api/admin/guest-list', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'remove', name })
    });
    showToast('Removed ' + name);
    loadData();
  }

  async function approveRsvp(id) {
    await fetch('/api/admin/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action: 'approve' })
    });
    showToast('Approved');
    loadData();
  }

  async function rejectRsvp(id) {
    if (!confirm('Reject this RSVP? This will delete it.')) return;
    await fetch('/api/admin/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action: 'reject' })
    });
    showToast('Rejected');
    loadData();
  }

  async function approvePlusOne(id) {
    await fetch('/api/admin/approve-plus-one', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action: 'approve' })
    });
    showToast('Plus one approved');
    loadData();
  }

  async function rejectPlusOne(id) {
    if (!confirm('Reject this plus one?')) return;
    await fetch('/api/admin/approve-plus-one', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, action: 'reject' })
    });
    showToast('Plus one rejected');
    loadData();
  }

  async function adminDeletePlusOne(id) {
    if (!confirm('Remove this plus one completely?')) return;
    await fetch('/api/admin/delete-plus-one', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id })
    });
    showToast('Plus one removed');
    loadData();
  }

  function showAdminTab(tab) {
    const tabs = ['manage','invitelinks','plusonelinks','guestlist'];
    document.querySelectorAll('.admin-tab').forEach((t, i) => {
      t.classList.toggle('active', tab === tabs[i]);
    });
    tabs.forEach(t => {
      const panel = document.getElementById('panel-' + t);
      if (panel) panel.classList.toggle('active', tab === t);
    });
    if (tab === 'invitelinks') renderInviteTable();
    if (tab === 'plusonelinks') renderPlusOneInviteTable();
    if (tab === 'guestlist') { renderAdminGuestList(); }
  }

  let _adminData = null;

  function renderInviteTable() {
    if (!_adminData) return;
    const tbody = document.getElementById('invite-table');
    const guests = _adminData.guest_list;
    const rsvps = _adminData.rsvps;
    const baseUrl = location.origin;

    if (guests.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-text">No guests added yet. Add guests in the Manage tab.</td></tr>';
      return;
    }

    tbody.innerHTML = guests.map((g, idx) => {
      const rsvp = rsvps.find(r => r.name.toLowerCase() === g.name.toLowerCase());
      const statusHtml = rsvp
        ? '<span class="status-badge status-' + rsvp.status + '">' + statusLabel(rsvp.status) + '</span>'
        : '<span class="no-rsvp">Not yet</span>';
      const inviteUrl = baseUrl + '/?invite=' + g.invite_token;
      const btnId = 'copy-' + g.id;
      const tokenTail = g.invite_token ? g.invite_token.slice(-6) : '';
      const mainRow = '<tr><td style="color:#999;font-size:13px;font-weight:600;text-align:center">' + (idx + 1) + '</td><td><strong>' + esc(g.name) + '</strong></td><td>' + statusHtml + '</td><td><div style="display:flex;align-items:center;gap:8px"><button class="copy-btn" id="' + btnId + '" onclick="copyLink(\'' + esc(inviteUrl).replace(/'/g, "\\'") + '\',\'' + btnId + '\')" title="' + esc(inviteUrl) + '">Copy Link</button><span style="font-size:10px;color:#bbb;font-family:monospace">...' + esc(tokenTail) + '</span></div></td></tr>';
      const igVal = (rsvp && rsvp.instagram) || g.instagram || '';
      const fbVal = (rsvp && rsvp.facebook) || g.facebook || '';
      const phoneVal = (rsvp && rsvp.phone) || '';
      const hasIg = !!igVal;
      const hasFb = !!fbVal;
      const hasPhone = !!phoneVal;
      function socialLink(url, platform) {
        if (!url) return '';
        const href = url.startsWith('http') ? url : 'https://' + platform + '.com/' + url;
        return href;
      }
      const igTick = hasIg ? '<a href="' + esc(socialLink(igVal, 'instagram')) + '" target="_blank" rel="noopener" style="color:#1a7a42;font-size:11px;text-decoration:none" title="Open profile">&#10003;</a> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const fbTick = hasFb ? '<a href="' + esc(socialLink(fbVal, 'facebook')) + '" target="_blank" rel="noopener" style="color:#1a7a42;font-size:11px;text-decoration:none" title="Open profile">&#10003;</a> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const phoneTick = hasPhone ? '<span style="color:#1a7a42;font-size:11px">&#10003;</span> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const detailsRow = '<tr><td colspan="4" style="padding:4px 12px 14px;border-bottom:2px solid #eee"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><span style="font-size:11px;color:#999;font-weight:600">SOCIALS & PHONE:</span></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px">' + igTick + '<input type="text" id="guest-ig-' + g.id + '" value="' + esc(igVal).replace(/"/g, '&quot;') + '" placeholder="Instagram URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px">' + fbTick + '<input type="text" id="guest-fb-' + g.id + '" value="' + esc(fbVal).replace(/"/g, '&quot;') + '" placeholder="Facebook URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + phoneTick + '<input type="text" id="guest-phone-' + g.id + '" value="' + esc(phoneVal).replace(/"/g, '&quot;') + '" placeholder="Phone number" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"><button class="action-btn approve-btn" style="font-size:11px;padding:4px 10px" onclick="saveGuestDetails(' + g.id + ')">Save</button></div></td></tr>';
      return mainRow + detailsRow;
    }).join('');
  }

  function copyLink(url, btnId) {
    navigator.clipboard.writeText(url).then(() => {
      const btn = document.getElementById(btnId);
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    }).catch(() => {
      // Fallback for older browsers
      const ta = document.createElement('textarea');
      ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      const btn = document.getElementById(btnId);
      btn.textContent = 'Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
    });
  }

  const LETTER_COLORS_ADMIN = {A:'#E74C3C',B:'#E67E22',C:'#F1C40F',D:'#2ECC71',E:'#1ABC9C',F:'#3498DB',G:'#9B59B6',H:'#E91E63',I:'#FF5722',J:'#FF9800',K:'#8BC34A',L:'#00BCD4',M:'#673AB7',N:'#F06292',O:'#D32F2F',P:'#FF7043',Q:'#CDDC39',R:'#26A69A',S:'#42A5F5',T:'#7E57C2',U:'#EC407A',V:'#FF8A65',W:'#66BB6A',X:'#FFD700',Y:'#29B6F6',Z:'#AB47BC'};
  function getAvatarColor(name) {
    const letter = (name || '?').charAt(0).toUpperCase();
    return LETTER_COLORS_ADMIN[letter] || '#999';
  }
  const IG_SVG = '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:#fff"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>';
  const FB_SVG = '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:#fff"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385h-3.047v-3.47h3.047v-2.642c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953h-1.514c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385c5.737-.9 10.125-5.864 10.125-11.854z"/></svg>';

  function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  function renderGuestList(guests, myName) {
    return '<ul class="guest-list">' + guests.map(g => {
      const color = getAvatarColor(g.name);
      const isMe = myName && g.name.toLowerCase() === myName.toLowerCase();
      let socials = '';
      if (g.instagram || g.facebook) {
        socials = '<span class="guest-socials">';
        if (g.instagram) {
          const igUrl = g.instagram.startsWith('http') ? g.instagram : 'https://instagram.com/' + g.instagram;
          socials += '<a class="guest-social-link ig-link" href="' + escapeHtml(igUrl) + '" target="_blank" rel="noopener" title="Instagram">' + IG_SVG + '</a>';
        }
        if (g.facebook) {
          const fbUrl = g.facebook.startsWith('http') ? g.facebook : 'https://facebook.com/' + g.facebook;
          socials += '<a class="guest-social-link fb-link" href="' + escapeHtml(fbUrl) + '" target="_blank" rel="noopener" title="Facebook">' + FB_SVG + '</a>';
        }
        socials += '</span>';
      }
      const avatarHtml = g.profile_pic ? '<span class="avatar avatar-clickable" style="padding:0;overflow:hidden" onclick="expandAvatar(this)"><img src="' + g.profile_pic + '" style="width:100%;height:100%;object-fit:cover;border-radius:50%"></span>' : '<span class="avatar" style="background:' + color + '">' + escapeHtml(g.name.charAt(0).toUpperCase()) + '</span>';
      return '<li>' + avatarHtml + '<span class="guest-name">' + escapeHtml(g.name) + '</span>' + socials + (isMe ? '<span class="guest-badge badge-you">You</span>' : '') + '</li>';
    }).join('') + '</ul>';
  }

  function expandAvatar(el) {
    if (el.classList.contains('expanded')) {
      el.classList.remove('expanded');
    } else {
      document.querySelectorAll('.avatar-clickable.expanded').forEach(a => a.classList.remove('expanded'));
      el.classList.add('expanded');
    }
  }
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.avatar-clickable')) {
      document.querySelectorAll('.avatar-clickable.expanded').forEach(a => a.classList.remove('expanded'));
    }
  });

  async function renderAdminGuestList() {
    try {
      const res = await fetch('/api/rsvps');
      const data = await res.json();
      const pills = document.getElementById('admin-count-pills');
      const container = document.getElementById('admin-guest-list-container');

      pills.innerHTML = '';
      if (data.going_count > 0) pills.innerHTML += '<span class="count-pill going-pill" style="cursor:pointer" onclick="document.getElementById(\'admin-section-going\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.going_count + ' going</span>';
      if (data.maybe_count > 0) pills.innerHTML += '<span class="count-pill maybe-pill" style="cursor:pointer" onclick="document.getElementById(\'admin-section-maybe\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.maybe_count + ' maybe</span>';
      if (data.cant_go_count > 0) pills.innerHTML += '<span class="count-pill cant-pill" style="cursor:pointer" onclick="document.getElementById(\'admin-section-cantgo\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.cant_go_count + " can't go</span>";
      if (data.invited_count > 0) pills.innerHTML += '<span class="count-pill" style="background:#f0eeeb;color:#999;cursor:pointer" onclick="document.getElementById(\'admin-section-invited\').scrollIntoView({behavior:\'smooth\',block:\'start\'})">' + data.invited_count + ' invited</span>';

      if (data.guests.length === 0 && (!data.invited || data.invited.length === 0)) {
        container.innerHTML = '<div class="empty-state"><div class="empty-emoji">&#128064;</div><p>No RSVPs yet.<br>Be the first to join!</p></div>';
        return;
      }

      const going = data.guests.filter(g => g.status === 'going');
      const maybe = data.guests.filter(g => g.status === 'maybe');
      const cant = data.guests.filter(g => g.status === 'cant_go');

      let html = '';
      if (going.length > 0) {
        html += '<div class="guest-section-label" id="admin-section-going">Going &#127881;</div>';
        html += renderGuestList(going, '');
      }
      if (maybe.length > 0) {
        html += '<div class="guest-section-label" id="admin-section-maybe">Maybe &#129300;</div>';
        html += renderGuestList(maybe, '');
      }
      if (cant.length > 0) {
        html += '<div class="guest-section-label" id="admin-section-cantgo">Can\'t Go</div>';
        html += renderGuestList(cant, '');
      }
      const invited = data.invited || [];
      if (invited.length > 0) {
        html += '<div class="guest-section-label" id="admin-section-invited" style="color:#bbb">Invited &#9993;&#65039;</div>';
        html += '<ul class="guest-list">' + invited.map(g => {
          const color = getAvatarColor(g.name);
          return '<li style="opacity:0.5"><span class="avatar" style="background:' + color + '">' + escapeHtml(g.name.charAt(0).toUpperCase()) + '</span><span class="guest-name">' + escapeHtml(g.name) + '</span></li>';
        }).join('') + '</ul>';
      }
      container.innerHTML = html;
    } catch (e) { console.error('Failed to load guest list', e); }
  }

  function renderPlusOneInviteTable() {
    if (!_adminData) return;
    const tbody = document.getElementById('plusone-invite-table');
    const plusOnes = (_adminData.plus_ones || []).filter(p => p.approved === 1);
    const rsvps = _adminData.rsvps;
    const baseUrl = location.origin;

    if (plusOnes.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-text">No approved plus ones yet.</td></tr>';
      return;
    }

    tbody.innerHTML = plusOnes.map((p, idx) => {
      const rsvp = rsvps.find(r => r.name.toLowerCase() === p.name.toLowerCase());
      const statusHtml = rsvp
        ? '<span class="status-badge status-' + rsvp.status + '">' + statusLabel(rsvp.status) + '</span>'
        : '<span class="no-rsvp">Not yet</span>';
      const inviteUrl = p.invite_token ? baseUrl + '/?invite=' + p.invite_token : '';
      const btnId = 'po-copy-' + p.id;
      const tokenTail = p.invite_token ? p.invite_token.slice(-6) : '';
      const mainRow = '<tr><td style="color:#999;font-size:13px;font-weight:600;text-align:center">' + (idx + 1) + '</td><td><strong>' + esc(p.name) + '</strong><div style="font-size:10px;color:#999;margin-top:2px">Added by ' + esc(p.added_by) + '</div></td><td>' + statusHtml + '</td><td>' + (inviteUrl ? '<div style="display:flex;align-items:center;gap:8px"><button class="copy-btn" id="' + btnId + '" onclick="copyLink(\'' + esc(inviteUrl).replace(/'/g, "\\'") + '\',\'' + btnId + '\')" title="' + esc(inviteUrl) + '">Copy Link</button><span style="font-size:10px;color:#bbb;font-family:monospace">...' + esc(tokenTail) + '</span></div>' : '<span class="no-rsvp">No link</span>') + '</td></tr>';

      const igVal = (rsvp && rsvp.instagram) || '';
      const fbVal = (rsvp && rsvp.facebook) || '';
      const phoneVal = p.phone || (rsvp && rsvp.phone) || '';
      const hasIg = !!igVal;
      const hasFb = !!fbVal;
      const hasPhone = !!phoneVal;
      function socialLink(url, platform) {
        if (!url) return '';
        return url.startsWith('http') ? url : 'https://' + platform + '.com/' + url;
      }
      const igTick = hasIg ? '<a href="' + esc(socialLink(igVal, 'instagram')) + '" target="_blank" rel="noopener" style="color:#1a7a42;font-size:11px;text-decoration:none" title="Open profile">&#10003;</a> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const fbTick = hasFb ? '<a href="' + esc(socialLink(fbVal, 'facebook')) + '" target="_blank" rel="noopener" style="color:#1a7a42;font-size:11px;text-decoration:none" title="Open profile">&#10003;</a> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const phoneTick = hasPhone ? '<span style="color:#1a7a42;font-size:11px">&#10003;</span> ' : '<span style="color:#ccc;font-size:11px">&#10007;</span> ';
      const detailsRow = '<tr><td colspan="4" style="padding:4px 12px 14px;border-bottom:2px solid #eee"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><span style="font-size:11px;color:#999;font-weight:600">SOCIALS & PHONE:</span></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px">' + igTick + '<input type="text" id="po-ig-' + p.id + '" value="' + esc(igVal).replace(/"/g, '&quot;') + '" placeholder="Instagram URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px">' + fbTick + '<input type="text" id="po-fb-' + p.id + '" value="' + esc(fbVal).replace(/"/g, '&quot;') + '" placeholder="Facebook URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + phoneTick + '<input type="text" id="po-phone-' + p.id + '" value="' + esc(phoneVal).replace(/"/g, '&quot;') + '" placeholder="Phone number" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"><button class="action-btn approve-btn" style="font-size:11px;padding:4px 10px" onclick="savePlusOneDetails(' + p.id + ')">Save</button></div></td></tr>';
      return mainRow + detailsRow;
    }).join('');
  }

  async function saveGuestDetails(guestId) {
    const igInput = document.getElementById('guest-ig-' + guestId);
    const fbInput = document.getElementById('guest-fb-' + guestId);
    const phoneInput = document.getElementById('guest-phone-' + guestId);
    const instagram = igInput ? igInput.value.trim() : '';
    const facebook = fbInput ? fbInput.value.trim() : '';
    const phone = phoneInput ? phoneInput.value.trim() : '';
    try {
      const res = await fetch('/api/admin/update-guest-socials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: guestId, instagram, facebook, phone })
      });
      if (res.ok) {
        showToast('Details saved!');
        loadData();
      }
    } catch (e) { showToast('Failed to save'); }
  }

  async function savePlusOneDetails(poId) {
    const igInput = document.getElementById('po-ig-' + poId);
    const fbInput = document.getElementById('po-fb-' + poId);
    const phoneInput = document.getElementById('po-phone-' + poId);
    const instagram = igInput ? igInput.value.trim() : '';
    const facebook = fbInput ? fbInput.value.trim() : '';
    const phone = phoneInput ? phoneInput.value.trim() : '';
    try {
      const res = await fetch('/api/admin/update-plusone-details', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: poId, instagram, facebook, phone })
      });
      if (res.ok) {
        showToast('Details saved!');
        loadData();
      }
    } catch (e) { showToast('Failed to save'); }
  }

  async function postAnnouncement() {
    const input = document.getElementById('announcement-input');
    const message = input.value.trim();
    if (!message) return;
    try {
      const res = await fetch('/api/admin/announcement', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message })
      });
      if (res.ok) {
        input.value = '';
        showToast('Update posted');
        loadData();
      } else { showToast('Failed to post'); }
    } catch (e) { showToast('Something went wrong'); }
  }

  async function deleteAnnouncement(id) {
    if (!confirm('Delete this update?')) return;
    try {
      const res = await fetch('/api/admin/announcement', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id })
      });
      if (res.ok) {
        showToast('Update deleted');
        loadData();
      }
    } catch (e) { showToast('Something went wrong'); }
  }

  function renderAnnouncements() {
    if (!_adminData) return;
    const list = document.getElementById('admin-announcements-list');
    const anns = _adminData.announcements || [];
    if (anns.length === 0) {
      list.innerHTML = '<p style="color:#bbb;font-size:13px">No updates posted yet</p>';
      return;
    }
    list.innerHTML = anns.map(a => {
      const d = new Date(a.created_at);
      const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const h = d.getHours(); const m = d.getMinutes();
      const ampm = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      const timeStr = months[d.getMonth()] + ' ' + d.getDate() + ' at ' + h12 + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm;
      return '<div style="background:#f9f8f6;border-radius:10px;padding:12px 14px;margin-bottom:8px;display:flex;align-items:flex-start;gap:10px"><div style="flex:1"><div style="font-size:14px;line-height:1.5">' + esc(a.message) + '</div><div style="font-size:11px;color:#999;margin-top:4px">' + timeStr + '</div></div><button onclick="deleteAnnouncement(' + a.id + ')" style="background:none;border:none;color:#c0392b;font-size:18px;cursor:pointer;padding:0 4px;font-weight:700;flex-shrink:0" title="Delete">&times;</button></div>';
    }).join('');
  }

  // Enter key in add-name input
  document.getElementById('add-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addGuest(); }
  });

  loadData();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if not ADMIN_PASSWORD:
        print("WARNING: ADMIN_PASSWORD env var not set. Admin login will be disabled.")
        print("   Run with: ADMIN_PASSWORD=yourpass python3 server.py\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Party RSVP server running at http://localhost:{PORT}")
    print(f"Admin panel at http://localhost:{PORT}/admin")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
