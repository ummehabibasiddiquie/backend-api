import bcrypt
import hashlib
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import os

# Load encryption key from environment variables
def get_encryption_key():
    """Get encryption key from environment or generate a new one"""
    key = os.getenv("ENCRYPTION_KEY")
    
    if not key:
        # Generate a new Fernet key
        new_key = Fernet.generate_key().decode()
        print(f"Generated new encryption key: {new_key}")
        print("Please add this to your .env file: ENCRYPTION_KEY=" + new_key)
        return new_key.encode()
    
    # Validate the key format
    try:
        key_bytes = key.encode()
        # Try to use the key to validate it
        test_cipher = Fernet(key_bytes)
        return key_bytes
    except Exception as e:
        print(f"Invalid ENCRYPTION_KEY format: {e}")
        print("Generating new encryption key...")
        new_key = Fernet.generate_key().decode()
        print(f"New encryption key: {new_key}")
        print("Please update your .env file: ENCRYPTION_KEY=" + new_key)
        return new_key.encode()

# Initialize cipher with environment key
ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def hash_password(password: str) -> bytes:
    """Hash password using bcrypt (for authentication)"""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())

def verify_password(password: str, hashed: bytes) -> bool:
    return bcrypt.checkpw(password.encode(), hashed)
    """Verify password against bcrypt hash"""
    return bcrypt.checkpw(password.encode(), hashed)

def encrypt_password(password: str) -> str:
    """
    Encrypt password using Fernet symmetric encryption
    Returns base64 encoded encrypted string
    """
    if not password:
        return ""
    
    # Convert to bytes if needed
    if isinstance(password, str):
        password = password.encode()
    
    # Encrypt the password
    encrypted_password = cipher_suite.encrypt(password)
    
    # Return as base64 string for storage
    return base64.urlsafe_b64encode(encrypted_password).decode()

def decrypt_password(encrypted_password: str) -> str:
    """
    Decrypt password from base64 encoded encrypted string
    Returns original plain text password
    """
    if not encrypted_password:
        return ""
    
    try:
        # Decode from base64
        encrypted_bytes = base64.urlsafe_b64decode(encrypted_password.encode())
        
        # Decrypt the password
        decrypted_password = cipher_suite.decrypt(encrypted_bytes)
        
        # Return as string
        return decrypted_password.decode()
    except Exception as e:
        # Re-raise the exception so the calling function can handle it
        raise Exception(f"Password decryption failed: {e}")

def safe_decrypt_password(encrypted_password: str) -> str:
    """
    Safely decrypt password with fallback for plain text
    Returns decrypted password or original if decryption fails
    """
    if not encrypted_password:
        return ""
    
    try:
        return decrypt_password(encrypted_password)
    except Exception:
        # If decryption fails, return as-is (plain text)
        return encrypted_password

def sha256_hash(text: str) -> str:
    """
    Generate SHA-256 hash of text (for one-way hashing if needed)
    """
    return hashlib.sha256(text.encode()).hexdigest()