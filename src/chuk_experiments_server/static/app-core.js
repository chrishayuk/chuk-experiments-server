const $=(s,r=document)=>r.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtDt=v=>v?String(v).slice(0,16).replace("T"," "):"—";
const fmtCost=v=>(v===null||v===undefined)?"—":"$"+Number(v).toFixed(4);
/* The "<tr><td colspan=N class=empty>message</td></tr>" fallback used to be
   hand-written at every table's own .map(...).join("")||... call site. */
const emptyRow=(colspan,message)=>`<tr><td colspan="${colspan}" class="empty">${esc(message)}</td></tr>`;
const renderRows=(items,rowFn,colspan,message)=>items.map(rowFn).join("")||emptyRow(colspan,message);
function externalRefCell(a){
  const meta=a.meta||{};
  if(meta.git_repo&&meta.git_commit){
    const short=String(meta.git_commit).slice(0,10);
    const url=`https://github.com/${meta.git_repo}/commit/${meta.git_commit}`;
    return {chip:"git",chipLabel:"git",link:`<a href="${esc(url)}" target="_blank" rel="noopener" class="mono">${esc(meta.git_repo)}@${esc(short)}</a>`};
  }
  if(meta.hf_repo_id){
    const revision=meta.hf_revision||"main";
    const segment=meta.hf_repo_type==="dataset"?"datasets/":"";
    const url=`https://huggingface.co/${segment}${meta.hf_repo_id}/tree/${revision}`;
    return {chip:"hf",chipLabel:"HF Hub",link:`<a href="${esc(url)}" target="_blank" rel="noopener" class="mono">${esc(meta.hf_repo_id)}@${esc(revision)}</a>`};
  }
  // Fallback for callers with only a uri string and no meta (e.g. pins —
  // PinSummary doesn't carry the artifact's meta): parse the same git+/hf://
  // uri shape directly instead of needing the structured fields.
  const uri=String(a.uri||"");
  let m;
  if((m=uri.match(/^git\+https:\/\/github\.com\/([^/]+\/[^/@]+)@(.+)$/))){
    const short=m[2].slice(0,10);
    return {chip:"git",chipLabel:"git",link:`<a href="https://github.com/${m[1]}/commit/${m[2]}" target="_blank" rel="noopener" class="mono">${esc(m[1])}@${esc(short)}</a>`};
  }
  if((m=uri.match(/^hf:\/\/(model|dataset)\/([^@]+)@?(.*)$/))){
    const revision=m[3]||"main",segment=m[1]==="dataset"?"datasets/":"";
    return {chip:"hf",chipLabel:"HF Hub",link:`<a href="https://huggingface.co/${segment}${m[2]}/tree/${revision}" target="_blank" rel="noopener" class="mono">${esc(m[2])}@${esc(revision)}</a>`};
  }
  return null;
}
function verifyBadge(a){
  const title=esc([a.verify_detail,a.verified_at?`checked ${fmtDt(a.verified_at)}`:""].filter(Boolean).join(" — "));
  if(a.verify_status==="verified")return `<span class="st good" title="${title}">verified</span>`;
  if(a.verify_status==="missing")return `<span class="st bad" title="${title}">missing</span>`;
  if(a.verify_status==="unverifiable")return `<span class="st warn" title="${title}">unverifiable</span>`;
  return `<span class="st mut">not checked</span>`;
}

/* STATUS_CLASS is server-injected (see app.html's inline script, sourced
   from constants.STATUS_CSS_CLASS in web.py's app_shell) — no more
   hand-copied JS mirror to drift out of sync. */
const pill=s=>`<span class="st ${STATUS_CLASS[s]||"mut"}">${esc(s)}</span>`;

/* Renders an arbitrary design/config/workspec object as labeled
   sections (dt/dd per top-level key, bulleted arrays, nested objects
   indented) instead of one opaque JSON blob — the whole point is that
   headline fields (a gate, a deliverables list) should be scannable, not
   buried in brackets and quotes. */
function humanizeKey(k){return String(k).replace(/_/g," ");}
function renderValue(v){
  if(v==null||v==="")return `<span class="muted">—</span>`;
  if(Array.isArray(v)){
    if(!v.length)return `<span class="muted">—</span>`;
    return `<ul>${v.map(item=>`<li>${typeof item==="object"&&item!==null?esc(JSON.stringify(item)):esc(item)}</li>`).join("")}</ul>`;
  }
  if(typeof v==="object")return renderKV(v);
  return esc(String(v));
}
function renderKV(obj){
  const entries=Object.entries(obj||{});
  if(!entries.length)return `<span class="muted">—</span>`;
  return `<dl class="kv">${entries.map(([k,v])=>`<dt>${esc(humanizeKey(k))}</dt><dd>${renderValue(v)}</dd>`).join("")}</dl>`;
}

async function api(path,options){
  const r=await fetch(path,options&&options.body
    ?{...options,headers:{"Content-Type":"application/json",...options.headers}}
    :options);
  let body=null;
  try{body=await r.json();}catch(e){/* non-JSON error body */}
  if(!r.ok)throw new Error((body&&body.error)||("http_"+r.status));
  return body;
}

/* Fixed page size, real offset-based paging (not a growing "load more") —
   each page issues its own bounded query so page 20 costs the same as
   page 1. Fetches PAGE_SIZE+1 rows to detect a next page without a
   separate count query. */
const PAGE_SIZE=25;
function pagerHtml(hashPath,baseParams,offset,count,hasMore){
  const prevParams=new URLSearchParams(baseParams);
  prevParams.set("offset",String(Math.max(0,offset-PAGE_SIZE)));
  const nextParams=new URLSearchParams(baseParams);
  nextParams.set("offset",String(offset+PAGE_SIZE));
  const from=count?offset+1:0;
  const to=offset+count;
  return `<div class="pager">
    ${offset>0?`<a href="#${hashPath}?${prevParams}">&larr; Prev</a>`:`<span class="muted">&larr; Prev</span>`}
    <span class="muted">${from}&ndash;${to}</span>
    ${hasMore?`<a href="#${hashPath}?${nextParams}">Next &rarr;</a>`:`<span class="muted">Next &rarr;</span>`}
  </div>`;
}
