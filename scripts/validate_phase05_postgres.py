"""Disposable localhost-only PostgreSQL validation with synthetic records."""
from pathlib import Path
import os
import secrets
import socket
import subprocess
import sys
from datetime import date

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.chdir(root)
bin_dir = Path(os.environ.get('POSTGRES_BIN', 'C:/Program Files/PostgreSQL/18/bin'))
workspace = root / 'instance' / ('phase05-pg-' + secrets.token_hex(4))
workspace.mkdir()
password = secrets.token_urlsafe(24)
pwfile = workspace / 'password.txt'
pwfile.write_text(password)
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]

def run(name, *args):
    if os.name != 'nt':
        name = name.removesuffix('.exe')
    print('Running', name, args[-1] if name == 'pg_ctl.exe' else '', flush=True)
    with (workspace / 'operator.log').open('a') as output:
        result = subprocess.run([str(bin_dir / name), *args], stdout=output, stderr=output,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=60)
    if result.returncode:
        print((workspace / 'operator.log').read_text())
        raise RuntimeError(name + ' failed')

started = False
try:
    run('initdb.exe', '-D', str(workspace / 'data'), '-U', 'phase05_test', '-A', 'scram-sha-256',
        '--pwfile', str(pwfile), '--encoding=UTF8', '--locale=C')
    pwfile.unlink()
    run('pg_ctl.exe', '-D', str(workspace / 'data'), '-l', str(workspace / 'server.log'),
        '-o', f'-h 127.0.0.1 -p {port}', '-w', 'start')
    started = True
    from sqlalchemy.engine import URL
    from sqlalchemy import text
    from app import (create_app, db, User, MembershipDetails, CommunityProfile,
                     VerifiedCommunityRecord, CommunityProfileHighlight, VerifiedCommunityRecordParticipant)
    from flask_migrate import upgrade, downgrade, check
    url = URL.create('postgresql+psycopg2', username='phase05_test', password=password,
                     host='127.0.0.1', port=port, database='postgres')
    app = create_app({'TESTING': True, 'SECRET_KEY': 'synthetic-local-test', 'SQLALCHEMY_DATABASE_URI': url})
    with app.app_context():
        upgrade(revision='c04f1a2b3d4e')
        # Phase 04 table shape predates the new column, so seed via SQL.
        user_id = db.session.execute(text("""INSERT INTO "user" (full_name,email,username,password_hash,status,is_admin,created_at)
            VALUES ('Synthetic Test','synthetic@example.test','synthetic','unused','active',true,now()) RETURNING id""")).scalar_one()
        db.session.commit()
        upgrade(revision='d05a1b2c3d4e')
        legacy_id = db.session.execute(text("""INSERT INTO verified_community_record
            (user_id,title,record_type,occurred_on,verified,shareable,sensitive,verified_by_id,verified_at)
            VALUES (:member,'Legacy synthetic contribution','activity','2026-01-01',true,true,false,:member,now())
            RETURNING id"""), {'member': user_id}).scalar_one()
        db.session.commit()
        upgrade()
        check()
        assert db.session.get(VerifiedCommunityRecord, legacy_id).participants[0].user_id == user_id
        user = db.session.get(User, user_id)
        user.membership_details = MembershipDetails(first_name='Synthetic', last_name='Test',
            community='Test area', date_of_birth=date(1990,1,1), age_19_or_older=True,
            mailing_address='Synthetic only', membership_type='adult', activities=[])
        profile = CommunityProfile(user_id=user_id, display_name='Test Alias', directory_opt_in=True,
            status='approved', interests=['Hiking'], show_interests=True,
            participation_goals=['Join Society activities'], show_goals=True, show_highlights=True)
        db.session.add(profile)
        record = VerifiedCommunityRecord(user_id=user_id, title='Synthetic volunteer contribution',
            record_type='volunteer', occurred_on=date(2026,1,1), volunteer_minutes=60,
            verified=True, shareable=True, sensitive=False, verified_by_id=user_id)
        db.session.add(record)
        db.session.flush()
        profile.highlights.append(CommunityProfileHighlight(record_id=record.id))
        db.session.commit()
    with app.test_client() as client:
        with client.session_transaction() as session:
            session['user_id'] = user_id
        response = client.get('/api/community/profiles?interest=Hiking&goal=Join+Society+activities')
        assert response.status_code == 200, response.status_code
        assert len(response.json['profiles']) == 1, response.json
        assert len(response.json['profiles'][0]['highlights']) == 1
        assert client.get('/members/search?interest=Hiking').status_code == 200
        assert client.get('/admin/community/records').status_code == 200
        assert client.get('/member/profile').status_code == 200
        app.config['DIRECTORY_REQUESTS_PER_MINUTE'] = 2
        assert client.get('/api/community/profiles').status_code == 429
    with app.app_context():
        # Refuse a downgrade that would silently drop another member's contribution.
        other = User(full_name='Synthetic Second', email='second@example.test', username='second',
                     password_hash='unused', status='active')
        db.session.add(other)
        db.session.flush()
        other_id = other.id
        shared = db.session.get(VerifiedCommunityRecord, legacy_id)
        shared.participants.append(VerifiedCommunityRecordParticipant(user_id=other.id))
        db.session.commit()
        rejected = app.test_cli_runner().invoke(args=['db', 'downgrade', 'd05a1b2c3d4e'])
        assert rejected.exit_code != 0, 'Shared-record downgrade must be rejected'
        db.session.remove()
        shared = db.session.get(VerifiedCommunityRecord, legacy_id)
        assert len(shared.participants) == 2
        shared.participants = [p for p in shared.participants if p.user_id == user_id]
        db.session.flush()
        db.session.delete(db.session.get(User, other_id))
        db.session.commit()
        downgrade(revision='c04f1a2b3d4e')
        assert db.session.execute(text('SELECT count(*) FROM "user"')).scalar_one() == 1
        assert db.session.execute(text('SELECT count(*) FROM community_profile')).scalar_one() == 1
        db.session.commit()  # Release read locks before Alembic uses a separate connection.
        upgrade()
        check()
        db.session.remove()
        db.engine.dispose()
    print('PASS: PostgreSQL 18 migration/backfill, shared-record downgrade guard, drift check, JSON filters, highlights, forms, quota and safe downgrade/re-upgrade.')
finally:
    if started:
        run('pg_ctl.exe', '-D', str(workspace / 'data'), '-m', 'fast', '-w', 'stop')
    if pwfile.exists():
        pwfile.unlink()
