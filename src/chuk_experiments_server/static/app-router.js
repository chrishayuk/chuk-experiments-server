function route(){
  const hash=(location.hash||"#/").slice(1);
  const [path,queryString]=hash.split("?");
  const params=new URLSearchParams(queryString||"");
  let m;
  if((m=path.match(/^\/experiments\/(.+)$/)))loadExperimentDetail(decodeURIComponent(m[1]));
  else if(path==="/experiments")loadExperimentsList(params);
  else if((m=path.match(/^\/runs\/(.+)$/)))loadRunDetail(decodeURIComponent(m[1]));
  else if(path==="/search")loadSearch(params);
  else if(path==="/pins")loadPins();
  else if(path==="/external-refs")loadExternalRefs(params);
  else if(path==="/team")loadTeam();
  else loadOverview();
}
window.addEventListener("hashchange",route);
route();
