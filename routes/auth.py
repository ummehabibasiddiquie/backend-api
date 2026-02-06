from flask import Blueprint, request
from config import get_db_connection, BASE_UPLOAD_URL, UPLOAD_SUBDIRS
from utils.response import api_response
from utils.security import encrypt_password, decrypt_password, safe_decrypt_password
from datetime import datetime
from utils.validators import (
    is_valid_username,
    is_valid_email,
    is_valid_password,
    is_valid_phone
)
from utils.validators import validate_request
import json
import re

auth_bp = Blueprint("auth", __name__)

def _to_id_array_json(val):
    if val is None:
        return json.dumps([])
    if isinstance(val, str) and val.strip() == "":
        return json.dumps([])
    if isinstance(val, list):
        cleaned = []
        for x in val:
            if x is None:
                continue
            s = str(x).strip()
            if s.isdigit():
                cleaned.append(int(s))
        return json.dumps(cleaned)
    if isinstance(val, int):
        return json.dumps([val])
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return json.dumps([int(s)])
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                cleaned = []
                for x in parsed:
                    s2 = str(x).strip()
                    if s2.isdigit():
                        cleaned.append(int(s2))
                return json.dumps(cleaned)
            if isinstance(parsed, int):
                return json.dumps([parsed])
            if isinstance(parsed, str) and parsed.strip().isdigit():
                return json.dumps([int(parsed.strip())])
        except Exception:
            pass
    return json.dumps([])

def safe_filename_part(value: str) -> str:
    if value is None:
        return "NA"
    s = str(value).strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_]", "", s)
    return s or "NA"

def build_profile_pic_filename(user_name: str, original_filename: str) -> str:
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")
    ext = original_filename.rsplit(".", 1)[1].lower().strip()

    now = datetime.now()
    date_part = now.strftime("%d-%b-%Y")   # 05-Feb-2026
    time_part = now.strftime("%I%p")       # 10AM / 09PM
    return f"{safe_filename_part(user_name)}_{date_part}_{time_part}.{ext}"

@auth_bp.route("/user", methods=["POST"])
def user_handler():

    # -------------------------
    # Detect request type
    # -------------------------
    is_multipart = (request.content_type or "").startswith("multipart/form-data")

    # =========================================================
    # LOGIN (JSON only)
    # =========================================================
    if not is_multipart:
        data, err = validate_request(allow_empty_json=False)
        if err:
            return err

        is_login_request = set(data.keys()) == {"user_email", "user_password", "device_id", "device_type"}
        if not is_login_request:
            return api_response(400, "Invalid request format for login")

        user_email = data["user_email"].strip().lower()
        user_password = data["user_password"]

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute("""
                SELECT 
                    u.*,
                    p.project_creation_permission,
                    p.user_creation_permission
                FROM tfs_user u
                LEFT JOIN user_permission p ON u.user_id = p.user_id
                WHERE u.user_email = %s
                  AND u.is_delete != 0
                LIMIT 1
            """, (user_email,))
            user = cursor.fetchone()

            if not user:
                return api_response(401, "Invalid email or password")

            if user.get("is_active") != 1:
                return api_response(403, "User account is inactive")

            stored_password = user.get("user_password")
            if stored_password is None:
                return api_response(401, "Invalid email or password")
            
            # Use safe_decrypt_password to handle both encrypted and plain text
            # This function returns decrypted password or original plain text
            final_password = safe_decrypt_password(stored_password)
            
            if user_password != final_password:
                return api_response(401, "Invalid email or password")

            if user.get("profile_picture"):
                filename = user.get("profile_picture")
                user["profile_picture"] = f"{BASE_UPLOAD_URL}/{UPLOAD_SUBDIRS['PROFILE_PIC']}/{filename}"
            else:
                user["profile_picture"] = None

            user.pop("user_password", None)
            return api_response(200, "Login successful", user)

        finally:
            try: cursor.close()
            except: pass
            try: conn.close()
            except: pass

    # =========================================================
    # REGISTRATION (multipart/form-data)
    # =========================================================
    form = request.form

    required = ["user_name", "user_email", "user_password", "role_id"]
    for f in required:
        if not form.get(f):
            return api_response(400, f"{f} is required")

    user_name = form["user_name"].strip()
    user_email = form["user_email"].strip().lower()
    # user_password = form["user_password"]
    user_password = encrypt_password(form["user_password"])
    role_id = str(form["role_id"]).strip().lower()

    designation_id = form.get("designation_id")
    team = form.get("team")
    user_tenure = form.get("user_tenure")

    user_number = form.get("user_number")
    user_address = form.get("user_address")
    device_id = form.get("device_id")
    device_type = form.get("device_type")

    project_manager = _to_id_array_json(form.get("project_manager"))
    assistant_manager = _to_id_array_json(form.get("assistant_manager"))
    qa = _to_id_array_json(form.get("qa"))

    if not is_valid_username(user_name):
        return api_response(400, "Username must contain only alphabets")

    if not is_valid_email(user_email):
        return api_response(400, "Invalid email format")

    if not is_valid_password(user_password):
        return api_response(400, "Password must be at least 6 characters")

    if user_number and not is_valid_phone(user_number):
        return api_response(400, "Invalid phone number")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ✅ file upload (profile_picture)
    profile_picture = None
    uploaded = request.files.get("profile_picture")
    if uploaded and uploaded.filename:
        try:
            from utils.file_utils import save_uploaded_file
            custom_filename = build_profile_pic_filename(user_name, uploaded.filename)
            profile_picture = save_uploaded_file(uploaded, UPLOAD_SUBDIRS["PROFILE_PIC"], custom_filename)
        except ValueError as e:
            return api_response(400, str(e))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        cursor.execute(
            "SELECT user_id FROM tfs_user WHERE user_email=%s and is_active != 0 and is_delete != 0",
            (user_email,)
        )
        if cursor.fetchone():
            conn.rollback()
            return api_response(409, "User already exists")

        cursor.execute("""
            INSERT INTO tfs_user (
                user_name,
                profile_picture,
                profile_picture_base64,
                user_number,
                user_address,
                user_email,
                user_password,
                is_active,
                is_delete,
                role_id,
                designation_id,
                user_tenure,
                project_manager_id,
                asst_manager_id,
                qa_id,
                team_id,
                device_id,
                device_type,
                created_date,
                updated_date
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            user_name,
            profile_picture,
            None,              # ✅ remove base64
            user_number,
            user_address,
            user_email,
            user_password,
            1,
            1,
            role_id,
            designation_id,
            user_tenure,
            project_manager,
            assistant_manager,
            qa,
            team,
            device_id,
            device_type,
            now,
            now
        ))

        new_user_id = cursor.lastrowid

        cursor.execute("""SELECT role_name FROM user_role WHERE role_id=%s""", (role_id,))
        role = cursor.fetchone()

        if role and role.get("role_name") in ["qa", "agent"]:
            project_creation_permission = 0
            user_creation_permission = 0
        else:
            project_creation_permission = 1
            user_creation_permission = 1

        cursor.execute("""
            INSERT INTO user_permission (
                role_id,
                user_id,
                project_creation_permission,
                user_creation_permission
            )
            VALUES (%s, %s, %s, %s)
        """, (
            role_id,
            new_user_id,
            project_creation_permission,
            user_creation_permission
        ))

        conn.commit()
        return api_response(201, "User registered successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Registration failed: {str(e)}")

    finally:
        try: cursor.close()
        except: pass
        try: conn.close()
        except: pass
