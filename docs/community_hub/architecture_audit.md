# Community Hub architecture audit

**Status:** Phase 00 draft for owner review  
**Scope:** Read-only inspection of the DMWAS checkout; no application behavior changed. Plan source: `Docs/DMWAS_Codex_Implementation_Plan.md` on the repository's `main` branch. That file is not present in this local checkout.

## Existing architecture

- **Application:** One Flask application in `app.py`; database models, route handlers, authentication/session checks, CSRF, email and CLI commands are colocated there. No blueprint structure was found.
- **Persistence:** Flask-SQLAlchemy with SQLite as local default and PostgreSQL URL support via `DATABASE_URL`; PostgreSQL URLs are adapted to psycopg2. Models include `User`, `MembershipDetails`, `MembershipPreferences`, `MembershipConsent`, `MembershipTerm`, and `PasswordResetCode`.
- **Authentication:** Existing session-based login uses a `user_id` session value. `login_required`/administrator checks protect member/admin routes. Membership state and an `is_admin` boolean are the current authorization inputs; there is no separate role/permission service.
- **Eligibility:** Date of birth is stored in membership details; adult threshold is 19. Membership expiry is evaluated lazily on selected request/login/admin paths. Adult dues are represented in membership terms, but the README notes they are not collected online and the approval workflow's payment policy must be reconciled before using active status as proof of all Hub permissions.
- **Member UX:** `templates/base.html` includes member navigation. `templates/member_home.html` and `templates/member_resources.html` are placeholder pages. `app.py` provides an active-member name search at `/members/search`; there is no member-editable profile or directory opt-in.
- **Admin UX:** Admin request/member pages support review of membership records; admin identity is a boolean flag. No general administrative audit-log model was found.
- **Email:** Resend API calls are synchronous HTTP requests. There is no checked-in worker/queue or digest scheduler.
- **Uploads/storage:** No user upload or object-storage flow was found. Existing static assets include Society documents; do not add profile-photo uploads until storage, validation, moderation, retention and deletion are decided.
- **Schema/deployment:** `Procfile` runs `flask --app app init-db` before Gunicorn; startup also creates missing tables. This is additive table creation, not a versioned migration system. The README documents Railway PostgreSQL deployment. No migration framework, CI workflow, backup/restore runbook, job runner, or scheduler configuration was found in the repository; external Railway configuration was not inspected.
- **Runtime:** `runtime.txt` specifies Python 3.12.16; the configured local `.venv` is Python 3.14.0. Test results should be repeated under the declared production runtime before release.

## Test baseline

Command: `python -m pytest --basetemp=.pytest-tmp-validation` using the repository `.venv` (Python 3.14.0). Result: **29 passed, 2 failed**. Failures are existing assertions in `tests/test_site.py`: `test_admin_can_approve_request_and_member_can_sign_in` expects the phrase “confirming adult dues were received when applicable”; `test_admin_members_table_includes_admins_except_society_administrator` expects `Administrator</span>`. The remaining tests passed. This is a baseline, not introduced by the Hub work. The original default temp directory issue was avoided by placing pytest temp files under the workspace.

## Reuse points and constraints

1. Reuse the existing `User` primary key and membership records; do not create duplicate identity/authentication tables.
2. Put policy checks in a centralized service and enforce them in handlers and data queries, not just navigation/templates.
3. Keep legal identity, email, phone, street address, DOB and guardian details private; expose only explicitly opted-in, independently visible profile fields.
4. Keep public activities separate from existing Society programs unless the owner explicitly approves integration.
5. Scope organizer access to assigned activities and the minimum registration data needed for operations.
6. Require CSRF for browser mutations, validate every submitted field, and rate-limit discovery, connection, messaging and report actions.
7. Do not include private message content in email or ordinary logs. Admin conversation access must be report/evidence-scoped and audited.
8. Do not add a production deploy, destructive schema change, or live activity/messaging capability without explicit owner approval and tested recovery procedures.

## Findings and blockers

The checkout can support incremental Hub work, but the plan's phases 01–02 are not demonstrably complete. There is no migration history, verified backup/restore process, CI configuration, or scheduler in the repo. Several material policy decisions in `risk_register.md` remain open. The next safe step is owner review of these findings and decisions; production-facing feature implementation should wait for that gate.
