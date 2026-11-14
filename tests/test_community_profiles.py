"""Phase 04 privacy boundaries, erasure and schema recovery."""
from datetime import date, timedelta
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import inspect, text

from app import CommunityProfile, CommunityProfileReview, MembershipTerm, User, create_app, db
from tests.conftest import login_as


@pytest.fixture()
def profiles(app, make_user):
    with app.app_context():
        owner, viewer, admin = make_user(), make_user(), make_user(is_admin=True)
        youth = make_user(birth_date=date.today().replace(year=date.today().year - 10))
        db.session.add_all([owner, viewer, admin, youth])
        db.session.flush()
        profile = CommunityProfile(
            user_id=owner.id, display_name="Trail Alias", biography="Hidden biography marker",
            general_community="Hidden community marker", interests=["Hiking"],
            participation_goals=["Join Society activities"], directory_opt_in=True,
            status="approved", photo=photo_bytes(),
        )
        db.session.add(profile)
        db.session.commit()
        return dict(owner=owner.id, viewer=viewer.id, admin=admin.id,
                    youth=youth.id, profile=profile.id)


def photo_bytes():
    output = BytesIO()
    image = Image.new("RGB", (800, 400), "blue")
    exif = Image.Exif()
    exif[270] = "private image metadata"
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def save_profile(client, **values):
    return client.post("/member/profile", data={
        "csrf_token": "test-csrf-token", "display_name": "Trail Alias", **values,
    })


def moderate(client, ids, action="approve", version=1):
    login_as(client, ids["admin"])
    return client.post(f"/admin/community/profiles/{ids['profile']}/{action}", data={
        "csrf_token": "test-csrf-token", "profile_version": str(version),
        "moderation_note": "Reviewed submitted fields",
    })


@pytest.mark.parametrize("account", ["admin", "owner"])
def test_profile_is_auto_approved_on_create_and_edit(client, app, profiles, account):
    # Use a new profile for both roles.
    with app.app_context():
        if account == "owner":
            db.session.delete(db.session.get(CommunityProfile, profiles["profile"]))
            db.session.commit()
    profiles = dict(profiles, admin=profiles[account])
    login_as(client, profiles["admin"])
    for biography in ("First submission", "Updated submission"):
        response = save_profile(client, directory_opt_in="yes", biography=biography)
        assert response.status_code == 302
        with app.app_context():
            profile = CommunityProfile.query.filter_by(user_id=profiles["admin"]).one()
            assert profile.status == "approved"
            assert profile.moderated_by_id == profiles["admin"]
            assert profile.moderated_at is not None
            assert profile.reviews[-1].decision == "approve"
            assert profile.reviews[-1].moderator_user_id == profiles["admin"]
        login_as(client, profiles["viewer"])
        assert client.get(f"/api/community/profiles/{profiles['admin']}").json == {"display_name": "Trail Alias"}
        login_as(client, profiles["admin"])
    with app.app_context():
        assert len(CommunityProfile.query.filter_by(user_id=profiles["admin"]).one().reviews) == 2
    # Administrator status never grants directory consent on the member's behalf.
    assert save_profile(client).status_code == 302
    with app.app_context():
        profile = CommunityProfile.query.filter_by(user_id=profiles["admin"]).one()
        assert not profile.directory_opt_in
        assert profile.status == "draft"
    login_as(client, profiles["viewer"])
    assert client.get(f"/api/community/profiles/{profiles['admin']}").status_code == 404


@pytest.mark.parametrize("ineligible", ["youth", "expired"])
def test_ineligible_admin_cannot_auto_approve_directory_profile(client, app, profiles, ineligible):
    with app.app_context():
        admin = db.session.get(User, profiles["admin"])
        if ineligible == "youth":
            admin.membership_details.date_of_birth = date.today()
        else:
            admin.status = "inactive"
        db.session.commit()
    login_as(client, profiles["admin"])
    assert save_profile(client, directory_opt_in="yes").status_code == 400
    with app.app_context():
        assert CommunityProfile.query.filter_by(user_id=profiles["admin"]).first() is None


def test_html_api_and_search_never_expose_hidden_fields(client, profiles):
    login_as(client, profiles["viewer"])
    api = client.get(f"/api/community/profiles/{profiles['owner']}")
    assert api.json == {"display_name": "Trail Alias"}
    assert api.headers["Cache-Control"] == "private, no-store"
    html = client.get(f"/members/{profiles['owner']}/profile")
    search = client.get("/members/search?q=Hidden&interests=Hiking")
    for response in (html, search):
        assert response.status_code == 200
        for value in (b"Hidden biography marker", b"Hidden community marker",
                      b"synthetic-1@example.test", b"1 Example Street"):
            assert value not in response.data
    assert b"Trail Alias" not in search.data  # Directory is a Phase 05 feature.
    assert client.get(f"/members/{profiles['owner']}/profile/photo").status_code == 404


@pytest.mark.parametrize("flag,key,value", [
    ("show_biography", "biography", "Hidden biography marker"),
    ("show_community", "community", "Hidden community marker"),
    ("show_interests", "interests", ["Hiking"]),
    ("show_goals", "goals", ["Join Society activities"]),
    ("show_photo", "photo_url", None),
])
def test_visibility_flags_apply_independently_to_api(client, app, profiles, flag, key, value):
    with app.app_context():
        setattr(db.session.get(CommunityProfile, profiles["profile"]), flag, True)
        db.session.commit()
    login_as(client, profiles["viewer"])
    data = client.get(f"/api/community/profiles/{profiles['owner']}").json
    assert set(data) == {"display_name", key}
    assert data[key] == (value if value is not None else f"/members/{profiles['owner']}/profile/photo")


@pytest.mark.parametrize("state", ["draft", "pending", "rejected", "opted_out", "expired", "youth"])
def test_undiscoverable_profiles_fail_closed_for_direct_urls_and_api(client, app, profiles, state):
    with app.app_context():
        profile = db.session.get(CommunityProfile, profiles["profile"])
        profile.show_photo = True
        if state == "opted_out":
            profile.directory_opt_in = False
        elif state == "expired":
            # Status is deliberately stale: policy must inspect the term too.
            db.session.add(MembershipTerm(
                user_id=profiles["owner"], term_number=1, membership_type="adult",
                status="active", approval_date=date(2020, 1, 1), start_date=date(2020, 1, 1),
                expiry_date=date.today() - timedelta(days=1), annual_dues_cents=1000,
                payment_status="paid",
            ))
        elif state == "youth":
            profile.user.membership_details.date_of_birth = date.today()
        else:
            profile.status = state
        db.session.commit()
    login_as(client, profiles["viewer"])
    for path in (f"/members/{profiles['owner']}/profile",
                 f"/members/{profiles['owner']}/profile/photo",
                 f"/api/community/profiles/{profiles['owner']}"):
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("viewer", ["anonymous", "youth", "expired"])
def test_ineligible_viewers_cannot_read_profiles_or_api(client, app, profiles, viewer):
    if viewer != "anonymous":
        user_id = profiles["youth"] if viewer == "youth" else profiles["viewer"]
        if viewer == "expired":
            with app.app_context():
                db.session.get(User, user_id).status = "expired"
                db.session.commit()
        login_as(client, user_id)
    assert client.get(f"/api/community/profiles/{profiles['owner']}").status_code == 403
    assert client.get(f"/members/{profiles['owner']}/profile").status_code in {302, 403}


def test_edits_are_auto_approved_and_opt_out_hides_immediately(client, app, profiles):
    login_as(client, profiles["owner"])
    assert save_profile(client, directory_opt_in="yes", biography="Changed content").status_code == 302
    with app.app_context():
        profile = db.session.get(CommunityProfile, profiles["profile"])
        assert profile.status == "approved"
        assert profile.moderated_at is not None
    login_as(client, profiles["viewer"])
    assert client.get(f"/api/community/profiles/{profiles['owner']}").status_code == 200
    login_as(client, profiles["owner"])
    assert save_profile(client).status_code == 302
    login_as(client, profiles["viewer"])
    assert client.get(f"/api/community/profiles/{profiles['owner']}").status_code == 404


def test_moderation_hides_private_fields_and_can_unpublish_with_audit(client, app, profiles):
    login_as(client, profiles["admin"])
    page = client.get("/admin/community/profiles?status=approved")
    assert b"Trail Alias" in page.data
    assert b"Hidden biography marker" not in page.data
    assert b"Hidden community marker" not in page.data
    assert client.get(f"/members/{profiles['owner']}/profile/photo").status_code == 404
    assert moderate(client, profiles, action="reject").status_code == 302
    with app.app_context():
        assert db.session.get(CommunityProfile, profiles["profile"]).status == "rejected"
        review = CommunityProfileReview.query.one()
        assert review.moderator_user_id == profiles["admin"]
        assert review.decision == "reject"
    login_as(client, profiles["viewer"])
    assert client.get(f"/api/community/profiles/{profiles['owner']}").status_code == 404


def test_stale_review_cannot_unpublish_new_content(client, app, profiles):
    login_as(client, profiles["owner"])
    save_profile(client, directory_opt_in="yes")
    assert moderate(client, profiles, action="reject", version=1).status_code == 409
    with app.app_context():
        assert CommunityProfileReview.query.count() == 1
        assert db.session.get(CommunityProfile, profiles["profile"]).status == "approved"
    assert moderate(client, profiles, action="reject", version=2).status_code == 302


def test_preview_and_mutations_are_owner_scoped_and_csrf_protected(client, app, profiles):
    login_as(client, profiles["viewer"])
    assert b"Trail Alias" not in client.get(f"/member/profile/preview?user_id={profiles['owner']}").data
    assert client.post("/member/profile", data={"display_name": "Changed"}).status_code == 400
    assert client.post("/member/profile/delete", data={"confirm_delete": "yes"}).status_code == 400
    assert client.get("/admin/community/profiles").status_code == 403
    assert client.post(f"/admin/community/profiles/{profiles['profile']}/reject", data={
        "csrf_token": "test-csrf-token", "profile_version": "1",
    }).status_code == 403
    assert client.post("/member/profile/delete", data={
        "csrf_token": "test-csrf-token", "confirm_delete": "yes", "user_id": profiles["owner"],
    }).status_code == 302
    with app.app_context():
        assert db.session.get(CommunityProfile, profiles["profile"]) is not None


@pytest.mark.parametrize("field,value", [
    ("display_name", ""), ("display_name", "x" * 81),
    ("biography", "x" * 501), ("general_community", "x" * 121),
    ("interests", "Not an allowed interest"),
    ("participation_goals", "Not an allowed goal"),
])
def test_validation_leaves_previous_profile_unchanged(client, app, profiles, field, value):
    login_as(client, profiles["owner"])
    assert save_profile(client, **{field: value}).status_code == 400
    with app.app_context():
        assert db.session.get(CommunityProfile, profiles["profile"]).version == 1


def test_photo_upload_is_normalized_private_and_removed(client, app, profiles):
    login_as(client, profiles["owner"])
    response = save_profile(client, photo=(BytesIO(photo_bytes()), "location.jpg"))
    assert response.status_code == 302
    photo = client.get(f"/members/{profiles['owner']}/profile/photo")
    assert photo.status_code == 200
    assert photo.mimetype == "image/jpeg"
    assert "no-store" in photo.headers["Cache-Control"]
    assert b"private image metadata" not in photo.data
    with Image.open(BytesIO(photo.data)) as image:
        assert image.size == (512, 256)
        assert not image.getexif()
    assert save_profile(client, remove_photo="yes").status_code == 302
    assert client.get(f"/members/{profiles['owner']}/profile/photo").status_code == 404


def test_shared_photo_is_auto_approved_and_hidden_after_opt_out(client, profiles):
    login_as(client, profiles["owner"])
    save_profile(client, directory_opt_in="yes", show_photo="yes",
                 photo=(BytesIO(photo_bytes()), "photo.jpg"))
    login_as(client, profiles["viewer"])
    photo_url = f"/members/{profiles['owner']}/profile/photo"
    assert client.get(photo_url).status_code == 200
    login_as(client, profiles["admin"])
    assert client.get(photo_url).status_code == 200  # Only the sharing submission.
    login_as(client, profiles["viewer"])
    assert client.get(photo_url).status_code == 200
    login_as(client, profiles["owner"])
    save_profile(client, show_photo="yes")
    login_as(client, profiles["viewer"])
    assert client.get(photo_url).status_code == 404


def test_script_markup_is_escaped_in_profile_and_review_pages(client, app, profiles):
    with app.app_context():
        profile = db.session.get(CommunityProfile, profiles["profile"])
        profile.biography = "<script>alert('profile')</script>"
        profile.show_biography = True
        db.session.commit()
    login_as(client, profiles["viewer"])
    response = client.get(f"/members/{profiles['owner']}/profile")
    assert b"<script>alert" not in response.data
    assert b"&lt;script&gt;alert" in response.data
    login_as(client, profiles["admin"])
    response = client.get("/admin/community/profiles?status=approved")
    assert b"<script>alert" not in response.data
    assert b"&lt;script&gt;alert" in response.data


def test_oversized_photo_request_is_rejected_before_processing(client, profiles):
    login_as(client, profiles["owner"])
    assert save_profile(client, photo=(BytesIO(b"x" * (4 * 1024 * 1024)), "photo.jpg")).status_code == 413


@pytest.mark.parametrize("content,name", [
    (b"<svg onload='alert(1)'/>", "photo.png"),
    (b"not an image", "photo.jpg"),
    (b"x" * (2 * 1024 * 1024 + 1), "photo.jpg"),
], ids=["disguised-svg", "invalid-jpeg", "oversized"])
def test_invalid_photos_are_rejected_without_saving(client, app, profiles, content, name):
    login_as(client, profiles["owner"])
    assert save_profile(client, photo=(BytesIO(content), name)).status_code == 400
    with app.app_context():
        assert db.session.get(CommunityProfile, profiles["profile"]).version == 1


def test_expired_owner_can_delete_profile_and_history_without_losing_membership(client, app, profiles):
    assert moderate(client, profiles, action="reject").status_code == 302
    with app.app_context():
        db.session.get(User, profiles["owner"]).status = "expired"
        db.session.commit()
    login_as(client, profiles["owner"])
    assert client.get("/member/profile").status_code == 200
    assert b"Private" in client.get("/member/profile/preview").data
    assert client.post("/member/profile/delete", data={"csrf_token": "test-csrf-token"}).status_code == 400
    assert client.post("/member/profile/delete", data={
        "csrf_token": "test-csrf-token", "confirm_delete": "yes",
    }).status_code == 302
    with app.app_context():
        assert CommunityProfile.query.count() == 0
        assert CommunityProfileReview.query.count() == 0
        assert db.session.get(User, profiles["owner"]).membership_details is not None


def test_migration_upgrade_downgrade_preserves_existing_members(tmp_path):
    test_app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'migration.sqlite').as_posix()}"})
    runner = test_app.test_cli_runner()
    def command(*args):
        result = runner.invoke(args=["db", *args])
        assert result.exit_code == 0, result.output
    command("upgrade", "958ef19b9634")
    with test_app.app_context():
        db.session.add(User(full_name="Synthetic Existing", email="existing@example.test",
                            username="existing", password_hash="unused", status="active"))
        db.session.commit()
    command("upgrade")
    command("check")
    with test_app.app_context():
        db.session.execute(text("PRAGMA foreign_keys=ON"))
        member = User.query.one()
        profile = CommunityProfile(user_id=member.id, display_name="Migration profile")
        db.session.add(profile)
        db.session.flush()
        db.session.add(CommunityProfileReview(profile_id=profile.id, moderator_user_id=member.id,
                                             decision="approve"))
        db.session.commit()
    command("downgrade", "958ef19b9634")
    with test_app.app_context():
        assert User.query.one().username == "existing"
        assert "community_profile" not in inspect(db.engine).get_table_names()
    command("upgrade")
    command("check")
