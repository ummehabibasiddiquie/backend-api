import base64
import os
import re
from io import BytesIO
from PIL import Image
from config import UPLOAD_FOLDER, UPLOAD_SUBDIRS

def save_base64_image_as_webp(base64_string, user_name):
    if not base64_string:
        return None

    if "," not in base64_string:
        raise ValueError("Invalid base64 image format")

    header, encoded = base64_string.split(",", 1)

    # Decode base64
    image_bytes = base64.b64decode(encoded)
    image = Image.open(BytesIO(image_bytes)).convert("RGB")

    # Safe filename
    safe_name = re.sub(r"[^a-zA-Z0-9]", "_", user_name.lower())
    filename = f"{safe_name}_profile_picture.webp"

    # Resolve profile pictures folder from config
    profile_pic_folder = os.path.join(
        UPLOAD_FOLDER,
        UPLOAD_SUBDIRS["PROFILE_PIC"]
    )

    os.makedirs(profile_pic_folder, exist_ok=True)

    # ✅ FULL FILE PATH (THIS WAS MISSING)
    file_path = os.path.join(profile_pic_folder, filename)

    # Save image
    image.save(file_path, "WEBP", quality=80)

    return filename
