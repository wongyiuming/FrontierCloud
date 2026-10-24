"""One bounded cancellation step; committed ownership and grace stay final."""
from sqlalchemy import select

from app.services import resource_migration_schema as s
from app.services.resource_cancel_finalize_master import CancellationFinalization
from app.services.resource_cancel_intent import published
from app.services.resource_cancel_master import PublicationCancellation
from app.services.resource_cleanup_master import Cleanup
from app.services.resource_cutover_master import COMMITTED
from app.services.resource_finalize_master import Finalization
from app.services.resource_transfer_protocol import TransferError


class CancellationExecution:
    def __init__(self,store,transport,**options):
        self.store=store;self.publication=PublicationCancellation(store,transport,**options)
        self.cleanup=Cleanup(store,transport,**options);self.t=self.publication.t
        self.cancelled=CancellationFinalization(store,transport,**options)
        self.committed=Finalization(store,transport,outcome='cancelled',**options)

    async def advance(self,lease):
        await self.t.journal.renew(lease,ttl=60)
        async with self.store.database.begin() as conn:
            task=await self.t.journal._fenced(conn,lease)
            if task['state']!='cancelling' or not published(task):raise TransferError('migration_cancel_publication_intent_required')
            unfinished=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==lease.task_id,
                s.units.c.state.not_in((*COMMITTED,'cancelled'))).limit(1))
            unack=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==lease.task_id,
                s.units.c.state.in_(COMMITTED),s.units.c.native_acknowledged==0).order_by(s.units.c.unit_id).limit(1))
            pending=await conn.scalar(select(s.units.c.unit_id).where(s.units.c.task_id==lease.task_id,
                s.units.c.state.in_(['committed','cleanup_pending'])).order_by(s.units.c.cleanup_after,s.units.c.unit_id).limit(1))
        if unfinished is not None:
            return await self.publication.advance(lease)
        if unack is not None:
            result=await self.cleanup.cutover.acknowledge_one(lease,unack)
            return lease,dict(phase='committed_acknowledgement',task_completed=False,**result)
        if pending is not None:
            return await self.cleanup.advance(lease,pending)
        # Closed units already attest that their native I/O was joined. Do
        # not repeat stop on a peer whose terminal receipt was committed
        # before a lost response or a subsequent runner incarnation.
        finalizer=self.committed if task['committed_bytes']==task['total_bytes'] else self.cancelled
        return lease,await finalizer.advance(lease)
