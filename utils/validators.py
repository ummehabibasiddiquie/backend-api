import re

# USERNAME Validation

def is_valid_username(username):
    """Only letters and spaces allowed"""
    if not username:
        return False
    pattern = r'^[A-Za-z ]+$'
    return bool(re.match(pattern, username))

# EMAIL Validation

def is_valid_email(email):
    """Basic email format check"""
    if not email:
        return False
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return bool(re.match(pattern, email))

# PASSWORD Validation

def is_valid_password(password):
    """At least 6 characters"""
    if not password or len(password) < 6:
        return False
    return True

# PHONE NUMBER Validation

def is_valid_phone(number):
    """Only digits, 10-15 characters"""
    if not number:
        return True  # optional field
    pattern = r'^\d{10,15}$'
    return bool(re.match(pattern, number))
