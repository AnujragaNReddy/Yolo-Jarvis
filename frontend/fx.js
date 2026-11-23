// Visual effects: themes, particle background, network radar, CPU/RAM history lines, headline ticker.
const FX = (() => {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches
  const THEMES = {
    jarvis: { name: 'JARVIS', glow: [0, 216, 255], bright: [108, 233, 255], think: [255, 181, 71], speak: [61, 255, 220], warm: [255, 181, 71] },
    mark: { name: 'MARK III', glow: [255, 74, 74], bright: [255, 206, 102], think: [255, 206, 102], speak: [255, 150, 100], warm: [255, 206, 102] },
    friday: { name: 'FRIDAY', glow: [176, 124, 255], bright: [218, 196, 255], think: [255, 150, 205], speak: [140, 220, 255], warm: [255, 150, 205] },
    vision: { name: 'VISION', glow: [61, 255, 168], bright: [178, 255, 224], think: [255, 214, 92], speak: [120, 205, 255], warm: [255, 214, 92] },
  }
  const order = Object.keys(THEMES)
  let theme = THEMES.jarvis
  const rgb = (c, a = 1) => `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${a})`

  function setTheme(key) {
    const t = THEMES[key] || THEMES.jarvis
    theme = t
    const s = document.documentElement.style
    s.setProperty('--glow-rgb', t.glow.join(', '))
    s.setProperty('--cyan', rgb(t.glow))
    s.setProperty('--cyan-2', rgb(t.bright))
    s.setProperty('--cyan-dim', rgb(t.glow, 0.55))
    s.setProperty('--panel-edge', rgb(t.glow, 0.22))
    s.setProperty('--line', rgb(t.glow, 0.13))
    s.setProperty('--amber', rgb(t.warm))
    document.documentElement.dataset.theme = key
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#02060d')
    // Reactor is a top-level const, which isn't a property of window.
    if (typeof Reactor !== 'undefined') Reactor.setPalette({ standby: t.glow, active: t.bright, thinking: t.think, speaking: t.speak })
    const btn = document.getElementById('btn-theme')
    if (btn) btn.textContent = t.name
    try { localStorage.setItem('jarvis.theme', key) } catch { /* private mode */ }
  }
  function nextTheme() {
    const cur = document.documentElement.dataset.theme || 'jarvis'
    setTheme(order[(order.indexOf(cur) + 1) % order.length])
  }

  /* particle field */
  const bg = document.getElementById('bg')
  const bctx = bg.getContext('2d')
  let parts = []
  function sizeBg() {
    const dpr = Math.min(devicePixelRatio || 1, 1.5)
    bg.width = innerWidth * dpr
    bg.height = innerHeight * dpr
    const n = Math.round(Math.min(70, (innerWidth * innerHeight) / 22000))
    parts = Array.from({ length: n }, () => ({
      x: Math.random() * bg.width, y: Math.random() * bg.height,
      vx: (Math.random() - 0.5) * 0.25 * dpr, vy: (Math.random() - 0.5) * 0.25 * dpr, r: (Math.random() * 1.4 + 0.4) * dpr,
    }))
  }
  function drawBg() {
    if (!document.hidden) {
      bctx.clearRect(0, 0, bg.width, bg.height)
      const link = 120 * (bg.width / innerWidth)
      for (const p of parts) {
        if (!reduced) { p.x += p.vx; p.y += p.vy }
        if (p.x < 0 || p.x > bg.width) p.vx *= -1
        if (p.y < 0 || p.y > bg.height) p.vy *= -1
      }
      for (let i = 0; i < parts.length; i++) {
        const a = parts[i]
        for (let j = i + 1; j < parts.length; j++) {
          const b = parts[j]
          const d = Math.hypot(a.x - b.x, a.y - b.y)
          if (d < link) {
            bctx.strokeStyle = rgb(theme.glow, 0.12 * (1 - d / link))
            bctx.lineWidth = 1
            bctx.beginPath(); bctx.moveTo(a.x, a.y); bctx.lineTo(b.x, b.y); bctx.stroke()
          }
        }
        bctx.fillStyle = rgb(theme.bright, 0.55)
        bctx.beginPath(); bctx.arc(a.x, a.y, a.r, 0, Math.PI * 2); bctx.fill()
      }
    }
    if (!reduced) requestAnimationFrame(drawBg)
  }

  /* radar: one blip per online device, new ones in the warm colour */
  const radar = document.getElementById('radar')
  let blips = []
  function setRadar(online, fresh) {
    if (!radar) return
    const want = Math.min(online, 40)
    while (blips.length < want) blips.push({ a: Math.random() * Math.PI * 2, r: 0.25 + Math.random() * 0.68, hot: 0 })
    blips.length = want
    blips.forEach((b, i) => { b.fresh = i < fresh })
  }
  function drawRadar() {
    if (radar && !document.hidden && radar.offsetParent) {
      const dpr = Math.min(devicePixelRatio || 1, 2)
      const w = radar.clientWidth * dpr
      if (radar.width !== w) { radar.width = w; radar.height = w }
      const c = radar.getContext('2d')
      const cx = w / 2
      const R = w / 2 - 2 * dpr
      const sweep = (performance.now() / 2600) * Math.PI * 2
      c.clearRect(0, 0, w, w)
      c.strokeStyle = rgb(theme.glow, 0.25)
      c.lineWidth = dpr
      for (const k of [0.33, 0.66, 1]) { c.beginPath(); c.arc(cx, cx, R * k, 0, Math.PI * 2); c.stroke() }
      c.beginPath(); c.moveTo(cx - R, cx); c.lineTo(cx + R, cx); c.moveTo(cx, cx - R); c.lineTo(cx, cx + R); c.stroke()
      if (!reduced) {
        const g = c.createConicGradient ? c.createConicGradient(sweep - 0.9, cx, cx) : null
        if (g) {
          g.addColorStop(0, rgb(theme.glow, 0)); g.addColorStop(0.14, rgb(theme.glow, 0.35)); g.addColorStop(0.145, rgb(theme.glow, 0))
          c.fillStyle = g
          c.beginPath(); c.arc(cx, cx, R, 0, Math.PI * 2); c.fill()
        }
      }
      for (const b of blips) {
        const diff = ((sweep - b.a) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2)
        if (diff < 0.08 || reduced) b.hot = 1
        b.hot *= 0.985
        const col = b.fresh ? theme.warm : theme.bright
        c.fillStyle = rgb(col, 0.25 + b.hot * 0.75)
        c.beginPath(); c.arc(cx + Math.cos(b.a) * R * b.r, cx + Math.sin(b.a) * R * b.r, (2 + b.hot * 1.5) * dpr, 0, Math.PI * 2); c.fill()
      }
    }
    if (!reduced) requestAnimationFrame(drawRadar)
    else setTimeout(drawRadar, 2000)
  }

  /* history lines: one series per line, the label names it */
  const hist = { cpu: [], ram: [] }
  function spark(cpu, ram) {
    for (const [k, v] of [['cpu', cpu], ['ram', ram]]) {
      if (v == null) continue
      hist[k].push(v)
      if (hist[k].length > 60) hist[k].shift()
      const cv = document.getElementById(`spark-${k}`)
      if (!cv) continue
      const dpr = Math.min(devicePixelRatio || 1, 2)
      const w = cv.clientWidth * dpr
      const hgt = cv.clientHeight * dpr
      cv.width = w; cv.height = hgt
      const c = cv.getContext('2d')
      const data = hist[k]
      const x = (i) => (i / 59) * (w - 4 * dpr) + 2 * dpr
      const y = (val) => hgt - 3 * dpr - (val / 100) * (hgt - 6 * dpr)
      const off = 60 - data.length
      c.beginPath()
      data.forEach((val, i) => (i ? c.lineTo(x(i + off), y(val)) : c.moveTo(x(i + off), y(val))))
      c.strokeStyle = rgb(theme.glow)
      c.lineWidth = 2 * dpr
      c.lineJoin = 'round'
      c.stroke()
      c.lineTo(x(59), hgt); c.lineTo(x(off), hgt); c.closePath()
      const fill = c.createLinearGradient(0, 0, 0, hgt)
      fill.addColorStop(0, rgb(theme.glow, 0.25)); fill.addColorStop(1, rgb(theme.glow, 0))
      c.fillStyle = fill
      c.fill()
      const last = data[data.length - 1]
      c.fillStyle = rgb(theme.bright)
      c.beginPath(); c.arc(x(59), y(last), 3 * dpr, 0, Math.PI * 2); c.fill()
      const label = document.getElementById(`spark-${k}-v`)
      if (label) label.textContent = `${Math.round(last)}%`
    }
  }

  /* headline ticker */
  let tickerKey = ''
  function ticker(news) {
    const el = document.getElementById('ticker-track')
    if (!el || !news?.length) return
    const key = news.map((n) => n.title).join('|')
    if (key === tickerKey) return
    tickerKey = key
    el.replaceChildren()
    for (let copy = 0; copy < 2; copy++) {
      for (const n of news) {
        const a = document.createElement('a')
        if (/^https?:\/\//.test(n.link)) a.href = n.link
        a.target = '_blank'
        a.rel = 'noopener noreferrer'
        a.textContent = n.title
        if (copy) a.setAttribute('aria-hidden', 'true')
        const src = document.createElement('small')
        src.textContent = n.source || ''
        a.append(' ', src)
        el.append(a)
      }
    }
    el.style.animationDuration = `${Math.max(40, news.length * 12)}s`
  }

  function init() {
    let saved = 'jarvis'
    try { saved = localStorage.getItem('jarvis.theme') || 'jarvis' } catch { /* private mode */ }
    setTheme(saved)
    sizeBg()
    addEventListener('resize', sizeBg)
    requestAnimationFrame(drawBg)
    drawRadar()
    document.querySelectorAll('.panel').forEach((p, i) => p.style.setProperty('--i', i))
  }

  return { init, setTheme, nextTheme, setRadar, spark, ticker }
})()
