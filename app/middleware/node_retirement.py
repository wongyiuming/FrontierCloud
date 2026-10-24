"""Hold a native request lease until routing and response delivery really finish."""
from starlette.responses import JSONResponse

from app.services import node_retirement_runtime
from app.services.node_retirement_gate import GateError


class NodeRetirementMiddleware:
    def __init__(self,app,*,runtime_provider=node_retirement_runtime.current):
        self.app,self.runtime_provider=app,runtime_provider

    async def __call__(self,scope,receive,send):
        if (scope['type']!='http' or not scope.get('path','').startswith('/internal/v1/')
                or scope.get('path')=='/internal/v1/retirement' and scope.get('method')=='POST'):
            await self.app(scope,receive,send);return
        try:
            runtime=self.runtime_provider();runtime.check();lease=runtime.gate.lease()
        except (GateError,OSError):
            await JSONResponse({'code':'native_request_unavailable'},status_code=503,headers={'Cache-Control':'no-store'})(scope,receive,send)
            return
        with lease:
            await self.app(scope,receive,send)
