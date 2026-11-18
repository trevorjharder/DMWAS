# Community Hub Phase 05 — Directory and Highlights

## Scope and decisions

The owner authorized Phase 05 and explicitly chose administrator-verified records
now, because activity attendance and volunteer-hour sources do not exist until
later phases. This implementation reuses the existing accounts, membership
policy, profiles, automatic profile approval, CSRF and design system. It does not
implement Phase 06 activity proposals or Phase 07 registration/attendance.

## Behavior and privacy

- `/members/search` is the adult-only directory. `/api/community/profiles` exposes
  the same filtered projection for authenticated active adults. Display-name
  search is case-insensitive and treats SQL wildcard characters literally.
- Interest, general-community and participation-goal filters only match fields
  the profile owner selected for sharing. Interests/goals use exact standardized
  array membership. Legal names, email, addresses, biography and verified identity
  are never searched. Filter controls use standard choices rather than revealing
  private member values in suggestions.
- Both SQL queries and direct profile URLs require directory opt-in, approved
  profile status, active account, recorded adult birth date, and a current paid or
  waived latest membership term. The existing policy for legacy active accounts
  with no term history is preserved. Eligibility is filtered before pagination.
- Pages contain at most 20 profiles, with stable display-name/ID ordering and
  previous/next navigation. One extra row determines the next-page link; there is
  no unbounded count query. Search/filter inputs and page offsets are bounded.
- A composite index supports directory visibility and ordering. Photo presence
  is projected without loading image bytes. Highlight associations and verified
  records are loaded in batches, avoiding a query per result.
- Discovery HTML, JSON and photo requests share a fixed UTC-minute quota of 60
  requests per account, stored in `directory_access_window`. PostgreSQL/SQLite
  atomic upsert enforces the quota across workers and sessions. Excess requests
  receive 429, a retry interval and a safe message. One quota row per account is
  reused; account deletion removes it. No search text or viewing history is kept.
- Administrators manage verified records at `/admin/community/records`. Each has
  an owner, safe summary, completion date, activity/volunteer type, optional whole
  volunteer minutes, explicit verification, shareable and sensitive flags.
  Creation and every edit produce an audit snapshot with administrator and time.
  Administrators can select multiple participants for one record and edit that
  member list later. Volunteer minutes apply to each selected member. Future dates
  and invalid minutes are rejected.
- Records start private unless an administrator explicitly marks them shareable.
  A sensitive flag always prevents sharing, including when shareable is checked.
  Administrators must classify the underlying activity, not just its summary.
- Members may select up to ten of **their own** verified, shareable, non-sensitive
  records they participated in on their existing profile form. A separate highlight visibility control
  is off by default. Selection alone does not publish anything; profile opt-in
  and every existing field/eligibility rule still apply.
- Each render/API response rechecks ownership and record verification/privacy.
  Revoking verification or marking a selected record private/sensitive hides it
  immediately. Stale or deliberately forged associations cannot leak another
  member's records. Profile deletion removes selection links; the verified source
  and its administrative audit remain. Account deletion removes owned sources,
  links and quota rows with the rest of the account data. Shared records and their
  audit history remain available for the other participants; records with no
  remaining participant are removed.

## Files

- `app.py`: directory SQL/service/API, shared quota, verified-source and audit
  models, admin verification, owner selection and safe projection/deletion.
- `templates/member_search.html`, `directory_rate_limit.html`: search, filters,
  pagination, result cards and safe empty/rate-limit states.
- `templates/admin_community_records.html`, `profile_highlights.html`: verification
  forms and shared profile-highlight rendering.
- Existing base/hub/profile/review/privacy templates and `static/styles.css`:
  navigation, responsive layout, selection controls and privacy disclosure.
- `migrations/versions/d05a1b2c3d4e_directory_and_verified_highlights.py`: four new
  tables, directory index and default-hidden profile highlight setting.
- `tests/test_directory.py`: privacy, filtering, authorization, ownership,
  verification/revocation, pagination, query count, quota and deletion regressions.
- `scripts/validate_phase05_postgres.py`: disposable localhost PostgreSQL smoke
  and migration/rollback check with synthetic data only. Set `POSTGRES_BIN` if
  PostgreSQL binaries are not in the default Windows PostgreSQL 18 location.

## Migration and recovery

Phase 05 revision `d05a1b2c3d4e` follows `c04f1a2b3d4e`. No existing directory
consent or profile approval is changed. No verified records are invented or
backfilled. There are no new runtime dependencies.

Before deployment to the existing **Testing** environment, take and verify a
fresh backup, confirm its database reference, and check the current revision:

```powershell
python -m flask --app app db current
python -m flask --app app db upgrade
python -m flask --app app db check
```

The new application queries the new profile column and quota table; its migration
must run **before** starting the Phase 05 application. Railway's previously
configured custom Start Command skips the repository Procfile migration step.
Use the existing Procfile or, after backup and revision checks, set the Testing
Start Command to:

```sh
python -m flask --app app db upgrade && exec gunicorn --workers 2 --threads 4 --bind 0.0.0.0:$PORT 'app:create_app()'
```

Do not stamp `head` to work around a missing table. The Phase 04 repair already
adopted the existing Testing baseline; verify that remains true rather than
repeating baseline adoption blindly. The initial implementation did not modify
Railway settings or migrate a live database. The subsequent Testing repair below
records the later database and startup-command changes.

To roll back: stop Phase 05 traffic, preserve a backup of verified sources/audits
and member selections, run `python -m flask --app app db downgrade c04f1a2b3d4e`,
then restore the Phase 04 application version. Downgrade drops all Phase 05
sources, audits, selections and quota data, the index, and the highlight visibility
column. It preserves existing users, memberships, profiles, photos and profile
reviews. Re-upgrade recreates empty Phase 05 tables. Use the preserved backup if
Phase 05 data must be recovered; do not treat a lossy downgrade as data recovery.

## Verification and gate

Final local result: **130 passed in 102.03 seconds** on Python 3.14.0. Five
warnings concern SQLite expression-index reflection, described below. The
PostgreSQL 18 validation completed successfully, including shutdown of its
disposable local instance. `git diff --check` and `python -m pip check` pass.

The automated suite exercises hidden-field filtering, literal wildcards, latest
term/youth/expiry/status restrictions, direct HTML/JSON authorization, stable
pagination, a maximum of ten SELECTs for a page of twenty profiles, shared quotas
across sessions/endpoints and window resets, administrator-only verification,
ownership, separate highlight consent, stale private/sensitive/revoked sources,
and profile/account deletion.

The disposable PostgreSQL 18 check verifies real JSON interest/goal predicates,
highlight projection, directory/admin/profile form responses, quota enforcement,
schema drift and upgrade/downgrade/re-upgrade preserving synthetic users/profiles.
All temporary PostgreSQL instances are stopped afterward. SQLite migration tests
emit known expression-index reflection warnings; PostgreSQL reflection validates
the index without relying on SQLite's incomplete expression-index reflection.

Run the checks with:

```powershell
python -m pytest -q --tb=short -p no:cacheprovider --basetemp .pytest-temp/phase05-final
python scripts/validate_phase05_postgres.py
```

**Phase 05 local gate: met.** Both checks passed. Live Testing migration,
deployed browser/mobile review and deployment acceptance remain outstanding.
The Society's privacy/retention and staffing sign-offs still apply before public
launch. Phase 06 is the next phase and is not implemented here.

## Subsequent Testing repair

After the Phase 05 application was deployed without its migration, `/member`
failed because `community_profile.show_highlights` was missing. The existing
Testing web service and database reference were verified; the database was on
`c04f1a2b3d4e` and matched the expected pre-Phase-05 schema.

A fresh custom-format PostgreSQL backup was saved under the gitignored path
`instance/backups/testing-before-phase05-20261118T032201Z.dump` (67,730 bytes;
SHA-256 `5282d3ac5a6531e457124e39daef03d275d153b469255b02afcd040c0623b077`).
The backup archive listing was validated. The database was upgraded to
`d05a1b2c3d4e`; PostgreSQL drift checking passed and existing record counts were
unchanged. Read-only application checks against Testing returned 200 for
`/member` and `/member/profile/preview`.

The previously approved temporary Testing database endpoint was removed, and its
proxy list was confirmed empty. An environment-specific configuration patch then
set Testing's web Start Command to the migration-first command above, with
`skipDeploys: true`. The saved command was verified afterward, and Production's
web Start Command was confirmed unchanged. No deployment or Production database
change was performed. The existing running Testing application can use the
repaired schema immediately; the saved startup command applies on future starts
using that configuration. A full backup restore rehearsal remains outstanding.

## Shared verified records update

Administrators can now create **one** activity or volunteer record with multiple
members, and add/remove participants in the existing edit form. Duplicate member
IDs are deduplicated; empty, invalid or nonexistent member lists are rejected
atomically. Every participant change is recorded in the administrator audit.
Each participant chooses highlights independently. Public profile responses never
include the participant list. Removing someone clears that member's selections
and prevents further sharing of the record without affecting the others.

Migration `e05b2c3d4e5f` follows `d05a1b2c3d4e`. It adds
`verified_community_record_participant` and backfills the existing member for
every verified record, preserving current profile selections. The original
`user_id` column is retained as a compatibility pointer to a primary participant;
sharing authorization uses the participant association. Application account
deletion moves that pointer to a remaining participant before deleting the old
account, preserving shared records and others' selections.

Back up Testing before deployment, then let the migration-first Start Command run
`db upgrade`. The expected revision after this update is **`e05b2c3d4e5f`**.
No live database changes or deployment were performed for this update.

Downgrading to `d05a1b2c3d4e` is blocked while any record has more than one
participant, to prevent silent loss of other members' contributions. Preserve a
backup and resolve shared records explicitly before attempting that rollback;
restoring the pre-update backup is the recovery alternative. A successful
single-participant downgrade drops the new association table and keeps the
original records and member assignments.

Validation: **135 tests passed in 74.55 seconds**, with the same five known SQLite
expression-index warnings. PostgreSQL 18 checks passed for legacy backfill,
schema drift, highlight access, the shared-record downgrade guard, and safe
downgrade/re-upgrade. The disposable local PostgreSQL server was stopped.

## Directory wording and sharing controls update

Member-facing labels now use "directory" rather than "adult directory". The
eligibility policy is unchanged. Verified highlights render below participation
goals on shared profiles and private previews.

The verified-record form now has one checkbox: **Allow members to share this
activity**. Saving as an authorized administrator verifies the record; unchecking
sharing makes it private. The separate verification and sensitive-activity
checkboxes were removed. Existing legacy verification/sensitivity restrictions
remain enforced until an administrator edits the record. Such a record shows
sharing unchecked initially, even if its old shareable flag was set. An explicit
sharing choice on save is authoritative and clears the old sensitivity flag;
saving with sharing unchecked keeps it private. Changes remain audited.

This update needs application deployment but no additional database migration.
Validation: **137 tests passed in 64.91 seconds**, with the same five known SQLite
expression-index reflection warnings. Coverage includes the single sharing
control, preservation of older private records, and highlight ordering.
