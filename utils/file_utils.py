import base64
import os
import uuid
import mimetypes
from config import UPLOAD_FOLDER, UPLOAD_SUBDIRS

def save_base64_file(base64_string, subfolder):
    """
    Save a base64 file to disk and return the filename.
    """
    if not base64_string:
        return None

    if "," not in base64_string:
        raise ValueError("Invalid base64 format")

    header, encoded = base64_string.split(",", 1)

    # Extract MIME type
    try:
        mime_type = header.split(";")[0].split(":")[1]
    except IndexError:
        raise ValueError("Invalid base64 header")

    # Guess file extension from MIME type
    extension = mimetypes.guess_extension(mime_type) or ".bin"

    # Decode Base64
    file_bytes = base64.b64decode(encoded)

    # Generate unique filename
    filename = f"{uuid.uuid4()}{extension}"

    # Folder path
    folder = os.path.join(
        UPLOAD_FOLDER,
        UPLOAD_SUBDIRS.get(subfolder, subfolder)
    )
    os.makedirs(folder, exist_ok=True)

    file_path = os.path.join(folder, filename)

    # Save file
    with open(file_path, "wb") as f:
        f.write(file_bytes)

    return filename