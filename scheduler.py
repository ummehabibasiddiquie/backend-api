import requests
import os
from datetime import datetime

def run():
    base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:5000")
    url = f"{base_url}/qc/assign-daily-hours"

    print(f"[{datetime.now()}] Triggering daily hour assignment...")

    try:
        response = requests.post(url, timeout=30)
        print("Status:", response.status_code)
        print("Response:", response.text)
    except Exception as e:
        print("Error:", str(e))


if __name__ == "__main__":
    run()