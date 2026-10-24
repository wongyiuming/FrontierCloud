"""Real bounded Unix RPC under the actual Compose certificate CPU limit."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx

from app.services.certmanager import client


async def main():
    inputs=[];results=[];factory=client.httpx.AsyncHTTPTransport

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self,**options):self.inner=factory(**options)
        async def handle_async_request(self,request):
            inputs.append(dict(method=request.method,path=request.url.path,body=json.loads(request.content)))
            return await self.inner.handle_async_request(request)
        async def aclose(self):await self.inner.aclose()

    async def call(index,raw=False):
        started=time.monotonic()
        try:
            value=await (client._request(action='status') if raw else client.certificate_control())
            results.append(dict(index=index,independent_rpc=raw,state=value['state'],
                ok=value['state']=='ACTIVE' and value['controller_running'] and value['https_ready'] and value['serving_matches'],
                elapsed_seconds=round(time.monotonic()-started,4)))
        except client.CertificateControlError as error:
            results.append(dict(index=index,independent_rpc=raw,ok=False,error_code=error.code,
                elapsed_seconds=round(time.monotonic()-started,4)))

    with patch.object(client.httpx,'AsyncHTTPTransport',side_effect=lambda **options:RecordingTransport(**options)):
        for offset in range(0,16,4):await asyncio.gather(*(call(offset+i) for i in range(4)))
        shared_inputs=len(inputs)
        for offset in range(16,24,4):await asyncio.gather(*(call(offset+i,raw=True) for i in range(4)))
    passed=all(row['ok'] for row in results) and shared_inputs==4 and len(inputs)==12
    print(json.dumps(dict(format='frontiercloud-python-certificate-status-native',version=1,passed=passed,
        actual_unix_rpc=True,concurrency=4,caller_requests=24,shared_status_wire_requests=shared_inputs,
        actual_wire_inputs=inputs,results=results),sort_keys=True))
    if not passed:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
