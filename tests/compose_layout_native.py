"""Render real Compose inputs, including the explicit historical-MySQL overlay."""
import json
import os
from pathlib import Path
import subprocess


ROOT=Path(__file__).resolve().parents[1]


def render(*files,**overrides):
    command=['docker','compose']
    for name in files:command.extend(['-f',str(ROOT/name)])
    command.extend(['config','--format','json'])
    environment={**os.environ,'COMPOSE_PROFILES':'','COMPOSE_FILE':'','DB_TYPE':'sqlite',
        'TLS_ENABLED':'true','SERVER_NAME':'storage.4399.com','STORAGE_TRANSPORT':'direct',
        'STORAGE_ENDPOINT':'https://storage.4399.com',**overrides}
    result=subprocess.run(command,cwd=ROOT,env=environment,capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('compose_layout_render_failed_'+files[-1])
    return json.loads(result.stdout)['services']


def main():
    checks=[]
    def check(name,value):
        if not value:raise AssertionError(name)
        checks.append(name)
    default=render('docker-compose.yaml')
    check('default_sqlite_excludes_mysql',default['web']['environment']['DB_TYPE']=='sqlite' and 'mysql' not in default)
    mysql=render('docker-compose.yaml','docker-compose.mysql.yaml')
    check('documented_mysql_overlay_activates_database_without_extra_profile',mysql['web']['environment']['DB_TYPE']=='mysql'
        and 'mysql' in mysql and not mysql['mysql'].get('profiles')
        and mysql['web']['depends_on']['mysql']['condition']=='service_healthy')
    check('mysql_preserves_existing_volume_and_secret_mounts',any(row['target']=='/var/lib/mysql' for row in mysql['mysql']['volumes'])
        and any(row['target']=='/run/frontiercloud-secrets' and row.get('read_only') for row in mysql['mysql']['volumes']))
    storage=render('docker-compose.storage.yaml')
    check('storage_is_independent_of_business_mysql','mysql' not in storage and 'nginx' not in storage and 'web' in storage)
    check('web_keeps_one_cpu',float(default['web']['cpus'])==float(mysql['web']['cpus'])==float(storage['web']['cpus'])==1)
    print(json.dumps(dict(format='frontiercloud-python-compose-layout',version=1,checks=checks,count=len(checks),passed=True)))


if __name__=='__main__':main()
