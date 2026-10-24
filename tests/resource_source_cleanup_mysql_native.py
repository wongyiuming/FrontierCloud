"""Real MySQL cleanup transactions with a declared virtual unit-test clock."""
import asyncio

from tests.resource_cutover_mysql_native import run_cases
from tests.test_resource_source_cleanup import SourceCleanupTests


def cases():
    return [(SourceCleanupTests,name) for name in (
        'test_original_grace_is_required_and_cannot_be_shortened',
        'test_verified_replicas_are_removed_without_keys_or_master_business_changes',
        'test_active_http_reader_and_live_durable_lease_both_prevent_deletion',
        'test_unknown_source_file_is_preserved_and_blocks_cleanup',
        'test_unlink_then_native_sql_audit_failure_recovers_after_service_restart',
        'test_lost_master_audit_reconciles_cumulative_native_progress',
        'test_changed_file_after_durable_unlink_intent_is_not_removed',
        'test_changed_master_ownership_refuses_source_cleanup_before_any_intent',
        'test_new_runner_fences_old_cleanup_calls_without_rewinding_progress',
        'test_new_runner_refreshes_authentic_old_target_proof_but_refuses_tampering',
        'test_lost_initial_native_intent_reply_keeps_master_runner_usable',
        'test_expiry_during_unlink_has_no_success_ack_and_reconciles_the_durable_intent',
        'test_boolean_cleanup_progress_is_refused_without_losing_the_real_task',
        'test_unknown_entry_can_be_reconciled_after_owner_preserves_it_elsewhere',
        'test_replaced_source_root_never_removes_new_host_data_or_old_originals',
        'test_recording_cleanup_preserves_target_footer_and_current_master_recordings',
        'test_source_identity_status_does_not_wait_for_the_physical_unlink')]


if __name__=='__main__':asyncio.run(run_cases(cases(),scope='source-cleanup-virtual-clock-faults',source_cleanup=True))
