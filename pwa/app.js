const app = {
  data: null,
  weather: null,
  history: [],
  view: 'dashboard',
  stream: null,
  scanning: false,
  facing: 'environment',
  refreshTimer: null,
  liveSocket: null,
  historyDays: 7,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const title = value => String(value || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
const fmtDate = value => { if (!value) return '—'; const d = new Date(value + (String(value).length === 10 ? 'T00:00:00' : '')); return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString(undefined,{month:'short',day:'numeric'}); };
const fmtDateTime = value => { if (!value) return '—'; const d = new Date(value); return Number.isNaN(d.getTime()) ? value : d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); };
const pct = value => `${Math.round(Number(value || 0) * 100)}%`;
const showToast = message => { const el = $('#toast'); el.textContent = message; el.classList.add('show'); clearTimeout(showToast.timer); showToast.timer = setTimeout(() => el.classList.remove('show'), 2600); };

async function getJSON(url, options = {}) {
  const response = await fetch(url, {cache:'no-store', ...options});
  if (response.status === 401) { showLoginGate(); throw new Error('401 Unauthorized'); }
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

// Shown only when the server actually returns a 401 -- which never happens
// with auth disabled (the default), so this stays invisible in normal/dev
// use and only activates in a deployment with TOMATOIQ_AUTH_REQUIRED=true.
function showLoginGate() {
  const gate = $('#login-gate');
  if (!gate.hidden) return; // already open
  gate.hidden = false;
  $('#sign-out-button').hidden = false;
}
function hideLoginGate() { $('#login-gate').hidden = true; $('#login-error').hidden = true; }

$('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target;
  const username = form.username.value.trim();
  const password = form.password.value;
  const errorEl = $('#login-error');
  errorEl.hidden = true;
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password}),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      errorEl.textContent = response.status === 429
        ? 'Too many attempts -- wait a few minutes and try again.'
        : (body.detail || 'Invalid username or password.');
      errorEl.hidden = false;
      return;
    }
    form.reset();
    hideLoginGate();
    await loadDashboard();
    if (!app.liveSocket) connectLiveUpdates();
  } catch {
    errorEl.textContent = 'Unable to reach the sign-in service.';
    errorEl.hidden = false;
  }
});

$('#sign-out-button').addEventListener('click', async () => {
  try { await fetch('/api/auth/logout', {method: 'POST'}); } catch {}
  location.reload();
});

// Single source of truth for the three honest connection states the UI can
// show. "source" comes straight from /api/dashboard's `source` field
// (live | stale | waiting), or is forced to null when the API itself is
// unreachable. Never invent a fourth, more flattering state.
function setConnection(reachable, source) {
  const pill = $('#live-pill');
  const label = $('#connection-label');
  const dot = $('#header-live-icon'); // may not exist on every view; guarded below
  pill.classList.remove('offline', 'stale');
  if (!reachable) {
    pill.querySelector('span').textContent = 'OFFLINE';
    pill.classList.add('offline');
    label.textContent = 'Connection issue';
    return;
  }
  if (source === 'live') {
    pill.querySelector('span').textContent = 'LIVE';
    label.textContent = 'Detector connected';
  } else if (source === 'stale') {
    pill.querySelector('span').textContent = 'DETECTOR OFFLINE';
    pill.classList.add('stale');
    label.textContent = 'Detector stopped responding';
  } else {
    pill.querySelector('span').textContent = 'NO DETECTOR';
    pill.classList.add('stale');
    label.textContent = 'Waiting for detector';
  }
}

// The camera-card header badge reflects the actual state of the *browser*
// live-scan feature (separate from the desktop detector reflected by
// setConnection above) -- it must never say "Real-time detection" unless
// scanning is actually active and reaching /api/detect.
function setCameraStatusBadge(text, offline) {
  const badge = $('#camera-live-status');
  if (!badge) return;
  badge.innerHTML = `<i></i>${escapeHtml(text)}`;
  badge.classList.toggle('offline', !!offline);
}

function updateClock() {
  const now = new Date();
  $('#current-date').textContent = now.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric',year:'numeric'});
  $('#current-time').textContent = now.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'});
}

function kpiCard(value, label, icon, tone, trend) {
  return `<article class="card kpi ${tone}"><div class="kpi-top"><div class="kpi-icon">${icon}</div><label>${escapeHtml(label)}</label></div><strong>${Number(value || 0).toLocaleString()}</strong><span class="trend ${trend < 0 ? 'warning' : ''}">${trend == null ? 'Live state' : `${trend >= 0 ? '↑' : '↓'} ${Math.abs(trend).toFixed(0)}% vs. previous snapshot`}</span></article>`;
}

function deriveHealth(state) {
  const total = Math.max(1, Number(state.total_tomatoes || 0));
  const riskRate = Number(state.disease_suspect_count || 0) / total;
  const ripeRate = Number((state.counts_by_class || {}).fully_ripened || 0) / total;
  const coverage = total > 0 ? 100 : 0;
  const health = Math.round(Math.max(0, Math.min(100, 100 - riskRate * 100)));
  const readiness = Math.round(ripeRate * 100);
  const ripeness = Math.round(Math.min(100, ((state.counts_by_class || {}).green || 0) / total * 100 + readiness));
  const data = {health, readiness, ripeness: Math.min(100, ripeness), coverage};
  data.overall = Math.round(data.health * .45 + data.readiness * .2 + data.ripeness * .2 + data.coverage * .15);
  return data;
}

function trendFor(field) {
  const points = app.history || [];
  if (points.length < 2) return null;
  const previous = Number(points[points.length - 2]?.[field] || 0);
  const current = Number(points[points.length - 1]?.[field] || 0);
  if (!previous) return current ? 100 : 0;
  return ((current - previous) / previous) * 100;
}

function trendForClassCount(className) {
  const points = app.history || [];
  if (points.length < 2) return null;
  const previous = Number(points[points.length - 2]?.counts_by_class?.[className] || 0);
  const current = Number(points[points.length - 1]?.counts_by_class?.[className] || 0);
  if (!previous) return current ? 100 : 0;
  return ((current - previous) / previous) * 100;
}

function renderDashboard() {
  const tpl = $('#dashboard-template').content.cloneNode(true);
  $('#view-root').replaceChildren(tpl);
  const {state, config, farm} = app.data;
  const counts = state.counts_by_class || {};
  $('#kpis').innerHTML = [
    kpiCard(state.total_tomatoes, 'Tomatoes detected', '●', 'neutral', trendFor('total_tomatoes')),
    kpiCard(state.ready_now, 'Ready for harvest', '✓', 'green', trendFor('ready_now')),
    kpiCard(counts.half_ripened || 0, 'Ripening soon', '◐', 'amber', trendForClassCount('half_ripened')),
    kpiCard(state.disease_suspect_count, 'At risk / needs inspection', '!', 'red', trendFor('disease_suspect_count')),
  ].join('');

  renderForecast(state);
  renderDistribution(state);
  renderRecommendations(state);
  renderWeather(app.weather, config);
  renderHealth(state);
  renderProfiles(state);
  renderAlerts(state);
  renderHistory(app.history);
  renderAnalytics(app.history, app.historyDays);
  bindCamera();
  $$('.segmented button').forEach(button => button.addEventListener('click', async () => {
    $$('.segmented button').forEach(b => b.classList.remove('active')); button.classList.add('active');
    app.historyDays = Number(button.dataset.days); await loadHistory(app.historyDays); renderAnalytics(app.history, app.historyDays);
  }));
}

function renderForecast(state) {
  const groups = {};
  (state.tomatoes || []).forEach(t => { const date = t.estimated_harvest_date; if (date && date !== 'unknown') (groups[date] ||= []).push(t); });
  const rows = Object.entries(groups).sort(([a],[b]) => a.localeCompare(b)).slice(0,6);
  $('#forecast').innerHTML = rows.length ? rows.map(([date, tomatoes]) => {
    const ready = tomatoes.filter(t => t.ready_now).length;
    return `<div class="forecast-row"><span class="date">${fmtDate(date)}</span><span class="count">🍅 ${tomatoes.length} ${ready ? `<span class="ready">· ${ready} ready</span>` : 'expected'}</span></div>`;
  }).join('') : '<div class="empty">No harvest predictions available yet. Start detection to build a forecast.</div>';
}

function renderDistribution(state) {
  const counts = state.counts_by_class || {}; const total = Math.max(0, Number(state.total_tomatoes || 0));
  const entries = [['green',counts.green||0,'#3F6E32'],['half_ripened',counts.half_ripened||0,'#B97A1E'],['fully_ripened',counts.fully_ripened||0,'#BC3B2A']];
  let cursor = 0; const segments = entries.map(([,n,c]) => { const start = cursor; cursor += total ? n/total*100 : 0; return `${c} ${start}% ${cursor}%`; });
  $('#donut').style.background = `conic-gradient(${segments.join(',') || '#E7ECE0 0 100%'})`;
  $('#donut-total').textContent = total.toLocaleString();
  $('#legend').innerHTML = entries.map(([name,n,c]) => `<div class="legend-row"><span class="legend-label"><i style="background:${c}"></i>${title(name)}</span><b>${total ? Math.round(n/total*100) : 0}% <span class="muted">(${n})</span></b></div>`).join('');
}

function renderRecommendations(state) {
  const ready = Number(state.ready_now || 0); const flagged = (state.tomatoes || []).filter(t => t.disease_suspect); const half = Number((state.counts_by_class || {}).half_ripened || 0);
  const actions = [];
  if (ready) actions.push(['red','Harvest ready tomatoes',`${ready} tomato${ready===1?' is':'es are'} ready for harvest today.`,'harvest']);
  if (flagged.length) actions.push(['amber','Inspect possible abnormalities',`${flagged.length} tomato${flagged.length===1?' needs':'es need'} a human inspection.`,'notifications']);
  if (half) actions.push(['amber','Monitor ripening fruit',`${half} tomato${half===1?' is':'es are'} in the half-ripened stage.`,'tomatoes']);
  if (!actions.length) actions.push(['green','No immediate actions','The detector is connected and there are no current priority actions.','dashboard']);
  $('#recommendations').innerHTML = actions.map(([severity,head,body,view]) => `<button class="stack-item severity-${severity}" data-view="${view}"><i class="status-dot"></i><span><strong>${escapeHtml(head)}</strong><small>${escapeHtml(body)}</small></span><span class="chevron">›</span></button>`).join('');
}

function renderWeather(weather, config) {
  const gdd = config.gdd || {}; const requiredMap = gdd.gdd_required || {}; const current = weather?.daily_gdd?.[0] || 0; const maxRequired = Math.max(0,...Object.values(requiredMap).map(Number));
  $('#weather-source').textContent = weather?.available ? weather.source : 'Unavailable';
  if (!weather?.available) { $('#weather-content').innerHTML = `<div class="empty">${escapeHtml(weather?.reason || 'Live weather is unavailable. Harvest estimates continue using configured model rules.')}</div>`; return; }
  const temp = Number(weather.temperature_c); const humidity = Number(weather.humidity_pct); const wind = Number(weather.wind_kmh);
  const gddPercent = maxRequired ? Math.min(100,current/maxRequired*100) : 0;
  $('#weather-content').innerHTML = `<div class="weather-main"><div class="temperature">${Number.isFinite(temp)?Math.round(temp):'—'}°C</div><div><strong>Current conditions</strong><div class="muted">Farm weather feed</div></div></div><div class="weather-details"><div class="weather-detail"><small>Humidity</small><strong>${Number.isFinite(humidity)?Math.round(humidity):'—'}%</strong></div><div class="weather-detail"><small>Wind</small><strong>${Number.isFinite(wind)?Math.round(wind):'—'} km/h</strong></div></div><div><div class="card-head"><small class="muted">Daily GDD</small><strong>${current.toFixed(1)} / ${maxRequired || '—'}</strong></div><div class="gdd-bar"><i style="width:${gddPercent}%"></i></div><small class="muted">Base ${gdd.base_temp_c ?? '—'}°C · cap ${gdd.cap_c ?? '—'}°C · ${escapeHtml(gdd.enabled ? 'weather-based estimation' : 'configured fallback')}</small></div>`;
}

function renderHealth(state) {
  const h = deriveHealth(state); $('#health-score').textContent = h.overall; $('#health-status').textContent = h.overall >= 80 ? 'Good' : h.overall >= 60 ? 'Monitor' : 'Attention';
  $('#health-ring').style.background = `conic-gradient(var(--green) ${h.overall}%,#E7ECE0 ${h.overall}% 100%)`;
  const rows = [['Crop health',h.health],['Ripeness',h.ripeness],['Harvest readiness',h.readiness],['Data coverage',h.coverage]];
  $('#health-breakdown').innerHTML = rows.map(([name,value]) => `<div class="health-row"><span>${name}</span><b>${value}</b><span class="bar"><i style="width:${value}%"></i></span></div>`).join('');
  $('#health-note').textContent = state.disease_suspect_count ? `${state.disease_suspect_count} tomato${state.disease_suspect_count===1?'':'es'} currently require inspection. Screening flags are not disease diagnoses.` : 'No inspection flags are currently reported by the detector.';
}

function renderProfiles(state) {
  const tomatoes = (state.tomatoes || []).slice().sort((a,b) => Number(b.disease_suspect)-Number(a.disease_suspect) || Number(b.ready_now)-Number(a.ready_now)).slice(0,8);
  $('#profiles').innerHTML = tomatoes.length ? tomatoes.map(t => `<button class="profile" data-tomato-id="${escapeHtml(t.tomato_id)}"><div class="profile-head"><div class="tomato-avatar">🍅</div><div><div class="profile-id">#${escapeHtml(String(t.tomato_id).padStart(3,'0'))}</div><span class="badge">${escapeHtml(title(t.ripeness_class))}</span></div></div><small>Harvest: ${escapeHtml(fmtDate(t.estimated_harvest_date))}<br>${t.disease_suspect ? `⚠ Inspection · ${pct(t.disease_confidence)}` : '● No screening flag'}</small></button>`).join('') : '<div class="empty">No tracked tomatoes yet.</div>';
}

function renderAlerts(state) {
  const alerts = [];
  (state.tomatoes || []).filter(t => t.disease_suspect).slice(0,5).forEach(t => alerts.push(['red',`Possible abnormality — Tomato #${t.tomato_id}`,`${title(t.disease_reason || 'Visual screening flag')} · ${pct(t.disease_confidence)}`]));
  if (state.ready_now) alerts.push(['green',`Harvest ready — ${state.ready_now} tomato${state.ready_now===1?'':'es'}`,'Review the harvest forecast for the affected fruit.']);
  $('#alerts').innerHTML = alerts.length ? alerts.slice(0,6).map(([severity,head,body]) => `<div class="stack-item severity-${severity}"><i class="status-dot"></i><span><strong>${escapeHtml(head)}</strong><small>${escapeHtml(body)}</small></span></div>`).join('') : '<div class="empty">No active alerts.</div>';
  $('#nav-alert-count').textContent = alerts.length; $('#header-alert-count').textContent = alerts.length;
}

function renderHistory(points) {
  const rows = (points || []).slice(-6).reverse(); $('#scan-history').innerHTML = rows.length ? rows.map(p => `<div class="history-item"><strong>${escapeHtml(fmtDateTime(p.timestamp))}</strong><span>${Number(p.total_tomatoes||0)} detected · ${Number(p.ready_now||0)} ready · ${Number(p.disease_suspect_count||0)} flagged</span></div>`).join('') : '<div class="empty">Scan history will appear as live state snapshots are received.</div>';
}

function renderAnalytics(points, days) {
  const root = $('#harvest-chart'); if (!root) return; const cutoff = Date.now() - days*86400000; const filtered = (points||[]).filter(p => new Date(p.timestamp).getTime() >= cutoff);
  const byDay = {}; filtered.forEach(p => { const d = new Date(p.timestamp); const key = d.toISOString().slice(0,10); byDay[key] = p; });
  const entries = Object.entries(byDay).slice(-Math.min(days,14)); const max = Math.max(1,...entries.map(([,p]) => Number(p.ready_now||0)));
  root.innerHTML = entries.length ? entries.map(([date,p]) => `<div class="bar-col"><b>${Number(p.ready_now||0)}</b><i style="height:${Math.max(3,Number(p.ready_now||0)/max*78)}%" title="${Number(p.ready_now||0)} ready"></i><span>${fmtDate(date)}</span></div>`).join('') : '<div class="empty">Analytics will populate after live snapshots have been collected.</div>';
  const latest = points?.[points.length-1] || {}; const total = Number(latest.total_tomatoes||0); const flagged = Number(latest.disease_suspect_count||0);
  $('#mini-stats').innerHTML = `<div class="mini-stat"><small>Latest monitored</small><strong>${total.toLocaleString()} tomatoes</strong></div><div class="mini-stat"><small>Current inspection rate</small><strong>${total ? (flagged/total*100).toFixed(1) : '0.0'}%</strong></div>`;
}

function bindSettingsForm() {
  const form = $('#settings-form'); if (!form) return;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(form);
    const payload = {};
    for (const [key, raw] of data.entries()) {
      if (raw === '') continue; // untouched/blank field -> don't overwrite with empty
      if (key.endsWith('_enabled')) payload[key] = raw === 'true';
      else if (['farm_name','farm_manager','farm_variety'].includes(key)) payload[key] = raw;
      else payload[key] = Number(raw);
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true; button.textContent = 'Saving…';
    try {
      await getJSON('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      showToast('Settings saved.');
      await loadDashboard();
    } catch {
      showToast('Could not save settings — check the server connection.');
    } finally {
      button.disabled = false; button.textContent = 'Save settings';
    }
  });
}

function bindCamera() {
  const video = $('#video'); if (!video) return; const canvas = $('#overlay'); const ctx = canvas.getContext('2d');
  const status = message => $('#camera-status').textContent = message;
  setCameraStatusBadge('Camera off', true);
  async function startCamera() {
    if (app.stream) app.stream.getTracks().forEach(track => track.stop());
    try {
      app.stream = await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:app.facing}},audio:false});
      video.srcObject = app.stream; $('#camera-empty').hidden = true;
      status('Camera ready. Start live scan.');
      setCameraStatusBadge('Camera ready · scan paused', true);
    } catch {
      $('#camera-empty').hidden = false;
      status('Camera unavailable. Allow browser camera access to scan live.');
      setCameraStatusBadge('Camera unavailable', true);
    }
  }
  async function infer() {
    if (!app.scanning || !video.videoWidth) return;
    const temp = document.createElement('canvas'); temp.width = 640; temp.height = Math.round(640*video.videoHeight/video.videoWidth); temp.getContext('2d').drawImage(video,0,0,temp.width,temp.height);
    const blob = await new Promise(resolve => temp.toBlob(resolve,'image/jpeg',.72)); const form = new FormData(); form.append('frame',blob,'frame.jpg');
    try {
      const result = await getJSON('/api/detect',{method:'POST',body:form});
      drawBoxes(result.detections);
      $('#camera-stats').textContent = `${result.count} tomatoes · ${result.detections.filter(d=>d.label==='fully_ripened').length} ripe in frame · ${app.data.state.disease_suspect_count||0} flagged`;
      status(`Live detection active · ${new Date().toLocaleTimeString()}`);
      setCameraStatusBadge('Real-time detection', false);
    } catch {
      status('Detection service unavailable. Retrying…');
      setCameraStatusBadge('Detection service unavailable', true);
    }
    if (app.scanning) setTimeout(infer,500);
  }
  function drawBoxes(detections) { canvas.width=video.videoWidth; canvas.height=video.videoHeight; ctx.clearRect(0,0,canvas.width,canvas.height); detections.forEach(d=>{const x=d.x*canvas.width,y=d.y*canvas.height,w=d.width*canvas.width,h=d.height*canvas.height;const c=d.label==='green'?'#3F6E32':d.label==='half_ripened'?'#B97A1E':'#BC3B2A';ctx.strokeStyle=c;ctx.lineWidth=3;ctx.strokeRect(x,y,w,h);ctx.fillStyle=c;ctx.font='700 14px system-ui';ctx.fillText(`${title(d.label)} ${pct(d.confidence)}`,x,Math.max(16,y-7));});}
  $('#scan-button').onclick = () => {
    app.scanning=!app.scanning;
    $('#scan-button').textContent=app.scanning?'Ⅱ Pause':'▶ Start live scan';
    status(app.scanning?'Starting live detection…':'Live scan paused');
    setCameraStatusBadge(app.scanning?'Starting…':'Scan paused', true);
    if(app.scanning) infer();
  };
  $('#switch-camera').onclick = () => { app.facing=app.facing==='environment'?'user':'environment'; startCamera(); };
  startCamera();
}

function renderSubpage(view) {
  const {state, farm, config} = app.data; const tomatoes = state.tomatoes || [];
  const wrappers = {
    scanner: ['Live Scanner','Continuous camera detection and field-level observation.'],
    tomatoes: ['Tomato Profiles','Inspect every tracked tomato and its current harvest assessment.'],
    harvest: ['Harvest Calendar','A data-driven view of estimated harvest readiness.'],
    analytics: ['Analytics','Historical trends built from live detector snapshots.'],
    farm: ['Farm Map','Farm configuration and monitoring coverage.'],
    reports: ['Reports','Export current detector results for field operations.'],
    notifications: ['Notifications','Current inspection and harvest alerts.'],
    settings: ['Settings','Configuration currently powering TomatoIQ.'],
  };
  const [heading,description] = wrappers[view];
  let body='';
  if(view==='scanner') body=`<div class="card camera-card" style="grid-column:auto"><div class="card-head"><div><span class="eyebrow">Live scanner</span><h2>Field camera</h2></div></div><div class="camera-stage" id="scanner-stage"><video id="video" autoplay playsinline muted></video><canvas id="overlay"></canvas><div class="camera-empty" id="camera-empty"><div>◉</div><strong>Camera ready</strong><small>Start live scanning below.</small></div><div class="camera-status" id="camera-status">Camera initializing…</div></div><div class="camera-footer"><span id="camera-stats">0 tomatoes</span><button class="secondary" id="switch-camera">↺ Switch</button><button class="primary" id="scan-button">▶ Start live scan</button></div></div>`;
  if(view==='tomatoes') body=`<div class="card"><table class="data-table"><thead><tr><th>ID</th><th>Ripeness</th><th>Harvest</th><th>Status</th><th>Confidence</th></tr></thead><tbody>${tomatoes.map(t=>`<tr><td>#${escapeHtml(t.tomato_id)}</td><td>${escapeHtml(title(t.ripeness_class))}</td><td>${escapeHtml(fmtDate(t.estimated_harvest_date))}</td><td>${t.disease_suspect?'⚠ Inspect':t.ready_now?'✓ Ready':'Monitoring'}</td><td>${t.disease_suspect?pct(t.disease_confidence):'—'}</td></tr>`).join('')||'<tr><td colspan="5">No tracked tomatoes.</td></tr>'}</tbody></table></div>`;
  if(view==='harvest') { const groups={}; tomatoes.forEach(t=>{if(t.estimated_harvest_date && t.estimated_harvest_date!=='unknown')(groups[t.estimated_harvest_date] ||= []).push(t)}); body=`<div class="card"><div class="history-list">${Object.entries(groups).sort().map(([d,a])=>`<div class="history-item"><strong>${escapeHtml(fmtDate(d))}</strong><span>${a.length} predicted · ${a.filter(t=>t.ready_now).length} ready now</span></div>`).join('')||'<div class="empty">No harvest predictions yet.</div>'}</div></div>`; }
  if(view==='analytics') body=`<div class="card"><div id="full-analytics" class="chart-area" style="height:300px"></div></div>`;
  if(view==='farm') body=`<div class="card"><div class="hero-card"><span class="eyebrow">Active farm</span><h2>${escapeHtml(farm.name)}</h2><div>${escapeHtml(farm.location?.city || 'Configured location')}${farm.location?.country ? ', '+escapeHtml(farm.location.country) : ''}</div></div><div class="settings-grid"><div class="setting"><strong>Crop variety</strong><small>${escapeHtml(farm.variety || 'Not configured')}</small></div><div class="setting"><strong>Farm size</strong><small>${farm.size_hectares ? `${farm.size_hectares} hectares` : 'Not configured'}</small></div><div class="setting"><strong>Latitude</strong><small>${farm.location?.latitude ?? 'Not configured'}</small></div><div class="setting"><strong>Longitude</strong><small>${farm.location?.longitude ?? 'Not configured'}</small></div></div></div>`;
  if(view==='reports') body=`<div class="card"><div class="hero-card"><span class="eyebrow">Export</span><h2>Current tomato report</h2><p>Download the current detector state as CSV. No dashboard values are embedded in the report.</p><a class="primary" href="/api/report" download>Download CSV report</a></div></div>`;
  if(view==='notifications') { const flagged=tomatoes.filter(t=>t.disease_suspect); body=`<div class="card"><div class="stack">${flagged.map(t=>`<div class="stack-item severity-red"><i class="status-dot"></i><span><strong>Possible abnormality — Tomato #${escapeHtml(t.tomato_id)}</strong><small>${escapeHtml(t.disease_reason || 'Visual screening flag')} · ${pct(t.disease_confidence)}</small></span></div>`).join('')}${state.ready_now?`<div class="stack-item severity-green"><i class="status-dot"></i><span><strong>Harvest ready</strong><small>${state.ready_now} tomato${state.ready_now===1?'':'es'} currently ready for harvest.</small></span></div>`:'<div class="empty">No active alerts.</div>'}</div></div>`; }
  if(view==='settings') body=`<div class="card"><form id="settings-form" class="settings-grid">
    <label class="setting"><strong>Detection confidence threshold</strong><input type="number" name="confidence_threshold" min="0" max="1" step="0.01" value="${Number(config.confidence_threshold).toFixed(2)}"></label>
    <label class="setting"><strong>GDD-based harvest estimation</strong><select name="gdd_enabled"><option value="true" ${config.gdd?.enabled?'selected':''}>Enabled (weather-based)</option><option value="false" ${!config.gdd?.enabled?'selected':''}>Disabled (flat day-count fallback)</option></select></label>
    <label class="setting"><strong>GDD base temperature (°C)</strong><input type="number" name="gdd_base_temp_c" step="0.5" value="${config.gdd?.base_temp_c ?? ''}"></label>
    <label class="setting"><strong>GDD heat cap (°C)</strong><input type="number" name="gdd_cap_c" step="0.5" value="${config.gdd?.cap_c ?? ''}"></label>
    <label class="setting"><strong>Disease/anomaly screening</strong><select name="disease_screening_enabled"><option value="true" ${config.disease_screening?.enabled?'selected':''}>Enabled</option><option value="false" ${!config.disease_screening?.enabled?'selected':''}>Disabled</option></select></label>
    <label class="setting"><strong>Farm name</strong><input type="text" name="farm_name" value="${escapeHtml(farm.name || '')}"></label>
    <label class="setting"><strong>Farm manager</strong><input type="text" name="farm_manager" value="${escapeHtml(farm.manager || '')}"></label>
    <label class="setting"><strong>Crop variety</strong><input type="text" name="farm_variety" value="${escapeHtml(farm.variety || '')}"></label>
    <label class="setting"><strong>Farm size (hectares)</strong><input type="number" step="0.1" name="farm_size_hectares" value="${farm.size_hectares ?? ''}"></label>
    <label class="setting"><strong>Latitude</strong><input type="number" step="0.0001" name="farm_latitude" value="${farm.location?.latitude ?? ''}"></label>
    <label class="setting"><strong>Longitude</strong><input type="number" step="0.0001" name="farm_longitude" value="${farm.location?.longitude ?? ''}"></label>
    <div class="setting" style="grid-column:1/-1;display:flex;justify-content:flex-end"><button class="primary" type="submit">Save settings</button></div>
  </form></div>`;
  $('#view-root').innerHTML=`<section class="view subpage"><div class="hero-card"><span class="eyebrow">TOMATOIQ</span><h2>${escapeHtml(heading)}</h2><p>${escapeHtml(description)}</p></div>${body}</section>`;
  if(view==='scanner') bindCamera();
  if(view==='settings') bindSettingsForm();
  if(view==='analytics'){const root=$('#full-analytics'); const max=Math.max(1,...app.history.map(p=>Number(p.ready_now||0))); root.innerHTML=app.history.slice(-20).map(p=>`<div class="bar-col"><b>${Number(p.ready_now||0)}</b><i style="height:${Math.max(3,Number(p.ready_now||0)/max*88)}%"></i><span>${fmtDate(p.timestamp)}</span></div>`).join('') || '<div class="empty">Collecting history…</div>';}
}

async function loadHistory(days=7){ try { const result=await getJSON(`/api/history?days=${days}`); app.history=result.points || []; } catch { app.history=[]; } }
async function loadWeather(){ try { app.weather=await getJSON('/api/weather'); } catch { app.weather={available:false,reason:'Weather service unavailable.'}; } }
async function loadDashboard(){
  try {
    app.data=await getJSON('/api/dashboard');
    setConnection(true,app.data.source); $('#sidebar-farm').textContent=app.data.farm.name || 'Configured farm';
    $('#greeting').textContent=app.data.farm.manager ? `Good morning, ${app.data.farm.manager}!` : `${app.data.farm.name || 'Farm'} intelligence`;
    $('#subtitle').textContent=app.data.source==='live' ? 'Live crop monitoring and harvest readiness.' : 'Waiting for the detector to publish live state.';
    const selector=$('#farm-selector'); if(!selector.options.length){ const option=document.createElement('option'); option.value=app.data.farm.name; option.textContent=app.data.farm.name; selector.append(option); }
    if(!app.weather) await loadWeather();
    await loadHistory(app.historyDays);
    if(app.view==='dashboard') renderDashboard(); else renderSubpage(app.view);
  } catch(error) { setConnection(false); $('#subtitle').textContent='Unable to reach the TomatoIQ service. Retrying automatically…'; }
}

function navigate(view){ app.view=view; $$('.nav-item').forEach(item=>item.classList.toggle('active',item.dataset.view===view)); if(view==='dashboard') renderDashboard(); else renderSubpage(view); $('#sidebar').classList.remove('open'); }

document.addEventListener('click', event => { const nav=event.target.closest('[data-view]'); if(nav) navigate(nav.dataset.view); const profile=event.target.closest('[data-tomato-id]'); if(profile) showTomato(profile.dataset.tomatoId); });
function showTomato(id){ const tomato=(app.data?.state?.tomatoes||[]).find(t=>String(t.tomato_id)===String(id)); if(!tomato)return; showToast(`Tomato #${tomato.tomato_id}: ${title(tomato.ripeness_class)} · harvest ${fmtDate(tomato.estimated_harvest_date)}`); }
$('#mobile-menu').onclick=()=>$('#sidebar').classList.toggle('open');
$('#notifications-button').onclick=()=>navigate('notifications');
window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();window.installPrompt=event;});
if('serviceWorker' in navigator) navigator.serviceWorker.register('/assets/sw.js').catch(()=>{});
function connectLiveUpdates() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = app.liveSocket = new WebSocket(`${protocol}://${location.host}/api/live`);
  socket.onmessage = async event => {
    const update = JSON.parse(event.data);
    if (update.type !== 'dashboard_update') return;
    // The socket delivers state immediately. Refresh configuration/history only
    // when a detector update arrives instead of polling every five seconds.
    await loadDashboard();
  };
  socket.onclose = () => { app.liveSocket = null; setTimeout(connectLiveUpdates, 5000); };
  socket.onerror = () => socket.close();
}
updateClock(); setInterval(updateClock,1000); loadDashboard(); connectLiveUpdates();
// A low-frequency recovery sync protects users behind WebSocket-blocking proxies.
app.refreshTimer=setInterval(loadDashboard,60000); setInterval(async()=>{await loadWeather(); if(app.view==='dashboard') renderDashboard();},10*60*1000);
