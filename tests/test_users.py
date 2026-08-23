"""Tests for the authenticated user profile endpoints."""

PROFILE_URL = "/api/users/me"


def test_get_profile_requires_authentication(client):
    response = client.get(PROFILE_URL)

    assert response.status_code == 401


def test_get_own_profile(client, authenticated_user):
    user, headers = authenticated_user(email="profile@example.com")

    response = client.get(PROFILE_URL, headers=headers)

    assert response.status_code == 200

    profile = response.get_json()["data"]["user"]

    assert profile["id"] == user["id"]
    assert profile["email"] == "profile@example.com"
    assert "password_hash" not in profile


def test_update_own_profile(client, authenticated_user):
    _, headers = authenticated_user(email="update@example.com")

    response = client.put(
        PROFILE_URL,
        headers=headers,
        json={"first_name": "Updated", "last_name": "Name"},
    )

    assert response.status_code == 200

    profile = response.get_json()["data"]["user"]

    assert profile["first_name"] == "Updated"
    assert profile["last_name"] == "Name"


def test_update_profile_rejects_unknown_fields(client, authenticated_user):
    _, headers = authenticated_user(email="fields@example.com")

    response = client.put(PROFILE_URL, headers=headers, json={"role": "admin"})

    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_update_profile_rejects_duplicate_phone_number(
    client, create_user, authenticated_user
):
    create_user(email="taken@example.com", phone_number="0712345678")
    _, headers = authenticated_user(email="mine@example.com")

    response = client.put(
        PROFILE_URL, headers=headers, json={"phone_number": "0712345678"}
    )

    assert response.status_code == 400
