"""Tests for beneficiary management and ownership enforcement."""

from app.extensions import db
from app.models import Beneficiary

BENEFICIARIES_URL = "/api/beneficiaries"


def test_list_requires_authentication(client):
    assert client.get(BENEFICIARIES_URL).status_code == 401


def test_create_requires_authentication(client):
    response = client.post(BENEFICIARIES_URL, json={"beneficiary_user_id": 1})

    assert response.status_code == 401


def test_delete_requires_authentication(client):
    assert client.delete(f"{BENEFICIARIES_URL}/1").status_code == 401


def test_create_valid_beneficiary(client, create_user, authenticated_user):
    target = create_user(email="target@example.com", first_name="Ann")
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"beneficiary_user_id": target["id"]}
    )

    assert response.status_code == 201

    beneficiary = response.get_json()["data"]["beneficiary"]

    assert beneficiary["beneficiary_user_id"] == target["id"]
    assert beneficiary["name"] == target["name"]
    # Internal fields of the beneficiary account are never exposed.
    assert "password_hash" not in beneficiary
    assert "role" not in beneficiary
    assert "status" not in beneficiary


def test_user_only_sees_own_beneficiaries(
    client, app, create_user, authenticated_user, login
):
    target = create_user(email="shared-target@example.com")
    owner, owner_headers = authenticated_user(email="owner@example.com")
    other, other_headers = authenticated_user(email="stranger@example.com")

    client.post(
        BENEFICIARIES_URL,
        headers=owner_headers,
        json={"beneficiary_user_id": target["id"]},
    )
    client.post(
        BENEFICIARIES_URL,
        headers=other_headers,
        json={"beneficiary_user_id": owner["id"]},
    )

    response = client.get(BENEFICIARIES_URL, headers=owner_headers)

    assert response.status_code == 200

    beneficiaries = response.get_json()["data"]["beneficiaries"]

    assert len(beneficiaries) == 1
    assert beneficiaries[0]["beneficiary_user_id"] == target["id"]

    with app.app_context():
        assert Beneficiary.query.count() == 2


def test_duplicate_beneficiary_rejected(client, create_user, authenticated_user):
    target = create_user(email="dup-target@example.com")
    _, headers = authenticated_user(email="dup-owner@example.com")

    payload = {"beneficiary_user_id": target["id"]}

    assert client.post(BENEFICIARIES_URL, headers=headers, json=payload).status_code == 201

    response = client.post(BENEFICIARIES_URL, headers=headers, json=payload)

    assert response.status_code == 409
    assert response.get_json()["error"] == "DUPLICATE_BENEFICIARY"


def test_nonexistent_beneficiary_rejected(client, authenticated_user):
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"beneficiary_user_id": 999999}
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "USER_NOT_FOUND"


def test_self_beneficiary_rejected(client, authenticated_user):
    user, headers = authenticated_user(email="self@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"beneficiary_user_id": user["id"]}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "SELF_BENEFICIARY_NOT_ALLOWED"


def test_inactive_user_cannot_be_added_as_beneficiary(
    client, create_user, authenticated_user
):
    frozen = create_user(email="frozen@example.com", status="Frozen")
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"beneficiary_user_id": frozen["id"]}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "INVALID_BENEFICIARY"


def test_invalid_payload_rejected(client, authenticated_user):
    _, headers = authenticated_user(email="owner@example.com")

    for payload in ({}, {"beneficiary_user_id": "abc"}, {"beneficiary_user_id": 0},
                    {"beneficiary_user_id": None}, {"beneficiary_user_id": -3}):
        response = client.post(BENEFICIARIES_URL, headers=headers, json=payload)

        assert response.status_code == 400, payload
        assert response.get_json()["success"] is False


def test_delete_own_beneficiary(client, app, create_user, authenticated_user):
    target = create_user(email="target@example.com")
    _, headers = authenticated_user(email="owner@example.com")

    created = client.post(
        BENEFICIARIES_URL, headers=headers, json={"beneficiary_user_id": target["id"]}
    )
    beneficiary_id = created.get_json()["data"]["beneficiary"]["id"]

    response = client.delete(f"{BENEFICIARIES_URL}/{beneficiary_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    with app.app_context():
        assert db.session.get(Beneficiary, beneficiary_id) is None


def test_cannot_delete_another_users_beneficiary(
    client, app, create_user, authenticated_user
):
    target = create_user(email="target@example.com")
    _, owner_headers = authenticated_user(email="owner@example.com")
    _, attacker_headers = authenticated_user(email="attacker@example.com")

    created = client.post(
        BENEFICIARIES_URL,
        headers=owner_headers,
        json={"beneficiary_user_id": target["id"]},
    )
    beneficiary_id = created.get_json()["data"]["beneficiary"]["id"]

    response = client.delete(
        f"{BENEFICIARIES_URL}/{beneficiary_id}", headers=attacker_headers
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "BENEFICIARY_NOT_FOUND"

    with app.app_context():
        # The owner's record is untouched.
        assert db.session.get(Beneficiary, beneficiary_id) is not None


def test_delete_missing_beneficiary_returns_not_found(client, authenticated_user):
    _, headers = authenticated_user(email="owner@example.com")

    response = client.delete(f"{BENEFICIARIES_URL}/999999", headers=headers)

    assert response.status_code == 404


# --- User-facing identifier resolution (phone / email) ---


def test_create_valid_beneficiary_by_phone(client, create_user, authenticated_user):
    target = create_user(
        email="phone-target@example.com",
        first_name="Ben",
        phone_number="0712345678",
    )
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "0712345678"}
    )

    assert response.status_code == 201
    beneficiary = response.get_json()["data"]["beneficiary"]
    assert beneficiary["beneficiary_user_id"] == target["id"]
    assert beneficiary["phone_number"] == "0712345678"


def test_create_valid_beneficiary_by_email(client, create_user, authenticated_user):
    target = create_user(email="email-target@example.com", first_name="Cara")
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"email": "email-target@example.com"}
    )

    assert response.status_code == 201
    beneficiary = response.get_json()["data"]["beneficiary"]
    assert beneficiary["beneficiary_user_id"] == target["id"]
    assert beneficiary["email"] == "email-target@example.com"


def test_create_beneficiary_by_email_is_case_insensitive(
    client, create_user, authenticated_user
):
    target = create_user(email="MixedCase@example.com", first_name="Dani")
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"email": "mixedcase@example.com"}
    )

    assert response.status_code == 201
    assert response.get_json()["data"]["beneficiary"]["beneficiary_user_id"] == target["id"]


def test_create_beneficiary_normalises_phone_format(
    client, create_user, authenticated_user
):
    # Stored as the local 07... form; lookup uses the 254... canonical form.
    target = create_user(
        email="fmt-target@example.com",
        first_name="Eli",
        phone_number="0712345678",
    )
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "254712345678"}
    )

    assert response.status_code == 201
    assert response.get_json()["data"]["beneficiary"]["beneficiary_user_id"] == target["id"]


def test_create_beneficiary_nonexistent_phone_rejected(
    client, authenticated_user
):
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "254799999999"}
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "USER_NOT_FOUND"


def test_create_beneficiary_nonexistent_email_rejected(
    client, authenticated_user
):
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"email": "nobody@example.com"}
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "USER_NOT_FOUND"


def test_duplicate_beneficiary_by_phone_rejected(
    client, create_user, authenticated_user
):
    create_user(
        email="dup-phone@example.com", phone_number="0712111222"
    )
    _, headers = authenticated_user(email="owner@example.com")

    assert (
        client.post(
            BENEFICIARIES_URL, headers=headers, json={"phone_number": "0712111222"}
        ).status_code
        == 201
    )
    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "0712111222"}
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "DUPLICATE_BENEFICIARY"


def test_self_beneficiary_by_phone_rejected(client, authenticated_user):
    _, headers = authenticated_user(
        email="self-phone@example.com", phone_number="0712333444"
    )

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "0712333444"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "SELF_BENEFICIARY_NOT_ALLOWED"


def test_inactive_beneficiary_by_phone_rejected(
    client, create_user, authenticated_user
):
    create_user(
        email="frozen-phone@example.com", phone_number="0712555666", status="Frozen"
    )
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "0712555666"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "INVALID_BENEFICIARY"


def test_invalid_phone_format_rejected(client, authenticated_user):
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL, headers=headers, json={"phone_number": "not-a-phone"}
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_multiple_identifiers_rejected(client, create_user, authenticated_user):
    create_user(email="multi@example.com", phone_number="0712777888")
    _, headers = authenticated_user(email="owner@example.com")

    response = client.post(
        BENEFICIARIES_URL,
        headers=headers,
        json={"phone_number": "0712777888", "email": "multi@example.com"},
    )

    assert response.status_code == 400
    assert response.get_json()["success"] is False
