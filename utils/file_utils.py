import base64
import os
import uuid
import mimetypes
from config import UPLOAD_FOLDER, UPLOAD_SUBDIRS

def save_base64_project_file(base64_string):
    if not base64_string:
        return None

    if "," not in base64_string:
        raise ValueError("Invalid base64 format")

    header, encoded = base64_string.split(",", 1)

    # Example header:
    # data:application/pdf;base64
    # data:application/vnd.ms-excel;base64
    # data:image/png;base64
    try:
        mime_type = header.split(";")[0].split(":")[1]
    except IndexError:
        raise ValueError("Invalid base64 header")

    # Guess file extension from MIME
    extension = mimetypes.guess_extension(mime_type) or ""

    file_bytes = base64.b64decode(encoded)

    filename = f"{uuid.uuid4()}{extension}"

    folder = os.path.join(
        UPLOAD_FOLDER,
        UPLOAD_SUBDIRS["PROJECT_PPRT"]
    )
    os.makedirs(folder, exist_ok=True)

    file_path = os.path.join(folder, filename)

    with open(file_path, "wb") as f:
        f.write(file_bytes)

    return filename
