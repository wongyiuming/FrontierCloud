"""Exercise the deployed MySQL -> snapshot -> Nginx path with owned fixtures.

Run on a Docker host from the deployment directory: python tests/security_stack_smoke.py
No host port, admin key rotation, or existing security object is changed.
"""
import json
import subprocess
import time
import uuid
from pathlib import Path


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def web(code):
    program = """
import asyncio,json
from datetime import datetime,timedelta,timezone
from sqlalchemy import text
from app.core.db import engine
from app.core.redis import redis_client
from app.services import ip_security as s
async def work():
CODE
try:
    asyncio.run(work())
finally:
    pass
""".replace("CODE", "\n".join("    " + line for line in code.splitlines()))
    return run("docker", "compose", "exec", "-T", "web", "python", "-c", program)


def main():
    web_id = run("docker", "compose", "ps", "-q", "web")
    details = json.loads(run("docker", "inspect", web_id))[0]
    network = next(iter(details["NetworkSettings"]["Networks"]))
    name = "frontiercloud-security-probe-" + uuid.uuid4().hex[:12]
    token = uuid.uuid4().hex
    fixtures_owned = False
    run("docker", "run", "-d", "--name", name, "--network", network,
        "--entrypoint", "python", details["Image"], "-c", "import time; time.sleep(300)")
    try:
        ca_path = Path('certs/fullchain.pem')
        if ca_path.is_file():
            run("docker", "cp", str(ca_path.resolve()), name + ":/tmp/frontiercloud-ca.pem")
        client = json.loads(run("docker", "inspect", name))[0]
        ip = client["NetworkSettings"]["Networks"][network]["IPAddress"]
        fixtures = [ip, "10.199.254.235", "13.11.1.1", "2001:db8::7391"]
        ip_values = repr(fixtures)
        web(f"""
for ip in {ip_values}:
    async with engine.connect() as conn:
        for table in ['ip_auto_ban_events', 'ip_permanent_whitelist', 'ip_security_audit_log', 'ip_security_locks']:
            assert not await conn.scalar(text('SELECT COUNT(*) FROM ' + table + ' WHERE ip_address=:ip'), {{'ip':ip}}), 'Fixture collision; refusing changes'
print('fixtures-unused')
""")
        fixtures_owned = True
        config = json.loads(web("from app.core.config import settings\nprint(json.dumps({'host':settings.SERVER_NAME,'tls':settings.TLS_ENABLED}))"))

        def request(path):
            # The configured host and TLS mode are non-secret deployment settings.
            scheme = "https" if config["tls"] else "http"
            code = f"""
import ssl,socket,urllib.request,urllib.error
resolver=socket.getaddrinfo
socket.getaddrinfo=lambda host,*args,**kwargs: resolver('nginx' if host=={config['host']!r} else host,*args,**kwargs)
req=urllib.request.Request('{scheme}://{config['host']}{path}',headers={{'X-Real-IP':'127.0.0.1'}})
try:
    context=ssl.create_default_context()
    from pathlib import Path
    if Path('/tmp/frontiercloud-ca.pem').is_file(): context.load_verify_locations('/tmp/frontiercloud-ca.pem')
    with urllib.request.urlopen(req,context=context,timeout=10) as r: print(r.status)
except urllib.error.HTTPError as e: print(e.code)
"""
            return int(run("docker", "exec", name, "python", "-c", code))

        def expect_status(path, expected):
            deadline = time.monotonic() + 15
            while True:
                actual = request(path)
                if actual == expected:
                    return
                if time.monotonic() >= deadline:
                    raise AssertionError(f"Expected {expected}, got {actual}")
                time.sleep(1)

        expect_status("/static/js/admin.js", 200)
        web(f"""
for value in ['13.11.1.1', '10.199.254.235', '2001:db8::7391']:
    await s.manual_ban_ip(value, 'a'*64, 'owned security smoke fixture')
for _ in range(3):
    await s.unban_ip('13.11.1.1', 'a'*64)
    await s.manual_ban_ip('13.11.1.1', 'a'*64, 'owned repeated fixture')
for order in ['asc','desc']:
    found=[]
    first=await s.list_security_summary(page_size=17, ip_order=order)
    for page in range(1, first['pagination']['pages']+1):
        result=await s.list_security_summary(page_size=17, page=page, ip_order=order)
        found.extend(item['ip'] for item in result['events']+result['whitelist'])
    assert len(found)==len(set(found)), 'Duplicate IP across pages/panels'
    assert (found.index('10.199.254.235') < found.index('13.11.1.1')) == (order=='asc')
    assert (found.index('13.11.1.1') < found.index('2001:db8::7391')) == (order=='asc')
await s.add_whitelist('13.11.1.1', 'a'*64, 'owned whitelist fixture')
result=await s.list_security_summary(ip_filter='13.11.1.1')
assert result['pagination']['total']==1 and len(result['whitelist'])==1 and not result['events']
assert not (await s.list_security_summary(ip_filter='13.11.1.1', status_filter='active'))['events']
await s.remove_whitelist('13.11.1.1', 'a'*64)
async with engine.connect() as conn:
    actions=(await conn.execute(text('SELECT action FROM ip_security_audit_log WHERE ip_address=:ip ORDER BY created_at,id'), {{'ip':'13.11.1.1'}})).scalars().all()
    assert actions==['manual_ban','unban','manual_ban','unban','manual_ban','unban','manual_ban','whitelist_add','whitelist_remove'], actions
await s.manual_permanent_ban_ip({ip!r}, 'a'*64, 'owned edge fixture')
print('mysql-summary-order-pagination-audit-ok')
""")
        proof_path = "/edge-security-proof-" + token
        time.sleep(3)
        expect_status(proof_path, 403)
        logs = run("docker", "compose", "logs", "--no-log-prefix", "--since", "60s", "nginx")
        matches = []
        for line in logs.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("path") == proof_path:
                matches.append(record)
        assert any(r.get("security_blocked") == 1 and r.get("upstream_addr") == "-"
                   and r.get("client_ip") == ip for r in matches), "No edge-only denial evidence"
        web(f"await s.unban_ip({ip!r}, 'a'*64)")
        expect_status("/static/js/admin.js", 200)
        web(f"await s.manual_permanent_ban_ip({ip!r}, 'a'*64, 'owned allowlist test')\nawait s.add_whitelist({ip!r}, 'a'*64)")
        expect_status("/static/js/admin.js", 200)
        web(f"""
await s.remove_whitelist({ip!r}, 'a'*64)
await s.manual_ban_ip({ip!r}, 'a'*64, 'owned expiry test')
async def shorten(conn):
    await conn.execute(text("UPDATE ip_auto_ban_events SET expires_at=:expiry WHERE ip_address=:ip AND status='active'"), {{'ip':{ip!r}, 'expiry':datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=10)}})
await s._run_state_transaction(shorten)
""")
        expect_status(proof_path, 403)
        time.sleep(11)
        expect_status("/static/js/admin.js", 200)
        print("security-stack-smoke-ok: unique numeric IP summaries, durable timeline, Nginx-only denial, spoof resistance, release, whitelist, expiry")
    finally:
        try:
            if fixtures_owned:
                web(f"""
async def cleanup(conn):
    for ip in {ip_values}:
        for table in ['ip_security_audit_log','ip_auto_ban_events','ip_permanent_whitelist','ip_security_locks']:
            await conn.execute(text('DELETE FROM '+table+' WHERE ip_address=:ip'), {{'ip':ip}})
await s._run_state_transaction(cleanup)
for ip in {ip_values}:
    await redis_client.delete(s._violation_key(ip))
print('owned-security-fixtures-removed')
""")
        finally:
            run("docker", "rm", "-f", name)


if __name__ == "__main__":
    main()
