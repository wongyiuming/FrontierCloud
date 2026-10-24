"""Host entry keeps recovery self-contained and exposes safe precise failures."""
from contextlib import redirect_stdout
import errno
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import frontiercloud_operator as entry
from scripts import operator_abort as abort
from scripts import operator_backup as backup
from scripts import operator_deployment as mapping
from scripts import operator_entry as handoff


class EntryTests(unittest.TestCase):
    def call(self,arguments):
        output=io.StringIO()
        with redirect_stdout(output):code=entry.main(arguments)
        return code,json.loads(output.getvalue())

    def test_recover_uses_current_directory_without_plan_version_node_or_backup_arguments(self):
        root=Path('/srv/frontiercloud')
        result={'phase':'aborted','abort_complete':True,'deployment_changed':False}
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(Path,'cwd',return_value=root),patch.object(handoff,'start',return_value=result) as recover:
            self.assertEqual(self.call(['recover']),(0,result))
        recover.assert_called_once_with(root,command='recover')

    def test_unpublished_source_refuses_before_original_abort_or_restore_selection(self):
        error=mapping.DeploymentError('source_not_published','a production operation requires published source')
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',side_effect=error),patch.object(entry,'recorded_operation',return_value=None),\
                patch.object(abort.OriginalAbort,'abort') as selected:
            code,result=self.call(['recover'])
        self.assertEqual(code,1)
        self.assertEqual(result['error'],'source_not_published')
        selected.assert_not_called()

    def test_os_failure_preserves_errno_and_recorded_phase_without_paths_or_private_values(self):
        error=OSError(errno.ENOSPC,'sensitive password and source path must remain private','/private/.env')
        phase={'recorded_phase':'backing_up','business_may_have_written':False}
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',side_effect=error),patch.object(entry,'recorded_operation',return_value=phase):
            code,result=self.call(['recover'])
        self.assertEqual(code,1)
        self.assertEqual(result['error'],'host_space_exhausted')
        self.assertEqual(result['host_errno'],errno.ENOSPC)
        self.assertEqual(result['recorded_phase'],'backing_up')
        self.assertIsNone(result['deployment_changed'])
        self.assertNotIn('sensitive',json.dumps(result))
        self.assertNotIn('/private',json.dumps(result))

    def test_permission_missing_and_cross_device_errors_remain_distinct(self):
        for number,expected in [(errno.EACCES,'host_permission_denied'),(errno.ENOENT,'host_material_missing'),
                (errno.EXDEV,'host_cross_device_refused'),(errno.EROFS,'host_readonly_filesystem')]:
            self.assertEqual(entry.error_details(OSError(number,'private details'))['error'],expected)

    def test_secondary_journal_failure_never_masks_the_original_error(self):
        original=mapping.DeploymentError('maintenance_identity_changed','the unknown fence is preserved')
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',side_effect=original),patch.object(entry,'recorded_operation',side_effect=backup.BackupError('invalid private receipt')):
            code,result=self.call(['recover'])
        self.assertEqual((code,result['error']),(1,'maintenance_identity_changed'))
        self.assertNotIn('recorded_phase',result)

    def test_non_linux_host_never_reads_a_host_operation_or_dispatches_recovery(self):
        with patch.object(entry.sys,'platform','win32'),patch.object(handoff,'start') as recover,patch.object(entry,'recorded_operation') as recorded:
            code,result=self.call(['recover'])
        self.assertEqual((code,result['error']),(1,'unsupported_host'))
        recover.assert_not_called()
        recorded.assert_not_called()

    def test_non_root_host_never_reads_or_dispatches_recovery(self):
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=1001,create=True),\
                patch.object(handoff,'start') as recover,patch.object(entry,'recorded_operation') as recorded:
            code,result=self.call(['recover'])
        self.assertEqual((code,result['error']),(1,'unsupported_host'))
        recover.assert_not_called()
        recorded.assert_not_called()

    def test_upgrade_default_directory_dispatches_without_manual_material(self):
        result={'state':'already_current','deployment_changed':False}
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',return_value=result) as start:
            self.assertEqual(self.call(['upgrade']),(0,result))
        start.assert_called_once_with(Path.cwd(),command='upgrade')

    def test_install_uses_default_directory_and_only_initial_storage_selector(self):
        for arguments,storage in ((['install'],None),(['install','--storage'],True)):
            result={'phase':'ready','installation_complete':True}
            with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                    patch.object(handoff,'start',return_value=result) as start:
                self.assertEqual(self.call(arguments),(0,result))
            start.assert_called_once_with(Path.cwd(),command='install',storage=storage)

    def test_install_failure_reports_recorded_install_phase_and_bottom_error(self):
        original=mapping.DeploymentError('install_volume_changed','the existing volume is preserved')
        observed={'recorded_phase':'starting','business_may_have_written':True}
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',side_effect=original),patch.object(entry,'recorded_operation',return_value=observed):
            code,result=self.call(['install'])
        self.assertEqual((code,result['command'],result['error'],result['recorded_phase']),(1,'install','install_volume_changed','starting'))

    def test_source_ci_pending_reports_waiting_and_preserves_the_specific_error(self):
        error=entry.ReleaseError('source_ci_pending','the exact source CI is still running',waiting=True)
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch.object(handoff,'start',side_effect=error),patch.object(entry,'recorded_operation',return_value=None):
            code,result=self.call(['upgrade'])
        self.assertEqual((code,result['state'],result['error']),(3,'waiting','source_ci_pending'))
        self.assertEqual(result['message'],str(error))

    def test_handoff_keeps_the_underlying_prepare_error_and_recorded_phase(self):
        error=mapping.DeploymentError('candidate_adds_persistent_resource','new persistent state requires a recovery mapping')
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch('scripts.operator_registry.private_json',return_value={'command':'upgrade'}),patch.object(handoff,'continue_upgrade',side_effect=error) as dispatch,\
                patch.object(entry,'recorded_operation',return_value={'recorded_phase':'images_prepared'}):
            code,result=self.call(['--operator-handoff','/private/request.json'])
        self.assertEqual((code,result['command'],result['error']),(1,'upgrade','candidate_adds_persistent_resource'))
        self.assertEqual(result['recorded_phase'],'images_prepared')
        dispatch.assert_called_once_with(Path('/private/request.json'))

    def test_private_recovery_failure_retains_its_command_and_bottom_error(self):
        error=mapping.DeploymentError('recovery_after_open_refused','new business writes must be retained')
        with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                patch('scripts.operator_registry.private_json',return_value={'command':'recover'}),\
                patch.object(handoff,'continue_upgrade',side_effect=error),patch.object(entry,'recorded_operation',return_value=None):
            code,result=self.call(['--operator-handoff','/private/request.json'])
        self.assertEqual((code,result['command'],result['error']),(1,'recover','recovery_after_open_refused'))

    def test_private_non_object_or_invalid_command_is_a_structured_refusal(self):
        for request in ([],{'command':[]},{'command':'invalid'}):
            with patch.object(entry.sys,'platform','linux'),patch.object(entry.os,'geteuid',return_value=0,create=True),\
                    patch('scripts.operator_registry.private_json',return_value=request),\
                    patch.object(handoff,'continue_upgrade') as dispatch,patch.object(entry,'recorded_operation',return_value=None):
                code,result=self.call(['--operator-handoff','/private/request.json'])
            self.assertEqual((code,result['error']),(1,'operator_handoff_invalid'))
            dispatch.assert_not_called()
