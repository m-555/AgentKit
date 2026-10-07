"""Lifecycle categories, real tab interactions and read-only runtime detail."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit import dashboard, db, jobs


def test_categories_and_tabs_keep_dead_history_out_of_working():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for view interactions")
    folder = Path(dashboard.__file__).parent
    sources = {name: (folder / name).read_text(encoding="utf-8") for name in
               ("dashboard_categories.js", "dashboard_view.js")}
    program = r"""
const assert=require('assert'),fs=require('fs');const src=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.events={};this.attrs={};this.value='all';}
 append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
 addEventListener(name,callback){this.events[name]=callback;}setAttribute(key,value){this.attrs[key]=value;}
 get childElementCount(){return this.children.length;}focus(){this.focused=true;}
}
const ids=Object.fromEntries(['project','allowance','sessions','tasks','summary','history-controls','history-filter',
 'page-controls','page-previous','page-next','page-range','tab-explanation','sessions-heading','tasks-heading','notice','connection','activity-panel','tab-working','tab-waiting','tab-attention','tab-history']
 .map(id=>[id,new Element(id)]));
global.document={getElementById:id=>ids[id],createElement:tag=>new Element(tag)};
global.window={AgentKitAllowance:{render(){}},AgentKitTeam:{updateSnapshot(){}}};
global.setTimeout=()=>0;global.clearTimeout=()=>{};
eval(src['dashboard_categories.js']);const api=window.AgentKitCategories;
const failed={id:1,purpose:'worker',task_id:4,state:'FAILED',status:'FAILED',monitor_alive:false,child_alive:false};
const live={...failed,id:2,state:'RUNNING',status:'RUNNING',monitor_alive:true};
const moved={...failed,id:3,continuation:{target_provider:'codex',target_model:'sol',generation:2}};
assert.equal(api.classify(live,{status:'READY'},false).bucket,'working');
assert.equal(api.classify(failed,null,false).historyKind,'crashed');
assert.equal(api.classify(moved,null,false).historyKind,'transferred');
assert.equal(api.classify({...moved,child_alive:true},null,false).bucket,'attention');
assert.equal(api.classify({...failed,ownership_uncertain:true},null,false).bucket,'attention');
assert.equal(api.classify({state:'STALE'},null,true).historyKind,'stale');
assert.equal(api.classify({...failed,progress:'WAITING_QUOTA'},null,false).bucket,'history');
assert.equal(api.classify({status:'DONE'},null,false).historyKind,'done');
assert.equal(api.classify({status:'CANCELLED'},null,false).historyKind,'cancelled');
const task={id:9,title:'Example',role:'backend-builder',status:'PLANNED',job_state:'PLANNING',dependencies:[{id:'types',status:'READY'}]};
const reason=api.classify(task,task,false,{execution_paused:true}).reason;
assert(reason.includes('paused')&&reason.includes('PLANNING')&&reason.includes('types'));
const view={project:'demo',execution_paused:true,tasks:[task],processes:[live,failed,moved],external_managers:[{state:'STALE'}]};
global.fetch=async()=>({ok:true,json:async()=>view});
const descendants=item=>[item,...item.children.flatMap(descendants)];
(async()=>{
 eval(src['dashboard_view.js']);for(let i=0;i<4;i++)await new Promise(resolve=>setImmediate(resolve));
 assert.equal(ids['tab-working'].attrs['aria-selected'],'true');assert.equal(ids.tasks.children.length,0);
 assert(!descendants(ids.sessions).some(item=>item.textContent==='FAILED'));
 ids['tab-waiting'].events.click();assert.equal(ids['tab-waiting'].attrs['aria-selected'],'true');
 assert.equal(ids.tasks.children.length,1);assert(descendants(ids.tasks).some(item=>String(item.textContent).includes('Prerequisites')));
 ids['tab-history'].events.click();assert.equal(ids['activity-panel'].attrs['aria-labelledby'],'tab-history');
 ids['history-filter'].value='transferred';ids['history-filter'].events.change();assert.equal(ids.sessions.children.length,1);
 assert(descendants(ids.sessions).some(item=>item.textContent==='TRANSFERRED'));
 ids['tab-history'].events.keydown({key:'Home',preventDefault(){}});assert(ids['tab-working'].focused);
 for(let i=0;i<24;i++)view.tasks.push({...task,id:100+i,title:'Bounded '+i});
 ids['tab-waiting'].events.click();assert.equal(ids.tasks.children.length,12);
 assert.equal(ids['page-range'].textContent,'1 - 12 of 25');assert(ids['page-previous'].disabled);
 ids['page-next'].events.click();assert.equal(ids.tasks.children.length,12);assert.equal(ids['page-range'].textContent,'13 - 24 of 25');
 ids['page-next'].events.click();assert.equal(ids.tasks.children.length,1);assert(ids['page-next'].disabled);
 ids['tab-working'].events.click();ids['tab-waiting'].events.click();assert.equal(ids['page-range'].textContent,'25 - 25 of 25');
 ids['page-previous'].events.click();assert.equal(ids['page-range'].textContent,'13 - 24 of 25');
 process.stdout.write('categories and tabs passed');
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", program], input=json.dumps(sources),
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_waiting_and_handoff_metadata_are_read_only(project_root, conn, monkeypatch):
    jobs.create(project_root, "pending", "Example job")
    task_id = db.create_task(conn, title="waiting", job_id="pending", depends_on=["types"], blocker="Needs qualification")
    conn.execute("INSERT INTO handoffs(task_id,generation,source_provider,source_model,target_provider,target_model,created_at) VALUES(?,?,?,?,?,?,?)",
                 (task_id, 2, "claude-code", "opus", "codex", "sol", db.utcnow()))
    view = {"tasks": [{"id": task_id, "spec_id": "waiting", "status": "READY"},
                      {"id": 99, "spec_id": "types", "status": "PLANNED"}],
            "processes": [{"id": 1, "task_id": task_id, "provider": "claude-code", "requested_model": "opus", "generation": 1}],
            "external_managers": []}
    monkeypatch.setattr(dashboard.live, "snapshot", lambda root: view)
    before = list(conn.iterdump())
    result = dashboard.build_snapshot(project_root)
    assert result['tasks'][0]['job_state'] == 'PLANNING'
    assert result['tasks'][0]['dependencies'] == [{'id': 'types', 'status': 'PLANNED'}]
    assert result['processes'][0]['continuation']['target_provider'] == 'codex'
    assert list(conn.iterdump()) == before
    view['processes'][0]['requested_model'] = 'another-model'
    view['processes'][0].pop('continuation')
    assert 'continuation' not in dashboard.build_snapshot(project_root)['processes'][0]
