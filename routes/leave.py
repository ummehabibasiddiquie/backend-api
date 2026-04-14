from flask import Blueprint, request, jsonify
from config import get_db_connection
from utils.target_utils import recalculate_target

leave_bp = Blueprint("leave", __name__)

@leave_bp.route("/apply", methods=["POST"])
def apply_leave():
    data = request.json

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO leaves
        (user_id, leave_type_id, from_date, to_date,
         total_days, is_planned_leave, added_date)
        VALUES (%s,%s,%s,%s,%s,%s,CURDATE())
    """, (
        data["user_id"],
        data["leave_type_id"],
        data["from_date"],
        data["to_date"],
        data["total_days"],
        data["is_planned_leave"]
    ))

    conn.commit()
    return jsonify({"status": 200, "message": "Leave applied"})