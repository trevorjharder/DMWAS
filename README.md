# District of Mount Waddington Access Society

A small standalone Flask website for the District of Mount Waddington Access Society. It is separate from the Fireapp project.

## Current first-step features

- Public About, Notices, and Contact pages.
- Member access requests with hashed passwords; requests remain pending until an administrator approves them.
- Member sign-in and a placeholder private member landing page.
- A protected admin page to approve or decline pending requests.
- Optional email notification to `members@accessni.ca` when SMTP is configured.
- PostgreSQL support for Railway, with SQLite as a local-development default.

The About page summarizes the six purposes in the Society constitution provided so far. The public contact page intentionally shows no details until approved contact information is configured. The Notices page is an empty placeholder; notice editing is not implemented.

## Local development

Use Python 3.12 or newer.

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Set `SECRET_KEY` to a strong random value for a stable local session.
4. Create the initial database schema with `flask --app app init-db`.
5. Create the first administrator with `flask --app app create-admin`. The command prompts for a username, email, and password without echoing the password.
6. Run locally with `flask --app app run --debug`.
7. Run tests with `pytest`.

The local default database is `dmwas.db` in the project directory. Keep database files and secrets out of version control.

## Railway deployment

- Add a Railway PostgreSQL service and provide its `DATABASE_URL` to the web service.
- Set a stable, private `SECRET_KEY` in Railway variables. Do not rely on the local fallback in production, especially with multiple Gunicorn workers.
- Set `SESSION_COOKIE_SECURE=true` when the service is served over HTTPS.
- Deploy this repository; the included `Procfile` starts Gunicorn.
- Run `flask --app app init-db` for a new database, then create the initial admin using `flask --app app create-admin` in an authenticated Railway shell. Do not expose administrator signup publicly.
- Connect `accessNI.ca` in Railway and follow its current domain/DNS instructions. Verify HTTPS before launch.

### Optional SMTP notifications

Configure `SMTP_HOST`, `SMTP_PORT` (defaults to `587`), `SMTP_FROM`, and optionally `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_USE_TLS` (defaults to `true`). The recipient defaults to `members@accessni.ca`; it can be changed using `ADMIN_NOTIFICATION_EMAIL`. When SMTP settings are absent, requests are saved normally and a message is written to the application log instead of being emailed.

### Public contact settings

Set any approved public details using `PUBLIC_CONTACT_EMAIL`, `PUBLIC_CONTACT_PHONE`, and `PUBLIC_CONTACT_ADDRESS`. These are displayed on the Contact page. Do not put private Society/member information in these variables.

## Membership workflow and outstanding decisions

Applicants provide their name, email, optional phone number, username, and password. Passwords are stored as hashes. An account is unable to sign in until an administrator approves it. The admin screen asks the reviewer to confirm that the Society's required checks have been completed.

The constitution supplied so far contains the Society name and purposes, but does not state membership eligibility, dues, payment confirmation, or approval authority. Before public launch, confirm the Society's membership and approval policy and adjust the access request and admin review flow to match. Dues are not collected online. Declined-request retention/reapplication rules and password recovery are also not yet decided.

## Next steps

Before launch, provide approved public contact details, logo/colors/photos, any initial notices, and the Society's membership/approval policy. Choose and test the email delivery service and configure its credentials securely in Railway. Member documents, the member directory, notice editing, password recovery, and online dues payments are outside this first implementation step.
