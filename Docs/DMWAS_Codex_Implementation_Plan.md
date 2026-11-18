# MWAS Community Hub — Codex Implementation Plan

## How to use
Give Codex the Master Instruction first. Then provide one phase at a time. Do not allow Codex to proceed to the next phase until tests and acceptance criteria pass. Codex must inspect the existing Flask/PostgreSQL/Railway repository before proposing code changes. Do not provide production secrets or database exports containing personal data.

## Master Instruction
You are extending the existing Mount Waddington Access Society (MWAS) website with a Community Hub. The existing stack is Flask, PostgreSQL and Railway. Reuse the existing authentication, membership records, database conventions, front-end patterns and deployment configuration. Do not rewrite working components, create duplicate user accounts, or introduce a separate authentication service. Favor a modular monolith, following the repository's current patterns.

The Hub supports: (1) member profiles and an opt-in adult member directory with granular privacy controls; (2) public activity listings, adult-member proposals requiring administrator approval, members-only registration, organizer approvals, waitlists, attendance and volunteer hours; (3) accepted adult-to-adult connections, text-only private messages, unread counts, grouped email digests, blocking, reporting and moderation.

Rules: publicly viewable activities, but only active members may register; only active adult members may propose activities or use adult networking; youth accounts are excluded from adult discovery and messaging. Existing public programs must remain unaffected. Activity organizers see only registration-relevant participant data, not unrestricted private profiles. Administrators cannot browse private conversations by default. Expired members may read existing conversations but not send or receive new messages until renewal. No read receipts. Display names are member-selected; verified identity stays private. Directory participation is explicit opt-in, and individual fields are independently controlled. Members can selectively publish their own verified activity and volunteer highlights. Co-organizers are invited by primary organizers and approved by administrators.

Engineering rules: inspect before modifying; describe intended files and migrations; preserve existing routes and UI; use server-side authorization on every protected endpoint and database query; use CSRF protection for browser mutations; validate inputs and upload types; rate-limit sensitive actions; use parameterized queries/ORM; do not log sensitive message contents; use idempotent jobs and transactional capacity updates; avoid unnecessary dependencies. Implement migrations with a tested rollback or documented safe recovery. Use staging and a tested backup before production changes. Add automated tests for access, privacy, membership expiry, waitlist concurrency, moderation and negative cases. Never deploy to production without explicit approval. At the end of each phase report changed files, migrations, tests and results, known risks, and next phase. Stop if required details are unknown rather than guessing.

## Phase 00 — Repository Audit and Design
Inspect application entry points, Flask blueprints, templates/static assets, models, ORM, login/session handling, roles, age eligibility, membership/payment/expiry logic, email, jobs, storage, Railway config, migrations, tests, backup and deployment processes. Produce `docs/community_hub/architecture_audit.md`, `requirements.md`, `data_model_proposal.md`, `risk_register.md` and `implementation_backlog.md`. Identify reuse points and unknowns. Do not modify production functionality or add features. **Gate:** owner reviews findings and signs off on integration plan.

## Phase 01 — Safe Development Foundation
Set up or document separate staging app/database, migration framework using existing conventions, test fixtures with synthetic users, CI checks, backup/restore verification, rollback instructions and environment variable validation. Ensure production secrets never enter source control. **Gate:** staging deploy, test run, migration up/down or recovery rehearsal, and restoration process documented.

## Phase 02 — Membership and Authorization
Implement a centralized permission policy/service using existing user and membership records. Capabilities: public activity browsing; authenticated account management; active-member registration; active-adult activity proposals and directory/messaging; organizer access scoped to their own activity; administrator review/moderation; expired-member read-only existing conversations. Do not infer adult status from a display name or editable profile field; define reliable eligibility source and youth policy. Protect routes and service/database operations, not just navigation. **Gate:** role/status matrix tests including unauthorized direct API requests and membership changes.

## Phase 03 — Hub Shell
Add navigation, member dashboard and empty states, accessible mobile-responsive pages, account renewal prompts and administrator navigation using existing design system. Keep unfinished functions disabled or hidden. **Gate:** public pages unchanged, authorization tests pass, no dead links.

## Phase 04 — Profiles and Privacy
Add community profile tied to existing user ID; display name, optional photo, biography, general community, standardized interests, participation goals, independent visibility controls and directory opt-in (off by default). Keep legal identity/email private. Implement profile edit, preview, moderation and data deletion/retention hooks. Privacy must apply to API responses and searches, not only templates. Restrict youth accounts from adult discovery. **Gate:** tests for hidden fields, direct URLs, search leaks, opt-out, profile moderation, and ownership.

## Phase 05 — Directory and Highlights
Adult-only authenticated directory, name and interest search, optional community/goal filters, only opted-in profiles, pagination, rate limits and safe empty states. Allow members to select their own verified activity and volunteer records for profile highlights; private/sensitive activities must not leak. **Gate:** privacy, filtering, permissions and query-performance tests.

## Phase 06 — Activity Proposals and Approval
Activity drafts with conditional fields for category, dates/time zone, location, capacity, accessibility, equipment, requirements, safety information, registration mode, organizer and official-vs-member classification. Adult members submit for admin review; admins approve, reject, request changes, unpublish. Primary organizers invite co-organizers subject to admin approval and scoped permissions. Record audit trail and version significant changes for reapproval. Public listings must not expose participant details or unnecessary precise locations. Existing public programs remain separate. **Gate:** state-transition, permission, validation, audit and public listing tests; insurer/policy review gate before live member-organized events.

## Phase 07 — Registration, Waitlists, Attendance, Volunteering
Members-only registration with immediate or organizer-approved modes; membership check at registration and before activity. Enforce capacity transactionally; FIFO waitlist with eligible-member offers, expiration and promotion jobs. Default offer duration of 24h is provisional and must be owner-approved; define close-to-start behavior, renewal grace and cancellation deadlines before enabling production. Organizers/co-organizers confirm attendance, manage roster with minimum necessary data, and submit hours; admins verify hours and preserve correction history. Notifications for confirmation, requests, changes and cancellations. **Gate:** concurrent registration tests, waitlist idempotency, expiry tests, permissions and reporting accuracy.

## Phase 08 — Connections and Messaging
Adult opt-in-directory discovery and connection requests, accept/decline/disconnect/block, anti-spam limits, no repeated unwanted requests, one-to-one text-only messaging between eligible accepted connections, unread counts for recipient, no read receipts or presence indicators, no attachments. Group unread email digests without message bodies; digest frequency configurable. Expired members can read pre-existing conversations, cannot send/receive new messages, and cannot access directory. Moderation uses member-submitted reports, scoped evidence review, audit trail, and designated administrators; no unrestricted admin message browsing. Establish retention/deletion and escalation policy before launch. **Gate:** ownership/access tests, block/disconnect, youth/expiry, rate limiting, digest idempotency, reporting and data retention tests.

## Phase 09 — Release Readiness
Run full functional regression, security/privacy review, accessibility and mobile testing, performance and load tests, email deliverability checks, backup/restore and rollback rehearsal. Confirm moderation staffing, incident response, privacy policy, terms, activity organizer guidance, insurance review and youth policy. Pilot with a small invited adult member group; collect feedback and resolve launch blockers. Deploy only after explicit owner approval. **Gate:** signed acceptance checklist and rollback plan.

## Cross-cutting acceptance criteria
- Existing website authentication and membership payments/renewals are not broken.
- Every protected endpoint enforces authorization server-side.
- Hidden/opted-out profiles never appear in directory responses or search results.
- Youth cannot use adult directory, connections or messaging.
- Capacity is never exceeded under concurrent requests.
- Activity organizer access is scoped to assigned activities and required registration data.
- No private message body is sent in email notifications or ordinary application logs.
- Membership expiry changes privileges promptly and preserves permitted read-only history.
- All significant administrative changes are audited.
- No production deploy without tested backup, rollback and owner approval.

## Open decisions requiring owner sign-off
1. Existing login, membership, expiry and age/guardian models.
2. Existing ORM, migrations, email provider and worker infrastructure.
3. Exact organizer/participant safety and liability policy; insurer review.
4. Waitlist offer duration, near-start handling and renewal grace.
5. Privacy retention/deletion schedule, message report handling and moderator staffing.
6. Youth activity registration/guardian permissions.
7. Public listing location granularity and accessibility fields.
8. Notification frequency, opt-out preferences and email service limits.

## Codex response format after each phase
1. Findings and assumptions.
2. Files added/modified.
3. Database migrations and rollback steps.
4. Tests executed and actual results.
5. Security/privacy considerations.
6. Remaining blockers and questions.
7. Explicit statement whether the phase acceptance gate is met.
