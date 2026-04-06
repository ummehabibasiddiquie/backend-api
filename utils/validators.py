from flask import request
from utils.response import api_response
import re

# USERNAME Validation

def is_valid_username(username):
    """Only letters and spaces allowed"""
    if not username:
        return False
    pattern = r'^[A-Za-z ]+$'
    return bool(re.match(pattern, username))

# EMAIL Validation

def is_valid_email(email):
    """Basic email format check"""
    if not email:
        return False
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return bool(re.match(pattern, email))

# PASSWORD Validation

def is_valid_password(password):
    """At least 6 characters"""
    if not password or len(password) < 6:
        return False
    return True

# PHONE NUMBER Validation

def is_valid_phone(number):
    """Only digits, 10-15 characters"""
    if not number:
        return True  # optional field
    pattern = r'^\d{10,15}$'
    return bool(re.match(pattern, number))
    
    
# PROFILE PICTURE (BASE64) Validation

# def is_valid_base64_image(base64_string):
    # if not base64_string:
        # return True  # optional field

    # if not isinstance(base64_string, str):
        # return False

    # pattern = r"^data:image\/(png|jpg|jpeg|webp);base64,[A-Za-z0-9+/=\s]+$"
    # return bool(re.match(pattern, base64_string))


GLOBAL_REQUIRED = ["device_id", "device_type"]

def validate_request(required=None, any_of=None, allow_empty_json=False, include_global=True):
    required = required or []
    any_of = any_of or []

    data = request.get_json(silent=True)

    if data is None:
        return None, api_response(400, "Invalid JSON or no body received")

    if not isinstance(data, dict):
        return None, api_response(400, "Invalid JSON body")

    if not data and not allow_empty_json:
        return None, api_response(400, "Empty JSON body is not allowed")

    # global required fields (device_id/device_type)
    if include_global:
        missing_global = [f for f in GLOBAL_REQUIRED if data.get(f) in (None, "", [])]
        if missing_global:
            return None, api_response(400, f"Missing required field(s): {', '.join(missing_global)}")

    # api required fields
    missing = [f for f in required if data.get(f) in (None, "", [])]
    if missing:
        return None, api_response(400, f"Missing required field(s): {', '.join(missing)}")

    # at least one field from any_of
    if any_of and not any(data.get(f) not in (None, "", []) for f in any_of):
        return None, api_response(400, f"At least one field is required: {', '.join(any_of)}")

    return data, None
