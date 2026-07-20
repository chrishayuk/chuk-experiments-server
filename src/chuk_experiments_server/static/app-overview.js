async function loadOverview(){
  let programmes,recent,running,health;
  try{
    [programmes,recent,running,health]=await Promise.all([
      api("/v1/programmes"),
      api("/v1/experiments?limit=10"),
      api("/v1/experiments?"+new URLSearchParams({status:"running",limit:"10"})),
      api("/v1/experiments/health"),
    ]);
  }catch(e){$("#app").innerHTML=`<p class="err">Failed to load overview: ${esc(e.message)}</p>`;return;}

  const totalExperiments=programmes.reduce((sum,p)=>sum+(p.experiment_count||0),0);
  const tiles=[["Programmes",programmes.length],["Total experiments",totalExperiments],
    ["Currently running",running.length]];
  const tilesH=tiles.map(([l,v])=>`<div class="tile"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("")
    +`<a class="tile" href="#/experiments?needs_conclusion=true"><div class="v">${esc(health.needs_conclusion)}</div><div class="l">Needs conclusion</div></a>`
    +`<a class="tile" href="#/experiments?needs_next_action=true"><div class="v">${esc(health.needs_next_action)}</div><div class="l">Needs next action</div></a>`;

  const progRows=renderRows(programmes,p=>`<tr class="click" onclick="location.hash='#/experiments?programme='+encodeURIComponent('${esc(p.slug)}')"><td><a href="#/experiments?programme=${encodeURIComponent(p.slug)}">${esc(p.name)}</a></td><td class="num">${esc(p.experiment_count)}</td></tr>`,2,"No programmes yet");
  const recentRows=renderRows(recent,experimentRow,4,"Nothing yet");
  const runningRows=running.map(experimentRow).join("");

  $("#app").innerHTML=`
    <section><p class="eyebrow">Health</p><div class="tiles">${tilesH}</div></section>
    <section><div class="card"><div class="hd"><h3>Programmes</h3></div>
      <table><tr><th>Programme</th><th>Experiments</th></tr>${progRows}</table></div></section>
    <section><div class="card"><div class="hd"><h3>Recently updated</h3></div>
      <table><tr><th>Experiment</th><th>Programme</th><th>Status</th><th>Updated</th></tr>${recentRows}</table></div></section>
    ${running.length?`<section><div class="card"><div class="hd"><h3>Currently running</h3></div>
      <table><tr><th>Experiment</th><th>Programme</th><th>Status</th><th>Updated</th></tr>${runningRows}</table></div></section>`:""}
  `;
}

function experimentRow(e){
  return `<tr><td><a href="#/experiments/${encodeURIComponent(e.slug)}">${esc(e.title)}</a></td><td>${esc(e.programme_slug)}</td><td>${pill(e.status)}</td><td class="muted">${fmtDt(e.updated_at)}</td></tr>`;
}
