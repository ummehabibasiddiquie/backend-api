import base64
import os
import uuid
import mimetypes
from config import UPLOAD_FOLDER, UPLOAD_SUBDIRS
import re
from werkzeug.utils import secure_filename
from flask import current_app

def _safe_filename_part(value: str) -> str:
    if value is None:
        return "NA"
    s = str(value).strip().replace(" ", "_")
    # remove characters that are not allowed in filenames (Windows-safe)
    for ch in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
        s = s.replace(ch, "")
    return s or "NA"


def _safe_filename(name: str) -> str:
    """
    Converts to a filesystem-safe filename stem (no extension).
    Keeps letters/numbers/_/-
    """
    name = (name or "").strip()
    name = re.sub(r"[^\w\-]+", "_", name)   # replace other chars with _
    name = re.sub(r"_+", "_", name).strip("_")
    return name or str(uuid.uuid4())


def _detect_extension_from_header(header: str, default_ext: str = "bin") -> str:
    header = (header or "").lower()

    # common types - extend if needed
    if "application/pdf" in header:
        return "pdf"
    if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in header:
        return "xlsx"
    if "application/vnd.ms-excel" in header:
        return "xls"
    if "text/csv" in header:
        return "csv"
    if "image/png" in header:
        return "png"
    if "image/jpeg" in header or "image/jpg" in header:
        return "jpg"
    if "image/webp" in header:
        return "webp"

    return default_ext


def save_base64_file(
    base64_str,
    upload_subdir,
    custom_name=None,
    force_ext=None,
    default_ext="bin"
):
    """
    Backward compatible.

    Existing usage (works unchanged):
        save_base64_file(base64_str, upload_subdir)

    New optional usage:
        save_base64_file(base64_str, upload_subdir, custom_name="TC_task_user_03_02_2026_12PM")

    Params:
      - custom_name: filename WITHOUT extension (recommended). If extension is included, it's kept.
      - force_ext: if you want to force a specific extension like "pdf" or "xlsx"
      - default_ext: used if header doesn't provide a known type

    Returns:
      - saved filename (not full path)
    """
    if not base64_str:
        return None

    # split data URL if present
    if isinstance(base64_str, str) and "," in base64_str:
        header, b64_data = base64_str.split(",", 1)
    else:
        header, b64_data = "", base64_str

    ext = force_ext or _detect_extension_from_header(header, default_ext=default_ext)

    # ensure folder
    os.makedirs(upload_subdir, exist_ok=True)

    # decide filename stem
    if custom_name:
        stem = _safe_filename(custom_name)
    else:
        stem = str(uuid.uuid4())

    # if custom_name already contains ".ext", keep it
    filename = stem
    if "." not in filename:
        filename = f"{stem}.{ext}"
    else:
        # ensure it ends with ext if force_ext provided
        if force_ext and not filename.lower().endswith("." + force_ext.lower()):
            filename = f"{stem}.{force_ext}"

    file_path = os.path.join(upload_subdir, filename)

    # decode and save
    with open(file_path, "wb") as f:
        f.write(base64.b64decode(b64_data))

    return filename

ALLOWED_EXTENSIONS = {"pdf","png","jpg","jpeg","xlsx","xls","csv","doc","docx","txt"}

def is_allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file_storage, upload_subdir: str, custom_filename: str) -> str:
    """
    Generic save function.
    Caller decides the filename.
    """
    if not file_storage or file_storage.filename == "":
        return None

    filename = secure_filename(custom_filename)

    if not is_allowed_file(filename):
        raise ValueError("Unsupported file type")

    target_dir = os.path.join(UPLOAD_FOLDER, upload_subdir)
    os.makedirs(target_dir, exist_ok=True)

    full_path = os.path.join(target_dir, filename)
    file_storage.save(full_path)

    return filename
