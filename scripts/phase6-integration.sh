#!/usr/bin/env bash
set -euo pipefail

: "${CONTROL_PLANE_ADMIN_TOKEN:?CONTROL_PLANE_ADMIN_TOKEN is required}"
: "${CONTROL_PLANE_INTERNAL_TOKEN:?CONTROL_PLANE_INTERNAL_TOKEN is required}"
: "${CHATWOOT_CI_TOKEN:?CHATWOOT_CI_TOKEN is required}"
: "${CHATWOOT_WEBHOOK_PHASE6_A_SECRET:?CHATWOOT_WEBHOOK_PHASE6_A_SECRET is required}"
: "${CHATWOOT_WEBHOOK_PHASE6_B_SECRET:?CHATWOOT_WEBHOOK_PHASE6_B_SECRET is required}"
: "${MATRIX_CLIENT_CI_TOKEN:?MATRIX_CLIENT_CI_TOKEN is required}"
: "${EGRESS_PROXY_PHASE6_A_PASSWORD:?EGRESS_PROXY_PHASE6_A_PASSWORD is required}"
: "${EGRESS_PROXY_PHASE6_B_PASSWORD:?EGRESS_PROXY_PHASE6_B_PASSWORD is required}"

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.phase6.yaml)

wait_healthy() {
  local service="$1" timeout="$2" cid status end
  cid="$(${DC[@]} ps -q "$service")"
  test -n "$cid"
  end=$((SECONDS + timeout))
  while (( SECONDS < end )); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
    if [[ "$status" == healthy ]]; then return 0; fi
    if [[ "$status" == unhealthy || "$status" == exited || "$status" == dead ]]; then return 1; fi
    sleep 2
  done
  return 1
}

cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

"${DC[@]}" config -q
"${DC[@]}" up -d chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel control-plane
wait_healthy chatwoot-double 60
wait_healthy matrix-client-double 60
wait_healthy proxy-a 60
wait_healthy proxy-b 60
wait_healthy direct-egress-sentinel 60
wait_healthy control-plane 90

# Build two independent tenant routes, prove sticky A/B resolver assignments across
# every protected traffic class, exercise the selected proxy endpoints over real
# TCP, then interleave Matrix -> Chatwoot and Chatwoot -> Matrix traffic.
"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";
import {connect} from "node:net";
const base="http://127.0.0.1:3000";
const admin={authorization:"Bearer "+process.env.CONTROL_PLANE_ADMIN_TOKEN,"content-type":"application/json"};
const internal={authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"};
const post=async(path,body,headers=admin,expected=undefined)=>{const r=await fetch(base+path,{method:"POST",headers,body:JSON.stringify(body)});const text=await r.text();if(expected!==undefined? r.status!==expected : !r.ok){console.error(path,r.status,text);process.exit(1)}return text?JSON.parse(text):{}};
const get=async(path,headers=internal)=>{const r=await fetch(base+path,{headers});const text=await r.text();if(!r.ok){console.error(path,r.status,text);process.exit(1)}return text?JSON.parse(text):{}};

const mk=async(suffix,account,inbox,proxyHost,secretRef,hookRef)=>{
  const tenant=(await post("/api/v1/tenants",{slug:`phase6-${suffix}`,name:`Phase 6 ${suffix.toUpperCase()}`})).data;
  const profileResp=await post("/api/v1/egress-profiles",{provider:"fixture",scheme:"http",host:proxyHost,port:8081,username:`ci-${suffix}`,secretRef,status:"healthy"});
  const profile=profileResp.data;
  if(profile.secretRef!=="[configured]") throw new Error("egress secret reference was not redacted");
  const connection=(await post("/api/v1/meta-connections",{tenantId:tenant.id,matrixOwnerMxid:`@phase6-${suffix}:matrix.example.com`,metaAccountId:`meta-phase6-${suffix}`,mautrixLoginId:`login-phase6-${suffix}`})).data;
  const binding=(await post("/api/v1/chatwoot-bindings",{tenantId:tenant.id,chatwootAccountId:String(account),chatwootInboxId:String(inbox),apiBaseUrl:"http://chatwoot-double:8080",credentialRef:"env:CHATWOOT_CI_TOKEN",status:"active"})).data;
  await post(`/api/v1/meta-connections/${connection.id}/egress`,{egressProfileId:profile.id});
  await post(`/api/v1/meta-connections/${connection.id}/chatwoot`,{chatwootBindingId:binding.id});
  await post(`/api/v1/meta-connections/${connection.id}/activate`,{});
  const hook=(await post(`/api/v1/chatwoot-bindings/${binding.id}/webhook`,{secretRef:hookRef})).data;
  if(hook.secretRef!=="[configured]") throw new Error("webhook secret reference was not redacted");
  return {tenant,profile,connection,binding,hook};
};
const a=await mk("a",1,10,"proxy-a","env:EGRESS_PROXY_PHASE6_A_PASSWORD","env:CHATWOOT_WEBHOOK_PHASE6_A_SECRET");
const b=await mk("b",2,20,"proxy-b","env:EGRESS_PROXY_PHASE6_B_PASSWORD","env:CHATWOOT_WEBHOOK_PHASE6_B_SECRET");

const proxyRequest=(proxyUrl,target)=>new Promise((resolve,reject)=>{
  const p=new URL(proxyUrl); let data=""; let settled=false;
  const socket=connect(Number(p.port),p.hostname,()=>{
    const auth=Buffer.from(`${decodeURIComponent(p.username)}:${decodeURIComponent(p.password)}`).toString("base64");
    socket.write(`GET ${target} HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nProxy-Authorization: Basic ${auth}\r\nConnection: close\r\n\r\n`);
  });
  socket.setTimeout(3000,()=>socket.destroy(new Error("proxy socket timeout")));
  socket.on("data",chunk=>data+=chunk.toString());
  socket.on("error",err=>{if(!settled){settled=true;reject(err)}});
  socket.on("close",()=>{if(!settled){settled=true;resolve(data)}});
});
const traffic=["login","messaging","media","e2ee"];
const assignments={a:new Set(),b:new Set()};
for(const cls of traffic){
  for(const [key,route] of [["a",a],["b",b]]){
    const q=new URLSearchParams({meta_account_id:route.connection.metaAccountId,login_id:route.connection.mautrixLoginId,reason:`phase6-${cls}`,traffic_class:cls});
    const resolved=(await get(`/internal/v1/egress/resolve?${q}`)).data;
    assignments[key].add(resolved.assignment_id);
    const expectedHost=key==="a"?"proxy-a":"proxy-b";
    if(new URL(resolved.proxy_url).hostname!==expectedHost) throw new Error(`tenant ${key} resolved to wrong egress`);
    const response=await proxyRequest(resolved.proxy_url,`http://direct-egress-sentinel:8083/protected/${key}/${cls}`);
    if(!response.startsWith("HTTP/1.1 200")&&!response.startsWith("HTTP/1.0 200")) throw new Error(`proxy ${key} did not accept ${cls}`);
  }
}
if(assignments.a.size!==1||assignments.b.size!==1) throw new Error("egress assignment rotated across traffic classes");
if([...assignments.a][0]===[...assignments.b][0]) throw new Error("tenant A and B share one egress assignment");
const pa=await (await fetch("http://proxy-a:8081/_test/state")).json();
const pb=await (await fetch("http://proxy-b:8081/_test/state")).json();
const direct=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();
if(pa.hits.length!==4||pb.hits.length!==4) throw new Error(`expected four protected hits per egress, got ${pa.hits.length}/${pb.hits.length}`);
if(direct.hits!==0) throw new Error(`protected traffic reached direct sentinel ${direct.hits} times`);

const eventA={connectionId:a.connection.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-a-1",senderId:"contact-phase6-a",text:"from meta A",occurredAt:"2026-09-11T14:00:00.000Z",provenance:"meta"};
const eventB={connectionId:b.connection.id,roomId:"!phase6-b:matrix.example.com",remoteThreadId:"thread-phase6-b",remoteContactId:"contact-phase6-b",eventId:"$phase6-b-1",senderId:"contact-phase6-b",text:"from meta B",occurredAt:"2026-09-11T14:00:01.000Z",provenance:"meta"};
if((await post("/internal/v1/matrix/events",eventA,internal)).data?.status!=="delivered") throw new Error("tenant A Matrix ingress failed");
if((await post("/internal/v1/matrix/events",eventB,internal)).data?.status!=="delivered") throw new Error("tenant B Matrix ingress failed");
if((await post("/internal/v1/matrix/events",eventA,internal)).data?.status!=="duplicate") throw new Error("tenant A duplicate Matrix event was not suppressed");
const cw=await (await fetch("http://chatwoot-double:8080/_test/state")).json();
const msgA=cw.messages.find(m=>m.inboxId===10), msgB=cw.messages.find(m=>m.inboxId===20);
if(!msgA||!msgB||cw.messages.length!==2) throw new Error("distinct Chatwoot routes were not observed");
if(msgA.content!=="from meta A"||msgB.content!=="from meta B") throw new Error("Chatwoot messages crossed tenant routes");
const convA=cw.conversations.find(c=>c.inboxId===10), convB=cw.conversations.find(c=>c.inboxId===20);
if(!convA||!convB||convA.id===convB.id) throw new Error("distinct Chatwoot conversations were not created");

const sign=async(route,secret,payload,expected=200)=>{
  const raw=JSON.stringify(payload),ts=String(Math.floor(Date.now()/1000));
  const sig="sha256="+createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");
  const r=await fetch(base+`/webhooks/chatwoot/${route.binding.id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig,"x-chatwoot-delivery":`phase6-${route.binding.id}-${payload.id}`},body:raw});
  const text=await r.text(); if(r.status!==expected){console.error(r.status,text);process.exit(1)} return text?JSON.parse(text):{};
};
const payloadA={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent A",created_at:"2026-09-11T14:01:00.000Z",account:{id:1},inbox:{id:10},conversation:{id:convA.id},sender:{id:81,type:"user",name:"Agent A"},attachments:[]};
const payloadB={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent B",created_at:"2026-09-11T14:01:01.000Z",account:{id:2},inbox:{id:20},conversation:{id:convB.id},sender:{id:82,type:"user",name:"Agent B"},attachments:[]};
if((await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,payloadA)).data?.status!=="delivered") throw new Error("tenant A webhook failed");
if((await sign(b,process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET,payloadB)).data?.status!=="delivered") throw new Error("tenant B webhook failed");
if((await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,payloadA)).data?.status!=="duplicate") throw new Error("tenant A duplicate webhook was not suppressed");
const mismatch=await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,{...payloadA,id:6002,account:{id:2},inbox:{id:20},conversation:{id:convB.id}},409);
if(mismatch.error?.code!=="CHATWOOT_ROUTE_MISMATCH") throw new Error("cross-tenant Chatwoot mismatch did not fail closed");
const mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();
const mxA=mx.events.filter(e=>e.roomId==="!phase6-a:matrix.example.com"), mxB=mx.events.filter(e=>e.roomId==="!phase6-b:matrix.example.com");
if(mxA.length!==1||mxB.length!==1||mx.events.length!==2) throw new Error("Matrix destinations crossed tenants or duplicated");
if(mxA[0].content.body!=="agent A"||mxB[0].content.body!=="agent B") throw new Error("Matrix reply contents crossed tenants");

console.log(JSON.stringify({a:{connectionId:a.connection.id,bindingId:a.binding.id,assignmentId:[...assignments.a][0]},b:{connectionId:b.connection.id,bindingId:b.binding.id,assignmentId:[...assignments.b][0]}}));
' > /tmp/phase6-state.json

# Assigned proxy unavailable: keep resolver assignment sticky, make A unavailable,
# and prove a protected request fails at A without ever reaching direct sentinel.
"${DC[@]}" exec -T control-plane bun -e 'await fetch("http://proxy-a:8081/_test/down",{method:"POST"})'
"${DC[@]}" exec -T control-plane bun -e '
import {connect} from "node:net";
const r=await fetch("http://127.0.0.1:3000/internal/v1/egress/resolve?meta_account_id=meta-phase6-a&login_id=login-phase6-a&reason=proxy-failure&traffic_class=messaging",{headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN}});if(!r.ok)process.exit(1);const p=(await r.json()).data;const u=new URL(p.proxy_url);
const result=await new Promise(resolve=>{let data="";const s=connect(Number(u.port),u.hostname,()=>s.write("GET http://direct-egress-sentinel:8083/protected/failure HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nConnection: close\r\n\r\n"));s.setTimeout(3000,()=>s.destroy());s.on("data",c=>data+=c);s.on("error",()=>resolve("transport-error"));s.on("close",()=>resolve(data));});
if(typeof result!=="string"||(!result.includes(" 502 ")&&result!=="transport-error")) throw new Error("unavailable assigned egress did not fail");
const direct=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();if(direct.hits!==0) throw new Error("proxy failure fell back to direct sentinel");
'
"${DC[@]}" exec -T control-plane bun -e 'await fetch("http://proxy-a:8081/_test/up",{method:"POST"})'

# Chatwoot API timeout: pause the only Chatwoot endpoint, require the Matrix ingress
# to fail rather than route elsewhere, then retry the exact same event after recovery.
"${DC[@]}" pause chatwoot-double >/dev/null
"${DC[@]}" exec -T control-plane bun -e '
const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true});const row=d.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");d.close();
const ev={connectionId:row.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-timeout-a",senderId:"contact-phase6-a",text:"timeout A",occurredAt:"2026-09-11T14:02:00.000Z",provenance:"meta"};
const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"},body:JSON.stringify(ev)});if(r.ok){console.error(await r.text());throw new Error("Chatwoot timeout unexpectedly succeeded")}
'
"${DC[@]}" unpause chatwoot-double >/dev/null
wait_healthy chatwoot-double 30
"${DC[@]}" exec -T control-plane bun -e '
const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true});const row=d.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");d.close();
const ev={connectionId:row.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-timeout-a",senderId:"contact-phase6-a",text:"timeout A",occurredAt:"2026-09-11T14:02:00.000Z",provenance:"meta"};
const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"},body:JSON.stringify(ev)});const body=await r.json();if(!r.ok||body.data?.status!=="delivered"){console.error(r.status,body);process.exit(1)}
const state=await (await fetch("http://chatwoot-double:8080/_test/state")).json();if(state.messages.filter(m=>m.sourceEventId==="$phase6-timeout-a").length!==1) throw new Error("timeout retry duplicated or lost Chatwoot message");
'

# Restart persistence: both conversation bindings, processed-event records and
# egress assignments must survive. Replays in both directions must remain no-ops.
"${DC[@]}" restart control-plane >/dev/null
wait_healthy control-plane 90
"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";
const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true});
const conns=d.query("select id,meta_account_id,mautrix_login_id from meta_connections where meta_account_id in (?,?) order by meta_account_id").all("meta-phase6-a","meta-phase6-b");
const rows=d.query("select cb.id as binding_id,cb.chatwoot_account_id,cb.chatwoot_inbox_id,conv.chatwoot_conversation_id,conv.matrix_room_id from chatwoot_bindings cb join conversation_bindings conv on conv.chatwoot_account_id=cb.chatwoot_account_id and conv.chatwoot_inbox_id=cb.chatwoot_inbox_id where cb.chatwoot_inbox_id in (?,?) order by cb.chatwoot_inbox_id").all("10","20");d.close();
if(conns.length!==2||rows.length!==2) throw new Error("restart lost durable routing identity");
const internal={authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"};
for(const [i,c] of conns.entries()){
 const suffix=i===0?"a":"b",room=`!phase6-${suffix}:matrix.example.com`,thread=`thread-phase6-${suffix}`,contact=`contact-phase6-${suffix}`,eventId=`$phase6-${suffix}-1`;
 const ev={connectionId:c.id,roomId:room,remoteThreadId:thread,remoteContactId:contact,eventId,senderId:contact,text:`from meta ${suffix.toUpperCase()}`,occurredAt:i===0?"2026-09-11T14:00:00.000Z":"2026-09-11T14:00:01.000Z",provenance:"meta"};
 const rr=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:internal,body:JSON.stringify(ev)});const rb=await rr.json();if(!rr.ok||rb.data?.status!=="duplicate") throw new Error(`restart Matrix replay ${suffix} was not duplicate`);
 const q=new URLSearchParams({meta_account_id:c.meta_account_id,login_id:c.mautrix_login_id,reason:"restart",traffic_class:"messaging"});const er=await fetch(`http://127.0.0.1:3000/internal/v1/egress/resolve?${q}`,{headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN}});if(!er.ok) throw new Error(`restart egress resolve ${suffix} failed`);
 const eu=new URL((await er.json()).data.proxy_url);if(eu.hostname!==(suffix==="a"?"proxy-a":"proxy-b")) throw new Error(`restart egress assignment changed for ${suffix}`);
}
const sign=async(row,secret,id,content)=>{const payload={event:"message_created",id,message_type:"outgoing",private:false,content,created_at:"2026-09-11T14:01:00.000Z",account:{id:Number(row.chatwoot_account_id)},inbox:{id:Number(row.chatwoot_inbox_id)},conversation:{id:Number(row.chatwoot_conversation_id)},sender:{id:80,type:"user",name:"Agent"},attachments:[]};const raw=JSON.stringify(payload),ts=String(Math.floor(Date.now()/1000)),sig="sha256="+createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");const r=await fetch(`http://127.0.0.1:3000/webhooks/chatwoot/${row.binding_id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig},body:raw});const body=await r.json();if(!r.ok||body.data?.status!=="duplicate") throw new Error("restart webhook replay was not duplicate")};
await sign(rows[0],process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,6001,"agent A");await sign(rows[1],process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET,6001,"agent B");
const cw=await (await fetch("http://chatwoot-double:8080/_test/state")).json();if(cw.messages.length!==3) throw new Error(`restart produced Chatwoot duplicates: ${cw.messages.length}`);
const mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();if(mx.events.length!==2) throw new Error(`restart produced Matrix duplicates: ${mx.events.length}`);
const direct=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();if(direct.hits!==0) throw new Error("restart/failure path reached direct sentinel");
'

# Security evidence: all synthetic credential canaries must remain absent from
# service logs after both success and failure paths.
logs="$("${DC[@]}" logs --no-color control-plane chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel 2>&1 || true)"
for secret in \
  "$CONTROL_PLANE_ADMIN_TOKEN" \
  "$CONTROL_PLANE_INTERNAL_TOKEN" \
  "$CHATWOOT_CI_TOKEN" \
  "$CHATWOOT_WEBHOOK_PHASE6_A_SECRET" \
  "$CHATWOOT_WEBHOOK_PHASE6_B_SECRET" \
  "$MATRIX_CLIENT_CI_TOKEN" \
  "$EGRESS_PROXY_PHASE6_A_PASSWORD" \
  "$EGRESS_PROXY_PHASE6_B_PASSWORD"; do
  if printf "%s" "$logs" | grep -Fq "$secret"; then
    echo "Synthetic Phase 6 secret leaked into logs" >&2
    exit 1
  fi
done

echo "Phase 6 multi-tenant production proof passed"
