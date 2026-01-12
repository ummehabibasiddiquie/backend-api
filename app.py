from flask import Flask
from routes.auth import auth_bp
from routes.user import user_bp
from routes.project import project_bp
from routes.dropdown import dropdown_bp
from routes.task import task_bp
from routes.tracker import tracker_bp
from routes.user_permission import permission_bp
from routes.dashboard import dashboard_bp

from flask_cors import CORS
import os


app = Flask(__name__)

BASE_URL = os.getenv("BASE_URL", "/")

app.register_blueprint(auth_bp, url_prefix=f"{BASE_URL}/auth")
app.register_blueprint(user_bp, url_prefix=f"{BASE_URL}/user")
app.register_blueprint(project_bp, url_prefix=f"{BASE_URL}/project")
app.register_blueprint(dropdown_bp, url_prefix=f"{BASE_URL}/dropdown")
app.register_blueprint(task_bp, url_prefix=f"{BASE_URL}/task")
app.register_blueprint(tracker_bp, url_prefix=f"{BASE_URL}/tracker")
app.register_blueprint(permission_bp, url_prefix=f"{BASE_URL}/permission")
app.register_blueprint(dashboard_bp,url_prefix=f"{BASE_URL}/dashboard")

# CORS(app, supports_credentials=True)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route("/")
def home():
    return "Flask Auth API is running!"

if __name__ == "__main__":
    # app.run(debug=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
