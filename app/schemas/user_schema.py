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