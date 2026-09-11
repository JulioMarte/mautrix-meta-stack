#!/usr/bin/env bash
set -euo pipefail

DC=(docker compose -f compose.yaml -f compose.control-plane.yaml -f compose.phase4.yaml)

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
"${DC[@]}" up -d chatwoot-double matrix-media-double control-plane
wait_healthy chatwoot-double 60
wait_healthy matrix-media-double 60
wait_healthy control-plane 90

"${DC[@]}" exec -T control-plane bun -e '
const base="http://127.0.0.1:3000";
const admin={authorization:"Bearer "+process.env.CONTROL_PLANE_ADMIN_TOKEN,"content-type":"application/json"};
const internal={authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"};
const post=async(path,body,headers=admin)=>{const r=await fetch(base+path,{method:"POST",headers,body:JSON.stringify(body)});const text=await r.text();if(!r.ok){console.error(path,r.status,text);process.exit(1)}return text?JSON.parse(text):{}};
const route=async(suffix,account,inbox)=>{
  const t=(await post("/api/v1/tenants",{slug:"phase4-"+suffix,name:"Phase 4 "+suffix})).data;
  const p=(await post("/api/v1/egress-profiles",{provider:"fixture",scheme:"http",host:"proxy-"+suffix+".test",port:8080,status:"healthy"})).data;
  const c=(await post("/api/v1/meta-connections",{tenantId:t.id,matrixOwnerMxid:"@phase4-"+suffix+":matrix.example.com",metaAccountId:"meta-phase4-"+suffix})).data;
  const w=(await post("/api/v1/chatwoot-bindings",{tenantId:t.id,chatwootAccountId:String(account),chatwootInboxId:String(inbox),apiBaseUrl:"http://chatwoot-double:8080",credentialRef:"env:CHATWOOT_CI_TOKEN",status:"active"})).data;
  await post(`/api/v1/meta-connections/${c.id}/egress`,{egressProfileId:p.id});
  await post(`/api/v1/meta-connections/${c.id}/chatwoot`,{chatwootBindingId:w.id});
  await post(`/api/v1/meta-connections/${c.id}/activate`,{});
  return {t,c,w};
};
const a=await route("a",1,10); const b=await route("b",1,20);
const eventA={connectionId:a.c.id,roomId:"!room-a:matrix.example.com",remoteThreadId:"thread-a",remoteContactId:"remote-contact",eventId:"$media-a",senderId:"remote-contact",senderDisplayName:"Alice",text:"attachments",occurredAt:"2026-09-11T11:00:00.000Z",provenance:"meta",attachments:[
 {kind:"image",url:"mxc://matrix.example.com/photo",mimeType:"image/jpeg",fileName:"photo.jpg"},
 {kind:"audio",url:"mxc://matrix.example.com/voice",mimeType:"audio/ogg",fileName:"voice.ogg"},
 {kind:"file",url:"mxc://matrix.example.com/pdf",mimeType:"application/pdf",fileName:"invoice.pdf"}
]};
const first=await post("/internal/v1/matrix/events",eventA,internal);
if(first.data?.status!=="delivered") { console.error(first); process.exit(1); }
const duplicate=await post("/internal/v1/matrix/events",eventA,internal);
if(duplicate.data?.status!=="duplicate") { console.error(duplicate); process.exit(1); }
const eventB={connectionId:b.c.id,roomId:"!room-b:matrix.example.com",remoteThreadId:"thread-b",remoteContactId:"remote-contact",eventId:"$text-b",senderId:"remote-contact",text:"tenant b",occurredAt:"2026-09-11T11:00:01.000Z",provenance:"meta"};
const second=await post("/internal/v1/matrix/events",eventB,internal);
if(second.data?.status!=="delivered") { console.error(second); process.exit(1); }
console.log(JSON.stringify({a,b}));
' > /tmp/phase4-routes.json

"${DC[@]}" exec -T control-plane bun -e '
const state=await (await fetch("http://chatwoot-double:8080/_test/state")).json();
if(state.messages.length!==2) throw new Error("expected exactly two Chatwoot messages after duplicate replay");
const a=state.messages.find((m)=>m.inboxId===10); const b=state.messages.find((m)=>m.inboxId===20);
if(!a||!b) throw new Error("tenant inbox routing mismatch");
if(a.attachments.length!==3) throw new Error("expected image, voice and PDF attachments");
const got=a.attachments.map((x)=>[x.name,x.type,x.content]);
const expected=[["photo.jpg","image/jpeg","fixture-photo-jpeg"],["voice.ogg","audio/ogg","fixture-voice-ogg"],["invoice.pdf","application/pdf","fixture-pdf-document"]];
if(JSON.stringify(got)!==JSON.stringify(expected)) { console.error(got); throw new Error("attachment payload mismatch"); }
if(b.attachments.length!==0 || b.content!=="tenant b") throw new Error("tenant B message mismatch");
'

# Prove ambiguous post-commit Chatwoot errors are reconciled rather than duplicated.
"${DC[@]}" exec -T control-plane bun -e 'await fetch("http://chatwoot-double:8080/_test/fail-after-next-message-create",{method:"POST"})'
"${DC[@]}" exec -T control-plane bun -e '
const db=(await import("bun:sqlite")).Database; const d=new db("/data/control-plane.db",{readonly:true});
const row=d.query("select id from meta_connections where meta_account_id = ?").get("meta-phase4-a"); d.close();
const event={connectionId:row.id,roomId:"!room-a:matrix.example.com",remoteThreadId:"thread-a",remoteContactId:"remote-contact",eventId:"$ambiguous-a",senderId:"remote-contact",text:"ambiguous",occurredAt:"2026-09-11T11:00:02.000Z",provenance:"meta"};
const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"},body:JSON.stringify(event)});
if(!r.ok){console.error(r.status,await r.text());process.exit(1)} const body=await r.json(); if(body.data?.status!=="delivered") {console.error(body);process.exit(1)}
'
"${DC[@]}" exec -T control-plane bun -e 'const s=await (await fetch("http://chatwoot-double:8080/_test/state")).json();if(s.messages.filter((m)=>m.sourceEventId==="$ambiguous-a").length!==1)process.exit(1)'

# Persistence/restart: processed event and conversation binding must prevent a second side effect.
"${DC[@]}" restart control-plane
wait_healthy control-plane 90
"${DC[@]}" exec -T control-plane bun -e '
const {Database}=await import("bun:sqlite"); const d=new Database("/data/control-plane.db",{readonly:true}); const row=d.query("select id from meta_connections where meta_account_id = ?").get("meta-phase4-a"); d.close();
const event={connectionId:row.id,roomId:"!room-a:matrix.example.com",remoteThreadId:"thread-a",remoteContactId:"remote-contact",eventId:"$media-a",senderId:"remote-contact",senderDisplayName:"Alice",text:"attachments",occurredAt:"2026-09-11T11:00:00.000Z",provenance:"meta",attachments:[{kind:"image",url:"mxc://matrix.example.com/photo",mimeType:"image/jpeg",fileName:"photo.jpg"},{kind:"audio",url:"mxc://matrix.example.com/voice",mimeType:"audio/ogg",fileName:"voice.ogg"},{kind:"file",url:"mxc://matrix.example.com/pdf",mimeType:"application/pdf",fileName:"invoice.pdf"}]};
const r=await fetch("http://127.0.0.1:3000/internal/v1/matrix/events",{method:"POST",headers:{authorization:"Bearer "+process.env.CONTROL_PLANE_INTERNAL_TOKEN,"content-type":"application/json"},body:JSON.stringify(event)});const body=await r.json();if(body.data?.status!=="duplicate"){console.error(body);process.exit(1)}
const state=await (await fetch("http://chatwoot-double:8080/_test/state")).json();if(state.messages.filter((m)=>m.sourceEventId==="$media-a").length!==1)process.exit(1);
'

logs="$("${DC[@]}" logs --no-color control-plane chatwoot-double matrix-media-double 2>&1 || true)"
for secret in "$CHATWOOT_CI_TOKEN" "$MATRIX_MEDIA_CI_TOKEN" "$CONTROL_PLANE_INTERNAL_TOKEN"; do
  if printf '%s' "$logs" | grep -Fq "$secret"; then
    echo "Synthetic secret leaked into Phase 4 logs" >&2
    exit 1
  fi
done

echo "Phase 4 Matrix -> Chatwoot attachment topology passed"
