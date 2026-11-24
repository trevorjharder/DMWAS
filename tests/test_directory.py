"""Directory privacy, verified highlights, pagination and shared quota."""
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql

from app import (CommunityProfile, CommunityProfileHighlight, DirectoryAccessWindow,
    MembershipDetails, MembershipTerm, User, VerifiedCommunityRecord, VerifiedCommunityRecordAudit,
    db, eligible_community_profiles)
from app import VerifiedCommunityRecordParticipant, highlight_options
from tests.conftest import login_as


@pytest.fixture()
def directory(app, make_user):
    with app.app_context():
        owner, viewer, admin = make_user(), make_user(), make_user(is_admin=True)
        db.session.add_all([owner, viewer, admin])
        db.session.flush()
        profile = CommunityProfile(user_id=owner.id, display_name="Trail Alias", status="approved",
            directory_opt_in=True, biography="Secret biography", general_community="Hidden area",
            interests=["Hiking"], participation_goals=["Join Society activities"])
        db.session.add(profile)
        db.session.commit()
        return {"owner": owner.id, "viewer": viewer.id, "admin": admin.id, "profile": profile.id}


def search(client, **filters):
    return client.get("/api/community/profiles?" + urlencode(filters))


def test_directory_only_searches_shared_fields_and_display_names(client, app, directory):
    login_as(client, directory["viewer"])
    result = search(client, q="trail").json
    assert result["profiles"] == [{"display_name": "Trail Alias",
                                   "profile_url": f"/members/{directory['owner']}/profile"}]
    for filters in ({"q": "Synthetic"}, {"q": "Secret biography"}, {"community": "Hidden area"},
                    {"interest": "Hiking"}, {"goal": "Join Society activities"}):
        assert search(client, **filters).json["profiles"] == []
    with app.app_context():
        profile = db.session.get(CommunityProfile, directory["profile"])
        profile.show_community = profile.show_interests = profile.show_goals = True
        db.session.commit()
    for filters in ({"community": "hidden"}, {"interest": "Hiking"},
                    {"goal": "Join Society activities"}, {"q": "ALIAS", "interest": "Hiking", "community": "area"}):
        assert len(search(client, **filters).json["profiles"]) == 1
    assert b"No matching profiles" in client.get("/members/search?q=nonexistent").data
    assert b"Trail Alias" in client.get("/members/search?q=trail").data


@pytest.mark.parametrize("state", ["opted_out", "draft", "pending", "rejected", "youth", "expired", "unpaid", "inactive"])
def test_directory_filters_ineligible_profiles_before_results(client, app, directory, state):
    with app.app_context():
        profile = db.session.get(CommunityProfile, directory["profile"])
        if state == "opted_out":
            profile.directory_opt_in = False
        elif state == "youth":
            profile.user.membership_details.date_of_birth = date.today()
        elif state == "inactive":
            profile.user.status = "inactive"
        elif state in {"expired", "unpaid"}:
            db.session.add(MembershipTerm(user_id=profile.user_id, term_number=1,
                membership_type="adult", status="active", approval_date=date.today(),
                start_date=date.today(), expiry_date=date.today() + timedelta(days=-1 if state == "expired" else 100),
                payment_status="unpaid" if state == "unpaid" else "paid", annual_dues_cents=1000))
        else:
            profile.status = state
        db.session.commit()
    login_as(client, directory["viewer"])
    assert search(client).json["profiles"] == []
    assert client.get(f"/api/community/profiles/{directory['owner']}").status_code == 404


def test_directory_uses_latest_term_not_older_active_term(client, app, directory):
    with app.app_context():
        for number, status in ((1, "active"), (2, "pending")):
            db.session.add(MembershipTerm(user_id=directory["owner"], term_number=number,
                membership_type="adult", status=status, approval_date=date.today(), start_date=date.today(),
                expiry_date=date.today() + timedelta(days=100), payment_status="paid", annual_dues_cents=1000))
        db.session.commit()
    login_as(client, directory["viewer"])
    assert search(client).json["profiles"] == []


@pytest.mark.parametrize("viewer", ["anonymous", "youth", "expired"])
def test_directory_and_json_require_active_adult_viewer(client, app, directory, viewer):
    if viewer != "anonymous":
        with app.app_context():
            user = db.session.get(User, directory["viewer"])
            if viewer == "youth":
                user.membership_details.date_of_birth = date.today()
            else:
                user.status = "inactive"
            db.session.commit()
        login_as(client, directory["viewer"])
    assert search(client).status_code == 403
    assert client.get("/members/search").status_code in {302, 403}


@pytest.mark.parametrize("filters", [{"page": "oops"}, {"page": "0"}, {"page": "10001"},
    {"interest": "Invented interest"}, {"goal": "Invalid goal"}, {"q": "x" * 121}])
def test_invalid_directory_filters_fail_without_results(client, directory, filters):
    login_as(client, directory["viewer"])
    assert search(client, **filters).status_code == 400


def test_search_wildcards_are_literal(client, directory):
    login_as(client, directory["viewer"])
    assert search(client, q="%").json["profiles"] == []
    assert search(client, q="_").json["profiles"] == []


def test_pagination_is_stable_and_query_count_is_bounded(client, app, directory):
    with app.app_context():
        for number in range(25):
            user = User(full_name="Private name", username=f"page-{number}", email=f"page-{number}@example.test",
                        password_hash="unused", status="active")
            user.membership_details = MembershipDetails(first_name="Private", last_name="Name",
                community="Secret", date_of_birth=date(1990, 1, 1), age_19_or_older=True,
                mailing_address="Secret", membership_type="adult", activities=[])
            db.session.add(user)
            db.session.flush()
            db.session.add(CommunityProfile(user_id=user.id, display_name="Repeated alias",
                directory_opt_in=True, status="approved", show_photo=True, photo=b"not-fetched"))
        db.session.commit()
    login_as(client, directory["viewer"])
    statements = []
    def track(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    with app.app_context():
        event.listen(db.engine, "before_cursor_execute", track)
        try:
            first = search(client).json
        finally:
            event.remove(db.engine, "before_cursor_execute", track)
    assert len(first["profiles"]) == 20 and first["has_next"]
    assert len(statements) <= 10  # No query per row and no loading photo blobs.
    assert any("LIMIT" in statement.upper() for statement in statements)
    second = search(client, page=2).json
    assert len(second["profiles"]) == 6 and second["has_previous"] and not second["has_next"]
    first_urls = {row["profile_url"] for row in first["profiles"]}
    assert not first_urls & {row["profile_url"] for row in second["profiles"]}
    assert first["profiles"] == search(client).json["profiles"]
    assert search(client, page=50).json["profiles"] == []


def test_directory_rate_limit_shared_across_clients_and_endpoints(client, app, directory):
    app.config["DIRECTORY_REQUESTS_PER_MINUTE"] = 2
    login_as(client, directory["viewer"])
    assert search(client).status_code == 200
    other = app.test_client()
    login_as(other, directory["viewer"])
    assert other.get(f"/api/community/profiles/{directory['owner']}").status_code == 200
    response = client.get("/members/search")
    assert response.status_code == 429 and 1 <= int(response.headers["Retry-After"]) <= 60
    assert "no-store" in response.headers["Cache-Control"]
    with app.app_context():
        window = db.session.get(DirectoryAccessWindow, directory["viewer"])
        assert window.requests == 2
        window.window_number -= 1
        db.session.commit()
    assert search(client).status_code == 200
    login_as(other, directory["owner"])
    assert search(other).status_code == 200


def record_payload(directory, **changes):
    return {"csrf_token": "test-csrf-token", "user_id": str(directory["owner"]),
            "title": "Verified trail work", "record_type": "volunteer", "occurred_on": "2026-01-01",
            "volunteer_minutes": "90", "verified": "yes", "shareable": "yes", **changes}


def test_admin_verification_member_selection_and_immediate_revocation(client, app, directory):
    login_as(client, directory["viewer"])
    assert client.get("/admin/community/records").status_code == 403
    assert client.post("/admin/community/records", data=record_payload(directory)).status_code == 403
    login_as(client, directory["admin"])
    assert client.get("/admin/community/records").status_code == 200
    assert client.post("/admin/community/records", data=record_payload(directory)).status_code == 302
    with app.app_context():
        record = VerifiedCommunityRecord.query.one()
        record_id = record.id
        assert VerifiedCommunityRecordAudit.query.one().actor_id == directory["admin"]
    login_as(client, directory["owner"])
    assert b"Verified trail work" in client.get("/member/profile/community/edit").data
    payload = {"csrf_token": "test-csrf-token", "display_name": "Trail Alias", "directory_opt_in": "yes",
               "show_highlights": "yes", "highlights": str(record_id)}
    assert client.post("/member/profile", data=payload).status_code == 302
    login_as(client, directory["viewer"])
    response = client.get(f"/api/community/profiles/{directory['owner']}")
    assert response.json["highlights"] == [{"title": "Verified trail work", "type": "volunteer",
                                             "date": "2026-01-01", "volunteer_minutes": 90}]
    assert b"Verified trail work" in client.get(f"/members/{directory['owner']}/profile").data
    login_as(client, directory["admin"])
    assert client.post("/admin/community/records", data=record_payload(directory, record_id=record_id,
        shareable="")).status_code == 302
    with app.app_context():
        assert VerifiedCommunityRecordAudit.query.count() == 2
    login_as(client, directory["viewer"])
    assert client.get(f"/api/community/profiles/{directory['owner']}").json["highlights"] == []
    assert b"Verified trail work" not in client.get(f"/members/{directory['owner']}/profile").data


@pytest.mark.parametrize("invalid", ["other_owner", "private", "sensitive", "unverified"])
def test_member_cannot_select_invalid_highlights_or_leak_stale_selection(client, app, directory, invalid):
    with app.app_context():
        record = VerifiedCommunityRecord(user_id=directory["viewer"] if invalid == "other_owner" else directory["owner"],
            title="Never reveal this record", record_type="activity", occurred_on=date(2026, 1, 1),
            verified=invalid != "unverified", shareable=invalid != "private", sensitive=invalid == "sensitive",
            verified_by_id=directory["admin"])
        db.session.add(record)
        db.session.flush()
        record_id = record.id
        profile = db.session.get(CommunityProfile, directory["profile"])
        profile.show_highlights = True
        profile.highlights.append(CommunityProfileHighlight(record_id=record_id))
        db.session.commit()
    login_as(client, directory["owner"])
    assert b"Never reveal this record" not in client.get("/member/profile/community/edit").data
    assert client.post("/member/profile", data={"csrf_token": "test-csrf-token", "display_name": "Trail Alias",
        "highlights": str(record_id), "show_highlights": "yes"}).status_code == 400
    login_as(client, directory["viewer"])
    assert client.get(f"/api/community/profiles/{directory['owner']}").json["highlights"] == []
    assert b"Never reveal this record" not in client.get(f"/members/{directory['owner']}/profile").data


def test_verified_records_are_private_by_default_and_cannot_change_owner(client, app, directory):
    login_as(client, directory["admin"])
    assert client.post("/admin/community/records", data=record_payload(directory, shareable="")).status_code == 302
    with app.app_context():
        record_id = VerifiedCommunityRecord.query.one().id
    assert client.post("/admin/community/records", data=record_payload(directory,
        record_id=record_id, user_id=directory["viewer"])).status_code == 400
    assert client.post("/admin/community/records", data=record_payload(directory,
        occurred_on="2999-01-01")).status_code == 400
    assert client.post("/admin/community/records", data=record_payload(directory,
        volunteer_minutes="-1")).status_code == 400
    assert client.post("/admin/community/records", data={}).status_code == 400


def test_profile_and_account_deletion_clean_up_highlight_data(client, app, directory):
    login_as(client, directory["admin"])
    client.post("/admin/community/records", data=record_payload(directory))
    with app.app_context():
        record_id = VerifiedCommunityRecord.query.one().id
    login_as(client, directory["owner"])
    client.post("/member/profile", data={"csrf_token": "test-csrf-token", "display_name": "Trail Alias",
        "directory_opt_in": "yes", "highlights": str(record_id), "show_highlights": "yes"})
    client.get("/members/search")
    assert client.post("/member/profile/delete", data={"csrf_token": "test-csrf-token",
        "confirm_delete": "yes"}).status_code == 302
    with app.app_context():
        assert CommunityProfileHighlight.query.count() == 0
        assert VerifiedCommunityRecord.query.count() == 1
        assert VerifiedCommunityRecordAudit.query.count() == 1
    login_as(client, directory["admin"])
    assert client.post(f"/admin/members/{directory['owner']}/delete", data={
        "csrf_token": "test-csrf-token"}).status_code == 302
    with app.app_context():
        assert db.session.get(User, directory["owner"]) is None
        assert VerifiedCommunityRecord.query.count() == 0
        assert VerifiedCommunityRecordAudit.query.count() == 0
        assert DirectoryAccessWindow.query.filter_by(user_id=directory["owner"]).count() == 0


def test_selected_highlights_require_separate_visibility_choice(client, app, directory):
    login_as(client, directory["admin"])
    client.post("/admin/community/records", data=record_payload(directory))
    with app.app_context():
        record_id = VerifiedCommunityRecord.query.one().id
    login_as(client, directory["owner"])
    assert client.post("/member/profile", data={"csrf_token": "test-csrf-token",
        "display_name": "Trail Alias", "directory_opt_in": "yes", "highlights": str(record_id)}).status_code == 302
    login_as(client, directory["viewer"])
    assert "highlights" not in client.get(f"/api/community/profiles/{directory['owner']}").json
    assert b"Verified trail work" not in client.get(f"/members/{directory['owner']}/profile").data


def test_postgres_directory_query_compiles_with_eligibility_in_sql(app):
    with app.app_context():
        sql = str(eligible_community_profiles().statement.compile(dialect=postgresql.dialect()))
        assert "max(membership_term.term_number)" in sql
        assert "date_of_birth" in sql and "EXISTS" in sql


def test_shared_record_is_one_entry_with_independent_member_highlights(client, app, directory):
    login_as(client, directory["admin"])
    payload = record_payload(directory)
    payload.pop("user_id")
    payload["user_ids"] = [str(directory["owner"]), str(directory["viewer"]), str(directory["owner"])]
    assert client.post("/admin/community/records", data=payload).status_code == 302
    with app.app_context():
        record = VerifiedCommunityRecord.query.one()
        record_id = record.id
        assert VerifiedCommunityRecordParticipant.query.count() == 2
        assert [r.id for r in highlight_options(directory["owner"])] == [record_id]
        assert [r.id for r in highlight_options(directory["viewer"])] == [record_id]
        assert highlight_options(directory["admin"]) == []
        assert VerifiedCommunityRecordAudit.query.one().snapshot["member_ids"] == sorted(
            [directory["owner"], directory["viewer"]])
    for member_id in (directory["owner"], directory["viewer"]):
        login_as(client, member_id)
        assert client.post("/member/profile", data={"csrf_token": "test-csrf-token",
            "display_name": "Shared activity member", "directory_opt_in": "yes", "show_highlights": "yes",
            "highlights": str(record_id)}).status_code == 302
    login_as(client, directory["admin"])
    for member_id in (directory["owner"], directory["viewer"]):
        detail = client.get(f"/api/community/profiles/{member_id}").json
        assert len(detail["highlights"]) == 1
        assert set(detail["highlights"][0]) == {"title", "date", "type", "volunteer_minutes"}
    # One member opting out does not affect the other member's choice.
    login_as(client, directory["owner"])
    client.post("/member/profile", data={"csrf_token": "test-csrf-token", "display_name": "Private member"})
    login_as(client, directory["admin"])
    assert client.get(f"/api/community/profiles/{directory['owner']}").status_code == 404
    assert len(client.get(f"/api/community/profiles/{directory['viewer']}").json["highlights"]) == 1
    # Removing a participant clears only that member's selections.
    payload.update(record_id=record_id, user_ids=[str(directory["owner"])])
    assert client.post("/admin/community/records", data=payload).status_code == 302
    assert client.get(f"/api/community/profiles/{directory['viewer']}").json["highlights"] == []
    with app.app_context():
        assert highlight_options(directory["viewer"]) == []
        assert CommunityProfileHighlight.query.count() == 0


def test_deleting_primary_member_preserves_shared_record_for_others(client, app, directory):
    login_as(client, directory["admin"])
    payload = record_payload(directory)
    payload.pop("user_id")
    payload["user_ids"] = [str(directory["owner"]), str(directory["viewer"])]
    client.post("/admin/community/records", data=payload)
    with app.app_context():
        record_id = VerifiedCommunityRecord.query.one().id
    login_as(client, directory["viewer"])
    client.post("/member/profile", data={"csrf_token": "test-csrf-token", "display_name": "Remaining member",
        "directory_opt_in": "yes", "show_highlights": "yes", "highlights": str(record_id)})
    login_as(client, directory["admin"])
    assert client.post(f"/admin/members/{directory['owner']}/delete", data={
        "csrf_token": "test-csrf-token"}).status_code == 302
    with app.app_context():
        record = db.session.get(VerifiedCommunityRecord, record_id)
        assert record.user_id == directory["viewer"]
        assert [participant.user_id for participant in record.participants] == [directory["viewer"]]
        assert VerifiedCommunityRecordAudit.query.count() == 1
    assert len(client.get(f"/api/community/profiles/{directory['viewer']}").json["highlights"]) == 1


@pytest.mark.parametrize("member_ids", [[], ["invalid"], ["999999999"]])
def test_shared_record_invalid_member_lists_are_atomic(client, app, directory, member_ids):
    login_as(client, directory["admin"])
    payload = record_payload(directory)
    payload.pop("user_id")
    payload["user_ids"] = member_ids
    assert client.post("/admin/community/records", data=payload).status_code == 400
    with app.app_context():
        assert VerifiedCommunityRecord.query.count() == 0
        assert VerifiedCommunityRecordParticipant.query.count() == 0


def test_single_sharing_control_preserves_legacy_privacy(client, app, directory):
    with app.app_context():
        record = VerifiedCommunityRecord(user_id=directory["owner"], title="Legacy hidden record",
            record_type="activity", occurred_on=date(2026, 1, 1), verified=True,
            shareable=True, sensitive=True, verified_by_id=directory["admin"])
        db.session.add(record)
        db.session.commit()
        record_id = record.id
    login_as(client, directory["admin"])
    page = client.get("/admin/community/records")
    assert b"Allow members to share this activity" in page.data
    assert b'name="verified"' not in page.data
    assert b'name="sensitive"' not in page.data
    assert b'name="shareable" value="yes" checked' not in page.data
    payload = record_payload(directory, record_id=record_id, shareable="")
    assert client.post("/admin/community/records", data=payload).status_code == 302
    with app.app_context():
        record = db.session.get(VerifiedCommunityRecord, record_id)
        assert record.verified and not record.shareable
        assert highlight_options(directory["owner"]) == []
    payload["shareable"] = "yes"
    assert client.post("/admin/community/records", data=payload).status_code == 302
    with app.app_context():
        record = db.session.get(VerifiedCommunityRecord, record_id)
        assert record.verified and record.shareable and not record.sensitive
        assert len(highlight_options(directory["owner"])) == 1


def test_verified_highlights_follow_goals_in_profile_and_preview(client, app, directory):
    login_as(client, directory["admin"])
    client.post("/admin/community/records", data=record_payload(directory))
    with app.app_context():
        record_id = VerifiedCommunityRecord.query.one().id
    login_as(client, directory["owner"])
    client.post("/member/profile", data={"csrf_token": "test-csrf-token", "display_name": "Trail Alias",
        "directory_opt_in": "yes", "show_highlights": "yes", "highlights": str(record_id),
        "show_goals": "yes", "participation_goals": "Join Society activities"})
    for path in ("/member/profile/preview", f"/members/{directory['owner']}/profile"):
        page = client.get(path)
        assert page.status_code == 200
        assert page.data.index(b"Participation goals") < page.data.index(b"Verified highlights")
        assert b"adult directory" not in page.data.lower()
    assert b"adult member directory" not in client.get("/members/search").data.lower()
