import mysql.connector
import os, uuid


# Project base directory
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Logical upload directory (no full path stored anywhere else)
UPLOAD_DIR = "uploads"
UPLOAD_FOLDER = os.path.join(BASE_DIR, UPLOAD_DIR)

# Sub-folders for different file types
UPLOAD_SUBDIRS = {
    "PROFILE_PIC": "profile_pictures",
    "PROJECT_PPRT": "project_pprt",
    "TASK_FILES": "task_files",
    "TRACKER_FILES": "tracker_files"
}

def get_db_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="",   # default XAMPP password is empty
        database="tfs_hrms"
    )
