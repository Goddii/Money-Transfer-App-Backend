ALLOWED_ADMIN_STATUSES = {"Active", "Frozen"}


def validate_admin_user_create(data):
    """Validate the admin "create user" payload.

    Accepts either a single ``name`` (split into first/last) or explicit
    ``first_name``/``last_name``. Returns a normalised dict ready for user
    creation.
    """

    if not isinstance(data, dict):
        raise ValueError("Invalid request body.")

    email = (data.get("email") or "").strip()
    password = data.get("password")

    if not email:
        raise ValueError("Email is required.")
    if "@" not in email or "." not in email:
        raise ValueError("A valid email is required.")
    if not password or not str(password).strip():
        raise ValueError("Password is required.")

    name = (data.get("name") or "").strip()
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()

    if not first_name or not last_name:
        if name:
            parts = name.split(" ", 1)
            first_name = first_name or parts[0]
            last_name = last_name or (parts[1] if len(parts) > 1 else "")
        if not first_name:
            raise ValueError("First name is required.")
        if not last_name:
            last_name = ""

    phone_number = (data.get("phone") or data.get("phone_number") or "").strip() or None
    initial_balance_raw = data.get("initial_balance", 0)

    try:
        initial_balance = to_money(initial_balance_raw)
    except (TypeError, ValueError):
        raise ValueError("Initial balance must be a valid number.")

    if initial_balance < 0:
        raise ValueError("Initial balance cannot be negative.")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "password": str(password),
        "phone_number": phone_number,
        "initial_balance": initial_balance,
    }


def validate_admin_user_update(data):
    """Validate the admin "update user" payload.

    Permits first name, last name, phone and account status only. Any other
    field is rejected so sensitive attributes (role, email, password) cannot be
    changed through this endpoint.
    """

    if not isinstance(data, dict):
        raise ValueError("Invalid request body.")

    allowed_fields = {"first_name", "last_name", "phone_number", "status"}
    unexpected = set(data.keys()) - allowed_fields
    if unexpected:
        raise ValueError(
            "Invalid fields: " + ", ".join(sorted(unexpected))
        )

    validated = {}

    if "first_name" in data:
        value = (data["first_name"] or "").strip()
        if not value:
            raise ValueError("First name cannot be empty.")
        validated["first_name"] = value

    if "last_name" in data:
        value = (data["last_name"] or "").strip()
        if not value:
            raise ValueError("Last name cannot be empty.")
        validated["last_name"] = value

    if "phone_number" in data:
        validated["phone_number"] = (data["phone_number"] or "").strip() or None

    if "status" in data:
        value = (data["status"] or "").strip()
        if value not in ALLOWED_ADMIN_STATUSES:
            raise ValueError("Status must be one of: Active, Frozen.")
        validated["status"] = value

    if not validated:
        raise ValueError("No valid fields supplied for update.")

    return validated


from app.utils.helpers import to_money


def validate_user_update(data):
    allowed_fields = {
        "first_name",
        "last_name",
        "phone_number",
    }

    unexpected_fields = set(data.keys()) - allowed_fields

    if unexpected_fields:
        raise ValueError(
            "Invalid fields: "
            + ", ".join(sorted(unexpected_fields))
        )

    if "first_name" in data:
        if not data["first_name"].strip():
            raise ValueError("First name cannot be empty.")

    if "last_name" in data:
        if not data["last_name"].strip():
            raise ValueError("Last name cannot be empty.")

    return {
        key: value
        for key, value in data.items()
        if key in allowed_fields
    }