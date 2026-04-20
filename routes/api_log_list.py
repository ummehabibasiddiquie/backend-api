from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from datetime import datetime

def get_action_description(api_name):
    mapping = {
        'add_tracker': 'added a tracker',
        'update_tracker': 'updated a tracker',
        'delete_tracker': 'deleted a tracker',
        'view_trackers': 'viewed trackers',
        'add_user_monthly_target': 'added a user monthly target',
        'update_user_monthly_target': 'updated a user monthly target',
        'delete_user_monthly_target': 'deleted a user monthly target',
        'list_user_monthly_targets': 'viewed user monthly targets',
        # Add more mappings as needed
    }
    return mapping.get(api_name, api_name)

api_log_list_bp = Blueprint("api_log_list", __name__)

@api_log_list_bp.route("/logs", methods=["POST"])
def get_api_logs():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT l.*, u.user_name
            FROM api_call_logs l
            LEFT JOIN tfs_user u ON l.user_id = u.user_id
            ORDER BY l.timestamp DESC
        """)
        logs = cursor.fetchall()
        for log in logs:
            log["action"] = f"{log.get('user_name', 'Unknown User')} {get_action_description(log['api_name'])} at {log['timestamp']} from {log.get('device_type', '')} ({log.get('device_id', '')})"
        return api_response(200, "API logs fetched successfully", logs)
    except Exception as e:
        return api_response(500, f"Failed to fetch logs: {str(e)}")
    finally:
        cursor.close()
        conn.close()
