const $ = selector => document.querySelector(selector)
const colors = ['#3b82f6', '#8b5cf6', '#d97706', '#059669', '#db2777', '#0891b2']
const zoomLevels = [1, 8, 16, 32, 64]
const state = { summaries: [], jobs: [], seenCompleted: new Set(), transcript: null, selected: null, selectedRange: null, splitPoints: new Map(), correction: null, dirty: false, buffers: [], audio: [], duration: 0, zoom: 1, raf: null }
const RANGE_EPSILON = 0.01

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
const preciseClock = seconds => {
  const totalHundredths = Math.max(0, Math.round((seconds || 0) * 100))
  const whole = Math.floor(totalHundredths / 100), hundredths = totalHundredths % 100
  return `${clock(whole)}.${String(hundredths).padStart(2, '0')}`
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
    const review = transcriptReviewStatus(item, job)
    const jobLine = job ? `<div class="job-line ${job.status}"><span>${job.status === 'queued' ? `Queue ${job.position}` : escapeHtml(job.stage)}</span><span>${job.status === 'running' ? `${Math.round(job.progress * 100)}%` : job.status}</span></div>${active ? `<div class="job-progress"><i style="width:${Math.max(2, job.progress * 100)}%"></i></div>` : ''}` : ''
    const action = active ? '' : `<button class="queue-action" data-id="${item.id}" data-status="${item.status}">${job?.status === 'failed' ? 'Retry' : item.status === 'ready' ? 'Re-run' : item.status === 'stale' ? 'Update' : 'Queue'}</button>`
    return `<div class="transcript-card ${state.transcript?.id === item.id ? 'selected' : ''}" data-id="${item.id}" role="button" tabindex="0">
      <div class="transcript-card-top"><strong>${escapeHtml(item.title)} <em class="status ${review.kind}" title="${escapeHtml(review.title)}">${escapeHtml(review.label)}</em></strong>${action}</div>
      <span>${escapeHtml(item.startedAt || item.name)}${item.status === 'ready' ? ` · ${item.turnCount} turns` : ''}</span>${jobLine}
    </div>`
  }).join('')
  document.querySelectorAll('.transcript-card').forEach(card => {
    card.onclick = event => { if (!event.target.closest('.queue-action')) selectTranscript(card.dataset.id) }
    card.onkeydown = event => { if (event.key === 'Enter') selectTranscript(card.dataset.id) }
  })
  document.querySelectorAll('.queue-action').forEach(button => button.onclick = event => requestQueue(button.dataset.id, event))
}

function transcriptReviewStatus(item, job) {
  if (job?.status === 'failed') return {kind:'failed', label:'FAILED', title:job.error || 'Processing failed'}
  if (job?.status === 'running') return {kind:'processing', label:'PROCESSING', title:job.stage || 'Processing recording'}
  if (job?.status === 'queued') return {kind:'processing', label:'QUEUED', title:'Waiting to process'}
  if (item.status === 'unprocessed') return {kind:'work', label:'TO PROCESS', title:'Transcript has not been generated'}
  if (item.status === 'stale') return {kind:'work', label:'UPDATE', title:'Derived transcript needs regeneration'}
  if (item.review?.complete) return {kind:'done', label:'DONE', title:'All speaker runs are assigned'}
  const runs = item.review?.unassignedRunCount
  const speakers = item.review?.unassignedSpeakerCount
  return {
    kind:'work',
    label:runs == null ? 'REVIEW' : `${runs} UNNAMED`,
    title:runs == null ? 'Transcript needs review' : `${speakers} anonymous ${speakers === 1 ? 'speaker' : 'speakers'} across ${runs} unnamed ${runs === 1 ? 'run' : 'runs'}`,
  }
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
  state.selectedRange = null
  state.splitPoints = new Map()
  state.correction = null
  state.dirty = false
  seedAttendees()
  selectDefaultRange(state.selected)
  state.duration = state.transcript.turns.at(-1)?.end || 0
  $('#meeting-title').textContent = state.transcript.title
  $('#meeting-meta').textContent = state.transcript.editable ? `${state.transcript.name} · ${state.transcript.turns.length} turns · ${state.transcript.status}` : `${state.transcript.name} · ${state.transcript.status}`
  setSaveState(state.transcript.status === 'stale' && state.transcript.staleReason === 'hints' ? 'Guidance changed · regenerate' : state.transcript.editable ? (state.transcript.hintRevision ? 'Guidance loaded' : 'No guidance file') : 'Needs processing')
  renderSummaries(); renderTranscript(); renderInspector(); await loadAudio(); drawWaveform()
}

function seedAttendees() {
  const hints = state.transcript.hints
  hints.attendees = [...new Set((hints.attendees || []).map(item => typeof item === 'string' ? item : item.identity).filter(Boolean))]
  hints.hotwords ||= []; hints.speakers ||= []; hints.edits ||= []
}

function renderTranscript() {
  renderTranscriptHeader()
  if (!state.transcript.editable) {
    $('#transcript').innerHTML = `<div class="empty recording-empty"><strong>${state.transcript.status === 'stale' ? 'Stale transcript' : 'No transcript yet'}</strong><span>${state.transcript.status === 'stale' ? 'This Markdown was produced by an unsupported speech2md schema.' : 'This recording has not been processed by speech2md.'}</span><button class="primary regenerate-inline">${state.transcript.status === 'stale' ? 'Regenerate transcript' : 'Generate transcript'}</button></div>`
    $('.regenerate-inline').onclick = event => requestQueue(state.transcript.id, event)
    return
  }
  $('#transcript').innerHTML = state.transcript.turns.map(turn => {
    const color = speakerColor(turn.speaker)
    const assignment = speakerAssignment(turn)
    const slices = turnSlices(turn)
    const hinted = slices.some(slice => proposedIdentity(slice))
    const identities = [...new Set(slices.map(slice => assignedIdentity(slice) || turn.speaker))]
    const proposal = slices.length > 1 && identities.length > 1
      ? `<span class="proposed-speaker" title="Proposed split speaker assignments">→ split: ${identities.map(escapeHtml).join(' / ')}</span>`
      : assignment.changed
      ? `<span class="proposed-speaker" title="Proposed speaker reassignment">→ ${escapeHtml(assignment.proposed)}</span>`
      : hinted
      ? '<span class="hinted-speaker" title="Explicit speaker guidance">GUIDED</span>'
      : ''
    const wording = proposedWording(turn)
    const ranges = (slices.length > 1 || hinted) ? `<div class="turn-ranges">${slices.map(slice => {
      const identity = assignedIdentity(slice) || turn.speaker
      const selected = selectedRangeMatches(slice) ? 'selected' : ''
      const explicit = proposedIdentity(slice) ? 'hinted' : ''
      return `<button class="turn-range ${selected} ${explicit}" data-start="${slice.start}" data-end="${slice.end}"><span>${preciseClock(slice.start)}–${preciseClock(slice.end)}</span><strong>${explicit ? 'GUIDED · ' : ''}${escapeHtml(identity)}</strong></button>`
    }).join('')}</div>` : ''
    return `<article class="turn ${state.selected?.index === turn.index ? 'selected' : ''}" data-index="${turn.index}" style="--speaker-color:${color}">
      <span class="turn-time">${clock(turn.start)}</span><span class="speaker-dot"></span>
      <div class="turn-body"><div class="turn-speaker"><span>${escapeHtml(assignment.current)}</span>${proposal}</div><div class="turn-text">${escapeHtml(turn.text)}</div>${ranges}${wording ? `<div class="proposed-wording"><span>PROPOSED</span><div>${wording}</div></div>` : ''}</div>
    </article>`
  }).join('')
  document.querySelectorAll('.turn').forEach(element => {
    element.onclick = event => {
      if (window.getSelection()?.toString().trim()) return
      selectTurn(Number(element.dataset.index), true)
    }
    element.ondblclick = () => { if (state.audio[0]?.paused) play() }
    element.onmouseup = () => captureCorrection(element)
    element.querySelectorAll('.turn-range').forEach(button => button.onclick = event => {
      event.stopPropagation()
      selectTurnRange(Number(element.dataset.index), Number(button.dataset.start), Number(button.dataset.end), true)
    })
  })
}

function selectTurn(index, seek = false) {
  state.selected = state.transcript.turns[index]
  state.selectedRange = null
  selectDefaultRange(state.selected)
  state.correction = null
  if (seek) seekTo(state.selected.start)
  renderTranscript(); renderInspector(); drawWaveform(); scrollSelectedTurn()
}

function selectDefaultRange(turn) {
  if (!turn) return
  const slices = turnSlices(turn)
  if (slices.length > 1) state.selectedRange = {turnIndex: turn.index, start: slices[0].start, end: slices[0].end}
}

function reconcileSelectedRange() {
  if (!state.selected) return
  const slices = turnSlices(state.selected)
  if (slices.length <= 1) { state.selectedRange = null; return }
  const selected = slices.find(selectedRangeMatches) || slices[0]
  state.selectedRange = {turnIndex: state.selected.index, start: selected.start, end: selected.end}
}

function selectTurnRange(index, start, end, seek = false) {
  state.selected = state.transcript.turns[index]
  state.selectedRange = {turnIndex: index, start, end}
  state.correction = null
  if (seek) seekTo(start)
  renderTranscript(); renderInspector(); drawWaveform(); scrollSelectedTurn()
}

function scrollSelectedTurn(behavior = 'auto') {
  const index = state.selected?.index
  if (index == null) return
  requestAnimationFrame(() => document.querySelector(`.turn[data-index="${index}"]`)?.scrollIntoView({block:'center', behavior}))
}

function syncSelectionToTime(time) {
  if (!state.transcript?.editable) return
  const containing = state.transcript.turns
    .filter(turn => turn.start <= time && turn.end > time)
    .sort((left, right) => right.start - left.start)
  const turn = containing.find(candidate => candidate.index === state.selected?.index) || containing[0]
  if (!turn) return
  const slices = turnSlices(turn)
  const slice = slices.find(candidate => candidate.start <= time && candidate.end > time) || slices[0]
  const range = slices.length > 1 ? {turnIndex: turn.index, start: slice.start, end: slice.end} : null
  const unchanged = state.selected?.index === turn.index
    && ((!range && !state.selectedRange) || (range && selectedRangeMatches(slice)))
  if (unchanged) return
  state.selected = turn
  state.selectedRange = range
  state.correction = null
  renderTranscript(); renderInspector(); scrollSelectedTurn('auto')
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

function turnSlices(turn) {
  const boundaries = new Set([turn.start, turn.end])
  const track = turn.track || state.transcript.audio[0]?.role
  for (const speaker of state.transcript.hints.speakers) {
    for (const range of speaker.ranges || []) {
      if (!sameTrack(range.track, track) || !overlaps(turn, range)) continue
      if (range.start > turn.start + RANGE_EPSILON && range.start < turn.end - RANGE_EPSILON) boundaries.add(range.start)
      if (range.end > turn.start + RANGE_EPSILON && range.end < turn.end - RANGE_EPSILON) boundaries.add(range.end)
    }
  }
  for (const point of state.splitPoints.get(turn.index) || []) {
    if (point > turn.start + RANGE_EPSILON && point < turn.end - RANGE_EPSILON) boundaries.add(point)
  }
  const ordered = [...boundaries].sort((left, right) => left - right)
  return ordered.slice(0, -1).map((start, index) => ({
    ...turn,
    start,
    end: ordered[index + 1],
    parentIndex: turn.index,
  }))
}

function selectedRangeMatches(range) {
  return state.selectedRange?.turnIndex === range.parentIndex
    && Math.abs(state.selectedRange.start - range.start) < RANGE_EPSILON
    && Math.abs(state.selectedRange.end - range.end) < RANGE_EPSILON
}

function assignmentRange(turn) {
  if (state.selectedRange?.turnIndex !== turn.index) return turn
  return {...turn, start: state.selectedRange.start, end: state.selectedRange.end, parentIndex: turn.index}
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

function editMatchesTurn(edit, turn) {
  return (!edit.track || edit.track === turn.track) && overlaps(edit, turn) && turn.text.includes(edit.before)
}

function proposedWording(turn) {
  const replacements = []
  for (const edit of state.transcript.hints.edits) {
    const occurrences = state.transcript.turns.reduce((count, candidate) => {
      if (!editMatchesTurn(edit, candidate)) return count
      return count + candidate.text.split(edit.before).length - 1
    }, 0)
    if (occurrences !== 1 || !editMatchesTurn(edit, turn)) continue
    replacements.push({start: turn.text.indexOf(edit.before), before: edit.before, after: edit.after})
  }
  replacements.sort((left, right) => left.start - right.start)
  let cursor = 0, applied = 0, html = ''
  for (const replacement of replacements) {
    if (replacement.start < cursor) continue
    html += escapeHtml(turn.text.slice(cursor, replacement.start))
    html += `<del>${escapeHtml(replacement.before)}</del><ins>${escapeHtml(replacement.after)}</ins>`
    cursor = replacement.start + replacement.before.length
    applied += 1
  }
  return applied ? html + escapeHtml(turn.text.slice(cursor)) : ''
}

async function renderInspector() {
  if (!state.transcript.editable) {
    $('#inspector').innerHTML = `<div class="legacy-inspector"><div class="eyebrow">${state.transcript.status.toUpperCase()} RECORDING</div><h2>${escapeHtml(state.transcript.title)}</h2><div class="subhead">${escapeHtml(state.transcript.audio.map(item => item.name).join(', ') || 'No audio source found')}</div><div class="notice">↻ <span>${state.transcript.status === 'stale' ? 'The existing Markdown is left untouched until you explicitly regenerate it with the current speech2md.' : 'Generate current Markdown and voiceprints from this recording.'}</span></div>${metadataEditorHtml()}<button class="primary regenerate-inspector">${state.transcript.status === 'stale' ? 'Regenerate with speech2md' : 'Generate with speech2md'}</button></div>`
    $('.regenerate-inspector').onclick = event => requestQueue(state.transcript.id, event)
    bindMetadataEditor()
    return
  }
  if (!state.selected) return
  if (state.correction) return renderCorrectionInspector()
  const turn = state.selected
  const target = assignmentRange(turn)
  const slices = turnSlices(turn)
  const playhead = state.audio[0]?.currentTime ?? turn.start
  const existingBoundary = slices.some(slice => Math.abs(slice.start - playhead) < RANGE_EPSILON || Math.abs(slice.end - playhead) < RANGE_EPSILON)
  const canSplit = playhead > turn.start + RANGE_EPSILON && playhead < turn.end - RANGE_EPSILON && !existingBoundary
  const unidentified = !assignedIdentity(target)
  const filename = state.transcript.name.replace(/\.[^.]+$/, '.hint.yaml')
  $('#inspector').innerHTML = `<div class="legacy-inspector">
    <header class="guidance-header">
      <div><span class="guidance-title">◫ Guidance</span><small>${escapeHtml(filename)}</small></div>
      <span class="guidance-saved">● ${state.dirty ? 'Unsaved' : 'Saved'}</span>
    </header>
    ${guidanceDocumentHtml()}
    ${guidanceHotwordsHtml()}
    <section class="guidance-section people-guidance">
      <div class="guidance-section-heading"><div><strong>SPEAKERS &amp; ATTENDEES</strong><small>${unidentified ? 'Selected run is not assigned yet' : 'Speaker ranges are nested guidance'}</small></div><button id="show-add-attendee" title="Add attendee">＋</button></div>
      ${unidentified ? unidentifiedSelectionHtml(target, canSplit, playhead) : ''}
      <div id="guidance-people">${guidancePeopleHtml()}</div>
      <div id="anonymous-speakers">${anonymousSpeakersHtml()}</div>
      <form id="add-attendee-form" class="guidance-add-row" hidden><input id="new-attendee" class="field" placeholder="Name"><button class="secondary">Add person</button></form>
    </section>`
  bindGuidanceEditor()
  if (!unidentified) return
  try {
    const candidates = await api(`/api/transcripts/${state.transcript.id}/candidates/${encodeURIComponent(turn.speaker)}`)
    if (state.selected !== turn || state.correction) return
    const container = $('#voiceprint-candidates')
    if (!container) return
    container.innerHTML = voiceprintCandidatesHtml(candidates)
    container.querySelectorAll('.candidate').forEach(button => button.onclick = () => assignIdentity(button.dataset.identity))
  } catch (error) { const container = $('#voiceprint-candidates'); if (container) container.innerHTML = `<div class="guidance-empty">${escapeHtml(error.message)}</div>` }
}

function guidanceDocumentHtml() {
  const metadata = metadataValues()
  const row = (key, value, type = 'text') => `<label class="guidance-property"><span>${key}</span><input data-guidance-metadata="${key}" type="${type}" value="${escapeHtml(value)}"></label>`
  return `<section class="guidance-section guidance-document"><div class="guidance-section-heading"><strong>DOCUMENT</strong></div>
    ${row('title', metadata.title)}${row('started_at', metadata.started_at)}${row('ended_at', metadata.ended_at)}${row('calendar_event', metadata.calendar_event, 'url')}
  </section>`
}

function guidanceHotwordsHtml() {
  const words = state.transcript.hints.hotwords || []
  return `<section class="guidance-section guidance-hotwords"><div class="guidance-section-heading"><strong>HOTWORDS <em>${words.length}</em></strong><button id="show-add-hotword" title="Add hotword">＋</button></div>
    <div class="hotword-list">${words.map(word => `<button class="hotword" data-hotword="${escapeHtml(word)}" title="Remove hotword">${escapeHtml(word)} <span>×</span></button>`).join('') || '<span class="guidance-empty">None</span>'}</div>
    <form id="add-hotword-form" class="guidance-add-row" hidden><input id="new-hotword" class="field" placeholder="Hotword"><button class="secondary">Add</button></form>
  </section>`
}

function unidentifiedSelectionHtml(target, canSplit, playhead) {
  return `<div class="unidentified-guidance">
    <div class="unidentified-run"><span class="identity-dot" style="--identity-color:var(--muted)"></span><strong>Unidentified speaker</strong><time>${preciseClock(target.start)} → ${preciseClock(target.end)}</time></div>
    <div class="voiceprint-panel"><div class="voiceprint-heading"><strong>CLOSEST VOICEPRINTS</strong><small>all transcripts</small></div><div id="voiceprint-candidates"><div class="guidance-empty">Comparing voiceprints…</div></div>
      <form id="custom-identity-form" class="guidance-add-row"><input id="custom-identity" class="field" placeholder="Assign another name"><button class="secondary">Assign</button></form>
      <button id="split-turn" class="split-guidance" ${canSplit ? '' : 'disabled'}>Split at playhead${canSplit ? ` · ${preciseClock(playhead)}` : ''}</button>
    </div>
  </div>`
}

function voiceprintCandidatesHtml(candidates) {
  const names = attendeeNames()
  const attendees = new Set(names)
  const matches = new Map(candidates.map(candidate => [candidate.identity, candidate]))
  const attendeeCandidates = names.map((identity, order) => matches.get(identity) || {identity, similarity: null, source: 'No comparable voiceprint', order})
    .sort((left, right) => (right.similarity ?? -1) - (left.similarity ?? -1) || (left.order ?? 0) - (right.order ?? 0))
  const groups = [
    ['ATTENDEES', attendeeCandidates, attendeeCandidates.length],
    ['OTHER TRANSCRIPTS', candidates.filter(candidate => !attendees.has(candidate.identity)), 4],
  ]
  const html = groups.map(([label, items, limit]) => items.length ? `<div class="candidate-group"><span>${label}</span>${items.slice(0, limit).map(candidate => `
    <button class="candidate" data-identity="${escapeHtml(candidate.identity)}"><i class="identity-dot" style="--identity-color:${speakerColor(candidate.identity)}"></i><strong>${escapeHtml(candidate.identity)}</strong><small>${escapeHtml(candidate.source)}</small><b class="${candidate.similarity == null ? 'unavailable' : ''}">${candidate.similarity == null ? '—' : `${Math.round(candidate.similarity * 100)}%`}</b><em>${attendees.has(candidate.identity) ? '✓' : '+'}</em></button>`).join('')}</div>` : '').join('')
  return html || '<div class="guidance-empty">No attendees or identified voiceprints in this folder yet.</div>'
}

function attendeeNames() {
  return (state.transcript.hints.attendees || []).map(item => typeof item === 'string' ? item : item.identity).filter(Boolean)
}

function guidancePeopleHtml() {
  const speakers = new Map((state.transcript.hints.speakers || []).map(speaker => [speaker.identity, speaker.ranges || []]))
  return attendeeNames().map(identity => {
    const ranges = speakers.get(identity) || []
    return `<div class="guidance-person">
      <div class="guidance-person-row"><span class="identity-dot" style="--identity-color:${speakerColor(identity)}"></span><strong>${escapeHtml(identity)}</strong><button class="assign-person" data-identity="${escapeHtml(identity)}" title="Assign selected range to ${escapeHtml(identity)}">＋</button><button class="remove-person" data-identity="${escapeHtml(identity)}" title="Remove attendee">×</button></div>
      <div class="nested-ranges">${ranges.length ? ranges.map((range, index) => ({range, index})).sort((left, right) => left.range.start - right.range.start || left.range.end - right.range.end).map(({range, index}) => guidanceRangeHtml(identity, range, index)).join('') : '<span class="no-ranges">↳ No speaker range guidance</span>'}</div>
    </div>`
  }).join('')
}

function anonymousRuns() {
  const runs = []
  for (const turn of state.transcript?.turns || []) {
    for (const slice of turnSlices(turn)) if (!assignedIdentity(slice)) runs.push({turn, slice})
  }
  return runs
}

function anonymousSpeakersHtml() {
  const groups = new Map()
  for (const {turn, slice} of anonymousRuns()) {
    const group = groups.get(turn.speaker) || {speaker: turn.speaker, count: 0, duration: 0}
    group.count += 1
    group.duration += slice.end - slice.start
    groups.set(turn.speaker, group)
  }
  if (!groups.size) return ''
  return `<div class="anonymous-heading"><strong>ANONYMOUS SPEAKERS</strong><span>${groups.size}</span></div>${[...groups.values()].map(group => `
    <button class="anonymous-speaker ${state.selected?.speaker === group.speaker && !assignedIdentity(assignmentRange(state.selected)) ? 'selected' : ''}" data-speaker="${escapeHtml(group.speaker)}"><i class="identity-dot" style="--identity-color:${speakerColor(group.speaker)}"></i><strong>${escapeHtml(group.speaker)}</strong><small>${group.count} ${group.count === 1 ? 'run' : 'runs'} · ${clock(group.duration)}</small><span>Jump →</span></button>`).join('')}`
}

function renderTranscriptHeader() {
  const button = $('#next-unidentified')
  const count = state.transcript?.editable ? anonymousRuns().length : 0
  button.disabled = !count
  button.textContent = count ? `Next unnamed · ${count} →` : 'No unnamed speakers'
}

function jumpToUnnamed(speaker = '') {
  const runs = anonymousRuns().filter(({turn}) => !speaker || turn.speaker === speaker)
  if (!runs.length) return toast(speaker ? `${speaker} has no unnamed runs` : 'No unnamed speakers remain')
  const anchor = state.selected ? assignmentRange(state.selected).start : (state.audio[0]?.currentTime || -1)
  const next = runs.find(({slice}) => slice.start > anchor + RANGE_EPSILON) || runs[0]
  selectTurnRange(next.turn.index, next.slice.start, next.slice.end, true)
}

function guidanceRangeHtml(identity, range, index) {
  const selected = selectedGuidanceRange(identity, range) ? 'selected' : ''
  return `<div class="nested-range ${selected}"><button class="select-guidance-range" data-identity="${escapeHtml(identity)}" data-index="${index}">↳ <time>${preciseClock(range.start)} → ${preciseClock(range.end)}</time>${range.track ? `<small>${escapeHtml(range.track)}</small>` : ''}</button><button class="remove-guidance-range" data-identity="${escapeHtml(identity)}" data-index="${index}" title="Remove range">×</button></div>`
}

function selectedGuidanceRange(identity, range) {
  if (!state.selected) return false
  const target = assignmentRange(state.selected)
  return assignedIdentity(target) === identity && sameTrack(range.track, target.track) && overlaps(range, target)
}

function bindGuidanceEditor() {
  document.querySelectorAll('[data-guidance-metadata]').forEach(input => input.onchange = saveGuidanceMetadata)
  $('#show-add-hotword').onclick = () => { $('#add-hotword-form').hidden = false; $('#new-hotword').focus() }
  $('#add-hotword-form').onsubmit = async event => {
    event.preventDefault()
    const word = $('#new-hotword').value.trim(); if (!word) return
    if (!state.transcript.hints.hotwords.some(value => value.toLowerCase() === word.toLowerCase())) state.transcript.hints.hotwords.push(word)
    markDirty(); await saveHints(); renderInspector()
  }
  document.querySelectorAll('.hotword').forEach(button => button.onclick = async () => {
    state.transcript.hints.hotwords = state.transcript.hints.hotwords.filter(word => word !== button.dataset.hotword)
    markDirty(); await saveHints(); renderInspector()
  })
  $('#show-add-attendee').onclick = () => { $('#add-attendee-form').hidden = false; $('#new-attendee').focus() }
  $('#add-attendee-form').onsubmit = addAttendee
  document.querySelectorAll('.assign-person').forEach(button => button.onclick = () => assignIdentity(button.dataset.identity))
  document.querySelectorAll('.remove-person').forEach(button => button.onclick = () => removeAttendee(button.dataset.identity))
  document.querySelectorAll('.select-guidance-range').forEach(button => button.onclick = () => selectGuidanceRange(button.dataset.identity, Number(button.dataset.index)))
  document.querySelectorAll('.remove-guidance-range').forEach(button => button.onclick = () => removeGuidanceRange(button.dataset.identity, Number(button.dataset.index)))
  document.querySelectorAll('.anonymous-speaker').forEach(button => button.onclick = () => jumpToUnnamed(button.dataset.speaker))
  if ($('#split-turn')) $('#split-turn').onclick = splitTurnAtPlayhead
  if ($('#custom-identity-form')) $('#custom-identity-form').onsubmit = event => { event.preventDefault(); assignIdentity($('#custom-identity').value.trim()) }
}

async function saveGuidanceMetadata() {
  const selectors = {title:'title', started_at:'started_at', ended_at:'ended_at', calendar_event:'calendar_event'}
  for (const key of Object.keys(selectors)) state.transcript.hints[key] = document.querySelector(`[data-guidance-metadata="${key}"]`).value.trim() || null
  if (state.transcript.hints.title) { state.transcript.title = state.transcript.hints.title; $('#meeting-title').textContent = state.transcript.title }
  markDirty(); await saveHints(); renderInspector()
}

async function removeAttendee(identity) {
  if (state.transcript.hints.speakers.some(speaker => speaker.identity === identity)) return toast('Remove speaker guidance first')
  state.transcript.hints.attendees = attendeeNames().filter(name => name !== identity)
  markDirty(); await saveHints(); renderInspector()
}

function selectGuidanceRange(identity, index) {
  const range = state.transcript.hints.speakers.find(speaker => speaker.identity === identity)?.ranges[index]
  if (!range) return
  const turn = state.transcript.turns.find(candidate => sameTrack(range.track, candidate.track) && candidate.start <= range.start + RANGE_EPSILON && candidate.end > range.start + RANGE_EPSILON)
    || state.transcript.turns.find(candidate => sameTrack(range.track, candidate.track) && overlaps(candidate, range))
  if (!turn) return
  const slice = turnSlices(turn).find(candidate => proposedIdentity(candidate) === identity && overlaps(candidate, range)) || turn
  selectTurnRange(turn.index, slice.start, slice.end, true)
}

async function removeGuidanceRange(identity, index) {
  const speaker = state.transcript.hints.speakers.find(item => item.identity === identity)
  if (!speaker?.ranges[index]) return
  speaker.ranges.splice(index, 1)
  state.transcript.hints.speakers = state.transcript.hints.speakers.filter(item => item.ranges.length)
  reconcileSelectedRange(); markDirty(); await saveHints(); renderTranscript(); renderInspector(); drawWaveform()
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
    <button id="save-metadata" class="secondary metadata-save">Save metadata guidance</button>
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

async function addAttendee(event) {
  event?.preventDefault()
  const input = $('#new-attendee'); const identity = input.value.trim(); if (!identity) return
  if (!attendeeNames().includes(identity)) state.transcript.hints.attendees.push(identity)
  input.value = ''; markDirty(); await saveHints(); renderInspector()
}

async function assignIdentity(identity) {
  if (!identity) return toast('Enter an identity first')
  const turn = state.selected; const target = assignmentRange(turn); const track = turn.track || state.transcript.audio[0]?.role
  for (const speaker of state.transcript.hints.speakers) {
    speaker.ranges = (speaker.ranges || []).flatMap(range => subtractRange(range, target, track))
  }
  state.transcript.hints.speakers = state.transcript.hints.speakers.filter(speaker => speaker.ranges.length)
  let speaker = state.transcript.hints.speakers.find(item => item.identity === identity)
  if (!speaker) { speaker = {identity, ranges: []}; state.transcript.hints.speakers.push(speaker) }
  speaker.ranges.push({...(state.transcript.audio.length > 1 ? {track} : {}), start: target.start, end: target.end})
  speaker.ranges = mergeRanges(speaker.ranges)
  if (!state.transcript.hints.attendees.some(item => (typeof item === 'string' ? item : item.identity) === identity)) state.transcript.hints.attendees.push(identity)
  reconcileSelectedRange()
  markDirty(); await saveHints(); renderInspector(); renderTranscript(); drawWaveform()
}
const sameTrack = (left, right) => !left || !right || left === right

function subtractRange(range, target, track) {
  if (!sameTrack(range.track, track) || !overlaps(range, target)) return [range]
  const remaining = []
  if (range.start < target.start - RANGE_EPSILON) remaining.push({...range, end: target.start})
  if (range.end > target.end + RANGE_EPSILON) remaining.push({...range, start: target.end})
  return remaining
}

function mergeRanges(ranges) {
  const ordered = [...ranges].sort((left, right) => (left.track || '').localeCompare(right.track || '') || left.start - right.start || left.end - right.end)
  const merged = []
  for (const range of ordered) {
    const previous = merged.at(-1)
    if (previous && (previous.track || '') === (range.track || '') && range.start <= previous.end + RANGE_EPSILON) previous.end = Math.max(previous.end, range.end)
    else merged.push({...range})
  }
  return merged
}

function splitTurnAtPlayhead() {
  const turn = state.selected
  const point = Math.round((state.audio[0]?.currentTime ?? turn.start) * 100) / 100
  if (point <= turn.start + RANGE_EPSILON || point >= turn.end - RANGE_EPSILON) return toast('Move the playhead inside this turn first')
  const points = state.splitPoints.get(turn.index) || []
  if (!points.some(value => Math.abs(value - point) < RANGE_EPSILON)) points.push(point)
  state.splitPoints.set(turn.index, points)
  const right = turnSlices(turn).find(slice => Math.abs(slice.start - point) < RANGE_EPSILON)
  state.selectedRange = right ? {turnIndex: turn.index, start: right.start, end: right.end} : null
  renderTranscript(); renderInspector(); drawWaveform(); toast(`Split at ${preciseClock(point)}`)
}

function renderCorrectionInspector() {
  const turn = state.selected; const correction = state.correction
  $('#inspector').innerHTML = `
    <div class="eyebrow">TRANSCRIPT CORRECTION</div><h2>${clock(turn.start)} · ${escapeHtml(turn.speaker)}</h2><div class="subhead">${escapeHtml(turn.track || state.transcript.audio[0]?.role || 'mixed')} track</div>
    <div class="section"><span class="section-label">SELECTED TEXT</span><div class="selected-phrase">${escapeHtml(correction.before)}</div></div>
    <div class="section"><label class="section-label">REPLACE WITH</label><input id="replacement" class="field" value="${escapeHtml(correction.after)}"></div>
    <div class="section checkbox"><input id="add-hotword" type="checkbox" ${correction.hotword ? 'checked' : ''}><label for="add-hotword"><strong>Add replacement as a hotword</strong><br><small>Helps future transcriptions recognize this term.</small></label></div>
    <div class="hint-note">Saves localized guidance to the adjacent <code>.hint.yaml</code> only. Transcript Markdown changes only after explicit regeneration.</div>
    <button id="save-correction" class="primary">Save correction</button>
    <button id="cancel-correction" class="secondary" style="width:100%;margin-top:8px">Cancel</button></div>`
  $('#replacement').select()
  $('#save-correction').onclick = async () => {
    const after = $('#replacement').value.trim(); if (!after) return toast('Replacement cannot be empty')
    const edit = {start: turn.start, end: turn.end, before: correction.before, after}
    if (state.transcript.audio.length > 1 && turn.track) edit.track = turn.track
    state.transcript.hints.edits.push(edit)
    if ($('#add-hotword').checked && !state.transcript.hints.hotwords.some(value => value.toLowerCase() === after.toLowerCase())) state.transcript.hints.hotwords.push(after)
    markDirty(); await saveHints(); state.correction = null; renderTranscript(); renderInspector()
  }
  $('#cancel-correction').onclick = () => { state.correction = null; renderInspector() }
}

function markDirty() { state.dirty = true; setSaveState('Unsaved guidance', 'dirty') }
async function saveHints() {
  try {
    setSaveState('Saving…')
    const result = await api(`/api/transcripts/${state.transcript.id}/hints`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:state.transcript.hintRevision, hints:state.transcript.hints})})
    state.transcript.hintRevision = result.revision; state.dirty = false
    state.summaries = await api('/api/transcripts')
    const summary = state.summaries.find(item => item.id === state.transcript.id)
    if (summary) state.transcript.status = summary.status
    $('#meeting-meta').textContent = state.transcript.editable ? `${state.transcript.name} · ${state.transcript.turns.length} turns · ${state.transcript.status}` : `${state.transcript.name} · ${state.transcript.status}`
    setSaveState(state.transcript.status === 'stale' ? 'Saved · regenerate' : 'Guidance saved', 'saved'); renderSummaries(); toast('Guidance saved')
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
  if (state.transcript.audio.length <= 1) {
    for (const turn of state.transcript.turns) turn.track = state.transcript.audio[0]?.role || 'mixed'
    reconcileSelectedRange()
    return
  }
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
  reconcileSelectedRange()
  renderTranscript(); renderInspector()
}

function play() {
  if (!state.audio.length) return
  state.audio.forEach(audio => { audio.currentTime = state.audio[0].currentTime; audio.play().catch(() => {}) })
  $('#play').textContent = '❚❚'; tick()
}
function pause() { state.audio.forEach(audio => audio.pause()); cancelAnimationFrame(state.raf); $('#play').textContent = '▶' }
function seekTo(seconds) { state.audio.forEach(audio => { audio.currentTime = Math.min(seconds, audio.duration || seconds) }); updateClock(); drawWaveform() }
function tick() {
  updateClock()
  syncSelectionToTime(state.audio[0]?.currentTime || 0)
  drawWaveform()
  if (!state.audio[0]?.paused) state.raf = requestAnimationFrame(tick); else pause()
}
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
    context.fillStyle = '#777'; context.font = '9px Geist Mono'; context.fillText((state.transcript?.audio[lane]?.role || 'MIXED').toUpperCase(), 8, y + 13)
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
    const lane = Math.max(0, state.transcript.audio.findIndex(source => source.role === turn.track)), y = lane * laneHeight
    for (const slice of turnSlices(turn)) {
      if (slice.end <= start || slice.start >= end) continue
      const x = (slice.start - start) / (end - start) * width, right = (slice.end - start) / (end - start) * width
      const selected = selectedRangeMatches(slice) || (state.selected?.index === turn.index && !state.selectedRange)
      context.fillStyle = speakerColor(assignedIdentity(slice) || turn.speaker) + (selected ? 'cc' : '50')
      context.fillRect(x, y + laneHeight - 8, Math.max(1, right - x), 6)
    }
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
  seekTo(time)
  const slices = turnSlices(turn)
  const slice = slices.length > 1 && slices.find(item => item.start <= time && item.end > time)
  if (slice) selectTurnRange(turn.index, slice.start, slice.end, false)
  else selectTurn(turn.index, false)
}

function requestQueue(transcriptId, event) {
  event?.stopPropagation()
  enqueueRecording(transcriptId)
}

async function enqueueRecording(transcriptId) {
  try {
    const job = await api(`/api/transcripts/${transcriptId}/regenerate`, {method:'POST'})
    toast(job.mode === 'cached' ? 'Fast update started' : 'Recording added to queue'); await refreshJobs()
  } catch (error) { toast(error.message) }
}

$('#search').oninput = renderSummaries
$('#next-unidentified').onclick = () => jumpToUnnamed()
$('#play').onclick = () => state.audio[0]?.paused ? play() : pause()
$('#waveform').onclick = waveformClick
$('#zoom').oninput = event => { state.zoom = zoomLevels[Number(event.target.value)]; $('#zoom-label').textContent = state.zoom === 1 ? 'FIT' : `${state.zoom}×`; drawWaveform() }
$('#zoom-in').onclick = () => { $('#zoom').value = Math.min(4, Number($('#zoom').value) + 1); $('#zoom').dispatchEvent(new Event('input')) }
$('#zoom-out').onclick = () => { $('#zoom').value = Math.max(0, Number($('#zoom').value) - 1); $('#zoom').dispatchEvent(new Event('input')) }
$('#regenerate').onclick = event => state.transcript && requestQueue(state.transcript.id, event)
window.onresize = drawWaveform
window.onkeydown = event => { if (event.code === 'Space' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) { event.preventDefault(); state.audio[0]?.paused ? play() : pause() } }
window.onbeforeunload = event => { if (state.dirty) event.preventDefault() }

loadSummaries().then(() => setInterval(refreshJobs, 1000)).catch(error => { $('#transcript').innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>` })
