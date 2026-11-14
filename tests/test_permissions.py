from datetime import date
from types import SimpleNamespace

import pytest

from permissions import Capability, adult_birth_date_cutoff, has_capability, is_adult_member


TODAY = date(2026, 11, 14)


def make_user(
    *,
    user_id=1,
    status="active",
    is_admin=False,
    birth_date=date(1990, 1, 1),
    membership_type="adult",
    stored_age_flag=False,
):
    return SimpleNamespace(
        id=user_id,
        status=status,
        is_admin=is_admin,
        membership_details=SimpleNamespace(
            date_of_birth=birth_date,
            membership_type=membership_type,
            age_19_or_older=stored_age_flag,
        ),
        membership_terms=[],
    )


def test_public_activity_browsing_does_not_require_an_account():
    assert has_capability(None, Capability.PUBLIC_ACTIVITY_READ)


@pytest.mark.parametrize("status,allowed", [
    ("active", True), ("inactive", True), ("expired", True),
    ("pending", False), ("rejected", False),
])
def test_profile_management_preserves_privacy_choices_after_expiry(status, allowed):
    assert has_capability(make_user(status=status), Capability.PROFILE_MANAGEMENT) is allowed
    assert not has_capability(None, Capability.PROFILE_MANAGEMENT)


@pytest.mark.parametrize("capability", [Capability.ACCOUNT_ACCESS, Capability.ACTIVITY_REGISTRATION])
def test_active_members_can_access_account_and_register(capability):
    assert has_capability(make_user(), capability, today=TODAY)


@pytest.mark.parametrize("status", ["pending", "rejected", "inactive", "expired"])
def test_non_active_accounts_cannot_access_account_or_register(status):
    user = make_user(status=status)
    assert not has_capability(user, Capability.ACCOUNT_ACCESS, today=TODAY)
    assert not has_capability(user, Capability.ACTIVITY_REGISTRATION, today=TODAY)


def test_unpaid_or_expired_term_blocks_active_member_capabilities():
    user = make_user()
    term = SimpleNamespace(
        term_number=1,
        status="active",
        expiry_date=date(2026, 12, 1),
        payment_status="unpaid",
    )
    user.membership_terms.append(term)

    assert not has_capability(user, Capability.ACCOUNT_ACCESS, today=TODAY)
    assert not has_capability(user, Capability.ACTIVITY_REGISTRATION, today=TODAY)

    term.payment_status = "paid"
    assert has_capability(user, Capability.ACCOUNT_ACCESS, today=TODAY)
    term.expiry_date = date(2026, 11, 13)
    assert not has_capability(user, Capability.ACCOUNT_ACCESS, today=TODAY)


def test_expired_latest_term_revokes_active_capabilities_even_if_status_is_stale():
    user = make_user(status="active")
    user.membership_terms.append(
        SimpleNamespace(
            term_number=2,
            status="active",
            expiry_date=date(2026, 11, 13),
        )
    )
    user.membership_terms.append(
        SimpleNamespace(
            term_number=1,
            status="active",
            expiry_date=date(2025, 11, 14),
        )
    )

    assert not has_capability(user, Capability.ACTIVITY_REGISTRATION, today=TODAY)
    assert not has_capability(user, Capability.ADULT_DIRECTORY, today=TODAY)
    assert has_capability(
        user,
        Capability.CONVERSATION_READ,
        today=TODAY,
        conversation_participant_ids={1, 2},
    )


def test_adult_capabilities_use_date_of_birth_not_membership_label_or_age_flag():
    adult = make_user(
        birth_date=date(1990, 1, 1),
        membership_type="youth",
        stored_age_flag=False,
    )
    youth = make_user(
        user_id=2,
        birth_date=date(2010, 1, 1),
        membership_type="adult",
        stored_age_flag=True,
    )

    assert is_adult_member(adult, TODAY)
    assert has_capability(adult, Capability.ACTIVITY_PROPOSAL, today=TODAY)
    assert has_capability(adult, Capability.ADULT_DIRECTORY, today=TODAY)
    assert not is_adult_member(youth, TODAY)
    assert not has_capability(youth, Capability.ADULT_DIRECTORY, today=TODAY)
    assert not has_capability(youth, Capability.ADULT_MESSAGING, today=TODAY)


def test_missing_birth_date_details_fail_closed_for_adult_capabilities():
    user = SimpleNamespace(id=1, status="active", is_admin=False, membership_details=None)
    assert not has_capability(user, Capability.ACTIVITY_PROPOSAL, today=TODAY)
    assert not has_capability(user, Capability.ADULT_DIRECTORY, today=TODAY)


def test_admin_review_requires_an_active_administrator():
    active_admin = make_user(is_admin=True)
    expired_admin = make_user(status="expired", is_admin=True)
    ordinary_member = make_user(is_admin=False)

    assert has_capability(active_admin, Capability.ADMIN_REVIEW, today=TODAY)
    assert not has_capability(expired_admin, Capability.ADMIN_REVIEW, today=TODAY)
    assert not has_capability(ordinary_member, Capability.ADMIN_REVIEW, today=TODAY)


def test_activity_management_is_scoped_to_owner_or_approved_coorganizer():
    owner = make_user(user_id=10)
    coorganizer = make_user(user_id=11)
    unrelated_member = make_user(user_id=12)

    assert has_capability(owner, Capability.ACTIVITY_MANAGEMENT, activity_owner_id=10)
    assert has_capability(
        coorganizer,
        Capability.ACTIVITY_MANAGEMENT,
        activity_owner_id=10,
        approved_coorganizer_ids={11},
    )
    assert not has_capability(
        unrelated_member,
        Capability.ACTIVITY_MANAGEMENT,
        activity_owner_id=10,
        approved_coorganizer_ids={11},
    )
    assert not has_capability(
        make_user(user_id=10, status="expired"),
        Capability.ACTIVITY_MANAGEMENT,
        activity_owner_id=10,
    )


def test_expired_adult_can_read_participating_conversation_but_not_send():
    expired_adult = make_user(user_id=20, status="expired")
    participants = {20, 21}

    assert has_capability(
        expired_adult,
        Capability.CONVERSATION_READ,
        today=TODAY,
        conversation_participant_ids=participants,
    )
    assert not has_capability(
        expired_adult,
        Capability.MESSAGE_SEND,
        today=TODAY,
        conversation_participant_ids=participants,
        connection_accepted=True,
    )
    assert not has_capability(
        make_user(user_id=22, status="expired"),
        Capability.CONVERSATION_READ,
        today=TODAY,
        conversation_participant_ids=participants,
    )


def test_sending_requires_active_adult_accepted_unblocked_participant():
    adult = make_user(user_id=30)
    kwargs = {
        "today": TODAY,
        "conversation_participant_ids": {30, 31},
        "connection_accepted": True,
    }

    assert has_capability(adult, Capability.MESSAGE_SEND, **kwargs)
    assert not has_capability(adult, Capability.MESSAGE_SEND, **(kwargs | {"blocked": True}))
    assert not has_capability(adult, Capability.MESSAGE_SEND, **(kwargs | {"connection_accepted": False}))
    assert not has_capability(make_user(user_id=32), Capability.MESSAGE_SEND, **kwargs)


def test_adult_birth_date_cutoff_matches_age_calculation_at_leap_boundary():
    assert adult_birth_date_cutoff(date(2027, 2, 28)) == date(2008, 2, 28)
    assert not is_adult_member(
        make_user(birth_date=date(2008, 2, 29)), date(2027, 2, 28)
    )
    assert is_adult_member(
        make_user(birth_date=date(2008, 2, 28)), date(2027, 2, 28)
    )
