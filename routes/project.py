from flask import Blueprint, request
from utils.response import api_response
from config import get_db_connection

project_bp = Blueprint("project", __name__)
    

@project_bp.route("/create", methods=["POST"])
def create_project():
    data = request.get_json()

    # Required fields
    required_fields = ["project_name", "manager_id"]
    for field in required_fields:
        if not data.get(field):
            return api_response(400, f"{field} is required")

    project_name = data["project_name"].strip()
    manager_id = data["manager_id"]
    assistant_manager_id = data.get("assistant_manager_id")
    qa_id = data.get("qa_id")
    no_of_agents = data.get("no_of_agents", 0)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        conn.start_transaction()

        # Insert project
        cursor.execute("""
            INSERT INTO tfs_project 
            (project_name, manager_id, assistant_manager_id, qa_id, no_of_agents)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            project_name,
            manager_id,
            assistant_manager_id,
            qa_id,
            no_of_agents
        ))

        project_id = cursor.lastrowid
        conn.commit()

        return api_response(201, "Project created successfully", {"project_id": project_id})

    except Exception as e:
        conn.rollback()
        return api_response(500, f"Project creation failed: {str(e)}")

    finally:
        cursor.close()
        conn.close()
