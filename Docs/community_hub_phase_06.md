# Community Hub Phase 06 — Activity proposals and approval

## Workflows

- Active adult members open **My activities** to save a draft or submit it for review directly from the form. Active administrators can also create official Society activities. A draft may be incomplete. Submission requires the public title and summary, category, dates, location, private meeting details, accessibility information, capacity, and registration mode. New activities use Vancouver time. Existing activities retain their recorded time zone. Enter **Online** in Location for virtual activities. In-person activities also require safety information.
- The **Activity review** queue lets administrators approve, reject, request changes or unpublish. The primary organizer can withdraw a pending submission or edit it after a decision. A material edit saves a new revision and takes an approved listing offline until the new version is approved. An administrator who authors a member proposal needs another administrator to approve it.
- Primary organizers can invite an eligible adult by their directory display name when creating a draft or from the activity workspace. Only eligible adults with a visible, opted-in directory profile appear in the picker. Invitees accept or decline in **My activities**, and an administrator approves accepted assignments. Approved co-organizers can read organizer details for the specific approved activity while both organizers are eligible. The primary organizer or an administrator can revoke access, and a co-organizer can leave.
- The administrator history records revision, action, status change, actor, time and reviewer note. Stale forms receive a conflict response. Member-workspace mutations share a 30-per-minute account limit.
- **Activities** shows the approved public title, summary, category, local times and location. The Location text box is for a general area or online format. Reviewers check the title, summary and location for personal information and exact meeting details before approving. The listing query selects public fields only. Eligibility changes, edits, unpublishing and the activity end time can remove a listing.

Public member-organized listings are disabled by default. Leave
`MEMBER_ACTIVITY_PUBLICATION_ENABLED=false` on live services until the Society
completes the insurer and policy review, confirms public location and accessibility
disclosure, and reviews approved proposals for publication. Official Society
activities can appear after administrator approval. Complete a staging walkthrough
using synthetic drafts and members. Registration, waitlists and attendance are
Phase 07 work.

## Migration and recovery

Revision `f06c3d4e5f6a` follows `e05b2c3d4e5f` and adds the activity,
revision, co-organizer, audit and action-limit tables. Install the updated
requirements before running the application. The first-party `tzdata` package
supplies IANA time zones on Windows, where a system IANA database is typically
absent. [Python time zone data documentation](https://docs.python.org/3/library/zoneinfo.html#data-sources)

Back up and verify the existing Testing database before applying the migration.
Confirm that Testing uses the migration-first start command and the intended
database. Apply the schema before starting the new application.

```powershell
python -m flask --app app db current
python -m flask --app app db upgrade
python -m flask --app app db check
```

For a schema rollback, stop writes and preserve a backup of Phase 06 records.
Run `python -m flask --app app db downgrade e05b2c3d4e5f`, then restore the
previous application version. The downgrade removes Phase 06 drafts, revisions,
invitations, audit history and limits. Re-upgrading creates empty Phase 06 tables.
Restore the preserved backup to recover Phase 06 records.

## Acceptance checks

Disposable PostgreSQL 18 passed the original Phase 06 schema checking, upgrade,
downgrade, re-upgrade, and publication checks. The form update requires no
database migration and passed 43 activity and permission tests. The core form
changes also passed the 158-test suite in an isolated integration with the
committed Phase 05 baseline, with five existing SQLite expression-index
reflection warnings.

Run `python -m pytest tests/test_activities.py` for Phase 06, then run the full
suite. Verify the migration and rollback against a disposable PostgreSQL
database. In Testing, walk through proposal editing, rejection and reapproval,
co-organizer consent and revocation, and anonymous listings with the publication
switch off and on. Check that an approved public listing shows no private
meeting details or participant identities.

Keep the existing Phase 05 directory and profile workflow in the release
candidate. If the full suite cannot import the directory models, reconcile the
Phase 05 application changes before release rather than stamping the database to a different revision. Record Testing acceptance and
insurer and policy approval before enabling public member-organized listings.
