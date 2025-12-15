from flask import Flask
from routes.auth import auth_bp
from routes.user import user_bp
from flask_cors import CORS


app = Flask(__name__)

app.register_blueprint(auth_bp, url_prefix="/auth")
app.register_blueprint(user_bp, url_prefix="/user")

# CORS(app, supports_credentials=True)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route("/")
def home():
    return "Flask Auth API is running!"

if __name__ == "__main__":
    # app.run(debug=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
