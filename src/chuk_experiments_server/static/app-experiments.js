/* EXPERIMENT_STATUSES is server-injected (see app.html's inline script,
   sourced from constants.ExperimentStatus in web.py's app_shell). */

/* Mirrors constants.DEFAULT_EXPERIMENT_SORT/DEFAULT_EXPERIMENT_ORDER — kept
   in sync by hand. Clicking an inactive column sorts it ascending; clicking
   the already-active column flips direction. */
function sortHeader(basePath,params,column,label){
  const currentSort=params.get("sort")||"updated_at";
  const currentOrder=params.get("order")||"desc";
  const isActive=currentSort===column;
  const nextOrder=isActive&&currentOrder==="asc"?"desc":"asc";
  const p=new URLSearchParams(params);
  p.set("sort",column);p.set("order",nextOrder);p.delete("offset");
  const arrow=isActive?(currentOrder==="asc"?" ▲":" ▼"):"";
  return `<a href="#${basePath}?${p}">${esc(label)}${arrow}</a>`;
}

async function loadExperimentsList(params){
  const programme=params.get("programme")||"";
  const status=params.get("status")||"";
  const tag=params.get("tag")||"";
  const needsConclusion=params.get("needs_conclusion")==="true";
  const needsNextAction=params.get("needs_next_action")==="true";
  const sort=params.get("sort")||"updated_at";
  const order=params.get("order")||"desc";
  const offset=Math.max(0,parseInt(params.get("offset")||"0",10)||0);

  let experiments,programmes;
  try{
    const apiParams=new URLSearchParams({limit:String(PAGE_SIZE+1),offset:String(offset),sort,order});
    if(programme)apiParams.set("programme",programme);
    if(status)apiParams.set("status",status);
    if(tag)apiParams.set("tag",tag);
    if(needsConclusion)apiParams.set("needs_conclusion","true");
    if(needsNextAction)apiParams.set("needs_next_action","true");
    [experiments,programmes]=await Promise.all([api("/v1/experiments?"+apiParams),api("/v1/programmes")]);
  }catch(e){$("#app").innerHTML=`<p class="err">Failed to load experiments: ${esc(e.message)}</p>`;return;}

  const hasMore=experiments.length>PAGE_SIZE;
  experiments=experiments.slice(0,PAGE_SIZE);

  const rows=renderRows(experiments,e=>`<tr><td><a href="#/experiments/${encodeURIComponent(e.slug)}">${esc(e.title)}</a></td><td>${esc(e.programme_slug)}</td><td>${pill(e.status)}</td><td>${(e.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</td><td class="muted">${fmtDt(e.updated_at)}</td></tr>`,5,"No experiments match these filters");

  /* Everything except offset — shared by sort headers and the pager
     (pagerHtml adds its own offset on top). */
  const sortParams=new URLSearchParams();
  if(programme)sortParams.set("programme",programme);
  if(status)sortParams.set("status",status);
  if(tag)sortParams.set("tag",tag);
  if(needsConclusion)sortParams.set("needs_conclusion","true");
  if(needsNextAction)sortParams.set("needs_next_action","true");
  sortParams.set("sort",sort);sortParams.set("order",order);

  const statusChips=[["","all"]].concat(EXPERIMENT_STATUSES.map(s=>[s,s])).map(([value,label])=>{
    const p=new URLSearchParams(sortParams);
    p.delete("status");p.delete("offset");
    if(value)p.set("status",value);
    return `<a class="chip-filter${value===status?" active":""}" href="#/experiments?${p}">${esc(label)}</a>`;
  }).join("");

  const progOptions=programmes.map(p=>`<option value="${esc(p.slug)}" ${p.slug===programme?"selected":""}>${esc(p.name)}</option>`).join("");

  $("#app").innerHTML=`
    <h2>Experiments</h2>
    ${needsConclusion?`<p class="muted">Showing completed experiments with no recorded conclusion.</p>`:""}
    ${needsNextAction?`<p class="muted">Showing planned/running experiments with no recorded next action.</p>`:""}
    <div class="filters">
      <div class="chip-row" id="status-chips">${statusChips}</div>
      <select id="programme-select"><option value="">All programmes</option>${progOptions}</select>
      <form id="tag-form">
        <input type="text" name="tag" placeholder="tag" value="${esc(tag)}">
        <button type="submit">Go</button>
      </form>
      ${(programme||status||tag||needsConclusion||needsNextAction)?`<a href="#/experiments">Clear</a>`:""}
    </div>
    <table><tr>
      <th>${sortHeader("/experiments",sortParams,"title","Experiment")}</th>
      <th>${sortHeader("/experiments",sortParams,"programme_slug","Programme")}</th>
      <th>${sortHeader("/experiments",sortParams,"status","Status")}</th>
      <th>Tags</th>
      <th>${sortHeader("/experiments",sortParams,"updated_at","Updated")}</th>
    </tr>${rows}</table>
    ${pagerHtml("/experiments",sortParams,offset,experiments.length,hasMore)}
  `;
  $("#programme-select").addEventListener("change",ev=>{
    const p=new URLSearchParams(sortParams);
    p.delete("offset");
    if(ev.target.value)p.set("programme",ev.target.value);else p.delete("programme");
    location.hash="#/experiments?"+p;
  });
  $("#tag-form").addEventListener("submit",ev=>{
    ev.preventDefault();
    const v=new FormData(ev.target).get("tag");
    const p=new URLSearchParams(sortParams);
    p.delete("offset");
    if(v)p.set("tag",v);else p.delete("tag");
    location.hash="#/experiments?"+p;
  });
}

async function loadExperimentDetail(slug){
  let experiment;
  try{experiment=await api("/v1/experiments/"+encodeURIComponent(slug));}
  catch(e){$("#app").innerHTML=`<h1>Not found</h1><p class="err">${esc(e.message)}</p>`;return;}

  const writeup=experiment.latest_writeup;
  const hasDesign=experiment.design&&Object.keys(experiment.design).length;
  const runs=experiment.runs||[];
  const runsRows=renderRows(runs,r=>`<tr><td><a href="#/runs/${encodeURIComponent(r.id)}" class="mono">${esc(r.slug)}</a></td><td>${pill(r.status)}</td><td>${esc(r.backend||"—")}</td><td>${fmtCost(r.cost_usd)}</td><td>${r.wandb_url?`<a href="${esc(r.wandb_url)}" target="_blank" rel="noopener">dashboard</a>`:"—"}</td></tr>`,5,"No runs yet");

  /* The experiment list itself has no result columns, and finding out
     "what happened" otherwise means opening every run individually — pull
     each run's results in parallel and roll them into one table here. */
  const resultsByRunId={};
  await Promise.all(runs.map(async r=>{
    try{resultsByRunId[r.id]=(await api(`/v1/runs/${encodeURIComponent(r.id)}`)).results||[];}
    catch(e){/* best-effort */}
  }));
  const rollupRows=[];
  runs.forEach(r=>{
    (resultsByRunId[r.id]||[]).forEach(res=>{
      const value=res.value!=null?esc(res.value):(res.value_json?esc(JSON.stringify(res.value_json)):"—");
      rollupRows.push(`<tr><td><a href="#/runs/${encodeURIComponent(r.id)}" class="mono">${esc(r.slug)}</a></td><td>${esc(res.name)}</td><td>${value}</td><td>${esc(res.verdict||"—")}</td></tr>`);
    });
  });

  $("#app").innerHTML=`
    <p class="muted"><a href="#/experiments?programme=${encodeURIComponent(experiment.programme_slug)}">${esc(experiment.programme_name)}</a> / ${esc(experiment.slug)}</p>
    <h2>${esc(experiment.title)}</h2>
    <p>${pill(experiment.status)} ${(experiment.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}</p>
    ${experiment.hypothesis?`<div class="card"><strong>Hypothesis</strong><p>${esc(experiment.hypothesis)}</p></div>`:""}
    ${experiment.conclusion?`<div class="card"><strong>Conclusion</strong><p>${esc(experiment.conclusion)}</p></div>`:""}
    ${experiment.next_action?`<div class="card"><strong>Next action</strong><p>${esc(experiment.next_action)}</p></div>`:""}
    ${rollupRows.length?`<h3>Results</h3><table><tr><th>Run</th><th>Metric</th><th>Value</th><th>Verdict</th></tr>${rollupRows.join("")}</table>`:""}
    <h3>Write-up ${writeup?`<span class="muted">(v${writeup.version}, ${esc(writeup.author)})</span>`:""}</h3>
    ${writeup?`<div class="writeup">${writeup.body_html}</div>`:`<p class="empty">No write-up yet</p>`}
    ${hasDesign?`<div class="card"><strong>Design</strong>${renderKV(experiment.design)}</div>`:""}
    <h3>Runs</h3>
    <table><tr><th>Run</th><th>Status</th><th>Backend</th><th>Cost</th><th>W&amp;B</th></tr>${runsRows}</table>
  `;
}
