"""Transactional connection state, unread calculations and retention."""
from dataclasses import dataclass
from datetime import timedelta, timezone

from sqlalchemy import and_, case, exists, func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import selectinload

from extensions import db
from messaging_models import (
    MessageDigest, MessageEvidence, MessagePair, MessageReport, MessageReportEvent,
    MessagingPreference, MessagingQuota, PrivateMessage, utc_now,
)
from permissions import Capability, has_capability


@dataclass(frozen=True)
class MessagingPolicy:
    digest_interval_hours: int = 24
    message_days: int = 180
    closed_report_days: int = 365
    moderator_ids: frozenset[int] = frozenset()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def history_access(user):
    return user is not None and has_capability(
        user, Capability.CONVERSATION_READ, conversation_participant_ids={user.id},
    )


def active_network_member(user):
    return has_capability(user, Capability.ADULT_MESSAGING)


def is_moderator(user, policy):
    return (user is not None and user.id in policy.moderator_ids
            and has_capability(user, Capability.ADMIN_REVIEW))


def participating(user_id):
    return or_(MessagePair.low_id == user_id, MessagePair.high_id == user_id)


def blocked_with(viewer_id, other_id):
    return exists().where(
        or_(and_(MessagePair.low_id == viewer_id, MessagePair.high_id == other_id),
            and_(MessagePair.high_id == viewer_id, MessagePair.low_id == other_id)),
        or_(MessagePair.low_blocked.is_(True), MessagePair.high_blocked.is_(True)),
    )


def insert_for(model):
    return (pg_insert if db.engine.dialect.name == "postgresql" else sqlite_insert)(model)


def create_connection(sender_id, recipient_id):
    low, high = sorted((sender_id, recipient_id))
    statement = insert_for(MessagePair).values(
        low_id=low, high_id=high, requested_by_low=low == sender_id, status="pending",
    ).on_conflict_do_nothing(index_elements=["low_id", "high_id"]).returning(MessagePair.id)
    return db.session.execute(statement).scalar_one_or_none()


def lock_pair(pair_id, user_id=None):
    statement = update(MessagePair).where(MessagePair.id == pair_id)
    if user_id is not None:
        statement = statement.where(participating(user_id))
    result = db.session.execute(statement.values(updated_at=MessagePair.updated_at)
                                .returning(MessagePair.id).execution_options(synchronize_session=False))
    if result.scalar_one_or_none() is None:
        return None
    return db.session.get(MessagePair, pair_id, populate_existing=True)


def can_send(user, peer, pair):
    return active_network_member(peer) and has_capability(
        user, Capability.MESSAGE_SEND, conversation_participant_ids={pair.low_id, pair.high_id},
        connection_accepted=pair.status == "accepted", blocked=pair.blocked,
    )


def unread_filter(user_id):
    return and_(
        PrivateMessage.sent_by_low != (MessagePair.low_id == user_id),
        PrivateMessage.id > case(
            (MessagePair.low_id == user_id, MessagePair.low_read_through),
            else_=MessagePair.high_read_through),
    )


def unread_counts(user_id, pair_ids):
    if not pair_ids:
        return {}
    return dict(db.session.query(PrivateMessage.pair_id, func.count(PrivateMessage.id))
                .join(MessagePair, MessagePair.id == PrivateMessage.pair_id)
                .filter(PrivateMessage.pair_id.in_(pair_ids), participating(user_id),
                        unread_filter(user_id)).group_by(PrivateMessage.pair_id).all())


def unread_for_digest(User, user_id):
    pairs = MessagePair.query.filter(
        participating(user_id), MessagePair.status == "accepted",
        MessagePair.low_blocked.is_(False), MessagePair.high_blocked.is_(False),
    ).all()
    peer_ids = {pair.peer_id(user_id) for pair in pairs} - {None}
    peers = User.query.filter(User.id.in_(peer_ids)).options(
        selectinload(User.membership_details), selectinload(User.membership_terms),
    )
    eligible = {peer.id for peer in peers if active_network_member(peer)}
    pair_ids = [pair.id for pair in pairs if pair.peer_id(user_id) in eligible]
    return db.session.query(func.count(PrivateMessage.id), func.max(PrivateMessage.id)).join(
        MessagePair, MessagePair.id == PrivateMessage.pair_id,
    ).filter(PrivateMessage.pair_id.in_(pair_ids), participating(user_id),
             unread_filter(user_id)).one()


def prepare_digest(User, user_id, policy, now):
    """Claim one delivery without holding a transaction open during email."""
    enabled = db.session.execute(update(MessagingPreference).where(
        MessagingPreference.user_id == user_id, MessagingPreference.digest_enabled.is_(True),
    ).values(digest_enabled=MessagingPreference.digest_enabled).returning(
        MessagingPreference.user_id,
    )).scalar_one_or_none()
    user = db.session.get(User, user_id, populate_existing=True) if enabled else None
    if not active_network_member(user):
        return None
    row = db.session.get(MessageDigest, user_id, populate_existing=True)
    pending = row is not None and row.completed_at is None
    if pending and now - utc(row.prepared_at) >= timedelta(hours=23):
        # A crashed worker may have delivered this. Never replay beyond the provider's key window.
        row.completed_at = now
        row.claimed_at = None
        return None
    if pending and row.claimed_at and now - utc(row.claimed_at) < timedelta(minutes=5):
        return None
    if row and row.completed_at and now - utc(row.completed_at) < timedelta(hours=policy.digest_interval_hours):
        return None
    count, through = unread_for_digest(User, user_id)
    if not count:
        if pending:
            row.completed_at = now
            row.claimed_at = None
        return None
    if not pending:
        if row and through <= row.through_id:
            return None
        if row is None:
            row = MessageDigest(user_id=user_id)
            db.session.add(row)
        row.through_id = through
        row.unread_count = count
        row.prepared_at = now
        row.completed_at = None
    row.claimed_at = now
    return user.email, row.through_id, row.unread_count


def finish_digest(user_id, through, claim, completed_at=None):
    db.session.execute(update(MessageDigest).where(
        MessageDigest.user_id == user_id, MessageDigest.through_id == through,
        MessageDigest.claimed_at == claim, MessageDigest.completed_at.is_(None),
    ).values(claimed_at=None, completed_at=completed_at))
    db.session.commit()


def forget_network_member(user_id):
    """Erase authored chat and identifying links. Restricted evidence has its own policy."""
    ids = db.session.execute(db.select(MessagePair.id).where(
        participating(user_id)).order_by(MessagePair.id)).scalars().all()
    for pair_id in ids:
        pair = lock_pair(pair_id, user_id)
        if pair is None:
            continue
        low = pair.low_id == user_id
        PrivateMessage.query.filter_by(pair_id=pair_id, sent_by_low=low).delete(synchronize_session=False)
        if low:
            pair.low_id = None
        else:
            pair.high_id = None
        pair.status = "disconnected"
    for model, field in (
        (MessageReport, MessageReport.reporter_id), (MessageReport, MessageReport.subject_id),
        (MessageReportEvent, MessageReportEvent.actor_id),
    ):
        model.query.filter(field == user_id).update({field: None}, synchronize_session=False)
    for model in (MessagingQuota, MessageDigest, MessagingPreference):
        model.query.filter_by(user_id=user_id).delete(synchronize_session=False)


def prune_network(policy, now, *, apply):
    messages = PrivateMessage.query.filter(
        PrivateMessage.created_at < now - timedelta(days=policy.message_days),
    )
    reports = MessageReport.query.filter(
        MessageReport.status.in_(("resolved", "dismissed")),
        MessageReport.closed_at < now - timedelta(days=policy.closed_report_days),
    )
    if not apply:
        return messages.count(), reports.count()
    old_report_ids = reports.with_entities(MessageReport.id).statement
    for model in (MessageReportEvent, MessageEvidence):
        model.query.filter(model.report_id.in_(old_report_ids)).delete(synchronize_session=False)
    removed = messages.delete(synchronize_session=False), reports.delete(synchronize_session=False)
    db.session.commit()
    return removed
