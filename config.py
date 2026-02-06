import mysql.connector
import os, uuid
from dotenv import load_dotenv

load_dotenv()


# Project base directory
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Logical upload directory (no full path stored anywhere else)
UPLOAD_DIR = "uploads"
UPLOAD_FOLDER = os.path.join(BASE_DIR, UPLOAD_DIR)

# Web-accessible base URL for uploads (matches Nginx /python/ prefix)
# BASE_UPLOAD_URL = os.getenv("BASE_UPLOAD_URL", "/python/uploads")
BASE_UPLOAD_URL = os.getenv("BASE_UPLOAD_URL", "/python/uploads")

# Sub-folders for different file types
UPLOAD_SUBDIRS = {
    "PROFILE_PIC": "profile_pictures",
    "PROJECT_PPRT": "project_pprt",
    "TASK_FILES": "task_files",
    "TRACKER_FILES": "tracker_files",
}

RESET_SECRET_KEY = os.getenv("RESET_SECRET_KEY")
RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "300"))
RESET_FRONTEND_URL = os.getenv("RESET_FRONTEND_URL")

if not RESET_SECRET_KEY:
    raise RuntimeError("RESET_SECRET_KEY is missing")


# Check if encryption key exists and is valid
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    print("WARNING: ENCRYPTION_KEY is missing from .env file. A new key will be generated.")
else:
    try:
        from cryptography.fernet import Fernet
        Fernet(ENCRYPTION_KEY.encode())
        print("✅ ENCRYPTION_KEY is valid")
    except Exception as e:
        print(f"⚠️  Invalid ENCRYPTION_KEY format: {e}")
        print("A new key will be generated. Please update your .env file.")
        
        
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),  # Use env var or default to 'localhost'
        port=int(os.getenv("DB_PORT", 3306)),  # Use env var or default to 3306
        user=os.getenv("DB_USERNAME"),  # Use env var or default to 'root'
        password=os.getenv("DB_PASSWORD", ""),  # Use env var or default to empty string
        database=os.getenv(
            "DB_DATABASE", "tfs_hrms"
        ),  # Use env var or default to 'tfs_hrms'
    )
    
    # Environment validation on startup
def validate_environment():
    """Validate all required environment variables"""
    required_vars = {
        "DB_HOST": "Database host",
        "DB_USERNAME": "Database username", 
        "DB_DATABASE": "Database name",
        "RESET_SECRET_KEY": "Reset secret key"
    }
    
    missing_vars = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing_vars.append(f"{var} ({description})")
    
    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease add these to your .env file.")
        return False
    
    print("✅ All required environment variables are present")
    return True

# Run validation on import
validate_environment()

    # print("DB USER:", os.getenv("DB_USERNAME"))
    # print("DB PASS:", os.getenv("DB_PASSWORD"))
    # print("DB NAME:", os.getenv("DB_DATABASE"))
