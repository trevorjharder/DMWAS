# Community Hub requirements baseline

**Status:** Draft derived from `Docs/DMWAS_Codex_Implementation_Plan.md` on `main`; pending Society owner review. The plan file is not checked into this local working tree. This document is a requirements baseline, not approval to deploy or expose features publicly.

## Product scope

Extend the existing DMWAS Flask site as a modular monolith, reusing current login, user identity, membership and deployment patterns. Do not duplicate accounts or introduce a second authentication service. The Hub comprises:

1. Member profiles and an adult member directory with explicit opt-in and independent field-level visibility.
2. Public activity listings; adult-member activity proposals subject to administrator review; members-only registration; organizer approvals; waitlists; attendance and volunteer-hour records.
3. Accepted adult-to-adult connections and text-only private messages, unread counts, grouped email digests, blocking, reporting and moderation.

Existing public programs must remain unaffected.

## Authorization and privacy requirements

- Public users may browse approved public activity listings; only active members may register.
- Only active adults may propose activities or participate in adult directory/networking features.
- Youth accounts must not be discoverable in the adult directory and must not use adult connections or messaging.
- Expired members may read existing conversations but may not send or receive new messages; they cannot access the directory. Re-check status at the time of each protected operation.
- Directory participation is explicit opt-in, off by default. Display name is member-selected; verified identity remains private. Each optional profile field has its own visibility setting.
- Only members may publish their own verified activity/volunteer highlights, with explicit selection; private or sensitive participation must not leak.
- Activity organizers/co-organizers see only registration data needed for their assigned activity, not full/private profiles. Co-organizers require primary-organizer invitation and administrator approval.
- Administrators cannot browse private conversations by default. Reports provide scoped evidence access; all review actions are audited.
- All authorization is enforced server-side on every route/service/query. Browser mutations require CSRF protection. Validate uploads and inputs; rate-limit sensitive actions; use ORM/parameterized queries; never log private message bodies.

## Feature requirements

### Hub shell

Add accessible, responsive navigation and member dashboard/empty states, renewal prompts and appropriate administrator navigation. Hide or disable unfinished functions; preserve public pages and existing routes.

### Profiles and directory

Profiles are tied to the existing user ID. Candidate fields: member-selected display name, optional photo, biography, general community, standardized interests, participation goals and independent visibility choices. Legal identity/contact details stay private. Include edit/preview, moderation and retention/deletion hooks. Directory is authenticated, adult-only, paginated, searchable by name/interests with optional community/goal filters, and limited to opted-in profiles. Do not leak hidden profiles/fields through queries or response payloads. Add rate limits and safe empty states.

### Activities and registration

Activity proposal fields include category, date/time/time zone, location, capacity, accessibility, equipment, requirements, safety information, registration mode, organizer and official-vs-member classification. Member proposals require adult status and admin review. Significant edits require reapproval and an audit/version history. Public listings must not expose participant details or unnecessary precise locations. Existing programs remain separate.

Registration supports immediate or organizer-approved modes, transactional capacity enforcement and FIFO waitlisting. Promotion offers expire; notifications cover confirmations, requests, changes and cancellations. Attendance and volunteer hours are recorded by authorized organizers, with admin verification and correction history. Roster visibility is minimized.

### Connections and messaging

Adult directory discovery may initiate requests. Support accept/decline/disconnect/block; prevent repeated unwanted requests and add anti-spam limits. Messaging is one-to-one, text-only and only between eligible accepted connections; no attachments, read receipts or presence indicators. Show recipient unread counts. Digests are grouped, configurable and contain no message bodies. Expired members retain permitted historical read-only access. Implement member reports, scoped evidence review, audit trail and defined retention/escalation policy before launch.

## Engineering and release requirements

- Inspect and preserve current application behavior; document intended files and migrations before each phase.
- Establish a staging app/database, migration approach, synthetic test fixtures, CI checks, backup/restore verification, rollback/recovery instructions and environment validation.
- Use idempotent jobs and transactional capacity updates. Test rollback or document safe recovery for schema changes.
- Automated coverage must include authorization, privacy, membership expiry, waitlist concurrency/idempotency, moderation and negative cases.
- Release readiness includes full regression, security/privacy review, accessibility/mobile, performance/load, email, backup/restore and rollback rehearsal, moderation staffing, policy/legal review and small invited adult pilot.
- Never deploy to production without explicit owner approval.

## Open decisions

See `risk_register.md`. At minimum, resolve age/guardian policy, membership standing/payment semantics, waitlist/near-start behavior, renewal grace, youth activity rules, retention/deletion, message reporting/moderation, location granularity, notification preferences, staging and recovery ownership before enabling the affected features.
