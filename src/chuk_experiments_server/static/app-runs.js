async function loadRunDetail(id){
  let run;
  try{run=await api("/v1/runs/"+encodeURIComponent(id));}
  catch(e){$("#app").innerHTML=`<h1>Not found</h1><p class="err">${esc(e.message)}</p>`;return;}

  const resultsRows=renderRows(run.results||[],r=>`<tr><td>${esc(r.name)}</td><td>${r.value!=null?esc(r.value):(r.value_json?esc(JSON.stringify(r.value_json)):"—")}</td><td>${esc(r.verdict||"—")}</td><td class="muted">${esc(r.notes||"—")}</td><td class="muted">${esc(r.submitted_by)}</td></tr>`,5,"No results yet");

  /* Lineage is only meaningful for named (dedup-eligible) artifacts — skip
     the extra request for the common case of a one-off unnamed pointer.
     Pins are fetched in full alongside it — cheap today, and cross-
     referencing client-side avoids a per-artifact "is this pinned" route. */
  const artifacts=run.artifacts||[];
  const lineageByArtifactId={};
  let pins=[];
  await Promise.all([
    ...artifacts.filter(a=>a.name).map(async a=>{
      try{lineageByArtifactId[a.id]=await api(`/v1/artifacts/${a.id}/lineage`);}catch(e){/* best-effort */}
    }),
    (async()=>{try{pins=await api("/v1/pins");}catch(e){/* best-effort */}})(),
  ]);
  const pinNamesByArtifactId={};
  pins.forEach(p=>{(pinNamesByArtifactId[p.artifact_id]=pinNamesByArtifactId[p.artifact_id]||[]).push(p.name);});

  const artifactsRows=renderRows(artifacts,a=>{
    const lineage=lineageByArtifactId[a.id];
    const usedByCount=lineage?lineage.used_by_run_ids.length:0;
    const pinBadges=(pinNamesByArtifactId[a.id]||[]).map(n=>`<span class="tag">pinned: ${esc(n)}</span>`).join(" ");
    const nameCell=a.name?`${esc(a.name)}${usedByCount?`<br><span class="muted">used by ${usedByCount} other run${usedByCount!==1?"s":""}</span>`:""}${pinBadges?`<br>${pinBadges}`:""}`:"—";
    const meta=a.meta||{};
    const ref=externalRefCell(a);
    if(ref){
      const path=`${ref.link}<br>${verifyBadge(a)}`;
      return `<tr><td>${esc(a.kind)}</td><td>${nameCell}</td><td><span class="chip ${ref.chip}">${esc(ref.chipLabel)}</span></td><td class="muted">${path}</td><td class="muted">via verify_artifact</td></tr>`;
    }
    if(String(a.uri||"").startsWith("gdrive://")){
      const path=meta.source_path||a.uri;
      return `<tr><td>${esc(a.kind)}</td><td>${nameCell}</td><td><span class="chip drive">Drive · archive</span></td><td class="muted mono">${esc(path)}</td><td><a href="/v1/artifacts/${a.id}/download" target="_blank" rel="noopener">open in Drive</a></td></tr>`;
    }
    return `<tr><td>${esc(a.kind)}</td><td>${nameCell}</td><td><span class="chip r2">R2</span></td><td class="muted mono">${esc(a.uri)}</td><td><a href="/v1/artifacts/${a.id}/download">download</a></td></tr>`;
  },5,"No artifacts yet");

  const hasConfig=run.config&&Object.keys(run.config).length;
  const hasWorkspec=run.workspec&&Object.keys(run.workspec).length;

  $("#app").innerHTML=`
    <p class="muted"><a href="#/experiments/${encodeURIComponent(run.experiment_slug)}">${esc(run.experiment_title)}</a> / <span class="mono">${esc(run.slug)}</span></p>
    <h2 class="mono">${esc(run.slug)}</h2>
    <p>${pill(run.status)}</p>
    <div class="card"><table>
      <tr><td class="muted">Backend</td><td>${esc(run.backend||"—")}</td></tr>
      <tr><td class="muted">Priority</td><td>${esc(run.priority)}</td></tr>
      <tr><td class="muted">Cost</td><td>${fmtCost(run.cost_usd)}</td></tr>
      <tr><td class="muted">W&amp;B</td><td>${run.wandb_url?`<a href="${esc(run.wandb_url)}" target="_blank" rel="noopener">${esc(run.wandb_url)}</a>`:"—"}</td></tr>
      <tr><td class="muted">Started</td><td>${fmtDt(run.started_at)}</td></tr>
      <tr><td class="muted">Ended</td><td>${fmtDt(run.ended_at)}</td></tr>
      ${run.claimed_by?`<tr><td class="muted">Claimed by</td><td>${esc(run.claimed_by)}</td></tr>`:""}
    </table></div>
    <h3>Results</h3>
    <table><tr><th>Name</th><th>Value</th><th>Verdict</th><th>Notes</th><th>By</th></tr>${resultsRows}</table>
    <h3>Artifacts</h3>
    <table><tr><th>Kind</th><th>Name</th><th>Location</th><th>Path</th><th></th></tr>${artifactsRows}</table>
    ${hasConfig?`<div class="card"><strong>Config</strong>${renderKV(run.config)}</div>`:""}
    ${hasWorkspec?`<div class="card"><strong>Workspec</strong>${renderKV(run.workspec)}</div>`:""}
  `;
}
