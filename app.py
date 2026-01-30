from flask import Flask
from routes.auth import auth_bp
from routes.user import user_bp
from routes.project import project_bp
from routes.dropdown import dropdown_bp
from routes.task import task_bp
from routes.tracker import tracker_bp
from routes.user_permission import permission_bp
from routes.dashboard import dashboard_bp
from routes.project_monthly_tracker import project_monthly_tracker_bp
from routes.user_monthly_tracker import user_monthly_tracker_bp
from routes.api_log_list import api_log_list_bp

from flask_cors import CORS
import os


app = Flask(__name__)

BASE_URL =  ""
# os.getenv("BASE_URL", "/")

app.register_blueprint(auth_bp, url_prefix=f"/auth")
app.register_blueprint(user_bp, url_prefix=f"/user")
app.register_blueprint(project_bp, url_prefix=f"/project")
app.register_blueprint(dropdown_bp, url_prefix=f"/dropdown")
app.register_blueprint(task_bp, url_prefix=f"/task")
app.register_blueprint(tracker_bp, url_prefix=f"/tracker")
app.register_blueprint(permission_bp, url_prefix=f"/permission")
app.register_blueprint(dashboard_bp,url_prefix=f"/dashboard")
app.register_blueprint(project_monthly_tracker_bp,url_prefix=f"/project_monthly_tracker")
app.register_blueprint(user_monthly_tracker_bp,url_prefix=f"/user_monthly_tracker")
app.register_blueprint(api_log_list_bp, url_prefix="/api_log_list")

# CORS(app, supports_credentials=True)
CORS(app, resources={r"/*": {"origins": "*"}})


@app.route("/")
def home():
    return "Flask Auth API is running!"

@app.route("/uploads/<path:filename>")
def serve_uploads(filename):
    from config import UPLOAD_FOLDER
    from flask import send_from_directory
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == "__main__":
    # app.run(debug=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
