const $ = selector => document.querySelector(selector)
const colors = ['#3b82f6', '#8b5cf6', '#d97706', '#059669', '#db2777', '#0891b2']
const zoomLevels = [1, 8, 16, 32, 64]
const state = { summaries: [], jobs: [], seenCompleted: new Set(), transcript: null, selected: null, correction: null, dirty: false, buffers: [], audio: [], duration: 0, zoom: 1, raf: null }

const api = async (path, options) => {
  const response = await fetch(path, options)
  const value = await response.json()
  if (!response.ok) throw new Error(value.error || response.statusText)
  return value
}

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]))
const clock = seconds => {
  const value = Math.max(0, Math.floor(seconds || 0))
  return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map(item => String(item).padStart(2, '0')).join(':')
}
const speakerColor = speaker => colors[Math.abs([...speaker].reduce((sum, character) => sum + character.charCodeAt(0), 0)) % colors.length]
const toast = message => { $('#toast').textContent = message; $('#toast').classList.add('show'); setTimeout(() => $('#toast').classList.remove('show'), 1800) }
const setSaveState = (text, kind = '') => { $('#save-state').textContent = text; $('#save-state').className = `save-state ${kind}` }

async function loadSummaries() {
  state.summaries = await api('/api/transcripts')
  await refreshJobs()
  updateTranscriptCount()
  renderSummaries()
  if (state.summaries[0]) await selectTranscript(state.summaries[0].id)
}

function renderSummaries() {
  const query = $('#search').value.toLowerCase()
  const activeJobs = new Map(state.jobs.filter(job => ['queued','running'].includes(job.status)).map(job => [job.transcriptId, job]))
  const latestJobs = new Map(state.jobs.map(job => [job.transcriptId, job]))
  $('#transcript-list').innerHTML = state.summaries.filter(item => `${item.title} ${item.name}`.toLowerCase().includes(query)).map(item => {
    const job = activeJobs.get(item.id) || latestJobs.get(item.id)
    const active = job && ['queued','running'].includes(job.status)
    const jobLine = job ? `<div class="job-line ${job.status}"><span>${job.status === 'queued' ? `Queue ${job.position}` : escapeHtml(job.stage)}</span><span>${job.status === 'running' ? `${Math.round(job.progress * 100)}%` : job.status}</span></div>${active ? `<div class="job-progress"><i style="width:${Math.max(2, job.progress * 100)}%"></i></div>` : ''}` : ''
    const action = active ? '' : `<button class="queue-action" data-id="${item.id}" data-status="${item.status}">${job?.status === 'failed' ? 'Retry' : item.status === 'ready' ? 'Re-run' : 'Queue'}</button>`
    return `<div class="transcript-card ${state.transcript?.id === item.id ? 'selected' : ''}" data-id="${item.id}" role="button" tabindex="0">
      <div class="transcript-card-top"><strong>${escapeHtml(item.title)} <em class="status ${item.status}">${item.status}</em></strong>${action}</div>
      <span>${escapeHtml(item.startedAt || item.name)}${item.status === 'ready' ? ` · ${item.turnCount} turns` : ''}</span>${jobLine}
    </div>`
  }).join('')
  document.querySelectorAll('.transcript-card').forEach(card => {
    card.onclick = event => { if (!event.target.closest('.queue-action')) selectTranscript(card.dataset.id) }
    card.onkeydown = event => { if (event.key === 'Enter') selectTranscript(card.dataset.id) }
  })
  document.querySelectorAll('.queue-action').forEach(button => button.onclick = event => requestQueue(button.dataset.id, button.dataset.status, event.currentTarget, event))
}

function updateTranscriptCount() {
  const active = state.jobs.filter(job => ['queued','running'].includes(job.status)).length
  $('#transcript-count').textContent = active ? `${state.summaries.length} · ${active} queued` : state.summaries.length
}

async function refreshJobs() {
  try {
    const jobs = await api('/api/jobs')
    const newlyCompleted = jobs.filter(job => job.status === 'complete' && !state.seenCompleted.has(job.id))
    jobs.filter(job => job.status === 'complete').forEach(job => state.seenCompleted.add(job.id))
    state.jobs = jobs
    if (newlyCompleted.length) state.summaries = await api('/api/transcripts')
    updateTranscriptCount(); renderSummaries()
  } catch { }
}

async function selectTranscript(id) {
  pause()
  state.transcript = await api(`/api/transcripts/${id}`)
  state.selected = state.transcript.turns[0] || null
  state.correction = null
  state.dirty = false
  seedAttendees()
  state.duration = state.transcript.turns.at(-1)?.end || 0
  $('#meeting-title').textContent = state.transcript.title
  $('#meeting-meta').textContent = state.transcript.editable ? `${state.transcript.name} · ${state.transcript.turns.length} turns · ${state.transcript.status}` : `${state.transcript.name} · ${state.transcript.status}`
  setSaveState(state.transcript.status === 'stale' && state.transcript.staleReason === 'hints' ? 'Hints changed · regenerate' : state.transcript.editable ? (state.transcript.hintRevision ? 'Hints loaded' : 'No hint file') : 'Needs processing')
  renderSummaries(); renderTranscript(); renderInspector(); await loadAudio(); drawWaveform()
}

function seedAttendees() {
  const hints = state.transcript.hints
  const existing = new Set((hints.attendees || []).map(item => typeof item === 'string' ? item : item.identity).filter(Boolean))
  for (const item of state.transcript.frontmatter.attendees || []) if (item?.identity) existing.add(item.identity)
  hints.attendees = [...existing]
  hints.hotwords ||= []; hints.speakers ||= []; hints.edits ||= []
}

function renderTranscript() {
  if (!state.transcript.editable) {
    $('#transcript').innerHTML = `<div class="empty recording-empty"><strong>${state.transcript.status === 'stale' ? 'Stale transcript' : 'No transcript yet'}</strong><span>${state.transcript.status === 'stale' ? 'This Markdown was produced by an unsupported speech2md schema.' : 'This recording has not been processed by speech2md.'}</span><button class="primary regenerate-inline">${state.transcript.status === 'stale' ? 'Regenerate transcript' : 'Generate transcript'}</button></div>`
    $('.regenerate-inline').onclick = event => requestQueue(state.transcript.id, state.transcript.status, event.currentTarget, event)
    return
  }
  $('#transcript').innerHTML = state.transcript.turns.map(turn => {
    const color = speakerColor(turn.speaker)
    const assignment = speakerAssignment(turn)
    const proposal = assignment.changed
      ? `<span class="proposed-speaker" title="Proposed speaker reassignment">→ ${escapeHtml(assignment.proposed)}</span>`
      : ''
    return `<article class="turn ${state.selected?.index === turn.index ? 'selected' : ''}" data-index="${turn.index}" style="--speaker-color:${color}">
      <span class="turn-time">${clock(turn.start)}</span><span class="speaker-dot"></span>
      <div class="turn-body"><div class="turn-speaker"><span>${escapeHtml(assignment.current)}</span>${proposal}</div><div class="turn-text">${escapeHtml(turn.text)}</div></div>
    </article>`
  }).join('')
  document.querySelectorAll('.turn').forEach(element => {
    element.onclick = event => {
      if (window.getSelection()?.toString().trim()) return
      selectTurn(Number(element.dataset.index), true)
    }
    element.onmouseup = () => captureCorrection(element)
  })
}

function selectTurn(index, seek = false) {
  state.selected = state.transcript.turns[index]
  state.correction = null
  document.querySelectorAll('.turn').forEach((element, item) => element.classList.toggle('selected', item === index))
  document.querySelector(`.turn[data-index="${index}"]`)?.scrollIntoView({block:'center', behavior:'smooth'})
  if (seek) seekTo(state.selected.start)
  renderInspector(); drawWaveform()
}

function captureCorrection(element) {
  const selection = window.getSelection()
  const before = selection?.toString().trim()
  if (!before || !element.querySelector('.turn-text')?.contains(selection.anchorNode)) return
  const turn = state.transcript.turns[Number(element.dataset.index)]
  if (!turn.text.includes(before)) return
  state.selected = turn
  state.correction = {before, after: before, hotword: true}
  renderTranscript(); renderInspector()
}

function proposedIdentity(turn) {
  for (const speaker of state.transcript.hints.speakers) {
    if ((speaker.ranges || []).some(range => overlaps(turn, range))) return speaker.identity
  }
  return ''
}

function renderedIdentity(turn) {
  return (state.transcript.frontmatter.attendees || []).find(item => item.handle === turn.speaker)?.identity || ''
}

function speakerAssignment(turn) {
  const rendered = renderedIdentity(turn)
  const proposed = proposedIdentity(turn)
  const current = rendered || turn.speaker
  return {current, proposed, changed: Boolean(proposed && proposed !== current)}
}

function assignedIdentity(turn) {
  return proposedIdentity(turn) || renderedIdentity(turn)
}
const overlaps = (left, right) => Math.min(left.end, right.end) > Math.max(left.start, right.start)

async function renderInspector() {
  if (!state.transcript.editable) {
    $('#inspector').innerHTML = `<div class="eyebrow">${state.transcript.status.toUpperCase()} RECORDING</div><h2>${escapeHtml(state.transcript.title)}</h2><div class="subhead">${escapeHtml(state.transcript.audio.map(item => item.name).join(', ') || 'No audio source found')}</div><div class="notice">↻ <span>${state.transcript.status === 'stale' ? 'The existing Markdown is left untouched until you explicitly regenerate it with the current speech2md.' : 'Generate current Markdown and voiceprints from this recording.'}</span></div>${metadataEditorHtml()}<button class="primary regenerate-inspector">${state.transcript.status === 'stale' ? 'Regenerate with speech2md' : 'Generate with speech2md'}</button>`
    $('.regenerate-inspector').onclick = event => requestQueue(state.transcript.id, state.transcript.status, event.currentTarget, event)
    bindMetadataEditor()
    return
  }
  if (!state.selected) return
  if (state.correction) return renderCorrectionInspector()
  const turn = state.selected
  const identity = assignedIdentity(turn)
  const assignment = speakerAssignment(turn)
  const track = turn.track || state.transcript.audio[0]?.role || 'inferred after audio loads'
  $('#inspector').innerHTML = `
    <div class="eyebrow">SPEAKER TURN</div><h2>${clock(turn.start)} · ${escapeHtml(turn.speaker)}</h2><div class="subhead">${escapeHtml(track)} track</div>
    ${assignment.changed ? `<div class="assignment-notice"><span>${escapeHtml(assignment.current)}</span><strong>→ ${escapeHtml(assignment.proposed)}</strong><small>proposed after regeneration</small></div>` : ''}
    <div class="notice">◌ <span>Turn end is inferred from the next turn. Track is chosen by the strongest audio activity in this interval.</span></div>
    <div class="section"><label class="section-label">SPEAKER IDENTITY</label><input id="identity" class="field" value="${escapeHtml(identity)}" placeholder="Name or identity URI"></div>
    <div class="section"><span class="section-label">VOICEPRINT MATCHES</span><div id="candidates"><div class="empty">Comparing voiceprints…</div></div></div>
    <button id="assign" class="primary">Assign identity</button>
    <div class="section"><span class="section-label">ATTENDEES</span><div id="attendees"></div><div class="add-row"><input id="new-attendee" class="field" placeholder="Add attendee"><button id="add-attendee">+</button></div></div>
    ${metadataEditorHtml()}`
  $('#assign').onclick = () => assignIdentity($('#identity').value.trim())
  $('#add-attendee').onclick = addAttendee
  renderAttendees()
  bindMetadataEditor()
  try {
    const candidates = await api(`/api/transcripts/${state.transcript.id}/candidates/${encodeURIComponent(turn.speaker)}`)
    if (state.selected !== turn || state.correction) return
    $('#candidates').innerHTML = candidates.length ? candidates.slice(0, 6).map(candidate => `
      <button class="candidate" data-identity="${escapeHtml(candidate.identity)}"><strong>${escapeHtml(candidate.identity)}</strong><span class="score">${candidate.similarity.toFixed(2)}</span><small>${escapeHtml(candidate.source)}</small></button>`).join('') : '<div class="empty">No identified voiceprints in this folder yet.</div>'
    document.querySelectorAll('.candidate').forEach(button => button.onclick = () => { $('#identity').value = button.dataset.identity })
  } catch (error) { $('#candidates').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>` }
}

function renderAttendees() {
  const container = $('#attendees'); if (!container) return
  container.innerHTML = state.transcript.hints.attendees.map(item => {
    const identity = typeof item === 'string' ? item : item.identity
    const assigned = state.transcript.hints.speakers.some(speaker => speaker.identity === identity)
    return `<div class="attendee"><span class="avatar">${escapeHtml(identity.slice(0,2).toUpperCase())}</span><span class="attendee-name">${escapeHtml(identity)}</span><small>${assigned ? 'has turns' : 'no turns'}</small><button class="remove" data-identity="${escapeHtml(identity)}" ${assigned ? 'title="Remove speaker assignments first"' : ''}>×</button></div>`
  }).join('')
  container.querySelectorAll('.remove').forEach(button => button.onclick = async () => {
    if (state.transcript.hints.speakers.some(speaker => speaker.identity === button.dataset.identity)) return toast('Remove speaker assignments first')
    state.transcript.hints.attendees = state.transcript.hints.attendees.filter(item => (typeof item === 'string' ? item : item.identity) !== button.dataset.identity)
    markDirty(); renderAttendees(); await saveHints()
  })
}

function metadataValues() {
  const hints = state.transcript.hints, frontmatter = state.transcript.frontmatter || {}
  return {
    title: hints.title || state.transcript.title || '',
    started_at: hints.started_at || frontmatter.started_at || '',
    ended_at: hints.ended_at || frontmatter.ended_at || '',
    calendar_event: hints.calendar_event || frontmatter.calendar_event || '',
  }
}

function metadataEditorHtml() {
  const metadata = metadataValues()
  return `<div class="section metadata-editor"><span class="section-label">MEETING METADATA</span>
    <label>Title<input id="metadata-title" class="field" value="${escapeHtml(metadata.title)}" placeholder="Meeting title"></label>
    <label>Recording started<input id="metadata-started" class="field mono-field" value="${escapeHtml(metadata.started_at)}" placeholder="2026-08-04T09:00:00+02:00"></label>
    <label>Recording ended<input id="metadata-ended" class="field mono-field" value="${escapeHtml(metadata.ended_at)}" placeholder="2026-08-04T10:00:00+02:00"></label>
    <label>Calendar event link<input id="metadata-event" class="field mono-field" value="${escapeHtml(metadata.calendar_event)}" placeholder="https://…"></label>
    <button id="save-metadata" class="secondary metadata-save">Save metadata hints</button>
  </div>`
}

function bindMetadataEditor() {
  const button = $('#save-metadata'); if (!button) return
  button.onclick = async () => {
    const metadata = {}
    for (const [key, selector] of Object.entries({title:'#metadata-title', started_at:'#metadata-started', ended_at:'#metadata-ended', calendar_event:'#metadata-event'})) {
      const value = $(selector).value.trim(); if (value) metadata[key] = value
    }
    for (const key of ['title','started_at','ended_at','calendar_event']) state.transcript.hints[key] = metadata[key] || null
    if (metadata.title) { state.transcript.title = metadata.title; $('#meeting-title').textContent = metadata.title }
    markDirty(); await saveHints()
  }
}

async function addAttendee() {
  const input = $('#new-attendee'); const identity = input.value.trim(); if (!identity) return
  if (!state.transcript.hints.attendees.some(item => (typeof item === 'string' ? item : item.identity) === identity)) state.transcript.hints.attendees.push(identity)
  input.value = ''; markDirty(); renderAttendees(); await saveHints()
}

async function assignIdentity(identity) {
  if (!identity) return toast('Enter an identity first')
  const turn = state.selected; const track = turn.track || state.transcript.audio[0]?.role
  for (const speaker of state.transcript.hints.speakers) speaker.ranges = (speaker.ranges || []).filter(range => !sameTrack(range.track, track) || !overlaps(range, turn))
  state.transcript.hints.speakers = state.transcript.hints.speakers.filter(speaker => speaker.ranges.length)
  let speaker = state.transcript.hints.speakers.find(item => item.identity === identity)
  if (!speaker) { speaker = {identity, ranges: []}; state.transcript.hints.speakers.push(speaker) }
  speaker.ranges.push({...(state.transcript.audio.length > 1 ? {track} : {}), start: turn.start, end: turn.end})
  if (!state.transcript.hints.attendees.some(item => (typeof item === 'string' ? item : item.identity) === identity)) state.transcript.hints.attendees.push(identity)
  markDirty(); await saveHints(); renderInspector(); renderTranscript(); drawWaveform()
}
const sameTrack = (left, right) => !left || !right || left === right

function renderCorrectionInspector() {
  const turn = state.selected; const correction = state.correction
  $('#inspector').innerHTML = `
    <div class="eyebrow">TRANSCRIPT CORRECTION</div><h2>${clock(turn.start)} · ${escapeHtml(turn.speaker)}</h2><div class="subhead">${escapeHtml(turn.track || state.transcript.audio[0]?.role || 'mixed')} track</div>
    <div class="section"><span class="section-label">SELECTED TEXT</span><div class="selected-phrase">${escapeHtml(correction.before)}</div></div>
    <div class="section"><label class="section-label">REPLACE WITH</label><input id="replacement" class="field" value="${escapeHtml(correction.after)}"></div>
    <div class="section checkbox"><input id="add-hotword" type="checkbox" ${correction.hotword ? 'checked' : ''}><label for="add-hotword"><strong>Add replacement as a hotword</strong><br><small>Helps future transcriptions recognize this term.</small></label></div>
    <div class="hint-note">Saves a localized edit to the adjacent <code>.hint.yaml</code> only. Transcript Markdown changes only after explicit regeneration.</div>
    <button id="save-correction" class="primary">Save correction</button>
    <button id="cancel-correction" class="secondary" style="width:100%;margin-top:8px">Cancel</button>`
  $('#replacement').select()
  $('#save-correction').onclick = async () => {
    const after = $('#replacement').value.trim(); if (!after) return toast('Replacement cannot be empty')
    const edit = {start: turn.start, end: turn.end, before: correction.before, after}
    if (state.transcript.audio.length > 1 && turn.track) edit.track = turn.track
    state.transcript.hints.edits.push(edit)
    if ($('#add-hotword').checked && !state.transcript.hints.hotwords.some(value => value.toLowerCase() === after.toLowerCase())) state.transcript.hints.hotwords.push(after)
    markDirty(); await saveHints(); state.correction = null; renderInspector()
  }
  $('#cancel-correction').onclick = () => { state.correction = null; renderInspector() }
}

function markDirty() { state.dirty = true; setSaveState('Unsaved hints', 'dirty') }
async function saveHints() {
  try {
    setSaveState('Saving…')
    const result = await api(`/api/transcripts/${state.transcript.id}/hints`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:state.transcript.hintRevision, hints:state.transcript.hints})})
    state.transcript.hintRevision = result.revision; state.dirty = false
    state.summaries = await api('/api/transcripts')
    const summary = state.summaries.find(item => item.id === state.transcript.id)
    if (summary) state.transcript.status = summary.status
    $('#meeting-meta').textContent = state.transcript.editable ? `${state.transcript.name} · ${state.transcript.turns.length} turns · ${state.transcript.status}` : `${state.transcript.name} · ${state.transcript.status}`
    setSaveState(state.transcript.status === 'stale' ? 'Saved · regenerate' : 'Saved to hints', 'saved'); renderSummaries(); toast('Hint file saved')
  } catch (error) { setSaveState('Save failed', 'dirty'); toast(error.message) }
}

async function loadAudio() {
  for (const audio of state.audio) { audio.pause(); audio.remove() }
  state.audio = []; state.buffers = []
  if (!state.transcript.audio.length) { inferTracks(); drawWaveform(); return }
  state.transcript.audio.forEach(source => {
    const url = `/api/transcripts/${state.transcript.id}/audio/${source.index}`
    const element = new Audio(url); element.preload = 'metadata'; state.audio[source.index] = element
    element.onloadedmetadata = () => { state.duration = Math.max(state.duration, element.duration || 0); updateClock(); drawWaveform() }
  })
  if (state.transcript.status !== 'ready') { drawWaveform(); return }
  const context = new AudioContext()
  await Promise.all(state.transcript.audio.map(async source => {
    const url = `/api/transcripts/${state.transcript.id}/audio/${source.index}`
    try { state.buffers[source.index] = await context.decodeAudioData(await (await fetch(url)).arrayBuffer()) } catch { state.buffers[source.index] = null }
  }))
  inferTracks(); drawWaveform()
}

function inferTracks() {
  if (state.transcript.audio.length <= 1) { for (const turn of state.transcript.turns) turn.track = state.transcript.audio[0]?.role || 'mixed'; return }
  for (const turn of state.transcript.turns) {
    const hinted = state.transcript.hints.speakers.flatMap(item => item.ranges || []).find(range => range.track && overlaps(range, turn))
    if (hinted) { turn.track = hinted.track; continue }
    let best = -1; let bestEnergy = -1
    state.buffers.forEach((buffer, index) => {
      if (!buffer) return
      const data = buffer.getChannelData(0), from = Math.floor(turn.start * buffer.sampleRate), to = Math.min(data.length, Math.ceil(turn.end * buffer.sampleRate)); let energy = 0, count = 0
      const step = Math.max(1, Math.floor((to - from) / 3000))
      for (let sample = from; sample < to; sample += step) { energy += data[sample] * data[sample]; count++ }
      energy = count ? energy / count : 0
      if (energy > bestEnergy) { bestEnergy = energy; best = index }
    })
    turn.track = state.transcript.audio[best]?.role || state.transcript.audio[0]?.role || 'mixed'
  }
  renderTranscript(); renderInspector()
}

function play() {
  if (!state.audio.length) return
  state.audio.forEach(audio => { audio.currentTime = state.audio[0].currentTime; audio.play().catch(() => {}) })
  $('#play').textContent = '❚❚'; tick()
}
function pause() { state.audio.forEach(audio => audio.pause()); cancelAnimationFrame(state.raf); $('#play').textContent = '▶' }
function seekTo(seconds) { state.audio.forEach(audio => { audio.currentTime = Math.min(seconds, audio.duration || seconds) }); updateClock(); drawWaveform() }
function tick() { updateClock(); drawWaveform(); if (!state.audio[0]?.paused) state.raf = requestAnimationFrame(tick); else pause() }
function updateClock() { $('#current-time').textContent = clock(state.audio[0]?.currentTime || 0); $('#duration').textContent = `/ ${clock(state.duration)}` }

function visibleRange() {
  const duration = Math.max(1, state.duration), windowSize = duration / state.zoom, current = state.audio[0]?.currentTime || state.selected?.start || 0
  const start = state.zoom === 1 ? 0 : Math.max(0, Math.min(duration - windowSize, current - windowSize / 2))
  return [start, start + windowSize]
}

function drawWaveform() {
  const canvas = $('#waveform'), ratio = devicePixelRatio || 1, bounds = canvas.getBoundingClientRect()
  canvas.width = Math.max(1, bounds.width * ratio); canvas.height = Math.max(1, bounds.height * ratio)
  const context = canvas.getContext('2d'); context.scale(ratio, ratio); const width = bounds.width, height = bounds.height
  context.clearRect(0, 0, width, height); const lanes = Math.max(1, state.transcript?.audio.length || 1), laneHeight = height / lanes, [start, end] = visibleRange()
  for (let lane = 0; lane < lanes; lane++) {
    const y = lane * laneHeight; context.fillStyle = lane % 2 ? '#fbfbf8' : '#f7f7f4'; context.fillRect(0, y, width, laneHeight - 2)
    context.fillStyle = '#777'; context.font = '9px Geist Mono'; context.fillText((state.transcript.audio[lane]?.role || 'MIXED').toUpperCase(), 8, y + 13)
    const buffer = state.buffers[lane]
    if (buffer) {
      const data = buffer.getChannelData(0); context.fillStyle = '#c7c7c2'
      for (let x = 0; x < width; x += 2) {
        const a = Math.floor((start + x / width * (end - start)) * buffer.sampleRate), b = Math.min(data.length, Math.floor((start + (x + 2) / width * (end - start)) * buffer.sampleRate)); let peak = 0
        const step = Math.max(1, Math.floor((b - a) / 20)); for (let sample = a; sample < b; sample += step) peak = Math.max(peak, Math.abs(data[sample] || 0))
        const bar = Math.max(1, peak * (laneHeight - 22)); context.fillRect(x, y + laneHeight / 2 - bar / 2 + 5, 1, bar)
      }
    }
  }
  for (const turn of state.transcript?.turns || []) {
    if (turn.end <= start || turn.start >= end) continue
    const lane = Math.max(0, state.transcript.audio.findIndex(source => source.role === turn.track)), y = lane * laneHeight
    const x = (turn.start - start) / (end - start) * width, right = (turn.end - start) / (end - start) * width
    context.fillStyle = speakerColor(turn.speaker) + (state.selected?.index === turn.index ? 'cc' : '50'); context.fillRect(x, y + laneHeight - 8, Math.max(1, right - x), 6)
  }
  const current = state.audio[0]?.currentTime || 0
  if (current >= start && current <= end) { const x = (current - start) / (end - start) * width; context.fillStyle = '#0066ff'; context.fillRect(x, 0, 1.5, height) }
  renderTimeline(start, end)
}

function renderTimeline(start, end) { $('#timeline').innerHTML = Array.from({length:6}, (_, index) => `<span>${clock(start + (end - start) * index / 5).slice(3)}</span>`).join('') }

function waveformClick(event) {
  const bounds = event.currentTarget.getBoundingClientRect(), [start, end] = visibleRange(), time = start + (event.clientX - bounds.left) / bounds.width * (end - start)
  const lane = Math.floor((event.clientY - bounds.top) / bounds.height * Math.max(1, state.transcript.audio.length)), role = state.transcript.audio[lane]?.role
  const containing = state.transcript.turns.filter(turn => turn.start <= time && turn.end > time && (!role || turn.track === role)).sort((a,b) => b.start - a.start)
  const turn = containing[0] || state.transcript.turns.reduce((best, item) => Math.abs(item.start-time) < Math.abs(best.start-time) ? item : best)
  seekTo(time); selectTurn(turn.index, false)
}

function requestQueue(transcriptId, status, anchor, event) {
  event?.stopPropagation()
  if (status === 'unprocessed') return enqueueRecording(transcriptId)
  showQueuePopover(anchor, transcriptId)
}

async function enqueueRecording(transcriptId) {
  try {
    await api(`/api/transcripts/${transcriptId}/regenerate`, {method:'POST'})
    toast('Recording added to queue'); await refreshJobs()
  } catch (error) { toast(error.message) }
}

function showQueuePopover(anchor, transcriptId) {
  const popover = $('#action-popover')
  popover.hidden = false
  popover.dataset.anchorId = transcriptId
  const anchorBounds = anchor.getBoundingClientRect(), width = 292
  let left = Math.max(12, Math.min(innerWidth - width - 12, anchorBounds.right - width))
  let top = anchorBounds.bottom + 8
  const height = popover.getBoundingClientRect().height
  if (top + height > innerHeight - 12) top = Math.max(12, anchorBounds.top - height - 8)
  popover.style.left = `${left}px`; popover.style.top = `${top}px`
  $('#popover-confirm').onclick = async () => { closeQueuePopover(); await enqueueRecording(transcriptId) }
  $('#popover-cancel').onclick = closeQueuePopover
  $('#popover-confirm').focus()
}

function closeQueuePopover() {
  const popover = $('#action-popover')
  popover.hidden = true
  delete popover.dataset.anchorId
}

$('#search').oninput = renderSummaries
$('#play').onclick = () => state.audio[0]?.paused ? play() : pause()
$('#waveform').onclick = waveformClick
$('#zoom').oninput = event => { state.zoom = zoomLevels[Number(event.target.value)]; $('#zoom-label').textContent = state.zoom === 1 ? 'FIT' : `${state.zoom}×`; drawWaveform() }
$('#zoom-in').onclick = () => { $('#zoom').value = Math.min(4, Number($('#zoom').value) + 1); $('#zoom').dispatchEvent(new Event('input')) }
$('#zoom-out').onclick = () => { $('#zoom').value = Math.max(0, Number($('#zoom').value) - 1); $('#zoom').dispatchEvent(new Event('input')) }
$('#regenerate').onclick = event => state.transcript && requestQueue(state.transcript.id, state.transcript.status, event.currentTarget, event)
window.onresize = drawWaveform
window.onkeydown = event => { if (event.code === 'Space' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) { event.preventDefault(); state.audio[0]?.paused ? play() : pause() } }
document.addEventListener('keydown', event => { if (event.key === 'Escape') closeQueuePopover() })
document.addEventListener('click', event => {
  const popover = $('#action-popover')
  if (!popover.hidden && !popover.contains(event.target) && !event.target.closest('.queue-action, #regenerate, .regenerate-inline, .regenerate-inspector')) closeQueuePopover()
})
window.onbeforeunload = event => { if (state.dirty) event.preventDefault() }

loadSummaries().then(() => setInterval(refreshJobs, 1000)).catch(error => { $('#transcript').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>` })
