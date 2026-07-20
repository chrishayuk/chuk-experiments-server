/* ROLE_SCOPE_CEILING is server-injected (see app.html's inline script,
   sourced from constants.ROLE_SCOPE_CEILING in web.py's app_shell): which
   scope checkboxes a given role is even allowed to see (the server enforces
   the same ceiling regardless — this is just so the form doesn't offer a
   choice it'll reject). */

async function loadTeam(){
  let me,keys;
  try{[me,keys]=await Promise.all([api("/v1/me"),api("/v1/keys")]);}
  catch(e){$("#app").innerHTML=`<p class="err">Failed to load team: ${esc(e.message)}</p>`;return;}

  const mcpUrl=location.origin+"/mcp";

  const isAdmin=me.role==="admin";
  let users=[];
  if(isAdmin){
    try{users=await api("/v1/users");}
    catch(e){$("#app").innerHTML=`<p class="err">Failed to load users: ${esc(e.message)}</p>`;return;}
  }

  const scopeChoices=(ROLE_SCOPE_CEILING[me.role]||[]).map(s=>
    `<label><input type="checkbox" name="scope" value="${s}"> ${esc(s)}</label>`).join(" ");

  const keyRows=renderRows(keys,k=>`<tr>
      <td>${esc(k.name)}</td>
      <td>${k.scopes.map(s=>`<span class="tag">${esc(s)}</span>`).join("")}</td>
      <td class="muted">${esc(k.created_by_email||"—")}</td>
      <td class="muted">${fmtDt(k.created_at)}</td>
      <td>${k.revoked_at?`<span class="st bad">revoked</span>`:`<button data-revoke-key="${k.id}">Revoke</button>`}</td>
    </tr>`,5,"No API keys yet");

  const userRows=renderRows(users,u=>`<tr>
      <td>${esc(u.email)}</td>
      <td><span class="tag">${esc(u.role)}</span></td>
      <td class="muted">${fmtDt(u.created_at)}</td>
      <td>${u.revoked_at?`<span class="st bad">revoked</span>`:`<button data-revoke-user="${u.id}">Revoke</button>`}</td>
    </tr>`,4,"No other users yet");

  $("#app").innerHTML=`
    <h2>Team</h2>
    <p class="muted">Signed in as ${me.email?esc(me.email):"a bearer admin key"} &middot; role <span class="tag">${esc(me.role)}</span></p>

    <div class="card">
      <div class="hd"><h3>Connect via MCP</h3></div>
      <p class="muted">Any MCP-capable client (e.g. Claude Code) can connect directly to this
        server at <span class="mono">${esc(mcpUrl)}</span> using an API key generated below.</p>
      <pre class="mono">claude mcp add --transport http chuk-experiments ${esc(mcpUrl)} --header "Authorization: Bearer &lt;YOUR_API_KEY&gt;"</pre>
      <p class="muted">Generate a key below and this command will be filled in for you, ready to copy.</p>
    </div>

    <div class="card">
      <div class="hd"><h3>My tokens</h3></div>
      <p class="muted">Used only for your own <span class="mono">verify_artifact</span> calls
        (git+/hf:// reference checks) — never shown again after saving, never shared with
        other users.</p>
      <table>
        <tr><td>GitHub</td>
          <td>${me.github_token_set?'<span class="st good">set</span>':'<span class="st mut">not set</span>'}</td>
          <td><form class="filters" data-token-form="github">
            <input type="password" name="token" placeholder="ghp_..." required>
            <button type="submit">Save</button>
            ${me.github_token_set?'<button type="button" data-clear-token="github">Clear</button>':""}
          </form></td></tr>
        <tr><td>Hugging Face</td>
          <td>${me.huggingface_token_set?'<span class="st good">set</span>':'<span class="st mut">not set</span>'}</td>
          <td><form class="filters" data-token-form="huggingface">
            <input type="password" name="token" placeholder="hf_..." required>
            <button type="submit">Save</button>
            ${me.huggingface_token_set?'<button type="button" data-clear-token="huggingface">Clear</button>':""}
          </form></td></tr>
      </table>
      <div id="token-status"></div>
    </div>

    <div class="card">
      <div class="hd"><h3>New API key</h3></div>
      <form class="filters" id="new-key-form">
        <input type="text" name="name" placeholder="key name" required>
        ${scopeChoices}
        <button type="submit">Generate</button>
      </form>
      <div id="new-key-box"></div>
    </div>

    <div class="card">
      <div class="hd"><h3>API keys</h3></div>
      <table><tr><th>Name</th><th>Scopes</th><th>Created by</th><th>Created</th><th></th></tr>${keyRows}</table>
    </div>

    ${isAdmin?`
    <div class="card">
      <div class="hd"><h3>Add user</h3></div>
      <form class="filters" id="add-user-form">
        <input type="email" name="email" placeholder="email" required>
        <select name="role"><option value="read">read</option><option value="write">write</option><option value="admin">admin</option></select>
        <button type="submit">Add</button>
      </form>
    </div>
    <div class="card">
      <div class="hd"><h3>Users</h3></div>
      <table><tr><th>Email</th><th>Role</th><th>Added</th><th></th></tr>${userRows}</table>
    </div>`:""}
  `;

  $("#new-key-form").addEventListener("submit",async ev=>{
    ev.preventDefault();
    const fd=new FormData(ev.target);
    const scopes=fd.getAll("scope");
    const box=$("#new-key-box");
    if(!scopes.length){box.innerHTML=`<p class="err">Choose at least one scope</p>`;return;}
    try{
      const created=await api("/v1/keys",{method:"POST",body:JSON.stringify({name:fd.get("name"),scopes})});
      const connectCmd=`claude mcp add --transport http chuk-experiments ${mcpUrl} --header "Authorization: Bearer ${created.raw_key}"`;
      box.innerHTML=`<div class="card"><strong>Copy this now — shown only once</strong>
        <p class="mono" style="word-break:break-all">${esc(created.raw_key)}</p>
        <p class="muted">Connect via MCP:</p>
        <pre class="mono" id="mcp-connect-cmd" style="white-space:pre-wrap">${esc(connectCmd)}</pre>
        <button type="button" data-copy-target="#mcp-connect-cmd">Copy command</button>
        <button type="button" id="new-key-done">Done</button></div>`;
      $("#new-key-done").addEventListener("click",loadTeam);
      ev.target.reset();
    }catch(e){box.innerHTML=`<p class="err">${esc(e.message)}</p>`;}
  });

  document.querySelectorAll("[data-token-form]").forEach(form=>{
    form.addEventListener("submit",async ev=>{
      ev.preventDefault();
      const provider=form.dataset.tokenForm;
      const token=new FormData(form).get("token");
      const box=$("#token-status");
      try{await api(`/v1/me/tokens/${provider}`,{method:"PUT",body:JSON.stringify({token})});loadTeam();}
      catch(e){box.innerHTML=`<p class="err">${esc(e.message)}</p>`;}
    });
  });

  if(isAdmin){
    $("#add-user-form").addEventListener("submit",async ev=>{
      ev.preventDefault();
      const fd=new FormData(ev.target);
      try{
        await api("/v1/users",{method:"POST",body:JSON.stringify({email:fd.get("email"),role:fd.get("role")})});
        loadTeam();
      }catch(e){$("#app").insertAdjacentHTML("afterbegin",`<p class="err">${esc(e.message)}</p>`);}
    });
  }

  $("#app").addEventListener("click",async ev=>{
    const keyId=ev.target.dataset.revokeKey;
    const userId=ev.target.dataset.revokeUser;
    const clearToken=ev.target.dataset.clearToken;
    const copyTarget=ev.target.dataset.copyTarget;
    if(keyId){await api("/v1/keys/"+keyId,{method:"DELETE"});loadTeam();}
    else if(userId){await api("/v1/users/"+userId,{method:"DELETE"});loadTeam();}
    else if(clearToken){await api(`/v1/me/tokens/${clearToken}`,{method:"DELETE"});loadTeam();}
    else if(copyTarget){
      const btn=ev.target,original=btn.textContent;
      await navigator.clipboard.writeText($(copyTarget).textContent);
      btn.textContent="Copied!";
      setTimeout(()=>{btn.textContent=original;},1500);
    }
  });
}
