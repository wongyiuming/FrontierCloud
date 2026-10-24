"""Actual selected SQLite/WAL/SHM, secret inodes and bounded native census."""
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import text,update

from app.core import db
from app.core.joined_io import joined_io
from app.core.sqlite_compat import create_sqlite_engine
from app.services import karaoke_storage,media_manager,runtime_gate,upload_claims
from app.services.federation import schema as fs
from app.services.federation.state import State
from app.services.node_retirement_files import Authority
from app.services.node_retirement_fences import RetirementError
from app.services.node_retirement_gate import GateError
from app.services.node_retirement_runtime import Runtime


@unittest.skipUnless(os.name=='posix','native selected-inode census requires Linux')
class RetirementFilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.base=Path(self.temporary.name);self.data=self.base/'data';self.secrets=self.base/'secrets'
        self.paths={'data':self.data,'media':self.data/'media','recordings':self.data/'recordings'}
        for path in [*self.paths.values(),self.secrets]:path.mkdir(parents=True,exist_ok=True)
        self.database=create_sqlite_engine('sqlite+aiosqlite:///'+str(self.data/'selected.db'))
        with patch.object(db,'engine',self.database),patch.object(db,'SECRET_DIR',self.base/'unused'):await db.init_db()
        key=Fernet.generate_key();self.original_key=key;self.store=State(self.database,key);await self.store.initialize()
        self.vault=self.secrets/'node-vault.key';self.vault.write_bytes(key);self.vault.chmod(0o600)
        async with self.database.begin() as conn:await conn.execute(update(fs.identity).values(role='Follower',endpoint='https://native.4399.com'))
        self.store.node.update(role='Follower',endpoint='https://native.4399.com')
        self.native=runtime_gate.acquire(self.data);self.runtime=Runtime(self.data,self.native)
        self.authority=await self.capture()

    async def asyncTearDown(self):
        self.authority.close();self.runtime.close();self.native.close();await self.database.dispose();self.temporary.cleanup()

    async def capture(self):
        return await Authority.capture(self.runtime,self.store,paths=self.paths,secrets=self.secrets,upload_leases=self.data/'upload-leases')

    def base_files(self):return 5 if self.database.dialect.name=='sqlite' else 2

    async def inspect(self,*,first_page_only=False):
        async with media_manager.media_mutation_lock,karaoke_storage._write_lock,self.database.begin() as conn:
            with self.runtime.gate.lease(exclusive=True) as lease:
                census=await self.authority.census(conn,lease)
                try:
                    count=1
                    while not await joined_io(census.page):
                        if first_page_only:return None,count
                        count+=1
                    return census.summary(),count
                finally:census.close()

    async def test_actual_selected_database_sidecars_are_retained_and_double_roots_are_not_added_twice(self):
        self.assertNotIn(self.data/'frontiercloud.db',self.authority.files)
        result,_=await self.inspect()
        self.assertEqual(result['retained_files'],5)
        expected=sum((self.data/name).stat().st_size for name in ('selected.db','selected.db-wal','selected.db-shm'))
        self.assertEqual(result['retained_bytes'],expected)
        self.assertEqual(self.vault.read_bytes(),self.original_key)
        self.assertEqual(len(result['roots_sha256']),64)
        self.assertEqual((result['external_control_files'],result['external_control_bytes']),(1,44))

    async def test_unknown_large_payload_is_not_opened_hashed_or_removed(self):
        target=self.paths['media']/'unknown.bin'
        with target.open('wb') as stream:stream.truncate(100*1024**2)
        original=target.stat();read=os.pread
        def checked(descriptor,*args):
            self.assertNotEqual((os.fstat(descriptor).st_dev,os.fstat(descriptor).st_ino),(original.st_dev,original.st_ino),'business payload was read')
            return read(descriptor,*args)
        with patch('os.pread',side_effect=checked):
            with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):await self.inspect()
        self.assertEqual((target.stat().st_ino,target.stat().st_size),(original.st_ino,100*1024**2))

    async def test_same_byte_vault_replacement_is_not_the_startup_inode(self):
        original=self.vault.read_bytes();retained=self.vault.with_name('retained-original-vault');self.vault.rename(retained)
        self.vault.write_bytes(original);self.vault.chmod(0o600)
        with self.assertRaisesRegex(RetirementError,'retirement_control_file_changed'):await self.inspect()
        self.assertEqual(retained.read_bytes(),original);self.assertEqual(self.vault.read_bytes(),original)

    async def test_same_inode_vault_tampering_is_observed_without_regeneration(self):
        inode=self.vault.stat().st_ino;replacement=Fernet.generate_key();self.vault.write_bytes(replacement)
        with self.assertRaisesRegex(RetirementError,'retirement_control_bytes_changed'):await self.inspect()
        self.assertEqual(self.vault.stat().st_ino,inode);self.assertEqual(self.vault.read_bytes(),replacement)

    async def test_default_lyric_name_alone_cannot_be_added_after_startup(self):
        from app.services.lyrics import DEFAULT_LYRIC_CONTENT
        path=self.paths['media']/'lyrics/default.lrc';path.parent.mkdir();path.write_bytes(DEFAULT_LYRIC_CONTENT.encode());path.chmod(0o644)
        with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):await self.inspect()
        self.assertEqual(path.read_bytes(),DEFAULT_LYRIC_CONTENT.encode())

    async def test_actual_builtin_default_is_pinned_and_counted_only_once(self):
        from app.services.lyrics import DEFAULT_LYRIC_CONTENT
        path=self.paths['media']/'lyrics/default.lrc';path.parent.mkdir();path.write_bytes(DEFAULT_LYRIC_CONTENT.encode());path.chmod(0o644)
        self.authority.close();self.authority=await self.capture()
        result,_=await self.inspect();self.assertEqual(result['retained_files'],self.base_files()+1)
        self.assertEqual(path.read_bytes(),DEFAULT_LYRIC_CONTENT.encode())
        path.write_bytes(b'foreign lyric bytes')
        with self.assertRaises(RetirementError):await self.inspect()
        self.assertEqual(path.read_bytes(),b'foreign lyric bytes')

    async def test_idle_coordination_is_retained_but_foreign_lock_owner_blocks(self):
        import fcntl
        root=self.data/'upload-leases'
        with upload_claims.claim('a'*32,root=root):pass
        path=root/('a'*32);inode=path.stat().st_ino
        result,_=await self.inspect();self.assertEqual(result['retained_files'],self.base_files()+1)
        descriptor=os.open(path,os.O_RDWR)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(RetirementError,'retirement_coordination_busy'):await self.inspect()
        finally:os.close(descriptor)
        await self.inspect();self.assertEqual(path.stat().st_ino,inode);self.assertEqual(path.read_bytes(),b'')

    async def test_nonempty_or_unknown_named_coordination_file_is_never_accepted(self):
        root=self.data/'upload-leases';root.mkdir()
        for name,content in (('a'*32,b'unknown bytes'),('foreign',b'')):
            path=root/name;path.write_bytes(content);path.chmod(0o600)
            with self.assertRaises(RetirementError):await self.inspect()
            self.assertEqual(path.read_bytes(),content)
            path.rename(self.base/name)

    async def test_root_replacement_is_refused_with_old_resources_intact(self):
        old=self.paths['recordings'];retained=old.with_name('retained-recordings');old.rename(retained);old.mkdir()
        with self.assertRaisesRegex(RetirementError,'retirement_root_changed'):await self.inspect()
        self.assertTrue(retained.is_dir());self.assertTrue(old.is_dir())

    async def test_selected_sqlite_copy_does_not_replace_the_original_database_authority(self):
        path=self.data/'selected.db';original=path.read_bytes();retained=path.with_name('retained-selected.db');path.rename(retained)
        path.write_bytes(original);path.chmod(0o600)
        with self.assertRaisesRegex(RetirementError,'retirement_control_file_changed'):await self.inspect()
        self.assertEqual(retained.read_bytes(),original);self.assertEqual(path.read_bytes(),original)

    async def test_many_empty_directories_use_bounded_pages_and_leave_no_census_descriptors(self):
        root=self.paths['media']/'music';root.mkdir()
        for index in range(1024):(root/f'{index:04}').mkdir()
        before=len(os.listdir('/proc/self/fd'));result,pages=await self.inspect()
        self.assertGreater(pages,20);self.assertGreaterEqual(result['directories'],1027)
        self.assertEqual(len(os.listdir('/proc/self/fd')),before)
        self.assertEqual(len(list(root.iterdir())),1024)

    async def test_shared_or_closed_request_capability_cannot_start_physical_final_clearance(self):
        async with self.database.begin() as conn:
            with self.runtime.gate.lease() as shared:
                with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):await self.authority.census(conn,shared)
            with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):await self.authority.census(conn,shared)

    async def edge(self,rows=(),whitelist=()):
        from app.services import edge_security
        path=self.data/'.ip-security/active-bans.tsv'
        edge_security.write_snapshot(rows,whitelist,path)
        self.authority.close()
        with patch.object(edge_security,'SNAPSHOT_PATH',path):self.authority=await self.capture()
        return path

    async def ban(self,ip='203.0.113.8',kind='auto'):
        now=datetime.now(timezone.utc).replace(tzinfo=None);expiry=now+timedelta(days=1)
        async with self.database.begin() as conn:
            await conn.execute(text("INSERT INTO ip_auto_ban_events (ip_address,trigger_count,window_started_at,banned_at,expires_at,ban_kind,status) "
                "VALUES (:ip,1,:now,:now,:expiry,:kind,'active')"),dict(ip=ip,now=now,expiry=expiry,kind=kind))
        return dict(ip_address=ip,expires_at=expiry,ban_kind=kind)

    async def test_exact_empty_edge_security_projection_is_retained(self):
        path=await self.edge();before=path.read_bytes();inode=path.stat().st_ino
        result,_=await self.inspect();self.assertEqual(result['retained_files'],self.base_files()+1)
        self.assertEqual(path.read_bytes(),before);self.assertEqual(path.stat().st_ino,inode)

    async def test_edge_security_projection_tracks_actual_sql_and_legitimate_republication(self):
        from app.services.edge_security import write_snapshot
        first=await self.ban();path=await self.edge([first]);await self.inspect();binding=self.authority.binding()
        second=await self.ban('203.0.113.9','permanent');write_snapshot([first,second],[],path)
        result,_=await self.inspect();self.assertEqual(result['retained_files'],self.base_files()+1)
        self.assertEqual(self.authority.binding(),binding)
        self.assertIn(b'203.0.113.9\t0\n',path.read_bytes())

    async def test_edge_security_arbitrary_bytes_cannot_hide_in_known_filename(self):
        path=await self.edge();raw=b'X'*31;path.write_bytes(raw)
        with self.assertRaisesRegex(RetirementError,'retirement_control_bytes_invalid'):await self.inspect()
        self.assertEqual(path.read_bytes(),raw)

    async def test_edge_security_unknown_neighbor_is_never_exempted(self):
        path=await self.edge();neighbor=path.with_name('unknown.bin');neighbor.write_bytes(b'keep it')
        with self.assertRaisesRegex(RetirementError,'retirement_unknown_file'):await self.inspect()
        self.assertEqual(neighbor.read_bytes(),b'keep it')

    async def test_edge_security_unsafe_file_mode_or_link_refuses(self):
        path=await self.edge();path.chmod(0o644)
        with self.assertRaisesRegex(RetirementError,'retirement_control_file_unsafe'):await self.inspect()
        path.chmod(0o600);retained=self.base/'retained-snapshot';path.rename(retained);path.symlink_to(retained)
        with self.assertRaises((OSError,RetirementError)):await self.inspect()
        self.assertTrue(path.is_symlink());self.assertEqual(retained.read_bytes(),b'# frontiercloud-ip-security-v1\n')

    async def test_edge_security_replacement_during_census_is_refused(self):
        path=await self.edge()
        async with self.database.begin() as conn:
            with self.runtime.gate.lease(exclusive=True) as lease:
                census=await self.authority.census(conn,lease)
                try:
                    replacement=self.base/'replacement';replacement.write_bytes(path.read_bytes());replacement.chmod(0o600);os.replace(replacement,path)
                    with self.assertRaisesRegex(RetirementError,'retirement_control_file_changed'):await joined_io(census.page)
                finally:census.close()

    async def test_edge_security_database_projection_has_a_bounded_metadata_limit(self):
        await self.edge();now=datetime.now(timezone.utc).replace(tzinfo=None)
        async with self.database.begin() as conn:
            await conn.execute(text('INSERT INTO ip_permanent_whitelist (ip_address,created_at) VALUES (:ip,:now)'),
                [dict(ip='2001:db8::'+format(index,'x'),now=now) for index in range(1,1026)])
        with self.assertRaisesRegex(RetirementError,'retirement_security_snapshot_limit'):await self.inspect()
