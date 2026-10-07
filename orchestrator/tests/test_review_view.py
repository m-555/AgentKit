"""Execute review-card interactions, stale-version guards and note preservation."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentkit import dashboard


def test_review_card_preserves_notes_blocks_stale_approval_and_allows_followup():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser-side workflow checks")
    source = Path(dashboard.__file__).with_name("dashboard_review.js").read_text(encoding="utf-8")
    program = r"""
const fs = require('fs');const assert = require('assert');
class Element {
 constructor(tag) {this.tag=tag;this.children=[];this.events={};this.value='';this.checked=false;}
 append(...items) {for (const item of items) {item.parent=this;this.children.push(item);}}
 addEventListener(name, callback) {this.events[name]=callback;}
 setAttribute() {}
 remove() {this.parent.children=this.parent.children.filter(child=>child!==this);}
}
const roots = Object.fromEntries(['review-jobs','review-status','dashboard-access'].map(id=>[id,new Element(id)]));
global.window={};global.document={getElementById:id=>roots[id],createElement:tag=>new Element(tag),dispatchEvent() {}};
let interval;global.setInterval=callback=>{interval=callback;};
let packet={job_id:'demo',revision:1,head:'a'.repeat(40),digest:'1'.repeat(64),status:'AWAITING_USER',review:'human',
 ready:true,preview_path:'/preview',requests:[{text:'Preserve behavior'}],acceptance:['Clear error'],
 cancelled_tasks:[],reasons:[],checks:{passed:true}};
const calls=[];
global.fetch=async(route, options={})=>{
 if (options.method==='POST') {
  const body=JSON.parse(options.body);calls.push(body);
  return {ok:true,json:async()=>({status:body.verdict==='PASS'?'DONE':'PLANNING',head:body.head})};
 }
 return {ok:true,json:async()=>({enabled:true,token:'operator-token',jobs:[packet]})};
};
const settle=async()=>{for(let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve));};
const descendants=item=>[item,...item.children.flatMap(descendants)];
const controls=()=>{
 const all=descendants(roots['review-jobs']);
 return {notes:all.find(item=>item.tag==='textarea'),tested:all.find(item=>item.tag==='input'),
  approve:all.find(item=>item.textContent==='Approve tested preview'),changes:all.find(item=>item.textContent==='Request changes'),
  refresh:all.find(item=>item.textContent==='Load changed preview')};
};
(async()=>{
 eval(JSON.parse(fs.readFileSync(0,'utf8')).source);await settle();
 let view=controls();assert(view.approve.disabled);
 view.notes.value='Tested the original version';view.notes.events.input();view.tested.checked=true;view.tested.events.change();
 assert(!view.approve.disabled);
 await interval();await settle();assert.strictEqual(controls().notes,view.notes);assert.equal(view.notes.value,'Tested the original version');
 packet={...packet,head:'b'.repeat(40),digest:'2'.repeat(64)};
 await interval();await settle();assert(view.approve.disabled);assert(view.changes.disabled);assert(!view.refresh.hidden);
 view.refresh.events.click();await settle();view=controls();assert(!view.tested.checked);assert.equal(view.notes.value,'');
 view.notes.value='Tested the updated version';view.notes.events.input();view.tested.checked=true;view.tested.events.change();
 view.approve.events.click();await settle();assert.equal(calls[0].head,packet.head);assert.equal(calls[0].digest,packet.digest);
 assert(view.approve.hidden);assert(view.changes.disabled);
 packet={...packet,status:'DONE'};await interval();await settle();
 view.notes.value='A follow-up error needs correction';view.notes.events.input();assert(!view.changes.disabled);
 view.changes.events.click();await settle();assert.equal(calls[1].verdict,'CHANGES');assert.equal(calls[1].head,packet.head);
 process.stdout.write('review interactions passed');
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", program], input=json.dumps({"source": source}),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "review interactions passed"
