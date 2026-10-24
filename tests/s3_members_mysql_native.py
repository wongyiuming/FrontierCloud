"""Actual MySQL S3 configuration and concurrent namespace admission, no bucket I/O."""
import asyncio
import json
import os
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db
from app.services import s3_schema as s
from app.services.federation.state import State
from app.services.s3_members import Conflict, Members
from app.services.s3_signing import Credentials, Location


async def main():
    url = make_url(os.environ['FC_DB_MYSQL_URL'])
    if os.environ.get('FC_DB_MYSQL_RUNTIME') != '1' or url.host != 'mysql' or url.database != 'frontiercloud_db_test':
        raise RuntimeError('Refusing S3 fixture outside disposable mysql/frontiercloud_db_test')
    started = time.monotonic()
    database = create_async_engine(url,pool_size=3,max_overflow=0)
    try:
        async with database.connect() as conn:
            for name in await db._table_names(conn):
                if not name.replace('_','').isalnum():
                    raise RuntimeError('unsafe fixture table')
                await conn.execute(text(f'DROP TABLE `{name}`'))
                await conn.commit()
        with patch.object(db,'engine',database):
            await db.init_db()
        key = Fernet.generate_key()
        store = State(database,key)
        await store.initialize()
        async with database.begin() as conn:
            await conn.execute(text("UPDATE node_identity SET role='Master'"))
        members = Members(store)
        location = Location('mysql-private-bucket','us-east-1',prefix='objects')
        credentials = Credentials('fixtureAccess123','private-fixture-secret-not-logged')
        values = await asyncio.gather(*[members.add('Archive',location,credentials,1024**3,'native') for _ in range(2)],return_exceptions=True)
        assert sum(isinstance(value,Conflict) for value in values) == 1
        member = next(value for value in values if isinstance(value,str))
        checks = ['parallel_namespace_one_owner']
        row = (await members.list())[0]
        assert row['physical_free_bytes'] is None and row['traffic_bytes_per_second'] is None
        assert not row['enabled'] and row['probe_state'] == 'unchecked'
        checks.append('no_fabricated_disk_or_traffic')
        try:
            await members.configure(member,1,name='Archive',quota=1024**3,enabled=True,actor='native')
        except Conflict:
            checks.append('unprobed_enable_refused')
        else:
            raise AssertionError('missing_probe_guard')
        revision = await members.probed(member,1)
        revision = await members.configure(member,revision,name='Archive',quota=1024**3,enabled=True,actor='native')
        checks.append('probed_revision_enables_member')
        async with database.begin() as conn:
            await conn.execute(insert(s.uploads).values(session_id='a'*32,member_id=member,
                master_id=store.node['node_id'],media_id='b'*64,object_id='c'*64,path_locator='d'*64,
                media_path='music/album/song.mp3',object_key='opaque',state='uncertain',expected_bytes=123,
                part_bytes=8*1024**2,part_count=1,credential_revision=revision,created_at=1000,updated_at=1000,expires_at=1300))
            await conn.execute(update(s.members).values(reserved_bytes=123))
        try:
            await members.configure(member,revision,name='Archive',quota=1024**3,enabled=False,actor='native',
                credentials=Credentials('replacement123','another-private-secret-value'))
        except Conflict:
            checks.append('uncertain_upload_freezes_credentials')
        else:
            raise AssertionError('missing_uncertain_upload_guard')
        try:
            await members.configure(member,revision,name='Archive',quota=122,enabled=False,actor='native')
        except Conflict:
            checks.append('reserved_quota_preserved')
        else:
            raise AssertionError('missing_quota_guard')
        restarted = Members(State(database,key))
        signer,seen = await restarted.signer(member)
        assert seen == revision and signer.credentials == credentials
        checks.append('restart_vault_identity_preserved')
        async with database.connect() as conn:
            sealed = await conn.scalar(select(s.members.c.credentials_sealed))
            audit = str(list((await conn.execute(text('SELECT detail FROM node_audit'))).tuples()))
        assert credentials.secret_key not in sealed+audit+str(row)
        assert credentials.access_key not in audit+str(row)
        checks.append('secrets_not_in_status_audit_or_plaintext_database')
        print(json.dumps(dict(format='frontiercloud-python-mysql-s3-members',version=1,count=len(checks),checks=checks,
            seconds=round(time.monotonic()-started,4),bucket_contacted=False,browser_acceptance=False),sort_keys=True))
    finally:
        await database.dispose()


if __name__ == '__main__':
    asyncio.run(main())
