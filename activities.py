from datetime import date, datetime, timezone
import re

from flask import (
    Blueprint, abort, current_app, flash, g, make_response, redirect, render_template, request,
    url_for,
)
from sqlalchemy import and_, func, or_
from sqlalchemy.orm.exc import StaleDataError

from activity_forms import (
    CATEGORIES, LOCATIONS, ORGANIZER_TEXT_FIELDS, PUBLIC_TEXT_FIELDS, REGISTRATION_MODES,
    TIME_ZONES, read_activity_form, validate_activity,
)
from activity_models import (
    Activity, ActivityActionWindow, ActivityAudit, ActivityCoorganizer, ActivityRevision,
)
from extensions import db
from permissions import Capability, adult_birth_date_cutoff, has_capability
from rate_limits import account_quota_retry_after


PAGE_SIZE = 20
STATUSES = {
    "draft": "Draft", "submitted": "Awaiting review", "changes_requested": "Changes requested",
    "rejected": "Rejected", "approved": "Approved", "unpublished": "Unpublished",
}
MEMBER_TRANSITIONS = {
    "submit": ({"draft", "unpublished"}, "submitted"),
    "withdraw": ({"submitted"}, "draft"),
}
REVIEW_TRANSITIONS = {
    "approve": ({"submitted"}, "approved"),
    "request_changes": ({"submitted"}, "changes_requested"),
    "reject": ({"submitted"}, "rejected"),
    "unpublish": ({"approved"}, "unpublished"),
}
COORGANIZER_TRANSITIONS = {
    "accept": ({"invited"}, "accepted"),
    "decline": ({"invited"}, "declined"),
    "approve": ({"accepted"}, "approved"),
    "revoke": ({"invited", "accepted", "approved"}, "revoked"),
    "leave": ({"accepted", "approved"}, "revoked"),
}
CURRENT_REVISION = and_(
    ActivityRevision.activity_id == Activity.id,
    ActivityRevision.number == Activity.revision_number,
)


def can_propose(user):
    return has_capability(user, Capability.ACTIVITY_PROPOSAL)


def can_review(user):
    return has_capability(user, Capability.ADMIN_REVIEW)


def eligible_organizer(user, classification):
    return can_review(user) if classification == "official" else can_propose(user)


def check_version(activity):
    if request.form.get("lock_version", type=int) != activity.lock_version:
        abort(409, description="This activity changed. Reload the page and try again.")


def apply_transition(action, old_status, transitions):
    if action not in transitions:
        abort(404)
    allowed, new_status = transitions[action]
    if old_status not in allowed:
        abort(409, description="This action is unavailable in the current status.")
    return new_status


def audit(activity, action, previous_status, new_status, *, note=None, subject_id=None):
    db.session.add(ActivityAudit(
        activity_id=activity.id, actor_id=g.current_user.id, subject_id=subject_id,
        revision_number=activity.revision_number, action=action,
        previous_status=previous_status, new_status=new_status, note=note,
    ))


def commit_activity():
    try:
        db.session.commit()
    except StaleDataError:
        db.session.rollback()
        abort(409, description="This activity changed. Reload the page and try again.")


def limit_activity_action():
    retry_after = account_quota_retry_after(
        ActivityActionWindow, g.current_user.id, current_app.config["ACTIVITY_ACTIONS_PER_MINUTE"])
    if retry_after is not None:
        abort(make_response(render_template("activities/rate_limit.html"), 429,
                            {"Retry-After": str(retry_after)}))


def activity_page(template, query, **context):
    page = request.args.get("page", 1, type=int)
    if not 1 <= page <= 10000:
        abort(400)
    rows = query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1).all()
    return render_template(template, rows=rows[:PAGE_SIZE], page=page,
                           has_next=len(rows) > PAGE_SIZE, **context)


def register_activities(app, *, User, MembershipDetails, MembershipTerm, login_required,
                        admin_required):
    hub = Blueprint("activities", __name__)

    @hub.context_processor
    def activity_locations():
        return {"locations": LOCATIONS}

    def public_query():
        member_publication = current_app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"]
        classifications = Activity.classification == "official"
        if member_publication:
            today = date.today()
            latest_term = db.select(func.max(MembershipTerm.term_number)).where(
                MembershipTerm.user_id == User.id
            ).correlate(User).scalar_subquery()
            # Match the central eligibility policy, including legacy active accounts.
            adult_ids = db.select(User.id).where(
                User.status == "active",
                User.membership_details.has(MembershipDetails.date_of_birth <= adult_birth_date_cutoff(today)),
                or_(~User.membership_terms.any(), User.membership_terms.any(and_(
                    MembershipTerm.term_number == latest_term, MembershipTerm.status == "active",
                    MembershipTerm.expiry_date >= today,
                    MembershipTerm.payment_status.in_(("paid", "waived")),
                ))),
            )
            classifications = or_(classifications, and_(
                Activity.classification == "member", Activity.organizer_id.in_(adult_ids)))
        return db.session.query(
            Activity.id, Activity.classification, ActivityRevision.public_details,
        ).join(ActivityRevision, CURRENT_REVISION).filter(
            Activity.status == "approved", Activity.organizer_id.is_not(None),
            ActivityRevision.ends_at > datetime.now(timezone.utc), classifications,
        )

    def member_activity(activity_id, *, owner_only=False):
        user = g.current_user
        invited = ActivityCoorganizer.query.filter_by(user_id=user.id, activity_id=activity_id)
        invited = invited.filter(ActivityCoorganizer.status.in_(("invited", "accepted", "approved")))
        scope = Activity.organizer_id == user.id
        if can_propose(user) and not owner_only:
            scope = or_(scope, invited.exists())
        activity = Activity.query.filter(Activity.id == activity_id, scope).first_or_404()
        if activity.organizer_id == user.id:
            if not eligible_organizer(user, activity.classification):
                abort(403)
            return activity, None
        return activity, invited.first_or_404()

    def render_draft(activity, public, organizer, errors=()):
        return render_template(
            "activities/draft.html", activity=activity, public=public, organizer=organizer,
            errors=errors, categories=CATEGORIES, locations=LOCATIONS, time_zones=TIME_ZONES,
            modes=REGISTRATION_MODES, public_fields=PUBLIC_TEXT_FIELDS,
            organizer_fields=ORGANIZER_TEXT_FIELDS, can_review=can_review(g.current_user),
        )

    def save_draft(activity=None):
        public, organizer = read_activity_form(request.form)
        errors, starts_at, ends_at = validate_activity(public, organizer)
        if activity is None:
            classification = request.form.get("classification", "member")
            if classification not in {"member", "official"}:
                abort(400)
            if not eligible_organizer(g.current_user, classification):
                abort(403)
        if errors:
            return render_draft(activity, public, organizer, errors), 400
        if activity is None:
            activity = Activity(organizer_id=g.current_user.id, classification=classification)
            db.session.add(activity)
            db.session.flush()
            previous_status, action = None, "create_draft"
        else:
            current = db.session.get(ActivityRevision, (activity.id, activity.revision_number))
            if current.public_details == public and current.organizer_details == organizer:
                flash("The draft already contains those details.", "info")
                return redirect(url_for(".member_detail", activity_id=activity.id))
            previous_status, action = activity.status, "revise"
            activity.revision_number += 1
            activity.status = "draft"
        db.session.add(ActivityRevision(
            activity_id=activity.id, number=activity.revision_number, public_details=public,
            organizer_details=organizer, starts_at=starts_at, ends_at=ends_at,
        ))
        audit(activity, action, previous_status, activity.status)
        commit_activity()
        if previous_status == "approved":
            flash("New draft saved. The listing needs approval before it can appear again.", "info")
        else:
            flash("Draft saved.", "success")
        return redirect(url_for(".member_detail", activity_id=activity.id))

    @hub.get("/activities")
    def public_list():
        return activity_page(
            "activities/list.html", public_query().order_by(ActivityRevision.starts_at, Activity.id),
            title="Activities", view="public",
        )

    @hub.get("/activities/<int:activity_id>")
    def public_detail(activity_id):
        listing = public_query().filter(Activity.id == activity_id).first_or_404()
        return render_template("activities/public_detail.html", listing=listing)

    @hub.get("/member/activities")
    @login_required
    def member_list():
        user = g.current_user
        if not (can_propose(user) or can_review(user)):
            abort(403)
        invited = and_(ActivityCoorganizer.user_id == user.id,
                       ActivityCoorganizer.activity_id == Activity.id)
        scope = []
        for classification in ("member", "official"):
            if eligible_organizer(user, classification):
                scope.append(and_(Activity.organizer_id == user.id,
                                  Activity.classification == classification))
        if can_propose(user):
            scope.append(ActivityCoorganizer.status.in_(("invited", "accepted", "approved")))
        query = db.session.query(
            Activity.id, Activity.status, ActivityRevision.public_details, ActivityCoorganizer.status,
        ).join(ActivityRevision, CURRENT_REVISION).outerjoin(
            ActivityCoorganizer, invited).filter(
            Activity.organizer_id.is_not(None), or_(*scope)).order_by(Activity.id.desc())
        return activity_page("activities/list.html", query, title="My activities", view="member",
                             statuses=STATUSES)

    @hub.route("/member/activities/new", methods=["GET", "POST"])
    @login_required
    def new_activity():
        if not (can_propose(g.current_user) or can_review(g.current_user)):
            abort(403)
        if request.method == "POST":
            limit_activity_action()
            return save_draft()
        return render_draft(None, {"time_zone": "America/Vancouver"}, {})

    @hub.get("/member/activities/<int:activity_id>")
    @login_required
    def member_detail(activity_id):
        activity, invitation = member_activity(activity_id)
        revision = db.session.get(ActivityRevision, (activity.id, activity.revision_number))
        if invitation:
            if not activity.organizer_id:
                abort(404)
            organizer = db.session.get(User, activity.organizer_id)
            approved = (activity.status == "approved" and invitation.status == "approved"
                        and eligible_organizer(organizer, activity.classification)
                        and has_capability(g.current_user, Capability.ACTIVITY_MANAGEMENT,
                                           approved_coorganizer_ids={invitation.user_id}))
            return render_template("activities/invitation.html", activity=activity,
                                   title=revision.public_details["title"] or "Untitled activity",
                                   invitation=invitation, revision=revision if approved else None,
                                   organizer_fields=ORGANIZER_TEXT_FIELDS, modes=REGISTRATION_MODES)
        latest_note = ActivityAudit.query.filter_by(
            activity_id=activity.id, revision_number=activity.revision_number,
        ).filter(ActivityAudit.note.is_not(None)).order_by(ActivityAudit.id.desc()).first()
        return render_template("activities/workspace.html", activity=activity, revision=revision,
                               coorganizers=coorganizers(activity), latest_note=latest_note,
                               statuses=STATUSES, organizer_fields=ORGANIZER_TEXT_FIELDS,
                               modes=REGISTRATION_MODES)

    @hub.route("/member/activities/<int:activity_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_activity(activity_id):
        activity, _ = member_activity(activity_id, owner_only=True)
        if activity.status == "submitted":
            abort(409, description="Withdraw the submission before editing it.")
        if request.method == "POST":
            limit_activity_action()
            # Quota accounting commits its own transaction, so recheck the current row.
            activity, _ = member_activity(activity_id, owner_only=True)
            check_version(activity)
            if activity.status == "submitted":
                abort(409, description="This activity changed. Reload the page and try again.")
            return save_draft(activity)
        current = db.session.get(ActivityRevision, (activity.id, activity.revision_number))
        return render_draft(activity, current.public_details, current.organizer_details)

    @hub.post("/member/activities/<int:activity_id>/state/<action>")
    @login_required
    def member_transition(activity_id, action):
        member_activity(activity_id, owner_only=True)
        limit_activity_action()
        activity, _ = member_activity(activity_id, owner_only=True)
        check_version(activity)
        old_status = activity.status
        new_status = apply_transition(action, old_status, MEMBER_TRANSITIONS)
        if action == "submit":
            revision = db.session.get(ActivityRevision, (activity.id, activity.revision_number))
            errors, _, _ = validate_activity(
                revision.public_details, revision.organizer_details, submitting=True)
            if errors:
                for error in errors:
                    flash(error, "error")
                return redirect(url_for(".edit_activity", activity_id=activity_id), code=303)
        activity.status = new_status
        audit(activity, action, old_status, new_status)
        commit_activity()
        flash(STATUSES[new_status] + ".", "success")
        return redirect(url_for(".member_detail", activity_id=activity_id))

    def coorganizers(activity):
        return db.session.query(ActivityCoorganizer, User).join(
            User, User.id == ActivityCoorganizer.user_id).filter(
            ActivityCoorganizer.activity_id == activity.id).order_by(User.id).all()

    @hub.post("/member/activities/<int:activity_id>/coorganizers")
    @login_required
    def invite_coorganizer(activity_id):
        member_activity(activity_id, owner_only=True)
        limit_activity_action()
        activity, _ = member_activity(activity_id, owner_only=True)
        check_version(activity)
        number = request.form.get("member_number", "").strip().upper()
        invited = None
        if re.fullmatch(r"DMWAS-[0-9]{6,10}", number):
            member_id = int(number.split("-")[1])
            if 0 < member_id <= 2_147_483_647:
                invited = db.session.get(User, member_id)
        if not can_propose(invited) or invited.id == activity.organizer_id:
            abort(400, description="The invitation could not be sent to that member number.")
        if db.session.get(ActivityCoorganizer, (activity.id, invited.id)):
            abort(409, description="An invitation already exists for that member.")
        activity.touch()
        db.session.add(ActivityCoorganizer(activity_id=activity.id, user_id=invited.id))
        audit(activity, "coorganizer_invite", None, "invited", subject_id=invited.id)
        commit_activity()
        flash("Invitation added to the member’s activity inbox.", "success")
        return redirect(url_for(".member_detail", activity_id=activity_id))

    @hub.post("/member/activities/<int:activity_id>/coorganizers/<int:user_id>/<action>")
    @login_required
    def member_coorganizer_transition(activity_id, user_id, action):
        owner_action = action == "revoke"
        if action not in {"accept", "decline", "leave", "revoke"}:
            abort(404)
        member_activity(activity_id, owner_only=owner_action)
        limit_activity_action()
        activity, invitation = member_activity(activity_id, owner_only=owner_action)
        if not activity.organizer_id:
            abort(404)
        if owner_action:
            invitation = db.get_or_404(ActivityCoorganizer, (activity_id, user_id))
        elif not invitation or invitation.user_id != user_id:
            abort(404)
        change_coorganizer(activity, invitation, action)
        if action in {"decline", "leave"}:
            return redirect(url_for(".member_list"))
        return redirect(url_for(".member_detail", activity_id=activity_id))

    def change_coorganizer(activity, invitation, action):
        check_version(activity)
        previous_status = invitation.status
        invitation.status = apply_transition(action, previous_status, COORGANIZER_TRANSITIONS)
        activity.touch()
        audit(activity, f"coorganizer_{action}", previous_status, invitation.status,
              subject_id=invitation.user_id)
        commit_activity()
        flash("Co-organizer status updated.", "success")

    @hub.get("/admin/community/activities")
    @admin_required
    def admin_list():
        status = request.args.get("status", "submitted")
        if status == "coorganizers":
            filter_by = db.select(ActivityCoorganizer.activity_id).where(
                ActivityCoorganizer.activity_id == Activity.id,
                ActivityCoorganizer.status == "accepted").exists()
        elif status in STATUSES:
            filter_by = Activity.status == status
        else:
            abort(400)
        query = db.session.query(Activity.id, Activity.status, ActivityRevision.public_details).join(
            ActivityRevision, CURRENT_REVISION).filter(filter_by).order_by(Activity.id.desc())
        return activity_page("activities/list.html", query, title="Activity review", view="admin",
                             statuses=STATUSES, status=status)

    @hub.get("/admin/community/activities/<int:activity_id>")
    @admin_required
    def admin_detail(activity_id):
        activity = db.get_or_404(Activity, activity_id)
        number = request.args.get("revision", type=int) if "revision" in request.args else activity.revision_number
        if not number or not 1 <= number <= activity.revision_number:
            abort(404)
        revision = db.get_or_404(ActivityRevision, (activity.id, number))
        organizer = db.session.get(User, activity.organizer_id) if activity.organizer_id else None
        audit_query = ActivityAudit.query.filter_by(activity_id=activity_id).order_by(
            ActivityAudit.id.desc())
        return activity_page(
            "activities/workspace.html", audit_query, activity=activity, revision=revision,
            statuses=STATUSES, organizer_fields=ORGANIZER_TEXT_FIELDS, modes=REGISTRATION_MODES,
            coorganizers=coorganizers(activity), organizer_user=organizer, reviewing=True,
            publication_enabled=current_app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"],
        )

    @hub.post("/admin/community/activities/<int:activity_id>/state/<action>")
    @admin_required
    def review_transition(activity_id, action):
        activity = db.get_or_404(Activity, activity_id)
        check_version(activity)
        previous_status = activity.status
        new_status = apply_transition(action, previous_status, REVIEW_TRANSITIONS)
        note = request.form.get("note", "").strip()
        if len(note) > 1000 or "\x00" in note or (action != "approve" and not note):
            abort(400, description="Enter a reviewer note of up to 1,000 characters.")
        if action == "approve":
            if request.form.get("confirm_public") != "yes":
                abort(400, description="Confirm the public title and summary are suitable to share.")
            organizer = db.session.get(User, activity.organizer_id) if activity.organizer_id else None
            if not eligible_organizer(organizer, activity.classification):
                abort(409, description="The primary organizer is no longer eligible.")
            if activity.classification == "member" and activity.organizer_id == g.current_user.id:
                abort(403, description="Another administrator must review this member proposal.")
            revision = db.session.get(ActivityRevision, (activity_id, activity.revision_number))
            if (not revision.starts_at
                    or revision.starts_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)):
                abort(409, description="The activity start time has passed.")
        activity.status = new_status
        audit(activity, action, previous_status, new_status, note=note or None)
        commit_activity()
        flash(STATUSES[new_status] + ".", "success")
        return redirect(url_for(".admin_detail", activity_id=activity_id))

    @hub.post("/admin/community/activities/<int:activity_id>/coorganizers/<int:user_id>/<action>")
    @admin_required
    def review_coorganizer(activity_id, user_id, action):
        if action not in {"approve", "revoke"}:
            abort(404)
        activity = db.get_or_404(Activity, activity_id)
        invitation = db.get_or_404(ActivityCoorganizer, (activity_id, user_id))
        if action == "approve":
            organizer = db.session.get(User, activity.organizer_id) if activity.organizer_id else None
            if not can_propose(db.session.get(User, user_id)) or not eligible_organizer(
                organizer, activity.classification,
            ):
                abort(409, description="The primary or invited organizer is no longer eligible.")
        change_coorganizer(activity, invitation, action)
        return redirect(url_for(".admin_detail", activity_id=activity_id))

    app.register_blueprint(hub)
