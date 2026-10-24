"""One process-owned planner and serial scheduler with joined shutdown."""
import time

from app.services.resource_migration_planning import Planner
from app.services.resource_migration_scheduler import Scheduler


class Runtime:
    def __init__(self,store,transport,*,traffic=None,wall=time.time,fenced=None):
        self.store,self.transport=store,transport
        self.options=dict(traffic=traffic,wall=wall);self.fenced=fenced;self.closed=False
        self._controllers()

    def _controllers(self):
        self.planner=Planner(self.store,self.transport,**self.options)
        self.scheduler=Scheduler(self.store,self.transport,fenced=self.fenced,**self.options)
        self.scheduler.admission.preview_epoch=self.planner.epoch
        self.scheduler.transfer.preparation.preview_epoch=self.planner.epoch

    def start(self):
        if self.closed:
            self._controllers();self.closed=False
        self.scheduler.start()

    async def stop(self):
        try:await self.planner.stop()
        finally:await self.scheduler.stop()
        self.closed=True


from app.services.federation.state import state
from app.services.federation.transport import transport

runtime=Runtime(state,transport)
