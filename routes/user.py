# routes/user.py
from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection, UPLOAD_SUBDIRS, BASE_UPLOAD_URL, UPLOAD_FOLDER
from utils.security import decrypt_password, encrypt_password, safe_decrypt_password
from utils.validators import validate_request
from utils.json_utils import to_db_json
from datetime import datetime
import json
import os
import re

user_bp = Blueprint("user", __name__)


# ------------------------
# HELPERS (existing)
# ------------------------
def _safe_json_list(val):
    """
    Converts DB value to list of ints.
    Handles: None, '[]', '[112,113]', 112, '112'
    """
    if val is None:
        return []

    if isinstance(val, list):
        return [int(x) for x in val if str(x).strip().isdigit()]

    if isinstance(val, int):
        return [val]

    if isinstance(val, str):
        s = val.strip()
        if not s:
            return []
        if s.isdigit():
            return [int(s)]
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if str(x).strip().isdigit()]
            if isinstance(parsed, int):
                return [parsed]
            if isinstance(parsed, str) and parsed.isdigit():
                return [int(parsed)]
        except Exception:
            return []

    return []


# ------------------------
# ABSOLUTE URL HELPERS
# ------------------------
def get_public_upload_base() -> str:
    """
    Builds absolute base like:
      https://tfshrms.cloud + /python/uploads
    request.host_url already contains scheme + host + trailing slash.
    """
    return request.host_url.rstrip("/") + (BASE_UPLOAD_URL or "")


def _attach_profile_picture_url(users):
    """
    Ensures profile_picture is returned as absolute URL.
    """
    base = get_public_upload_base().rstrip("/")
    sub = str(UPLOAD_SUBDIRS["PROFILE_PIC"]).strip("/")

    for u in users:
        filename = u.get("profile_picture")
        if filename:
            filename = os.path.basename(str(filename))  # safety
            u["profile_picture"] = f"{base}/{sub}/{filename}"
        else:
            u["profile_picture"] = None
    return users


# ------------------------
# NEW: safe filename + delete helpers (user-specific, NOT in file_utils)
# ------------------------
def safe_filename_part(value: str) -> str:
    if value is None:
        return "NA"
    s = str(value).strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_]", "", s)
    return s or "NA"


def build_profile_pic_filename(user_name: str, original_filename: str) -> str:
    """
    Format:
      <username>_<DD-Mon-YYYY>_<HHAM/HHPM>.<ext>
    """
    if "." not in (original_filename or ""):
        raise ValueError("Uploaded file has no extension")

    ext = original_filename.rsplit(".", 1)[1].lower().strip()

    now = datetime.now()
    date_part = now.strftime("%d-%b-%Y")  # 05-Feb-2026
    time_part = now.strftime("%I%p")      # 10AM / 09PM

    return f"{safe_filename_part(user_name)}_{date_part}_{time_part}.{ext}"


def safe_remove_profile_pic(filename: str) -> bool:
    """
    Deletes profile picture file if exists.
    Uses commonpath() (Windows-safe containment check).
    """
    if not filename:
        return False

    filename = os.path.basename(str(filename))
    profile_dir = os.path.abspath(os.path.join(UPLOAD_FOLDER, UPLOAD_SUBDIRS["PROFILE_PIC"]))
    abs_path = os.path.abspath(os.path.join(profile_dir, filename))

    # containment check
    if os.path.commonpath([profile_dir, abs_path]) != profile_dir:
        raise ValueError("Invalid file path")

    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True

    return False


# ------------------------
# LIST USERS
# ------------------------
@user_bp.route("/list", methods=["POST"])
def list_users():
    data, err = validate_request(required=["user_id"])
    if err:
        return err

    user_id = data.get("user_id")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT r.role_name
            FROM tfs_user u
            JOIN user_role r ON r.role_id = u.role_id
            WHERE u.user_id = %s AND u.is_active = 1 AND u.is_delete = 1
        """, (user_id,))
        role_row = cursor.fetchone()

        if not role_row:
            return api_response(404, "User not found")

        role = (role_row["role_name"] or "").strip().lower()

        if role == "agent":
            return api_response(200, "No users available", [])

        query = """
            SELECT
                u.user_id,
                u.user_name,
                u.user_email,
                u.user_number,
                u.user_address,
                u.user_password,
                u.user_tenure,
                u.profile_picture,
                u.is_active,
                u.project_manager_id,
                u.asst_manager_id,
                u.qa_id,
                r.role_name AS role,
                t.team_name,
                d.designation_id,
                d.designation
            FROM tfs_user u
            LEFT JOIN user_role r ON r.role_id = u.role_id
            LEFT JOIN user_designation d ON d.designation_id = u.designation_id
            LEFT JOIN team t ON u.team_id = t.team_id
            WHERE u.is_delete = 1
        """

        params: list = []

        # ✅ Role-based filtering (MariaDB-safe; supports BOTH JSON arrays and comma/bracket strings)
        # This avoids: invalid JSON errors + missing matches when stored value isn't valid JSON
        if role == "qa":
            query += """
                AND (
                    TRIM(COALESCE(u.qa_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(u.qa_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.append(str(int(user_id)))   # exact match
            params.append(str(int(user_id)))   # FIND_IN_SET

        elif role == "assistant manager":
            query += """
                AND (
                    TRIM(COALESCE(u.asst_manager_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(u.asst_manager_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.append(str(int(user_id)))   # exact match
            params.append(str(int(user_id)))   # FIND_IN_SET

        elif role in ["manager", "project manager"]:
            query += """
                AND (
                    TRIM(COALESCE(u.project_manager_id, '')) = %s
                    OR FIND_IN_SET(
                        %s,
                        REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(u.project_manager_id,''), '[',''), ']',''), '"',''), ' ', '')
                    ) > 0
                )
            """
            params.append(str(int(user_id)))   # exact match
            params.append(str(int(user_id)))   # FIND_IN_SET

        query += " ORDER BY u.user_id DESC"

        cursor.execute(query, tuple(params))
        users = cursor.fetchall()

        # Resolve project/asst/qa names
        all_ref_ids = set()
        for u in users:
            all_ref_ids.update(_safe_json_list(u.get("project_manager_id")))
            all_ref_ids.update(_safe_json_list(u.get("asst_manager_id")))
            all_ref_ids.update(_safe_json_list(u.get("qa_id")))

        id_to_user = {}
        if all_ref_ids:
            placeholders = ", ".join(["%s"] * len(all_ref_ids))
            cursor.execute(
                f"SELECT user_id, user_name FROM tfs_user WHERE user_id IN ({placeholders})",
                tuple(all_ref_ids)
            )
            rows = cursor.fetchall() or []
            id_to_user = {int(r["user_id"]): r["user_name"] for r in rows}

        for u in users:
            pm_ids = _safe_json_list(u.get("project_manager_id"))
            am_ids = _safe_json_list(u.get("asst_manager_id"))
            qa_ids = _safe_json_list(u.get("qa_id"))

            u["project_managers"] = [{"user_id": i, "user_name": id_to_user.get(i)} for i in pm_ids]
            u["asst_managers"] = [{"user_id": i, "user_name": id_to_user.get(i)} for i in am_ids]
            u["qas"] = [{"user_id": i, "user_name": id_to_user.get(i)} for i in qa_ids]

            u["project_manager_names"] = ", ".join([id_to_user.get(i) for i in pm_ids if id_to_user.get(i)]) or None
            u["asst_manager_names"] = ", ".join([id_to_user.get(i) for i in am_ids if id_to_user.get(i)]) or None
            u["qa_names"] = ", ".join([id_to_user.get(i) for i in qa_ids if id_to_user.get(i)]) or None

        # ✅ absolute url
        _attach_profile_picture_url(users)

        # ✅ decrypt passwords for frontend display (handles encrypted/plain)
        for user in users:
            if user.get("user_password"):
                user["user_password"] = safe_decrypt_password(user["user_password"])

        return api_response(200, "Users fetched successfully", users)

    except Exception as e:
        return api_response(500, f"Failed to fetch users: {str(e)}")

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass




# ------------------------
# UPDATE USER (multipart/form-data for file)
# ------------------------
@user_bp.route("/update_user", methods=["POST"])
def update_user():
    form = request.form
    user_id = form.get("user_id")
    if not user_id:
        return api_response(400, "user_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT user_id, user_name, profile_picture FROM tfs_user WHERE user_id=%s", (user_id,))
        existing = cursor.fetchone()
        if not existing:
            return api_response(404, "User not found")

        old_profile_file = existing.get("profile_picture")
        existing_name = existing.get("user_name") or "USER"

        user_fields = {
            "user_name": form.get("user_name"),
            "user_number": form.get("user_number"),
            "user_address": form.get("user_address"),
            "role_id": form.get("role_id"),
            "designation_id": form.get("designation_id"),
            "reporting_manager": form.get("reporting_manager"),
            "is_active": form.get("is_active"),
            "user_tenure": form.get("user_tenure"),
            "team_id": form.get("team_id"),
            "project_manager_id": to_db_json(form.get("project_manager_id"), allow_single=True),
            "asst_manager_id": to_db_json(form.get("asst_manager_id"), allow_single=True),
            "qa_id": to_db_json(form.get("qa_id"), allow_single=True),
        }

        # Encrypt password if provided
        user_password = form.get("user_password")
        if user_password:
            user_fields["user_password"] = encrypt_password(user_password)
            
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        user_update_cols = []
        user_update_vals = []

        # --- file handling
        uploaded = request.files.get("profile_picture")
        if uploaded and uploaded.filename:
            from utils.file_utils import save_uploaded_file  # generic

            use_name = (form.get("user_name") or existing_name)
            custom_filename = build_profile_pic_filename(use_name, uploaded.filename)

            # save new first
            new_filename = save_uploaded_file(
                uploaded,
                UPLOAD_SUBDIRS["PROFILE_PIC"],
                custom_filename
            )

            # delete old after successful save
            try:
                safe_remove_profile_pic(old_profile_file)
            except Exception as e:
                # don't fail update; but log reason
                print("DELETE FAILED (user update):", e, "old_file=", old_profile_file)

            user_fields["profile_picture"] = new_filename
            user_fields["profile_picture_base64"] = None  # clear base64 if column exists

        # build update
        for col, val in user_fields.items():
            if val is not None:
                user_update_cols.append(f"{col} = %s")
                user_update_vals.append(val)

        if not user_update_cols:
            return api_response(400, "No valid fields provided for update")

        user_update_cols.append("updated_date = %s")
        user_update_vals.append(now_str)

        update_user_query = f"""
            UPDATE tfs_user
            SET {', '.join(user_update_cols)}
            WHERE user_id = %s
        """
        user_update_vals.append(user_id)
        cursor.execute(update_user_query, user_update_vals)

        conn.commit()
        return api_response(200, "User updated successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to update user: {str(e)}")

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ------------------------
# DELETE USER (soft delete + remove file)
# ------------------------
@user_bp.route("/delete_user", methods=["PUT"])
def delete_user():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return api_response(400, "user_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT profile_picture FROM tfs_user WHERE user_id=%s", (user_id,))
        row = cursor.fetchone()
        if not row:
            return api_response(404, "User not found")

        profile_file = row.get("profile_picture")

        cursor.execute("""
            UPDATE tfs_user
            SET is_delete = 0, is_active = 0
            WHERE user_id = %s
        """, (user_id,))
        conn.commit()

        try:
            safe_remove_profile_pic(profile_file)
        except Exception as e:
            print("DELETE FAILED (user delete):", e, "file=", profile_file)

        return api_response(200, "User Deleted successfully")

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Failed to Delete user: {str(e)}")

    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
