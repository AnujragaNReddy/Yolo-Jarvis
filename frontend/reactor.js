// The arc reactor: rings that spin with JARVIS's state, and a core and sound ring driven by his voice.
const Reactor = (() => {
  const canvas = document.getElementById('reactor')
  const ctx = canvas.getContext('2d')
  const COLORS = {
    standby: [0, 216, 255], active: [108, 233, 255], thinking: [255, 181, 71], speaking: [61, 255, 220], off: [71, 107, 128],
  }
  let state = 'standby'
  let level = 0
  let target = 0
  let color = [...COLORS.standby]
  let spin = 1
  let t = 0
  const bars = new Array(72).fill(0)
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches

  function size() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const w = canvas.clientWidth || 460
    if (canvas.width !== Math.round(w * dpr)) {
      canvas.width = Math.round(w * dpr)
      canvas.height = Math.round(w * dpr)
    }
    return { w: canvas.width, dpr }
  }

  const rgba = (a) => `rgba(${color[0] | 0}, ${color[1] | 0}, ${color[2] | 0}, ${a})`

  function ring(cx, r, width, alpha, dash, rot) {
    ctx.save()
    ctx.translate(cx, cx)
    ctx.rotate(rot)
    ctx.beginPath()
    ctx.setLineDash(dash || [])
    ctx.arc(0, 0, r, 0, Math.PI * 2)
    ctx.lineWidth = width
    ctx.strokeStyle = rgba(alpha)
    ctx.stroke()
    ctx.restore()
  }

  function arcs(cx, r, width, alpha, parts, gap, rot) {
    ctx.save()
    ctx.translate(cx, cx)
    ctx.rotate(rot)
    const span = (Math.PI * 2) / parts
    for (let i = 0; i < parts; i++) {
      ctx.beginPath()
      ctx.arc(0, 0, r, i * span + gap / 2, (i + 1) * span - gap / 2)
      ctx.lineWidth = width
      ctx.strokeStyle = rgba(alpha * (i % 2 ? 0.55 : 1))
      ctx.stroke()
    }
    ctx.restore()
  }

  function draw() {
    const { w, dpr } = size()
    const cx = w / 2
    const R = w * 0.47
    t += reduced ? 0 : 1 / 60
    level += (target - level) * 0.25
    target *= 0.92
    const want = COLORS[state] || COLORS.standby
    for (let i = 0; i < 3; i++) color[i] += (want[i] - color[i]) * 0.08
    const wantSpin = state === 'thinking' ? 4 : state === 'active' ? 1.8 : state === 'speaking' ? 1.4 : 1
    spin += (wantSpin - spin) * 0.05
    const s = t * spin

    ctx.clearRect(0, 0, w, w)
    ctx.shadowColor = rgba(0.9)
    ctx.shadowBlur = 12 * dpr

    // outer tick ring
    ctx.save()
    ctx.translate(cx, cx)
    ctx.rotate(s * 0.08)
    for (let i = 0; i < 120; i++) {
      const a = (i / 120) * Math.PI * 2
      const long = i % 10 === 0
      ctx.beginPath()
      ctx.moveTo(Math.cos(a) * R, Math.sin(a) * R)
      ctx.lineTo(Math.cos(a) * (R - (long ? 14 : 6) * dpr), Math.sin(a) * (R - (long ? 14 : 6) * dpr))
      ctx.lineWidth = (long ? 2 : 1) * dpr
      ctx.strokeStyle = rgba(long ? 0.8 : 0.35)
      ctx.stroke()
    }
    ctx.restore()

    ring(cx, R * 0.93, 1 * dpr, 0.35)
    arcs(cx, R * 0.86, 4 * dpr, 0.85, 3, 0.9, s * 0.5)
    arcs(cx, R * 0.8, 2 * dpr, 0.5, 8, 0.35, -s * 0.8)
    ring(cx, R * 0.74, 1.5 * dpr, 0.45, [4 * dpr, 7 * dpr], s * 0.3)

    // sound ring
    ctx.save()
    ctx.translate(cx, cx)
    const n = bars.length
    for (let i = 0; i < n; i++) {
      const wave = (Math.sin(i * 0.55 + t * 6) + Math.sin(i * 0.23 - t * 3.3)) * 0.25 + 0.5
      const live = state === 'speaking' || state === 'active' ? level : level * 0.3
      bars[i] += ((0.08 + live * wave * 1.4) - bars[i]) * 0.3
      const a = (i / n) * Math.PI * 2 + s * 0.15
      const r0 = R * 0.56
      const r1 = r0 + bars[i] * R * 0.14
      ctx.beginPath()
      ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0)
      ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1)
      ctx.lineWidth = 3 * dpr
      ctx.lineCap = 'round'
      ctx.strokeStyle = rgba(0.35 + bars[i] * 0.6)
      ctx.stroke()
    }
    ctx.restore()

    // reactor coils
    ctx.save()
    ctx.translate(cx, cx)
    ctx.rotate(-s * 0.12)
    const coils = 10
    for (let i = 0; i < coils; i++) {
      const a0 = (i / coils) * Math.PI * 2 + 0.07
      const a1 = ((i + 1) / coils) * Math.PI * 2 - 0.07
      ctx.beginPath()
      ctx.arc(0, 0, R * 0.47, a0, a1)
      ctx.arc(0, 0, R * 0.34, a1 - 0.03, a0 + 0.03, true)
      ctx.closePath()
      ctx.fillStyle = rgba(0.1 + level * 0.25)
      ctx.strokeStyle = rgba(0.75)
      ctx.lineWidth = 1.5 * dpr
      ctx.fill()
      ctx.stroke()
    }
    ctx.restore()

    // triangle
    ctx.save()
    ctx.translate(cx, cx)
    ctx.rotate(s * 0.2)
    ctx.beginPath()
    for (let i = 0; i < 3; i++) {
      const a = (i / 3) * Math.PI * 2 - Math.PI / 2
      ctx.lineTo(Math.cos(a) * R * 0.27, Math.sin(a) * R * 0.27)
    }
    ctx.closePath()
    ctx.strokeStyle = rgba(0.6)
    ctx.lineWidth = 2 * dpr
    ctx.stroke()
    ctx.restore()

    // core
    const pulse = state === 'standby' ? 0.04 * Math.sin(t * 2) : 0
    const cr = R * (0.2 + level * 0.07 + pulse)
    const g = ctx.createRadialGradient(cx, cx, 0, cx, cx, cr * 1.9)
    g.addColorStop(0, 'rgba(255,255,255,0.95)')
    g.addColorStop(0.25, rgba(0.95))
    g.addColorStop(0.55, rgba(0.35))
    g.addColorStop(1, rgba(0))
    ctx.shadowBlur = 30 * dpr
    ctx.fillStyle = g
    ctx.beginPath()
    ctx.arc(cx, cx, cr * 1.9, 0, Math.PI * 2)
    ctx.fill()

    requestAnimationFrame(draw)
  }
  requestAnimationFrame(draw)

  return {
    setState(next) { state = next },
    pushLevel(v) { target = Math.max(target, Math.min(1, v)) },
  }
})()
