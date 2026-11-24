"""Directory privacy, pagination and shared quota."""
from datetime import date, timedelta
from urllib.parse import urlencode

import pytest
from flask_migrate import downgrade, upgrade
from sqlalchemy import MetaData, event, inspect
from sqlalchemy.dialects import postgresql

from app import (
    CommunityProfile, DirectoryAccessWindow, MembershipDetails, MembershipTerm, User,
    create_app, db, eligible_community_profiles,
)
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


def test_postgres_directory_query_compiles_with_eligibility_in_sql(app):
    with app.app_context():
        sql = str(eligible_community_profiles().statement.compile(dialect=postgresql.dialect()))
        assert "max(membership_term.term_number)" in sql
        assert "date_of_birth" in sql and "EXISTS" in sql


def test_removed_highlights_are_unavailable_in_profiles_and_admin(client, directory):
    login_as(client, directory["owner"])
    editor = "/member/profile/community/edit"
    form = client.get(editor)
    assert form.status_code == 200
    assert b'name="show_highlights"' not in form.data
    assert client.post(editor, data={
        "csrf_token": "test-csrf-token", "display_name": "Trail Alias", "directory_opt_in": "yes",
        "highlights": "obsolete", "show_highlights": "yes",
    }).status_code == 302
    assert b"Verified highlights" not in client.get("/member/profile/preview").data

    login_as(client, directory["viewer"])
    profile = client.get(f"/api/community/profiles/{directory['owner']}")
    assert profile.json == {"display_name": "Trail Alias"}

    login_as(client, directory["admin"])
    assert client.get("/admin/community/records").status_code == 404
    assert client.post("/admin/community/records", data={"csrf_token": "test-csrf-token"}).status_code == 404


def test_migration_erases_highlight_records_and_preserves_profiles(tmp_path, make_user):
    url = f"sqlite:///{tmp_path.as_posix()}/db.sqlite"
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": url})
    previous_revision = "b08e5f6a7b8c"
    removed_tables = {
        "verified_community_record", "verified_community_record_participant",
        "verified_community_record_audit", "community_profile_highlight",
    }
    with app.app_context():
        upgrade(revision=previous_revision)
        user = make_user(is_admin=True)
        profile = CommunityProfile(display_name="Trail Alias", user=user, directory_opt_in=True)
        db.session.add(profile)
        db.session.flush()
        profile_id, user_id = profile.id, user.id
        schema = MetaData()
        schema.reflect(bind=db.engine, only=[*removed_tables, "community_profile"])
        tables = schema.tables
        db.session.execute(tables["community_profile"].update()
                           .where(tables["community_profile"].c.id == profile_id)
                           .values(show_highlights=True))
        db.session.execute(tables["verified_community_record"].insert().values(
            id=1, user_id=user_id, title="Past volunteer work", record_type="volunteer",
            occurred_on=date(2026, 1, 1), volunteer_minutes=60, verified=True, shareable=True,
            sensitive=False, verified_by_id=user_id, verified_at=date(2026, 1, 2),
        ))
        db.session.execute(tables["verified_community_record_participant"].insert().values(
            record_id=1, user_id=user_id,
        ))
        db.session.execute(tables["verified_community_record_audit"].insert().values(
            record_id=1, actor_id=user_id, snapshot={"title": "Past volunteer work"},
            created_at=date(2026, 1, 2),
        ))
        db.session.execute(tables["community_profile_highlight"].insert().values(
            profile_id=profile_id, record_id=1,
        ))
        db.session.commit()
        db.session.remove()

        upgrade()
        inspector = inspect(db.engine)
        assert removed_tables.isdisjoint(inspector.get_table_names())
        assert "show_highlights" not in {column["name"] for column in inspector.get_columns("community_profile")}
        assert inspector.has_table("directory_access_window")
        assert inspector.has_table("activity_volunteer_hours")
        profile = db.session.get(CommunityProfile, profile_id)
        assert profile.display_name == "Trail Alias"
        assert profile.directory_opt_in
        db.session.remove()

        downgrade(revision=previous_revision)
        with db.engine.connect() as connection:
            for name in removed_tables:
                assert connection.execute(tables[name].select()).all() == []
            assert connection.execute(tables["community_profile"].select()
                                      .where(tables["community_profile"].c.id == profile_id)
                                      ).one().show_highlights is False
