# Community Hub Phase 07 — Registration, waitlists, attendance and volunteering

## Member and organizer workflows

- Active eligible members open an approved listing under **Activities**, select
  **Register**, and enter a name for the roster. Immediate registration confirms
  a place when capacity permits. Activities that require organizer approval
  collect requests first. Approved requests enter the waitlist when all places
  are taken. An unapproved request does not reserve a place.
- **My registrations** shows the member's own status, deadlines, attendance and
  verified volunteer time. **Activity notices** records confirmations, requests,
  offers, activity changes and cancellations in the application. Confirmed,
  eligible participants can see meeting and preparation information while the
  approved activity is current.
- Waitlist order is set when a registration enters the waitlist. Each live
  offer reserves one place. Eligible members must accept before the displayed
  deadline. Expiration or cancellation releases the place and the next eligible
  member can be offered it. A member who rejoins after expiry joins at the end.
- The primary organizer and approved co-hosts open **Manage roster** from the
  activity workspace. They can approve, decline or cancel registrations and
  record attendance when the activity starts. The roster shows the name supplied
  at registration and operational status. It excludes account contact and
  membership profile details.
- Organizers submit whole volunteer minutes for attendees after the activity
  ends. Administrators use **Volunteer hours** to verify submissions or return
  them with a reason. Corrections create a new version. Totals use the latest
  verified version per attendee, including a verified zero-minute correction.
- A material change pauses registration and returns outstanding offers to their
  prior waitlist position. Members receive a notice and retain confirmed bookings
  during review. Reapproval requires capacity to cover those bookings. If it
  remains paused at the last approved start, the job releases open places.
  Administrator unpublishing releases open registrations and sends a cancellation
  notice. Previously checked attendance and volunteer history are retained.

## Registration settings

Registration is available in local development and disabled by default on
deployed services. Before enabling it in a deployed environment, approve and set
the following values explicitly. Values shown are provisional local defaults.

| Setting | Local default | Effect |
| --- | --- | --- |
| `ACTIVITY_REGISTRATION_ENABLED` | `true` | Opens the Phase 07 workflows. |
| `ACTIVITY_OFFER_HOURS` | `24` | Offers end at this duration or at signup closure, whichever is earlier. |
| `ACTIVITY_SIGNUP_CLOSE_MINUTES` | `60` | Stops registrations, organizer approvals and offer acceptance before start. |
| `ACTIVITY_CANCELLATION_CLOSE_MINUTES` | `60` | Stops member cancellation of confirmed places before start. Organizers can still cancel a place before start. |
| `ACTIVITY_RENEWAL_GRACE_HOURS` | `24` | Time from the first failed eligibility check to renew, capped at activity start. Ineligible waitlist members are skipped while they renew. |
| `ACTIVITY_ALLOW_YOUTH_REGISTRATION` | `false` | Keep false until the Society approves youth and guardian arrangements. |

The existing member-publication switch and insurer and policy review also apply
to member-organized activities. During the signup-close window, the job checks
whether a participant's membership covers the activity day. It checks again
at the start before attendance can be recorded.

## Migration and scheduled job

Revision `a07d4e5f6a7b` follows `f06c3d4e5f6a` and adds registrations,
volunteer-hour versions and in-app notices. Back up the Testing database,
confirm its database reference, and apply the migration before starting the
new application.

```powershell
python -m flask --app app db upgrade
python -m flask --app app db check
python -m flask --app app reconcile-activity-registrations
```

Run the last command from a scheduler against the same database and settings as
the web application. A five-minute interval is a starting point for Testing.
The command batches activities and commits each one independently. Concurrent
workers, organizer approvals, cancellations and member registrations serialize
on the activity row. Re-running the job leaves already processed offers and
notices unchanged.

For rollback, stop web writes and the scheduled job, back up Phase 07 records,
run `python -m flask --app app db downgrade f06c3d4e5f6a`, and restore the
Phase 06 application. Downgrade drops bookings, attendance, verification history
and notices while preserving activities and proposals. Re-upgrade creates empty
Phase 07 tables. Recover Phase 07 records from the preserved backup.

## Acceptance

All 13 Phase 07 tests passed, including concurrent registrations and concurrent
cancellations. The full suite with the intact Phase 05 baseline passed 171 tests
with five existing SQLite expression-index warnings. The additional concurrent
cancellation test passed in the focused run. Disposable PostgreSQL 18 passed
concurrency, offer expiry, repeated reconciliation, schema checking and
upgrade/downgrade/re-upgrade. The shared checkout's existing Phase 05 model
mismatch still blocks full suite collection there.

Run `python -m pytest tests/test_activity_registrations.py` and
`python -m pytest tests` against the integrated release candidate.
In Testing, verify both registration modes, memberships at activity start,
simultaneous last-seat requests, FIFO offers and deadline expiry, notice
visibility, co-host scope, and corrected volunteer totals. Confirm the scheduled
job is running and record the owner's policy decisions before enabling
registration on a live service.
