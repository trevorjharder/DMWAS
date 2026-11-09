# District of Mount Waddington Access Society

A small standalone Flask website for the District of Mount Waddington Access Society. It is separate from the Fireapp project.

## Current first-step features

- Public About, Notices, and Contact pages.
- Membership applications with hashed passwords, contact and membership details, and pending approval until an administrator approves them.
- Optional volunteer and newsletter preferences, versioned consent records, and membership term/payment history.
- Member sign-in and a placeholder private member landing page.
- A protected admin page to approve or decline pending requests.
- Optional email notification to `members@accessni.ca` when Resend is configured.
- Email verification-code password recovery for account holders.
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
- The Procfile runs `flask --app app init-db` before Gunicorn starts. The app also creates missing tables on its first request as a fallback if Railway overrides the Procfile. Both paths are additive and safe to rerun; they do not alter or delete existing tables. Confirm Railway's Start Command is unset or uses the repository Procfile.
- Create the initial admin using `flask --app app create-admin` in an authenticated Railway shell. Do not expose administrator signup publicly.
- Connect `accessNI.ca` in Railway and follow its current domain/DNS instructions. Verify HTTPS before launch.

The membership application stores date of birth, mailing address, and (for youth applications) parent/guardian contact details. Review and adopt the draft Privacy Information page before collecting this information publicly. Adult/youth eligibility is calculated from date of birth as of the application date; the form does not process the $10 adult membership payment.

### Email delivery with Resend

Configure `RESEND_API_KEY` with the Resend API key and `RESEND_FROM` with a sender address on a domain verified in Resend. The legacy `SMTP_FROM` variable is accepted as a fallback sender name/address. The API sends over HTTPS, which works on Railway Hobby where outbound SMTP is disabled. Membership request notifications go to `members@accessni.ca` by default; change this with `ADMIN_NOTIFICATION_EMAIL`.

Password-reset verification codes are sent to the account email address. Codes are six digits, expire after 10 minutes, are single-use, and are limited to five attempts; new code requests are limited to one per account per minute. Reset requests use a generic response to avoid confirming whether an email address has an account. When Resend settings are absent, accounts and reset requests continue to work, but email is skipped and logged.

### Public contact settings

Set any approved public details using `PUBLIC_CONTACT_EMAIL`, `PUBLIC_CONTACT_PHONE`, and `PUBLIC_CONTACT_ADDRESS`. These are displayed on the Contact page. Do not put private Society/member information in these variables.

## Membership workflow and outstanding decisions

Applicants provide their name, email, phone number, community, date of birth, mailing address, membership choice, and account credentials. Youth applicants must provide parent/guardian details and an on-form consent attestation; the application does not independently verify the guardian's identity. Passwords are stored as hashes. An account is unable to sign in until an administrator approves it. The admin screen asks the reviewer to confirm that the Society's required checks have been completed.

Only active administrator accounts see the Manage Members and Access Requests navigation links, and the corresponding routes enforce administrator access. An administrator can promote an already-approved active member to administrator. Member IDs link to an administrator-only profile with application, consent, membership-term, and payment history.

Optional newsletter consent and volunteer-opportunity contact consent are recorded separately from essential membership communications. Each declaration and optional preference is recorded with a version and timestamp. The Society must provide a process for changing or withdrawing optional preferences before sending those messages.

Approving a new application creates a one-year membership term. Adult dues are recorded as $10 unpaid until an administrator records payment; youth dues are recorded as waived. Admins can record a payment/reference and renew expired terms. Term history preserves prior start dates for eligibility review. Voting eligibility is calculated from current age (19 or older) and approved account status; accounts without a date of birth are shown as unknown. The bylaws' more-than-one-year director eligibility requirement is separate. The generated member number is based on the account's primary key.

The `/privacy` page is a draft disclosure, not a substitute for Society-approved privacy policy. Before launch, approve the notice, retention schedule, privacy contact, guardian-consent process, membership-term rules, and director eligibility rules. Application acknowledgements are not recreational waivers.

The constitution supplied so far contains the Society name and purposes, but does not state membership eligibility, dues, payment confirmation, or approval authority. Before public launch, confirm the Society's membership and approval policy and adjust the access request and admin review flow to match. Dues are not collected online. Declined-request retention and reapplication rules are also not yet decided.

## Next steps

Before launch, provide approved public contact details, logo/colors/photos, any initial notices, and the Society's membership/approval policy. Configure and test the email delivery service securely in Railway. Member documents, the member directory, notice editing, and online dues payments are outside this first implementation step.
