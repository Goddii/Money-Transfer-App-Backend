import re

def validate_registration(data):
    required_fields = [
        "first_name",
        "last_name",
        "email",
        "password",
    ]

    for field in required_fields:
        if not data.get(field):
            raise ValueError(
                f"{field.replace('_', ' ').title()} is required."
            )

    first_name = data["first_name"].strip()
    last_name = data["last_name"].strip()
    email = data["email"].strip().lower()
    password = data["password"]

    if len(first_name) < 2:
        raise ValueError("First name must be at least 2 characters.")

    if len(last_name) < 2:
        raise ValueError("Last name must be at least 2 characters.")

    email_pattern = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

    if not re.match(email_pattern, email):
        raise ValueError("Please provide a valid email address.")

    if len(password) < 8:
        raise ValueError(
            "Password must be at least 8 characters."
        )

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password": password,
        "phone_number": (
            data.get("phone_number", "").strip()
            or None
        ),
    }
def validate_login(data):
    if not data.get("email"):
        raise ValueError("Email is required.")

    if not data.get("password"):
        raise ValueError("Password is required.")

    return {
        "email": data["email"].strip().lower(),
        "password": data["password"],
    }
