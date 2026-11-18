from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from threading import Barrier

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect

from activity_models import Activity, ActivityAudit, ActivityCoorganizer, ActivityRevision
from app import MembershipTerm, User, _registration_policy, create_app, db
from registration_models import ActivityNotice, ActivityRegistration, ActivityVolunteerHours
from tests.conftest import login_as, set_csrf


CSRF = "test-csrf-token"
ROSTERS = "/member/activities"
HOURS = "/admin/community/volunteer-hours"
REGISTRATIONS = "/member/registrations"


@pytest.fixture()
def clock(monkeypatch):
    times = {"now": datetime.combine(date.today(), time(12), timezone.utc)}
    monkeypatch.setattr("registration_service.utc_now", lambda: times["now"])
    return times


@pytest.fixture()
def people(make_user):
    users = [make_user(), make_user(is_admin=True), *(make_user() for _ in range(5))]
    users.append(make_user(birth_date=date.today(), membership_type="youth"))
    db.session.add_all(users)
    db.session.commit()
    return tuple(user.id for user in users)


@pytest.fixture()
def event(app, people, clock):
    app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"] = True

    def create(*, capacity=1, mode="immediate", starts=None, classification="member"):
        starts = starts or clock["now"] + timedelta(days=3)
        ends = starts + timedelta(hours=2)
        organizer = people[1] if classification == "official" else people[0]
        activity = Activity(organizer_id=organizer, classification=classification, status="approved")
        db.session.add(activity)
        db.session.flush()
        db.session.add(ActivityRevision(
            activity_id=activity.id, number=1, starts_at=starts, ends_at=ends,
            public_details={"title": "Synthetic trail day", "summary": "Local trail cleanup",
                            "location": "Mount Waddington area", "time_zone": "UTC",
                            "category": "Hiking", "starts": starts.strftime("%Y-%m-%dT%H:%M"),
                            "ends": ends.strftime("%Y-%m-%dT%H:%M")},
            organizer_details={"capacity": str(capacity), "registration_mode": mode,
                               "meeting_details": "PRIVATE trail meeting point"},
        ))
        db.session.add(ActivityAudit(
            activity_id=activity.id, actor_id=people[1], revision_number=1, action="approve",
            previous_status="submitted", new_status="approved",
        ))
        db.session.commit()
        return activity.id

    return create


def join(client, activity_id, user_id, name=None):
    login_as(client, user_id)
    return post(client, f"/activities/{activity_id}/register", action="register",
                display_name=name or f"Roster alias {user_id}")


def post(client, url, **fields):
    return client.post(url, data={"csrf_token": CSRF, **fields})


def booking(activity_id, user_id):
    return ActivityRegistration.query.filter_by(activity_id=activity_id, user_id=user_id).one()


def roster_action(client, activity_id, registration_id, action, **fields):
    return post(client, f"{ROSTERS}/{activity_id}/roster/{registration_id}/{action}", **fields)


def reconcile(app):
    result = app.test_cli_runner().invoke(args=["reconcile-activity-registrations", "--batch-size", "1"])
    assert result.exit_code == 0, result.exception
    db.session.expire_all()


def test_privacy_registration_and_publication_controls(app, client, event, people, clock):
    owner, _, first, second, _, _, _, youth = people
    activity_id = event()
    public = f"/activities/{activity_id}"
    enroll = f"{public}/register"
    roster = f"{ROSTERS}/{activity_id}/roster"
    assert "Sign in to register" in client.get(public).get_data(as_text=True)
    assert client.get(enroll).status_code == 302
    set_csrf(client)
    assert post(client, enroll, action="register", display_name="Forged").status_code == 302
    assert join(client, activity_id, youth).status_code == 403
    login_as(client, first)
    assert client.post(enroll, data={"action": "register", "display_name": "No CSRF"}).status_code == 400
    assert join(client, activity_id, first, "Trail Member").status_code == 303
    assert booking(activity_id, first).status == "confirmed"
    assert "PRIVATE trail meeting point" in client.get(enroll).get_data(as_text=True)
    assert "Trail Member" not in client.get(public).get_data(as_text=True)
    assert "PRIVATE" not in client.get(public).get_data(as_text=True)
    assert "Your registration is confirmed" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)
    login_as(client, second)
    assert "PRIVATE" not in client.get(enroll).get_data(as_text=True)
    assert "Trail Member" not in client.get(REGISTRATIONS).get_data(as_text=True)
    assert client.get(roster).status_code == 404
    login_as(client, owner)
    response = client.get(roster)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "Trail Member" in response.get_data(as_text=True)
    assert "Synthetic Member" not in response.get_data(as_text=True)
    assert "example.test" not in response.get_data(as_text=True)
    app.config["ACTIVITY_REGISTRATION_ENABLED"] = False
    assert client.get(enroll).status_code == 404
    assert "Sign in to register" not in app.test_client().get(public).get_data(as_text=True)
    app.config["ACTIVITY_REGISTRATION_ENABLED"] = True
    app.config["ACTIVITY_REGISTRATION_POLICY"] = replace(
        app.config["ACTIVITY_REGISTRATION_POLICY"], allow_youth=True)
    assert join(client, activity_id, youth).status_code == 303
    assert booking(activity_id, youth).status == "waitlisted"


def test_concurrent_registrations_reserve_exactly_one_seat(app, event, people):
    activity_id = event()
    participants = people[2:5]
    db.session.remove()
    start = Barrier(len(participants))

    def register(user_id):
        with app.test_client() as client:
            login_as(client, user_id)
            start.wait(timeout=10)
            return post(client, f"/activities/{activity_id}/register",
                        action="register", display_name=f"Alias {user_id}").status_code

    with ThreadPoolExecutor(max_workers=len(participants)) as pool:
        assert list(pool.map(register, participants)) == [303] * len(participants)
    assert sorted(booking(activity_id, user_id).status for user_id in participants) == [
        "confirmed", "waitlisted", "waitlisted",
    ]
    assert register_single(app, activity_id, participants[0]) == 303
    assert ActivityRegistration.query.filter_by(activity_id=activity_id).count() == 3


def register_single(app, activity_id, user_id):
    with app.test_client() as client:
        return join(client, activity_id, user_id).status_code


def test_concurrent_cancellations_offer_each_freed_seat_once(app, client, event, people, clock):
    activity_id = event(capacity=2)
    users = people[2:7]
    for user_id in users:
        assert join(client, activity_id, user_id).status_code == 303
        clock["now"] += timedelta(minutes=1)
    db.session.remove()
    start = Barrier(2)

    def cancel(user_id):
        with app.test_client() as session:
            login_as(session, user_id)
            start.wait(timeout=10)
            return post(session, f"/activities/{activity_id}/register", action="cancel").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(cancel, users[:2])) == [303, 303]
    assert [booking(activity_id, user_id).status for user_id in users] == [
        "cancelled", "cancelled", "offered", "offered", "waitlisted",
    ]
    assert ActivityNotice.query.filter_by(activity_id=activity_id, event="offered").count() == 2


def test_fifo_offers_expire_skip_ineligible_members_and_are_idempotent(app, client, event, people, clock):
    _, _, attendee, first, ineligible, next_member, _, _ = people
    activity_id = event()
    enroll = f"/activities/{activity_id}/register"
    for user_id in (attendee, first, ineligible, next_member):
        assert join(client, activity_id, user_id).status_code == 303
        clock["now"] += timedelta(minutes=1)
    db.session.get(User, ineligible).status = "inactive"
    db.session.commit()
    login_as(client, attendee)
    assert post(client, enroll, action="cancel").status_code == 303
    first_booking = booking(activity_id, first)
    assert first_booking.status == "offered"
    assert first_booking.offer_expires_at.replace(tzinfo=timezone.utc) == clock["now"] + timedelta(hours=24)
    assert booking(activity_id, next_member).status == "waitlisted"
    assert booking(activity_id, ineligible).renewal_until is not None
    login_as(client, next_member)
    assert post(client, enroll, action="accept").status_code == 409
    clock["now"] += timedelta(hours=24)
    reconcile(app)
    reconcile(app)
    assert booking(activity_id, first).status == "expired"
    assert booking(activity_id, ineligible).status == "expired"
    assert booking(activity_id, next_member).status == "offered"
    assert ActivityNotice.query.filter_by(activity_id=activity_id, event="offered").count() == 2
    login_as(client, next_member)
    assert "Accept my place" in client.get(enroll).get_data(as_text=True)
    assert post(client, enroll, action="accept").status_code == 303
    assert booking(activity_id, next_member).status == "confirmed"


def test_organizer_approval_and_cohost_scope(app, client, event, people):
    owner, admin, attendee, waiting, cohost, unrelated, _, _ = people
    activity_id = event(mode="organizer_approval")
    other_id = event()
    db.session.add(ActivityCoorganizer(activity_id=activity_id, user_id=cohost, status="accepted"))
    db.session.commit()
    for user_id in (attendee, waiting):
        assert join(client, activity_id, user_id).status_code == 303
        assert booking(activity_id, user_id).status == "requested"
    login_as(client, owner)
    assert "waiting for organizer approval" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)
    row_id = booking(activity_id, attendee).id
    for user_id in (unrelated, admin, cohost):
        login_as(client, user_id)
        assert roster_action(client, activity_id, row_id, "approve").status_code == 404
    db.session.get(ActivityCoorganizer, (activity_id, cohost)).status = "approved"
    db.session.commit()
    assert client.get(f"{ROSTERS}/{activity_id}/roster").status_code == 200
    assert client.get(f"{ROSTERS}/{other_id}/roster").status_code == 404
    assert roster_action(client, activity_id, row_id, "approve").status_code == 303
    assert roster_action(client, activity_id, booking(activity_id, waiting).id, "approve").status_code == 303
    assert booking(activity_id, attendee).status == "confirmed"
    assert booking(activity_id, waiting).status == "waitlisted"
    assert post(client, f"{HOURS}/{row_id}/1/verify").status_code == 403
    db.session.get(ActivityCoorganizer, (activity_id, cohost)).status = "revoked"
    db.session.commit()
    assert client.get(f"{ROSTERS}/{activity_id}/roster").status_code == 404


def test_offer_cannot_extend_past_signup_close_and_confirmed_cancellation_has_cutoff(
    app, client, event, people, clock,
):
    _, _, attendee, waiting, _, _, _, _ = people
    starts = clock["now"] + timedelta(hours=2)
    activity_id = event(starts=starts)
    enroll = f"/activities/{activity_id}/register"
    for member in (attendee, waiting):
        assert join(client, activity_id, member).status_code == 303
    clock["now"] = starts - timedelta(minutes=75)
    login_as(client, attendee)
    assert post(client, enroll, action="cancel").status_code == 303
    assert booking(activity_id, waiting).offer_expires_at.replace(tzinfo=timezone.utc) == starts - timedelta(hours=1)
    login_as(client, waiting)
    assert post(client, enroll, action="accept").status_code == 303
    clock["now"] = starts - timedelta(hours=1)
    assert post(client, enroll, action="cancel").status_code == 409
    assert booking(activity_id, waiting).status == "confirmed"


def test_membership_rechecked_before_start_and_attendance_requires_eligibility(app, client, event, people, clock):
    owner, _, expired, current, _, _, _, _ = people
    activity_id = event(capacity=2)
    today = clock["now"].date()
    db.session.add(MembershipTerm(user_id=expired, term_number=1, membership_type="adult",
                                  status="active", approval_date=today, start_date=today,
                                  expiry_date=today + timedelta(days=1), annual_dues_cents=1000,
                                  payment_status="paid"))
    db.session.commit()
    assert join(client, activity_id, expired).status_code == 303
    assert join(client, activity_id, current).status_code == 303
    start = clock["now"] + timedelta(days=3)
    clock["now"] = start - timedelta(minutes=30)
    reconcile(app)
    assert booking(activity_id, expired).renewal_until.replace(tzinfo=timezone.utc) == start
    assert "PRIVATE" not in client.get(f"/activities/{activity_id}").get_data(as_text=True)
    login_as(client, expired)
    assert "PRIVATE" not in client.get(f"/activities/{activity_id}/register").get_data(as_text=True)
    clock["now"] = start
    reconcile(app)
    assert booking(activity_id, expired).status == "expired"
    assert booking(activity_id, current).start_checked
    login_as(client, owner)
    for user_id, expected in ((expired, 409), (current, 303)):
        assert roster_action(client, activity_id, booking(activity_id, user_id).id,
                             "attendance", attendance="attended").status_code == expected


def test_hours_reviews_preserve_corrections_and_use_latest_verified_total(app, client, event, people, clock):
    owner, admin, attendee, *_ = people
    activity_id = event()
    assert join(client, activity_id, attendee).status_code == 303
    row_id = booking(activity_id, attendee).id
    login_as(client, owner)
    assert roster_action(client, activity_id, row_id, "attendance", attendance="attended").status_code == 409
    clock["now"] += timedelta(days=3, hours=3)
    assert roster_action(client, activity_id, row_id, "attendance", attendance="attended").status_code == 303
    assert roster_action(client, activity_id, row_id, "hours", minutes="90").status_code == 303
    assert roster_action(client, activity_id, row_id, "hours", minutes="95").status_code == 409
    assert client.get(HOURS).status_code == 403
    login_as(client, admin)
    assert "90 minutes" in client.get(HOURS).get_data(as_text=True)
    assert post(client, f"{HOURS}/{row_id}/1/verify").status_code == 303
    assert post(client, f"{HOURS}/{row_id}/1/verify").status_code == 409
    login_as(client, owner)
    assert roster_action(client, activity_id, row_id, "hours", minutes="110",
                         submission_note="Reviewed volunteer log.").status_code == 303
    roster = f"{ROSTERS}/{activity_id}/roster"
    assert "90 verified volunteer minutes" in client.get(roster).get_data(as_text=True)
    login_as(client, admin)
    assert post(client, f"{HOURS}/{row_id}/2/reject", review_note="Correct the total.").status_code == 303
    login_as(client, owner)
    assert roster_action(client, activity_id, row_id, "hours", minutes="100",
                         submission_note="Recounted sign-in sheets.").status_code == 303
    login_as(client, admin)
    assert post(client, f"{HOURS}/{row_id}/3/verify").status_code == 303
    assert [(row.version, row.status, row.minutes) for row in ActivityVolunteerHours.query.order_by(
        ActivityVolunteerHours.version)] == [(1, "verified", 90), (2, "rejected", 110), (3, "verified", 100)]
    login_as(client, attendee)
    assert "100 verified volunteer minutes" in client.get(REGISTRATIONS).get_data(as_text=True)
    assert "Your volunteer hours were verified" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)
    login_as(client, owner)
    assert "100 verified volunteer minutes" in client.get(roster).get_data(as_text=True)
    assert roster_action(client, activity_id, row_id, "attendance", attendance="absent").status_code == 409


def test_activity_changes_pause_bookings_and_reapproval_checks_capacity(app, client, event, people):
    from tests.test_activities import change, proposal

    owner, admin, first, second, _, _, _, _ = people
    activity_id = event(capacity=2)
    for member in (first, second):
        assert join(client, activity_id, member).status_code == 303
    login_as(client, owner)
    assert change(client, f"{ROSTERS}/{activity_id}/edit", activity_id,
                  **proposal(title="Updated trail day", capacity="1", draft_action="submit")).status_code == 302
    login_as(client, first)
    assert "changed" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)
    html = client.get(f"/activities/{activity_id}/register").get_data(as_text=True)
    assert "Synthetic trail day" in html and "Updated trail day" not in html and "PRIVATE" not in html
    admin_url = f"/admin/community/activities/{activity_id}/state"
    login_as(client, admin)
    assert change(client, f"{admin_url}/approve", activity_id, confirm_public="yes").status_code == 409
    login_as(client, owner)
    assert roster_action(client, activity_id, booking(activity_id, second).id, "cancel").status_code == 303
    login_as(client, admin)
    assert change(client, f"{admin_url}/approve", activity_id, confirm_public="yes").status_code == 302
    assert booking(activity_id, first).status == "confirmed"
    login_as(client, first)
    assert "updated the activity" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)
    login_as(client, admin)
    assert change(client, f"{admin_url}/unpublish", activity_id, note="Event cancelled.").status_code == 302
    assert booking(activity_id, first).status == "cancelled"
    login_as(client, first)
    assert "activity was cancelled" in client.get(f"{REGISTRATIONS}/notices").get_data(as_text=True)


def test_account_deletion_releases_seat_and_removes_registration_name(app, client, event, people):
    _, admin, attendee, waiting, *_ = people
    activity_id = event()
    for user_id in (attendee, waiting):
        assert join(client, activity_id, user_id).status_code == 303
    row_id = booking(activity_id, attendee).id
    login_as(client, admin)
    assert post(client, f"/admin/members/{attendee}/delete").status_code == 302
    reconcile(app)
    removed = db.session.get(ActivityRegistration, row_id)
    assert removed.user_id is None and removed.display_name == "" and removed.status == "cancelled"
    assert booking(activity_id, waiting).status == "offered"
    assert ActivityNotice.query.filter_by(user_id=attendee).count() == 0


def test_paused_activity_releases_bookings_at_last_approved_start(app, client, event, people, clock):
    from tests.test_activities import change

    owner, _, attendee, *_ = people
    activity_id = event()
    assert join(client, activity_id, attendee).status_code == 303
    login_as(client, owner)
    assert change(client, f"{ROSTERS}/{activity_id}/edit", activity_id, title="Incomplete revision").status_code == 302
    clock["now"] += timedelta(days=3)
    reconcile(app)
    reconcile(app)
    assert booking(activity_id, attendee).status == "cancelled"
    assert ActivityNotice.query.filter_by(user_id=attendee, event="activity_cancelled").count() == 1


def test_policy_values_must_be_explicit_when_required(monkeypatch):
    settings = {
        "ACTIVITY_OFFER_HOURS": "24", "ACTIVITY_SIGNUP_CLOSE_MINUTES": "60",
        "ACTIVITY_CANCELLATION_CLOSE_MINUTES": "90", "ACTIVITY_RENEWAL_GRACE_HOURS": "0",
        "ACTIVITY_ALLOW_YOUTH_REGISTRATION": "false",
    }
    for key in settings:
        monkeypatch.delenv(key, raising=False)
    assert _registration_policy(False).offer_hours == 24
    with pytest.raises(RuntimeError, match="ACTIVITY_OFFER_HOURS"):
        _registration_policy(True)
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    policy = _registration_policy(True)
    assert policy.cancellation_close_minutes == 90 and policy.renewal_grace_hours == 0


def test_registration_migration_rolls_back_without_removing_activity_data(tmp_path):
    url = f"sqlite:///{tmp_path.as_posix()}/db.sqlite"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url})
    with app.app_context():
        upgrade(revision="f06c3d4e5f6a")
        activity = Activity(classification="official", status="draft")
        db.session.add(activity)
        db.session.flush()
        activity_id = activity.id
        db.session.commit()
        db.session.remove()
        upgrade()
        assert inspect(db.engine).has_table("activity_registration")
        downgrade(revision="f06c3d4e5f6a")
        assert not inspect(db.engine).has_table("activity_registration")
        assert db.session.get(Activity, activity_id).status == "draft"
        db.session.remove()
        upgrade()
        assert inspect(db.engine).has_table("activity_volunteer_hours")
