"""
Krish and James Party V2 — Vercel Serverless (Flask + PostgreSQL)
"""

import json
import os
import hashlib
import hmac
import secrets
import time
from flask import Flask, request, jsonify, make_response
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS guest_list (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        invite_token TEXT UNIQUE
    )""")
    cur.execute("""
        DO $$ BEGIN
            CREATE UNIQUE INDEX guest_list_name_lower ON guest_list (LOWER(name));
        EXCEPTION WHEN duplicate_table THEN NULL;
        END $$;
    """)
    cur.execute("""CREATE TABLE IF NOT EXISTS rsvps (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'going',
        approved INTEGER NOT NULL DEFAULT 0,
        instagram TEXT DEFAULT '',
        facebook TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""")
    # Migrate: add phone column if missing
    try:
        cur.execute("ALTER TABLE rsvps ADD COLUMN phone TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()
    # Migrate: add instagram/facebook columns to guest_list for host pre-fill
    try:
        cur.execute("ALTER TABLE guest_list ADD COLUMN instagram TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        cur.execute("ALTER TABLE guest_list ADD COLUMN facebook TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()
    cur.execute("""CREATE TABLE IF NOT EXISTS plus_ones (
        id SERIAL PRIMARY KEY,
        added_by TEXT NOT NULL,
        name TEXT NOT NULL,
        phone TEXT DEFAULT '',
        invite_token TEXT UNIQUE,
        approved INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW()
    )""")
    # Migrate: add phone and invite_token columns if missing
    try:
        cur.execute("ALTER TABLE plus_ones ADD COLUMN phone TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        cur.execute("ALTER TABLE plus_ones ADD COLUMN invite_token TEXT UNIQUE")
        conn.commit()
    except Exception:
        conn.rollback()
    conn.commit()
    # Backfill invite tokens for guests that don't have one
    cur.execute("SELECT id FROM guest_list WHERE invite_token IS NULL")
    for row in cur.fetchall():
        cur.execute("UPDATE guest_list SET invite_token = %s WHERE id = %s", (secrets.token_urlsafe(12), row["id"]))
    # Backfill invite tokens for approved plus ones that don't have one
    cur.execute("SELECT id, name, phone FROM plus_ones WHERE approved = 1 AND (invite_token IS NULL OR invite_token = '')")
    for row in cur.fetchall():
        token = secrets.token_urlsafe(12)
        cur.execute("UPDATE plus_ones SET invite_token = %s WHERE id = %s", (token, row["id"]))
        # Also ensure they have an RSVP entry
        cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (row["name"],))
        if not cur.fetchone():
            cur.execute("INSERT INTO rsvps (name, status, approved, phone) VALUES (%s, 'maybe', 1, %s)", (row["name"], row["phone"] or ""))
    conn.commit()
    conn.close()


# Initialize DB on cold start
try:
    if DATABASE_URL:
        init_db()
except Exception as e:
    print(f"DB init error: {e}")


def make_admin_token():
    """Create an HMAC-signed admin session token (stateless)."""
    ts = str(int(time.time()))
    sig = hmac.new(ADMIN_PASSWORD.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_admin_token(token):
    """Verify an HMAC-signed admin token (valid for 24h)."""
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if time.time() - ts > 86400:
        return False
    expected = hmac.new(ADMIN_PASSWORD.encode(), ts_str.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def check_admin():
    token = request.cookies.get("admin_token", "")
    return verify_admin_token(token)


# ─── Routes ──────────────────────────────────────────────────────────────────


@app.route("/rsvp")
def rsvp_page():
    invite_token = request.args.get("invite", "").strip()
    if invite_token:
        conn = get_db()
        cur = conn.cursor()
        # Check guest_list first, then plus_ones
        cur.execute("SELECT name FROM guest_list WHERE invite_token = %s", (invite_token,))
        guest = cur.fetchone()
        if not guest:
            cur.execute("SELECT name FROM plus_ones WHERE invite_token = %s AND approved = 1", (invite_token,))
            guest = cur.fetchone()
        conn.close()
        if guest:
            prefill_script = f'<script>window.__INVITE_NAME={json.dumps(guest["name"])};localStorage.setItem("krish_james_party_v2_name",window.__INVITE_NAME);</script>'
            html = MAIN_HTML.replace('<head>', '<head>' + prefill_script, 1)
            return make_response(html, 200, {"Content-Type": "text/html; charset=utf-8"})
    return make_response(MAIN_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})


@app.route("/api/rsvps")
def api_rsvps():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, status, instagram, facebook, updated_at FROM rsvps WHERE approved = 1 ORDER BY updated_at DESC")
    rows = cur.fetchall()
    cur.execute("SELECT name, created_at FROM plus_ones WHERE approved = 1 ORDER BY created_at DESC")
    plus_ones = cur.fetchall()
    conn.close()

    guests = [{"name": r["name"], "status": r["status"], "instagram": r["instagram"] or "", "facebook": r["facebook"] or "", "time": str(r["updated_at"])} for r in rows]
    for p in plus_ones:
        guests.append({"name": p["name"], "status": "going", "instagram": "", "facebook": "", "time": str(p["created_at"])})

    going = [g for g in guests if g["status"] == "going"]
    maybe = [g for g in guests if g["status"] == "maybe"]
    cant = [g for g in guests if g["status"] == "cant_go"]
    return jsonify({
        "guests": guests,
        "going_count": len(going),
        "maybe_count": len(maybe),
        "cant_go_count": len(cant),
        "total": len(guests),
    })


@app.route("/api/my-status")
def api_my_status():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"found": False})
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, approved, instagram, facebook, phone FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({"found": True, "status": row["status"], "approved": row["approved"], "instagram": row["instagram"] or "", "facebook": row["facebook"] or "", "phone": row["phone"] or ""})
    return jsonify({"found": False})


@app.route("/api/plus-ones")
def api_plus_ones():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"plus_ones": []})
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, phone, approved FROM plus_ones WHERE LOWER(added_by) = LOWER(%s) ORDER BY created_at DESC", (name,))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"plus_ones": [{"id": r["id"], "name": r["name"], "phone": r["phone"] or "", "approved": r["approved"] == 1, "denied": r["approved"] == -1} for r in rows]})


@app.route("/")
def admin():
    if check_admin():
        return make_response(ADMIN_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})
    return make_response(ADMIN_LOGIN_HTML, 200, {"Content-Type": "text/html; charset=utf-8"})


@app.route("/api/admin/data")
def api_admin_data():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, invite_token, instagram, facebook FROM guest_list ORDER BY LOWER(name)")
    guest_list = cur.fetchall()
    cur.execute("SELECT id, name, status, approved, instagram, facebook, phone, created_at, updated_at FROM rsvps ORDER BY created_at DESC")
    rsvps = cur.fetchall()
    cur.execute("SELECT id, added_by, name, phone, invite_token, approved, created_at FROM plus_ones ORDER BY LOWER(added_by), created_at DESC")
    plus_ones = cur.fetchall()
    conn.close()
    # Convert timestamps to strings
    for r in rsvps:
        r["created_at"] = str(r["created_at"]) if r["created_at"] else ""
        r["updated_at"] = str(r["updated_at"]) if r["updated_at"] else ""
    for p in plus_ones:
        p["created_at"] = str(p["created_at"]) if p["created_at"] else ""
    return jsonify({"guest_list": guest_list, "rsvps": rsvps, "plus_ones": plus_ones})


@app.route("/api/rsvp", methods=["POST"])
def api_rsvp():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    status = body.get("status", "going")
    instagram = body.get("instagram", "").strip()
    facebook = body.get("facebook", "").strip()

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if status not in ("going", "maybe", "cant_go"):
        return jsonify({"error": "Invalid status"}), 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM guest_list WHERE LOWER(name) = LOWER(%s)", (name,))
    on_list = cur.fetchone()
    approved = 1 if on_list else 0

    cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE rsvps SET status = %s, instagram = %s, facebook = %s, updated_at = NOW() WHERE id = %s", (status, instagram, facebook, existing["id"]))
    else:
        cur.execute("INSERT INTO rsvps (name, status, approved, instagram, facebook) VALUES (%s, %s, %s, %s, %s)", (name, status, approved, instagram, facebook))
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "approved": bool(approved),
        "updated": bool(existing),
        "status": status,
    }), 200 if existing else 201


@app.route("/api/update-socials", methods=["POST"])
def api_update_socials():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    instagram = body.get("instagram", "").strip()
    facebook = body.get("facebook", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
    existing = cur.fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "RSVP not found"}), 404
    cur.execute("UPDATE rsvps SET instagram = %s, facebook = %s, updated_at = NOW() WHERE id = %s", (instagram, facebook, existing["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/update-phone", methods=["POST"])
def api_update_phone():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    phone = body.get("phone", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
    existing = cur.fetchone()
    if not existing:
        conn.close()
        return jsonify({"error": "RSVP not found"}), 404
    cur.execute("UPDATE rsvps SET phone = %s, updated_at = NOW() WHERE id = %s", (phone, existing["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/update-guest-socials", methods=["POST"])
def api_admin_update_guest_socials():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    guest_id = body.get("id")
    instagram = body.get("instagram", "").strip()
    facebook = body.get("facebook", "").strip()
    phone = body.get("phone", "").strip()
    if not guest_id:
        return jsonify({"error": "Guest ID is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE guest_list SET instagram = %s, facebook = %s WHERE id = %s", (instagram, facebook, guest_id))
    cur.execute("SELECT name FROM guest_list WHERE id = %s", (guest_id,))
    guest = cur.fetchone()
    if guest:
        cur.execute("SELECT id, instagram, facebook, phone FROM rsvps WHERE LOWER(name) = LOWER(%s)", (guest["name"],))
        rsvp = cur.fetchone()
        if rsvp:
            updates = []
            params = []
            if not rsvp["instagram"] and instagram:
                updates.append("instagram = %s"); params.append(instagram)
            if not rsvp["facebook"] and facebook:
                updates.append("facebook = %s"); params.append(facebook)
            if phone:
                updates.append("phone = %s"); params.append(phone)
            if updates:
                params.append(rsvp["id"])
                cur.execute("UPDATE rsvps SET " + ", ".join(updates) + " WHERE id = %s", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/update-plusone-details", methods=["POST"])
def api_admin_update_plusone_details():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    po_id = body.get("id")
    instagram = body.get("instagram", "").strip()
    facebook = body.get("facebook", "").strip()
    phone = body.get("phone", "").strip()
    if not po_id:
        return jsonify({"error": "Plus one ID is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    if phone:
        cur.execute("UPDATE plus_ones SET phone = %s WHERE id = %s", (phone, po_id))
    cur.execute("SELECT name FROM plus_ones WHERE id = %s", (po_id,))
    po = cur.fetchone()
    if po:
        cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (po["name"],))
        rsvp = cur.fetchone()
        if rsvp:
            updates = []
            params = []
            if instagram:
                updates.append("instagram = %s"); params.append(instagram)
            if facebook:
                updates.append("facebook = %s"); params.append(facebook)
            if phone:
                updates.append("phone = %s"); params.append(phone)
            if updates:
                params.append(rsvp["id"])
                cur.execute("UPDATE rsvps SET " + ", ".join(updates) + " WHERE id = %s", params)
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/send-alert", methods=["POST"])
def api_admin_send_alert():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    message = body.get("message", "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, phone FROM rsvps WHERE phone IS NOT NULL AND phone != '' AND approved = 1")
    recipients = cur.fetchall()
    conn.close()
    # Log the alert (in a real app, this would send SMS)
    print(f"[ALERT] Message: {message}")
    for r in recipients:
        print(f"  -> {r['name']}: {r['phone']}")
    return jsonify({"ok": True, "sent_to": len(recipients), "recipients": [{"name": r["name"], "phone": r["phone"]} for r in recipients]})


@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"error": "ADMIN_PASSWORD env var not set"}), 500
    body = request.get_json(force=True)
    pw = body.get("password", "")
    if pw == ADMIN_PASSWORD:
        token = make_admin_token()
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("admin_token", token, path="/", httponly=True, samesite="Strict", max_age=86400)
        return resp
    return jsonify({"error": "Wrong password"}), 401


@app.route("/api/admin/guest-list", methods=["POST"])
def api_admin_guest_list():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    action = body.get("action")
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400

    conn = get_db()
    cur = conn.cursor()
    if action == "add":
        try:
            token = secrets.token_urlsafe(12)
            cur.execute("INSERT INTO guest_list (name, invite_token) VALUES (%s, %s)", (name, token))
            cur.execute("UPDATE rsvps SET approved = 1 WHERE LOWER(name) = LOWER(%s)", (name,))
            conn.commit()
            conn.close()
            return jsonify({"ok": True}), 201
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            conn.close()
            return jsonify({"error": "Name already on list"}), 409
    elif action == "remove":
        cur.execute("DELETE FROM guest_list WHERE LOWER(name) = LOWER(%s)", (name,))
        cur.execute("DELETE FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
        cur.execute("DELETE FROM plus_ones WHERE LOWER(added_by) = LOWER(%s)", (name,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    else:
        conn.close()
        return jsonify({"error": "Invalid action"}), 400


@app.route("/api/admin/approve", methods=["POST"])
def api_admin_approve():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    rsvp_id = body.get("id")
    action = body.get("action")
    if not rsvp_id or action not in ("approve", "reject"):
        return jsonify({"error": "Invalid request"}), 400

    conn = get_db()
    cur = conn.cursor()
    if action == "approve":
        cur.execute("UPDATE rsvps SET approved = 1 WHERE id = %s", (rsvp_id,))
        cur.execute("SELECT name FROM rsvps WHERE id = %s", (rsvp_id,))
        row = cur.fetchone()
        if row:
            try:
                cur.execute("INSERT INTO guest_list (name, invite_token) VALUES (%s, %s)", (row["name"], secrets.token_urlsafe(12)))
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                cur.execute("UPDATE rsvps SET approved = 1 WHERE id = %s", (rsvp_id,))
    elif action == "reject":
        cur.execute("UPDATE rsvps SET approved = -1 WHERE id = %s", (rsvp_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/plus-one", methods=["POST"])
def api_plus_one():
    body = request.get_json(force=True)
    added_by = body.get("added_by", "").strip()
    name = body.get("name", "").strip()
    phone = body.get("phone", "").strip()
    if not added_by or not name:
        return jsonify({"error": "Name is required"}), 400
    if not phone:
        return jsonify({"error": "Phone number is required for plus ones"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM plus_ones WHERE LOWER(added_by) = LOWER(%s) AND LOWER(name) = LOWER(%s)", (added_by, name))
    existing = cur.fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"{name} is already on your plus one list"}), 409
    cur.execute("INSERT INTO plus_ones (added_by, name, phone) VALUES (%s, %s, %s)", (added_by, name, phone))
    conn.commit()
    conn.close()
    return jsonify({"ok": True}), 201


@app.route("/api/plus-one/remove", methods=["POST"])
def api_plus_one_remove():
    body = request.get_json(force=True)
    plus_one_id = body.get("id")
    added_by = body.get("added_by", "").strip()
    if not plus_one_id or not added_by:
        return jsonify({"error": "Invalid request"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM plus_ones WHERE id = %s AND LOWER(added_by) = LOWER(%s) AND approved = 0", (plus_one_id, added_by))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/approve-plus-one", methods=["POST"])
def api_admin_approve_plus_one():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    plus_one_id = body.get("id")
    action = body.get("action")
    if not plus_one_id or action not in ("approve", "reject"):
        return jsonify({"error": "Invalid request"}), 400
    conn = get_db()
    cur = conn.cursor()
    if action == "approve":
        token = secrets.token_urlsafe(12)
        cur.execute("UPDATE plus_ones SET approved = 1, invite_token = %s WHERE id = %s", (token, plus_one_id))
        # Auto-RSVP the plus one as "Maybe"
        cur.execute("SELECT name, phone FROM plus_ones WHERE id = %s", (plus_one_id,))
        po = cur.fetchone()
        if po:
            cur.execute("SELECT id FROM rsvps WHERE LOWER(name) = LOWER(%s)", (po["name"],))
            existing_rsvp = cur.fetchone()
            if not existing_rsvp:
                cur.execute("INSERT INTO rsvps (name, status, approved, phone) VALUES (%s, 'maybe', 1, %s)", (po["name"], po["phone"] or ""))
    elif action == "reject":
        cur.execute("UPDATE plus_ones SET approved = -1 WHERE id = %s", (plus_one_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/admin/delete-plus-one", methods=["POST"])
def api_admin_delete_plus_one():
    if not check_admin():
        return jsonify({"error": "Unauthorized"}), 401
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM plus_ones WHERE LOWER(name) = LOWER(%s)", (name,))
    cur.execute("DELETE FROM rsvps WHERE LOWER(name) = LOWER(%s)", (name,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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
  .phone-section { margin-top: 24px; padding-top: 20px; border-top: 1px solid #eee; }
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

  .guest-section-label { font-size: 12px; font-weight: 700; color: #999; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 20px; margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid #f0eee9; }
  .guest-section-label:first-child { margin-top: 0; }

  .guest-list { list-style: none; }
  .guest-list li { padding: 12px 0; border-bottom: 1px solid #f0eee9; display: flex; align-items: center; gap: 14px; font-size: 15px; font-weight: 500; }
  .guest-list li:last-child { border-bottom: none; }

  .avatar { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; color: #fff; flex-shrink: 0; }
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
      <div class="detail-text"><span class="detail-label">Date</span><span class="detail-value">Saturday, April 5, 2026</span></div>
    </div>
    <div class="detail-row">
      <div class="detail-icon">&#128336;</div>
      <div class="detail-text"><span class="detail-label">Time</span><span class="detail-value">8:00 PM onwards</span></div>
    </div>
    <div class="detail-row">
      <div class="detail-icon">&#128205;</div>
      <div class="detail-text"><span class="detail-label">Location</span><span class="detail-value">TBD - Check back soon!</span></div>
    </div>
    <div class="byob-msg">&#127865; Feel free to bring your own drinks — the more the merrier!</div>
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
      <div class="phone-section" id="phone-section">
      <div class="socials-header"><h3>Get party alerts &#128242;</h3></div>
      <p class="socials-prompt">Drop your number to stay in the loop on any updates! (optional)</p>
      <div class="social-input-row">
        <div class="social-icon-label" style="background:#1a1a1a"><svg viewBox="0 0 24 24" style="width:20px;height:20px;fill:#fff"><path d="M6.62 10.79c1.44 2.83 3.76 5.14 6.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/></svg></div>
        <input type="tel" class="social-input" id="phone-input" placeholder="Your phone number" autocomplete="off">
      </div>
      <button class="save-socials-btn" onclick="savePhone()">Save Number</button>
      </div>
      <div class="socials-section" id="socials-section">
      <div class="socials-header"><h3>Link your socials</h3></div>
      <p class="socials-prompt">Copy &amp; paste your profile links so other guests can find you! (optional)</p>
      <p class="socials-subtitle">The host may have already filled this in for you &#128064;</p>
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
        <input type="tel" id="plusone-phone" placeholder="Their phone number" autocomplete="off" style="flex:1;min-width:140px;padding:14px 16px;font-family:'DM Sans',sans-serif;font-size:15px;border:2px solid #e8e6e3;border-radius:14px;background:#faf9f7;outline:none">
        <button class="plusone-add-btn" onclick="addPlusOne()">Add</button>
      </div>
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
      const startX = Math.random() * heroRect.width;
      const startY = heroRect.height * 0.5 + Math.random() * heroRect.height * 0.5;
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
  function getName() { return window.__INVITE_NAME || getName(); }
  const LETTER_COLORS = {A:'#AC92EB',B:'#B89CE8',C:'#C4A6E5',D:'#9F86D9',E:'#9B7ED6',F:'#4FC1E8',G:'#5BC8EB',H:'#67CFEE',I:'#43B4DB',J:'#37A8CF',K:'#A0D568',L:'#ABD974',M:'#B6DD80',N:'#94C95C',O:'#88BD50',P:'#FFCE54',Q:'#FFD467',R:'#FFDA7A',S:'#FFC841',T:'#FFC22E',U:'#ED5564',V:'#EF6672',W:'#F17780',X:'#EB4453',Y:'#E93345',Z:'#FFD700'};
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
    const name = getName();
    if (!name) return;
    const nameGroup = document.getElementById('name-group');
    nameGroup.innerHTML = '<div class="returning-name">' + escapeHtml(name) + '</div>';
    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.id = 'name-input';
    hidden.value = name;
    nameGroup.appendChild(hidden);
    try {
      const res = await fetch('/api/my-status?name=' + encodeURIComponent(name));
      const data = await res.json();
      if (data.found) {
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
        document.getElementById('rsvp-intro').querySelector('p').textContent = 'Update your status or add your social links below.';
        document.getElementById('socials-section').style.display = '';
        document.getElementById('phone-section').style.display = '';
        document.getElementById('ig-input').value = data.instagram || '';
        document.getElementById('fb-input').value = data.facebook || '';
        document.getElementById('phone-input').value = data.phone || '';
      }
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
      const myName = getName();

      pills.innerHTML = '';
      if (data.going_count > 0) pills.innerHTML += '<span class="count-pill going-pill">' + data.going_count + ' going</span>';
      if (data.maybe_count > 0) pills.innerHTML += '<span class="count-pill maybe-pill">' + data.maybe_count + ' maybe</span>';
      if (data.cant_go_count > 0) pills.innerHTML += '<span class="count-pill cant-pill">' + data.cant_go_count + " can't go</span>";

      if (data.guests.length === 0) {
        container.innerHTML = '<div class="empty-state"><div class="empty-emoji">&#128064;</div><p>No RSVPs yet.<br>Be the first to join!</p></div>';
        return;
      }

      const going = data.guests.filter(g => g.status === 'going');
      const maybe = data.guests.filter(g => g.status === 'maybe');
      const cant = data.guests.filter(g => g.status === 'cant_go');

      let html = '';
      if (going.length > 0) {
        html += '<div class="guest-section-label">Going &#127881;</div>';
        html += renderGuestList(going, myName);
      }
      if (maybe.length > 0) {
        html += '<div class="guest-section-label">Maybe &#129300;</div>';
        html += renderGuestList(maybe, myName);
      }
      if (cant.length > 0) {
        html += '<div class="guest-section-label">Can\'t Go</div>';
        html += renderGuestList(cant, myName);
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
      return '<li><span class="avatar" style="background:' + color + '">' + escapeHtml(g.name.charAt(0).toUpperCase()) + '</span><span class="guest-name">' + escapeHtml(g.name) + '</span>' + socials + (isMe ? '<span class="guest-badge badge-you">You</span>' : '') + '</li>';
    }).join('') + '</ul>';
  }

  async function saveSocials() {
    const name = getName();
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

  async function savePhone() {
    const name = getName();
    if (!name) return;
    const phone = document.getElementById('phone-input').value.trim();
    try {
      const res = await fetch('/api/update-phone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, phone })
      });
      if (res.ok) showToast('&#10003; Phone number saved!');
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

      if (status === 'going') confettiShower();
      else if (status === 'cant_go') sadShower();
      else if (status === 'maybe') maybeExplosion();

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
      document.getElementById('socials-section').style.display = '';
      document.getElementById('phone-section').style.display = '';
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
    spawnWave(15, 0);
    spawnWave(12, 700);
    spawnWave(8, 1400);
  }

  // Plus Ones
  async function loadPlusOnes() {
    const name = getName();
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
    const name = getName();
    if (!name) return;
    const input = document.getElementById('plusone-name');
    const phoneInput = document.getElementById('plusone-phone');
    const friendName = input.value.trim();
    const friendPhone = phoneInput.value.trim();
    if (!friendName) { showToast('Please enter their name'); return; }
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
    const name = getName();
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

  document.getElementById('plusone-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); document.getElementById('plusone-phone').focus(); }
  });
  document.getElementById('plusone-phone').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addPlusOne(); }
  });

  const savedName = getName();
  if (savedName) {
    document.getElementById('name-input').value = savedName;
    refreshMyStatus();
  }
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
  .chip .remove { background: none; border: none; color: #c0392b; font-size: 16px; cursor: pointer; padding: 0 2px; font-weight: 700; }

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
    <div class="card">
      <h2>&#128203; Approved Guest List</h2>
      <div class="add-form">
        <input type="text" id="add-name" placeholder="Add a name to the guest list">
        <button onclick="addGuest()">Add</button>
      </div>
      <div class="chip-list" id="guest-chips"></div>
    </div>

    <div class="card" id="pending-card">
      <h2>&#9203; Pending Approvals</h2>
      <div id="pending-area"></div>
    </div>

    <div class="card">
      <h2>&#128101; All RSVPs</h2>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>Name</th><th>Status</th><th>Approved</th><th>Updated</th></tr></thead>
          <tbody id="rsvp-table"></tbody>
        </table>
      </div>
    </div>

    <div class="card" id="plusones-card">
      <h2>&#128101; Plus Ones</h2>
      <div id="plusones-area"></div>
    </div>
  </div>

  <!-- GUEST INVITE LINKS TAB -->
  <div class="admin-panel" id="panel-invitelinks">
    <div class="card">
      <h2>&#128279; Guest Invite Links</h2>
      <p style="color:#777;font-size:14px;margin-bottom:16px;">Share unique invite links with each guest. Their name will be pre-filled when they open the link.</p>
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>Guest Name</th><th>RSVP Status</th><th>Invite Link</th></tr></thead>
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
          <thead><tr><th>Guest Name</th><th>RSVP Status</th><th>Invite Link</th></tr></thead>
          <tbody id="plusone-invite-table"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- GUEST LIST TAB (read-only public view) -->
  <div class="admin-panel" id="panel-guestlist">
    <div class="card">
      <h2>&#127881; Guest List</h2>
      <p style="color:#777;font-size:14px;margin-bottom:16px;">This is exactly what guests see on the public site.</p>
      <div class="count-pills" id="admin-count-pills"></div>
      <div id="admin-guest-list-container"></div>
    </div>

    <div class="card">
      <h2>&#128242; Phone Numbers &amp; Alerts</h2>
      <div id="phone-list-area"></div>
      <div style="margin-top:16px;border-top:1px solid #eee;padding-top:16px;">
        <p style="color:#777;font-size:14px;margin-bottom:10px;">Send a note to all guests who provided a phone number:</p>
        <div class="add-form">
          <input type="text" id="alert-message" placeholder="Type your alert message...">
          <button onclick="sendAlert()">Send Alert</button>
        </div>
        <div id="alert-result" style="margin-top:10px;"></div>
      </div>
    </div>
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
              await fetch('/api/admin/delete-plus-one', { method: 'POST', headers: {'Content-Type':'application/json', 'X-Admin-Password': _adminPassword}, body: JSON.stringify({ name: gName }) });
              loadAdminData();
            }
          } else {
            removeGuest(gName);
          }
        };
      });
    }

    const pending = data.rsvps.filter(r => r.approved === 0);
    const pendingArea = document.getElementById('pending-area');
    if (pending.length === 0) {
      pendingArea.innerHTML = '<p class="empty-text">No pending approvals</p>';
      document.getElementById('pending-card').style.display = '';
    } else {
      pendingArea.innerHTML = '<table><thead><tr><th>Name</th><th>Status</th><th>Actions</th></tr></thead><tbody>' +
        pending.map(r =>
          '<tr><td>' + esc(r.name) + '</td><td><span class="status-badge status-' + r.status + '">' + statusLabel(r.status) + '</span></td><td class="actions"><button class="action-btn approve-btn" onclick="approveRsvp(' + r.id + ')">Approve</button><button class="action-btn reject-btn" onclick="rejectRsvp(' + r.id + ')">Reject</button></td></tr>'
        ).join('') + '</tbody></table>';
    }

    const tbody = document.getElementById('rsvp-table');
    if (data.rsvps.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-text">No RSVPs yet</td></tr>';
    } else {
      tbody.innerHTML = data.rsvps.map(r =>
        '<tr><td>' + esc(r.name) + '</td><td><span class="status-badge status-' + r.status + '">' + statusLabel(r.status) + '</span></td><td>' + (r.approved === 1 ? '<span class="status-badge status-going">Yes</span>' : r.approved === -1 ? '<span class="status-badge status-denied">Denied</span>' : '<span class="status-badge status-pending">Pending</span>') + '</td><td>' + (r.updated_at || '').replace('T',' ').substring(0,16) + '</td></tr>'
      ).join('');
    }

    renderInviteTable();

    const plusOnes = data.plus_ones || [];
    const poArea = document.getElementById('plusones-area');
    if (plusOnes.length === 0) {
      poArea.innerHTML = '<p class="empty-text">No plus ones submitted yet</p>';
    } else {
      const groups = {};
      plusOnes.forEach(p => {
        const key = p.added_by;
        if (!groups[key]) groups[key] = [];
        groups[key].push(p);
      });
      let html = '';
      Object.keys(groups).sort().forEach(inviter => {
        const items = groups[inviter];
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
          html += '<div class="plusone-group-item"><span class="po-name">' + esc(p.name) + phoneInfo + '</span>' + badge + actions + '</div>';
        });
        html += '</div></div>';
      });
      poArea.innerHTML = html;
    }
  }

  function statusLabel(s) {
    return { going: 'Going', maybe: 'Maybe', cant_go: "Can't Go" }[s] || s;
  }
  function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  async function addGuest() {
    const input = document.getElementById('add-name');
    const name = input.value.trim().replace(/[^a-zA-Z\s\-']/g, '').replace(/\s+/g, ' ').trim();
    if (!name) { input.value = ''; return; }
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
    if (tab === 'guestlist') { renderAdminGuestList(); renderPhoneList(); }
  }

  let _adminData = null;

  function renderInviteTable() {
    if (!_adminData) return;
    const tbody = document.getElementById('invite-table');
    const guests = _adminData.guest_list;
    const rsvps = _adminData.rsvps;
    const baseUrl = location.origin;

    if (guests.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty-text">No guests added yet. Add guests in the Manage tab.</td></tr>';
      return;
    }

    tbody.innerHTML = guests.map(g => {
      const rsvp = rsvps.find(r => r.name.toLowerCase() === g.name.toLowerCase());
      const statusHtml = rsvp
        ? '<span class="status-badge status-' + rsvp.status + '">' + statusLabel(rsvp.status) + '</span>'
        : '<span class="no-rsvp">Not yet</span>';
      const inviteUrl = baseUrl + '/rsvp?invite=' + g.invite_token;
      const btnId = 'copy-' + g.id;
      const tokenTail = g.invite_token ? g.invite_token.slice(-6) : '';
      const mainRow = '<tr><td><strong>' + esc(g.name) + '</strong></td><td>' + statusHtml + '</td><td><div style="display:flex;align-items:center;gap:8px"><button class="copy-btn" id="' + btnId + '" onclick="copyLink(\'' + esc(inviteUrl).replace(/'/g, "\\'") + '\',\'' + btnId + '\')" title="' + esc(inviteUrl) + '">Copy Link</button><span style="font-size:10px;color:#bbb;font-family:monospace">...' + esc(tokenTail) + '</span></div></td></tr>';
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
      const detailsRow = '<tr><td colspan="3" style="padding:4px 12px 14px;border-bottom:2px solid #eee"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><span style="font-size:11px;color:#999;font-weight:600">SOCIALS & PHONE:</span></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px">' + igTick + '<input type="text" id="guest-ig-' + g.id + '" value="' + esc(igVal).replace(/"/g, '&quot;') + '" placeholder="Instagram URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px">' + fbTick + '<input type="text" id="guest-fb-' + g.id + '" value="' + esc(fbVal).replace(/"/g, '&quot;') + '" placeholder="Facebook URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + phoneTick + '<input type="text" id="guest-phone-' + g.id + '" value="' + esc(phoneVal).replace(/"/g, '&quot;') + '" placeholder="Phone number" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"><button class="action-btn approve-btn" style="font-size:11px;padding:4px 10px" onclick="saveGuestDetails(' + g.id + ')">Save</button></div></td></tr>';
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

  const LETTER_COLORS = {A:'#AC92EB',B:'#B89CE8',C:'#C4A6E5',D:'#9F86D9',E:'#9B7ED6',F:'#4FC1E8',G:'#5BC8EB',H:'#67CFEE',I:'#43B4DB',J:'#37A8CF',K:'#A0D568',L:'#ABD974',M:'#B6DD80',N:'#94C95C',O:'#88BD50',P:'#FFCE54',Q:'#FFD467',R:'#FFDA7A',S:'#FFC841',T:'#FFC22E',U:'#ED5564',V:'#EF6672',W:'#F17780',X:'#EB4453',Y:'#E93345',Z:'#FFD700'};
  function getAvatarColor(name) {
    const letter = (name || '?').charAt(0).toUpperCase();
    return LETTER_COLORS[letter] || '#999';
  }
  const IG_SVG = '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:#fff"><path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/></svg>';
  const FB_SVG = '<svg viewBox="0 0 24 24" style="width:16px;height:16px;fill:#fff"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385h-3.047v-3.47h3.047v-2.642c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953h-1.514c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385c5.737-.9 10.125-5.864 10.125-11.854z"/></svg>';

  async function renderAdminGuestList() {
    try {
      const res = await fetch('/api/rsvps');
      const data = await res.json();
      const pills = document.getElementById('admin-count-pills');
      const container = document.getElementById('admin-guest-list-container');

      pills.innerHTML = '';
      if (data.going_count > 0) pills.innerHTML += '<span style="font-size:12px;font-weight:700;padding:5px 12px;border-radius:100px;background:#e8f8ef;color:#1a7a42">' + data.going_count + ' going</span> ';
      if (data.maybe_count > 0) pills.innerHTML += '<span style="font-size:12px;font-weight:700;padding:5px 12px;border-radius:100px;background:#fff8e6;color:#9a7b20">' + data.maybe_count + ' maybe</span> ';
      if (data.cant_go_count > 0) pills.innerHTML += '<span style="font-size:12px;font-weight:700;padding:5px 12px;border-radius:100px;background:#fde8e8;color:#c0392b">' + data.cant_go_count + " can't go</span> ";

      if (data.guests.length === 0) {
        container.innerHTML = '<p class="empty-text">No RSVPs yet.</p>';
        return;
      }

      const sortAlpha = (a, b) => a.name.localeCompare(b.name);
      const going = data.guests.filter(g => g.status === 'going').sort(sortAlpha);
      const maybe = data.guests.filter(g => g.status === 'maybe').sort(sortAlpha);
      const cant = data.guests.filter(g => g.status === 'cant_go').sort(sortAlpha);

      function renderGroup(guests) {
        return guests.map(g => {
          const color = getAvatarColor(g.name);
          let socials = '';
          if (g.instagram || g.facebook) {
            socials = '<span style="display:flex;gap:6px;align-items:center">';
            if (g.instagram) {
              const igUrl = g.instagram.startsWith('http') ? g.instagram : 'https://instagram.com/' + g.instagram;
              socials += '<a href="' + esc(igUrl) + '" target="_blank" rel="noopener" style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888);text-decoration:none" title="Instagram">' + IG_SVG + '</a>';
            }
            if (g.facebook) {
              const fbUrl = g.facebook.startsWith('http') ? g.facebook : 'https://facebook.com/' + g.facebook;
              socials += '<a href="' + esc(fbUrl) + '" target="_blank" rel="noopener" style="display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:8px;background:#1877f2;text-decoration:none" title="Facebook">' + FB_SVG + '</a>';
            }
            socials += '</span>';
          }
          return '<div style="display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid #f0eee9"><span style="width:42px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;color:#fff;flex-shrink:0;background:' + color + '">' + esc(g.name.charAt(0).toUpperCase()) + '</span><span style="flex:1;font-size:15px;font-weight:500">' + esc(g.name) + '</span>' + socials + '</div>';
        }).join('');
      }

      let html = '';
      if (going.length > 0) {
        html += '<div style="font-size:12px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.06em;margin-top:20px;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #f0eee9">Going &#127881;</div>';
        html += renderGroup(going);
      }
      if (maybe.length > 0) {
        html += '<div style="font-size:12px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.06em;margin-top:20px;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #f0eee9">Maybe &#129300;</div>';
        html += renderGroup(maybe);
      }
      if (cant.length > 0) {
        html += '<div style="font-size:12px;font-weight:700;color:#999;text-transform:uppercase;letter-spacing:0.06em;margin-top:20px;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #f0eee9">Can\'t Go</div>';
        html += renderGroup(cant);
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
      tbody.innerHTML = '<tr><td colspan="3" class="empty-text">No approved plus ones yet.</td></tr>';
      return;
    }

    tbody.innerHTML = plusOnes.map(p => {
      const rsvp = rsvps.find(r => r.name.toLowerCase() === p.name.toLowerCase());
      const statusHtml = rsvp
        ? '<span class="status-badge status-' + rsvp.status + '">' + statusLabel(rsvp.status) + '</span>'
        : '<span class="no-rsvp">Not yet</span>';
      const inviteUrl = p.invite_token ? baseUrl + '/rsvp?invite=' + p.invite_token : '';
      const btnId = 'po-copy-' + p.id;
      const tokenTail = p.invite_token ? p.invite_token.slice(-6) : '';
      const mainRow = '<tr><td><strong>' + esc(p.name) + '</strong><div style="font-size:10px;color:#999;margin-top:2px">Added by ' + esc(p.added_by) + '</div></td><td>' + statusHtml + '</td><td>' + (inviteUrl ? '<div style="display:flex;align-items:center;gap:8px"><button class="copy-btn" id="' + btnId + '" onclick="copyLink(\'' + esc(inviteUrl).replace(/'/g, "\\'") + '\',\'' + btnId + '\')" title="' + esc(inviteUrl) + '">Copy Link</button><span style="font-size:10px;color:#bbb;font-family:monospace">...' + esc(tokenTail) + '</span></div>' : '<span class="no-rsvp">No link</span>') + '</td></tr>';

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
      const detailsRow = '<tr><td colspan="3" style="padding:4px 12px 14px;border-bottom:2px solid #eee"><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px"><span style="font-size:11px;color:#999;font-weight:600">SOCIALS & PHONE:</span></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px">' + igTick + '<input type="text" id="po-ig-' + p.id + '" value="' + esc(igVal).replace(/"/g, '&quot;') + '" placeholder="Instagram URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px">' + fbTick + '<input type="text" id="po-fb-' + p.id + '" value="' + esc(fbVal).replace(/"/g, '&quot;') + '" placeholder="Facebook URL" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"></div><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' + phoneTick + '<input type="text" id="po-phone-' + p.id + '" value="' + esc(phoneVal).replace(/"/g, '&quot;') + '" placeholder="Phone number" style="padding:4px 8px;font-size:12px;border:1px solid #e8e6e3;border-radius:6px;flex:1;min-width:100px"><button class="action-btn approve-btn" style="font-size:11px;padding:4px 10px" onclick="savePlusOneDetails(' + p.id + ')">Save</button></div></td></tr>';
      return mainRow + detailsRow;
    }).join('');
  }

  function renderPhoneList() {
    if (!_adminData) return;
    const area = document.getElementById('phone-list-area');
    const rsvps = _adminData.rsvps.filter(r => r.approved === 1);
    if (rsvps.length === 0) {
      area.innerHTML = '<p class="empty-text">No approved RSVPs yet</p>';
      return;
    }
    let html = '<table><thead><tr><th>Name</th><th>Phone</th><th>Status</th></tr></thead><tbody>';
    rsvps.forEach(r => {
      const phone = r.phone ? esc(r.phone) : '<span style="color:#bbb;font-style:italic">Not provided</span>';
      html += '<tr><td><strong>' + esc(r.name) + '</strong></td><td>' + phone + '</td><td><span class="status-badge status-' + r.status + '">' + statusLabel(r.status) + '</span></td></tr>';
    });
    html += '</tbody></table>';
    area.innerHTML = html;
  }

  async function sendAlert() {
    const input = document.getElementById('alert-message');
    const message = input.value.trim();
    if (!message) return;
    try {
      const res = await fetch('/api/admin/send-alert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message })
      });
      const data = await res.json();
      if (res.ok) {
        input.value = '';
        const resultArea = document.getElementById('alert-result');
        if (data.sent_to > 0) {
          resultArea.innerHTML = '<div style="padding:12px;background:#e8f8ef;border-radius:10px;font-size:14px;color:#1a7a42">&#10003; Alert logged for ' + data.sent_to + ' guest(s): ' + data.recipients.map(r => esc(r.name)).join(', ') + '</div>';
        } else {
          resultArea.innerHTML = '<div style="padding:12px;background:#fff8e6;border-radius:10px;font-size:14px;color:#9a7b20">No guests have provided phone numbers yet.</div>';
        }
        showToast('Alert sent!');
      }
    } catch (e) { showToast('Failed to send alert'); }
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

  document.getElementById('add-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); addGuest(); }
  });

  loadData();
</script>
</body>
</html>
"""
