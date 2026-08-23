"""Regression tests protecting admin/user separation.

These only assert that admin authorization is unchanged by the new user
endpoints; admin functionality itself is out of scope for this work.
"""

OVERVIEW_URL = "/api/v1/admin/overview"
ADMIN_USERS_URL = "/api/v1/admin/users"


def test_admin_overview_requires_authentication(client):
    assert client.get(OVERVIEW_URL).status_code == 401


def test_normal_user_cannot_access_admin_overview(client, authenticated_user):
    _, headers = authenticated_user(email="normal@example.com")

    response = client.get(OVERVIEW_URL, headers=headers)

    assert response.status_code == 403


def test_normal_user_cannot_list_admin_users(client, authenticated_user):
    _, headers = authenticated_user(email="normal@example.com")

    assert client.get(ADMIN_USERS_URL, headers=headers).status_code == 403


def test_admin_can_access_admin_overview(client, authenticated_user):
    _, headers = authenticated_user(email="admin@example.com", role="admin")

    response = client.get(OVERVIEW_URL, headers=headers)

    assert response.status_code == 200
    assert "total_users" in response.get_json()


def test_admin_endpoints_reject_normal_user_wallet_data_access(
    client, create_user, authenticated_user
):
    """A normal user must not reach admin user-management routes."""
    target = create_user(email="target@example.com", balance="100.00")
    _, headers = authenticated_user(email="normal@example.com")

    response = client.get(
        f"{ADMIN_USERS_URL}/{target['id']}/profile", headers=headers
    )

    assert response.status_code == 403
