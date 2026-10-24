"""Read-only bounded Unix RPC; Web has no issuer account or private-cache mount."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx

from .assets import RUNTIME


CODES=frozenset({'controller_busy','controller_timeout','controller_unavailable','controller_request_rejected',
    'controller_response_invalid','controller_response_too_large'})
_status_task=None


class CertificateControlError(ValueError):
    def __init__(self,code):
        if code not in CODES:raise ValueError('invalid certificate control diagnostic')
        self.code=code
        super().__init__(code)


def unavailable(code,hostname):
    return dict(enabled=True,hostname=hostname,state='UNAVAILABLE',controller_running=None,https_ready=None,
        serving_matches=None,managed=True,auto_renew=True,last_error=code)


async def _request(*,action,browser_public_key='',browser_nonce=''):
    request = {"action": action, "browser_public_key": browser_public_key, "browser_nonce": browser_nonce}
    transport = httpx.AsyncHTTPTransport(uds=str(RUNTIME / "control.sock"), retries=0)
    try:
        async with asyncio.timeout(6),httpx.AsyncClient(transport=transport,trust_env=False,timeout=4) as client:
            async with client.stream("POST","http://certificate/control",json=request) as response:
                if response.status_code in {429,503}:raise CertificateControlError('controller_busy')
                if response.status_code in {408,504}:raise CertificateControlError('controller_timeout')
                if response.status_code>=500:raise CertificateControlError('controller_unavailable')
                if response.status_code!=200:raise CertificateControlError('controller_request_rejected')
                result=bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    result.extend(chunk)
                    if len(result)>131072:raise CertificateControlError('controller_response_too_large')
        value=json.loads(result)
        if not isinstance(value,dict):raise CertificateControlError('controller_response_invalid')
        if action=='status' and (value.get('state') not in {'DISABLED','PENDING','VALIDATING','ISSUING','ACTIVE','RENEWING','DEGRADED','FAILED'} or
                any(type(value.get(name)) is not bool for name in ('enabled','controller_running','https_ready','serving_matches'))):
            raise CertificateControlError('controller_response_invalid')
        return value
    except (httpx.TimeoutException,TimeoutError):raise CertificateControlError('controller_timeout') from None
    except httpx.HTTPError:raise CertificateControlError('controller_unavailable') from None
    except (json.JSONDecodeError,UnicodeError):raise CertificateControlError('controller_response_invalid') from None


def _settled(task):
    global _status_task
    if not task.cancelled():task.exception()
    if _status_task is task:_status_task=None


async def certificate_control(*, action="status", browser_public_key="", browser_nonce="") -> dict:
    global _status_task
    if action not in ("status", "download"):
        raise ValueError("certificate lifecycle is automatic; only status/download are available")
    if action!='status' or browser_public_key or browser_nonce:
        return await _request(action=action,browser_public_key=browser_public_key,browser_nonce=browser_nonce)
    # Share only overlapping identical readonly observations, never completed
    # results or browser-bound downloads. A cancelled waiter cannot cancel its
    # peers, and the underlying RPC has an independent total deadline.
    loop=asyncio.get_running_loop();task=_status_task
    if task is None or task.done() or task.get_loop() is not loop:
        task=loop.create_task(_request(action='status'));_status_task=task;task.add_done_callback(_settled)
    return copy.deepcopy(await asyncio.shield(task))
