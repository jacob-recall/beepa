"""Synthetic feature state + network-disabled worker in a marked test stack."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time

from sandbox import REPO


def install(root, project, compose, tokens):
    from notifications import Notifications
    assert json.loads((root/'.beepa-test-root').read_text())['project'] == project
    state = root/'master/control.local';state.mkdir(mode=0o700)
    config = {'manager':tokens['MASTER_MANAGER_USER'], 'manager_token':tokens['MASTER_MANAGER_TOKEN'],
              'telegram_token':'synthetic-unused', 'openrouter_key':'synthetic-unused'}
    (state/'config.json').write_text(json.dumps(config));(state/'config.json').chmod(0o600)
    (state/'enabled').touch(mode=0o600)
    app = root/'control-fixture'
    for name in ['master/control-core.mjs', 'master/control-domain.mjs',
                 'master/notification-routing.mjs', 'master/notification-routing-adapters.mjs',
                 'apps/master/crm-store.js', 'apps/master/crm-members.js', 'apps/master/crm-batches.js',
                 'apps/master/crm-account.js', 'shared/model/crm.js', 'shared/model/message_timestamps.js', 'shared/ui/el.js']:
        target = app/name;target.parent.mkdir(parents=True, exist_ok=True);shutil.copyfile(REPO/name, target)
    (app/'fixture.mjs').write_text('''
import {ControlCore} from './master/control-core.mjs';
import {writeFileSync} from 'node:fs';
const forbidden=async()=>{throw Error('Fixture attempted external work');};
const c=new ControlCore({path:'/state/control.db',domain:{},telegram:forbidden,model:forbidden});
if(process.argv.includes('--seed')){
  c.bindTransport({id:1,is_bot:true,username:'fixture_bot'});c.set('operator',{user:7,chat:7});
  c.draft({id:'review',kind:'send',created:Date.now(),body:'Synthetic review',room_id:'!fixture:master'});
  c.enqueue('notice','Synthetic alert');c.enqueue('uncertain','Synthetic alert');
  c.db.prepare("UPDATE outbox SET state='sending' WHERE id='uncertain:0'").run();
  c.enqueueCommand({request_id:'old-command',text:'/help'});c.db.close();
}else{
  const heartbeat=()=>writeFileSync('/state/heartbeat.json',JSON.stringify({
    operator:c.value('operator'),
    outbox:Object.fromEntries(c.db.prepare('SELECT id,state FROM outbox').all().map(x=>[x.id,x.state])),
    drafts:Object.fromEntries(c.db.prepare('SELECT id,state FROM drafts').all().map(x=>[x.id,x.state]))
  }));
  heartbeat();setInterval(heartbeat,100);
  process.on('SIGTERM',()=>{c.db.close();writeFileSync('/state/stopped','closed');process.exit(0);});
}
''')
    path = root/'compose.json';definition = json.loads(path.read_text())
    definition['services']['control'] = {
        'image':'node:22-alpine', 'container_name':project+'-control',
        'network_mode':'none', 'read_only':True, 'user':f'{os.getuid()}:{os.getgid()}',
        'volumes':[str(app)+':/app:ro',str(state)+':/state'],
        'entrypoint':['node','--experimental-default-type=module','--disable-warning=ExperimentalWarning','/app/fixture.mjs']}
    path.write_text(json.dumps(definition))
    subprocess.run(compose+['run','--rm','--no-deps','control','--seed'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(compose+['up','-d','control'], check=True, stdout=subprocess.DEVNULL)
    wait_worker(state)
    service = Notifications(root/'master/notifications.local',lambda _:None,lambda:{},push=lambda *_:201)
    with service.db:
        service.db.execute("INSERT INTO devices VALUES('fixture-browser','{}',1)")
        service.db.execute("INSERT INTO history(id,room,event,title,created) VALUES('fixture','!fixture:master','$fixture','Synthetic',1)")
        service.db.execute("INSERT INTO deliveries(id,device,state) VALUES('fixture','fixture-browser','pending')")
    service.db.close()
    (root/'master/notifications.local/vapid.pem').write_text('synthetic-vapid')
    return state


def wait_worker(state):
    deadline = time.monotonic()+20
    while time.monotonic()<deadline:
        try:
            return json.loads((state/'heartbeat.json').read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(.1)
    raise AssertionError('Synthetic control worker did not start')


def assert_restored(root, tokens, current):
    state = root/'master/control.local'
    config = json.loads((state/'config.json').read_text())
    assert config['manager_token'] == tokens['MASTER_MANAGER_TOKEN']
    assert config['telegram_token'] == config['openrouter_key'] == 'synthetic-unused'
    assert set(config['roster']) == {tokens['MASTER_'+name.upper()+'_USER'] for name in tokens['MASTER_TEAMMATES'].split()}
    with sqlite3.connect(state/'control.db') as db:
        operator = db.execute("SELECT v FROM kv WHERE k='operator'").fetchone()
        assert (json.loads(operator[0]) == {'user':8,'chat':8}) if current else operator is None
        assert dict(db.execute('SELECT id,state FROM outbox')) == {'notice:0':'accepted' if current else 'canceled','uncertain:0':'unknown'}
        assert db.execute("SELECT state FROM drafts WHERE id='review'").fetchone()[0] == 'review_required'
        assert db.execute("SELECT state FROM api_commands WHERE id='old-command'").fetchone()[0] == 'canceled'
    with sqlite3.connect(root/'master/notifications.local/notifications.db') as db:
        assert db.execute('SELECT enabled FROM devices').fetchone()[0] == (1 if current else 0)
        assert db.execute('SELECT state FROM deliveries').fetchone()[0] == ('accepted' if current else 'canceled')
