# Community Hub Phase 01 — Safe development foundation

**Status:** Repository foundation implemented; external staging and backup/restore gates remain unverified. No production service or database was accessed or changed.

## Staging setup

1. Create a separate Railway staging project or isolated staging service and a dedicated PostgreSQL database. Do not point staging at production, restore production personal data into staging, or reuse production credentials.
2. Configure staging variables in Railway's private variable store: `ENVIRONMENT=staging`, a newly generated `SECRET_KEY` of at least 32 characters, staging `DATABASE_URL`, and `SESSION_COOKIE_SECURE=true`. Do not put secret values in GitHub Actions, source files, command output, issues, or logs.
3. Keep Resend credentials unset in staging unless using a separately approved test sender and controlled recipient inbox. Set any admin notification address to a test mailbox. Do not deliver test messages to members.
4. Deploy the branch to staging only. Confirm the pre-deploy `flask --app app db upgrade` succeeds, then run the test suite and smoke-test public pages, login, membership review, payment recording and renewal against synthetic users.
5. Never use production data as test fixtures. The pytest fixtures use synthetic `example.test` identities and isolated SQLite databases.

## Migration workflow

- Create a revision only after model changes are reviewed: `flask --app app db migrate -m "Describe schema change"`.
- Inspect the generated revision. Autogeneration is a proposal; verify foreign keys, indexes, constraints, data backfills and both `upgrade()` and `downgrade()` manually. Do not accept an empty or destructive revision without understanding it.
- On a fresh disposable database, run `flask --app app db upgrade`, verify the app and schema, then run `flask --app app db downgrade base` to rehearse teardown. Recreate it with `flask --app app db upgrade` and rerun tests. The initial baseline downgrade drops all application tables; use only on an empty disposable database.
- On an existing database created by the former `create_all` startup, do **not** run `db upgrade` until it has a fresh backup and the existing schema has been reviewed against the baseline. In a maintenance window, first run `flask --app app db stamp head` (this changes only Alembic's version marker, not application tables), then run `flask --app app db check` and review the result. If differences are reported, stop: do not deploy or run `db upgrade`; remove the baseline marker with `flask --app app db stamp base` and resolve the drift through a separately reviewed recovery/migration plan. Only when `db check` reports no new operations should `flask --app app db upgrade` be considered safe. Stamping does not repair schema drift, and the stamp itself is not evidence that the tables match.
- Do not downgrade the initial baseline on a database containing member data: it drops the schema. For a failed production migration, stop traffic if needed and restore the verified pre-change backup to a separate database, validate it, then switch configuration according to the approved Railway recovery procedure. Never restore over production without explicit approval.
- For later additive revisions, rehearse upgrade and rollback on staging with synthetic data. If a downgrade would lose user data, prefer a forward-fix migration or documented restore procedure rather than claiming rollback is safe.

## Backup and restore rehearsal

This repository does not contain a backup credential or live database connection, so the rehearsal must be run by an authorized operator against the isolated staging database:

1. Create a custom-format PostgreSQL dump using the staging `DATABASE_URL` and `pg_dump`; store it in an access-controlled location, verify the command succeeded and the file is non-empty, and record its checksum. Do not commit the dump.
2. Provision a separate empty scratch PostgreSQL database. Restore with `pg_restore --no-owner --exit-on-error` using a staging-only connection string. Never target production for the rehearsal.
3. Verify restore success, expected tables and migration revision, then run application smoke tests and representative queries using synthetic records. Record restore duration, errors, and recovery-point/recovery-time observations.
4. Delete the scratch database and temporary dump according to the approved handling policy. Keep the approved backup in protected storage with documented retention and access controls.

Before any production migration, confirm a fresh, restorable backup, a named operator, maintenance/rollback procedure and explicit owner approval. Do not paste database URLs, passwords, dumps containing personal data, or secret values into chat or source control.

## Environment validation

- Local development may use SQLite and an ephemeral fallback session key; copy `.env.example` only for local use and set a private local `SECRET_KEY`.
- Staging/production startup now fails closed unless a `SECRET_KEY` is provided and at least 32 characters long, the configured SQLAlchemy URL is PostgreSQL, and `SESSION_COOKIE_SECURE` is true. Invalid boolean values fail startup rather than silently disabling the secure-cookie setting.
- Optional contact and email settings may be absent. Keep provider keys in Railway's private variables and never configure live recipients for staging tests.
- Git ignores `.env` and other `.env.*` files while allowing the non-secret `.env.example` template.

## CI and test baseline

GitHub Actions runs the test suite on pushes and pull requests using Python 3.12.16, matching `runtime.txt`; it requires no repository secrets. Run locally with `python -m pytest`. Tests should use isolated databases and synthetic users only.

The repository's pre-Phase-01 baseline had two HTML assertions that were sensitive to template whitespace; they are now whitespace-normalized. A separate existing admin-table test exposed an unwanted self-detail link; the template now hides only the signed-in administrator's own link while preserving links to other accounts. Legacy tests that depended on implicit `create_all` startup were replaced with explicit migration stamp/no-autocreate coverage. Migration adoption coverage stamps a synthetic complete schema first, then verifies `db check` and upgrade.

## Phase 01 acceptance checklist

- [x] Migration extension and initial model baseline are version-controlled.
- [x] App startup no longer creates tables implicitly; deployment runs migrations explicitly.
- [x] Environment validation covers deployed secret, PostgreSQL and HTTPS-cookie requirements.
- [x] Synthetic adult/youth user fixture and configuration/migration tests are present.
- [x] CI workflow and secrets-safe local environment template are present.
- [x] Staging, migration adoption, rollback and backup/restore instructions are documented.
- [x] Fresh SQLite migration upgrade/downgrade/re-upgrade and schema drift check completed locally.
- [x] Full local suite passes: 35 tests on Python 3.14.0.
- [ ] Staging environment created and deployed by an authorized operator.
- [ ] GitHub Actions and full test suite pass under Python 3.12.16.
- [ ] PostgreSQL migration upgrade/rollback or recovery rehearsal completed on staging.
- [ ] Backup restored and application smoke-tested on a separate scratch database.
- [ ] Owner accepts the staging and recovery evidence.

**Gate result:** Not met yet. Repository changes can be reviewed and tested locally, but external staging, PostgreSQL recovery and owner sign-off cannot be truthfully claimed from this workspace.
