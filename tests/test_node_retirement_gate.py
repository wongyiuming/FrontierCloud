"""Real Linux flock leases, descriptor identity and process lifetime boundaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from app.services.node_retirement_gate import Gate,GateError,Lease,NAME


@unittest.skipUnless(os.name=='posix','native retirement leases require Linux flock')
class RetirementGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)/'data';self.root.mkdir()
        self.gate=Gate(self.root);self.addCleanup(self.temporary.cleanup);self.addCleanup(self.gate.close)

    def test_shared_requests_join_and_exclusive_final_check_waits_without_unlink(self):
        inode=(self.root/NAME).stat().st_ino
        first=self.gate.lease();second=self.gate.lease()
        try:
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease(exclusive=True)
            first.close()
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease(exclusive=True)
        finally:first.close();second.close()
        with self.gate.lease(exclusive=True) as final:
            final.check(exclusive=True)
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease()
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease(exclusive=True)
        with self.gate.lease() as ordinary:
            with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):ordinary.check(exclusive=True)
        self.assertEqual((self.root/NAME).stat().st_ino,inode)

    def test_independent_gate_instances_serialize_on_the_same_inode(self):
        other=Gate(self.root)
        try:
            with self.gate.lease():
                with self.assertRaisesRegex(GateError,'retirement_requests_busy'):other.lease(exclusive=True)
            with other.lease(exclusive=True):
                with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease()
        finally:other.close()

    def test_actual_child_process_holds_shared_lease_until_exit(self):
        code="""import sys
from app.services.node_retirement_gate import Gate
gate=Gate(sys.argv[1])
with gate.lease():
 print('held',flush=True)
 sys.stdin.readline()
gate.close()
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'held')
            with self.assertRaisesRegex(GateError,'retirement_requests_busy'):self.gate.lease(exclusive=True)
            child.stdin.write('release\n');child.stdin.flush()
            self.assertEqual(child.wait(timeout=10),0,child.stderr.read())
            with self.gate.lease(exclusive=True):pass
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()

    def test_fork_cannot_reuse_parent_gate_or_lease_capability(self):
        with self.gate.lease() as lease:
            child=os.fork()
            if child==0:
                try:
                    for check in (self.gate.check,lease.check):
                        try:check()
                        except GateError:continue
                        os._exit(2)
                    os._exit(0)
                except BaseException:os._exit(3)
            _,status=os.waitpid(child,0);self.assertEqual(os.waitstatus_to_exitcode(status),0)
        with self.gate.lease(exclusive=True):pass

    def test_replaced_empty_lock_does_not_masquerade_as_original(self):
        original=self.root/'retained-lock';(self.root/NAME).rename(original)
        replacement=self.root/NAME;replacement.touch(mode=0o600)
        with self.assertRaisesRegex(GateError,'retirement_gate_changed'):self.gate.lease(exclusive=True)
        self.assertTrue(original.exists());self.assertTrue(replacement.exists())

    def test_unlinked_or_symlink_replacement_is_retained_and_refused(self):
        (self.root/NAME).unlink()
        with self.assertRaisesRegex(GateError,'retirement_gate_changed'):self.gate.check()
        target=self.root/'foreign';target.write_bytes(b'keep');(self.root/NAME).symlink_to(target)
        with self.assertRaisesRegex(GateError,'retirement_gate_changed'):self.gate.check()
        self.assertEqual(target.read_bytes(),b'keep');self.assertTrue((self.root/NAME).is_symlink())

    def test_root_replacement_cannot_reuse_an_existing_lock_name(self):
        self.root.rename(self.root.with_name('retained-data'));self.root.mkdir()
        other=Gate(self.root)
        try:
            with self.assertRaisesRegex(GateError,'retirement_root_changed'):self.gate.check()
        finally:other.close()
        self.assertTrue(self.root.with_name('retained-data').joinpath(NAME).exists())

    def test_mode_size_and_links_are_part_of_the_live_identity(self):
        for mutation in ('mode','size','link'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);gate=Gate(root);lock=root/NAME
                try:
                    if mutation=='mode':lock.chmod(0o644)
                    elif mutation=='size':lock.write_bytes(b'foreign')
                    else:os.link(lock,root/'foreign-link')
                    with self.assertRaisesRegex(GateError,'retirement_gate_changed'):gate.lease()
                    with self.assertRaisesRegex(GateError,'retirement_gate_unsafe'):Gate(root)
                    self.assertTrue(lock.exists())
                finally:gate.close()

    def test_closed_gate_and_released_lease_are_not_completion_authority(self):
        lease=self.gate.lease();lease.close();lease.close()
        with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):lease.check()
        self.gate.close();self.gate.close()
        with self.assertRaisesRegex(GateError,'retirement_gate_closed'):self.gate.lease(exclusive=True)

    def test_missing_root_is_not_created(self):
        missing=self.root/'missing'
        with self.assertRaisesRegex(GateError,'retirement_root_missing'):Gate(missing)
        self.assertFalse(missing.exists())

    def test_parent_traversal_or_filesystem_root_is_never_admitted(self):
        for root in (self.root/'..',Path(self.root.anchor)):
            with self.subTest(root=root),self.assertRaisesRegex(GateError,'retirement_root_unsafe'):Gate(root)

    def test_completion_requires_an_issued_exclusive_capability_not_a_flag_or_foreign_lease(self):
        descriptor=os.open(self.root/NAME,os.O_RDWR)
        try:
            with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):Lease(self.gate,descriptor,True)
        finally:os.close(descriptor)
        with self.gate.lease() as shared:
            shared.exclusive=True
            try:
                with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):self.gate.require(shared,exclusive=True)
            finally:shared.exclusive=False
        other=Gate(self.root)
        try:
            with other.lease(exclusive=True) as foreign:
                with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):self.gate.require(foreign,exclusive=True)
        finally:other.close()

    def test_gate_shutdown_closes_owned_leases_without_removing_the_lock(self):
        first=self.gate.lease();second=self.gate.lease();self.gate.close()
        self.assertIsNone(first.descriptor);self.assertIsNone(second.descriptor)
        self.assertTrue((self.root/NAME).exists())
        with self.assertRaisesRegex(GateError,'retirement_lease_invalid'):first.check()
