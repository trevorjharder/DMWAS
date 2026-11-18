# Phase 5 testing checklist

Use the existing Railway **Testing** environment and synthetic accounts/records.
Check a box only after observing the expected result. Record failures at the end.

Build/commit: __________  Tester: __________  Date: __________

## 1. Prepare Testing

- [ ] Confirm the web service uses Testing's PostgreSQL database, not Production.
- [ ] Take and verify a fresh database backup before applying the migration.
- [ ] Run `python -m flask --app app db upgrade` before starting Phase 5.
- [ ] Run `python -m flask --app app db current`: revision is `d05a1b2c3d4e`.
- [ ] Run `python -m flask --app app db check`: no schema changes are detected.
- [ ] Confirm the start command applies migrations before Gunicorn starts. See [deployment instructions](community_hub_phase_05.md#migration-and-recovery).
- [ ] Prepare an active adult administrator, two active adult members (A and B), a youth member and an expired/inactive member. Give A a recognizable alias that differs from their legal name.
- [ ] Use separate browser profiles or browsers for A and B so their sessions remain separate.

## 2. Member directory

Test as B, viewing A's profile.

- [ ] Open **Member area → Browse member directory**: the directory loads without an error.
- [ ] Save A's profile with directory opt-in OFF: A does not appear.
- [ ] Turn A's opt-in ON and save: A appears automatically, without administrator approval.
- [ ] Edit A's display name and save: the new alias appears and remains approved.
- [ ] Search for all or part of the alias, using different capitalization: A is found.
- [ ] Search for A's legal name, email or biography, using values absent from the alias: A is not found.
- [ ] Share A's interest and filter by it: A appears. Hide that interest and repeat: A disappears from that filtered result.
- [ ] Repeat the visible/hidden test for **general community** and **participation goal**.
- [ ] Combine name, interest, community and goal filters: results satisfy every filter.
- [ ] Search for a nonexistent alias: a useful empty state appears.
- [ ] Clear filters: unfiltered results return.
- [ ] Search for `%` or `_`: these behave as literal characters, not "match everyone" wildcards.

## 3. Profile privacy and access

- [ ] With A's optional fields hidden, B sees only the alias and fields A explicitly shares. A's legal identity, email, phone, address, birth date and guardian details are absent.
- [ ] Show A's photo: B can view it. Hide it: B cannot retrieve it through its previously copied photo URL.
- [ ] Copy A's profile URL. Turn A's directory opt-in OFF: the directory listing disappears and the copied profile URL returns 404 for B.
- [ ] Opt A back in. As administrator, unpublish the profile: the listing and copied direct URL become unavailable.
- [ ] As youth, try `/members/search`: access is denied, and the adult directory navigation is hidden.
- [ ] As an expired/inactive member, try the directory and a copied profile URL: access is denied.
- [ ] Sign out and open the directory/profile URLs: authentication is required.
- [ ] Expire a synthetic listed member's membership: the profile disappears for other members, including through copied URLs.
- [ ] Confirm unpaid members and youth profiles do not appear, even if a profile was previously approved and opted in.

## 4. Administrator-verified records

Open **Verified records** as administrator. Use completed, synthetic activities.

- [ ] Add an activity record for A with a safe summary and past completion date. Check **verified** and **safe to share**: it becomes available for A to select.
- [ ] Add a volunteer contribution for A with positive whole minutes: the saved minutes are correct.
- [ ] Leave **safe to share** unchecked on a record: it is unavailable in A's highlight choices.
- [ ] Mark a record sensitive, even with **safe to share** checked: it is unavailable for sharing.
- [ ] Leave verification unchecked: the record is unavailable for sharing.
- [ ] Try a future completion date or invalid volunteer minutes: the form rejects the entry.
- [ ] As an ordinary member, open `/admin/community/records`: access is denied.
- [ ] Confirm an existing record's owner cannot be reassigned through the update endpoint (covered by automated tests).
- [ ] Confirm creation and edits create administrator/time audit snapshots (covered by automated tests; no audit-history screen is included).

## 5. Member-selected highlights

- [ ] As A, open **Community profile**: only A's eligible verified records are offered.
- [ ] Select a record but leave **Show my selected verified highlights** OFF: B cannot see it.
- [ ] Turn highlight visibility ON, keep directory opt-in ON and save: B sees the selected title, completion date, type and volunteer minutes where applicable.
- [ ] Open A's private preview: it shows the same shared highlight fields.
- [ ] Deselect one record and save: it disappears while other selections remain.
- [ ] Try selecting more than ten records: saving is rejected without changing the saved profile.
- [ ] Turn highlight visibility OFF: all selected highlights disappear from B's view.
- [ ] Turn it back ON, then revoke a selected record's verification as administrator: it disappears immediately, without A saving again.
- [ ] Repeat by making a selected record private, then sensitive: it disappears in both cases.
- [ ] Attempt to submit another member's record ID: saving is rejected (covered by automated ownership tests).
- [ ] Turn directory opt-in OFF: neither profile nor highlights remain accessible to B.

## 6. Pagination, rate limits and layout

- [ ] With at least 21 eligible opted-in synthetic profiles, the first page shows 20 and has a **Next page** link.
- [ ] Move forward/back: there are no duplicates or missing profiles. Reloading preserves stable ordering.
- [ ] Apply a filter, then change pages: the filter is preserved.
- [ ] Open a page beyond the results: a safe empty state appears.
- [ ] Make more than 60 discovery requests in one UTC-minute window using the same synthetic account: further requests return 429 with a retry interval. Directory pages, profile pages, JSON and photo requests share this quota.
- [ ] Open another session for the same account: it shares the limit. A different eligible account retains its own allowance.
- [ ] After the minute window resets, browsing works again.
- [ ] At a narrow phone width, filters, cards and navigation remain usable without horizontal page scrolling.
- [ ] Use only the keyboard: labels, filters, links, checkboxes, verification forms and page navigation are reachable and usable.

## 7. API and regression checks

Use B's authenticated session. Find A's numeric ID in the profile URL.

- [ ] `/api/community/profiles` returns only eligible, opted-in profiles and shared fields.
- [ ] `/api/community/profiles?interest=Hiking` obeys interest visibility, just like HTML search.
- [ ] `/api/community/profiles/<A-id>` omits hidden fields, identity/contact details and invalid highlights.
- [ ] After A opts out or becomes ineligible, the detail endpoint returns 404 and list results exclude A.
- [ ] Without an active adult session, both API endpoints return 403.
- [ ] Profile/directory responses use `Cache-Control: private, no-store`.
- [ ] Existing public pages, login, membership requests, renewals and administrator member management still work.
- [ ] On disposable synthetic data, deleting a profile removes highlight selections but preserves the verified source records and membership account.
- [ ] Deleting that synthetic account removes owned verified records, their audit snapshots and quota rows (also covered by automated tests).
- [ ] Application logs show no missing-table, missing-column or new server errors during these checks.

## 8. Automated checks and acceptance

- [ ] Run `python -m pytest -q`: all tests pass. Implementation baseline: **130 passed**, with five known SQLite expression-index reflection warnings.
- [ ] Run `python scripts/validate_phase05_postgres.py` with local PostgreSQL binaries available: real PostgreSQL filtering, quota and upgrade/downgrade/re-upgrade checks pass, and the temporary server stops. This uses synthetic localhost data, not Railway.
- [ ] If a live rollback rehearsal is required, use a disposable restored backup and the [recovery instructions](community_hub_phase_05.md#migration-and-recovery). Phase 5 downgrade deletes Phase 5 records, audits and selections; do not try it casually on Testing data you need to retain.
- [ ] Resolve and retest failures. Record the tested commit and evidence.
- [ ] Accept Phase 5 in Testing before moving on to Phase 6. Production promotion requires separate approval.

## Failures and retests

| Check | Account/role | Steps to reproduce | Expected / actual result | Fixed and retested |
|---|---|---|---|---|
| | | | | |
| | | | | |
| | | | | |

Testing acceptance: **PASS / FAIL / BLOCKED**

Outstanding items: ______________________________________________

Accepted by/date: _______________________________________________
