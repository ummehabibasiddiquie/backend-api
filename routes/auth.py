from flask import Blueprint, request
from config import get_db_connection, BASE_UPLOAD_URL, UPLOAD_SUBDIRS
from utils.response import api_response
from datetime import datetime
from utils.validators import (
    is_valid_username,
    is_valid_email,
    is_valid_password,
    is_valid_phone
)
from utils.security import hash_password
from utils.security import verify_password
from utils.image_utils import save_base64_image_as_webp
# from app import BASE_URL
from utils.validators import validate_request

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/user", methods=["POST"])
def user_handler():
    UPLOAD_URL_PREFIX = "/uploads"
    data, err = validate_request(allow_empty_json=False)
    if err:
        return err
    
    is_login_request = set(data.keys()) == {"user_email", "user_password", "device_id", "device_type"}
    
    # LOGIN 
    if is_login_request:
        data, err = validate_request(required=["user_email", "user_password"])
        if err:
            return err

        user_email = data["user_email"].strip().lower()
        user_password = data["user_password"]

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        try:
            cursor.execute("""
                SELECT 
                    u.*,
                    r.project_creation_permission,
                    r.user_creation_permission
                FROM tfs_user u
                LEFT JOIN user_permission r ON u.user_id = r.user_id
                WHERE u.user_email = %s and is_active != 0 and is_delete != 0
            """, (user_email,))

            user = cursor.fetchone()
            cursor.close()
            conn.close()

            if not user:
                return api_response(401, "Invalid email or password")

            if user["is_active"] != 1:
                return api_response(403, "User account is inactive")

            # Password check
            # if not verify_password(
                # user_password,
                # user["user_password"].encode()
                # if isinstance(user["user_password"], str)
                # else user["user_password"]
            # ):
                # return api_response(401, "Invalid email or password")
                
            if not user_password :
                return api_response(401, "Invalid email or password")
            
            if user["profile_picture"] :
                filename = user.get("profile_picture")
                # user["profile_picture"] =  f"{UPLOAD_URL_PREFIX}/{UPLOAD_SUBDIRS['PROFILE_PIC']}/{filename}"
                user["profile_picture"] =  f"{BASE_UPLOAD_URL}/{UPLOAD_SUBDIRS['PROFILE_PIC']}/{filename}"

            # Remove password before sending
            user.pop("user_password", None)

            # Return full flattened JSON object
            return api_response(200, "Login successful", user)
        
        finally :
            cursor.close()
            conn.close()


    # -----------------------------------
    #          REGISTRATION
    # -----------------------------------
    
    data, err = validate_request(required=["user_name", "user_email", "user_password", "role_id"])
    if err:
        return err

    user_name = data["user_name"].strip()
    user_email = data["user_email"].strip().lower()
    user_password = data["user_password"]
    role_id = data["role_id"].strip().lower()
    
    designation_id = data.get("designation_id")
    project_manager = data.get("project_manager")
    assistant_manager = data.get("assistant_manager")
    qa = data.get("qa")
    team = data.get("team")
    user_tenure = data.get("user_tenure")

    user_number = data.get("user_number")
    user_address = data.get("user_address")
    device_id = data["device_id"]
    device_type = data["device_type"]
    
    now = datetime.now()
    formatted_now = now.strftime("%Y-%m-%d %H:%M:%S")
    created_date = formatted_now
    updated_date = formatted_now
    
    is_active = 1
    is_delete = 1
    

    # Validations
    if not is_valid_username(user_name):
        return api_response(400, "Username must contain only alphabets")

    if not is_valid_email(user_email):
        return api_response(400, "Invalid email format")

    if not is_valid_password(user_password):
        return api_response(400, "Password must be at least 6 characters")

    if user_number and not is_valid_phone(user_number):
        return api_response(400, "Invalid phone number")
        
    
    profile_picture_base64 = data.get("profile_picture")
    # profile_picture = data.get("profile_picture")

    if profile_picture_base64 :
        profile_picture = save_base64_image_as_webp(profile_picture_base64,user_name)
        
    # hashed_password = hash_password(user_password)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        # Check existing email
        cursor.execute(
            "SELECT user_id FROM tfs_user WHERE user_email=%s and is_active != 0 and is_delete != 0",
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
            project_manager,
            assistant_manager,
            qa,
            team,
            device_id,
            device_type,
            created_date,
            updated_date
        ))

        new_user_id = cursor.lastrowid

        cursor.execute("""
            SELECT 
                role_name
            FROM user_role
            WHERE role_id = %s
        """, (role_id,))

        role = cursor.fetchone()
        print(role)

        # Permission Logic
        if role["role_name"] in ["qa", "agent"]:
            project_creation_permission = 0
            user_creation_permission = 0
        else:
            project_creation_permission = 1
            user_creation_permission = 1

        print("User id : ",new_user_id)
        # Insert into user_permission
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
        cursor.close()
        conn.close()

# @auth_bp.route('/forgot-password', methods=['POST'])
# def forgot_password():
    # data = request.get_json()
    # user_email = (data.get('user_email') or '').strip().lower()
    # if not is_valid_email(user_email):
    #     return api_response(400, 'Invalid email format')

    # conn = get_db_connection()
    # cursor = conn.cursor(dictionary=True)
    # cursor.execute('SELECT user_id FROM tfs_user WHERE user_email=%s AND is_active=1 AND is_delete=1', (user_email,))
    # user = cursor.fetchone()
    # if not user:
    #     return api_response(404, 'No active user found with this email')

    # # Generate token
    # s = URLSafeTimedSerializer('your-secret-key')
    # token = s.dumps(user_email, salt='password-reset-salt')
    # reset_link = f"{}/reset-password?token={token}"

    # # Send email (simple example, replace with your SMTP config)
    # msg = MIMEMultipart()
    # msg['From'] = 'noreply@yourdomain.com'
    # msg['To'] = user_email
    # msg['Subject'] = 'Password Reset Request'
    # body = f"Click the link to reset your password: <a href='{reset_link}'>{reset_link}</a>\nThis link is valid for 1 hour."
    # msg.attach(MIMEText(body, 'html'))
    # try:
    #     smtp = smtplib.SMTP('localhost')  # Or your SMTP server
    #     smtp.sendmail(msg['From'], [msg['To']], msg.as_string())
    #     smtp.quit()
    # except Exception as e:
    #     return api_response(500, f'Failed to send email: {str(e)}')
    # finally:
    #     cursor.close()
    #     conn.close()
    # return api_response(200, 'Password reset link sent to your email')
