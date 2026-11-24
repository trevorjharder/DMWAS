# Community Hub Phase 08 — Connections and messaging

## Member workflows

- Active adult members open an opted-in profile in **Search Members** and send a
  connection request. The requester shares their chosen community display name
  with the recipient. The recipient can accept, decline, or block. Withdrawn,
  declined, disconnected and blocked pairs cannot send repeated requests.
- **Messages** lists one-to-one conversations and the recipient's own unread
  counts. A conversation becomes available once the recipient accepts. Open the
  conversation to send a text message or mark displayed messages as read. An
  expired adult can sign in to read existing conversations. Both participants
  need a current eligible membership for new messages.
- Blocking closes the connection and hides each profile from the other's
  directory results and co-host picker. Unblocking clears the block and leaves
  the previous connection closed. Existing conversation history remains
  subject to its ordinary retention period.
- **Email reminders** are opt-in. A digest contains a grouped unread count and
  sign-in instructions. It is sent only for new unread activity in eligible
  connections. If a delivery fails, retries use the same snapshot and
  idempotency key within 23 hours. [Resend idempotency keys](https://resend.com/docs/dashboard/emails/idempotency-keys)

## Moderation and retention

- A participant can submit a reason, optional context and up to five selected
  messages from a conversation. The case stores copies of those messages.
  A participant can submit one open report per conversation at a time.
- The **Message reports** queue is limited to active administrators on the
  designated moderator list. Cases involving the moderator are excluded.
  The queue has its own evidence view and records who opened the evidence,
  who closed the report, when, and the decision note.
- The retention job removes ordinary messages past their configured age. Open
  cases retain their selected evidence. Closed cases, their evidence and their
  review history are removed together after their configured period.
  Account deletion removes authored chat, digest preferences and identifying
  account links to retained evidence. The remaining member can read their own
  retained chat with a former-member label. Minimal connection state prevents
  renewed unwanted requests while both accounts exist.
- Shared rate limits allow five connection requests and five reports per UTC
  day, and 30 messages per minute per account. Blocking remains available
  independently of these limits.

Phase 08 restored the previously tested Phase 05 directory and profile privacy
paths used by member discovery. Phase 08 uses the existing membership system
and Resend delivery integration.

## Configuration and jobs

The feature is enabled for local development and disabled by default on
deployed services. Approve retention, digest frequency, moderation staffing,
escalation procedures and backup handling before enabling it for members.

| Setting | Provisional local value | Purpose |
| --- | --- | --- |
| `COMMUNITY_MESSAGING_ENABLED` | `true` | Expose messaging and moderation workflows. |
| `MESSAGING_DIGEST_INTERVAL_HOURS` | `24` | Minimum interval between unread email reminders. |
| `MESSAGING_MESSAGE_DAYS` | `180` | Age at which ordinary private messages are eligible for removal. |
| `MESSAGING_CLOSED_REPORT_DAYS` | `365` | Age from case closure at which reports and evidence are eligible for removal. |
| `MESSAGING_MODERATOR_IDS` | Empty | Comma-separated administrator account IDs for case review. |

Set every policy value explicitly when enabling a deployed service. Configure
Resend with the existing `RESEND_API_KEY` and `RESEND_FROM` settings for email.
Run the digest command on a schedule using the web service's database and
configuration. Confirm delivery in Testing with consenting synthetic accounts.
Preview retention first and schedule its application after approval.

```powershell
python -m flask --app app send-community-message-digests
python -m flask --app app prune-community-messaging
python -m flask --app app prune-community-messaging --apply
```

## Migration and recovery

Revision `b08e5f6a7b8c` follows `a07d4e5f6a7b` and adds connections,
private messages, report evidence and audit history, quotas, preferences and
digest delivery state. Back up and verify the Testing database, confirm the
database reference and migrate before starting the new application.

```powershell
python -m flask --app app db upgrade
python -m flask --app app db check
```

To roll back, stop messaging traffic and jobs, preserve a protected backup of
Phase 08 data, run `python -m flask --app app db downgrade a07d4e5f6a7b`, and
restore the Phase 07 application. The downgrade drops Phase 08 records and
preserves users, profiles, activities and registrations. A fresh upgrade
creates empty Phase 08 tables. Restore the protected backup to recover them.

## Acceptance

The shared checkout passed all 187 tests, including 15 Phase 08 tests, with
five existing SQLite expression-index reflection warnings. Disposable
PostgreSQL 18 passed concurrent connection requests, unread digest claims,
restricted evidence review, blocking, schema checking, and upgrade,
downgrade and re-upgrade. Digest delivery and retry tests used a mock provider.

Run `python -m pytest tests` and confirm the migration and rollback against a
disposable PostgreSQL database. In Testing, check member opt-in discovery,
acceptance, blocking, expired-member history, unread counts, digest delivery
and report evidence access. Verify the moderator allowlist and escalation
process before enabling live messaging. Phase 09 covers release readiness.
