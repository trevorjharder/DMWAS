from werkzeug.security import check_password_hash

from app import User, _database_uri, db
from tests.conftest import login_as, set_csrf


def test_postgres_urls_select_installed_psycopg2_driver(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@db.example.test:5432/society")
    assert _database_uri() == "postgresql+psycopg2://user:pass@db.example.test:5432/society"

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db.example.test:5432/society")
    assert _database_uri() == "postgresql+psycopg2://user:pass@db.example.test:5432/society"


def test_public_pages_render(client):
    assert client.get("/").status_code == 302
    for path in ("/about", "/notices", "/contact"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"District of Mount Waddington" in response.data


def test_contact_uses_configured_public_email(client):
    response = client.get("/contact")
    assert b"hello@example.test" in response.data


def test_post_without_csrf_is_rejected(client):
    response = client.post("/membership/request", data={})
    assert response.status_code == 400


def test_access_request_stores_hash_and_is_not_login_enabled(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "full_name": "Test Member",
            "email": "member@example.test",
            "phone": "250-555-0100",
            "username": "testmember",
            "password": "Correct-Horse-Battery-1",
            "confirm_password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        member = User.query.filter_by(username="testmember").one()
        assert member.status == "pending"
        assert member.password_hash != "Correct-Horse-Battery-1"
        assert check_password_hash(member.password_hash, "Correct-Horse-Battery-1")
        member_id = member.id

    set_csrf(client)
    response = client.post(
        "/login",
        data={
            "csrf_token": "test-csrf-token",
            "username": "testmember",
            "password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 200
    with client.session_transaction() as session:
        assert "user_id" not in session

    assert member_id is not None


def test_admin_can_approve_request_and_member_can_sign_in(client, app):
    with app.app_context():
        admin = User(
            full_name="Society administrator",
            email="admin@example.test",
            username="siteadmin",
            status="active",
            is_admin=True,
        )
        admin.set_password("Admin-Password-Here-1")
        member = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="pending",
        )
        member.set_password("Member-Password-Here-1")
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id = admin.id
        member_id = member.id

    login_as(client, admin_id)
    assert client.get("/admin/requests").status_code == 200
    response = client.post(
        f"/admin/requests/{member_id}/approve",
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, member_id).status == "pending"

    response = client.post(
        f"/admin/requests/{member_id}/approve",
        data={"csrf_token": "test-csrf-token", "checks_confirmed": "yes"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, member_id).status == "active"

    with client.session_transaction() as session:
        session.clear()
        session["csrf_token"] = "test-csrf-token"
    response = client.post(
        "/login",
        data={
            "csrf_token": "test-csrf-token",
            "username": "testmember",
            "password": "Member-Password-Here-1",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/member")
    assert client.get("/member").status_code == 200
    member_home = client.get("/member")
    assert b"Resources" in member_home.data
    assert b"Search Members" in member_home.data
    assert b"Manage Members" not in member_home.data
    assert client.get("/member/resources").status_code == 200
    assert client.get("/members/search?q=Test").status_code == 200


def test_non_admin_cannot_review_requests(client, app):
    with app.app_context():
        member = User(
            full_name="Ordinary Member",
            email="ordinary@example.test",
            username="ordinary",
            status="active",
            is_admin=False,
        )
        member.set_password("Ordinary-Member-Password-1")
        db.session.add(member)
        db.session.commit()
        member_id = member.id

    login_as(client, member_id)
    assert client.get("/admin/requests").status_code == 403
    assert client.get("/admin/members").status_code == 403
    assert b"Manage Members" not in client.get("/member").data


def test_admin_member_page_is_linked_and_lists_accounts(client, app):
    with app.app_context():
        admin = User(
            full_name="Society administrator",
            email="admin@example.test",
            username="siteadmin",
            status="active",
            is_admin=True,
        )
        admin.set_password("Admin-Password-Here-1")
        member = User(
            full_name="Active Member",
            email="active@example.test",
            username="active-member",
            status="active",
        )
        member.set_password("Active-Member-Password-1")
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id = admin.id

    login_as(client, admin_id)
    home = client.get("/member")
    assert b"Manage Members" in home.data
    assert b"Access Requests" in home.data

    response = client.get("/admin/members")
    assert response.status_code == 200
    assert b"Active Member" in response.data
    assert b"active@example.test" in response.data


def test_invalid_access_request_does_not_create_user(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "full_name": "Bad Request",
            "email": "not-an-email",
            "username": "ab",
            "password": "short",
            "confirm_password": "different",
        },
    )
    assert response.status_code == 400
    with app.app_context():
        assert User.query.count() == 0
