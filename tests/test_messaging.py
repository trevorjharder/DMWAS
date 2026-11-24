from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from threading import Barrier

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect

from app import CommunityProfile, MembershipTerm, User, _messaging_policy, create_app, db
from messaging_models import (
    MessageEvidence, MessagePair, MessageReport, MessageReportEvent, PrivateMessage,
)
from messaging_service import prepare_digest
from tests.conftest import login_as, set_csrf


CSRF = "test-csrf-token"
CONNECTIONS = "/member/connections"
MESSAGES = "/member/messages"
MODERATION = "/admin/community/message-reports"


@pytest.fixture()
def people(app, make_user):
    users = [make_user(), make_user(), make_user(), make_user(is_admin=True),
             make_user(is_admin=True), make_user(birth_date=date.today(), membership_type="youth")]
    db.session.add_all(users)
    db.session.flush()
    for user, name in zip(users, ("Trail Alice", "Trail Bob", "Trail Carol", "Ordinary Admin",
                                  "Review Admin", "Youth Alias")):
        db.session.add(CommunityProfile(
            user_id=user.id, display_name=name, status="approved", directory_opt_in=True))
    db.session.commit()
    ids = tuple(user.id for user in users)
    app.config["MESSAGING_POLICY"] = replace(app.config["MESSAGING_POLICY"],
                                             moderator_ids=frozenset({ids[4]}))
    return ids


def post(client, url, **fields):
    return client.post(url, data={"csrf_token": CSRF, **fields})


def text(response):
    return response.get_data(as_text=True)


def request_connection(client, sender, recipient):
    login_as(client, sender)
    return post(client, f"{CONNECTIONS}/request/{recipient}")


def find(sender, recipient):
    low, high = sorted((sender, recipient))
    return MessagePair.query.filter_by(low_id=low, high_id=high).one()


def connect(client, sender, recipient):
    assert request_connection(client, sender, recipient).status_code == 303
    pair_id = find(sender, recipient).id
    login_as(client, recipient)
    assert post(client, f"{CONNECTIONS}/{pair_id}/accept").status_code == 303
    return pair_id


def send(client, sender, pair_id, body):
    login_as(client, sender)
    return post(client, f"{MESSAGES}/{pair_id}/send", body=body)


def run_cli(app, command, *args):
    response = app.test_cli_runner().invoke(args=[command, *args])
    assert response.exit_code == 0, response.exception
    db.session.expire_all()
    return response.output


def test_directory_discovery_and_request_requires_a_visible_adult_target(app, client, people):
    alice, bob, carol, _, _, youth = people
    db.session.get(User, carol).community_profile.directory_opt_in = False
    db.session.commit()
    assert client.get(MESSAGES).status_code == 302
    assert request_connection(client, youth, bob).status_code == 403
    assert request_connection(client, alice, youth).status_code == 404
    assert request_connection(client, alice, carol).status_code == 404
    profile = text(client.get(f"/members/{bob}/profile"))
    assert "Send connection request" in profile
    assert "Trail Bob" in profile and "Synthetic Member" not in profile
    assert "Trail Carol" not in text(client.get("/members/search"))
    assert client.post(f"{CONNECTIONS}/request/{bob}").status_code == 400
    assert request_connection(client, alice, bob).status_code == 303
    assert "View connection request" in text(client.get(f"/members/{bob}/profile"))
    assert find(alice, bob).status == "pending"
    app.config["COMMUNITY_MESSAGING_ENABLED"] = False
    assert "Send connection request" not in text(client.get(f"/members/{bob}/profile"))
    assert client.get(MESSAGES).status_code == 404


def test_accept_decline_disconnect_and_no_unwanted_repeat_requests(client, people):
    alice, bob, carol, _, _, _ = people
    assert request_connection(client, alice, bob).status_code == 303
    pair_id = find(alice, bob).id
    assert post(client, f"{CONNECTIONS}/{pair_id}/accept").status_code == 409
    assert send(client, alice, pair_id, "before consent").status_code == 404
    assert request_connection(client, bob, alice).status_code == 303
    assert find(alice, bob).status == "pending"
    assert "Incoming connection request" in text(client.get(CONNECTIONS))
    assert post(client, f"{CONNECTIONS}/{pair_id}/decline").status_code == 303
    assert request_connection(client, alice, bob).status_code == 303
    assert find(alice, bob).status == "declined"
    assert "Send connection request" not in text(client.get(f"/members/{bob}/profile"))

    second = connect(client, alice, carol)
    login_as(client, alice)
    assert post(client, f"{CONNECTIONS}/{second}/disconnect").status_code == 303
    assert send(client, carol, second, "after disconnect").status_code == 403
    assert request_connection(client, carol, alice).status_code == 303
    assert find(alice, carol).status == "disconnected"


def test_private_text_unread_marking_and_no_receipts(client, people):
    assert db.engine.hide_parameters
    alice, bob, carol, ordinary, designated, _ = people
    pair_id = connect(client, alice, bob)
    assert send(client, alice, pair_id, "<script>Private marker</script>").status_code == 303
    first = PrivateMessage.query.one().id
    assert "unread message" not in text(client.get(MESSAGES))
    login_as(client, bob)
    assert "1 unread message" in text(client.get(MESSAGES))
    thread = client.get(f"{MESSAGES}/{pair_id}")
    assert thread.headers["Cache-Control"] == "private, no-store"
    assert "&lt;script&gt;Private marker&lt;/script&gt;" in text(thread)
    assert "Mark as read" in text(thread)
    assert send(client, alice, pair_id, "Second private message").status_code == 303
    login_as(client, bob)
    assert post(client, f"{MESSAGES}/{pair_id}/read", through=first).status_code == 303
    assert "1 unread message" in text(client.get(MESSAGES))
    second = PrivateMessage.query.order_by(PrivateMessage.id.desc()).first().id
    assert post(client, f"{MESSAGES}/{pair_id}/read", through=second).status_code == 303
    assert "unread message" not in text(client.get(MESSAGES))
    login_as(client, alice)
    assert "Mark as read" not in text(client.get(f"{MESSAGES}/{pair_id}"))
    for outsider in (carol, ordinary, designated):
        login_as(client, outsider)
        assert client.get(f"{MESSAGES}/{pair_id}").status_code == 404
        assert post(client, f"{MESSAGES}/{pair_id}/read", through=second).status_code == 404


def test_block_hides_both_directions_and_unblock_keeps_old_connection_closed(client, people):
    alice, bob, _, _, _, _ = people
    pair_id = connect(client, alice, bob)
    assert send(client, alice, pair_id, "Historical marker").status_code == 303
    login_as(client, bob)
    assert post(client, f"{CONNECTIONS}/{pair_id}/block").status_code == 303
    for viewer, target, label in ((alice, bob, "Trail Bob"), (bob, alice, "Trail Alice")):
        login_as(client, viewer)
        assert client.get(f"/members/{target}/profile").status_code == 404
        assert client.get(f"/api/community/profiles/{target}").status_code == 404
        assert label not in text(client.get("/members/search"))
        assert label not in text(client.get("/member/activities/new"))
        assert send(client, viewer, pair_id, "Contact after block").status_code == 403
        assert "Historical marker" in text(client.get(f"{MESSAGES}/{pair_id}"))
    login_as(client, bob)
    assert "Trail Alice" in text(client.get(f"{CONNECTIONS}?status=blocked"))
    assert post(client, f"{CONNECTIONS}/{pair_id}/unblock").status_code == 303
    assert client.get(f"/members/{alice}/profile").status_code == 200
    assert send(client, bob, pair_id, "Contact after unblock").status_code == 403
    assert find(alice, bob).status == "disconnected"


def test_expiry_preserves_read_history_and_prevents_both_message_directions(client, people):
    alice, bob, _, _, _, youth = people
    pair_id = connect(client, alice, bob)
    assert send(client, alice, pair_id, "Existing history").status_code == 303
    today = date.today()
    db.session.add(MembershipTerm(
        user_id=bob, term_number=1, membership_type="adult", status="active",
        approval_date=today - timedelta(days=366), start_date=today - timedelta(days=366),
        expiry_date=today - timedelta(days=1), annual_dues_cents=1000, payment_status="paid",
    ))
    db.session.commit()
    assert send(client, alice, pair_id, "Blocked by recipient expiry").status_code == 403
    with client.session_transaction() as session:
        session.clear()
    set_csrf(client)
    result = post(client, f"/login?next={MESSAGES}/{pair_id}", username=db.session.get(User, bob).username,
                  password="Synthetic-Password-1")
    assert result.location == f"{MESSAGES}/{pair_id}"
    page = text(client.get(f"{MESSAGES}/{pair_id}"))
    assert "Existing history" in page and "read-only" in page
    assert "Send message" not in page and "Messages" in page
    set_csrf(client)
    assert post(client, f"{MESSAGES}/{pair_id}/send", body="Expired sender").status_code == 403
    assert client.get(CONNECTIONS).status_code == 403
    assert client.get("/api/community/profiles").status_code == 403
    login_as(client, youth)
    assert client.get(MESSAGES).status_code == 403
    assert client.get(f"{MESSAGES}/{pair_id}").status_code == 403


def test_cross_requests_are_serialized_without_implicit_acceptance(app, people):
    alice, bob, *_ = people
    db.session.remove()
    start = Barrier(2)

    def go(pair):
        with app.test_client() as client:
            start.wait(timeout=10)
            return request_connection(client, *pair).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(go, ((alice, bob), (bob, alice)))) == [303, 303]
    assert MessagePair.query.count() == 1
    assert find(alice, bob).status == "pending"


def test_outbound_quotas_are_shared_and_block_remains_available(app, client, people, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr("rate_limits.datetime", FixedClock)
    alice, bob, carol, *_ = people
    pair_id = connect(client, alice, bob)
    app.config["MESSAGING_MESSAGES_PER_MINUTE"] = 1
    assert send(client, alice, pair_id, "First").status_code == 303
    with app.test_client() as other:
        response = send(other, alice, pair_id, "Second")
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert post(other, f"{CONNECTIONS}/{pair_id}/block").status_code == 303
    app.config["MESSAGING_REQUESTS_PER_DAY"] = 1
    assert request_connection(client, alice, carol).status_code == 429


def test_validation_rejects_files_and_overlong_messages(client, people):
    alice, bob, *_ = people
    pair_id = connect(client, alice, bob)
    for value in (" ", "a" * 2001, "NUL\x00"):
        assert send(client, alice, pair_id, value).status_code == 400
    result = client.post(f"{MESSAGES}/{pair_id}/send", data={
        "csrf_token": CSRF, "body": "text", "attachment": (BytesIO(b"synthetic"), "document.txt"),
    })
    assert result.status_code == 400
    assert send(client, alice, pair_id, "a" * 25000).status_code == 413
    assert PrivateMessage.query.count() == 0


def test_reports_copy_only_selected_evidence_and_audit_moderator_access(app, client, people):
    alice, bob, carol, ordinary, designated, _ = people
    pair_id = connect(client, alice, bob)
    assert send(client, alice, pair_id, "Selected evidence marker").status_code == 303
    selected = PrivateMessage.query.one().id
    assert send(client, bob, pair_id, "Unreported private marker").status_code == 303
    other = connect(client, alice, carol)
    send(client, carol, other, "Unrelated conversation marker")
    forged = PrivateMessage.query.filter_by(pair_id=other).one().id
    login_as(client, bob)
    url = f"{MESSAGES}/{pair_id}/report"
    assert post(client, url, reason="spam", evidence=str(forged)).status_code == 400
    assert post(client, url, reason="spam", evidence=str(selected), note="Escalation context").status_code == 303
    assert post(client, url, reason="spam", evidence=str(selected)).status_code == 409
    report_id = MessageReport.query.one().id
    review_url = f"{MODERATION}/{report_id}"
    login_as(client, ordinary)
    assert client.get(MODERATION).status_code == 403
    assert post(client, review_url, action="viewed").status_code == 403
    app.config["MESSAGING_POLICY"] = replace(app.config["MESSAGING_POLICY"],
                                             moderator_ids=frozenset({alice, designated}))
    login_as(client, alice)
    assert client.get(MODERATION).status_code == 403
    db.session.get(User, alice).is_admin = True
    db.session.commit()
    assert client.get(review_url).status_code == 404
    assert f"Report {report_id}" not in text(client.get(MODERATION))
    login_as(client, designated)
    assert "Report " in text(client.get(MODERATION))
    assert "Selected evidence marker" not in text(client.get(review_url))
    assert client.post(review_url, data={"action": "viewed"}).status_code == 400
    result = text(post(client, review_url, action="viewed"))
    assert "Selected evidence marker" in result and "Escalation context" in result
    assert "Unreported private marker" not in result and "Unrelated conversation marker" not in result
    assert post(client, review_url, action="resolved", note="Escalation recorded.").status_code == 303
    assert post(client, review_url, action="dismissed", note="Already closed").status_code == 409
    assert [event.action for event in MessageReportEvent.query.order_by(MessageReportEvent.id)] == [
        "submitted", "viewed", "resolved",
    ]


def test_opt_in_digest_groups_counts_and_retries_same_delivery_without_private_content(
    app, client, people, monkeypatch,
):
    alice, bob, carol, *_ = people
    first = connect(client, alice, bob)
    second = connect(client, carol, bob)
    send(client, alice, first, "First body to omit")
    send(client, carol, second, "Second body to omit")
    deliveries = []
    now = {"value": datetime.now(timezone.utc)}
    monkeypatch.setattr("messaging_routes.utc_now", lambda: now["value"])

    def provider(**fields):
        deliveries.append(fields)
        if len(deliveries) == 1:
            raise TimeoutError("synthetic timeout")
        return True

    monkeypatch.setattr("app._send_resend_email", provider)
    command = "send-community-message-digests"
    assert "Delivered 0" in run_cli(app, command)
    login_as(client, bob)
    assert post(client, f"{MESSAGES}/preferences", digest="yes").status_code == 303
    assert "Delivered 0" in run_cli(app, command)
    assert "Delivered 1" in run_cli(app, command)
    assert deliveries[0] == deliveries[1]
    assert deliveries[0]["recipient"] == db.session.get(User, bob).email
    assert "2 unread" in deliveries[0]["text"]
    assert "dmwas-message-digest/" in deliveries[0]["idempotency_key"]
    assert "body to omit" not in deliveries[0]["text"] and "Trail Alice" not in deliveries[0]["text"]
    assert "Delivered 0" in run_cli(app, command)
    now["value"] += timedelta(hours=25)
    assert "Delivered 0" in run_cli(app, command)
    send(client, alice, first, "New unread")
    assert "Delivered 1" in run_cli(app, command)
    assert deliveries[2]["idempotency_key"] != deliveries[1]["idempotency_key"]
    login_as(client, bob)
    assert post(client, f"{MESSAGES}/preferences").status_code == 303
    send(client, alice, first, "New after opt-out")
    now["value"] += timedelta(hours=25)
    assert "Delivered 0" in run_cli(app, command)
    assert len(deliveries) == 3


def test_digest_snapshot_has_one_concurrent_claim(app, client, people):
    alice, bob, *_ = people
    pair_id = connect(client, alice, bob)
    send(client, alice, pair_id, "Unread marker")
    login_as(client, bob)
    assert post(client, f"{MESSAGES}/preferences", digest="yes").status_code == 303
    db.session.remove()
    barrier = Barrier(2)
    now = datetime.now(timezone.utc)

    def claim(_):
        with app.app_context():
            barrier.wait(timeout=10)
            result = prepare_digest(User, bob, app.config["MESSAGING_POLICY"], now)
            db.session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (0, 1)))
    assert sum(result is not None for result in results) == 1


def test_retention_keeps_open_evidence_and_message_ids_remain_monotonic(app, client, people):
    alice, bob, _, _, designated, _ = people
    pair_id = connect(client, alice, bob)
    send(client, alice, pair_id, "Restricted evidence marker")
    old = PrivateMessage.query.one()
    old_id = old.id
    old.created_at = datetime.now(timezone.utc) - timedelta(days=500)
    db.session.commit()
    login_as(client, bob)
    assert post(client, f"{MESSAGES}/{pair_id}/report", reason="safety", evidence=str(old_id)).status_code == 303
    assert "Would remove 1 messages and 0 closed reports" in run_cli(app, "prune-community-messaging")
    assert PrivateMessage.query.count() == 1
    run_cli(app, "prune-community-messaging", "--apply")
    assert PrivateMessage.query.count() == 0
    assert MessageEvidence.query.one().body == "Restricted evidence marker"
    send(client, alice, pair_id, "New message")
    assert PrivateMessage.query.one().id > old_id
    report = MessageReport.query.one()
    login_as(client, designated)
    assert post(client, f"{MODERATION}/{report.id}", action="resolved", note="Resolved").status_code == 303
    report.closed_at = datetime.now(timezone.utc) - timedelta(days=500)
    db.session.commit()
    run_cli(app, "prune-community-messaging", "--apply")
    for model in (MessageReport, MessageReportEvent, MessageEvidence):
        assert model.query.count() == 0
    assert PrivateMessage.query.count() == 1


def test_account_erasure_keeps_restricted_evidence_and_anonymizes_history(client, people):
    alice, bob, _, admin, _, _ = people
    pair_id = connect(client, alice, bob)
    send(client, bob, pair_id, "Authored by removed account")
    evidence = PrivateMessage.query.one().id
    send(client, alice, pair_id, "Retained own message")
    assert post(client, f"{MESSAGES}/{pair_id}/report", reason="other", evidence=str(evidence)).status_code == 303
    login_as(client, admin)
    assert post(client, f"/admin/members/{bob}/delete").status_code == 302
    assert db.session.get(User, bob) is None
    assert MessageReport.query.one().subject_id is None
    assert MessageEvidence.query.one().body == "Authored by removed account"
    login_as(client, alice)
    page = text(client.get(f"{MESSAGES}/{pair_id}"))
    assert "Former member" in page and "Retained own message" in page
    assert "Authored by removed account" not in page and "Trail Bob" not in page
    assert send(client, alice, pair_id, "No recipient").status_code == 403


def test_messaging_policy_requires_explicit_deployed_values(monkeypatch):
    values = {"MESSAGING_DIGEST_INTERVAL_HOURS": "48", "MESSAGING_MESSAGE_DAYS": "180",
              "MESSAGING_CLOSED_REPORT_DAYS": "365", "MESSAGING_MODERATOR_IDS": "10, 20"}
    for key in values:
        monkeypatch.delenv(key, raising=False)
    assert _messaging_policy(False).digest_interval_hours == 24
    with pytest.raises(RuntimeError, match="MESSAGING_DIGEST_INTERVAL_HOURS"):
        _messaging_policy(True)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert _messaging_policy(True).moderator_ids == frozenset({10, 20})


def test_messaging_migration_roundtrip_preserves_phase7_metadata(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path.as_posix()}/db.sqlite"})
    with app.app_context():
        upgrade(revision="a07d4e5f6a7b")
        assert inspect(db.engine).has_table("activity_registration")
        upgrade()
        assert inspect(db.engine).has_table("private_message")
        downgrade(revision="a07d4e5f6a7b")
        assert inspect(db.engine).has_table("activity_registration")
        assert not inspect(db.engine).has_table("private_message")
        upgrade()
        assert inspect(db.engine).has_table("message_report")
