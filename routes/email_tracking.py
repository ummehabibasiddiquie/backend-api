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

@email_tracking_bp.route("/open.gif", methods=["GET"])
def track_open():
    print("Received tracking request with params:", request.args)  # Debug log
    receiver = norm_email(request.args.get("to", ""))
    sender = norm_email(request.args.get("from", ""))  # 'from' query param
    send_key = (request.args.get("k", "") or "").strip()

    if sender and receiver and send_key:
        try:
            print("TRACK_DB_HOST =", os.getenv("TRACK_DB_HOST"))
            print("TRACK_DB_NAME =", os.getenv("TRACK_DB_NAME"))
            conn = get_tracking_db()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT IGNORE INTO email_open_events
                (sender_email, receiver_email, send_key, opened_at)
                VALUES (%s, %s, %s, %s)
                """,
                (sender, receiver, send_key, datetime.utcnow()),
            )
            print(f"Tracked open: sender={sender}, receiver={receiver}, send_key={send_key}")   
            conn.commit()
            cur.close()
            conn.close()
            
        except Exception as e:
            print("Error : ", e)  # Log error but don't break response
            # Always return pixel, never break response
            pass

    resp = Response(PIXEL_BYTES, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@email_tracking_bp.route("/unsub", methods=["GET"])
def unsubscribe():
    receiver = norm_email(request.args.get("to", ""))
    sender = norm_email(request.args.get("from", ""))

    if sender and receiver:
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
            (sender, receiver, datetime.utcnow()),
        )
        conn.commit()
        cur.close()
        conn.close()

    return """
    <html><body style="font-family:Arial;padding:24px;">
      <h3>You have been unsubscribed.</h3>
      <p>You will no longer receive emails from this sender.</p>
    </body></html>
    """

