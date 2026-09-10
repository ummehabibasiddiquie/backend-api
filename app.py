from flask import Flask
from routes.auth import auth_bp
from routes.user import user_bp
from routes.project import project_bp
from routes.project_category import project_category_bp
from routes.dropdown import dropdown_bp
from routes.task import task_bp
from routes.tracker import tracker_bp
from routes.user_permission import permission_bp
from routes.dashboard import dashboard_bp
from routes.project_monthly_tracker import project_monthly_tracker_bp
from routes.user_monthly_tracker import user_monthly_tracker_bp
from routes.api_log_list import api_log_list_bp
from routes.password_reset import password_reset_bp
from routes.qc import qc_bp
from routes.qc_afd import qc_afd_bp
from routes.qc_audit import qc_audit_bp
from routes.qc_rework import qc_rework_bp
from routes.qc_history_user_based import qc_history_user_bp
from routes.user_monthly_report import user_monthly_report_bp
from routes.holiday import holiday_bp
from routes.roster import roster_bp
from routes.report_email import report_email_bp
from routes.qa_tracker import qa_tracker_bp
import routes.roster_workflow  # Import workflow routes BEFORE registering blueprint

from scheduler import start_scheduler


from flask_cors import CORS
import os



app = Flask(__name__)

BASE_URL =  ""
# os.getenv("BASE_URL", "/")

app.register_blueprint(auth_bp, url_prefix=f"/auth")
app.register_blueprint(user_bp, url_prefix=f"/user")
app.register_blueprint(project_bp, url_prefix=f"/project")
app.register_blueprint(project_category_bp, url_prefix=f"/project_category")
app.register_blueprint(dropdown_bp, url_prefix=f"/dropdown")
app.register_blueprint(task_bp, url_prefix=f"/task")
app.register_blueprint(tracker_bp, url_prefix=f"/tracker")
app.register_blueprint(permission_bp, url_prefix=f"/permission")
app.register_blueprint(dashboard_bp,url_prefix=f"/dashboard")
app.register_blueprint(project_monthly_tracker_bp,url_prefix=f"/project_monthly_tracker")
app.register_blueprint(user_monthly_tracker_bp,url_prefix=f"/user_monthly_tracker")
app.register_blueprint(api_log_list_bp, url_prefix="/api_log_list")
app.register_blueprint(password_reset_bp, url_prefix="/password_reset")
app.register_blueprint(qc_bp, url_prefix="/qc")
app.register_blueprint(qc_afd_bp, url_prefix="/qc_afd")
app.register_blueprint(qc_audit_bp, url_prefix="/qc_audit")
app.register_blueprint(qc_rework_bp, url_prefix="/qc_rework")
app.register_blueprint(qc_history_user_bp, url_prefix="/qc_history_user")
app.register_blueprint(user_monthly_report_bp, url_prefix="/user_monthly_report")
app.register_blueprint(holiday_bp, url_prefix="/holiday")
app.register_blueprint(roster_bp, url_prefix="/roster")
app.register_blueprint(report_email_bp, url_prefix="/report_email")
app.register_blueprint(qa_tracker_bp, url_prefix="/qa_tracker")

# print("\n==== REGISTERED ROUTES ====")
# for r in app.url_map.iter_rules():
#     print(r, r.methods)
# print("==== END ROUTES ====\n")


# CORS(app, supports_credentials=True)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.route("/")
def home():
    return "Flask Auth API is running!"

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    # Start the scheduler
    start_scheduler()
    # Reloader watches AppData/site-packages on Windows and restarts mid-request,
    # which drops frontend calls (OPTIONS succeeds, POST never hits).
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)