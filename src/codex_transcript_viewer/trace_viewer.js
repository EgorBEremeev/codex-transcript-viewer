(() => {
  "use strict";
  const breakdown = JSON.parse(document.getElementById("breakdown-data").textContent);
  const analysis = JSON.parse(document.getElementById("spans-data").textContent);
  const COLORS = { input_tokens: "#246bce", cached_input_tokens: "#67a6ee", cache_write_input_tokens: "#8358c7", output_tokens: "#149174", reasoning_output_tokens: "#df7c2c", model_context_window: "#8e99aa" };
  const TOKEN_KEYS = ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"];
  const state = { sessionId: breakdown.root_session_id, role: "", spanKind: "", eventKind: "", search: "", domain: null, enabled: new Set([...TOKEN_KEYS, "model_context_window"]) };
  const sessions = new Map((breakdown.sessions || []).map(session => [session.session_id, session]));
  const events = new Map();
  for (const session of sessions.values()) for (const event of session.events || []) events.set(event.event_id, event);
  const spans = analysis.spans || [];
  const spansById = new Map(spans.map(span => [span.span_id, span]));
  const sessionSpans = new Map(spans.filter(span => span.kind === "session").map(span => [span.session_id, span]));
  const toolSpans = spans.filter(span => span.kind === "tool");
  const toolByCall = new Map(toolSpans.map(span => [span.start_event_id, span]));
  const allDated = [...events.values()].filter(event => Number.isFinite(event.timestamp_ms));
  const fullDomain = [Math.min(...allDated.map(event => event.timestamp_ms)), Math.max(...allDated.map(event => event.timestamp_ms))];
  const roles = new Map();
  for (const session of sessions.values()) {
    const role = session.meta && session.meta.agent_role ? session.meta.agent_role : "user";
    if (!roles.has(role)) roles.set(role, []);
    roles.get(role).push(session);
  }
  const fmt = new Intl.NumberFormat("ru-RU");
  const shortTime = ms => Number.isFinite(ms) ? new Date(ms).toLocaleTimeString("ru-RU", {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "—";
  const duration = ms => !Number.isFinite(ms) ? "—" : ms >= 3600000 ? `${(ms / 3600000).toFixed(2)} h` : ms >= 60000 ? `${(ms / 60000).toFixed(2)} m` : `${(ms / 1000).toFixed(2)} s`;
  const number = value => Number.isFinite(value) ? fmt.format(value) : "—";
  const esc = value => String(value == null ? "" : value).replace(/[&<>\"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[char]));
  const eventDetails = event => event && event.details && typeof event.details === "object" ? event.details : {};
  const payload = event => Number(eventDetails(event).payload_size && eventDetails(event).payload_size.serialized_json_utf8_bytes) || 0;
  const size = (event, field) => Number(eventDetails(event)[field] && eventDetails(event)[field].serialized_json_utf8_bytes) || 0;

  function fillControls() {
    const roleSelect = document.getElementById("role-filter");
    for (const role of [...roles.keys()].sort()) roleSelect.insertAdjacentHTML("beforeend", `<option value="${esc(role)}">${esc(role)}</option>`);
    const sessionSelect = document.getElementById("session-filter");
    for (const [role, list] of roles) for (const session of list) sessionSelect.insertAdjacentHTML("beforeend", `<option value="${esc(session.session_id)}">${esc(role)} · ${esc(session.agent_path || "user")}</option>`);
    const kinds = new Set(); for (const event of events.values()) kinds.add(event.kind || event.payload_type || "unknown");
    const eventSelect = document.getElementById("event-filter");
    for (const kind of [...kinds].sort()) eventSelect.insertAdjacentHTML("beforeend", `<option value="${esc(kind)}">${esc(kind)}</option>`);
    roleSelect.addEventListener("change", event => { state.role = event.target.value; render(); });
    sessionSelect.addEventListener("change", event => { if (event.target.value) state.sessionId = event.target.value; render(); });
    document.getElementById("span-filter").addEventListener("change", event => { state.spanKind = event.target.value; renderTrace(); renderTable(); });
    eventSelect.addEventListener("change", event => { state.eventKind = event.target.value; renderTrace(); renderTable(); });
    document.getElementById("search-filter").addEventListener("input", event => { state.search = event.target.value.trim().toLocaleLowerCase(); renderTrace(); renderTable(); });
    document.getElementById("reset-zoom").addEventListener("click", () => { state.domain = null; render(); });
  }

  function selectedSession() { return sessions.get(state.sessionId) || sessions.get(breakdown.root_session_id) || [...sessions.values()][0]; }
  function activeDomain() { return state.domain || fullDomain; }
  function spanLabel(span) {
    if (span.kind !== "tool") return span.kind;
    const attrs = span.attributes || {}; const nested = Array.isArray(attrs.nested_calls) ? attrs.nested_calls[0] : null;
    return [attrs.name, nested && nested.tool, nested && nested.command_name].filter(Boolean).join(" → ") || "tool";
  }
  function matchText(value) { return !state.search || String(value || "").toLocaleLowerCase().includes(state.search); }
  function visibleSessions() {
    return [...roles.entries()].filter(([role]) => !state.role || role === state.role).map(([role, list]) => [role, list.filter(session => !state.search || matchText(session.agent_path) || matchText(session.session_id))]).filter(([, list]) => list.length);
  }
  function xFor(value, width, domain) { return ((value - domain[0]) / Math.max(1, domain[1] - domain[0])) * width; }

  function renderHeader() {
    const task = spans.find(span => span.kind === "task");
    document.getElementById("trace-summary").textContent = `Root ${breakdown.root_session_id} · source breakdown schema ${breakdown.schema_version} · span analysis ${analysis.analysis_version}`;
    document.getElementById("summary-cards").innerHTML = [
      ["Sessions", sessions.size], ["Events", events.size], ["Spans", spans.length], ["Wall clock", duration(task && task.duration_ms)]
    ].map(([label, value]) => `<div class="summary-card">${esc(label)}<strong>${esc(value)}</strong></div>`).join("");
  }

  function canvasSize(canvas, width, height) {
    const ratio = window.devicePixelRatio || 1; canvas.width = Math.max(1, Math.floor(width * ratio)); canvas.height = Math.max(1, Math.floor(height * ratio)); canvas.style.width = `${width}px`; canvas.style.height = `${height}px`; const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); return ctx;
  }

  function renderTrace() {
    const labels = document.getElementById("trace-labels"); labels.innerHTML = "";
    const rows = []; for (const [role, list] of visibleSessions()) { rows.push({role}); for (const session of list) rows.push({session}); }
    for (const row of rows) {
      const element = document.createElement("div");
      if (row.role) { element.className = "trace-label role"; element.textContent = row.role; }
      else { element.className = `trace-label ${row.session.session_id === state.sessionId ? "selected" : ""}`; element.textContent = row.session.agent_path || "user"; element.title = row.session.session_id; element.onclick = () => { state.sessionId = row.session.session_id; document.getElementById("session-filter").value = state.sessionId; render(); }; }
      labels.appendChild(element);
    }
    const wrap = document.querySelector(".trace-canvas-wrap"); const canvas = document.getElementById("trace-canvas"); const width = wrap.clientWidth; const height = Math.max(wrap.clientHeight, rows.length * 28); const ctx = canvasSize(canvas, width, height); const domain = activeDomain();
    document.getElementById("time-range").textContent = `${shortTime(domain[0])} — ${shortTime(domain[1])} · ${duration(domain[1] - domain[0])}`;
    ctx.clearRect(0, 0, width, height); ctx.font = "11px Segoe UI";
    for (let i = 0; i <= 5; i++) { const x = Math.round(width * i / 5) + .5; ctx.strokeStyle = "#e4eaf3"; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); ctx.fillStyle="#637188"; ctx.fillText(shortTime(domain[0] + (domain[1]-domain[0])*i/5), Math.min(width - 66, x + 3), 12); }
    let index = 0;
    for (const row of rows) {
      const y = index++ * 28; if (row.role) { ctx.fillStyle="#edf3fc"; ctx.fillRect(0,y,width,26); continue; }
      const session = row.session; const sessionSpan = sessionSpans.get(session.session_id); ctx.fillStyle = session.session_id === state.sessionId ? "#dceaff" : "#f8fafc"; ctx.fillRect(0,y,width,27);
      if (sessionSpan && Number.isFinite(sessionSpan.start_ms)) { const start=xFor(sessionSpan.start_ms,width,domain), end=xFor(sessionSpan.end_ms || sessionSpan.start_ms,width,domain); ctx.fillStyle="#a8c8fb"; ctx.fillRect(Math.max(0,start), y+10, Math.max(2,end-start), 8); }
      const showTurns = !state.spanKind || state.spanKind === "turn";
      if (showTurns) for (const span of spans.filter(item => item.kind === "turn" && item.session_id === session.session_id)) {
        if (!Number.isFinite(span.start_ms)) continue; const start=xFor(span.start_ms,width,domain), end=xFor(span.end_ms || span.start_ms,width,domain); ctx.strokeStyle="#8e99aa"; ctx.lineWidth=1; ctx.strokeRect(Math.max(0,start)+.5, y+5.5, Math.max(3,end-start)-1, 17);
      }
      const showTools = !state.spanKind || state.spanKind === "tool";
      if (showTools) for (const span of toolSpans.filter(item => item.session_id === session.session_id && matchText(spanLabel(item)))) {
        if (!Number.isFinite(span.start_ms)) continue; const start=xFor(span.start_ms,width,domain), end=xFor(span.end_ms || span.start_ms,width,domain); ctx.fillStyle="#3278de"; ctx.fillRect(Math.max(0,start), y+7, Math.max(3,end-start), 14);
      }
      const markers = (session.events || []).filter(event => Number.isFinite(event.timestamp_ms) && (!state.eventKind || event.kind === state.eventKind) && matchText(event.kind));
      for (const event of markers) { const x=xFor(event.timestamp_ms,width,domain); if (x < 0 || x > width) continue; ctx.fillStyle = event.kind === "token_count" ? "#8b53bf" : event.kind.includes("compacted") ? "#d1762b" : event.kind.includes("message") ? "#07846b" : "#44536b"; ctx.beginPath(); ctx.arc(x,y+14,2.5,0,Math.PI*2); ctx.fill(); }
    }
    let drag = null;
    canvas.onpointerdown = event => { drag = {x:event.offsetX, y:event.offsetY}; canvas.setPointerCapture(event.pointerId); };
    canvas.onpointerup = event => { if (!drag) return; const dx=event.offsetX-drag.x; if (Math.abs(dx)>8) { const a=Math.max(0,Math.min(width,drag.x)), b=Math.max(0,Math.min(width,event.offsetX)); const current=activeDomain(); state.domain=[current[0]+(current[1]-current[0])*Math.min(a,b)/width,current[0]+(current[1]-current[0])*Math.max(a,b)/width]; render(); } else { const row=Math.floor(drag.y/28); const target=rows[row]; if (target && target.session) { state.sessionId=target.session.session_id; document.getElementById("session-filter").value=state.sessionId; render(); } } drag=null; };
  }

  function tokenSeries(session, key, source) { return (session.events || []).filter(event => event.kind === "token_count" && Number.isFinite(event.timestamp_ms)).map(event => ({x:event.timestamp_ms, y:Number(eventDetails(event).info && eventDetails(event).info[source] && eventDetails(event).info[source][key]) || 0})); }
  function drawChart(id, series, domain) {
    const canvas=document.getElementById(id), width=canvas.parentElement.clientWidth-18, height=canvas.clientHeight || 185, ctx=canvasSize(canvas,width,height); const visible=series.flatMap(item=>item.points).filter(point=>point.x>=domain[0]&&point.x<=domain[1]); const max=Math.max(1,...visible.map(point=>point.y)); ctx.clearRect(0,0,width,height); ctx.strokeStyle="#dfe6f0"; for(let i=0;i<4;i++){const y=18+(height-36)*i/3;ctx.beginPath();ctx.moveTo(35,y);ctx.lineTo(width-4,y);ctx.stroke();} ctx.fillStyle="#637188";ctx.font="10px Segoe UI";ctx.fillText(number(max),2,18);ctx.fillText("0",15,height-17);
    for (const item of series) { if (!state.enabled.has(item.key) || !item.points.length) continue; ctx.strokeStyle=item.color;ctx.lineWidth=1.7;ctx.beginPath();let moved=false; for(const point of item.points){if(point.x<domain[0]||point.x>domain[1])continue;const x=35+xFor(point.x,width-39,domain),y=height-18-(point.y/max)*(height-36);if(!moved){ctx.moveTo(x,y);moved=true;}else ctx.lineTo(x,y);}ctx.stroke(); }
  }

  function renderMetrics() {
    const session=selectedSession(); if (!session) return; document.getElementById("metrics-title").textContent=`Кумулятивные метрики · ${session.agent_path || "user"}`; const domain=activeDomain(); let cumulative=0; const payloadPoints=[];
    for(const event of session.events || []) { if(!Number.isFinite(event.timestamp_ms))continue; cumulative+=payload(event); payloadPoints.push({x:event.timestamp_ms,y:cumulative}); }
    drawChart("payload-chart", [{key:"payload",color:"#1f6feb",points:payloadPoints}],domain);
    drawChart("total-chart", TOKEN_KEYS.map(key=>({key,color:COLORS[key],points:tokenSeries(session,key,"total_token_usage")})),domain);
    drawChart("last-chart", [...TOKEN_KEYS,"model_context_window"].map(key=>({key,color:COLORS[key],points:key==="model_context_window"?(session.events||[]).filter(event=>event.kind==="token_count"&&Number.isFinite(event.timestamp_ms)).map(event=>({x:event.timestamp_ms,y:Number(eventDetails(event).info&&eventDetails(event).info.model_context_window)||0})):tokenSeries(session,key,"last_token_usage")})),domain);
    document.getElementById("series-toggles").innerHTML=[...TOKEN_KEYS,"model_context_window"].map(key=>`<label><input data-series="${key}" type="checkbox" ${state.enabled.has(key)?"checked":""}><span style="color:${COLORS[key]}">■</span>${esc(key)}</label>`).join("");
    for(const box of document.querySelectorAll("[data-series]")) box.onchange=event=>{if(event.target.checked)state.enabled.add(event.target.dataset.series);else state.enabled.delete(event.target.dataset.series);renderMetrics();};
  }

  function operation(event, tool) { if (tool) return spanLabel(tool); const d=eventDetails(event); if ((event.kind||"").includes("message")) return [d.author,d.recipient].filter(Boolean).join(" → ") || event.kind; return event.kind || event.payload_type || event.outer_type; }
  function rowsFor(session) {
    let cumulative=0; const cumulativeById=new Map(); for(const event of session.events || []) { cumulative+=payload(event); cumulativeById.set(event.event_id,cumulative); }
    const linkedOutput=new Set(); for(const span of toolSpans.filter(span=>span.session_id===session.session_id)) for(const id of span.event_ids.slice(1)) linkedOutput.add(id);
    return (session.events||[]).filter(event=>!linkedOutput.has(event.event_id)).map(event=>{const tool=toolByCall.get(event.event_id);const d=eventDetails(event), info=d.info||{}, last=info.last_token_usage||{}, total=info.total_token_usage||{};const endId=tool&&tool.end_event_id;return {event,tool,time:tool&&tool.end_ms||event.timestamp_ms,duration:tool?tool.duration_ms:event.duration&&event.duration.observed_ms,spanKind:tool?"tool":"event",eventKind:event.kind,op:operation(event,tool),inPayload:tool?tool.attributes.input_bytes:size(event,"input_size")||size(event,"arguments_size"),outPayload:tool?tool.attributes.output_bytes:size(event,"output_size"),cum:tool?cumulativeById.get(endId)||cumulativeById.get(event.event_id):cumulativeById.get(event.event_id),last,total};}).filter(row=>(!state.eventKind||row.eventKind===state.eventKind)&&matchText(`${row.op} ${row.eventKind}`));
  }
  function renderTable() { const session=selectedSession(); if(!session)return;const rows=rowsFor(session);document.getElementById("table-title").textContent=`Events · ${session.agent_path||"user"}`;document.getElementById("table-count").textContent=`${number(rows.length)} rows`;const cols=["Время","Длительность","Span kind","Event kind","Операция / участники","Payload input","Payload output","Payload cumulative","Last input","Last cached","Last cache write","Last output","Last reasoning","Total input","Total cached","Total cache write","Total output","Total reasoning"];const cell=(value,css="number")=>`<td class="${css}">${esc(value)}</td>`;const body=rows.map(row=>`<tr><td>${esc(shortTime(row.time))}</td>${cell(duration(row.duration),"")}${cell(row.spanKind,"")}${cell(row.eventKind,`kind-${row.eventKind}`)}${cell(row.op,"operation")}${cell(number(row.inPayload))}${cell(number(row.outPayload))}${cell(number(row.cum))}${cell(number(row.last.input_tokens))}${cell(number(row.last.cached_input_tokens))}${cell(number(row.last.cache_write_input_tokens))}${cell(number(row.last.output_tokens))}${cell(number(row.last.reasoning_output_tokens))}${cell(number(row.total.input_tokens))}${cell(number(row.total.cached_input_tokens))}${cell(number(row.total.cache_write_input_tokens))}${cell(number(row.total.output_tokens))}${cell(number(row.total.reasoning_output_tokens))}</tr>`).join("");document.getElementById("event-table").innerHTML=`<table><thead><tr>${cols.map(col=>`<th>${esc(col)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`; }
  function render() { renderTrace(); renderMetrics(); renderTable(); }
  fillControls(); renderHeader(); render(); window.addEventListener("resize", () => render());
})();
