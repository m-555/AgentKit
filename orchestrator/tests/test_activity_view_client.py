"""Execute browser UI behavior with a small DOM; no model/service dependencies."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit import dashboard, repo, workspace_view


def test_actual_window_mirrors_preserve_display_pause_and_worker_readonly():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = Path(dashboard.__file__).with_name("dashboard_activity.js").read_text(encoding="utf-8")
    program = r"""
const fs = require('fs');
class Element {
 constructor(tag) { this.tagName=tag; this.children=[]; this.events={}; this.textContent='';
   this.scrollTop=0; this.scrollHeight=500; this.clientHeight=200; }
 append(...items) { for (const item of items) { item.parent=this; this.children.push(item); } }
 replaceChildren(...items) { this.children=[]; this.append(...items); }
 setAttribute(name,value) { this[name]=value; }
 addEventListener(name,handler) { this.events[name]=handler; }
 remove() { if(this.parent) this.parent.children=this.parent.children.filter(item=>item!==this); }
 querySelectorAll(selector) { return this.children.filter(item=>'.'+item.className===selector); }
 get firstChild() { return this.children[0]; }
}
const ids=Object.fromEntries(['project-usage','usage-coverage','usage-breakdown','live-screens'].map(id=>[id,new Element('div')]));
global.window={}; global.document={getElementById:id=>ids[id],createElement:tag=>new Element(tag)};
let calls=0, revoked=[];
global.URL={createObjectURL:()=> 'blob:frame-'+calls,revokeObjectURL:url=>revoked.push(url)};
global.fetch=async (path, options)=>{ calls++;
 if(path!='/api/terminal/process-7' || options.method) throw Error('Unexpected endpoint or mutation');
 return {ok:true,blob:async()=>({type:'image/png'})}; };
eval(fs.readFileSync(0,'utf8'));
const view={tasks:[{id:47,title:'Scoped source change'}],processes:[{id:7,task_id:47,provider:'claude-code',role:'backend-builder',monitor_alive:true,status:'RUNNING',state:'RUNNING',worktree:'E:/worktree'}],
 project_usage:{recorded_tokens:225,normalized_input_tokens:185,counters:{output_tokens:40,thinking_tokens:16},
 complete_launches:2,partial_launches:0,missing_launches:0,launches:2,external_sessions_unmetered:0,providers:[],roles:[]}};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 window.AgentKitActivity.update(view); await flush();
 const card=ids['live-screens'].children[0], screen=card.children[5];
 if(!screen || screen.className!=='terminal-monitor') throw Error('Missing actual window monitor');
 const actualImage=screen.children[0].tagName==='img' && screen.children[0].src==='blob:frame-1';
 const readonly=card.children.every(item=>item.tagName!=='form');
 screen.scrollTop=45;
 window.AgentKitActivity.update(view); await flush();
 const retained=ids['live-screens'].children[0]===card && screen.children.length===1 && screen.scrollTop===45;
 const pause=card.children[3].children[0]; pause.events.click();
 const before=calls;
 window.AgentKitActivity.update(view); await flush();
 const paused=calls===before && pause.textContent==='Resume display';
 pause.events.click();
 window.AgentKitActivity.update(view); await flush();
 const resumed=screen.children[0].src==='blob:frame-3';
 const total=ids['project-usage'].children[0].children[0].textContent;
 window.AgentKitActivity.update({...view,processes:[]});
 const closed=ids['live-screens'].children.length===1 && ids['live-screens'].children[0].className==='empty';
 console.log(JSON.stringify({actualImage,readonly,retained,paused,resumed,total,closed,calls,revoked:revoked.length}));
})().catch(error=>{ console.error(error); process.exit(1); });
"""
    completed = subprocess.run([node, "-e", program], input=source, text=True,
                               capture_output=True, timeout=15, check=True)
    result = json.loads(completed.stdout)
    assert all(result[key] for key in ("actualImage", "readonly", "retained", "paused", "resumed", "closed"))
    assert result["total"] == "225" and result["calls"] == 3
    assert result["revoked"] == 3
    assert "innerHTML" not in source and "insertAdjacentHTML" not in source


def test_workspace_view_reports_actual_integration_checkout_without_switching(project_root, conn, tmp_path):
    integration = tmp_path / "combined-preview"
    subprocess.run(["git", "worktree", "add", "-b", "integration", str(integration)],
                   cwd=project_root, capture_output=True, check=True)
    before = repo.current_branch(project_root)
    head = repo.head_commit(integration)
    result = workspace_view.snapshot(project_root)
    assert result["integration"]["branch"] == "integration"
    assert Path(result["integration"]["checkout"]).resolve() == integration.resolve()
    assert result["integration"]["head"] == head
    assert repo.current_branch(project_root) == before


def test_manager_window_input_sends_once_and_disables_on_missing_window():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = Path(dashboard.__file__).with_name("dashboard_activity.js").read_text(encoding="utf-8")
    program = r"""
const fs = require('fs');
class Element {
 constructor(tag) { this.tagName=tag; this.children=[]; this.events={}; this.textContent=''; }
 append(...items) { for(const item of items) {item.parent=this; this.children.push(item);} }
 replaceChildren(...items) {this.children=[]; this.append(...items);}
 setAttribute(name,value) {this[name]=value;}
 addEventListener(name,handler) {this.events[name]=handler;}
 remove() {if(this.parent) this.parent.children=this.parent.children.filter(item=>item!==this);}
 querySelectorAll(selector) {return this.children.filter(item=>'.'+item.className===selector);}
}
const target=new Element('div');
global.window={}; global.document={getElementById:id=>id==='live-screens'?target:null,createElement:tag=>new Element(tag)};
global.URL={createObjectURL:()=> 'blob:manager',revokeObjectURL:()=>{}};
let available=false, delivered=[], terminalCalls=0;
global.fetch=async (path, options={})=>{
 if(path==='/api/terminal/manager-demo') {
  terminalCalls++; return {ok:available,blob:async()=>({type:'image/png'}),json:async()=>({message:'No live registered CLI window'})};
 }
 if(path==='/api/reviews') return {ok:true,json:async()=>({enabled:true,token:'operator-token'})};
 if(path==='/api/terminal/input') {
  if(options.method!=='POST' || options.headers['X-AgentKit-Token']!=='operator-token') throw Error('Missing operator guard');
  delivered.push(JSON.parse(options.body)); return {ok:true,json:async()=>({message:'Input delivered'})};
 }
 throw Error('Unexpected endpoint');
};
eval(fs.readFileSync(0,'utf8'));
const view={external_managers:[{job_id:'demo',provider:'codex',state:'ACTIVE'}]};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 window.AgentKitActivity.update(view); await flush();
 const card=target.children[0], form=card.children[6], input=form.children[0], send=form.children[1];
 if(form.tagName!=='form' || !send.disabled) throw Error('Missing manager form or absent-window guard');
 input.value='Continue the current job';
 await form.events.submit({preventDefault:()=>{}});
 if(delivered.length) throw Error('Sent to unavailable manager');
 available=true; window.AgentKitActivity.update(view); await flush();
 if(send.disabled) throw Error('Live manager input unavailable');
 await form.events.submit({preventDefault:()=>{}});
 if(delivered.length!==1 || delivered[0].job_id!=='demo' || input.value!=='') throw Error('Manager message delivery failed');
 available=false; window.AgentKitActivity.update(view); await flush();
 if(!send.disabled) throw Error('Input left enabled after ownership/window loss');
 console.log(JSON.stringify({delivered:delivered.length,terminalCalls}));
})().catch(error=>{console.error(error);process.exit(1);});
"""
    completed = subprocess.run([node, "-e", program], input=source, text=True,
                               capture_output=True, timeout=15, check=True)
    assert json.loads(completed.stdout) == {"delivered": 1, "terminalCalls": 3}
