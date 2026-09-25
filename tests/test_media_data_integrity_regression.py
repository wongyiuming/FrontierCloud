import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import UniqueConstraint

from app.api.v1 import admin_cluster_integrity as cluster
from app.api.v1 import admin_delete_integrity as media_delete
from app.api.v1 import admin_masterlocal_recovery as upload_entry
from app.api.v1 import karaoke_integrity as karaoke_api
from app.services import karaoke_delete_integrity as karaoke_delete
from app.services.federation import schema as fs


class _Result:
    def __init__(self, row=None): self._row = row
    def mappings(self): return self
    def first(self): return self._row


class _Connection:
    def __init__(self, row=None): self.row = row; self.executed = []
    async def execute(self, statement, params=None): self.executed.append((str(statement), params or {})); return _Result(self.row)


class _Context:
    def __init__(self, connection): self.connection = connection
    async def __aenter__(self): return self.connection
    async def __aexit__(self, *_args): return False


class _Database:
    def __init__(self, connection): self.connection = connection
    def begin(self): return _Context(self.connection)
    def connect(self): return _Context(self.connection)


class GlobalMediaOwnershipTests(unittest.TestCase):
    def test_catalog_enforces_one_logical_path_and_one_member_object_owner(self):
        constraints = {c.name: tuple(col.name for col in c.columns) for c in fs.global_media.constraints if isinstance(c, UniqueConstraint)}
        self.assertEqual(constraints["uq_global_media_path"], ("path_locator",)); self.assertEqual(constraints["uq_global_media_placement"], ("storage_member_id", "object_id")); self.assertFalse(fs.global_media.c.path_locator.nullable)
    def test_upload_path_lease_is_unique_until_session_is_converged(self):
        constraints = {c.name: tuple(col.name for col in c.columns) for c in fs.upload_sessions.constraints if isinstance(c, UniqueConstraint)}
        self.assertEqual(constraints["uq_cluster_upload_path"], ("path_locator",)); self.assertTrue(fs.upload_sessions.c.path_locator.nullable)


class UploadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _reserved_local(self): return {"upload_id":"u"*32,"state":"reserved","member_kind":"MasterLocal","storage_member_id":"m"*32,"media_id":"a"*64,"media_path":"music/artist/song.mp3","expected_bytes":3}
    async def test_unindexed_local_bytes_are_not_deleted_without_explicit_recovery_permission(self):
        row=self._reserved_local(); fail_upload=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); target=root/row["media_path"]; target.parent.mkdir(parents=True); target.write_bytes(b"ID3")
            with patch.object(cluster,"MEDIA_ROOT",root), patch.object(cluster.resource_pool,"upload_session",new=AsyncMock(return_value=row)), patch.object(cluster.resource_pool,"fail_upload",new=fail_upload), patch.object(cluster,"_local_object_exists",new=AsyncMock(return_value=False)): cleaned=await cluster._cleanup_upload_session(row["upload_id"])
            self.assertFalse(cleaned); self.assertTrue(target.exists()); fail_upload.assert_not_awaited()
    async def test_explicit_cancel_deletes_local_bytes_before_releasing_reservation(self):
        row=self._reserved_local(); connection=_Connection(); database=_Database(connection); fail_upload=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); target=root/row["media_path"]; target.parent.mkdir(parents=True); target.write_bytes(b"ID3")
            with patch.object(cluster,"MEDIA_ROOT",root), patch.object(cluster.node_state,"database",database), patch.object(cluster.resource_pool,"upload_session",new=AsyncMock(return_value=row)), patch.object(cluster.resource_pool,"fail_upload",new=fail_upload), patch.object(cluster,"_local_object_exists",new=AsyncMock(return_value=False)): cleaned=await cluster._cleanup_upload_session(row["upload_id"],allow_unindexed_local=True)
            self.assertTrue(cleaned); self.assertFalse(target.exists()); self.assertTrue(any("DELETE FROM media_objects" in sql for sql,_ in connection.executed)); fail_upload.assert_awaited_once_with(row["upload_id"],database)
    async def test_remote_404_releases_reservation_because_bytes_are_already_absent(self):
        row={"upload_id":"u"*32,"state":"reserved","member_kind":"Follower","storage_member_id":"f"*32,"media_id":"a"*64,"media_path":"music/artist/song.mp3","expected_bytes":3}; relation={"relationship_id":"r"*32,"credential":"sealed","state":"active","status":"online","peer_endpoint":"https://follower.example"}; fail_upload=AsyncMock()
        with patch.object(cluster.resource_pool,"upload_session",new=AsyncMock(return_value=row)), patch.object(cluster,"_member_relation",new=AsyncMock(return_value=({"member_id":row["storage_member_id"]},relation))), patch.object(cluster.node_runtime,"call",new=AsyncMock(side_effect=cluster.p.ProtocolError("HTTP 404"))), patch.object(cluster.resource_pool,"fail_upload",new=fail_upload): cleaned=await cluster._cleanup_upload_session(row["upload_id"])
        self.assertTrue(cleaned); fail_upload.assert_awaited_once_with(row["upload_id"],cluster.node_state.database)
    async def test_new_upload_reconciles_stale_path_before_reserving_again(self):
        events=[]
        async def recover(): events.append("recover-expired"); return 0
        async def logical(_): events.append("logical-path"); return "music/artist/song.mp3"
        async def reconcile(_): events.append("reconcile-path")
        async def create(*_): events.append("reserve"); return {"upload_id":"u"*32}
        payload=cluster.ClusterUploadReservation(target_dir="music/artist",filename="song.mp3",size_bytes=3)
        with patch.object(upload_entry,"recover_expired_masterlocal",new=recover), patch.object(upload_entry.cluster,"_upload_logical_path",new=logical), patch.object(upload_entry.delete_integrity,"reconcile_upload_path",new=reconcile), patch.object(upload_entry.cluster,"create_upload_session",new=create): result=await upload_entry.create_upload_session(payload,object(),"session")
        self.assertEqual(result["upload_id"],"u"*32); self.assertEqual(events,["recover-expired","logical-path","reconcile-path","reserve"])
    async def test_reconciliation_failure_blocks_new_reservation(self):
        create=AsyncMock(); payload=cluster.ClusterUploadReservation(target_dir="music/artist",filename="song.mp3",size_bytes=3)
        with patch.object(upload_entry,"recover_expired_masterlocal",new=AsyncMock(return_value=0)), patch.object(upload_entry.cluster,"_upload_logical_path",new=AsyncMock(return_value="music/artist/song.mp3")), patch.object(upload_entry.delete_integrity,"reconcile_upload_path",new=AsyncMock(side_effect=HTTPException(409,"pending delete"))), patch.object(upload_entry.cluster,"create_upload_session",new=create):
            with self.assertRaises(HTTPException) as raised: await upload_entry.create_upload_session(payload,object(),"session")
        self.assertEqual(raised.exception.status_code,409); create.assert_not_awaited()


class MediaDeleteIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_bytes_are_removed_before_catalog_and_playback_facts(self):
        row={"media_id":"a"*64,"object_id":"b"*64,"storage_member_id":"m"*32,"media_path":"music/artist/song.mp3","size_bytes":3}; connection=_Connection()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); target=root/row["media_path"]; target.parent.mkdir(parents=True); target.write_bytes(b"ID3")
            with patch.object(media_delete,"MEDIA_ROOT",root), patch.object(media_delete.node_state,"node",{"node_id":row["storage_member_id"],"role":"Master"}), patch.object(media_delete.node_state,"database",_Database(connection)), patch.object(media_delete,"_pending_media",new=AsyncMock(return_value=row)): removed=await media_delete.retry_pending_media(row["media_id"])
            self.assertTrue(removed); self.assertFalse(target.exists())
        sql="\n".join(s for s,_ in connection.executed)
        for expected in ("DELETE FROM media_lyric_links","DELETE FROM media_playback_events","DELETE FROM media_playback_stats","DELETE FROM media_objects","UPDATE cluster_storage_members","DELETE FROM global_media_objects"): self.assertIn(expected,sql)
    async def test_offline_remote_owner_keeps_pending_delete_facts_for_recovery(self):
        row={"media_id":"a"*64,"object_id":"b"*64,"storage_member_id":"f"*32,"media_path":"music/artist/song.mp3","size_bytes":3}; database=_Database(_Connection())
        with patch.object(media_delete.node_state,"node",{"node_id":"m"*32,"role":"Master"}), patch.object(media_delete.node_state,"database",database), patch.object(media_delete,"_pending_media",new=AsyncMock(return_value=row)), patch.object(media_delete.resource_pool,"list_members",new=AsyncMock(return_value=[{"member_id":row["storage_member_id"],"relationship_id":"r"*32,"health":"offline"}])): removed=await media_delete.retry_pending_media(row["media_id"])
        self.assertFalse(removed); self.assertEqual(database.connection.executed,[])


class KaraokeDeleteIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def _deleting_row(self): return {"recording_id":"r"*32,"user_id":"u"*32,"state":"deleting","size_bytes":10,"storage_member_id":"m"*32,"sha256":"a"*64}
    async def test_physical_recording_delete_failure_keeps_metadata_for_recovery(self):
        row=self._deleting_row(); finalize=AsyncMock(return_value=True); database=_Database(_Connection(row=row))
        with patch.object(karaoke_delete.state,"database",database), patch.object(karaoke_delete,"_physical_delete",new=AsyncMock(side_effect=OSError("storage offline"))), patch.object(karaoke_delete,"finalize_recording_delete",new=finalize): removed=await karaoke_delete.recover_recording(row["user_id"],row["recording_id"])
        self.assertFalse(removed); finalize.assert_not_awaited()
    async def test_recording_metadata_is_finalized_only_after_physical_delete(self):
        row=self._deleting_row(); events=[]
        async def physical(_): events.append("physical")
        async def finalize(*_,**__): events.append("metadata"); return True
        database=_Database(_Connection(row=row))
        with patch.object(karaoke_delete.state,"database",database), patch.object(karaoke_delete,"_physical_delete",new=physical), patch.object(karaoke_delete,"finalize_recording_delete",new=finalize): removed=await karaoke_delete.recover_recording(row["user_id"],row["recording_id"])
        self.assertTrue(removed); self.assertEqual(events,["physical","metadata"])
    async def test_recording_delete_is_scoped_to_authenticated_owner(self):
        stage=AsyncMock(return_value={"state":"deleting"}); recover=AsyncMock(return_value=True)
        with patch.object(karaoke_api.legacy,"_user",new=AsyncMock(return_value={"user_id":"owner-user"})), patch.object(karaoke_api.deletion,"stage_recording",new=stage), patch.object(karaoke_api.deletion,"recover_recording",new=recover): result=await karaoke_api.delete_recording("recording-1",object())
        self.assertEqual(result,{"status":"deleted"}); stage.assert_awaited_once_with("owner-user","recording-1",pending=False); self.assertEqual(recover.await_args.args[:2],("owner-user","recording-1"))
    async def test_unfinished_recording_delete_surfaces_recoverable_503(self):
        with patch.object(karaoke_api.legacy,"_user",new=AsyncMock(return_value={"user_id":"owner-user"})), patch.object(karaoke_api.deletion,"stage_recording",new=AsyncMock(return_value={"state":"deleting"})), patch.object(karaoke_api.deletion,"recover_recording",new=AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as raised: await karaoke_api.delete_recording("recording-1",object())
        self.assertEqual(raised.exception.status_code,503)


if __name__ == "__main__": unittest.main()
