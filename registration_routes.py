"""Member registration, organizer rosters and administrator hours review."""
import re
from zoneinfo import ZoneInfo

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from sqlalchemy import and_, func

from activity_forms import ACTIVITY_TIME_ZONE, ORGANIZER_TEXT_FIELDS, location_label
from activity_models import Activity, ActivityAudit, ActivityCoorganizer, ActivityRevision
from extensions import db
from permissions import Capability, has_capability
from registration_models import ActivityNotice, ActivityRegistration, ActivityVolunteerHours
from registration_service import (
    NOTICE_TEXT, OPEN_STATUSES, STATUS_LABELS, RegistrationService, approved_revision_join,
    as_utc, change_status,
    latest_verified_hours, lock_activity, registration_notice,
)


def note_value(field):
    note = request.form.get(field, "").strip()
    if len(note) > 500 or "\x00" in note:
        abort(400, description="Enter a note of up to 500 characters.")
    return note


def paginated(query):
    page = request.args.get("page", 1, type=int)
    if not 1 <= page <= 10000:
        abort(400)
    rows = query.offset((page - 1) * 25).limit(26).all()
    return {"rows": rows[:25], "page": page, "has_next": len(rows) > 25}


def register_registration_routes(app, *, User, organizer_eligible, login_required,
                                 admin_required, limit_activity_action):
    routes = Blueprint("registrations", __name__)

    @routes.before_request
    def enabled():
        if not current_app.config["ACTIVITY_REGISTRATION_ENABLED"]:
            abort(404)

    @routes.context_processor
    def template_values():
        return {"registration_labels": STATUS_LABELS, "notice_text": NOTICE_TEXT,
                "location_label": location_label, "deadline_text": deadline_text}

    def deadline_text(value):
        return as_utc(value).astimezone(ZoneInfo(ACTIVITY_TIME_ZONE)).strftime("%b %d, %Y at %H:%M %Z")

    def service_for(activity):
        return RegistrationService(
            User=User, activity=activity, policy=current_app.config["ACTIVITY_REGISTRATION_POLICY"],
            organizer_eligible=organizer_eligible,
            member_publication=current_app.config["MEMBER_ACTIVITY_PUBLICATION_ENABLED"],
        )

    def get_service(activity_id, *, locking=False):
        activity = lock_activity(activity_id) if locking else db.session.get(Activity, activity_id)
        if activity is None:
            abort(404)
        return service_for(activity)

    @routes.get("/member/registrations")
    @login_required
    def my_registrations():
        user_id = g.current_user.id
        query = db.session.query(ActivityRegistration, ActivityRevision.public_details).join(
            Activity, Activity.id == ActivityRegistration.activity_id,
        ).outerjoin(ActivityRevision, approved_revision_join()).filter(
            ActivityRegistration.user_id == user_id).order_by(ActivityRegistration.updated_at.desc(),
                                                             ActivityRegistration.id.desc())
        context = paginated(query)
        totals = latest_verified_hours([registration.id for registration, _ in context["rows"]])
        return render_template("activities/my_registrations.html", title="My registrations",
                               verified=totals, **context)

    @routes.get("/member/registrations/notices")
    @login_required
    def notices():
        query = db.session.query(ActivityNotice, ActivityRevision.public_details).join(
            Activity, Activity.id == ActivityNotice.activity_id,
        ).outerjoin(ActivityRevision, approved_revision_join()).filter(
            ActivityNotice.user_id == g.current_user.id).order_by(ActivityNotice.id.desc())
        return render_template("activities/registration_notices.html", title="Activity notices",
                               **paginated(query))

    @routes.route("/activities/<int:activity_id>/register", methods=["GET", "POST"])
    @login_required
    def enroll(activity_id):
        if request.method == "POST":
            limit_activity_action()
        service = get_service(activity_id, locking=request.method == "POST")
        user = g.current_user
        registration = service.registrations().filter_by(user_id=user.id).first()
        if registration is None and (not service.is_live or not service.end or service.now >= service.end):
            abort(404)
        if request.method == "POST":
            service.reconcile()
            action = request.form.get("action", "")
            if action == "register":
                register_member(service, registration, user)
            elif action == "accept":
                if (not registration or registration.status != "offered"
                        or not service.signup_open or not service.eligible(user)):
                    abort(409, description="The waitlist offer is no longer available.")
                change_status(registration, "confirmed", "confirmed", service.now)
            elif action == "cancel":
                if not can_cancel(service, registration):
                    abort(409, description="The cancellation deadline has passed or the registration has closed.")
                change_status(registration, "cancelled", "cancelled", service.now)
                service.reconcile()
            else:
                abort(400)
            db.session.commit()
            return redirect(url_for(".enroll", activity_id=activity_id), code=303)

        private_details = None
        if (registration and registration.status == "confirmed" and service.is_live
                and service.end and service.now < service.end and service.eligible(user)
                and registration.renewal_until is None
                and (service.now < service.signup_closes or service.eligible(user, service.start))):
            private_details = service.revision.organizer_details
        return render_template(
            "activities/register.html", service=service,
            details=service.approved_revision.public_details if service.approved_revision else {},
            registration=registration, eligible=service.eligible(user), private_details=private_details,
            organizer_fields=ORGANIZER_TEXT_FIELDS,
            can_cancel=can_cancel(service, registration),
            available=service.available_seats() if service.signup_open else 0,
            suggested_name=user.community_profile.display_name if user.community_profile else "",
            can_accept=(registration and registration.status == "offered" and service.signup_open
                        and service.eligible(user) and service.now < as_utc(registration.offer_expires_at)),
        )

    def can_cancel(service, registration):
        if (not registration or registration.status not in OPEN_STATUSES or not service.start
                or service.now >= service.start):
            return False
        return registration.status != "confirmed" or service.now < service.cancellation_closes

    def register_member(service, registration, user):
        if registration and registration.status in OPEN_STATUSES:
            return
        if not service.signup_open or not service.eligible(user):
            abort(403, description="Registration requires an eligible active membership and an open activity.")
        if registration and registration.status == "declined":
            abort(409, description="The organizer declined this registration. Contact the Society for help.")
        display_name = request.form.get("display_name", "").strip()
        if not display_name or len(display_name) > 80 or any(char in display_name for char in "\r\n\x00"):
            abort(400, description="Enter a name of up to 80 characters for the organizer.")
        if registration is None:
            registration = ActivityRegistration(activity_id=service.activity.id, user_id=user.id)
        registration.display_name = display_name
        registration.queued_at = None
        if service.revision.organizer_details["registration_mode"] == "immediate":
            service.allocate(registration)
        else:
            change_status(registration, "requested", "requested", service.now)
            service.notify_organizers_of_request()
        db.session.add(registration)

    def roster_service(activity_id, *, locking=False):
        service = get_service(activity_id, locking=locking)
        activity, actor = service.activity, g.current_user
        owner = activity.organizer_id == actor.id and organizer_eligible(actor, activity.classification)
        invitation = db.session.get(ActivityCoorganizer, (activity_id, actor.id))
        assigned = (invitation and invitation.status == "approved" and service.is_live
                    and has_capability(actor, Capability.ACTIVITY_PROPOSAL))
        if not (owner or assigned) or not has_capability(
            actor, Capability.ACTIVITY_MANAGEMENT, activity_owner_id=activity.organizer_id,
            approved_coorganizer_ids={actor.id} if assigned else (),
        ):
            abort(404)
        if not service.start:
            abort(404)
        return service

    @routes.get("/member/activities/<int:activity_id>/roster")
    @login_required
    def roster(activity_id):
        service = roster_service(activity_id)
        registrations = service.registrations().order_by(
            ActivityRegistration.display_name, ActivityRegistration.id).all()
        ids = [row.id for row in registrations]
        verified = latest_verified_hours(ids)
        latest = latest_hours(ids)
        return render_template(
            "activities/roster.html", title="Activity roster", service=service, registrations=registrations,
            verified=verified, latest=latest, total_minutes=sum(verified.values()),
        )

    def latest_hours(ids):
        if not ids:
            return {}
        latest_versions = db.select(
            ActivityVolunteerHours.registration_id,
            func.max(ActivityVolunteerHours.version).label("version"),
        ).where(ActivityVolunteerHours.registration_id.in_(ids)).group_by(
            ActivityVolunteerHours.registration_id).subquery()
        return {row.registration_id: row for row in ActivityVolunteerHours.query.join(
            latest_versions, and_(ActivityVolunteerHours.registration_id == latest_versions.c.registration_id,
                                  ActivityVolunteerHours.version == latest_versions.c.version))}

    @routes.post("/member/activities/<int:activity_id>/roster/<int:registration_id>/<action>")
    @login_required
    def roster_action(activity_id, registration_id, action):
        limit_activity_action()
        service = roster_service(activity_id, locking=True)
        registration = service.registrations().filter_by(id=registration_id).first_or_404()
        service.reconcile()
        if action == "approve":
            if (not service.signup_open or registration.status != "requested"
                    or not service.eligible(db.session.get(User, registration.user_id))):
                abort(409, description="This request is no longer eligible for approval.")
            service.allocate(registration)
        elif action == "decline":
            if registration.status != "requested":
                abort(409)
            change_status(registration, "declined", "declined", service.now)
        elif action == "cancel":
            if registration.status not in OPEN_STATUSES or service.now >= service.start:
                abort(409)
            change_status(registration, "cancelled", "cancelled", service.now)
            service.reconcile()
        elif action == "attendance":
            record_attendance(service, registration)
        elif action == "hours":
            submit_hours(service, registration)
        else:
            abort(404)
        db.session.commit()
        flash("Roster updated.", "success")
        return redirect(url_for(".roster", activity_id=activity_id), code=303)

    def record_attendance(service, registration):
        status = request.form.get("attendance")
        if status not in {"attended", "absent"}:
            abort(400, description="Choose an attendance status.")
        if (service.now < service.start or registration.status != "confirmed"
                or not registration.start_checked):
            abort(409, description="Attendance requires a confirmed participant with an eligible membership at activity start.")
        if registration.attendance == status:
            return
        if status == "absent":
            latest = latest_hours([registration.id]).get(registration.id)
            if (latest and latest.status == "pending"
                    or latest_verified_hours([registration.id]).get(registration.id, 0) > 0):
                abort(409, description="Resolve pending or verified volunteer hours before removing attendance.")
        db.session.add(ActivityAudit(
            activity_id=service.activity.id, actor_id=g.current_user.id, subject_id=registration.user_id,
            revision_number=service.activity.revision_number, action="attendance",
            previous_status=registration.attendance, new_status=status, created_at=service.now,
        ))
        registration.attendance = status
        registration.attendance_at = service.now
        registration.attendance_by_id = g.current_user.id

    def submit_hours(service, registration):
        if (not service.end or service.now < service.end or registration.attendance != "attended"):
            abort(409, description="Volunteer hours can be submitted for attendance after the activity ends.")
        raw = request.form.get("minutes", "")
        if not re.fullmatch(r"[0-9]{1,6}", raw) or not 0 <= int(raw) <= 600000:
            abort(400, description="Enter whole volunteer minutes from 0 to 600,000.")
        minutes = int(raw)
        note = note_value("submission_note")
        latest = latest_hours([registration.id]).get(registration.id)
        if latest and latest.status == "pending":
            abort(409, description="This volunteer-hours submission is awaiting review.")
        if latest and not note:
            abort(400, description="Explain the reason for the correction.")
        if not latest and minutes == 0:
            abort(400, description="Enter a positive number of minutes for the first submission.")
        db.session.add(ActivityVolunteerHours(
            registration_id=registration.id, version=latest.version + 1 if latest else 1,
            minutes=minutes, submission_note=note or None, submitted_by_id=g.current_user.id,
            submitted_at=service.now,
        ))

    @routes.get("/admin/community/volunteer-hours")
    @admin_required
    def hours_review():
        status = request.args.get("status", "pending")
        if status not in {"pending", "verified", "rejected"}:
            abort(400)
        query = db.session.query(ActivityVolunteerHours, ActivityRegistration,
                                 ActivityRevision.public_details).join(
            ActivityRegistration, ActivityRegistration.id == ActivityVolunteerHours.registration_id,
        ).join(Activity, Activity.id == ActivityRegistration.activity_id).outerjoin(
            ActivityRevision, approved_revision_join(),
        ).filter(ActivityVolunteerHours.status == status).order_by(
            ActivityVolunteerHours.submitted_at.desc(), ActivityVolunteerHours.registration_id,
            ActivityVolunteerHours.version.desc())
        return render_template("activities/volunteer_review.html", title="Volunteer hours review",
                               status=status, **paginated(query))

    @routes.post("/admin/community/volunteer-hours/<int:registration_id>/<int:version>/<action>")
    @admin_required
    def review_hours(registration_id, version, action):
        if action not in {"verify", "reject"}:
            abort(404)
        registration = db.get_or_404(ActivityRegistration, registration_id)
        service = get_service(registration.activity_id, locking=True)
        db.session.refresh(registration)
        submission = db.get_or_404(ActivityVolunteerHours, (registration_id, version))
        if submission.status != "pending":
            abort(409, description="These volunteer hours have already been reviewed.")
        note = note_value("review_note")
        if action == "reject" and not note:
            abort(400, description="Enter a reason for returning these hours.")
        submission.status = "verified" if action == "verify" else "rejected"
        submission.reviewed_at = service.now
        submission.reviewed_by_id = g.current_user.id
        submission.review_note = note or None
        registration_notice(registration.user_id, service.activity.id,
                            f"hours_{submission.status}", service.now)
        db.session.commit()
        return redirect(url_for(".hours_review"), code=303)

    @app.cli.command("reconcile-activity-registrations")
    @click.option("--batch-size", type=click.IntRange(1, 1000), default=100, show_default=True)
    def reconcile_registrations(batch_size):
        """Expire deadlines, check membership and promote eligible waitlisted members."""
        if not current_app.config["ACTIVITY_REGISTRATION_ENABLED"]:
            click.echo("Activity registration is disabled.")
            return
        after, processed = 0, 0
        while True:
            ids = db.session.execute(db.select(Activity.id).where(
                Activity.id > after, db.select(ActivityRegistration.id).where(
                    ActivityRegistration.activity_id == Activity.id,
                    ActivityRegistration.status.in_(OPEN_STATUSES),
                    ActivityRegistration.start_checked.is_(False),
                ).exists(),
            ).order_by(Activity.id).limit(batch_size)).scalars().all()
            db.session.commit()
            if not ids:
                break
            for activity_id in ids:
                activity = lock_activity(activity_id)
                if activity:
                    service_for(activity).reconcile()
                    processed += 1
                db.session.commit()
            after = ids[-1]
        click.echo(f"Checked {processed} activities.")

    app.register_blueprint(routes)
