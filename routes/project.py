from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection
from utils.cloudinary_utils import upload_to_cloudinary, delete_from_cloudinary, FOLDER_PROJECT
from utils.file_utils import is_allowed_file
import json
from datetime import datetime

project_bp = Blueprint("project", __name__)

# ---------------- HELPERS ---------------- #

def safe_filename_part(value: str):
    if not value:
        return "NA"
    return value.strip().replace(" ", "_")


def build_project_filename(project_name, project_code, original_filename, index, total):
    ext = original_filename.rsplit(".", 1)[1].lower()
    date_part = datetime.now().strftime("%d-%b-%Y")

    name_part = safe_filename_part(project_name)
    code_part = safe_filename_part(project_code)

    suffix = f"_{index}" if total > 1 else ""

    return f"{name_part}_{code_part}_{date_part}{suffix}.{ext}"


def parse_db_files(val):

    if not val:
        return []

    if isinstance(val, list):
        return val

    if isinstance(val, str):
        try:
            return json.loads(val)
        except:
            return [val]

    return []


def safe_delete_cloudinary_project_files(file_list):

    for f in file_list or []:
        try:
            delete_from_cloudinary(f, resource_type="raw")
        except Exception as e:
            print("Cloudinary delete failed:", e)


def _get_json_list(form, key):

    raw = form.get(key)

    if not raw:
        return []

    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return val
    except:
        pass

    return []


def _get_uploaded_files():
    files = request.files.getlist("files")
    if not files:
        files = request.files.getlist("file")
    return [f for f in files if f and f.filename]


# ---------------- CREATE PROJECT ---------------- #

@project_bp.route("/create", methods=["POST"])
def create_project():

    form = request.form

    required_fields = ["project_name", "project_code", "project_manager_id"]

    for f in required_fields:
        if not form.get(f):
            return api_response(400, f"{f} is required")

    project_name = form.get("project_name").strip()
    project_code = form.get("project_code").strip()
    project_description = form.get("project_description")

    if project_description == "null":
        project_description = None

    project_manager_id = form.get("project_manager_id")

    asst_project_manager_id = _get_json_list(form, "asst_project_manager_id")
    project_team_id = _get_json_list(form, "project_team_id")
    project_qa_id = _get_json_list(form, "project_qa_id")

    project_category_id = form.get("project_category_id")

    # NEW FLAGS
    requires_ai_evaluation = str(form.get("requires_ai_evaluation", "false")).lower() in ("true", "1")
    requires_duplicate_check = str(form.get("requires_duplicate_check", "false")).lower() in ("true", "1")

    uploaded_files = _get_uploaded_files()

    saved_urls = []

    try:

        total = len(uploaded_files)

        for idx, fs in enumerate(uploaded_files, start=1):

            if not is_allowed_file(fs.filename):
                raise ValueError(f"Unsupported file type: {fs.filename}")

            custom_name = build_project_filename(
                project_name,
                project_code,
                fs.filename,
                idx,
                total
            )

            url, _ = upload_to_cloudinary(
                fs,
                FOLDER_PROJECT,
                display_name=custom_name,
                resource_type="raw"
            )

            saved_urls.append(url)

    except Exception as e:

        safe_delete_cloudinary_project_files(saved_urls)

        return api_response(400, f"File upload failed: {str(e)}")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:

        conn.start_transaction()

        cursor.execute(
            """
            INSERT INTO project (
                project_name,
                project_code,
                project_description,
                project_manager_id,
                asst_project_manager_id,
                project_team_id,
                project_qa_id,
                project_pprt,
                project_category_id,
                ai_evaluation,
                duplicate_check,
                created_date,
                updated_date,
                is_active
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            """,
            (
                project_name,
                project_code,
                project_description,
                project_manager_id,
                json.dumps(asst_project_manager_id),
                json.dumps(project_team_id),
                json.dumps(project_qa_id),
                json.dumps(saved_urls),
                project_category_id,
                requires_ai_evaluation,
                requires_duplicate_check,
                now,
                now
            )
        )

        conn.commit()

        return api_response(
            201,
            "Project created successfully",
            {
                "files": saved_urls,
                "requires_ai_evaluation": requires_ai_evaluation,
                "requires_duplicate_check": requires_duplicate_check
            }
        )

    except Exception as e:

        conn.rollback()
        safe_delete_cloudinary_project_files(saved_urls)

        return api_response(500, f"Project creation failed: {str(e)}")

    finally:

        cursor.close()
        conn.close()


# ---------------- UPDATE PROJECT ---------------- #

@project_bp.route("/update", methods=["POST"])
def update_project():

    form = request.form
    project_id = form.get("project_id")

    if not project_id:
        return api_response(400, "project_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        cursor.execute(
            "SELECT * FROM project WHERE project_id=%s AND is_active=1",
            (project_id,)
        )

        existing = cursor.fetchone()

        if not existing:
            return api_response(404, "Project not found")

        update_values = {}

        # normal fields
        for key in [
            "project_name",
            "project_code",
            "project_description",
            "project_manager_id",
            "project_category_id",
        ]:

            if form.get(key) is not None:

                val = form.get(key)

                if key in ["project_name", "project_code"]:
                    val = val.strip()

                if key == "project_description" and val == "null":
                    val = None

                update_values[key] = val
        
        # json list fields
        for key in [
            "asst_project_manager_id",
            "project_team_id",
            "project_qa_id",
        ]:
            if form.get(key) is not None:
                update_values[key] = json.dumps(_get_json_list(form, key))


        # FLAG MAPPING (API -> DB)

        if form.get("requires_ai_evaluation") is not None:
            update_values["ai_evaluation"] = str(form.get("requires_ai_evaluation")).lower() in ("true", "1")

        if form.get("requires_duplicate_check") is not None:
            update_values["duplicate_check"] = str(form.get("requires_duplicate_check")).lower() in ("true", "1")
            
        if not update_values:
            return api_response(400, "No fields to update")

        set_clause = ", ".join(f"{k}=%s" for k in update_values.keys())

        params = list(update_values.values()) + [project_id]

        cursor.execute(
            f"""
            UPDATE project
            SET {set_clause},
            updated_date = NOW()
            WHERE project_id=%s
            """,
            tuple(params)
        )

        conn.commit()

        return api_response(200, "Project updated successfully")

    except Exception as e:

        conn.rollback()
        return api_response(500, f"Project update failed: {str(e)}")

    finally:

        cursor.close()
        conn.close()


# ---------------- LIST PROJECTS ---------------- #

@project_bp.route("/list", methods=["POST"])
def list_projects():

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        cursor.execute("""
            SELECT
                project_id,
                project_name,
                project_code,
                project_description,
                project_manager_id,
                asst_project_manager_id,
                project_team_id,
                project_qa_id,
                project_category_id,
                project_pprt,
                ai_evaluation,
                duplicate_check,
                created_date,
                updated_date
            FROM project
            WHERE is_active = 1
            ORDER BY project_id DESC
        """)

        projects = cursor.fetchall()

        result = []

        for proj in projects:

            result.append({

                "project_id": proj["project_id"],
                "project_name": proj["project_name"],
                "project_code": proj["project_code"],
                "project_description": proj["project_description"],

                "project_manager_id": proj["project_manager_id"],
                "asst_project_manager_id": json.loads(proj.get("asst_project_manager_id") or "[]"),
                "project_team_id": json.loads(proj.get("project_team_id") or "[]"),
                "project_qa_id": json.loads(proj.get("project_qa_id") or "[]"),

                "project_category_id": proj["project_category_id"],

                "project_files": parse_db_files(proj["project_pprt"]),

                "requires_ai_evaluation": bool(proj["ai_evaluation"]),
                "requires_duplicate_check": bool(proj["duplicate_check"]),

                "created_date": proj["created_date"],
                "updated_date": proj["updated_date"]

            })

        return api_response(200, "Projects fetched successfully", result)

    except Exception as e:

        return api_response(500, f"Failed to fetch projects: {str(e)}")

    finally:

        cursor.close()
        conn.close()


# ---------------- DELETE PROJECT ---------------- #

@project_bp.route("/delete", methods=["POST"])
def delete_project():

    data = request.get_json() or {}

    project_id = data.get("project_id")

    if not project_id:
        return api_response(400, "project_id is required")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:

        conn.start_transaction()

        cursor.execute(
            "SELECT project_pprt FROM project WHERE project_id=%s AND is_active=1",
            (project_id,)
        )

        project = cursor.fetchone()

        if not project:
            return api_response(404, "Project not found")

        files = parse_db_files(project.get("project_pprt"))

        safe_delete_cloudinary_project_files(files)

        cursor.execute(
            """
            UPDATE project
            SET is_active = 0,
                updated_date = NOW()
            WHERE project_id = %s
            """,
            (project_id,)
        )

        conn.commit()

        return api_response(200, "Project deleted successfully")

    except Exception as e:

        conn.rollback()
        return api_response(500, f"Project delete failed: {str(e)}")

    finally:

        cursor.close()
        conn.close()