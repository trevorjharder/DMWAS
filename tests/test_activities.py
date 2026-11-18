from datetime import date, datetime, time, timedelta, timezone

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from activity_models import Activity, ActivityAudit, ActivityCoorganizer, ActivityRevision
from app import MembershipTerm, User, create_app, db
from tests.conftest import login_as, set_csrf


CSRF = "test-csrf-token"
MEMBER = "/member/activities"
ADMIN = "/admin/community/activities"


@pytest.fixture()
def people(make_user):
    owner, reviewer, guest, youth = (
        make_user(), make_user(is_admin=True), make_user(), make_user(birth_date=date.today())
    )
    db.session.add_all([owner, reviewer, guest, youth])
    db.session.commit()
    return owner.id, reviewer.id, guest.id, youth.id


def proposal(**changes):
    start = datetime.combine(date.today() + timedelta(days=3), time(12), timezone.utc)
    return {
        "title": "Synthetic trail day", "summary": "Help maintain local trails.",
        "category": "Hiking", "location": "mount_waddington", "time_zone": "UTC",
        "starts": start.strftime("%Y-%m-%dT%H:%M"),
        "ends": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
        "capacity": "10", "registration_mode": "organizer_approval",
        "meeting_details": "PRIVATE meeting location", "accessibility": "Uneven terrain",
        "equipment": "Work gloves", "requirements": "Closed footwear",
        "safety": "PRIVATE emergency plan", **changes,
    }


def create_draft(client, **fields):
    response = client.post(f"{MEMBER}/new", data={"csrf_token": CSRF, **fields})
    assert response.status_code == 302
    return int(response.location.rsplit("/", 1)[-1])


def change(client, event_url, activity_id, *, version=None, **fields):
    if version is None:
        version = db.session.get(Activity, activity_id).lock_version
    return client.post(event_url, data={"csrf_token": CSRF, "lock_version": version, **fields})


def submit_and_approve(client, owner, reviewer, activity_id):
    login_as(client, owner)
    assert change(client, f"{MEMBER}/{activity_id}/state/submit", activity_id).status_code == 302
    login_as(client, reviewer)
    assert change(client, f"{ADMIN}/{activity_id}/state/approve", activity_id,
                  confirm_public="yes").status_code == 302


def test_review_transitions_versioned_audit_and_public_privacy(app, client, people):
    owner, reviewer, _, _ = people
    login_as(client, owner)
    assert client.get(MEMBER).status_code == 200
    assert client.get(f"{MEMBER}/new").status_code == 200
    activity_id = create_draft(client, **proposal())
    private = f"{MEMBER}/{activity_id}"
    admin = f"{ADMIN}/{activity_id}"
    public = f"/activities/{activity_id}"
    assert client.get(private).headers["Cache-Control"] == "private, no-store"
    assert client.get(public).status_code == 404
    assert change(client, f"{private}/state/submit", activity_id).status_code == 302
    assert client.get(f"{private}/edit").status_code == 409

    login_as(client, reviewer)
    assert "Synthetic trail day" in client.get(ADMIN).get_data(as_text=True)
    assert "PRIVATE meeting location" in client.get(admin).get_data(as_text=True)
    assert change(client, f"{admin}/state/approve", activity_id).status_code == 400
    assert change(client, f"{admin}/state/request_changes", activity_id, note="Clarify the route.").status_code == 302
    assert change(client, f"{admin}/state/approve", activity_id, confirm_public="yes").status_code == 409
    login_as(client, owner)
    assert "Clarify the route." in client.get(private).get_data(as_text=True)
    assert change(client, f"{private}/state/submit", activity_id).status_code == 409
    assert change(client, f"{private}/edit", activity_id, **proposal(summary="Help clear local trails.")).status_code == 302
    submit_and_approve(client, owner, reviewer, activity_id)
    assert client.get(public).status_code == 404
    assert "Synthetic trail day" not in client.get("/activities").get_data(as_text=True)
    app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"] = True
    anonymous = app.test_client()
    for url in (public, "/activities"):
        response = anonymous.get(url)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Synthetic trail day" in body and "Mount Waddington area" in body
        assert "Help clear local trails." in body
        for hidden in ("PRIVATE", "Uneven terrain", "Work gloves", "Closed footwear", "Clarify the route.",
                       "Synthetic Member", "DMWAS-000"):
            assert hidden not in body

    assert "Help maintain local trails." in client.get(f"{admin}?revision=1").get_data(as_text=True)
    assert change(client, f"{admin}/state/unpublish", activity_id, note="Venue changed.").status_code == 302
    assert anonymous.get(public).status_code == 404
    assert [(event.action, event.revision_number, event.actor_id) for event in
            ActivityAudit.query.filter_by(activity_id=activity_id).order_by(ActivityAudit.id)] == [
        ("create_draft", 1, owner), ("submit", 1, owner), ("request_changes", 1, reviewer),
        ("revise", 2, owner), ("submit", 2, owner), ("approve", 2, reviewer), ("unpublish", 2, reviewer),
    ]


def test_significant_change_unpublishes_and_stale_review_cannot_approve(app, client, people):
    owner, reviewer, _, _ = people
    app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"] = True
    login_as(client, owner)
    activity_id = create_draft(client, **proposal())
    private = f"{MEMBER}/{activity_id}"
    admin = f"{ADMIN}/{activity_id}"
    public = f"/activities/{activity_id}"
    submit_and_approve(client, owner, reviewer, activity_id)
    approved_version = db.session.get(Activity, activity_id).lock_version
    assert client.get(public).status_code == 200
    login_as(client, owner)
    assert change(client, f"{private}/edit", activity_id, **proposal()).status_code == 302
    assert db.session.get(Activity, activity_id).lock_version == approved_version
    assert change(client, f"{private}/edit", activity_id, **proposal(safety="Updated plan")).status_code == 302
    assert client.get(public).status_code == 404
    assert change(client, f"{private}/state/submit", activity_id).status_code == 302
    login_as(client, reviewer)
    assert change(client, f"{admin}/state/reject", activity_id, version=approved_version,
                  note="Stale review").status_code == 409
    assert change(client, f"{admin}/state/reject", activity_id, note="Updated plan incomplete").status_code == 302
    assert db.session.get(Activity, activity_id).status == "rejected"
    assert db.session.get(ActivityRevision, (activity_id, 1)).organizer_details["safety"] == "PRIVATE emergency plan"


def test_coorganizer_consent_scope_reapproval_and_revocation(client, people):
    owner, reviewer, guest, youth = people
    login_as(client, owner)
    activity_id = create_draft(client, **proposal())
    private = f"{MEMBER}/{activity_id}"
    admin = f"{ADMIN}/{activity_id}"
    invitations = f"{private}/coorganizers"
    assert change(client, invitations, activity_id, member_number=f"DMWAS-{youth:06d}").status_code == 400
    assert change(client, invitations, activity_id, member_number=f"DMWAS-{guest:06d}").status_code == 302
    assert change(client, invitations, activity_id, member_number=f"DMWAS-{guest:06d}").status_code == 409
    assert "Synthetic Member" not in client.get(private).get_data(as_text=True).split("Co-organizers", 1)[1]
    login_as(client, reviewer)
    assert change(client, f"{admin}/coorganizers/{guest}/approve", activity_id).status_code == 409
    login_as(client, guest)
    assert "Synthetic trail day" in client.get(MEMBER).get_data(as_text=True)
    assert "PRIVATE" not in client.get(private).get_data(as_text=True)
    assert client.get(f"{private}/edit").status_code == 404
    assert change(client, f"{private}/state/submit", activity_id).status_code == 404
    assert change(client, invitations, activity_id, member_number=f"DMWAS-{reviewer:06d}").status_code == 404
    assert change(client, f"{invitations}/{guest}/accept", activity_id).status_code == 302
    submit_and_approve(client, owner, reviewer, activity_id)
    login_as(client, guest)
    assert "PRIVATE" not in client.get(private).get_data(as_text=True)
    login_as(client, reviewer)
    assert "Synthetic trail day" in client.get(f"{ADMIN}?status=coorganizers").get_data(as_text=True)
    assert change(client, f"{admin}/coorganizers/{guest}/approve", activity_id).status_code == 302
    login_as(client, guest)
    assert "PRIVATE meeting location" in client.get(private).get_data(as_text=True)
    assert client.get(admin).status_code == 403
    login_as(client, owner)
    assert change(client, f"{invitations}/{guest}/revoke", activity_id).status_code == 302
    login_as(client, guest)
    assert client.get(private).status_code == 404
    assert db.session.get(ActivityCoorganizer, (activity_id, guest)).status == "revoked"


def test_permissions_csrf_classification_and_official_listing(app, client, people):
    owner, reviewer, guest, youth = people
    set_csrf(client)
    assert client.get(MEMBER).status_code == 302
    login_as(client, youth)
    assert client.get(MEMBER).status_code == 403
    assert client.post(f"{MEMBER}/new", data={"csrf_token": CSRF}).status_code == 403
    login_as(client, owner)
    assert client.post(f"{MEMBER}/new", data=proposal()).status_code == 400
    assert client.post(f"{MEMBER}/new", data={"csrf_token": CSRF, "classification": "official"}).status_code == 403
    login_as(client, reviewer)
    activity_id = create_draft(client, **proposal(classification="official"))
    submit_and_approve(client, reviewer, reviewer, activity_id)
    assert client.get(f"/activities/{activity_id}").status_code == 200
    login_as(client, guest)
    assert client.get(f"{MEMBER}/{activity_id}").status_code == 404
    assert change(client, f"{ADMIN}/{activity_id}/state/unpublish", activity_id, note="Forged").status_code == 403
    assert app.test_client().get("/notices").status_code == 200


@pytest.mark.parametrize("invalid, expected", [
    ({"capacity": "0"}, "capacity between"),
    ({"location": "private address"}, "public location"),
    ({"starts": "2025-11-02T01:30", "time_zone": "America/Edmonton"}, "occur exactly once"),
    ({"starts": "2025-03-09T02:30", "time_zone": "America/Edmonton"}, "occur exactly once"),
    ({"time_zone": "Not/a-zone"}, "a time zone"),
    ({"summary": "a" * 801}, "up to 800"),
])
def test_invalid_draft_is_not_saved(client, people, invalid, expected):
    login_as(client, people[0])
    response = client.post(f"{MEMBER}/new", data={"csrf_token": CSRF, **proposal(**invalid)})
    assert response.status_code == 400
    assert expected in response.get_data(as_text=True)
    assert Activity.query.count() == 0
    assert ActivityAudit.query.count() == 0


def test_incomplete_draft_can_be_saved_but_safety_required_at_submission(client, people):
    login_as(client, people[0])
    activity_id = create_draft(client)
    private = f"{MEMBER}/{activity_id}"
    response = change(client, f"{private}/state/submit", activity_id)
    assert response.status_code == 303
    assert "Enter an activity title" in client.get(response.location).get_data(as_text=True)
    assert change(client, f"{private}/edit", activity_id, **proposal(safety="")).status_code == 302
    response = change(client, f"{private}/state/submit", activity_id)
    assert "Enter safety information" in client.get(response.location).get_data(as_text=True)
    assert change(client, f"{private}/edit", activity_id,
                  **proposal(location="online", safety="", meeting_details="https://example.test/meet")).status_code == 302
    assert change(client, f"{private}/state/submit", activity_id).status_code == 302


def test_expiry_hides_member_listing_and_access(app, client, people):
    owner, reviewer, _, _ = people
    app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"] = True
    login_as(client, owner)
    activity_id = create_draft(client, **proposal())
    submit_and_approve(client, owner, reviewer, activity_id)
    assert client.get(f"/activities/{activity_id}").status_code == 200
    today = date.today()
    db.session.add(MembershipTerm(
        user_id=owner, term_number=1, membership_type="adult", status="active", approval_date=today,
        start_date=today - timedelta(days=365), expiry_date=today - timedelta(days=1),
        annual_dues_cents=1000, payment_status="paid",
    ))
    db.session.commit()
    assert app.test_client().get(f"/activities/{activity_id}").status_code == 404
    login_as(client, owner)
    assert client.get(f"{MEMBER}/{activity_id}").status_code == 302


def test_activity_quota_is_shared_by_account_across_sessions(app, client, people, monkeypatch):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr("rate_limits.datetime", FixedClock)
    app.config["ACTIVITY_ACTIONS_PER_MINUTE"] = 1
    login_as(client, people[0])
    create_draft(client)
    other_session = app.test_client()
    login_as(other_session, people[0])
    response = other_session.post(f"{MEMBER}/new", data={"csrf_token": CSRF})
    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    login_as(other_session, people[2])
    create_draft(other_session)


def test_member_deletion_unpublishes_and_detaches_owned_and_invited_activities(app, client, people):
    owner, reviewer, guest, _ = people
    app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"] = True
    login_as(client, guest)
    second_activity = create_draft(client, **proposal())
    assert change(client, f"{MEMBER}/{second_activity}/coorganizers", second_activity,
                  member_number=f"DMWAS-{owner:06d}").status_code == 302
    login_as(client, owner)
    activity_id = create_draft(client, **proposal())
    submit_and_approve(client, owner, reviewer, activity_id)
    assert client.post(f"/admin/members/{owner}/delete", data={"csrf_token": CSRF}).status_code == 302
    assert client.get(f"/activities/{activity_id}").status_code == 404
    assert db.session.get(Activity, activity_id).status == "unpublished"
    assert db.session.get(ActivityCoorganizer, (second_activity, owner)) is None
    assert ActivityAudit.query.filter_by(actor_id=owner).count() == 0
    assert ActivityAudit.query.filter_by(subject_id=owner).count() == 0


def test_activity_migration_round_trip_preserves_prior_data(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path.as_posix()}/db.sqlite"})
    phase_five = "e05b2c3d4e5f"
    with app.app_context():
        upgrade(revision=phase_five)
        user = User(full_name="Existing User", email="prior@example.test", username="prior",
                    password_hash="synthetic", status="active")
        db.session.add(user)
        db.session.commit()
        user_id = user.id
        db.session.remove()
        upgrade()
        assert inspect(db.engine).has_table("activity_audit")
        downgrade(revision=phase_five)
        assert not inspect(db.engine).has_table("activity")
        assert db.session.get(User, user_id).username == "prior"
        db.session.remove()
        upgrade()
        assert inspect(db.engine).has_table("activity_revision")


def test_concurrent_revision_update_cannot_commit_stale_review(client, people):
    login_as(client, people[0])
    activity_id = create_draft(client, **proposal())
    with Session(db.engine) as stale, Session(db.engine) as recent:
        old = stale.get(Activity, activity_id)
        current = recent.get(Activity, activity_id)
        current.status = "submitted"
        recent.commit()
        old.status = "unpublished"
        with pytest.raises(StaleDataError):
            stale.commit()
    assert db.session.get(Activity, activity_id).status == "submitted"


def test_member_can_withdraw_pending_submission_for_editing(client, people):
    owner, reviewer, _, _ = people
    login_as(client, owner)
    activity_id = create_draft(client, **proposal())
    private = f"{MEMBER}/{activity_id}"
    assert change(client, f"{private}/state/submit", activity_id).status_code == 302
    assert change(client, f"{private}/state/withdraw", activity_id).status_code == 302
    assert client.get(f"{private}/edit").status_code == 200
    login_as(client, reviewer)
    assert change(client, f"{ADMIN}/{activity_id}/state/approve", activity_id,
                  confirm_public="yes").status_code == 409


def test_administrator_cannot_self_approve_a_member_proposal(client, people):
    reviewer = people[1]
    login_as(client, reviewer)
    activity_id = create_draft(client, **proposal(classification="member"))
    assert change(client, f"{MEMBER}/{activity_id}/state/submit", activity_id).status_code == 302
    assert change(client, f"{ADMIN}/{activity_id}/state/approve", activity_id,
                  confirm_public="yes").status_code == 403
