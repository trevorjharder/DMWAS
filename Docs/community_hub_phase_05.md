# Community Hub Phase 05 — Member Directory

## Scope and decisions

The directory uses the existing accounts, membership policy, profiles, automatic
profile approval, CSRF and design system.

## Behavior and privacy

- `/members/search` is the adult-only directory. `/api/community/profiles` exposes
  the same filtered projection for authenticated active adults. Display-name
  search is case-insensitive and treats SQL wildcard characters literally.
- Interest, general-community and participation-goal filters only match fields
  the profile owner selected for sharing. Interests/goals use exact standardized
  array membership. Legal names, email, addresses, biography and verified identity
  are never searched. Filter controls use standard choices rather than revealing
  private member values in suggestions.
- Both SQL queries and direct profile URLs require directory opt-in, approved
  profile status, active account, recorded adult birth date, and a current paid or
  waived latest membership term. The existing policy for legacy active accounts
  with no term history is preserved. Eligibility is filtered before pagination.
- Pages contain at most 20 profiles, with stable display-name/ID ordering and
  previous/next navigation. One extra row determines the next-page link; there is
  no unbounded count query. Search/filter inputs and page offsets are bounded.
- A composite index supports directory visibility and ordering. Photo presence
  is projected without loading image bytes.
- Discovery HTML, JSON and photo requests share a fixed UTC-minute quota of 60
  requests per account, stored in `directory_access_window`. PostgreSQL/SQLite
  atomic upsert enforces the quota across workers and sessions. Excess requests
  receive 429, a retry interval and a safe message. One quota row per account is
  reused; account deletion removes it. No search text or viewing history is kept.

## Files

- `app.py` provides directory queries, JSON responses and shared quotas.
- `templates/member_search.html`, `directory_rate_limit.html`: search, filters,
  pagination, result cards and safe empty/rate-limit states.
- Existing base/hub/profile/review/privacy templates and `static/styles.css`:
  navigation, responsive layout, selection controls and privacy disclosure.
- `tests/test_directory.py` covers privacy, filtering, authorization, pagination,
  query count and quotas.
- `scripts/validate_phase05_postgres.py`: disposable localhost PostgreSQL smoke
  and migration/rollback check with synthetic data only. Set `POSTGRES_BIN` if
  PostgreSQL binaries are not in the default Windows PostgreSQL 18 location.

## Migration and recovery

Phase 05 revision `d05a1b2c3d4e` follows `c04f1a2b3d4e`. No existing directory
consent or profile approval is changed.

Before deployment to the existing **Testing** environment, take and verify a
fresh backup, confirm its database reference, and check the current revision:

```powershell
python -m flask --app app db current
python -m flask --app app db upgrade
python -m flask --app app db check
```

Run migrations **before** starting the application. Railway's previously
configured custom Start Command skips the repository Procfile migration step.
Use the existing Procfile or, after backup and revision checks, set the Testing
Start Command to:

```sh
python -m flask --app app db upgrade && exec gunicorn --workers 2 --threads 4 --bind 0.0.0.0:$PORT 'app:create_app()'
```

Do not stamp `head` to work around a missing table. The Phase 04 repair already
adopted the existing Testing baseline; verify that remains true rather than
repeating baseline adoption blindly. The initial implementation did not modify
Railway settings or migrate a live database. The subsequent Testing repair below
records the later database and startup-command changes.

Revision `c09f6a7b8c9d` permanently removes the former profile highlights,
their source records, participant links, and audit history. Downgrading this
revision recreates empty tables. Recovery of deleted rows requires a database
backup from before the migration.

## Verification and gate

The automated suite exercises hidden-field filtering, literal wildcards, latest
term/youth/expiry/status restrictions, direct HTML/JSON authorization, stable
pagination, a maximum of ten SELECTs for a page of twenty profiles, and shared
quotas across sessions and endpoints.

The disposable PostgreSQL check covers JSON filters, directory and profile
pages, quotas, and schema agreement after upgrade. It also checks that
downgrade and re-upgrade preserve synthetic users and profiles. SQLite migration
tests report known expression-index reflection warnings. The PostgreSQL check
validates the directory index.

Run the checks with:

```powershell
python -m pytest -q --tb=short -p no:cacheprovider
python scripts/validate_phase05_postgres.py
```

## Subsequent Testing repair

After the Phase 05 application was deployed without its migration, `/member`
failed because the database schema was behind the application. The existing
Testing web service and database reference were verified; the database was on
`c04f1a2b3d4e` and matched the expected pre-Phase-05 schema.

A fresh custom-format PostgreSQL backup was saved under the gitignored path
`instance/backups/testing-before-phase05-20261118T032201Z.dump` (67,730 bytes;
SHA-256 `5282d3ac5a6531e457124e39daef03d275d153b469255b02afcd040c0623b077`).
The backup archive listing was validated. The database was upgraded to
`d05a1b2c3d4e`; PostgreSQL drift checking passed and existing record counts were
unchanged. Read-only application checks against Testing returned 200 for
`/member` and `/member/profile/preview`.

The previously approved temporary Testing database endpoint was removed, and its
proxy list was confirmed empty. An environment-specific configuration patch then
set Testing's web Start Command to the migration-first command above, with
`skipDeploys: true`. The saved command was verified afterward, and Production's
web Start Command was confirmed unchanged. No deployment or Production database
change was performed. The existing running Testing application can use the
repaired schema immediately; the saved startup command applies on future starts
using that configuration. A full backup restore rehearsal remains outstanding.
