async function loadExternalRefs(params){
  const offset=Math.max(0,parseInt(params.get("offset")||"0",10)||0);
  let refs;
  try{refs=await api(`/v1/artifacts/external-refs?limit=${PAGE_SIZE+1}&offset=${offset}`);}
  catch(e){$("#app").innerHTML=`<p class="err">Failed to load external refs: ${esc(e.message)}</p>`;return;}

  const hasMore=refs.length>PAGE_SIZE;
  refs=refs.slice(0,PAGE_SIZE);

  /* Counts are only over the current page, not the whole table — a
     pagination-safe aggregate would need its own query; noted as a known
     limitation rather than silently implying a global count. */
  const counts={verified:0,missing:0,unverifiable:0,unchecked:0};
  refs.forEach(r=>{counts[r.verify_status||"unchecked"]=(counts[r.verify_status||"unchecked"]||0)+1;});

  const rows=renderRows(refs,r=>{
    const ref=externalRefCell(r)||{chip:"r2",chipLabel:esc(r.kind),link:esc(r.uri)};
    return `<tr>
      <td><a href="#/experiments/${encodeURIComponent(r.experiment_slug)}">${esc(r.experiment_title)}</a></td>
      <td><a href="#/runs/${encodeURIComponent(r.run_id)}" class="mono">${esc(r.run_id)}</a></td>
      <td><span class="tag">${esc(r.kind)}</span></td>
      <td><span class="chip ${ref.chip}">${ref.chipLabel}</span> ${ref.link}${r.name?`<br><span class="muted">${esc(r.name)}</span>`:""}</td>
      <td>${verifyBadge(r)}</td>
      <td class="muted">${fmtDt(r.created_at)}</td>
    </tr>`;
  },6,"No git+/hf:// references registered yet");

  $("#app").innerHTML=`
    <h2>External refs</h2>
    <p class="muted">Every artifact across all experiments that references a git commit or a
      Hugging Face Hub model/dataset instead of uploaded bytes — set via
      <span class="mono">register_git_artifact</span>/<span class="mono">register_hf_artifact</span>.
      This page (${refs.length}): <span class="st good">${counts.verified} verified</span>,
      <span class="st bad">${counts.missing} missing</span>,
      <span class="st warn">${counts.unverifiable} unverifiable</span>,
      <span class="st mut">${counts.unchecked} never checked</span>.</p>
    <table><tr><th>Experiment</th><th>Run</th><th>Kind</th><th>Reference</th><th>Verify</th><th>Registered</th></tr>${rows}</table>
    ${pagerHtml("/external-refs",new URLSearchParams(),offset,refs.length,hasMore)}
  `;
}
