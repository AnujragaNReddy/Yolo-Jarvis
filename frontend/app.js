'use strict'

const $ = (id) => document.getElementById(id)

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue
    if (k === 'class') el.className = v
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v)
    else if (k === 'href') { if (/^https?:\/\//i.test(v)) el.href = v }
    else el.setAttribute(k, v === true ? '' : v)
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid))
  return el
}

async function api(path, { method = 'GET', body } = {}) {
  const r = await fetch(path, {
    method, headers: body !== undefined || method !== 'GET' ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : method !== 'GET' ? '{}' : undefined,
  })
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try { msg = (await r.json()).detail || msg } catch { /* not JSON */ }
    throw new Error(msg)
  }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r
}

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v) } catch { return d } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)) } catch { /* private mode */ } },
}

let settings = { honorific: 'sir', voice: 'en-GB-RyanNeural', speech_lang: 'en-US' }
let voiceOn = store.get('jarvis.voice', true)
let busy = false
let clockOffset = 0
let reminders = []
let lastByVoice = false

const you = () => settings.honorific || settings.user_name || 'sir'
const You = () => you().charAt(0).toUpperCase() + you().slice(1)

const MOBILE = matchMedia('(pointer: coarse)').matches || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
const IOS = /iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
const CAN_RECORD = !!(window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder)
// Per device: "wake" = always listen for "Jarvis" (browser speech recognition, good on desktop Chrome/Edge);
// "tap" = tap to talk, recorded here and transcribed on the PC (works on phones and every browser).
let listenMode = store.get('jarvis.listen', null)
function pickMode() {
  const SRok = !!(window.isSecureContext && (window.SpeechRecognition || window.webkitSpeechRecognition))
  if (listenMode === 'wake' && SRok) return 'wake'
  if (listenMode === 'tap' && CAN_RECORD) return 'tap'
  if (!MOBILE && SRok) return 'wake'
  if (CAN_RECORD) return 'tap'
  return 'type'
}
const wakeOn = () => pickMode() === 'wake'

/* ---------------- state ---------------- */
const LABEL = { standby: 'STANDBY', active: 'LISTENING', thinking: 'PROCESSING', speaking: 'SPEAKING', off: 'MIC OFF' }
let uiState = 'standby'
function setState(s) {
  uiState = s
  Reactor.setState(s)
  const el = $('state')
  el.textContent = LABEL[s] || s
  el.className = `state ${s}`
  $('mic').classList.toggle('live', s === 'active')
}

function hear(text, live = false) {
  const el = $('hear')
  el.textContent = text ? `“${text}”` : ''
  el.classList.toggle('live', live && !!text)
}

function activity(text) {
  const el = $('activity')
  el.replaceChildren()
  if (text) el.append(text, h('span', { class: 'dots' }))
}

function showAnswer(text) {
  const el = $('answer')
  el.replaceChildren(...text.split(/(\s+)/).map((w, i) => /\s+/.test(w) ? w : h('span', { class: 'word', style: `animation-delay:${Math.min(i * 18, 1800)}ms` }, w)))
}

function toast(title, text, alarm = false, ms = 6000) {
  const t = h('div', { class: `toast ${alarm ? 'alarm' : ''}`, role: 'status' }, h('b', {}, title), text)
  $('toasts').append(t)
  setTimeout(() => t.remove(), ms)
}

function chip(id, text, tone) {
  const el = $(id)
  el.textContent = text
  el.className = `chip ${tone || ''}`
}

/* ---------------- ears: wake word + speech recognition ---------------- */
// Browsers only allow the microphone on localhost or HTTPS, so a phone on http://<PC-IP> types instead.
const SR = window.isSecureContext ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null
const WAKE = /\b(?:hey |hi |ok |okay )?(?:jarvis|jervis|jarvas|jarves|jarviss|charvis|javis)\b[\s,.!?]*/i
const STOP = /^(stop|quiet|silence|shut up|be quiet|cancel|never ?mind|that's all|that is all|enough)[.!]?$/i

const ears = {
  rec: null, wanted: false, running: false, paused: false, attentiveUntil: 0,

  init() {
    if (!SR) { chip('chip-ears', 'EARS: TYPE ONLY', 'warn'); $('mic').classList.add('off'); return }
    const rec = new SR()
    rec.continuous = true
    rec.interimResults = true
    rec.lang = settings.speech_lang || 'en-US'
    rec.onstart = () => { this.running = true; chip('chip-ears', wakeOn() ? 'EARS: "JARVIS"' : 'EARS: ON', 'ok') }
    rec.onend = () => {
      this.running = false
      if (this.wanted && !this.paused) setTimeout(() => this.start(), 250)
      else if (!this.wanted && pickMode() === 'wake') chip('chip-ears', 'EARS: OFF')
    }
    rec.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        this.wanted = false
        chip('chip-ears', 'EARS: BLOCKED', 'bad')
        toast('Microphone blocked', 'Allow the microphone for this page (the icon in the address bar), then press the mic button.')
      } else if (e.error === 'network') {
        chip('chip-ears', 'EARS: NO NET', 'warn')
      } else if (e.error === 'audio-capture') {
        chip('chip-ears', 'EARS: NO MIC', 'bad')
      }
    }
    rec.onresult = (e) => this.onresult(e)
    this.rec = rec
  },

  start() {
    if (!this.rec) return
    this.wanted = true
    if (this.running || this.paused) return
    try { this.rec.start() } catch { /* already starting */ }
  },
  stop() {
    this.wanted = false
    this.attentiveUntil = 0
    try { this.rec?.abort() } catch { /* idle */ }
  },
  pause() { this.paused = true; try { this.rec?.abort() } catch { /* idle */ } },
  resume() { this.paused = false; if (this.wanted) this.start() },
  attend(ms) {
    if (!this.rec) { $('q').focus(); return }
    this.attentiveUntil = Date.now() + ms
    setState('active')
    this.start()
  },
  get attentive() { return Date.now() < this.attentiveUntil },

  onresult(e) {
    if (this.paused) return
    let interim = ''
    const finals = []
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i]
      if (r.isFinal) finals.push(r[0].transcript)
      else interim += r[0].transcript
    }
    if (interim && !busy) {
      if (this.attentive) { hear(interim.trim(), true); this.attentiveUntil = Math.max(this.attentiveUntil, Date.now() + 4000) }
      else if (wakeOn() && WAKE.test(interim)) { setState('active'); hear(interim.trim(), true) }
    }
    for (const f of finals) this.heard(f.trim())
  },

  heard(text) {
    if (!text) return
    if (busy) return
    const wake = wakeOn() && WAKE.test(text)
    if (wake) {
      const cmd = text.replace(WAKE, ' ').replace(/\s+/g, ' ').trim()
      if (cmd.replace(/[^\w]/g, '').length > 1) submit(cmd, text, true)
      else { chime(660, 0.08); hear(`Yes, ${you()}?`); this.attend(8000) }
      return
    }
    if (this.attentive) { submit(text, text, true); return }
    if (uiState === 'active') { setState('standby'); hear('') }
  },
}

/* ---------------- tap to talk: record here, transcribe on the PC ---------------- */
const recorder = {
  stream: null, active: false, stopFlag: false, idle: null,

  async open() {
    clearTimeout(this.idle)
    if (!this.stream) {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
    }
  },
  release() {
    // iPhones play quietly through the earpiece while the mic is open, so let go of it straight away there.
    clearTimeout(this.idle)
    this.idle = setTimeout(() => { this.stream?.getTracks().forEach((t) => t.stop()); this.stream = null }, IOS ? 0 : 20000)
  },
  stop() { this.stopFlag = true },

  // Records until you stop talking (about 1.2 s of quiet), you tap again, or nothing is said in time.
  async capture({ startTimeout = 8000, maxMs = 15000 } = {}) {
    await this.open()
    if (!audioCtx) audioCtx = new AudioContext()
    await audioCtx.resume().catch(() => {})
    const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'].find((m) => MediaRecorder.isTypeSupported?.(m)) || ''
    const mr = new MediaRecorder(this.stream, mime ? { mimeType: mime } : undefined)
    const chunks = []
    mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data) }
    const src = audioCtx.createMediaStreamSource(this.stream)
    const an = audioCtx.createAnalyser()
    an.fftSize = 1024
    src.connect(an)
    const buf = new Float32Array(an.fftSize)
    this.active = true
    this.stopFlag = false
    mr.start(250)
    const t0 = performance.now()
    let floor = 0.006
    let heard = false
    let lastLoud = 0
    await new Promise((resolve) => {
      const tick = () => {
        an.getFloatTimeDomainData(buf)
        let s = 0
        for (const v of buf) s += v * v
        const rms = Math.sqrt(s / buf.length)
        const t = performance.now() - t0
        const thr = Math.max(0.02, floor * 3)
        if (rms > thr) { heard = true; lastLoud = t } else floor = floor * 0.95 + rms * 0.05
        Reactor.pushLevel(rms * 7)
        if (this.stopFlag || t > maxMs || (heard && t - lastLoud > 1200) || (!heard && t > startTimeout)) return resolve()
        setTimeout(tick, 50)
      }
      tick()
    })
    const stopped = new Promise((r) => { mr.onstop = r })
    mr.stop()
    await stopped
    src.disconnect()
    this.active = false
    this.release()
    const long = performance.now() - t0 > 600
    if (!heard && !(this.stopFlag && long)) return null
    return new Blob(chunks, { type: mr.mimeType || mime || 'audio/webm' })
  },
}

function micProblem(e) {
  if (e?.name === 'NotAllowedError') return 'Microphone blocked. Allow it for this page in the browser\'s site settings, then tap the mic again.'
  if (e?.name === 'NotFoundError') return 'No microphone found on this device.'
  return `Microphone problem: ${e?.message || e}`
}

async function tapListen({ followUp = false } = {}) {
  if (recorder.active) { recorder.stop(); return }
  if (busy) return
  setState('active')
  hear(followUp ? '' : 'Listening…', true)
  let blob
  try {
    blob = await recorder.capture({ startTimeout: followUp ? 5000 : 8000 })
  } catch (e) {
    toast('Microphone', micProblem(e))
    chip('chip-ears', 'EARS: BLOCKED', 'bad')
    setState('standby')
    hear('')
    return
  }
  if (!blob) { setState('standby'); hear(''); return }
  setState('thinking')
  activity('Transcribing')
  let text = ''
  try {
    const r = await fetch('/api/stt', { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/webm' }, body: blob })
    const j = await r.json()
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`)
    text = j.text
  } catch (e) {
    toast('Speech', e.message)
  }
  activity('')
  if (!text) { hear('Sorry, I didn\'t catch that.'); setState('standby'); return }
  const wake = text.match(WAKE)
  const cmd = text.replace(WAKE, ' ').replace(/\s+/g, ' ').trim()
  if (wake && cmd.replace(/[^\w]/g, '').length <= 1) { chime(660, 0.08); tapListen({ followUp: true }); return }
  submit(cmd || text, text, true)
}

setInterval(() => {
  if (uiState === 'active' && !ears.attentive && !busy && !mouth.speaking && !recorder.active) {
    setState(idleState())
    if ($('hear').classList.contains('live')) hear('')
    if (!wakeOn() && ears.wanted) ears.stop()
  }
}, 300)

/* ---------------- mouth: neural voice with analyser, browser voice as fallback ---------------- */
let audioCtx = null
function chime(freq = 880, dur = 0.12, times = 1) {
  if (!audioCtx) return
  for (let i = 0; i < times; i++) {
    const o = audioCtx.createOscillator()
    const g = audioCtx.createGain()
    const t0 = audioCtx.currentTime + i * (dur + 0.08)
    o.frequency.value = freq * (i % 2 ? 1.25 : 1)
    o.type = 'sine'
    g.gain.setValueAtTime(0.0001, t0)
    g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02)
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
    o.connect(g).connect(audioCtx.destination)
    o.start(t0)
    o.stop(t0 + dur + 0.02)
  }
}

function idleState() {
  const mode = pickMode()
  return mode === 'wake' && !ears.wanted ? 'off' : 'standby'
}

const mouth = {
  speaking: false, src: null, done: null,

  async say(text, { voice, followUp = true } = {}) {
    if (!text) return
    if (!voiceOn) { this.after(followUp); return }
    this.stop()
    this.speaking = true
    ears.pause()
    setState('speaking')
    try {
      const r = await fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: voice || settings.voice }) })
      if (!r.ok) throw new Error('tts')
      const data = await r.arrayBuffer()
      if (!this.speaking) return
      await this.play(data)
      chip('chip-voice', 'VOICE: NEURAL', 'ok')
    } catch {
      if (this.speaking) { chip('chip-voice', 'VOICE: BROWSER', 'warn'); await this.browser(text) }
    } finally {
      this.speaking = false
      ears.resume()
      this.after(followUp)
    }
  },

  after(followUp) {
    // After a spoken question, keep the conversation going hands-free; after a typed one, don't.
    if (followUp && lastByVoice && pickMode() === 'tap') setTimeout(() => tapListen({ followUp: true }), 150)
    else if (followUp && lastByVoice && ears.wanted) ears.attend(6500)
    else setState(idleState())
  },

  // Plays through Web Audio, which phones allow once the page has been tapped (an <audio> element started
  // after a network request is blocked on iPhones).
  async play(data) {
    if (!audioCtx) audioCtx = new AudioContext()
    await audioCtx.resume().catch(() => {})
    const buffer = await audioCtx.decodeAudioData(data)
    if (!this.speaking) return
    await new Promise((resolve) => {
      const src = audioCtx.createBufferSource()
      src.buffer = buffer
      const an = audioCtx.createAnalyser()
      an.fftSize = 512
      src.connect(an).connect(audioCtx.destination)
      const buf = new Uint8Array(an.fftSize)
      let playing = true
      const tick = () => {
        if (!playing) return
        an.getByteTimeDomainData(buf)
        let sum = 0
        for (const v of buf) sum += ((v - 128) / 128) ** 2
        Reactor.pushLevel(Math.sqrt(sum / buf.length) * 4)
        requestAnimationFrame(tick)
      }
      const end = () => { playing = false; this.src = null; resolve() }
      src.onended = end
      this.src = src
      this.done = end
      src.start()
      tick()
    })
  },

  browser(text) {
    return new Promise((resolve) => {
      if (!window.speechSynthesis) return resolve()
      const u = new SpeechSynthesisUtterance(text.replace(/https?:\/\/\S+/g, ''))
      const voices = speechSynthesis.getVoices()
      u.voice = voices.find((v) => /en-GB/i.test(v.lang) && /male|ryan|george|daniel/i.test(v.name)) || voices.find((v) => /en-GB/i.test(v.lang)) || null
      u.rate = 1.02
      const beat = setInterval(() => Reactor.pushLevel(0.35 + Math.random() * 0.4), 90)
      u.onend = u.onerror = () => { clearInterval(beat); resolve() }
      this.done = () => { clearInterval(beat); resolve() }
      speechSynthesis.speak(u)
    })
  },

  stop() {
    const was = this.speaking
    this.speaking = false
    if (this.src) { try { this.src.stop() } catch { /* already ended */ } this.src = null }
    try { speechSynthesis.cancel() } catch { /* none */ }
    if (this.done) { this.done(); this.done = null }
    return was
  },
}

/* ---------------- chat ---------------- */
async function submit(text, heard, byVoice = false) {
  text = (text || '').trim()
  if (!text || busy) return
  if (STOP.test(text)) {
    mouth.stop()
    ears.attentiveUntil = 0
    hear('')
    setState(idleState())
    return
  }
  lastByVoice = byVoice
  busy = true
  ears.attentiveUntil = 0
  mouth.stop()
  hear(heard || text)
  addLog('user', text)
  $('cards').replaceChildren()
  $('answer').replaceChildren()
  setState('thinking')
  activity('Processing')
  let answer = null
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) })
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`)
    const reader = r.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let i
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i)
        buf = buf.slice(i + 2)
        const line = chunk.split('\n').find((l) => l.startsWith('data: '))
        if (!line) continue
        const ev = JSON.parse(line.slice(6))
        if (ev.type === 'thinking') activity('Thinking')
        else if (ev.type === 'tool') activity(ev.label || ev.name)
        else if (ev.type === 'answer') answer = ev
      }
    }
  } catch (e) {
    answer = { text: `I've lost contact with my core systems, ${you()}. ${e.message}`, cards: {}, tools: [] }
  }
  busy = false
  activity('')
  if (!answer) answer = { text: 'No reply came back.', cards: {}, tools: [] }
  showAnswer(answer.text)
  renderCards(answer.cards || {})
  addLog('assistant', answer.text, answer.tools)
  if ((answer.tools || []).some((t) => t === 'set_timer' || t === 'set_reminder')) askNotify()
  if ((answer.tools || []).includes('new_session')) loadHistory()
  refresh()
  loadMemory()
  await mouth.say(answer.text)
}

function addLog(role, text, tools = [], ts = Date.now() / 1000) {
  const log = $('log')
  const used = (tools || []).filter((t) => !['chat', 'greeting', 'wake'].includes(t))
  log.append(h('div', { class: `msg ${role}` },
    used.length ? h('span', { class: 'tools' }, used.join(' · ').replace(/_/g, ' ')) : null,
    text,
    h('time', {}, new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }))))
  while (log.children.length > 60) log.firstChild.remove()
  log.scrollTop = log.scrollHeight
}

async function loadHistory() {
  $('log').replaceChildren()
  try {
    const rows = await api('/api/history')
    for (const m of rows) addLog(m.role, m.content, m.tools, m.ts)
    if (!rows.length) $('log').append(h('p', { class: 'empty' }, 'Nothing said yet.'))
  } catch { /* backend down */ }
}

/* ---------------- cards ---------------- */
function renderCards(cards) {
  const out = []
  if (cards.news?.length) {
    out.push(h('div', { class: 'card' }, h('h3', {}, 'Headlines'),
      h('ul', {}, cards.news.slice(0, 6).map((n) => h('li', {},
        h('a', { href: n.link, target: '_blank', rel: 'noopener noreferrer' }, n.title), h('small', {}, n.source || ''))))))
  }
  if (Array.isArray(cards.files) && cards.files.length) {
    out.push(h('div', { class: 'card' }, h('h3', {}, 'Files found'),
      h('ul', {}, cards.files.map((f) => h('li', {},
        h('div', {}, f.name, h('div', { class: 'file-path' }, f.path)),
        h('button', { class: 'btn small', onclick: async () => {
          try { toast('Files', (await api('/api/open-file', { method: 'POST', body: { path: f.path } })).result) } catch (e) { toast('Files', e.message) }
        } }, 'Open'))))))
  }
  if (typeof cards.search === 'string') {
    out.push(h('div', { class: 'card' }, h('h3', {}, 'Web search'), h('pre', {}, cards.search)))
  }
  if (cards.weather && typeof cards.weather === 'object' && cards.weather.place) {
    const w = cards.weather
    out.push(h('div', { class: 'card' }, h('h3', {}, `Weather · ${w.place}`),
      h('ul', {}, h('li', {}, `${w.temperature}, ${w.now}`, h('small', {}, `feels ${w.feels_like}`)),
        h('li', {}, `Today ${w.today_low} – ${w.today_high}`, h('small', {}, `rain ${w.rain_chance}`)))))
  }
  if (cards.network && typeof cards.network === 'object') {
    const n = cards.network
    out.push(h('div', { class: 'card' }, h('h3', {}, 'Home network'),
      h('ul', {}, h('li', {}, `${n.online} online of ${n.known} known`),
        n.new?.length ? h('li', {}, `New: ${n.new.slice(0, 6).join(', ')}`) : null,
        n.risky?.length ? h('li', {}, `Risky services: ${n.risky.join(', ')}`) : null)))
  }
  $('cards').replaceChildren(...out)
}

/* ---------------- panels ---------------- */
const ICONS = {
  sun: '<circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  cloud: '<path d="M7 18h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.5 9.5 4.3 4.3 0 0 0 7 18z"/>',
  rain: '<path d="M7 15h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.5 6.5 4.3 4.3 0 0 0 7 15z"/><path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3"/>',
  snow: '<path d="M7 14h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.5 5.5 4.3 4.3 0 0 0 7 14z"/><path d="M8 18h.01M12 20h.01M16 18h.01M10 22h.01M14 22h.01"/>',
  storm: '<path d="M7 15h10.5a4 4 0 0 0 .6-7.95A6 6 0 0 0 6.5 6.5 4.3 4.3 0 0 0 7 15z"/><path d="M13 15l-2 4h3l-2 4"/>',
  fog: '<path d="M4 9h16M3 13h18M5 17h14"/>',
}
function weatherIcon(code, day) {
  const k = code >= 95 ? 'storm' : code >= 71 && code <= 86 && ![80, 81, 82].includes(code) ? 'snow'
    : code >= 51 ? 'rain' : code >= 45 ? 'fog' : code >= 2 ? 'cloud' : day ? 'sun' : 'moon'
  const span = h('div', { class: 'weather-icon', 'aria-hidden': 'true' })
  span.innerHTML = `<svg viewBox="0 0 24 24">${ICONS[k]}</svg>`
  return span
}

function renderWeather(w) {
  const el = $('weather')
  if (!w) { el.replaceChildren(h('p', { class: 'muted' }, 'Weather unavailable. Set your city in Settings.')); return }
  el.replaceChildren(
    h('div', { class: 'weather-main' }, weatherIcon(w.code, w.is_day),
      h('div', {}, h('div', { class: 'weather-temp' }, w.temperature), h('div', { class: 'weather-desc' }, w.now))),
    h('div', { class: 'kv' },
      h('span', {}, 'Place'), h('b', {}, w.place.split(',')[0]),
      h('span', {}, 'Feels like'), h('b', {}, w.feels_like),
      h('span', {}, 'High / low'), h('b', {}, `${w.today_high} / ${w.today_low}`),
      h('span', {}, 'Rain chance'), h('b', {}, w.rain_chance),
      h('span', {}, 'Humidity'), h('b', {}, w.humidity),
      h('span', {}, 'Wind'), h('b', {}, w.wind)))
}

function gauge(label, pct, text, lowIsBad = false) {
  const r = 26
  const c = 2 * Math.PI * r
  const p = Math.max(0, Math.min(100, pct ?? 0))
  const tone = lowIsBad ? (p <= 10 ? 'crit' : p <= 25 ? 'hot' : '') : p >= 90 ? 'crit' : p >= 75 ? 'hot' : ''
  const wrap = h('div', { class: `gauge ${tone}` })
  wrap.innerHTML = `<svg viewBox="0 0 64 64"><circle class="track" cx="32" cy="32" r="${r}"/>` +
    `<circle class="val" cx="32" cy="32" r="${r}" stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - p / 100)}" transform="rotate(-90 32 32)"/>` +
    `<text x="32" y="32"></text></svg>`
  wrap.querySelector('text').textContent = text ?? `${Math.round(p)}`
  wrap.append(h('small', {}, label))
  return wrap
}

function renderSystem(s) {
  if (!s) return
  const g = [gauge('CPU', s.cpu), gauge('RAM', s.ram), gauge('DISK', s.disk)]
  g.push(s.battery != null ? gauge(s.plugged ? 'CHARGING' : 'BATTERY', s.battery, null, true) : gauge('CORES', 100, s.cores))
  $('gauges').replaceChildren(...g)
  $('sys-meta').replaceChildren(
    h('div', {}, 'Memory ', h('b', {}, `${s.ram_used_gb} / ${s.ram_total_gb} GB`)),
    h('div', {}, 'System drive free ', h('b', {}, `${s.disk_free_gb} GB`)),
    h('div', {}, 'Uptime ', h('b', {}, `${s.uptime_h} h`)))
}

function renderNetwork(n) {
  const el = $('network')
  if (!n) {
    el.replaceChildren(h('p', { class: 'muted small' }, 'Yolo Network Scanner isn\'t running. Start it to let JARVIS watch your Wi-Fi.'))
    return
  }
  el.replaceChildren(
    h('div', { class: 'net-big' }, String(n.online), h('small', {}, `online · ${n.known} known`)),
    h('ul', { class: 'net-list' },
      n.new?.length ? h('li', { class: 'warn' }, `${n.new.length} new: ${n.new.slice(0, 3).join(', ')}`) : h('li', {}, 'No new devices'),
      n.risky?.length ? h('li', { class: 'warn' }, `Risky services on ${n.risky.length}`) : null,
      n.not_trusted_online?.length ? h('li', {}, `${n.not_trusted_online.length} online not marked trusted`) : null))
}

function fmtLeft(sec) {
  sec = Math.max(0, Math.round(sec))
  const hh = Math.floor(sec / 3600)
  const mm = Math.floor((sec % 3600) / 60)
  const ss = sec % 60
  return hh ? `${hh}:${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}` : `${mm}:${String(ss).padStart(2, '0')}`
}

function renderReminders() {
  const el = $('reminders')
  if (!reminders.length) { el.replaceChildren(h('p', { class: 'empty' }, 'Nothing scheduled. Try "set a timer for 10 minutes".')); return }
  const now = (Date.now() + clockOffset) / 1000
  el.replaceChildren(...reminders.map((r) => h('div', { class: 'row' },
    h('div', { class: 'txt' }, h('div', { class: 'kind' }, r.kind), r.text),
    h('span', { class: 'when', title: new Date(r.due * 1000).toLocaleString() },
      r.due - now < 86400 ? fmtLeft(r.due - now) : new Date(r.due * 1000).toLocaleDateString()),
    h('button', { class: 'x', title: 'Cancel', 'aria-label': `Cancel ${r.text}`, onclick: async () => {
      await api(`/api/reminders/${r.id}`, { method: 'DELETE' }); refresh()
    } }, '✕'))))
}
setInterval(() => { if (reminders.length) renderReminders() }, 1000)

function renderNotes(notes) {
  const el = $('notes')
  if (!notes?.length) { el.replaceChildren(h('p', { class: 'empty' }, 'Your list is empty.')); return }
  el.replaceChildren(...notes.map((n) => h('div', { class: 'row' },
    h('input', { type: 'checkbox', class: 'tick', 'aria-label': `Done: ${n.text}`, onchange: async () => {
      renderNotes(await api(`/api/notes/${n.id}/done`, { method: 'POST' }))
    } }),
    h('div', { class: 'txt' }, n.text))))
}

async function loadMemory() {
  try {
    const facts = await api('/api/memory')
    const el = $('memory')
    if (!facts.length) { el.replaceChildren(h('p', { class: 'empty' }, 'Tell me things to remember: "remember that my car is on level 3".')); return }
    el.replaceChildren(...facts.slice().reverse().map((f) => h('div', { class: 'row' },
      h('div', { class: 'txt' }, f.fact),
      h('button', { class: 'x', title: 'Forget', 'aria-label': `Forget ${f.fact}`, onclick: async () => {
        await api(`/api/memory/${f.id}`, { method: 'DELETE' }); loadMemory()
      } }, '✕'))))
  } catch { /* backend down */ }
}

async function refresh() {
  try {
    const s = await api('/api/status')
    clockOffset = s.now * 1000 - Date.now()
    renderSystem(s.system)
    renderWeather(s.weather)
    renderNetwork(s.network)
    reminders = s.reminders
    renderReminders()
    renderNotes(s.notes)
    $('banner').hidden = !$('banner').dataset.health
  } catch {
    showBanner('JARVIS core is offline. Start it with start.bat, then reload this page.')
  }
}

function showBanner(text) {
  const b = $('banner')
  b.textContent = text
  b.hidden = false
}

function tickClock() {
  const d = new Date(Date.now() + clockOffset)
  $('clock-time').textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  $('clock-sec').textContent = String(d.getSeconds()).padStart(2, '0')
  $('clock-date').textContent = d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
}
setInterval(tickClock, 1000)

/* ---------------- reminders arriving ---------------- */
let notifyAsked = false
function askNotify() {
  if (notifyAsked || !('Notification' in window) || Notification.permission !== 'default') return
  notifyAsked = true
  Notification.requestPermission().catch(() => {})
}

function listen() {
  const es = new EventSource('/api/events')
  es.onmessage = async (m) => {
    const ev = JSON.parse(m.data)
    if (ev.type !== 'reminder') return
    const line = ev.kind === 'timer' ? `${You()}, your ${ev.text} is done.` : `${You()}, a reminder: ${ev.text}.`
    chime(880, 0.14, 4)
    toast(ev.kind === 'timer' ? 'Timer' : 'Reminder', ev.text, true, 15000)
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      new Notification('J.A.R.V.I.S.', { body: line })
    }
    addLog('assistant', line, [ev.kind === 'timer' ? 'timer' : 'reminder'])
    refresh()
    const wait = () => (busy || mouth.speaking ? setTimeout(wait, 500) : mouth.say(line, { followUp: false }))
    setTimeout(wait, 900)
  }
}

/* ---------------- settings ---------------- */
async function openSettings() {
  const dlg = $('settings')
  const form = $('settings-form')
  try {
    const data = await api('/api/settings')
    settings = { ...settings, ...data.settings }
    $('voice-select').replaceChildren(...data.voices.map((v) => h('option', { value: v.id }, v.label)))
    const models = data.models.length ? data.models : [settings.model]
    $('model-select').replaceChildren(...models.map((m) => h('option', { value: m }, m)))
  } catch (e) { toast('Settings', e.message); return }
  for (const el of form.elements) {
    if (!el.name) continue
    if (el.type === 'checkbox') el.checked = settings[el.name] === 'true'
    else el.value = settings[el.name] ?? ''
  }
  const sel = $('listen-mode')
  const SRok = !!(window.isSecureContext && (window.SpeechRecognition || window.webkitSpeechRecognition))
  sel.replaceChildren(
    h('option', { value: 'wake', disabled: !SRok }, `Always listen for "Jarvis"${SRok ? '' : ' (needs Chrome or Edge)'}`),
    h('option', { value: 'tap', disabled: !CAN_RECORD }, 'Tap to talk, then hands-free follow-ups (best on phones)'))
  sel.value = pickMode() === 'type' ? 'tap' : pickMode()
  const box = $('lan-box')
  box.replaceChildren()
  try {
    const lan = await api('/api/lan')
    if (lan.link) {
      const copy = (text) => async () => {
        try { await navigator.clipboard.writeText(text); toast('Copied', text.length > 60 ? 'Link copied.' : text) } catch { toast('Copy', 'Select it and copy it.') }
      }
      box.append(h('b', {}, 'Use JARVIS on your phone (same Wi-Fi)'),
        h('ol', { class: 'muted small steps' },
          h('li', {}, 'Send yourself this link and open it on the phone. It\'s the key to this PC: only open it on your own devices.'),
          h('li', {}, 'The phone warns that the connection isn\'t private (JARVIS made its own certificate). Tap Advanced → Proceed (Android) or Show details → visit this website (iPhone). Or install the certificate below once to skip that.'),
          h('li', {}, 'Tap Activate, then the mic or the reactor, and allow the microphone. Tip: browser menu → Add to Home screen.')),
        h('div', { class: 'lan-row' }, h('code', {}, lan.link), h('button', { type: 'button', class: 'btn small', onclick: copy(lan.link) }, 'Copy')),
        h('p', { class: 'muted small' }, 'Optional, removes the warning: open ', h('code', {}, lan.ca_url),
          ' on the phone and install it. Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate. iPhone: Settings → Profile Downloaded → Install, then General → About → Certificate Trust Settings → turn on YOLO-JARVIS.'))
    } else if (!lan.enabled) {
      box.append(h('p', { class: 'muted small' }, 'Other devices: off. To use JARVIS from your phone, add JARVIS_LAN=true to backend\\.env and restart.'))
    }
  } catch { /* older backend */ }
  dlg.showModal()
}

async function saveSettings() {
  const form = $('settings-form')
  const values = {}
  for (const el of form.elements) {
    if (!el.name) continue
    values[el.name] = el.type === 'checkbox' ? String(el.checked) : el.value.trim()
  }
  if (!values.honorific) values.honorific = 'sir'
  listenMode = $('listen-mode').value
  store.set('jarvis.listen', listenMode)
  try {
    const r = await api('/api/settings', { method: 'POST', body: values })
    const langChanged = r.settings.speech_lang !== settings.speech_lang
    settings = { ...settings, ...r.settings }
    if (ears.rec && langChanged) { ears.rec.lang = settings.speech_lang; ears.pause(); ears.resume() }
    applyMode()
    toast('Settings', 'Saved.')
    refresh()
  } catch (e) { toast('Settings', e.message) }
}

function applyMode() {
  const mode = pickMode()
  if (mode === 'wake') {
    if (!ears.rec) ears.init()
    ears.start()
  } else {
    ears.stop()
  }
  chip('chip-ears', { wake: 'EARS: "JARVIS"', tap: 'EARS: TAP TO TALK', type: 'EARS: TYPE ONLY' }[mode], mode === 'type' ? 'warn' : 'ok')
  $('mic').classList.toggle('off', mode === 'type')
  setState(idleState())
}

function renderMute() {
  const b = $('btn-mute')
  b.classList.toggle('off', !voiceOn)
  b.innerHTML = voiceOn
    ? '<svg viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="m16 9 6 6M22 9l-6 6"/></svg>'
  chip('chip-voice', voiceOn ? 'VOICE: ON' : 'VOICE: MUTED', voiceOn ? 'ok' : 'warn')
}

/* ---------------- wiring ---------------- */
$('ask').addEventListener('submit', (e) => {
  e.preventDefault()
  const q = $('q')
  const text = q.value
  q.value = ''
  submit(text)
})

function talk() {
  if (!audioCtx) audioCtx = new AudioContext()
  audioCtx.resume().catch(() => {})
  const mode = pickMode()
  if (mode === 'tap') {
    if (recorder.active) { recorder.stop(); return }
    mouth.stop()
    if (busy) return
    chime(660, 0.08)
    tapListen()
    return
  }
  if (mode === 'type') { $('q').focus(); return }
  if (mouth.stop()) { ears.attend(8000); return }
  if (busy) return
  chime(660, 0.08)
  hear('')
  ears.attend(8000)
}
$('mic').addEventListener('click', talk)
$('reactor-hit').addEventListener('click', talk)

$('btn-mute').addEventListener('click', () => {
  voiceOn = !voiceOn
  store.set('jarvis.voice', voiceOn)
  if (!voiceOn) mouth.stop()
  renderMute()
})
$('btn-settings').addEventListener('click', openSettings)
$('btn-new').addEventListener('click', async () => {
  await api('/api/new-session', { method: 'POST' })
  loadHistory()
  $('answer').replaceChildren()
  $('cards').replaceChildren()
  toast('Conversation', 'Started a fresh conversation. Memories are kept.')
})
$('settings-form').addEventListener('submit', (e) => {
  if (e.submitter?.value === 'save') saveSettings()
})
$('test-voice').addEventListener('click', () => {
  mouth.say(`Good ${partOfDay()}, ${you()}. Voice systems online.`, { voice: $('voice-select').value, followUp: false })
})
$('note-form').addEventListener('submit', async (e) => {
  e.preventDefault()
  const input = $('note-input')
  if (!input.value.trim()) return
  renderNotes(await api('/api/notes', { method: 'POST', body: { text: input.value.trim() } }))
  input.value = ''
})

document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName) || $('settings').open
  if (e.key === 'Escape') { mouth.stop(); ears.attentiveUntil = 0 }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return
  if (e.code === 'Space' && !e.repeat) { e.preventDefault(); talk() }
  else if (e.key === 'm' || e.key === 'M') $('btn-mute').click()
  else if (e.key === '/') { e.preventDefault(); $('q').focus() }
})

const HINTS = ['"Jarvis, good morning"', '"what\'s the weather in London"', '"open Spotify"', '"play lo-fi beats"',
  '"set a timer for 5 minutes"', '"remind me to call mom at 6pm"', '"who is on my wifi"', '"system status"',
  '"find my resume"', '"remember that my car is on level 3"', '"what\'s 18% of 2450"', '"latest news about AI"',
  '"volume to 30"', '"lock the computer"', '"add milk to my list"']
setInterval(() => {
  const pick = HINTS.slice().sort(() => Math.random() - 0.5).slice(0, 4)
  const lead = { wake: 'Say "Jarvis" then: ', tap: `${MOBILE ? 'Tap' : 'Press Space or'} the mic, then: `, type: 'Type: ' }[pickMode()]
  $('hint').textContent = `${lead}${pick.join(' · ')}`
}, 12000)

function partOfDay() {
  const hr = new Date().getHours()
  return hr >= 5 && hr < 12 ? 'morning' : hr < 17 && hr >= 12 ? 'afternoon' : 'evening'
}

/* ---------------- boot ---------------- */
async function boot() {
  const log = $('boot-log')
  const line = async (text, ms = 260) => { log.textContent += `${text}\n`; await new Promise((r) => setTimeout(r, ms)) }
  tickClock()
  await line('> Initialising J.A.R.V.I.S. core…')
  let health = null
  try { health = await api('/api/health') } catch { /* offline */ }
  try { settings = { ...settings, ...(await api('/api/settings')).settings } } catch { /* offline */ }
  if (!health) {
    await line('> Core offline. Run start.bat and reload.', 0)
    $('boot-go').textContent = 'Core offline'
    return
  }
  const cloud = health.provider === 'groq'
  await line(`> Neural engine: ${cloud ? `${health.cloud_model} (Groq cloud), local ${health.model} as backup`
    : health.ollama ? `${health.model} ${health.model_installed ? 'online (local)' : 'NOT INSTALLED'}` : 'Ollama not running'}`)
  if (cloud) {
    chip('chip-brain', 'BRAIN: GROQ', 'ok')
  } else if (!health.ollama) {
    $('banner').dataset.health = '1'
    showBanner('Ollama isn\'t running, so JARVIS can only do quick commands. Start Ollama, then reload.')
  } else if (!health.model_installed) {
    $('banner').dataset.health = '1'
    showBanner(`The model ${health.model} isn't installed. In a terminal run: ollama pull ${health.model}`)
  }
  if (!cloud) {
    chip('chip-brain', health.ollama && health.model_installed ? `BRAIN: ${health.model.toUpperCase()}` : 'BRAIN: OFFLINE',
      health.ollama && health.model_installed ? 'ok' : 'bad')
  }
  const mode = pickMode()
  await line(`> Voice input: ${{ wake: 'always listening for "Jarvis"', tap: 'tap to talk, understood on your PC',
    type: !window.isSecureContext ? 'off (the microphone needs the https link); type commands' : 'no microphone support here; type commands' }[mode]}`)
  await line('> Telemetry, memory banks, schedule… linked')
  await Promise.all([refresh(), loadHistory(), loadMemory()])
  await line('> All systems operational.', 120)
  renderMute()
  listen()
  const go = $('boot-go')
  go.disabled = false
  go.textContent = 'Activate'
  go.focus()
  go.addEventListener('click', () => {
    audioCtx = new AudioContext()
    // Phones only let a page play sound after a tap; playing silence during this tap unlocks it for later.
    const unlock = audioCtx.createBufferSource()
    unlock.buffer = audioCtx.createBuffer(1, 1, 22050)
    unlock.connect(audioCtx.destination)
    unlock.start(0)
    $('boot').classList.add('gone')
    applyMode()
    chime(520, 0.1, 2)
    const name = settings.user_name ? `, ${settings.user_name}` : `, ${you()}`
    mouth.say(`Good ${partOfDay()}${name}. JARVIS online. All systems operational.`, { followUp: false })
  }, { once: true })
}
boot()
