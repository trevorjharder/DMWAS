import re
from io import BytesIO
from datetime import date, datetime, timedelta, timezone

import app as app_module
from sqlalchemy import inspect
from werkzeug.security import check_password_hash

from app import (
    MembershipConsent,
    MembershipDetails,
    MembershipPreferences,
    MembershipTerm,
    PasswordResetCode,
    User,
    _database_uri,
    _is_voting_eligible,
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
    for path in ("/about", "/notices", "/contact", "/donate"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"District of Mount Waddington" in response.data


def test_donate_page_shows_etransfer_email_and_equipment_contact_note(client):
    response = client.get("/donate")

    assert response.status_code == 200
    assert b"donations@accessni.ca" in response.data
    assert b"donate equipment" in response.data
    assert b'href="mailto:donations@accessni.ca"' in response.data


def test_contact_uses_configured_public_email(client):
    response = client.get("/contact")
    assert b"hello@example.test" in response.data


def test_contact_page_has_message_form_and_csrf(client):
    response = client.get("/contact")

    assert response.status_code == 200
    assert b"Send us a message" in response.data
    assert b"info@accessni.com" in response.data
    assert b'name="csrf_token"' in response.data
    assert b'name="message"' in response.data


def test_contact_form_sends_to_society_and_uses_visitor_as_reply_to(
    client, monkeypatch
):
    monkeypatch.setenv("RESEND_FROM", "DMWAS <noreply@accessni.ca>")
    monkeypatch.setenv("RESEND_CONTACT_FROM", "DMWAS <noreply@accessni.ca>")
    sent = []

    def fake_send(recipient, subject, text, reply_to=None, from_address=None):
        sent.append((recipient, subject, text, reply_to, from_address))
        return True

    monkeypatch.setattr(app_module, "_send_resend_email", fake_send)
    set_csrf(client)
    response = client.post(
        "/contact",
        data={
            "csrf_token": "test-csrf-token",
            "name": "Jordan Member",
            "email": "jordan@example.test",
            "subject": "Trail question",
            "message": "I have a question about the north trail.",
        },
    )

    assert response.status_code == 302
    assert response.location.endswith("/contact")
    assert sent == [
        (
            "info@accessni.com",
            "Contact form: Trail question",
            "Name: Jordan Member\nEmail: jordan@example.test\n\n"
            "I have a question about the north trail.",
            "jordan@example.test",
            "DMWAS <inquires@accessni.ca>",
        )
    ]


def test_contact_form_rejects_invalid_email_without_sending(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        app_module,
        "_send_resend_email",
        lambda *args, **kwargs: sent.append((args, kwargs)),
    )
    set_csrf(client)
    response = client.post(
        "/contact",
        data={
            "csrf_token": "test-csrf-token",
            "name": "Jordan Member",
            "email": "not-an-email",
            "subject": "Trail question",
            "message": "I have a question about the north trail.",
        },
    )

    assert response.status_code == 400
    assert b"Enter a valid email address" in response.data
    assert sent == []


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
        b"Adult membership dues are $10 per year",
        b"Interac",
        b"membership@accessni.ca",
        b"Adult membership will not be approved until payment has been received",
        b"Youth membership is free",
    ):
        assert field in response.data
    assert b"Are you 19 or older?" not in response.data
    assert re.search(rb'<input\s+id="mailing_address"', response.data)
    assert b"<textarea" not in response.data
    assert b'<h2>Member account</h2>' in response.data
    guardian_section = response.data.split(b'id="guardian-consent-section"', 1)[1].split(b"</fieldset>", 1)[0]
    assert b" hidden" in guardian_section
    assert b"Parent or guardian consent for applicants under 19" in guardian_section
    membership_section = response.data.split(b"Select your membership type", 1)[0].rsplit(b"<fieldset", 1)[1]
    assert b"guardian-consent-section" not in membership_section
    assert b'name="membership_type"' in response.data


def test_post_without_csrf_is_rejected(client):
    response = client.post("/membership/request", data={})
    assert response.status_code == 400


def test_password_reset_email_uses_resend_https_api(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_api_key")
    monkeypatch.setenv("RESEND_FROM", "DMWAS <noreply@example.test>")
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"id":"email-id"}'

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(app_module.urllib_request, "urlopen", fake_urlopen)
    member = User(
        full_name="Test Member",
        email="member@example.test",
        username="testmember",
    )

    app_module._send_password_reset_code(member, "123456")

    request, timeout = calls[0]
    payload = app_module.json.loads(request.data)
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_header("Authorization") == "Bearer re_test_api_key"
    assert request.get_header("User-agent") == "DMWAS-website/1.0"
    assert timeout == 10
    assert payload["from"] == "DMWAS <noreply@example.test>"
    assert payload["to"] == ["member@example.test"]
    assert payload["subject"] == "Your DMWAS password reset code"
    assert "123456" in payload["text"]


def test_resend_http_error_body_is_logged_without_changing_generic_response(
    client, app, monkeypatch, caplog
):
    with app.app_context():
        user = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="active",
        )
        user.set_password("Old-Password-Here-1")
        db.session.add(user)
        db.session.commit()

    def reject_email(_user, _code):
        raise app_module.HTTPError(
            "https://api.resend.com/emails",
            403,
            "Forbidden",
            {"x-resend-id": "req_test"},
            BytesIO(b'{"message":"sender domain is not verified"}'),
        )

    monkeypatch.setattr(app_module, "_send_password_reset_code", reject_email)
    set_csrf(client)
    response = client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "member@example.test"},
    )

    assert response.status_code == 302
    assert "HTTP 403" in caplog.text
    assert "req_test" in caplog.text
    assert "sender domain is not verified" in caplog.text
    assert b"sender domain is not verified" not in response.data


def test_forgot_password_sends_hashed_expiring_code(client, app, monkeypatch):
    sent = []

    def fake_send(user, code):
        sent.append((user.email, code))

    monkeypatch.setattr(app_module, "_send_password_reset_code", fake_send)
    with app.app_context():
        user = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="active",
        )
        user.set_password("Old-Password-Here-1")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    set_csrf(client)
    response = client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "member@example.test"},
    )
    assert response.status_code == 302
    assert response.location.endswith("/reset-password")
    assert sent[0][0] == "member@example.test"
    assert re.fullmatch(r"\d{6}", sent[0][1])

    with app.app_context():
        reset_code = PasswordResetCode.query.filter_by(user_id=user_id).one()
        assert reset_code.code_hash != sent[0][1]
        assert reset_code.expires_at > reset_code.created_at
        assert reset_code.used_at is None

    response = client.post(
        "/reset-password",
        data={
            "csrf_token": "test-csrf-token",
            "code": sent[0][1],
            "password": "New-Password-Here-2",
            "confirm_password": "New-Password-Here-2",
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/login")
    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.check_password("New-Password-Here-2")
        assert not user.check_password("Old-Password-Here-1")
        assert PasswordResetCode.query.filter_by(user_id=user_id).one().used_at is not None


def test_password_reset_code_is_single_use_and_limited_to_five_attempts(
    client, app, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        app_module,
        "_send_password_reset_code",
        lambda _user, code: sent.append(code),
    )
    with app.app_context():
        user = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="active",
        )
        user.set_password("Old-Password-Here-1")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    set_csrf(client)
    client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "member@example.test"},
    )
    for _ in range(5):
        response = client.post(
            "/reset-password",
            data={
                "csrf_token": "test-csrf-token",
                "code": "000000" if sent[0] != "000000" else "000001",
                "password": "New-Password-Here-2",
                "confirm_password": "New-Password-Here-2",
            },
        )
        assert response.status_code == 400

    response = client.post(
        "/reset-password",
        data={
            "csrf_token": "test-csrf-token",
            "code": sent[0],
            "password": "New-Password-Here-2",
            "confirm_password": "New-Password-Here-2",
        },
    )
    assert response.status_code == 400
    with app.app_context():
        reset_code = PasswordResetCode.query.filter_by(user_id=user_id).one()
        assert reset_code.attempts == 5
        assert reset_code.used_at is not None
        assert db.session.get(User, user_id).check_password("Old-Password-Here-1")


def test_expired_password_reset_code_is_rejected(client, app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        app_module,
        "_send_password_reset_code",
        lambda _user, code: sent.append(code),
    )
    with app.app_context():
        user = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="active",
        )
        user.set_password("Old-Password-Here-1")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    set_csrf(client)
    client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "member@example.test"},
    )
    with app.app_context():
        reset_code = PasswordResetCode.query.filter_by(user_id=user_id).one()
        reset_code.expires_at = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        )
        db.session.commit()

    response = client.post(
        "/reset-password",
        data={
            "csrf_token": "test-csrf-token",
            "code": sent[0],
            "password": "New-Password-Here-2",
            "confirm_password": "New-Password-Here-2",
        },
    )
    assert response.status_code == 400
    with app.app_context():
        reset_code = PasswordResetCode.query.filter_by(user_id=user_id).one()
        assert reset_code.used_at is not None
        assert db.session.get(User, user_id).check_password("Old-Password-Here-1")


def test_forgot_password_response_does_not_disclose_unknown_email(
    client, app, monkeypatch
):
    sent = []
    monkeypatch.setattr(
        app_module,
        "_send_password_reset_code",
        lambda _user, code: sent.append(code),
    )
    with app.app_context():
        user = User(
            full_name="Test Member",
            email="member@example.test",
            username="testmember",
            status="active",
        )
        user.set_password("Old-Password-Here-1")
        db.session.add(user)
        db.session.commit()

    set_csrf(client)
    client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "member@example.test"},
    )
    known_email_response = client.get("/reset-password")

    set_csrf(client)
    client.post(
        "/forgot-password",
        data={"csrf_token": "test-csrf-token", "email": "nobody@example.test"},
    )
    unknown_email_response = client.get("/reset-password")

    assert b"If an account exists for that email" in known_email_response.data
    assert b"If an account exists for that email" in unknown_email_response.data
    assert len(sent) == 1


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
    requests_page = client.get("/admin/requests")
    assert requests_page.status_code == 200
    assert b"confirming adult dues were received when applicable" in requests_page.data
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
        assert term.voting_eligible is True
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

    member_admin_page = client.get("/admin/members")
    assert member_admin_page.status_code == 200
    assert b"Eligible to vote" in member_admin_page.data

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
    assert client.get(f"/admin/members/{member_id}").status_code == 403
    assert client.post(
        f"/admin/members/{member_id}/make-admin",
        data={"csrf_token": "test-csrf-token"},
    ).status_code == 403
    member_home = client.get("/member")
    assert b"Manage Members" not in member_home.data
    assert b"Access Requests" not in member_home.data


def test_voting_eligibility_requires_age_19_and_approved_account():
    today = date(2038, 6, 1)

    approved_adult = User(
        full_name="Approved Adult",
        email="adult@example.test",
        username="approved-adult",
        status="active",
    )
    approved_adult.membership_details = MembershipDetails(
        first_name="Approved",
        last_name="Adult",
        community="Port McNeill",
        date_of_birth=date(2019, 6, 1),
        age_19_or_older=True,
        mailing_address="123 Main Street",
        membership_type="adult",
        activities=[],
    )

    pending_adult = User(
        full_name="Pending Adult",
        email="pending@example.test",
        username="pending-adult",
        status="pending",
    )
    pending_adult.membership_details = MembershipDetails(
        first_name="Pending",
        last_name="Adult",
        community="Port McNeill",
        date_of_birth=date(2000, 1, 1),
        age_19_or_older=True,
        mailing_address="123 Main Street",
        membership_type="adult",
        activities=[],
    )

    approved_youth = User(
        full_name="Approved Youth",
        email="youth@example.test",
        username="approved-youth",
        status="active",
    )
    approved_youth.membership_details = MembershipDetails(
        first_name="Approved",
        last_name="Youth",
        community="Port McNeill",
        date_of_birth=date(2019, 6, 2),
        age_19_or_older=False,
        mailing_address="123 Main Street",
        membership_type="youth",
        activities=[],
    )

    legacy_member = User(
        full_name="Legacy Member",
        email="legacy@example.test",
        username="legacy-member",
        status="active",
    )

    assert _is_voting_eligible(approved_adult, today) is True
    assert _is_voting_eligible(pending_adult, today) is False
    assert _is_voting_eligible(approved_youth, today) is False
    assert _is_voting_eligible(legacy_member, today) is None


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
        member.membership_details = MembershipDetails(
            first_name="Active",
            last_name="Member",
            community="Port McNeill",
            date_of_birth=date(1990, 1, 1),
            age_19_or_older=True,
            mailing_address="22 Coastal Road",
            membership_type="adult",
            activities=["Hiking", "Bouldering"],
        )
        member.membership_preferences = MembershipPreferences(
            volunteer_interest=True,
            volunteer_interests=["Outdoor trail maintenance"],
            volunteer_contact_ok=True,
            newsletter_opt_in=False,
        )
        member.consent_records.append(
            MembershipConsent(
                consent_type="membership_support",
                version="membership-support-v1",
                granted=True,
            )
        )
        member.membership_terms.append(
            MembershipTerm(
                term_number=1,
                membership_type="adult",
                status="active",
                approval_date=date(2026, 1, 1),
                start_date=date(2026, 1, 1),
                expiry_date=date(2027, 1, 1),
                annual_dues_cents=1000,
                payment_status="paid",
                payment_date=date(2026, 1, 3),
                payment_reference="receipt-987",
                voting_eligible=True,
            )
        )
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id = admin.id
        member_id = member.id

    login_as(client, admin_id)
    home = client.get("/member")
    assert b"Manage Members" in home.data
    assert b"Access Requests" in home.data

    response = client.get("/admin/members")
    assert response.status_code == 200
    assert b"Active Member" in response.data
    assert b"active@example.test" in response.data
    assert f'href="/admin/members/{member_id}"'.encode() in response.data
    assert b"Make administrator" in response.data

    detail = client.get(f"/admin/members/{member_id}")
    assert detail.status_code == 200
    for data in (
        b"2026-01-03",
        b"receipt-987",
        b"22 Coastal Road",
        b"Port McNeill",
        b"Bouldering",
        b"Outdoor trail maintenance",
        b"membership-support-v1",
    ):
        assert data in detail.data

    response = client.post(
        f"/admin/members/{member_id}/make-admin",
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, member_id).is_admin is True
    members_page = client.get("/admin/members")
    assert f'href="/admin/members/{member_id}"'.encode() in members_page.data
    assert b"Administrators" in members_page.data
    assert b"active@example.test" in members_page.data
    promoted_member_detail = client.get(f"/admin/members/{member_id}")
    assert promoted_member_detail.status_code == 200
    assert b"Active Member" in promoted_member_detail.data


def test_admin_members_table_includes_admins_except_society_administrator(client, app):
    with app.app_context():
        table_admin = User(
            full_name="Additional Administrator",
            email="additional-admin@example.test",
            username="additional-admin",
            status="active",
            is_admin=True,
        )
        table_admin.set_password("Additional-Admin-Password-1")
        society_admin = User(
            full_name="Society Administrator",
            email="tharder@accessni.ca",
            username="society-admin",
            status="active",
            is_admin=True,
        )
        society_admin.set_password("Society-Admin-Password-1")
        db.session.add_all([table_admin, society_admin])
        db.session.commit()
        table_admin_id = table_admin.id

    login_as(client, table_admin_id)
    response = client.get("/admin/members")

    assert response.status_code == 200
    member_table = response.data.split(b"<tbody>", 1)[1].split(b"</tbody>", 1)[0]
    administrators_section = response.data.split(b"<h2>Administrators</h2>", 1)[1]
    assert b"additional-admin@example.test" in member_table
    assert b"Administrator</span>" in member_table
    assert f'href="/admin/members/{table_admin_id}"'.encode() not in member_table
    assert b"tharder@accessni.ca" not in member_table
    assert b"tharder@accessni.ca" in administrators_section


def test_admin_can_permanently_delete_member_and_associated_records(client, app):
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
            full_name="Delete Me",
            email="delete@example.test",
            username="delete-me",
            status="active",
        )
        member.set_password("Delete-Member-Password-1")
        member.membership_details = MembershipDetails(
            first_name="Delete",
            last_name="Me",
            community="Port McNeill",
            date_of_birth=date(1990, 1, 1),
            age_19_or_older=True,
            mailing_address="123 Main Street",
            membership_type="adult",
            activities=["Hiking"],
        )
        member.membership_preferences = MembershipPreferences(
            volunteer_interest=True,
            volunteer_interests=["Outdoor trail maintenance"],
            volunteer_contact_ok=True,
            newsletter_opt_in=True,
        )
        member.consent_records.append(
            MembershipConsent(
                consent_type="newsletter",
                version="newsletter-v1",
                granted=True,
            )
        )
        member.membership_terms.append(
            MembershipTerm(
                term_number=1,
                membership_type="adult",
                status="active",
                approval_date=date.today(),
                start_date=date.today(),
                expiry_date=date.today().replace(year=date.today().year + 1),
                annual_dues_cents=1000,
                payment_status="paid",
                payment_date=date.today(),
                payment_reference="receipt-123",
            )
        )
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id = admin.id
        member_id = member.id

    login_as(client, admin_id)
    page = client.get("/admin/members")
    assert page.status_code == 200
    assert b"Permanently delete member" in page.data
    assert b"This cannot be undone" in page.data

    response = client.post(
        f"/admin/members/{member_id}/delete",
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, member_id) is None
        assert MembershipDetails.query.filter_by(user_id=member_id).count() == 0
        assert MembershipPreferences.query.filter_by(user_id=member_id).count() == 0
        assert MembershipConsent.query.filter_by(user_id=member_id).count() == 0
        assert MembershipTerm.query.filter_by(user_id=member_id).count() == 0
        assert db.session.get(User, admin_id) is not None

    response = client.post(
        f"/admin/members/{admin_id}/delete",
        data={"csrf_token": "test-csrf-token"},
    )
    assert response.status_code == 404


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
    assert b"membership type must match your age" in response.data
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
        assert term.voting_eligible is False


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
