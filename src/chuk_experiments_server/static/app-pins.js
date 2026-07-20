async function loadPins(){
  let pins;
  try{pins=await api("/v1/pins");}
  catch(e){$("#app").innerHTML=`<p class="err">Failed to load pins: ${esc(e.message)}</p>`;return;}

  const rows=renderRows(pins,p=>{
    const ref=externalRefCell({uri:p.uri});
    return `<tr>
      <td class="mono">${esc(p.name)}</td>
      <td>${esc(p.artifact_name||"—")}</td>
      <td><span class="tag">${esc(p.kind)}</span></td>
      <td><a href="#/runs/${encodeURIComponent(p.run_id)}" class="mono">${esc(p.run_id)}</a></td>
      <td class="muted mono">${ref?ref.link:esc(p.uri)}</td>
      <td class="muted">${fmtDt(p.updated_at)}</td>
    </tr>`;
  },6,"No pins yet");

  $("#app").innerHTML=`
    <h2>Pins</h2>
    <p class="muted">Named, repointable aliases onto a specific artifact (W&amp;B-style
      "latest"/"best") — set via the set_pin MCP tool or <span class="mono">PUT /v1/pins/{name}</span>.</p>
    <table><tr><th>Pin</th><th>Artifact</th><th>Kind</th><th>Run</th><th>URI</th><th>Updated</th></tr>${rows}</table>
  `;
}
