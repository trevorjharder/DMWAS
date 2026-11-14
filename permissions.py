"""Central capability policy for Community Hub access."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date
from enum import Enum
from typing import Protocol


class Capability(str, Enum):
    """Actions whose access rules are evaluated by the central policy."""

    PUBLIC_ACTIVITY_READ = "public_activity_read"
    ACCOUNT_ACCESS = "account_access"
    PROFILE_MANAGEMENT = "profile_management"
    RENEWAL_REQUEST = "renewal_request"
    ACTIVITY_REGISTRATION = "activity_registration"
    ACTIVITY_PROPOSAL = "activity_proposal"
    ADULT_DIRECTORY = "adult_directory"
    ADULT_MESSAGING = "adult_messaging"
    ADMIN_REVIEW = "admin_review"
    ACTIVITY_MANAGEMENT = "activity_management"
    CONVERSATION_READ = "conversation_read"
    MESSAGE_SEND = "message_send"


class MembershipDetailsLike(Protocol):
    date_of_birth: date


class MembershipTermLike(Protocol):
    term_number: int
    status: str
    expiry_date: date
    payment_status: str


class UserLike(Protocol):
    id: int | None
    status: str
    is_admin: bool
    membership_details: MembershipDetailsLike | None
    membership_terms: Sequence[MembershipTermLike]


def _latest_term(user: UserLike) -> MembershipTermLike | None:
    terms = getattr(user, "membership_terms", ())
    if not terms:
        return None
    return max(terms, key=lambda term: term.term_number)


def is_active_member(user: UserLike | None, today: date | None = None) -> bool:
    """Require approval, an in-date term, and recorded payment when a term exists."""
    if user is None or user.status != "active":
        return False
    term = _latest_term(user)
    if term is None:
        # Preserve compatibility for older active accounts with no term history.
        return True
    return (
        term.status == "active"
        and term.expiry_date >= (today or date.today())
        and term.payment_status in {"paid", "waived"}
    )


def is_adult_member(user: UserLike | None, today: date | None = None) -> bool:
    """Use recorded date of birth, never a display name or stored age flag."""
    if not is_active_member(user, today) or user is None or user.membership_details is None:
        return False

    return _is_19_or_older(user.membership_details.date_of_birth, today)


def adult_birth_date_cutoff(today: date | None = None) -> date:
    """Return the latest birth date that is 19 or older on ``today``."""
    current_date = today or date.today()
    try:
        return current_date.replace(year=current_date.year - 19)
    except ValueError:
        # Feb 29 birthdays reach the threshold on March 1 in non-leap years,
        # consistent with the application's existing age calculation.
        return current_date.replace(year=current_date.year - 19, day=28)


def has_capability(
    user: UserLike | None,
    capability: Capability,
    *,
    today: date | None = None,
    activity_owner_id: int | None = None,
    approved_coorganizer_ids: Collection[int] = (),
    conversation_participant_ids: Collection[int] = (),
    connection_accepted: bool = False,
    blocked: bool = False,
) -> bool:
    """Evaluate Hub permissions from current account and membership facts.

    A recorded active term must be paid or waived and in date. Legacy active
    accounts without term history retain the existing status-based behavior.
    """
    if capability is Capability.PUBLIC_ACTIVITY_READ:
        return True
    if user is None:
        return False

    if capability is Capability.PROFILE_MANAGEMENT:
        return user.status in {"active", "inactive", "expired"}

    if capability is Capability.RENEWAL_REQUEST:
        return (
            not user.is_admin
            and user.status in {"inactive", "expired"}
            and _latest_term(user) is not None
        )

    active_member = is_active_member(user, today)
    active_adult = is_adult_member(user, today)
    user_id = user.id

    if capability in {Capability.ACCOUNT_ACCESS, Capability.ACTIVITY_REGISTRATION}:
        return active_member
    if capability in {
        Capability.ACTIVITY_PROPOSAL,
        Capability.ADULT_DIRECTORY,
        Capability.ADULT_MESSAGING,
    }:
        return active_adult
    if capability is Capability.ADMIN_REVIEW:
        return active_member and user.is_admin
    if capability is Capability.ACTIVITY_MANAGEMENT:
        return (
            active_member
            and user_id is not None
            and (
                user_id == activity_owner_id
                or user_id in approved_coorganizer_ids
            )
        )
    if capability is Capability.CONVERSATION_READ:
        return (
            user.status in {"active", "inactive", "expired"}
            and is_adult_member_for_history(user, today)
            and user_id is not None
            and user_id in conversation_participant_ids
        )
    if capability is Capability.MESSAGE_SEND:
        return (
            active_adult
            and user_id is not None
            and user_id in conversation_participant_ids
            and connection_accepted
            and not blocked
        )
    return False


def is_adult_member_for_history(user: UserLike, today: date | None = None) -> bool:
    """Check adult eligibility while allowing an expired account read-only access."""
    if user.membership_details is None:
        return False
    return _is_19_or_older(user.membership_details.date_of_birth, today)


def _is_19_or_older(birth_date: date, today: date | None = None) -> bool:
    current_date = today or date.today()
    age = current_date.year - birth_date.year - (
        (current_date.month, current_date.day) < (birth_date.month, birth_date.day)
    )
    return age >= 19
