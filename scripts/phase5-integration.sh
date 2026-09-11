#!/usr/bin/env bash
set -euo pipefail

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.phase5.yaml)

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
"${DC[@]}" up -d chatwoot-double matrix-client-double control-plane
wait_healthy chatwoot-double 60
wait_healthy matrix-client-double 60
wait_healthy control-plane 90

"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";
const base="http://127.0.0.1:3000";
const admin={authorization:"Bearer "+process.env.CONTROL_PLANE_ADMIN_TOKEN,"content-type":"application/json"};
const internal={authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"};
const post=async(path,body,headers=admin)=>{const r=await fetch(base+path,{method:"POST",headers,body:JSON.stringify(body)});const text=await r.text();if(!r.ok){console.error(path,r.status,text);process.exit(1)}return text?JSON.parse(text):{}};
const tenant=(await post("/api/v1/tenants",{slug:"phase5-a",name:"Phase 5 A"})).data;
const egress=(await post("/api/v1/egress-profiles",{provider:"fixture",scheme:"http",host:"proxy-phase5.test",port:8080,status:"healthy"})).data;
const connection=(await post("/api/v1/meta-connections",{tenantId:tenant.id,matrixOwnerMxid:"@phase5:matrix.example.com",metaAccountId:"meta-phase5-a"})).data;
const binding=(await post("/api/v1/chatwoot-bindings",{tenantId:tenant.id,chatwootAccountId:"1",chatwootInboxId:"10",apiBaseUrl:"http://chatwoot-double:8080",credentialRef:"env:CHATWOOT_CI_TOKEN",status:"active"})).data;
await post(`/api/v1/meta-connections/${connection.id}/egress`,{egressProfileId:egress.id});
await post(`/api/v1/meta-connections/${connection.id}/chatwoot`,{chatwootBindingId:binding.id});
await post(`/api/v1/meta-connections/${connection.id}/activate`,{});
const hook=(await post(`/api/v1/chatwoot-bindings/${binding.id}/webhook`,{secretRef:"env:CHATWOOT_WEBHOOK_CI_SECRET"})).data;
if(hook.secretRef!=="[configured]"||hook.webhookPath!==`/webhooks/chatwoot/${binding.id}`) throw new Error("webhook configuration redaction/path mismatch");

const seed={connectionId:connection.id,roomId:"!phase5-room:matrix.example.com",remoteThreadId:"thread-phase5",remoteContactId:"remote-phase5",eventId:"$phase5-seed",senderId:"remote-phase5",text:"seed conversation",occurredAt:"2026-09-11T12:00:00.000Z",provenance:"meta"};
const seeded=await post("/internal/v1/matrix/events",seed,internal);
if(seeded.data?.status!=="delivered") throw new Error("failed to seed Phase 5 conversation binding through Phase 4");
const cw=await (await fetch("http://chatwoot-double:8080/_test/state")).json();
if(cw.conversations.length!==1||cw.messages.length!==1) throw new Error("unexpected Chatwoot seed state");
const conversationId=String(cw.conversations[0].id);

const sign=async(payload,expectStatus=200)=>{
  const raw=JSON.stringify(payload); const ts=String(Math.floor(Date.now()/1000));
  const sig="sha256="+createHmac("sha256",process.env.CHATWOOT_WEBHOOK_CI_SECRET).update(`${ts}.${raw}`).digest("hex");
  const r=await fetch(base+`/webhooks/chatwoot/${binding.id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig,"x-chatwoot-delivery":"delivery-"+payload.id},body:raw});
  const text=await r.text(); if(r.status!==expectStatus){console.error(r.status,text);process.exit(1)} return text?JSON.parse(text):{};
};
const outgoing={event:"message_created",id:900,message_type:"outgoing",private:false,content:"agent reply",created_at:"2026-09-11T12:05:00.000Z",account:{id:1},inbox:{id:10},conversation:{id:Number(conversationId)},sender:{id:8,type:"user",name:"Agent"},attachments:[
 {id:501,file_type:"file",data_url:"http://chatwoot-double:8080/files/agent.pdf",content_type:"application/pdf",extension:"pdf",file_size:16},
 {id:502,file_type:"audio",data_url:"http://chatwoot-double:8080/files/agent.ogg",content_type:"audio/ogg",extension:"ogg",file_size:18}
]};
const first=await sign(outgoing); if(first.data?.status!=="delivered") throw new Error("Phase 5 first delivery failed");
const duplicate=await sign(outgoing); if(duplicate.data?.status!=="duplicate") throw new Error("Phase 5 duplicate was not suppressed");

let mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();
if(mx.uploads.length!==2||mx.events.length!==3) throw new Error("expected text + two Matrix attachment events");
if(mx.uploads[0].content!=="phase5-agent-pdf"||mx.uploads[1].content!=="phase5-agent-voice") throw new Error("Matrix upload bytes mismatch");
for(const ev of mx.events){
  if(ev.roomId!=="!phase5-room:matrix.example.com") throw new Error("Matrix room routing mismatch");
  const p=ev.content?.["com.mautrix_meta_stack.provenance"];
  if(p?.source!=="chatwoot"||p?.source_event_id!==`${binding.id}:900`) throw new Error("Matrix provenance mismatch");
}

const badRaw=JSON.stringify({...outgoing,id:901});
const bad=await fetch(base+`/webhooks/chatwoot/${binding.id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":String(Math.floor(Date.now()/1000)),"x-chatwoot-signature":"sha256="+"0".repeat(64)},body:badRaw});
if(bad.status!==401) throw new Error("invalid webhook signature was accepted");
const ignored=await sign({...outgoing,id:902,private:true,attachments:[]});
if(ignored.data?.status!=="ignored") throw new Error("private Chatwoot message was not ignored");
const mismatch=await sign({...outgoing,id:903,inbox:{id:20},attachments:[]},409);
if(mismatch.error?.code!=="CHATWOOT_ROUTE_MISMATCH") throw new Error("cross-inbox route mismatch did not fail closed");
mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json(); if(mx.events.length!==3) throw new Error("rejected webhooks caused Matrix side effects");

await fetch("http://matrix-client-double:8082/_test/fail-after-next-send",{method:"POST"});
const ambiguous={...outgoing,id:904,content:"ambiguous matrix reply",attachments:[]};
const failed=await sign(ambiguous,503); if(failed.error?.code!=="MATRIX_SEND_HTTP_500") throw new Error("expected injected Matrix failure");
const retried=await sign(ambiguous); if(retried.data?.status!=="delivered") throw new Error("retry after ambiguous Matrix response failed");
mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();
const ambiguousEvents=mx.events.filter((ev)=>ev.content?.["com.mautrix_meta_stack.provenance"]?.source_event_id===`${binding.id}:904`);
if(ambiguousEvents.length!==1) throw new Error("Matrix txn id did not suppress ambiguous retry duplicate");

const echo=await post("/internal/v1/matrix/events",{...seed,eventId:"$phase5-echo",text:"agent reply",provenance:"chatwoot"},internal);
if(echo.data?.status!=="ignored_echo") throw new Error("Phase 4 did not suppress Phase 5 provenance echo");
const cwAfter=await (await fetch("http://chatwoot-double:8080/_test/state")).json(); if(cwAfter.messages.length!==1) throw new Error("echo loop created a Chatwoot side effect");
console.log(JSON.stringify({bindingId:binding.id,connectionId:connection.id,conversationId}));
' > /tmp/phase5-route.json

# Persistence/restart: a delivered Chatwoot webhook remains deduplicated after control-plane restart.
"${DC[@]}" restart control-plane
wait_healthy control-plane 90
"${DC[@]}" exec -T control-plane bun -e '
import {createHmac} from "node:crypto";
const {Database}=await import("bun:sqlite"); const d=new Database("/data/control-plane.db",{readonly:true});
const binding=d.query("select cb.id, conv.chatwoot_conversation_id from chatwoot_bindings cb join conversation_bindings conv on conv.chatwoot_inbox_id=cb.chatwoot_inbox_id and conv.chatwoot_account_id=cb.chatwoot_account_id where cb.chatwoot_inbox_id = ?").get("10"); d.close();
const payload={event:"message_created",id:900,message_type:"outgoing",private:false,content:"agent reply",created_at:"2026-09-11T12:05:00.000Z",account:{id:1},inbox:{id:10},conversation:{id:Number(binding.chatwoot_conversation_id)},sender:{id:8,type:"user",name:"Agent"},attachments:[{id:501,file_type:"file",data_url:"http://chatwoot-double:8080/files/agent.pdf",content_type:"application/pdf",extension:"pdf",file_size:16},{id:502,file_type:"audio",data_url:"http://chatwoot-double:8080/files/agent.ogg",content_type:"audio/ogg",extension:"ogg",file_size:18}]};
const raw=JSON.stringify(payload),ts=String(Math.floor(Date.now()/1000)),sig="sha256="+createHmac("sha256",process.env.CHATWOOT_WEBHOOK_CI_SECRET).update(`${ts}.${raw}`).digest("hex");
const r=await fetch(`http://127.0.0.1:3000/webhooks/chatwoot/${binding.id}`,{method:"POST",headers:{"content-type":"application/json","x-chatwoot-timestamp":ts,"x-chatwoot-signature":sig},body:raw});const body=await r.json();if(!r.ok||body.data?.status!=="duplicate"){console.error(r.status,body);process.exit(1)}
const mx=await (await fetch("http://matrix-client-double:8082/_test/state")).json();if(mx.events.length!==4) throw new Error("restart duplicate caused a second Matrix side effect");
'

logs="$("${DC[@]}" logs --no-color control-plane chatwoot-double matrix-client-double 2>&1 || true)"
for secret in "$CHATWOOT_CI_TOKEN" "$CHATWOOT_WEBHOOK_CI_SECRET" "$MATRIX_CLIENT_CI_TOKEN" "$CONTROL_PLANE_ADMIN_TOKEN" "$CONTROL_PLANE_INTERNAL_TOKEN"; do
  if printf '%s' "$logs" | grep -Fq "$secret"; then
    echo "Synthetic Phase 5 secret leaked into logs" >&2
    exit 1
  fi
done

echo "Phase 5 Chatwoot -> Matrix topology passed"
