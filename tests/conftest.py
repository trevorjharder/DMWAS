import pytest

from app import create_app, db


@pytest.fixture()
def app(tmp_path):
    database_path = tmp_path / "test.sqlite"
    test_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path.as_posix()}",
            "PUBLIC_CONTACT_EMAIL": "hello@example.test",
        }
    )
    with test_app.app_context():
        db.create_all()
        yield test_app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def set_csrf(client):
    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf-token"


def login_as(client, user_id):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["csrf_token"] = "test-csrf-token"
