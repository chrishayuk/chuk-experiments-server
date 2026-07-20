async function loadSearch(params){
  const q=params.get("q")||"";
  const offset=Math.max(0,parseInt(params.get("offset")||"0",10)||0);
  let hits=[],hasMore=false;
  if(q){
    try{hits=await api("/v1/search?"+new URLSearchParams({q,limit:String(PAGE_SIZE+1),offset:String(offset)}));}
    catch(e){$("#app").innerHTML=`<p class="err">${esc(e.message)}</p>`;return;}
    hasMore=hits.length>PAGE_SIZE;
    hits=hits.slice(0,PAGE_SIZE);
  }
  const rows=hits.map(h=>`<tr><td><a href="#/experiments/${encodeURIComponent(h.slug)}">${esc(h.title)}</a></td><td>${esc(h.programme_slug)}</td><td>${pill(h.status)}</td><td class="muted">${h.snippet}</td></tr>`).join("")
    ||(q?emptyRow(4,`No matches for "${esc(q)}"`):"");

  const baseParams=new URLSearchParams();
  if(q)baseParams.set("q",q);

  $("#app").innerHTML=`
    <h2>Search</h2>
    <form class="filters" id="search-form">
      <input type="text" name="q" placeholder="free-text query" value="${esc(q)}" autofocus>
      <button type="submit">Search</button>
    </form>
    ${q?`<table><tr><th>Experiment</th><th>Programme</th><th>Status</th><th>Snippet</th></tr>${rows}</table>${pagerHtml("/search",baseParams,offset,hits.length,hasMore)}`:`<p class="muted">Full-text search across experiment titles, hypotheses, and write-ups.</p>`}
  `;
  $("#search-form").addEventListener("submit",ev=>{
    ev.preventDefault();
    const v=new FormData(ev.target).get("q");
    location.hash="#/search"+(v?"?q="+encodeURIComponent(v):"");
  });
}
