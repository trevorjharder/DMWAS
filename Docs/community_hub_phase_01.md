# Community Hub Phase 01 — Safe development foundation

**Status:** Repository foundation implemented; the current Railway project is the existing staging environment in the deployment pipeline. Do not create another staging project, web service, or database for this phase. External staging and backup/restore gates remain unverified. No production service or database was accessed or changed.

**Owner decision:** Backup is deferred for now. Because the existing staging database reports no Alembic revision, leave the current direct-Gunicorn Start Command unchanged and do not run `db stamp` or `db upgrade` against it until a backup is available and the schema has been verified. This intentionally leaves the database-migration and recovery acceptance gates incomplete.

## Verify the existing staging environment

1. Use the existing Railway staging project and its existing staging PostgreSQL service. Do not create another database/service for this phase. Confirm the web service you deploy to is the staging service, not production, and its `DATABASE_URL` references the staging Postgres service.
2. In the staging web service's private Railway variables, confirm `ENVIRONMENT=staging`, a private `SECRET_KEY` of at least 32 characters, the staging database reference in `DATABASE_URL`, and `SESSION_COOKIE_SECURE=true`. Do not copy secret values into source, CI variables, command output, issues, or logs.
3. Keep Resend credentials unset unless using a separately approved test sender and controlled recipient inbox. Set any admin notification address to a test mailbox. Do not send test messages to members.
4. Deploy the branch through the existing pipeline to staging only. Confirm the pre-deploy `flask --app app db upgrade` succeeds, then run the test suite and smoke-test public pages, login, membership review, payment recording and renewal against synthetic users.
5. Never use production data as test fixtures. The pytest fixtures use synthetic `example.test` identities and isolated SQLite databases.
6. After Phase 01 is accepted and a later change is approved, use the existing release pipeline to promote the tested revision to production. Production promotion is not part of this phase and must not happen automatically as a side effect of staging verification.

**Railway start-command check:** The owner reports the service's current Start Command is `gunicorn --workers 2 --threads 4 --bind 0.0.0.0:$PORT 'app:create_app()'`. A custom Start Command overrides the repository Procfile, so this command starts Gunicorn directly and does **not** run `flask --app app db upgrade`. Before changing it, take the staging backup and inspect the database's Alembic revision/schema. If the schema is already on the baseline, either clear the custom Start Command so Railway uses the Procfile or set it to `flask --app app db upgrade && exec gunicorn --workers 2 --threads 4 --bind 0.0.0.0:$PORT 'app:create_app()'`. If an existing database has no migration revision, follow the adoption procedure below first; do not run `db upgrade` blindly against tables created by `create_all`.

## Migration workflow

- Create a revision only after model changes are reviewed: `flask --app app db migrate -m "Describe schema change"`.
- Inspect the generated revision. Autogeneration is a proposal; verify foreign keys, indexes, constraints, data backfills and both `upgrade()` and `downgrade()` manually. Do not accept an empty or destructive revision without understanding it.
- On a fresh disposable database, run `flask --app app db upgrade`, verify the app and schema, then run `flask --app app db downgrade base` to rehearse teardown. Recreate it with `flask --app app db upgrade` and rerun tests. The initial baseline downgrade drops all application tables; use only on an empty disposable database.
- On an existing database created by the former `create_all` startup, do **not** run `db upgrade` until it has a fresh backup and the existing schema has been reviewed against the baseline. In a maintenance window, first run `flask --app app db stamp head` (this changes only Alembic's version marker, not application tables), then run `flask --app app db check` and review the result. If differences are reported, stop: do not deploy or run `db upgrade`; remove the baseline marker with `flask --app app db stamp base` and resolve the drift through a separately reviewed recovery/migration plan. Only when `db check` reports no new operations should `flask --app app db upgrade` be considered safe. Stamping does not repair schema drift, and the stamp itself is not evidence that the tables match.
- Do not downgrade the initial baseline on a database containing member data: it drops the schema. For a failed production migration, stop traffic if needed and restore the verified pre-change backup to a separate database, validate it, then switch configuration according to the approved Railway recovery procedure. Never restore over production without explicit approval.
- For later additive revisions, rehearse upgrade and rollback on staging with synthetic data. If a downgrade would lose user data, prefer a forward-fix migration or documented restore procedure rather than claiming rollback is safe.

## Backup and restore rehearsal

This repository does not contain a backup credential or live database connection, so the rehearsal must be run by an authorized operator against the existing staging database:

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

GitHub Actions runs the test suite on pushes and pull requests using Python 3.12.16, matching `runtime.txt`; it requires no repository secrets. Run locally with `python -m pytest`. Tests should use isolated databases and synthetic users only. CI validation does not deploy or promote code to production.

The repository's pre-Phase-01 baseline had two HTML assertions that were sensitive to template whitespace; they are now whitespace-normalized. A separate existing admin-table test exposed an unwanted self-detail link; the template now hides only the signed-in administrator's own link while preserving links to other accounts. Legacy tests that depended on implicit `create_all` startup were replaced with explicit migration stamp/no-autocreate coverage. Migration adoption coverage stamps a synthetic complete schema first, then verifies `db check` and upgrade. GitHub Actions run “Phase 1.1” for commit `35f1eb5` completed successfully on Python 3.12.16; the full local suite also passes on Python 3.14.0.

## Phase 01 acceptance checklist

- [x] Migration extension and initial model baseline are version-controlled.
- [x] App startup no longer creates tables implicitly; repository Procfile applies reviewed migrations.
- [x] Environment validation covers deployed secret, PostgreSQL and HTTPS-cookie requirements.
- [x] Synthetic adult/youth user fixture and configuration/migration tests are present.
- [x] CI workflow and secrets-safe local environment template are present.
- [x] Existing staging environment, migration adoption, rollback and backup/restore instructions are documented.
- [x] Fresh SQLite migration upgrade/downgrade/re-upgrade and schema drift check completed locally.
- [x] Full local suite passes: 35 tests on Python 3.14.0 (re-run during completion review).
- [x] GitHub Actions and full test suite pass under Python 3.12.16 for commit `35f1eb5`.
- [x] Owner reports the existing staging app is running again after correcting its Railway variables.
- [x] Owner confirmed the running staging web service references the intended staging PostgreSQL service, not production.
- [x] Owner reports the read-only staging smoke test looks good.
- [ ] Configure the Railway Start Command to run the migration before Gunicorn (or clear the override to use the Procfile), after safe database adoption is verified.
- [ ] Record the PostgreSQL baseline/migration verification for staging. App availability alone does not confirm migrations ran; the currently reported direct Gunicorn command bypasses them.
- [ ] Backup restored and application smoke-tested on a separate scratch database.
- [ ] Owner accepts the staging and recovery evidence.

**Gate result:** Not met yet. Repository changes can be reviewed and tested locally, but staging deployment, PostgreSQL recovery and owner sign-off cannot be truthfully claimed from this workspace. The current staging environment is the target; no additional staging infrastructure is required.
