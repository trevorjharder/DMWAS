# Community Hub Phase 06 — Activity proposals and approval

## Workflows

- Active adult members open **My activities** to save drafts and submit them for review. Active administrators can also create official Society activities. A draft may be incomplete. Submission requires the public title and summary, category, dates, time zone, general area or online format, private meeting details, accessibility information, capacity, and registration mode. In-person activities also require safety information.
- The **Activity review** queue lets administrators approve, reject, request changes or unpublish. The primary organizer can withdraw a pending submission or edit it after a decision. A material edit saves a new revision and takes an approved listing offline until the new version is approved. An administrator who authors a member proposal needs another administrator to approve it.
- Primary organizers invite eligible adults by member number. Invitees accept or decline in **My activities**, and an administrator approves accepted assignments. Approved co-organizers can read organizer details for the specific approved activity while both organizers are eligible. The primary organizer or an administrator can revoke access, and a co-organizer can leave.
- The administrator history records revision, action, status change, actor, time and reviewer note. Stale forms receive a conflict response. Member-workspace mutations share a 30-per-minute account limit.
- **Activities** shows the approved public title, summary, category, local times and general area. The public location choices are Mount Waddington area, outside Mount Waddington and online. Reviewers check the title and summary for personal information and exact meeting details before approving. The listing query selects public fields only. Eligibility changes, edits, unpublishing and the activity end time can remove a listing.

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

Local validation passed all 18 Phase 06 tests. The isolated integration with
the committed Phase 05 baseline passed all 155 tests, with five existing SQLite
expression-index reflection warnings. Disposable PostgreSQL 18 passed schema
checking, upgrade, downgrade, re-upgrade, and publication checks.

Run `python -m pytest tests/test_activities.py` for Phase 06, then run the full
suite. Verify the migration and rollback against a disposable PostgreSQL
database. In Testing, walk through proposal editing, rejection and reapproval,
co-organizer consent and revocation, and anonymous listings with the publication
switch off and on. Check that an approved public listing shows no private
meeting details or participant identities.

Keep the existing Phase 05 directory and profile workflow in the release
candidate. If the full suite cannot import the verified-record or directory
models, reconcile the Phase 05 application changes before release rather than
stamping the database to a different revision. Record Testing acceptance and
insurer and policy approval before enabling public member-organized listings.
