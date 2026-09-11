#!/usr/bin/env bash
set -euo pipefail

for name in CONTROL_PLANE_ADMIN_TOKEN CONTROL_PLANE_INTERNAL_TOKEN CHATWOOT_CI_TOKEN CHATWOOT_WEBHOOK_PHASE6_A_SECRET CHATWOOT_WEBHOOK_PHASE6_B_SECRET MATRIX_CLIENT_CI_TOKEN EGRESS_PROXY_PHASE6_A_PASSWORD EGRESS_PROXY_PHASE6_B_PASSWORD; do
  test -n "${!name:-}" || { echo "$name is required" >&2; exit 1; }
done

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.phase6.yaml)
cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
wait_healthy() {
  local service="$1" end=$((SECONDS + ${2:-90})) cid status
  cid="$(${DC[@]} ps -q "$service")"; test -n "$cid"
  while ((SECONDS < end)); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" == unhealthy || "$status" == exited || "$status" == dead ]] && return 1
    sleep 2
  done
  return 1
}

"${DC[@]}" config -q
"${DC[@]}" up -d chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel control-plane
for service in chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel control-plane; do wait_healthy "$service" 90; done

"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";
import {connect} from "node:net";
const base="http://127.0.0.1:3000";
const admin={authorization:`Bearer ${process.env.CONTROL_PLANE_ADMIN_TOKEN}`,"content-type":"application/json"};
const internal={authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`,"content-type":"application/json"};
const req=async(method,path,body,headers=admin,expected)=>{const r=await fetch(base+path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});const text=await r.text();const parsed=text?JSON.parse(text):{};if(expected!==undefined?r.status!==expected:!r.ok)throw new Error(`${method} ${path}: ${r.status} ${text}`);return parsed};
const post=(path,body,headers=admin,expected)=>req("POST",path,body,headers,expected);
const get=(path,headers=internal)=>req("GET",path,undefined,headers);
const mk=async(suffix,account,inbox,proxyHost,secretRef,hookRef)=>{
 const tenant=(await post("/api/v1/tenants",{slug:`phase6-${suffix}`,name:`Phase 6 ${suffix.toUpperCase()}`})).data;
 const pr=await post("/api/v1/egress-profiles",{provider:"fixture",scheme:"http",host:proxyHost,port:8081,username:`ci-${suffix}`,secretRef,status:"healthy"}); if(pr.data.secretRef!=="[configured]")throw new Error("egress secret ref exposed");
 const connection=(await post("/api/v1/meta-connections",{tenantId:tenant.id,matrixOwnerMxid:`@phase6-${suffix}:matrix.example.com`,metaAccountId:`meta-phase6-${suffix}`,mautrixLoginId:`login-phase6-${suffix}`})).data;
 const binding=(await post("/api/v1/chatwoot-bindings",{tenantId:tenant.id,chatwootAccountId:String(account),chatwootInboxId:String(inbox),apiBaseUrl:"http://chatwoot-double:8080",credentialRef:"env:CHATWOOT_CI_TOKEN",status:"active"})).data;
 await post(`/api/v1/meta-connections/${connection.id}/egress`,{egressProfileId:pr.data.id}); await post(`/api/v1/meta-connections/${connection.id}/chatwoot`,{chatwootBindingId:binding.id}); await post(`/api/v1/meta-connections/${connection.id}/activate`,{});
 const hook=(await post(`/api/v1/chatwoot-bindings/${binding.id}/webhook`,{secretRef:hookRef})).data; if(hook.secretRef!=="[configured]")throw new Error("webhook secret ref exposed");
 return {tenant,connection,binding};
};
const a=await mk("a",1,10,"proxy-a","env:EGRESS_PROXY_PHASE6_A_PASSWORD","env:CHATWOOT_WEBHOOK_PHASE6_A_SECRET");
const b=await mk("b",2,20,"proxy-b","env:EGRESS_PROXY_PHASE6_B_PASSWORD","env:CHATWOOT_WEBHOOK_PHASE6_B_SECRET");
const proxyRequest=(proxyUrl,target)=>new Promise((resolve,reject)=>{const p=new URL(proxyUrl);let data="",done=false;const s=connect(Number(p.port),p.hostname,()=>{const auth=Buffer.from(`${decodeURIComponent(p.username)}:${decodeURIComponent(p.password)}`).toString("base64");s.write(`GET ${target} HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nProxy-Authorization: Basic ${auth}\r\nConnection: close\r\n\r\n`)});s.setTimeout(3000,()=>s.destroy(new Error("timeout")));s.on("data",c=>data+=c);s.on("error",e=>{if(!done){done=true;reject(e)}});s.on("close",()=>{if(!done){done=true;resolve(data)}})});
const assignments={a:new Set(),b:new Set()};
for(const cls of ["login","messaging","media","e2ee"])for(const [key,route] of [["a",a],["b",b]]){const q=new URLSearchParams({meta_account_id:route.connection.metaAccountId,login_id:route.connection.mautrixLoginId,reason:`phase6-${cls}`,traffic_class:cls});const e=await get(`/internal/v1/egress/resolve?${q}`);assignments[key].add(e.assignment_id);if(new URL(e.proxy_url).hostname!==(key==="a"?"proxy-a":"proxy-b"))throw new Error(`wrong ${key} proxy`);const response=await proxyRequest(e.proxy_url,`http://direct-egress-sentinel:8083/protected/${key}/${cls}`);if(!response.includes(" 200 "))throw new Error(`proxy ${key}/${cls} failed`)}
if(assignments.a.size!==1||assignments.b.size!==1||[...assignments.a][0]===[...assignments.b][0])throw new Error("assignment isolation failed");
const pa=await (await fetch("http://proxy-a:8081/_test/state")).json(),pb=await (await fetch("http://proxy-b:8081/_test/state")).json(),direct=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();if(pa.hits.length!==4||pb.hits.length!==4||direct.hits!==0)throw new Error("observable egress isolation failed");
const eventA={connectionId:a.connection.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-a-1",senderId:"contact-phase6-a",text:"from meta A",occurredAt:"2026-09-11T14:00:00.000Z",provenance:"meta"};
const eventB={connectionId:b.connection.id,roomId:"!phase6-b:matrix.example.com",remoteThreadId:"thread-phase6-b",remoteContactId:"contact-phase6-b",eventId:"$phase6-b-1",senderId:"contact-phase6-b",text:"from meta B",occurredAt:"2026-09-11T14:00:01.000Z",provenance:"meta"};
if((await post("/internal/v1/matrix/events",eventA,internal)).data.status!=="delivered"||(await post("/internal/v1/matrix/events",eventB,internal)).data.status!=="delivered"||(await post("/internal/v1/matrix/events",eventA,internal)).data.status!=="duplicate")throw new Error("Matrix->Chatwoot idempotency/routing failed");
const cw=await (await fetch("http://chatwoot-double:8080/_test/state")).json();const convA=cw.conversations.find(x=>x.inboxId===10),convB=cw.conversations.find(x=>x.inboxId===20);if(cw.messages.length!==2||!convA||!convB||convA.id===convB.id)throw new Error("Chatwoot A/B route split failed");
const sign=async(route,secret,payload,expected=200)=>{const raw=JSON.stringify(payload),ts=String(Math.floor(Date.now()/1000)),sig="sha256="+createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");return post(`/webhooks/chatwoot/${route.binding.id}`,payload,{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig,"x-chatwoot-delivery":`phase6-${route.binding.id}-${payload.id}`},expected)};
const payloadA={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent A",created_at:"2026-09-11T14:01:00.000Z",account:{id:1},inbox:{id:10},conversation:{id:convA.id},sender:{id:81,type:"user",name:"Agent A"},attachments:[]};
const payloadB={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent B",created_at:"2026-09-11T14:01:01.000Z",account:{id:2},inbox:{id:20},conversation:{id:convB.id},sender:{id:82,type:"user",name:"Agent B"},attachments:[]};
if((await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,payloadA)).data.status!=="delivered"||(await sign(b,process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET,payloadB)).data.status!=="delivered"||(await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,payloadA)).data.status!=="duplicate")throw new Error("Chatwoot->Matrix A/B routing/idempotency failed");
const mismatch=await sign(a,process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,{...payloadA,id:6002,account:{id:2},inbox:{id:20},conversation:{id:convB.id}},409);if(mismatch.error?.code!=="CHATWOOT_ROUTE_MISMATCH")throw new Error("cross-tenant route did not fail closed");
const mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();if(mx.events.length!==2||mx.events.filter(x=>x.roomId==="!phase6-a:matrix.example.com").length!==1||mx.events.filter(x=>x.roomId==="!phase6-b:matrix.example.com").length!==1)throw new Error("Matrix A/B destinations crossed");
' 

# Assigned proxy unavailable must not fall back to direct egress.
"${DC[@]}" exec -T control-plane bun -e 'await fetch("http://proxy-a:8081/_test/down",{method:"POST"});import {connect} from "node:net";const r=await fetch("http://127.0.0.1:3000/internal/v1/egress/resolve?meta_account_id=meta-phase6-a&login_id=login-phase6-a&reason=proxy-failure&traffic_class=messaging",{headers:{authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`}});if(!r.ok)throw new Error("resolver failed");const u=new URL((await r.json()).proxy_url);const result=await new Promise(resolve=>{let data="";const s=connect(Number(u.port),u.hostname,()=>s.write("GET http://direct-egress-sentinel:8083/failure HTTP/1.1\r\nHost: direct-egress-sentinel:8083\r\nConnection: close\r\n\r\n"));s.setTimeout(3000,()=>s.destroy());s.on("data",c=>data+=c);s.on("error",()=>resolve("transport-error"));s.on("close",()=>resolve(data))});if(!String(result).includes(" 502 ")&&result!=="transport-error")throw new Error("unavailable proxy unexpectedly succeeded");const d=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();if(d.hits!==0)throw new Error("proxy failure fell back direct");await fetch("http://proxy-a:8081/_test/up",{method:"POST"})'

# Deterministic Chatwoot timeout must be retryable with the same event ID.
"${DC[@]}" exec -T control-plane bun -e 'const r=await fetch("http://chatwoot-double:8080/_test/delay-next-message-create",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({ms:9000})});if(!r.ok)throw new Error("could not arm timeout")'
"${DC[@]}" exec -T control-plane bun -e 'const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true}),row=d.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");d.close();const ev={connectionId:row.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-timeout-a",senderId:"contact-phase6-a",text:"timeout A",occurredAt:"2026-09-11T14:02:00.000Z",provenance:"meta"};const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`,"content-type":"application/json"},body:JSON.stringify(ev)});if(r.ok)throw new Error("timeout unexpectedly succeeded")'
sleep 2
"${DC[@]}" exec -T control-plane bun -e 'const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true}),row=d.query("select id from meta_connections where meta_account_id=?").get("meta-phase6-a");d.close();const ev={connectionId:row.id,roomId:"!phase6-a:matrix.example.com",remoteThreadId:"thread-phase6-a",remoteContactId:"contact-phase6-a",eventId:"$phase6-timeout-a",senderId:"contact-phase6-a",text:"timeout A",occurredAt:"2026-09-11T14:02:00.000Z",provenance:"meta"};const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`,"content-type":"application/json"},body:JSON.stringify(ev)}),body=await r.json();if(!r.ok||body.data?.status!=="delivered")throw new Error(`timeout retry failed ${r.status}`);const s=await (await fetch("http://chatwoot-double:8080/_test/state")).json();if(s.messages.filter(x=>x.sourceEventId==="$phase6-timeout-a").length!==1)throw new Error("timeout retry duplicated/lost")'

# Restart must preserve identities, assignments and exact canonical duplicates.
"${DC[@]}" restart control-plane >/dev/null
wait_healthy control-plane 90
"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";const {Database}=await import("bun:sqlite");const d=new Database("/data/control-plane.db",{readonly:true});
const conns=d.query("select id,meta_account_id,mautrix_login_id from meta_connections where meta_account_id in (?,?) order by meta_account_id").all("meta-phase6-a","meta-phase6-b");
const rows=d.query("select cb.id binding_id,cb.chatwoot_account_id,cb.chatwoot_inbox_id,conv.chatwoot_conversation_id from chatwoot_bindings cb join conversation_bindings conv on conv.tenant_id=cb.tenant_id and conv.chatwoot_account_id=cb.chatwoot_account_id and conv.chatwoot_inbox_id=cb.chatwoot_inbox_id where cb.chatwoot_inbox_id in (?,?) order by cb.chatwoot_inbox_id").all("10","20");d.close();if(conns.length!==2||rows.length!==2)throw new Error("restart lost routes");
const internal={authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`,"content-type":"application/json"};for(const [i,c] of conns.entries()){const suffix=i?"b":"a",ev={connectionId:c.id,roomId:`!phase6-${suffix}:matrix.example.com`,remoteThreadId:`thread-phase6-${suffix}`,remoteContactId:`contact-phase6-${suffix}`,eventId:`$phase6-${suffix}-1`,senderId:`contact-phase6-${suffix}`,text:`from meta ${suffix.toUpperCase()}`,occurredAt:i?"2026-09-11T14:00:01.000Z":"2026-09-11T14:00:00.000Z",provenance:"meta"};const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:internal,body:JSON.stringify(ev)}),b=await r.json();if(!r.ok||b.data?.status!=="duplicate")throw new Error(`Matrix replay ${suffix} not duplicate`);const q=new URLSearchParams({meta_account_id:c.meta_account_id,login_id:c.mautrix_login_id,reason:"restart",traffic_class:"messaging"}),er=await fetch(`http://127.0.0.1:3000/internal/v1/egress/resolve?${q}`,{headers:{authorization:`Bearer ${process.env.CONTROL_PLANE_INTERNAL_TOKEN}`}});if(!er.ok||new URL((await er.json()).proxy_url).hostname!==(suffix==="a"?"proxy-a":"proxy-b"))throw new Error(`egress changed ${suffix}`)}
const replay=async(row,secret,payload,expected=200)=>{const raw=JSON.stringify(payload),ts=String(Math.floor(Date.now()/1000)),sig="sha256="+createHmac("sha256",secret).update(`${ts}.${raw}`).digest("hex");const r=await fetch(`http://127.0.0.1:3000/webhooks/chatwoot/${row.binding_id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig},body:raw}),body=await r.json();if(r.status!==expected)throw new Error(`webhook replay status ${r.status}: ${JSON.stringify(body)}`);return body};
const pA={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent A",created_at:"2026-09-11T14:01:00.000Z",account:{id:1},inbox:{id:10},conversation:{id:Number(rows[0].chatwoot_conversation_id)},sender:{id:81,type:"user",name:"Agent A"},attachments:[]};
const pB={event:"message_created",id:6001,message_type:"outgoing",private:false,content:"agent B",created_at:"2026-09-11T14:01:01.000Z",account:{id:2},inbox:{id:20},conversation:{id:Number(rows[1].chatwoot_conversation_id)},sender:{id:82,type:"user",name:"Agent B"},attachments:[]};
if((await replay(rows[0],process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,pA)).data?.status!=="duplicate"||(await replay(rows[1],process.env.CHATWOOT_WEBHOOK_PHASE6_B_SECRET,pB)).data?.status!=="duplicate")throw new Error("canonical restart replay not duplicate");
const reused=await replay(rows[0],process.env.CHATWOOT_WEBHOOK_PHASE6_A_SECRET,{...pA,content:"mutated reuse"},409);if(!reused.error)throw new Error("mutated event-ID reuse did not fail closed");
const cw=await (await fetch("http://chatwoot-double:8080/_test/state")).json(),mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json(),direct=await (await fetch("http://direct-egress-sentinel:8083/_test/state")).json();if(cw.messages.length!==3||mx.events.length!==2||direct.hits!==0)throw new Error("restart changed side effects or direct-egress sentinel");
'

logs="$("${DC[@]}" logs --no-color control-plane chatwoot-double matrix-client-double proxy-a proxy-b direct-egress-sentinel 2>&1 || true)"
for secret in "$CONTROL_PLANE_ADMIN_TOKEN" "$CONTROL_PLANE_INTERNAL_TOKEN" "$CHATWOOT_CI_TOKEN" "$CHATWOOT_WEBHOOK_PHASE6_A_SECRET" "$CHATWOOT_WEBHOOK_PHASE6_B_SECRET" "$MATRIX_CLIENT_CI_TOKEN" "$EGRESS_PROXY_PHASE6_A_PASSWORD" "$EGRESS_PROXY_PHASE6_B_PASSWORD"; do
  printf '%s' "$logs" | grep -Fq "$secret" && { echo "Synthetic Phase 6 secret leaked into service logs" >&2; exit 1; }
done

echo "Phase 6 multi-tenant production proof passed"
