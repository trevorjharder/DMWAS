from datetime import date
from itertools import count

import pytest

from app import MembershipDetails, User, _is_19_or_older, create_app, db


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


@pytest.fixture()
def make_user():
    """Build isolated synthetic member records for authorization tests."""
    sequence = count(1)

    def factory(
        *,
        status="active",
        is_admin=False,
        birth_date=date(1990, 1, 1),
        membership_type="adult",
    ):
        number = next(sequence)
        user = User(
            full_name=f"Synthetic Member {number}",
            email=f"synthetic-{number}@example.test",
            username=f"synthetic-{number}",
            status=status,
            is_admin=is_admin,
        )
        user.set_password("Synthetic-Password-1")
        user.membership_details = MembershipDetails(
            first_name="Synthetic",
            last_name=f"Member {number}",
            community="Port McNeill",
            date_of_birth=birth_date,
            age_19_or_older=_is_19_or_older(birth_date),
            mailing_address="1 Example Street",
            membership_type=membership_type,
            activities=[],
        )
        return user

    return factory


def set_csrf(client):
    with client.session_transaction() as session:
        session["csrf_token"] = "test-csrf-token"


def login_as(client, user_id):
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["csrf_token"] = "test-csrf-token"
