# Community Hub Phase 04 — Profiles and Privacy

## Findings and scope

Continues the existing uncommitted profile work after Phase 03. Profiles reuse
the existing `User` primary key and the Phase 02 capability policy. Adult
eligibility continues to use the recorded birth date (19+) and current membership
term/payment, rather than a profile field. Existing public programs, membership
applications and renewal records are preserved. Directory search, pagination and
highlights remain disabled for Phase 05.

## Implemented behavior

- A member-selected display name, biography, general community, standardized
  interests and goals; all optional fields and directory participation are hidden
  by default. No verified name, email, address, phone, birth date or guardian data
  is copied into the peer-facing projection.
- Optional JPEG/PNG photos are limited to 2 MB and 16 million pixels, decoded,
  resized to 512 pixels and re-encoded without EXIF or other source metadata.
  Only normalized bytes are stored in PostgreSQL, alongside the profile, so
  Railway does not need an ephemeral upload directory or another storage service.
  Photos have an independent visibility setting and an authenticated serving route;
  they are never written into `static/`. Requests are capped at 4 MB.
- Owner edit/preview and explicit self-service deletion. Approved, inactive and
  expired accounts retain their own privacy controls without regaining directory
  access. Youth can keep a private profile but cannot opt into adult discovery.
- Opted-in profiles require administrator approval. Any edit returns an opted-in
  profile to pending review. Opt-out hides it immediately. Administrators can
  reject or unpublish profiles and leave private feedback. Decisions record the
  administrator, time and note. A version check and ORM optimistic concurrency
  prevent approval of content changed since the review form was loaded.
- Moderation shows only fields selected for sharing, not unshared biography,
  location, interests, goals or photo. Administrators have no override to browse
  opted-out profiles through peer endpoints.
- `community_profile_fields()` is the allowlist shared by the HTML view, owner
  preview, moderation and `GET /api/community/profiles/<user_id>`. Direct HTML,
  JSON and photo routes enforce current owner/viewer eligibility, opt-in and
  approval; they use `private, no-store` caching. No directory search is enabled.
- Profile mutations require the existing CSRF protection. Owner operations take
  their target from the session, never a submitted user ID. Jinja escapes profile
  text. Validation failures leave the saved profile untouched.
- `delete_community_profile(user_id)` is a transactional deletion hook reused by
  member self-service and account deletion. It removes profile fields, the photo
  and profile review records, leaving membership records intact for profile-only
  deletion. Future approved retention jobs can call the same hook and commit.

## Files

- `app.py`, `permissions.py`: profile schema, privacy projection, photo handling,
  routes, review history, eligibility and deletion hook.
- `migrations/versions/c04f1a2b3d4e_community_profiles.py`: additive profile and review
  tables, indexes and rollback.
- `templates/member_profile_edit.html`, `member_profile_preview.html`,
  `community_profile_public.html`, `admin_community_profiles.html`: profile flows.
- `templates/base.html`, `member_home.html`, `membership_renewal.html`,
  `privacy.html`, and `static/styles.css`: navigation, renewal access, draft notice
  and responsive profile/photo styling.
- `requirements.txt`: Pillow for bounded image decoding and metadata removal.
- `tests/test_community_profiles.py`, `test_permissions.py`, `test_site.py`:
  privacy/ownership/expiry, migration recovery and existing-site regressions.

## Migration and recovery

Revision `c04f1a2b3d4e` follows `958ef19b9634`. There is no user backfill and no
automatic directory opt-in. Review and back up the existing staging database
using the Phase 01 runbook before applying:

```powershell
python -m flask --app app db upgrade
python -m flask --app app db check
```

If the existing database has no Alembic version, follow the Phase 01 adoption
runbook and stamp **958ef19b9634**, after verifying the baseline schema. Do not
stamp `head` on a Phase 03 database: that would skip creating the new tables.

For a rollback, stop the new application and preserve a backup of profiles first.
Run `python -m flask --app app db downgrade 958ef19b9634`, then restore the Phase 03
application revision. Downgrade deletes the two Phase 04 tables and their photos
and reviews; it preserves users and membership data. Re-upgrade recreates empty
profile tables. To recover deleted profiles, restore the reviewed backup instead.

## Verification and acceptance

Final local result: **95 passed in 43.02 seconds** on Python 3.14.0, using:

```powershell
python -m pytest -q --tb=short -p no:cacheprovider --basetemp .pytest-temp/phase04-final
```

`python -m pip check` reports no broken requirements, and `git diff --check`
passes for the changed source files. The pre-existing Copilot changes passed
56 tests before this continuation. CI's Python 3.12 run has not been triggered.

Automated coverage includes independent visibility flags, hidden-field HTML/API
and search checks, default privacy, direct URLs, opt-out, resubmission, review
authorization/audit, stale-review rejection, youth and expiry, owner isolation,
CSRF, input validation, escaped markup, photo decoding/metadata/size/visibility,
and deletion with membership preservation. A fresh synthetic SQLite database is
upgraded from the baseline, checked for schema drift, downgraded with foreign-key
checking enabled, then upgraded and checked again while preserving its user.

The initial implementation did not migrate or deploy a live database. In the
subsequent owner-authorized repair, the existing Railway **Testing** environment
was verified to use its matching PostgreSQL service. Its existing schema matched
the baseline and had no Alembic revision. A PostgreSQL 18 custom-format backup was
saved to the gitignored `instance/backups/testing-before-phase04-20261114T225337Z.dump`
(21,883 bytes; SHA-256 `541139dcac9d9f37bc0aa98db38af078bd3f986fd8ef56c6a962d969f391ae93`).
The archive listing was validated; a full restore rehearsal remains outstanding.
The verified baseline was stamped `958ef19b9634` and upgraded to `c04f1a2b3d4e`.
PostgreSQL schema drift checking passed and existing record counts were unchanged.
Read-only application checks against Testing returned HTTP 200 for `/member` and
`/member/profile/preview`. The edit route was excluded from the read-only check
because it takes a PostgreSQL row lock. The owner-approved temporary TCP endpoint
was removed afterward; the proxy list is empty. No SSH key was registered, and
the unused local temporary key was removed. Production was not changed. Railway's
custom web Start Command was not changed by this database repair.

Restore rehearsal and deployed browser/mobile acceptance remain outstanding.
The Society must approve its
retention schedule (including backups), privacy contact and moderation staffing
before public launch. No automatic retention duration is invented here. After
restoring an older backup, reapply recorded opt-outs/deletion requests before
reopening access, because backups can contain withdrawn profiles.

Phase 04's automated feature gate is met by the passing full test suite;
deployment acceptance remains subject to the staging and owner checks above.
The next phase is Phase 05, Directory and Highlights; it is not implemented here.
