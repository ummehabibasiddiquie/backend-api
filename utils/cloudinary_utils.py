import os
import uuid
import cloudinary
import cloudinary.uploader
import cloudinary.api

# ── Folder constants ──────────────────────────────────────────────────────────
FOLDER_TRACKER  = "hrms/tracker_files"
FOLDER_PROJECT  = "hrms/project_pprt"
FOLDER_TASK     = "hrms/task_files"
FOLDER_PROFILE  = "hrms/profile_pictures"
FOLDER_QC_REWORK = "hrms/qc_rework_files"


def _extract_public_id(url_or_public_id: str) -> str:
    """
    If given a full Cloudinary URL like
      https://res.cloudinary.com/<cloud>/raw/upload/v123/hrms/tracker_files/foo.xlsx
    extract the public_id (everything after /upload/v<digits>/ or /upload/).
    If already a plain public_id, return as-is.
    """
    if not url_or_public_id:
        return url_or_public_id

    marker = "/upload/"
    idx = url_or_public_id.find(marker)
    if idx == -1:
        return url_or_public_id  # already a public_id

    after_upload = url_or_public_id[idx + len(marker):]

    # strip optional version segment: v<digits>/
    if after_upload.startswith("v") and "/" in after_upload:
        ver_end = after_upload.index("/")
        possible_ver = after_upload[1:ver_end]
        if possible_ver.isdigit():
            after_upload = after_upload[ver_end + 1:]

    # strip extension for non-image resource types (raw)
    # Cloudinary public_ids for raw resources include the extension
    return after_upload


def upload_to_cloudinary(source, folder: str, display_name: str = None, resource_type: str = "auto"):
    """
    Upload a file to Cloudinary.

    Args:
        source: werkzeug FileStorage object OR a local file path (str).
        folder:  Cloudinary folder, e.g. FOLDER_TRACKER.
        display_name: desired filename stem (without extension).
                      If None, a UUID is used.
        resource_type: "auto" | "image" | "raw" | "video"

    Returns:
        (secure_url: str, public_id: str)  or raises on failure.
    """
    # Build a stable public_id so the URL is predictable
    stem = display_name or str(uuid.uuid4())
    # Note: Using the explicit `folder` argument ensures Cloudinary places
    # the file in the correct visual folder in their Media Library GUI.
    
    # Accept both FileStorage and file paths
    if hasattr(source, "read"):
        # werkzeug FileStorage
        data = source.stream
    else:
        data = source  # local path string

    result = cloudinary.uploader.upload(
        data,
        folder=folder,           # Explicit folder assignment
        public_id=stem,          # Just the filename stem
        resource_type=resource_type,
        use_filename=False,
        unique_filename=False,
        overwrite=True,
    )
    print(f"✅ Cloudinary upload OK: {result['public_id']}")
    return result["secure_url"], result["public_id"]


def delete_from_cloudinary(url_or_public_id: str, resource_type: str = "raw") -> bool:
    """
    Delete a file from Cloudinary.

    Args:
        url_or_public_id: full Cloudinary URL or bare public_id.
        resource_type: usually "raw" for Excel/PDF/CSV files; "image" for images.

    Returns:
        True if deleted, False otherwise.
    """
    if not url_or_public_id:
        return False

    public_id = _extract_public_id(url_or_public_id)

    try:
        result = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        success = result.get("result") == "ok"
        if success:
            print(f"✅ Cloudinary delete OK: {public_id}")
        else:
            print(f"⚠️  Cloudinary delete result: {result} for {public_id}")
        return success
    except Exception as e:
        print(f"❌ Cloudinary delete failed: {e} | public_id={public_id}")
        return False


def check_cloudinary_connection() -> bool:
    """
    Test Cloudinary connection.
    """
    try:
        cloudinary.api.ping()
        print("✅ Cloudinary connection successful")
        return True
    except Exception as e:
        print(f"❌ Cloudinary connection failed: {e}")
        return False
