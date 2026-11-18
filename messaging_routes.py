"""Member networking and restricted moderator interfaces."""
from datetime import timedelta
from urllib.error import URLError

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import or_, update

from extensions import db
from messaging_models import (
    MessageDigest, MessageEvidence, MessagePair, MessageReport, MessageReportEvent,
    MessagingPreference, MessagingQuota, PrivateMessage, utc_now,
)
from messaging_service import (
    active_network_member, can_send, create_connection, finish_digest, history_access, insert_for,
    is_moderator, lock_pair, participating, prepare_digest, prune_network, unread_counts,
    unread_for_digest,
)
from rate_limits import account_quota_retry_after


REPORT_REASONS = {
    "harassment": "Harassment or unwanted contact", "spam": "Spam",
    "safety": "Safety concern", "other": "Other concern",
}
PAGE_SIZE = 25


def bounded_page(query):
    page = request.args.get("page", 1, type=int)
    if not 1 <= page <= 10000:
        abort(400)
    rows = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1).all()
    return {"rows": rows[:PAGE_SIZE], "page": page, "has_next": len(rows) > PAGE_SIZE}


def form_text(field, max_length, *, required=False):
    value = request.form.get(field, "").strip()
    if len(value) > max_length or "\x00" in value or (required and not value):
        abort(400, description=f"Enter valid text of up to {max_length} characters.")
    return value


def register_messaging(app, *, User, CommunityProfile, profile_discoverable, send_email):
    hub = Blueprint("messaging", __name__)

    def policy():
        return current_app.config["MESSAGING_POLICY"]

    @hub.before_request
    def enabled():
        if not current_app.config["COMMUNITY_MESSAGING_ENABLED"]:
            abort(404)

    def reader(*, active=False):
        user = g.current_user
        if not user:
            return None
        if not (active_network_member(user) if active else history_access(user)):
            abort(403)
        return user

    def sign_in():
        return redirect(url_for("login", next=request.path))

    def quota(user_id, bucket):
        allowance, period = {
            "requests": (current_app.config["MESSAGING_REQUESTS_PER_DAY"], 86400),
            "reports": (current_app.config["MESSAGING_REPORTS_PER_DAY"], 86400),
            "messages": (current_app.config["MESSAGING_MESSAGES_PER_MINUTE"], 60),
        }[bucket]
        wait = account_quota_retry_after(
            MessagingQuota, user_id, allowance, period_seconds=period, bucket=bucket)
        if wait is not None:
            return render_template("messaging/rate_limit.html"), 429, {"Retry-After": str(wait)}

    def find_pair(pair_id, actor, *, locking=False, history=False):
        if not 0 < pair_id <= 2147483647:
            abort(404)
        pair = lock_pair(pair_id, actor.id) if locking else MessagePair.query.filter(
            MessagePair.id == pair_id, participating(actor.id)).first()
        if not pair or (history and pair.accepted_at is None):
            abort(404)
        return pair

    def peer_label(peer_id):
        if peer_id is None:
            return "Former member"
        return db.session.query(CommunityProfile.display_name).filter_by(
            user_id=peer_id).scalar() or "Member"

    def add_pair_labels(context, actor_id):
        peers = {pair.peer_id(actor_id) for pair in context["rows"]} - {None}
        profiles = dict(db.session.query(CommunityProfile.user_id, CommunityProfile.display_name)
                        .filter(CommunityProfile.user_id.in_(peers)).all())
        context["names"] = {
            pair.id: profiles.get(pair.peer_id(actor_id), "Member") if pair.peer_id(actor_id)
            else "Former member" for pair in context["rows"]
        }

    @hub.get("/member/connections")
    def connections():
        actor = reader(active=True)
        if not actor:
            return sign_in()
        status = request.args.get("status", "active")
        if status == "blocked":
            state = or_(db.and_(MessagePair.low_id == actor.id, MessagePair.low_blocked.is_(True)),
                        db.and_(MessagePair.high_id == actor.id, MessagePair.high_blocked.is_(True)))
        elif status == "active":
            state = MessagePair.status.in_(("pending", "accepted"))
        else:
            abort(400)
        context = bounded_page(MessagePair.query.filter(participating(actor.id), state).order_by(
            MessagePair.updated_at.desc(), MessagePair.id.desc()))
        add_pair_labels(context, actor.id)
        return render_template("messaging/connections.html", title="Connections", status=status,
                               **context)

    @hub.post("/member/connections/request/<int:user_id>")
    def request_connection(user_id):
        actor = reader(active=True)
        if not actor:
            return sign_in()
        if user_id == actor.id or not 0 < user_id <= 2147483647:
            abort(404)
        limited = quota(actor.id, "requests")
        if limited:
            return limited
        if not db.session.query(CommunityProfile.user_id).filter_by(user_id=actor.id).first():
            flash("Choose a community profile display name before connecting.", "info")
            return redirect(url_for("member_profile"), code=303)
        if not profile_discoverable(user_id):
            abort(404)
        created = create_connection(actor.id, user_id)
        db.session.commit()
        if created is None:
            flash("A new request is unavailable for this connection.", "info")
        else:
            flash("Connection request sent.", "success")
        return redirect(url_for(".connections"), code=303)

    @hub.post("/member/connections/<int:pair_id>/<action>")
    def change_connection(pair_id, action):
        actor = reader(active=action not in {"block", "unblock"})
        if not actor:
            return sign_in()
        pair = find_pair(pair_id, actor, locking=True)
        actor_low = pair.low_id == actor.id
        block_field = "low_blocked" if actor_low else "high_blocked"
        if action in {"block", "unblock"}:
            setattr(pair, block_field, action == "block")
            if action == "block":
                pair.status = "disconnected"
        elif action in {"accept", "decline", "withdraw"}:
            outgoing = pair.requested_by_low == actor_low
            if pair.status != "pending" or pair.blocked or outgoing != (action == "withdraw"):
                abort(409, description="The connection request is unavailable.")
            if action == "accept":
                if not active_network_member(db.session.get(User, pair.peer_id(actor.id))):
                    abort(409, description="The connection request is unavailable.")
                pair.status = "accepted"
                pair.accepted_at = utc_now()
            else:
                pair.status = "declined" if action == "decline" else "disconnected"
        elif action == "disconnect":
            if pair.status != "accepted":
                abort(409)
            pair.status = "disconnected"
        else:
            abort(404)
        pair.updated_at = utc_now()
        db.session.commit()
        target = ".connections" if active_network_member(actor) else ".inbox"
        flash("Connection updated.", "success")
        return redirect(url_for(target), code=303)

    @hub.get("/member/messages")
    def inbox():
        actor = reader()
        if not actor:
            return sign_in()
        context = bounded_page(MessagePair.query.filter(
            participating(actor.id), MessagePair.accepted_at.is_not(None),
        ).order_by(MessagePair.updated_at.desc(), MessagePair.id.desc()))
        add_pair_labels(context, actor.id)
        counts = unread_counts(actor.id, [pair.id for pair in context["rows"]])
        return render_template("messaging/inbox.html", title="Messages", unread=counts,
                               network_active=active_network_member(actor), **context)

    def message_page(pair):
        before = request.args.get("before", type=int)
        items = db.session.query(PrivateMessage).filter_by(pair_id=pair.id)
        if before is not None:
            if not 0 < before <= 2147483647:
                abort(404)
            items = items.filter(PrivateMessage.id < before)
        rows = items.order_by(PrivateMessage.id.desc()).limit(PAGE_SIZE + 1).all()
        latest = rows[0].id if rows else None
        older = rows[PAGE_SIZE - 1].id if len(rows) > PAGE_SIZE else None
        return list(reversed(rows[:PAGE_SIZE])), latest, older

    @hub.get("/member/messages/<int:pair_id>")
    def thread(pair_id):
        actor = reader()
        if not actor:
            return sign_in()
        pair = find_pair(pair_id, actor, history=True)
        peer_id = pair.peer_id(actor.id)
        peer = db.session.get(User, peer_id) if peer_id else None
        rows, latest, older = message_page(pair)
        low = pair.low_id == actor.id
        through = pair.low_read_through if low else pair.high_read_through
        return render_template(
            "messaging/thread.html", title=peer_label(peer_id), pair=pair, items=rows,
            can_send=can_send(actor, peer, pair), low=low, older=older,
            read_through=latest if any(item.id > through and item.sent_by_low != low for item in rows) else None,
            blocked_here=pair.low_blocked if low else pair.high_blocked,
        )

    @hub.post("/member/messages/<int:pair_id>/send")
    def send(pair_id):
        actor = reader(active=True)
        if not actor:
            return sign_in()
        limited = quota(actor.id, "messages")
        if limited:
            return limited
        if request.files:
            abort(400, description="Enter a text message.")
        body = form_text("body", 2000, required=True)
        pair = find_pair(pair_id, actor, locking=True, history=True)
        peer_id = pair.peer_id(actor.id)
        peer = db.session.get(User, peer_id) if peer_id else None
        if not can_send(actor, peer, pair):
            abort(403, description="This conversation is read-only.")
        db.session.add(PrivateMessage(pair_id=pair.id, sent_by_low=pair.low_id == actor.id, body=body))
        pair.updated_at = utc_now()
        db.session.commit()
        return redirect(url_for(".thread", pair_id=pair.id), code=303)

    @hub.post("/member/messages/<int:pair_id>/read")
    def mark_read(pair_id):
        actor = reader()
        if not actor:
            return sign_in()
        pair = find_pair(pair_id, actor, locking=True, history=True)
        through = request.form.get("through", type=int)
        low = pair.low_id == actor.id
        field = "low_read_through" if low else "high_read_through"
        if (through is not None and 0 < through <= 2147483647
                and db.session.query(PrivateMessage.id).filter_by(pair_id=pair.id, id=through).first()):
            setattr(pair, field, max(through, getattr(pair, field)))
            db.session.commit()
        return redirect(url_for(".thread", pair_id=pair.id), code=303)

    @hub.route("/member/messages/preferences", methods=["GET", "POST"])
    def preferences():
        actor = reader(active=True)
        if not actor:
            return sign_in()
        if request.method == "POST":
            enabled_value = request.form.get("digest") == "yes"
            statement = insert_for(MessagingPreference).values(user_id=actor.id, digest_enabled=enabled_value)
            db.session.execute(statement.on_conflict_do_update(
                index_elements=["user_id"], set_={"digest_enabled": enabled_value},
            ))
            db.session.commit()
            flash("Message preferences saved.", "success")
            return redirect(url_for(".preferences"), code=303)
        saved = db.session.get(MessagingPreference, actor.id)
        return render_template("messaging/preferences.html", title="Message preferences",
                               digest_enabled=bool(saved and saved.digest_enabled))

    @hub.route("/member/messages/<int:pair_id>/report", methods=["GET", "POST"])
    def report(pair_id):
        actor = reader()
        if not actor:
            return sign_in()
        if request.method == "POST":
            limited = quota(actor.id, "reports")
            if limited:
                return limited
        pair = find_pair(pair_id, actor, locking=request.method == "POST", history=True)
        if request.method == "POST":
            submit_report(actor, pair)
            flash("Report sent to the moderation team.", "success")
            return redirect(url_for(".thread", pair_id=pair.id), code=303)
        rows, _, older = message_page(pair)
        return render_template("messaging/report.html", title="Report conversation", pair=pair,
                               items=rows, older=older, reasons=REPORT_REASONS,
                               low=pair.low_id == actor.id)

    def submit_report(actor, pair):
        selected = request.form.getlist("evidence")
        reason = request.form.get("reason")
        note = form_text("note", 800)
        if (reason not in REPORT_REASONS or not 1 <= len(selected) <= 5
                or any(not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 10
                       or not 0 < int(value) <= 2147483647 for value in selected)):
            abort(400, description="Choose a reason and one to five messages from this conversation.")
        ids = set(map(int, selected))
        items = PrivateMessage.query.filter_by(pair_id=pair.id).filter(PrivateMessage.id.in_(ids)).all()
        if len(items) != len(ids):
            abort(400, description="Select evidence from this conversation.")
        if MessageReport.query.filter_by(pair_id=pair.id, reporter_id=actor.id, status="open").first():
            abort(409, description="There is already an open report from you for this conversation.")
        report = MessageReport(pair_id=pair.id, reporter_id=actor.id, subject_id=pair.peer_id(actor.id),
                               reason=reason, note=note or None)
        db.session.add(report)
        db.session.flush()
        low = pair.low_id == actor.id
        db.session.add_all([
            MessageEvidence(report_id=report.id, original_message_id=item.id, body=item.body,
                            from_reporter=item.sent_by_low == low, sent_at=item.created_at)
            for item in items
        ])
        db.session.add(MessageReportEvent(report_id=report.id, actor_id=actor.id, action="submitted"))
        db.session.commit()

    def moderator():
        user = g.current_user
        if not user:
            return None
        if not is_moderator(user, policy()):
            abort(403)
        return user

    def report_scope(user):
        return MessageReport.query.filter(
            or_(MessageReport.reporter_id.is_(None), MessageReport.reporter_id != user.id),
            or_(MessageReport.subject_id.is_(None), MessageReport.subject_id != user.id),
        )

    @hub.get("/admin/community/message-reports")
    def reports():
        actor = moderator()
        if not actor:
            return sign_in()
        status = request.args.get("status", "open")
        if status not in {"open", "resolved", "dismissed"}:
            abort(400)
        return render_template("messaging/report_queue.html", title="Message reports", status=status,
                               reasons=REPORT_REASONS, **bounded_page(report_scope(actor).filter_by(
                                   status=status).order_by(MessageReport.id.desc())))

    @hub.route("/admin/community/message-reports/<int:report_id>", methods=["GET", "POST"])
    def review_report(report_id):
        actor = moderator()
        if not actor:
            return sign_in()
        if request.method == "POST":
            # Lock only the submitted case. Moderator routes never query the conversation.
            scope = report_scope(actor).filter_by(id=report_id)
            result = db.session.execute(update(MessageReport).where(
                MessageReport.id == scope.with_entities(MessageReport.id).scalar_subquery(),
            ).values(status=MessageReport.status).returning(MessageReport.id)).scalar_one_or_none()
            if result is None:
                abort(404)
        report = report_scope(actor).filter_by(id=report_id).first_or_404()
        evidence = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "viewed":
                evidence = MessageEvidence.query.filter_by(report_id=report_id).order_by(
                    MessageEvidence.original_message_id).all()
                note = None
            elif action in {"resolved", "dismissed"} and report.status == "open":
                note = form_text("note", 800, required=True)
                report.status = action
                report.closed_at = utc_now()
            else:
                abort(409)
            db.session.add(MessageReportEvent(report_id=report_id, actor_id=actor.id, action=action, note=note))
            db.session.commit()
            if action != "viewed":
                flash("Report review recorded.", "success")
                return redirect(url_for(".reports"), code=303)
        events = MessageReportEvent.query.filter_by(report_id=report_id).order_by(MessageReportEvent.id).all()
        return render_template("messaging/review_report.html", title=f"Report {report.id}", report=report,
                               event_log=events, evidence=evidence, reasons=REPORT_REASONS)

    @app.cli.command("send-community-message-digests")
    def send_digests():
        """Deliver opt-in unread counts for current eligible members."""
        if not current_app.config["COMMUNITY_MESSAGING_ENABLED"]:
            click.echo("Community messaging is disabled.")
            return
        after, sent, failures = 0, 0, 0
        while True:
            ids = db.session.execute(db.select(MessagingPreference.user_id).where(
                MessagingPreference.user_id > after, MessagingPreference.digest_enabled.is_(True),
            ).order_by(MessagingPreference.user_id).limit(100)).scalars().all()
            db.session.commit()
            if not ids:
                break
            for user_id in ids:
                claim = utc_now()
                delivery = prepare_digest(User, user_id, policy(), claim)
                db.session.commit()
                if delivery is None:
                    continue
                address, through, count = delivery
                preference = db.session.get(MessagingPreference, user_id, populate_existing=True)
                user = db.session.get(User, user_id, populate_existing=True)
                eligible = bool(preference and preference.digest_enabled and active_network_member(user)
                                and unread_for_digest(User, user_id)[0])
                db.session.commit()
                if not eligible:
                    finish_digest(user_id, through, claim, completed_at=utc_now())
                    continue
                try:
                    delivered = send_email(
                        recipient=address, subject="Unread Community Hub messages",
                        text=f"You have {count} unread Community Hub message{'s' if count != 1 else ''}. "
                             "Sign in to the DMWAS Community Hub to view your messages or change reminders.",
                        idempotency_key=f"dmwas-message-digest/{user_id}/{through}",
                    )
                except (URLError, TimeoutError):
                    delivered = False
                finish_digest(user_id, through, claim, completed_at=utc_now() if delivered else None)
                sent += int(delivered)
                failures += int(not delivered)
            after = ids[-1]
        click.echo(f"Delivered {sent} unread digests. {failures} deliveries remain eligible for retry.")

    @app.cli.command("prune-community-messaging")
    @click.option("--apply", is_flag=True, help="Apply the approved retention settings.")
    def prune(apply):
        """Preview or apply the messaging retention policy."""
        if apply and not current_app.config["COMMUNITY_MESSAGING_ENABLED"]:
            raise click.ClickException("Enable the approved messaging configuration before pruning.")
        messages, reports = prune_network(policy(), utc_now(), apply=apply)
        verb = "Removed" if apply else "Would remove"
        click.echo(f"{verb} {messages} messages and {reports} closed reports.")

    app.register_blueprint(hub)
