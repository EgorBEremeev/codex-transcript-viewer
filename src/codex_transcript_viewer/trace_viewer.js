(() => {
  "use strict";
  const breakdown = JSON.parse(document.getElementById("breakdown-data").textContent);
  const analysis = JSON.parse(document.getElementById("spans-data").textContent);
  const COLORS = { input_tokens: "#246bce", cached_input_tokens: "#67a6ee", cache_write_input_tokens: "#8358c7", output_tokens: "#149174", reasoning_output_tokens: "#df7c2c", model_context_window: "#8e99aa" };
  const TOKEN_KEYS = ["input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"];
  const defaults = analysis.viewer_defaults || {};
  const state = { sessionId: breakdown.root_session_id, role: "", spanKind: "", eventKind: "", search: "", domain: null, untilMs: Number.isFinite(defaults.until_ms) ? defaults.until_ms : null, enabled: new Set([...TOKEN_KEYS, "model_context_window"]) };
  const includedEventIds = new Set(Object.keys(analysis.event_to_span || {}));
  const sessions = new Map((breakdown.sessions || []).map(session => ({...session, events: (session.events || []).filter(event => includedEventIds.has(event.event_id))})).filter(session => session.events.length).map(session => [session.session_id, session]));
  const events = new Map();
  for (const session of sessions.values()) for (const event of session.events || []) events.set(event.event_id, event);
  const spans = analysis.spans || [];
  const spansById = new Map(spans.map(span => [span.span_id, span]));
  const sessionSpans = new Map(spans.filter(span => span.kind === "session").map(span => [span.session_id, span]));
  const toolSpans = spans.filter(span => span.kind === "tool");
  const toolByCall = new Map(toolSpans.map(span => [span.start_event_id, span]));
  const reasoningSpans = spans.filter(span => span.kind === "reasoning");
  const reasoningByEnd = new Map(reasoningSpans.map(span => [span.end_event_id, span]));
  const allDated = [...events.values()].filter(event => Number.isFinite(event.timestamp_ms));
  const fullDomain = allDated.length ? [Math.min(...allDated.map(event => event.timestamp_ms)), Math.max(...allDated.map(event => event.timestamp_ms))] : [0, 1];
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
  const visibleAt = event => !Number.isFinite(event.timestamp_ms) || state.untilMs === null || event.timestamp_ms <= state.untilMs;
  const localInput = ms => Number.isFinite(ms) ? new Date(ms).getFullYear() + "-" + String(new Date(ms).getMonth() + 1).padStart(2, "0") + "-" + String(new Date(ms).getDate()).padStart(2, "0") + ":" + String(new Date(ms).getHours()).padStart(2, "0") + ":" + String(new Date(ms).getMinutes()).padStart(2, "0") + ":" + String(new Date(ms).getSeconds()).padStart(2, "0") : "";
  function parseLocalInput(value) { const match = /^(\d{4})-(\d{2})-(\d{2}):(\d{2}):(\d{2}):(\d{2})$/.exec(value.trim()); if (!match) return null; const parts = match.slice(1).map(Number); const date = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]); return date.getFullYear() === parts[0] && date.getMonth() === parts[1] - 1 && date.getDate() === parts[2] && date.getHours() === parts[3] && date.getMinutes() === parts[4] && date.getSeconds() === parts[5] ? date.getTime() : null; }

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
    const until = document.getElementById("until-filter");
    until.value = localInput(state.untilMs);
    document.getElementById("apply-until").addEventListener("click", () => { const value = parseLocalInput(until.value); if (value === null) { until.setCustomValidity("Используйте YYYY-MM-DD:HH:MM:SS"); until.reportValidity(); return; } until.setCustomValidity(""); state.untilMs = value; state.domain = null; render(); });
    document.getElementById("clear-until").addEventListener("click", () => { state.untilMs = null; state.domain = null; until.value = ""; render(); });
    document.getElementById("download-events-csv").addEventListener("click", downloadVisibleEventsCsv);
  }

  function selectedSession() { return sessions.get(state.sessionId) || sessions.get(breakdown.root_session_id) || [...sessions.values()][0]; }
  function activeDomain() { if (state.domain) return state.domain; const end = state.untilMs === null ? fullDomain[1] : Math.min(fullDomain[1], state.untilMs); return end >= fullDomain[0] ? [fullDomain[0], end] : [end - 1, end]; }
  function spanLabel(span) {
    if (span.kind !== "tool") return span.kind;
    const attrs = span.attributes || {};
    const nested = Array.isArray(attrs.nested_calls) ? attrs.nested_calls : [];
    const name = attrs.name || "tool";
    const calls = nested.map(call => [call && call.tool, call && (call.command_label || call.command_name)].filter(Boolean).join(" → ")).filter(Boolean);
    return calls.length ? [name, ...calls].join(" → ") : name;
  }
  function matchText(value) { return !state.search || String(value || "").toLocaleLowerCase().includes(state.search); }
  function visibleSessions() {
    return [...roles.entries()].filter(([role]) => !state.role || role === state.role).map(([role, list]) => [role, list.filter(session => !state.search || matchText(session.agent_path) || matchText(session.session_id))]).filter(([, list]) => list.length);
  }
  function xFor(value, width, domain) { return ((value - domain[0]) / Math.max(1, domain[1] - domain[0])) * width; }
  function isClipped(span) { return state.untilMs !== null && Number.isFinite(span.end_ms) && span.end_ms > state.untilMs && Number.isFinite(span.start_ms) && span.start_ms <= state.untilMs; }

  function renderHeader() {
    const task = spans.find(span => span.kind === "task");
    const cutoff = null;
    document.getElementById("trace-summary").textContent = `Root ${breakdown.root_session_id} · ${events.size} retained events${cutoff ? ` · since ${cutoff.local}` : ""} · source breakdown schema ${breakdown.schema_version} · span analysis ${analysis.analysis_version}`;
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
      if (sessionSpan && Number.isFinite(sessionSpan.start_ms) && (state.untilMs === null || sessionSpan.start_ms <= state.untilMs)) { const start=xFor(sessionSpan.start_ms,width,domain), end=xFor(state.untilMs === null ? (sessionSpan.end_ms || sessionSpan.start_ms) : Math.min(sessionSpan.end_ms || sessionSpan.start_ms, state.untilMs),width,domain); ctx.fillStyle="#a8c8fb"; ctx.fillRect(Math.max(0,start), y+10, Math.max(2,end-start), 8); if (isClipped(sessionSpan)) { ctx.strokeStyle="#d1762b"; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(end+.5,y+8); ctx.lineTo(end+.5,y+20); ctx.stroke(); } }
      const showTurns = !state.spanKind || state.spanKind === "turn";
      if (showTurns) for (const span of spans.filter(item => item.kind === "turn" && item.session_id === session.session_id)) {
        if (!Number.isFinite(span.start_ms) || (state.untilMs !== null && span.start_ms > state.untilMs)) continue; const start=xFor(span.start_ms,width,domain), end=xFor(state.untilMs === null ? (span.end_ms || span.start_ms) : Math.min(span.end_ms || span.start_ms, state.untilMs),width,domain); ctx.strokeStyle="#8e99aa"; ctx.lineWidth=1; ctx.strokeRect(Math.max(0,start)+.5, y+5.5, Math.max(3,end-start)-1, 17); if (isClipped(span)) { ctx.strokeStyle="#d1762b"; ctx.beginPath(); ctx.moveTo(end+.5,y+4); ctx.lineTo(end+.5,y+24); ctx.stroke(); }
      }
      const showTools = !state.spanKind || state.spanKind === "tool";
      if (showTools) for (const span of toolSpans.filter(item => item.session_id === session.session_id && matchText(spanLabel(item)))) {
        if (!Number.isFinite(span.start_ms) || (state.untilMs !== null && span.start_ms > state.untilMs)) continue; const start=xFor(span.start_ms,width,domain), end=xFor(state.untilMs === null ? (span.end_ms || span.start_ms) : Math.min(span.end_ms || span.start_ms, state.untilMs),width,domain); ctx.fillStyle="#3278de"; ctx.fillRect(Math.max(0,start), y+7, Math.max(3,end-start), 14); if (isClipped(span)) { ctx.strokeStyle="#f6c343"; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(end+.5,y+5); ctx.lineTo(end+.5,y+23); ctx.stroke(); }
      }
      const showReasoning = !state.spanKind || state.spanKind === "reasoning";
      if (showReasoning) for (const span of reasoningSpans.filter(item => item.session_id === session.session_id)) {
        if (!Number.isFinite(span.start_ms) || (state.untilMs !== null && span.start_ms > state.untilMs)) continue; const start=xFor(span.start_ms,width,domain), end=xFor(state.untilMs === null ? (span.end_ms || span.start_ms) : Math.min(span.end_ms || span.start_ms, state.untilMs),width,domain); ctx.fillStyle="#df7c2c"; ctx.fillRect(Math.max(0,start), y+11, Math.max(2,end-start), 6); if (isClipped(span)) { ctx.strokeStyle="#f6c343"; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(end+.5,y+8); ctx.lineTo(end+.5,y+20); ctx.stroke(); }
      }
      const markers = (session.events || []).filter(event => Number.isFinite(event.timestamp_ms) && visibleAt(event) && (!state.eventKind || event.kind === state.eventKind) && matchText(event.kind));
      for (const event of markers) { const x=xFor(event.timestamp_ms,width,domain); if (x < 0 || x > width) continue; ctx.fillStyle = event.kind === "token_count" ? "#8b53bf" : event.kind.includes("compacted") ? "#d1762b" : event.kind.includes("message") ? "#07846b" : "#44536b"; ctx.beginPath(); ctx.arc(x,y+14,2.5,0,Math.PI*2); ctx.fill(); }
    }
    let drag = null;
    canvas.onpointerdown = event => { drag = {x:event.offsetX, y:event.offsetY}; canvas.setPointerCapture(event.pointerId); };
    canvas.onpointerup = event => { if (!drag) return; const dx=event.offsetX-drag.x; if (Math.abs(dx)>8) { const a=Math.max(0,Math.min(width,drag.x)), b=Math.max(0,Math.min(width,event.offsetX)); const current=activeDomain(); state.domain=[current[0]+(current[1]-current[0])*Math.min(a,b)/width,current[0]+(current[1]-current[0])*Math.max(a,b)/width]; render(); } else { const row=Math.floor(drag.y/28); const target=rows[row]; if (target && target.session) { state.sessionId=target.session.session_id; document.getElementById("session-filter").value=state.sessionId; render(); } } drag=null; };
  }

  function tokenSeries(session, key, source) { return (session.events || []).filter(event => event.kind === "token_count" && Number.isFinite(event.timestamp_ms) && visibleAt(event)).map(event => ({x:event.timestamp_ms, y:Number(eventDetails(event).info && eventDetails(event).info[source] && eventDetails(event).info[source][key]) || 0})); }
  function drawChart(id, series, domain) {
    const canvas=document.getElementById(id), width=canvas.parentElement.clientWidth-18, height=canvas.clientHeight || 185, ctx=canvasSize(canvas,width,height); const visible=series.flatMap(item=>item.points).filter(point=>point.x>=domain[0]&&point.x<=domain[1]); const max=Math.max(1,...visible.map(point=>point.y)); ctx.clearRect(0,0,width,height); ctx.strokeStyle="#dfe6f0"; for(let i=0;i<4;i++){const y=18+(height-36)*i/3;ctx.beginPath();ctx.moveTo(35,y);ctx.lineTo(width-4,y);ctx.stroke();} ctx.fillStyle="#637188";ctx.font="10px Segoe UI";ctx.fillText(number(max),2,18);ctx.fillText("0",15,height-17);
    for (const item of series) { if ((!item.alwaysVisible && !state.enabled.has(item.key)) || !item.points.length) continue; ctx.strokeStyle=item.color;ctx.lineWidth=1.7;ctx.beginPath();let moved=false; for(const point of item.points){if(point.x<domain[0]||point.x>domain[1])continue;const x=35+xFor(point.x,width-39,domain),y=height-18-(point.y/max)*(height-36);if(!moved){ctx.moveTo(x,y);moved=true;}else ctx.lineTo(x,y);}ctx.stroke(); }
  }

  function renderMetrics() {
    const session=selectedSession(); if (!session) return; document.getElementById("metrics-title").textContent=`Кумулятивные метрики · ${session.agent_path || "user"}`; const domain=activeDomain(); let cumulative=0; const payloadPoints=[];
    for(const event of session.events || []) { if(!Number.isFinite(event.timestamp_ms) || !visibleAt(event))continue; cumulative+=payload(event); payloadPoints.push({x:event.timestamp_ms,y:cumulative}); }
    drawChart("payload-chart", [{key:"payload",color:"#1f6feb",points:payloadPoints,alwaysVisible:true}],domain);
    drawChart("total-chart", TOKEN_KEYS.map(key=>({key,color:COLORS[key],points:tokenSeries(session,key,"total_token_usage")})),domain);
    drawChart("last-chart", [...TOKEN_KEYS,"model_context_window"].map(key=>({key,color:COLORS[key],points:key==="model_context_window"?(session.events||[]).filter(event=>event.kind==="token_count"&&Number.isFinite(event.timestamp_ms)&&visibleAt(event)).map(event=>({x:event.timestamp_ms,y:Number(eventDetails(event).info&&eventDetails(event).info.model_context_window)||0})):tokenSeries(session,key,"last_token_usage")})),domain);
    document.getElementById("series-toggles").innerHTML=[...TOKEN_KEYS,"model_context_window"].map(key=>`<label><input data-series="${key}" type="checkbox" ${state.enabled.has(key)?"checked":""}><span style="color:${COLORS[key]}">■</span>${esc(key)}</label>`).join("");
    for(const box of document.querySelectorAll("[data-series]")) box.onchange=event=>{if(event.target.checked)state.enabled.add(event.target.dataset.series);else state.enabled.delete(event.target.dataset.series);renderMetrics();};
  }

  function operation(event, tool) { if (tool) return spanLabel(tool); const d=eventDetails(event); if (event.kind === "sub_agent_activity") return [d.agent_path,d.kind].filter(Boolean).join(" · ") || event.kind; if ((event.kind||"").includes("message")) return [d.author,d.recipient].filter(Boolean).join(" → ") || event.kind; return event.kind || event.payload_type || event.outer_type; }
  function rowsFor(session) {
    let cumulative=0; const cumulativeById=new Map(); for(const event of session.events || []) { if (!visibleAt(event)) continue; cumulative+=payload(event); cumulativeById.set(event.event_id,cumulative); }
    const linkedOutput=new Set(); for(const span of toolSpans.filter(span=>span.session_id===session.session_id)) for(const id of span.event_ids.slice(1)) linkedOutput.add(id);
    return (session.events||[]).filter(event=>visibleAt(event)&&!linkedOutput.has(event.event_id)).map(event=>{const tool=toolByCall.get(event.event_id), reasoning=reasoningByEnd.get(event.event_id), span=tool||reasoning;const d=eventDetails(event), info=d.info||{}, last=info.last_token_usage||{}, total=info.total_token_usage||{};const endId=span&&span.end_event_id;const clipped=span&&isClipped(span);const end=span ? (state.untilMs === null ? span.end_ms : Math.min(span.end_ms || span.start_ms, state.untilMs)) : event.timestamp_ms;const spanDuration=span&&Number.isFinite(span.start_ms)&&Number.isFinite(end)?Math.max(0,end-span.start_ms):span&&span.duration_ms;return {event,tool,time:end||event.timestamp_ms,duration:Number.isFinite(spanDuration)?spanDuration:event.duration&&event.duration.observed_ms,spanKind:tool?"tool":reasoning?"reasoning":"event",eventKind:event.kind,op:operation(event,tool),inPayload:tool?tool.attributes.input_bytes:size(event,"input_size")||size(event,"arguments_size"),outPayload:tool&&!clipped?tool.attributes.output_bytes:size(event,"output_size"),cum:tool&&clipped?cumulativeById.get(event.event_id):tool?cumulativeById.get(endId)||cumulativeById.get(event.event_id):cumulativeById.get(event.event_id),last,total};}).filter(row=>(!state.eventKind||row.eventKind===state.eventKind)&&(!state.spanKind||row.spanKind===state.spanKind)&&matchText(`${row.op} ${row.eventKind}`));
  }
  function renderTable() { const session=selectedSession(); if(!session)return;const rows=rowsFor(session);document.getElementById("table-title").textContent=`Events · ${session.agent_path||"user"}`;document.getElementById("table-count").textContent=`${number(rows.length)} rows`;const cols=["Время","Длительность","Span kind","Event kind","Операция / участники","Payload input","Payload output","Payload cumulative","Last input","Last cached","Last cache write","Last output","Last reasoning","Total input","Total cached","Total cache write","Total output","Total reasoning"];const cell=(value,css="number")=>`<td class="${css}">${esc(value)}</td>`;const body=rows.map(row=>`<tr><td>${esc(shortTime(row.time))}</td>${cell(duration(row.duration),"")}${cell(row.spanKind,"")}${cell(row.eventKind,`kind-${row.eventKind}`)}${cell(row.op,"operation")}${cell(number(row.inPayload))}${cell(number(row.outPayload))}${cell(number(row.cum))}${cell(number(row.last.input_tokens))}${cell(number(row.last.cached_input_tokens))}${cell(number(row.last.cache_write_input_tokens))}${cell(number(row.last.output_tokens))}${cell(number(row.last.reasoning_output_tokens))}${cell(number(row.total.input_tokens))}${cell(number(row.total.cached_input_tokens))}${cell(number(row.total.cache_write_input_tokens))}${cell(number(row.total.output_tokens))}${cell(number(row.total.reasoning_output_tokens))}</tr>`).join("");document.getElementById("event-table").innerHTML=`<table><thead><tr>${cols.map(col=>`<th>${esc(col)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`; }
  function csvCell(value) { const text=String(value == null ? "" : value); return /[",\r\n]/.test(text) ? `"${text.replace(/"/g,"\"\"")}"` : text; }
  function downloadVisibleEventsCsv() { const session=selectedSession(); if(!session)return; const rows=rowsFor(session); const headers=["session_id","agent_path","timestamp","duration_ms","span_kind","event_kind","operation","payload_input_bytes","payload_output_bytes","payload_cumulative_bytes","last_input_tokens","last_cached_input_tokens","last_cache_write_input_tokens","last_output_tokens","last_reasoning_output_tokens","total_input_tokens","total_cached_input_tokens","total_cache_write_input_tokens","total_output_tokens","total_reasoning_output_tokens"]; const lines=[headers.join(",")]; for(const row of rows){ lines.push([session.session_id||"",session.agent_path||"",row.event.timestamp||"",row.duration,row.spanKind,row.eventKind,row.op,row.inPayload,row.outPayload,row.cum,row.last.input_tokens,row.last.cached_input_tokens,row.last.cache_write_input_tokens,row.last.output_tokens,row.last.reasoning_output_tokens,row.total.input_tokens,row.total.cached_input_tokens,row.total.cache_write_input_tokens,row.total.output_tokens,row.total.reasoning_output_tokens].map(csvCell).join(",")); } const blob=new Blob(["\ufeff"+lines.join("\r\n")+"\r\n"],{type:"text/csv;charset=utf-8"}); const link=document.createElement("a"); link.href=URL.createObjectURL(blob); link.download=`${(session.agent_path||session.session_id||"events").replace(/[^A-Za-z0-9._-]/g,"_")}-events.csv`; link.click(); URL.revokeObjectURL(link.href); }
  function render() { renderTrace(); renderMetrics(); renderTable(); }
  fillControls(); renderHeader(); render(); window.addEventListener("resize", () => render());
})();
