from flask import Blueprint, request
from utils.api_log_utils import log_api_call
from utils.response import api_response

api_log_bp = Blueprint("api_log", __name__)

@api_log_bp.route("/log_api_call", methods=["POST"])
def log_api_call_endpoint():
    data = request.get_json() or {}
    api_name = data.get("api_name")
    user_id = data.get("user_id")
    device_id = data.get("device_id")
    device_type = data.get("device_type")
    if not api_name:
        return api_response(400, "api_name is required")
    log_api_call(api_name, user_id, device_id, device_type)
    return api_response(200, "API call logged successfully")
