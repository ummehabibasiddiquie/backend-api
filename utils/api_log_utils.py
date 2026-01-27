from config import get_db_connection
from datetime import datetime

def log_api_call(api_name, user_id, device_id, device_type, api_call_time=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if api_call_time is None:
            api_call_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO api_call_logs (api_name, user_id, device_id, device_type, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (api_name, user_id, device_id, device_type, api_call_time)
        )
        conn.commit()
    except Exception as e:
        print(f"API log error: {e}")
    finally:
        cursor.close()
        conn.close()
