from flask import Blueprint, request
from config import get_db_connection
from utils.response import api_response
from utils.validators import (
    is_valid_username,
    is_valid_email,
    is_valid_password,
    is_valid_phone
)
from utils.security import hash_password
from utils.security import verify_password

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/user", methods=["POST"])
def user_handler():
    data = request.get_json()

    if not data:
        return api_response(400, "Invalid JSON payload")

    # Extract primary fields
    user_email = data.get("user_email")
    user_password = data.get("user_password")

    # Check minimum login requirement
    is_login_request = (
        user_email is not None and
        user_password is not None and
        len(data.keys()) <= 2  # Means only login fields are there
    )

    # LOGIN
    if is_login_request:
        user_email = user_email.strip().lower()

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT 
                u.*, 
                r.role_name,
                r.project_creation_permission,
                r.user_creation_permission
            FROM tfs_user u
            LEFT JOIN user_role r ON u.user_id = r.user_id
            WHERE u.user_email = %s
        """, (user_email,))

        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if not user:
            return api_response(401, "Invalid email or password")

        if user["is_active"] != 1:
            return api_response(403, "User account is inactive")

        # Password check
        if not verify_password(
            user_password,
            user["user_password"].encode()
            if isinstance(user["user_password"], str)
            else user["user_password"]
        ):
            return api_response(401, "Invalid email or password")

        # Remove password before sending
        user.pop("user_password", None)

        # Return full flattened JSON object
        return api_response(200, "Login successful", user)


    # -----------------------------------
    #          REGISTRATION
    # -----------------------------------

    required_fields = [
        "user_name",
        "user_email",
        "user_password",
        "created_date",
        "user_role"
    ]

    for field in required_fields:
        if not data.get(field):
            return api_response(400, f"{field} is required")

    user_name = data["user_name"].strip()
    user_email = data["user_email"].strip().lower()
    user_password = data["user_password"]
    user_role = data["user_role"].strip().lower()
    created_date = data["created_date"]

    profile_picture = data.get("profile_picture")
    user_number = data.get("user_number")
    user_address = data.get("user_address")
    device_id = data.get("device_id")
    updated_date = data.get("updated_date")
    is_active = data.get("is_active", 1)

    # Validations
    if not is_valid_username(user_name):
        return api_response(400, "Username must contain only alphabets")

    if not is_valid_email(user_email):
        return api_response(400, "Invalid email format")

    if not is_valid_password(user_password):
        return api_response(400, "Password must be at least 6 characters")

    if user_number and not is_valid_phone(user_number):
        return api_response(400, "Invalid phone number")

    hashed_password = hash_password(user_password)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        # Check existing email
        cursor.execute(
            "SELECT user_id FROM tfs_user WHERE user_email=%s",
            (user_email,)
        )
        if cursor.fetchone():
            conn.rollback()
            return api_response(409, "User already exists")

        # Insert into tfs_user
        cursor.execute("""
            INSERT INTO tfs_user (
                user_name,
                profile_picture,
                user_number,
                user_address,
                user_email,
                user_password,
                is_active,
                user_role,
                device_id,
                created_date,
                updated_date
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            user_name,
            profile_picture,
            user_number,
            user_address,
            user_email,
            hashed_password,
            is_active,
            user_role,
            device_id,
            created_date,
            updated_date
        ))

        user_id = cursor.lastrowid

        # Permission Logic
        if user_role in ["qa", "agent"]:
            project_creation_permission = 0
            user_creation_permission = 0
        else:
            project_creation_permission = 1
            user_creation_permission = 1

        # Insert into user_role
        cursor.execute("""
            INSERT INTO user_role (
                role_name,
                user_id,
                project_creation_permission,
                user_creation_permission
            )
            VALUES (%s, %s, %s, %s)
        """, (
            user_role,
            user_id,
            project_creation_permission,
            user_creation_permission
        ))

        conn.commit()
        return api_response(201, "User registered successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Registration failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()
