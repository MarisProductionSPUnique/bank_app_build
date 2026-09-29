let csrf = '', pendingTransfer = null;
const $ = id => document.getElementById(id);
const currency = value => new Intl.NumberFormat('en-IN', {style:'currency', currency:'INR'}).format(value);
function notice(message, error=false) { $('notice').hidden=false; $('notice').className=error?'error':''; $('notice').textContent=message; }
async function api(path, options={}, inspect=true) {
  const start=performance.now();
  const response=await fetch(path, {...options, headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  let data; try {data=await response.json();} catch {data={error:'The server returned an unreadable response. Check the web-server logs.'};}
  if (inspect) {
    $('response-meta').textContent=`${options.method||'GET'} ${path} · HTTP ${response.status} · ${Math.round(performance.now()-start)} ms`;
    $('response-body').textContent=JSON.stringify({request_id:response.headers.get('X-Request-ID'),...data},null,2);
  }
  if(!response.ok) { const error=new Error(data.error||'Request failed'); error.status=response.status; throw error; }
  return data;
}
async function sessionState() {
  const state=await api('/api/session',{},false); csrf=state.csrf;
  $('login').hidden=!!state.user; $('workspace').hidden=!state.user; $('logout').hidden=!state.user;
  $('title').textContent=state.user?`Hello, ${state.user.name}.`:'Your banking classroom.';
  $('lab').hidden=!(state.user?.trainer && state.lab);
  if(state.user) await refresh();
}
async function refresh() {
  const [a,b,t]=await Promise.all([api('/api/accounts',{},false),api('/api/beneficiaries',{},false),api('/api/transactions',{},false)]);
  $('balance').textContent=currency(a.account.balance); $('account').textContent=a.account.number;
  const selected=$('recipient').value; $('recipient').replaceChildren();
  b.beneficiaries.forEach(person=>{const option=document.createElement('option');option.value=person.account;option.textContent=`${person.name} · ${person.account}`;$('recipient').append(option);});
  if([...$('recipient').options].some(o=>o.value===selected)) $('recipient').value=selected;
  $('transactions').replaceChildren();
  if(!t.transactions.length){const row=$('transactions').insertRow();const cell=row.insertCell();cell.colSpan=5;cell.textContent='No transfers yet. Make your first demo payment.';}
  t.transactions.forEach(t=>{const row=$('transactions').insertRow();[new Date(t.created).toLocaleString(),t.direction,currency(t.amount),t.status,`${t.id} / ${t.request_id}`].forEach(value=>{row.insertCell().textContent=value;});});
}
async function action(fn, button) {if(button)button.disabled=true;try{await fn();}catch(e){notice(e.message,true);}finally{if(button)button.disabled=false;}}
$('login-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{const values=Object.fromEntries(new FormData(e.target));const data=await api('/api/login',{method:'POST',body:JSON.stringify(values)},false);csrf=data.csrf;e.target.reset();await sessionState();notice('Signed in. All balances are demonstration money.');},e.submitter);});
$('logout').onclick=()=>action(async()=>{await api('/api/logout',{method:'POST'},false);pendingTransfer=null;$('response-body').textContent='Ready for your first request.';$('response-meta').textContent='Make a request to inspect its response.';await sessionState();notice('Signed out.');});
$('refresh').onclick=()=>action(async()=>{await refresh();notice('Account refreshed.');});
$('transfer-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{
 const payload={recipient:$('recipient').value,amount:$('amount').value};const fingerprint=JSON.stringify(payload);
 if(!pendingTransfer||pendingTransfer.fingerprint!==fingerprint)pendingTransfer={fingerprint,key:crypto.randomUUID()};
 let result;try{result=await api('/api/transfers',{method:'POST',headers:{'Idempotency-Key':pendingTransfer.key},body:fingerprint});}catch(error){if(error.status&&error.status<500)pendingTransfer=null;throw error;}
 pendingTransfer=null;notice(result.message);$('amount').value='';await refresh();
},e.submitter);});
$('beneficiary-form').addEventListener('submit',e=>{e.preventDefault();action(async()=>{await api('/api/beneficiaries',{method:'POST',body:JSON.stringify({account:$('beneficiary-account').value})});await refresh();notice('Beneficiary saved.');e.target.reset();},e.submitter);});
document.querySelectorAll('[data-scenario]').forEach(button=>button.onclick=()=>action(async()=>{const data=await api(`/api/lab/${button.dataset.scenario}`,{method:'POST'});notice(data.message);},button));
$('health').onclick=()=>action(async()=>{await api('/health/ready');notice('The application connected to the real database successfully.');});
sessionState().catch(e=>notice(e.message,true));
