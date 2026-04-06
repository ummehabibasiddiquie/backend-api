import requests

BASE_URL = "http://192.168.125.158:5000/auth/user"


def test_login():
    print("\n---- LOGIN TEST ----")

    payload = {
        "user_email": "sunny@transform.com",
        "user_password": "123456"
    }

    response = requests.post(BASE_URL, json=payload)

    print("STATUS:", response.status_code)
    print("RESPONSE:")
    try:
        print(json.dumps(response.json(), indent=4))
    except:
        print(response.text)


def test_register():
    print("\n---- REGISTRATION TEST ----")

    payload = {
        "user_name": "Zainab",
        "user_email": "zainab@transform.com",
        "user_password": "zainab",
        "user_role": "admin",
        "created_date": "2025-01-01",
        "updated_date": "2025-01-01",
        "profile_picture": None,
        "user_number": "9876543210",
        "user_address": "Pune",
        "device_id": "LAPTOP-0001"
    }

    response = requests.post(BASE_URL, json=payload)

    print("STATUS:", response.status_code)
    print("RESPONSE:")
    try:
        print(json.dumps(response.json(), indent=4))
    except:
        print(response.text)


if __name__ == "__main__":
    choice = input(
        "\nEnter 1 for LOGIN test or 2 for REGISTER test: "
    ).strip()

    if choice == "1":
        test_login()
    elif choice == "2":
        test_register()
    else:
        print("Invalid selection.")