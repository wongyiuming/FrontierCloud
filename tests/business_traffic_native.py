"""Exercise the actual Nginx module and Python observer in disposable containers."""
import asyncio
import hashlib
import http.client
import json
import resource
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.services.business_traffic import Monitor


async def main():
    started, started_cpu = time.monotonic(), time.process_time()
    monitor = Monitor()
    watcher = asyncio.create_task(monitor.watch_gateway('/counter/counter.sock'))
    size = 2 << 20
    evidence = {}
    try:
        for _ in range(200):
            if monitor.snapshot().get('reason') is None and monitor._gateway is not None:
                break
            await asyncio.sleep(.01)
        assert monitor.snapshot()['state'] == 'cooldown', monitor.snapshot()

        # Even an accidental TCP exposure of the status handler is refused.
        try:
            urllib.request.urlopen('http://127.0.0.1:18081/traffic', timeout=2)
            raise AssertionError('counter became public')
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        await asyncio.sleep(1.2)
        initial = monitor.snapshot()
        assert initial['recent_bytes_per_second'] < 10000, initial

        digest = hashlib.sha256()
        count, live = 0, []
        response = await asyncio.to_thread(urllib.request.urlopen,
            'http://127.0.0.1:18081/blob', timeout=5)
        try:
            while chunk := await asyncio.to_thread(response.read, 65536):
                digest.update(chunk)
                count += len(chunk)
                if count < size:
                    live.append(monitor.snapshot())
        finally:
            response.close()
        assert count == size
        assert digest.hexdigest() == hashlib.sha256(bytes(size)).hexdigest()
        assert any(sample['state'] == 'high_traffic' and not sample['can_run'] for sample in live), live
        evidence['sendfile'] = {'bytes': count, 'sha256': digest.hexdigest(),
                                'detected_before_completion': True,
                                'peak_recent_bytes_per_second': max(s['recent_bytes_per_second'] for s in live)}
        assert any(sample['active_business_requests'] and not sample['readers_clear'] for sample in live),live

        uploaded = []
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_PUT(self):
                remaining = int(self.headers['Content-Length'])
                received = 0
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    assert chunk
                    remaining -= len(chunk)
                    received += len(chunk)
                uploaded.append(received)
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 18082), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        upload_views = []
        def upload():
            connection = http.client.HTTPConnection('127.0.0.1', 18081, timeout=5)
            try:
                connection.putrequest('PUT', '/internal/v1/storage/native-fixture')
                connection.putheader('Content-Length', str(size))
                connection.endheaders()
                for _ in range(size // 65536):
                    connection.send(bytes(65536))
                    upload_views.append(monitor.snapshot())
                    time.sleep(.05)
                response = connection.getresponse()
                assert response.status == 204
                response.read()
            finally:
                connection.close()
        try:
            await asyncio.to_thread(upload)
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
        assert uploaded == [size], uploaded
        assert any(sample['ingress_bytes_per_second'] > 0 for sample in upload_views), upload_views
        evidence['upload'] = {'bytes': size, 'live_ingress_detected': True,
                              'peak_ingress_bytes_per_second': max(s['ingress_bytes_per_second'] for s in upload_views)}

        async def marker(name):
            async with asyncio.timeout(8):
                while not Path('/control/'+name).exists():await asyncio.sleep(.05)
        held=await asyncio.to_thread(urllib.request.urlopen,'http://127.0.0.1:18081/held',timeout=5)
        try:
            assert await asyncio.to_thread(held.read,1)==b'\0'
            async with asyncio.timeout(4):
                while monitor.snapshot()['active_business_requests']!=1:await asyncio.sleep(.05)
            Path('/control/reload-request').touch();await marker('reloaded')
            await asyncio.sleep(1.3)
            assert monitor.snapshot()['active_business_requests']==1 and not monitor.snapshot()['readers_clear'],monitor.snapshot()
            # A separate connection is accepted by the new generation while
            # the old worker still owns the rate-limited sendfile request.
            with await asyncio.to_thread(urllib.request.urlopen,'http://127.0.0.1:18081/health',timeout=3) as health:
                assert health.status==200
            Path('/control/kill-request').touch();await marker('killed')
            async with asyncio.timeout(5):
                while not monitor.snapshot()['readers_clear']:await asyncio.sleep(.05)
            evidence['reader_lifecycle']=dict(active_after_reload=1,old_worker_killed=True,
                dead_worker_count_reconciled=True,final_active=monitor.snapshot()['active_business_requests'])
        finally:held.close()

        # No test clock: real poller and actual counters must recover only after
        # the five-second window drains and thirty seconds of observed quiet.
        quiet_started = time.monotonic()
        assert not monitor.snapshot()['can_run']
        deadline = quiet_started + 42
        while not monitor.snapshot()['can_run'] and time.monotonic() < deadline:
            await asyncio.sleep(.25)
        view = monitor.snapshot()
        assert view['can_run'], view
        elapsed = time.monotonic() - quiet_started
        assert elapsed >= 29, elapsed
        evidence['quiet_recovery'] = {'elapsed_seconds': round(elapsed, 3), 'view': view}
        evidence['counter_tcp_status'] = 403
        evidence['python_fixture_resources'] = {
            'wall_seconds': round(time.monotonic() - started, 3),
            'cpu_seconds': round(time.process_time() - started_cpu, 4),
            'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'scope': 'observer plus test client, upload receiver and assertions; not isolated observer overhead'}
        print(json.dumps(evidence, indent=2), flush=True)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        assert monitor.snapshot()['state'] == 'unavailable'


if __name__ == '__main__':
    asyncio.run(main())
