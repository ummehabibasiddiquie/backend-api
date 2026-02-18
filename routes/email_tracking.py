from flask import Blueprint, request, Response
from datetime import datetime
import base64
import mysql.connector
import os

email_tracking_bp = Blueprint("email_tracking", __name__)

# 1x1 transparent GIF
GIF_BASE64 = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
PIXEL_BYTES = base64.b64decode(GIF_BASE64)

def norm_email(val: str) -> str:
    return (val or "").strip().lower()

def get_tracking_db():
    return mysql.connector.connect(
        host=os.getenv("TRACK_DB_HOST"),
        user=os.getenv("TRACK_DB_USER"),
        password=os.getenv("TRACK_DB_PASS"),
        database=os.getenv("TRACK_DB_NAME"),
        port=int(os.getenv("TRACK_DB_PORT", "3306")),
    )

def _pixel_response():
    resp = Response(PIXEL_BYTES, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def _now_epoch() -> int:
    return int(datetime.now().timestamp())

def _parse_epoch(val) -> int:
    try:
        if val is None:
            return 0
        s = str(val).strip()
        if s == "":
            return 0
        # allow float-looking values
        return int(float(s))
    except Exception:
        return 0


# =========================================================
# OPEN PIXEL (no prefetch storage + >=60s after scheduled time)
#
# Required query params (from template):
#   k  = draftId (unique per email)
#   st = scheduled send time epoch seconds ({{SendAt}})
#
# Optional:
#   to, from (for storing sender/receiver)
# =========================================================
@email_tracking_bp.route("/open.gif", methods=["GET"])
def track_open():
    send_key = (request.args.get("k") or "").strip()
    st_epoch = _parse_epoch(request.args.get("st"))

    # Optional (if your template provides them)
    receiver = norm_email(request.args.get("to", ""))
    sender = norm_email(request.args.get("from", ""))

    # Always return pixel (never break)
    if not send_key:
        return _pixel_response()

    # ✅ No prefetch storage:
    # If st missing/invalid -> do nothing
    if st_epoch <= 0:
        return _pixel_response()

    # ✅ Count open only if pixel hit >= 60s after scheduled send time
    now_epoch = _now_epoch()
    if now_epoch < (st_epoch + 60):
        return _pixel_response()

    # ✅ Insert open event ONLY after checks
    # If you want "only 1 open per email", add UNIQUE(send_key) and use INSERT IGNORE.
    try:
        conn = get_tracking_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO email_open_events
              (sender_email, receiver_email, send_key, opened_at)
            VALUES (%s, %s, %s, %s)
            """,
            (sender, receiver, send_key, datetime.now()),
        )

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Error in /open.gif:", e)

    return _pixel_response()


# =========================================================
# UNSUBSCRIBE (direct DB write, no extra page)
#
# Expected params (template controlled):
#   to, from  (recommended)
#   OR email + sender (if that's your naming)
#   k optional (not required)
# =========================================================
@email_tracking_bp.route("/unsub", methods=["GET", "POST"])
def unsubscribe():
    args = request.args if request.method == "GET" else request.form

    receiver = norm_email(args.get("to", "") or args.get("email", ""))
    sender = norm_email(args.get("from", "") or args.get("sender", ""))

    if sender and receiver:
        try:
            conn = get_tracking_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO email_subscription_preferences
                  (sender_email, receiver_email, is_subscribed, updated_at)
                VALUES (%s, %s, 0, %s)
                ON DUPLICATE KEY UPDATE
                  is_subscribed=0,
                  updated_at=VALUES(updated_at)
                """,
                (sender, receiver, datetime.now()),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print("Error in /unsub:", e)

    return Response("unsubscribed", mimetype="text/plain")


# =========================================================
# SUBSCRIBE (direct DB write, no extra page)
# =========================================================
@email_tracking_bp.route("/sub", methods=["GET", "POST"])
def subscribe():
    args = request.args if request.method == "GET" else request.form

    receiver = norm_email(args.get("to", "") or args.get("email", ""))
    sender = norm_email(args.get("from", "") or args.get("sender", ""))

    if sender and receiver:
        try:
            conn = get_tracking_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO email_subscription_preferences
                  (sender_email, receiver_email, is_subscribed, updated_at)
                VALUES (%s, %s, 1, %s)
                ON DUPLICATE KEY UPDATE
                  is_subscribed=1,
                  updated_at=VALUES(updated_at)
                """,
                (sender, receiver, datetime.now()),
            )
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print("Error in /sub:", e)

    return Response("subscribed", mimetype="text/plain")
