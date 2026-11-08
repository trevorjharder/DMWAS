from datetime import date

from sqlalchemy import inspect
from werkzeug.security import check_password_hash

from app import (
    MembershipConsent,
    MembershipDetails,
    MembershipPreferences,
    MembershipTerm,
    User,
    _database_uri,
    create_app,
    db,
)
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


def test_privacy_page_is_public_and_identifies_draft(client):
    response = client.get("/privacy")
    assert response.status_code == 200
    assert b"Draft for Society review before public launch" in response.data


def test_membership_application_form_shows_requested_fields(client):
    response = client.get("/membership/request")
    assert response.status_code == 200
    for field in (
        b"First name",
        b"Last name",
        b"Email address",
        b"Phone number",
        b"Community of residence",
        b"Date of birth",
        b"Are you 19 or older?",
        b"Mailing address",
        b"Adult Membership (19+)",
        b"Youth Membership (under 19)",
        b"Trail development and maintenance",
        b"Outdoor education",
        b"Would you be interested in volunteering?",
        b"Indoor climbing wall operations",
        b"Would you like to be contacted about volunteer opportunities?",
        b"I would like to receive newsletters",
        b"I agree to support the purposes",
        b"Parent or guardian consent for applicants under 19",
    ):
        assert field in response.data


def test_post_without_csrf_is_rejected(client):
    response = client.post("/membership/request", data={})
    assert response.status_code == 400


def test_access_request_stores_hash_and_is_not_login_enabled(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "first_name": "Test",
            "last_name": "Member",
            "email": "member@example.test",
            "phone": "250-555-0100",
            "community": "Port McNeill",
            "date_of_birth": "1990-01-01",
            "age_19_or_older": "yes",
            "mailing_address": "123 Main Street, Port McNeill, BC",
            "membership_type": "adult",
            "activities": ["Rock climbing", "Hiking"],
            "volunteer_interests": ["Outdoor trail maintenance"],
            "volunteer_contact_ok": "yes",
            "essential_communications": "yes",
            "newsletter_opt_in": "yes",
            "agree_membership_support": "yes",
            "acknowledge_approval": "yes",
            "acknowledge_privacy": "yes",
            "username": "testmember",
            "password": "Correct-Horse-Battery-1",
            "confirm_password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        member = User.query.filter_by(username="testmember").one()
        assert member.status == "pending"
        assert member.full_name == "Test Member"
        assert member.password_hash != "Correct-Horse-Battery-1"
        assert check_password_hash(member.password_hash, "Correct-Horse-Battery-1")
        details = MembershipDetails.query.filter_by(user_id=member.id).one()
        assert details.community == "Port McNeill"
        assert details.membership_type == "adult"
        assert details.activities == ["Rock climbing", "Hiking"]
        preferences = MembershipPreferences.query.filter_by(user_id=member.id).one()
        assert preferences.volunteer_interest is True
        assert preferences.volunteer_interests == ["Outdoor trail maintenance"]
        assert preferences.volunteer_contact_ok is True
        assert preferences.newsletter_opt_in is True
        consent_records = MembershipConsent.query.filter_by(user_id=member.id).all()
        assert {record.consent_type for record in consent_records} == {
            "membership_support",
            "approval_understanding",
            "privacy_acknowledgement",
            "essential_communications",
            "newsletter",
            "volunteer_contact",
        }
        assert all(record.version and record.accepted_at for record in consent_records)
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
        member.membership_details = MembershipDetails(
            first_name="Test",
            last_name="Member",
            community="Port McNeill",
            date_of_birth=date(1990, 1, 1),
            age_19_or_older=True,
            mailing_address="123 Main Street",
            membership_type="adult",
            activities=[],
        )
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
        term = MembershipTerm.query.filter_by(user_id=member_id).one()
        assert term.membership_type == "adult"
        assert term.annual_dues_cents == 1000
        assert term.payment_status == "unpaid"
        assert term.payment_date is None
        assert term.voting_eligible is None
        assert term.expiry_date.year == term.start_date.year + 1

    response = client.post(
        f"/admin/members/{member_id}/membership/payment",
        data={"csrf_token": "test-csrf-token", "payment_reference": "cash-receipt-42"},
    )
    assert response.status_code == 302
    with app.app_context():
        term = MembershipTerm.query.filter_by(user_id=member_id).one()
        assert term.payment_status == "paid"
        assert term.payment_date is not None
        assert term.payment_reference == "cash-receipt-42"

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


def test_membership_application_rejects_age_and_membership_mismatch(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "first_name": "Youth",
            "last_name": "Applicant",
            "email": "youth@example.test",
            "phone": "250-555-0100",
            "community": "Port Hardy",
            "date_of_birth": "2012-01-01",
            "age_19_or_older": "yes",
            "mailing_address": "123 Main Street",
            "membership_type": "adult",
            "volunteer_contact_ok": "no",
            "essential_communications": "yes",
            "agree_membership_support": "yes",
            "acknowledge_approval": "yes",
            "acknowledge_privacy": "yes",
            "username": "youthapplicant",
            "password": "Correct-Horse-Battery-1",
            "confirm_password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 400
    assert b"does not match your date of birth" in response.data
    with app.app_context():
        assert User.query.count() == 0


def test_youth_application_requires_guardian_consent(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "first_name": "Youth",
            "last_name": "Applicant",
            "email": "youth@example.test",
            "phone": "250-555-0100",
            "community": "Port Hardy",
            "date_of_birth": "2012-01-01",
            "age_19_or_older": "no",
            "mailing_address": "123 Main Street",
            "membership_type": "youth",
            "volunteer_contact_ok": "no",
            "essential_communications": "yes",
            "agree_membership_support": "yes",
            "acknowledge_approval": "yes",
            "acknowledge_privacy": "yes",
            "username": "youthapplicant",
            "password": "Correct-Horse-Battery-1",
            "confirm_password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 400
    assert b"parent or guardian must provide their name" in response.data
    with app.app_context():
        assert User.query.count() == 0


def test_youth_application_records_guardian_consent_and_waived_dues(client, app):
    set_csrf(client)
    response = client.post(
        "/membership/request",
        data={
            "csrf_token": "test-csrf-token",
            "first_name": "Youth",
            "last_name": "Applicant",
            "email": "youth@example.test",
            "phone": "250-555-0100",
            "community": "Port Hardy",
            "date_of_birth": "2012-01-01",
            "age_19_or_older": "no",
            "mailing_address": "123 Main Street",
            "membership_type": "youth",
            "volunteer_interests": ["not_at_this_time"],
            "volunteer_contact_ok": "no",
            "essential_communications": "yes",
            "agree_membership_support": "yes",
            "acknowledge_approval": "yes",
            "acknowledge_privacy": "yes",
            "guardian_name": "Parent Applicant",
            "guardian_email": "parent@example.test",
            "guardian_consent": "yes",
            "username": "youthapplicant",
            "password": "Correct-Horse-Battery-1",
            "confirm_password": "Correct-Horse-Battery-1",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        member = User.query.filter_by(username="youthapplicant").one()
        preferences = MembershipPreferences.query.filter_by(user_id=member.id).one()
        assert preferences.volunteer_interest is False
        assert preferences.volunteer_interests == []
        guardian_consent = MembershipConsent.query.filter_by(
            user_id=member.id, consent_type="guardian_consent"
        ).one()
        assert guardian_consent.granted is True
        assert guardian_consent.consented_by == "Parent Applicant"
        assert guardian_consent.contact_email == "parent@example.test"

        member.status = "pending"
        db.session.commit()
        user_id = member.id

    with app.app_context():
        admin = User(
            full_name="Society administrator",
            email="admin@example.test",
            username="siteadmin",
            status="active",
            is_admin=True,
        )
        admin.set_password("Admin-Password-Here-1")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
    login_as(client, admin_id)
    response = client.post(
        f"/admin/requests/{user_id}/approve",
        data={"csrf_token": "test-csrf-token", "checks_confirmed": "yes"},
    )
    assert response.status_code == 302
    with app.app_context():
        term = MembershipTerm.query.filter_by(user_id=user_id).one()
        assert term.membership_type == "youth"
        assert term.annual_dues_cents == 0
        assert term.payment_status == "waived"


def test_expired_member_is_blocked_and_admin_can_renew(client, app):
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
            full_name="Expired Member",
            email="expired@example.test",
            username="expired-member",
            status="active",
        )
        member.set_password("Expired-Member-Password-1")
        member.membership_terms.append(
            MembershipTerm(
                term_number=1,
                membership_type="adult",
                status="active",
                approval_date=date(2020, 1, 1),
                start_date=date(2020, 1, 1),
                expiry_date=date(2021, 1, 1),
                annual_dues_cents=1000,
                payment_status="paid",
            )
        )
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id = admin.id
        member_id = member.id

    set_csrf(client)
    expired_login = client.post(
        "/login",
        data={
            "csrf_token": "test-csrf-token",
            "username": "expired-member",
            "password": "Expired-Member-Password-1",
        },
    )
    assert expired_login.status_code == 200
    with app.app_context():
        assert db.session.get(User, member_id).status == "expired"

    login_as(client, member_id)
    response = client.get("/member")
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, member_id).status == "expired"
        assert MembershipTerm.query.filter_by(user_id=member_id, term_number=1).one().status == "expired"

    login_as(client, admin_id)
    response = client.post(
        f"/admin/members/{member_id}/membership/renew",
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 302
    with app.app_context():
        renewed_member = db.session.get(User, member_id)
        assert renewed_member.status == "active"
        terms = MembershipTerm.query.filter_by(user_id=member_id).order_by(MembershipTerm.term_number).all()
        assert len(terms) == 2
        assert terms[0].start_date == date(2020, 1, 1)
        assert terms[1].term_number == 2
        assert terms[1].payment_status == "unpaid"


def test_init_db_adds_new_tables_to_existing_schema(tmp_path):
    legacy_app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'legacy.sqlite').as_posix()}",
        }
    )
    with legacy_app.app_context():
        User.__table__.create(db.engine)
        MembershipDetails.__table__.create(db.engine)

    result = legacy_app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0, result.output
    with legacy_app.app_context():
        table_names = set(inspect(db.engine).get_table_names())
        assert {"membership_preferences", "membership_consent", "membership_term"} <= table_names


def test_first_request_creates_missing_membership_tables(client, app):
    with app.app_context():
        MembershipTerm.__table__.drop(db.engine)

    response = client.get("/")
    assert response.status_code == 302
    with app.app_context():
        assert inspect(db.engine).has_table("membership_term")
