/* Deskline office: your agent team as a pixel-art building.
   Truth rule: every word in a bubble and every number on a wall comes from
   /agents/office.json (real runs, real approvals, real money). Only movement,
   like walking to the elevator or a coffee run, is decoration. */
(() => {
  "use strict";
  if (window.DesklineOffice) { window.DesklineOffice.mount(); return; }

  /* ================================================================ helpers */
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const hexToRgb = (c) => { const n = parseInt(c.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
  const toHex = (a) => "#" + a.map((v) => Math.round(clamp(v, 0, 255)).toString(16).padStart(2, "0")).join("");
  const mix = (a, b, t) => { const x = hexToRgb(a), y = hexToRgb(b); return toHex(x.map((v, i) => lerp(v, y[i], t))); };
  const shade = (c, amt) => mix(c, amt < 0 ? "#000000" : "#ffffff", Math.abs(amt));
  function hash(s) { let h = 2166136261; for (const ch of String(s)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
  function seeded(seed) { let a = seed >>> 0; return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
  const money = (cents) => "$" + Math.round((cents || 0) / 100).toLocaleString("en-US");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const now = () => performance.now() / 1000;

  /* ============================================================ pixel font */
  // 3×5 glyphs, rows top to bottom. Used for in-world signage (scoreboard, plates).
  const GLYPHS = {
    A: "010101111101101", B: "110101110101110", C: "011100100100011", D: "110101101101110", E: "111100110100111",
    F: "111100110100100", G: "011100101101011", H: "101101111101101", I: "111010010010111", J: "001001001101010",
    K: "101101110101101", L: "100100100100111", M: "101111111101101", N: "110101101101101", O: "010101101101010",
    P: "110101110100100", Q: "010101101110011", R: "110101110101101", S: "011100010001110", T: "111010010010010",
    U: "101101101101111", V: "101101101101010", W: "101101111111101", X: "101101010101101", Y: "101101010010010",
    Z: "111001010100111", 0: "111101101101111", 1: "010110010010111", 2: "110001010100111", 3: "110001010001110",
    4: "101101111001001", 5: "111100110001110", 6: "011100111101111", 7: "111001010010010", 8: "111101111101111",
    9: "111101111001110", $: "011110111011110", ",": "000000000010100", ".": "000000000000010", ":": "000010000010000",
    "-": "000000111000000", "/": "001001010100100", "%": "101001010100101", "+": "000010111010000", "!": "010010010000010",
    "?": "110001010000010", "'": "010010000000000", "#": "101111101111101", " ": "000000000000000", "&": "010101010101011",
  };
  const pixelWidth = (text, s = 1) => Math.max(0, String(text).length * 4 * s - s);
  function pixelText(c, text, x, y, color, s = 1) {
    c.fillStyle = color;
    let cx = Math.round(x);
    for (const ch of String(text).toUpperCase()) {
      const g = GLYPHS[ch] || GLYPHS[" "];
      for (let i = 0; i < 15; i++) if (g[i] === "1") c.fillRect(cx + (i % 3) * s, Math.round(y) + ((i / 3) | 0) * s, s, s);
      cx += 4 * s;
    }
  }

  /* ============================================================== drawing */
  let ctx = null; // current world context for R()
  function R(x, y, w, h, col) { ctx.fillStyle = col; ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)); }
  function disc(cx, cy, r, col) { for (let dy = -r; dy <= r; dy++) { const w = Math.floor(Math.sqrt(r * r - dy * dy)); R(cx - w, cy + dy, 2 * w + 1, 1, col); } }
  function line(x0, y0, x1, y1, col) {
    x0 = Math.round(x0); y0 = Math.round(y0); x1 = Math.round(x1); y1 = Math.round(y1);
    const dx = Math.abs(x1 - x0), dy = -Math.abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    let err = dx + dy;
    for (let i = 0; i < 200; i++) { R(x0, y0, 1, 1, col); if (x0 === x1 && y0 === y1) break; const e2 = 2 * err; if (e2 >= dy) { err += dy; x0 += sx; } if (e2 <= dx) { err += dx; y0 += sy; } }
  }

  /* ============================================================== people */
  // Original characters, one per role. Facing right; mirrored when walking left.
  const LOOKS = {
    prospector: { skin: "#e0ac7e", hair: "#5b3a1e", shirt: "#7a8b3a", pants: "#4b4136", shoes: "#2b2118", acc: ["cap"], cap: "#c0563b" },
    demo_builder: { skin: "#a86b45", hair: "#1f1612", shirt: "#e07b2e", pants: "#2f4a7a", shoes: "#2a2a2a", acc: ["hardhat"] },
    outreach: { skin: "#f1c9a5", hair: "#c58b3a", shirt: "#3f6fc4", pants: "#2d3442", shoes: "#3a2a1a", acc: ["satchel"] },
    closer: { skin: "#8d5a3b", hair: "#141414", shirt: "#2c3550", pants: "#22283a", shoes: "#111111", acc: ["tie"] },
    quality_coach: { skin: "#f0c08f", hair: "#7b2f2a", shirt: "#7b5bd6", pants: "#3a3150", shoes: "#222222", acc: ["ponytail", "headset"] },
    account_manager: { skin: "#c98e62", hair: "#b9b9b9", shirt: "#2c9a8f", pants: "#4a4038", shoes: "#3b2b20", acc: ["glasses"] },
    billing_guardian: { skin: "#ffd9b8", hair: "#6b4a2a", shirt: "#f2f2ea", vest: "#4e6b3a", pants: "#333333", shoes: "#222222", acc: ["vest", "visor"] },
    ops_monitor: { skin: "#6e4630", hair: "#20160f", shirt: "#4b5563", pants: "#2a2f37", shoes: "#dddddd", acc: ["hood", "headset"] },
    daily_brief: { skin: "#e8b48c", hair: "#3b2a20", shirt: "#d9cfb8", pants: "#5a4a3a", shoes: "#2a1d14", acc: ["newsboy", "beard"] },
    strategist: { skin: "#d9a066", hair: "#2b2b33", shirt: "#8a3b5b", pants: "#2d2a36", shoes: "#1d1a22", acc: ["bun", "glasses"] },
  };
  const SPR_W = 16, SPR_H = 22;
  const spriteCache = new Map();
  function sprite(key, pose, frame = 0) {
    const id = `${key}|${pose}|${frame}`;
    let cv = spriteCache.get(id);
    if (!cv) {
      cv = document.createElement("canvas"); cv.width = SPR_W; cv.height = SPR_H;
      const prev = ctx; ctx = cv.getContext("2d"); ctx.translate(0, 2);
      drawPerson(LOOKS[key] || LOOKS.prospector, pose, frame);
      ctx = prev; spriteCache.set(id, cv);
    }
    return cv;
  }
  function drawPerson(L, pose, frame) {
    const seated = pose === "sit" || pose === "type" || pose === "sleep";
    const shirtD = shade(L.shirt, -0.22), skinD = shade(L.skin, -0.18), pantsD = shade(L.pants, -0.25);
    const acc = new Set(L.acc || []);
    const hy = seated ? 3 : 1;           // head top row
    const ty = hy + 7;                   // torso top row
    const th = seated ? 4 : 6;           // torso height
    const lean = pose === "sleep" ? 1 : 0;
    // legs
    if (seated) {
      R(4, 13, 8, 2, L.pants); R(10, 15, 2, 4, pantsD); R(10, 19, 3, 1, L.shoes);
    } else {
      R(4, 14, 6, 1, pantsD);
      const frames = [[[5, 4], [8, 9]], [[5, 5], [7, 7]], [[8, 9], [5, 4]], [[5, 5], [7, 7]]];
      const [far, near] = pose === "walk" ? frames[frame % 4] : [[5, 5], [7, 7]];
      const leg = (xs, col, shoe) => { R(xs[0], 15, 2, 2, col); R(xs[1], 17, 2, 2, col); R(xs[1], 19, 3, 1, shoe); };
      leg(far, pantsD, shade(L.shoes, -0.2)); leg(near, L.pants, L.shoes);
    }
    // behind the body
    if (acc.has("hood")) R(3, hy + 1, 2, 7, shirtD);
    if (acc.has("ponytail")) { R(1, hy + 1, 3, 3, L.hair); R(2, hy + 4, 2, 2, L.hair); }
    if (acc.has("bun")) { R(1, hy - 1 + (pose === "sleep" ? 3 : 0), 3, 3, L.hair); }
    if (pose === "cheer") { R(2, ty - 6, 2, 7, shirtD); R(2, ty - 7, 2, 1, L.skin); }
    // torso
    R(4 + lean, ty, 6, th, L.shirt); R(4 + lean, ty, 1, th, shirtD);
    if (acc.has("vest")) { R(5 + lean, ty, 4, th, L.vest); R(8 + lean, ty, 1, 2, L.shirt); }
    if (acc.has("tie")) { R(9, ty, 1, 1, "#f2f2f2"); R(9, ty + 1, 1, th - 2, "#c43b3b"); }
    if (acc.has("satchel")) { for (let i = 0; i < th; i++) R(5 + Math.min(i, 4), ty + i, 1, 1, "#7a4a24"); R(2, ty + 3, 3, 3, "#8b5a2b"); }
    // head (sleeping: forward and down onto the desk)
    const hx = pose === "sleep" ? 4 : 0, hd = pose === "sleep" ? 3 : 0;
    const H = hy + hd;
    R(4 + hx, H, 6, 6, L.skin);
    R(10 + hx, H + 3, 1, 1, L.skin);
    R(6 + hx, H + 2, 1, 2, skinD);
    if (pose === "sleep") R(8 + hx, H + 3, 2, 1, skinD); else R(8 + hx, H + 2, 1, 1, "#1d1a24");
    R(9 + hx, H + 4, 1, 1, pose === "cheer" ? "#6b2a2a" : skinD);
    R(3 + hx, H - 1, 7, 2, L.hair); R(3 + hx, H + 1, 3, 3, L.hair); R(9 + hx, H, 1, 1, L.hair);
    if (acc.has("cap")) { R(3 + hx, H - 1, 7, 2, L.cap); R(10 + hx, H, 3, 1, shade(L.cap, -0.25)); }
    if (acc.has("hardhat")) { R(3 + hx, H - 2, 7, 3, "#f2c230"); R(2 + hx, H, 10, 1, "#d9a514"); R(5 + hx, H - 2, 2, 1, "#ffe27a"); }
    if (acc.has("visor")) { R(4 + hx, H, 6, 1, "#2f8f4e"); R(10 + hx, H + 1, 2, 1, "#5fcf86"); }
    if (acc.has("newsboy")) { R(3 + hx, H - 1, 8, 2, "#6b6f78"); R(10 + hx, H + 1, 2, 1, "#4a4d54"); }
    if (acc.has("headset")) { R(5 + hx, H - 1, 3, 1, "#22252b"); R(5 + hx, H + 2, 2, 2, "#22252b"); R(7 + hx, H + 4, 3, 1, "#22252b"); }
    if (acc.has("glasses")) { R(7 + hx, H + 1, 3, 2, "#1d1d1d"); R(8 + hx, H + 1, 1, 1, "#cfe8ff"); }
    if (acc.has("beard")) { R(7 + hx, H + 4, 3, 2, L.hair); R(6 + hx, H + 3, 1, 3, L.hair); }
    // near arm
    if (pose === "type" || pose === "sit") {
      R(6, ty, 2, 2, shirtD); R(8, ty + 1, 4, 1, shirtD);
      R(12, ty + 1 - (pose === "type" && frame % 2 ? 1 : 0), 1, 1, L.skin);
    } else if (pose === "sleep") {
      R(7, ty + 1, 6, 1, shirtD); R(13, ty + 1, 1, 1, L.skin);
    } else if (pose === "paper") {
      R(6, ty + 1, 2, 2, shirtD); R(8, ty + 2, 2, 1, shirtD); R(10, ty + 2, 1, 1, L.skin);
      R(10, ty - 2, 4, 5, "#fbfbf7"); R(11, ty - 1, 2, 1, "#9aa3b2"); R(11, ty + 1, 2, 1, "#9aa3b2");
    } else if (pose === "cup") {
      R(6, ty + 1, 2, 2, shirtD); R(8, ty + 2, 2, 1, shirtD); R(10, ty + 2, 1, 1, L.skin);
      R(10, ty, 3, 3, "#f4f1ea"); R(13, ty + 1, 1, 1, "#f4f1ea"); R(10, ty, 3, 1, "#6b4226");
    } else if (pose === "cheer") {
      R(11, ty - 6, 2, 7, shirtD); R(11, ty - 7, 2, 1, L.skin);
    } else if (pose === "walk") {
      const sw = [1, 0, -1, 0][frame % 4]; R(6 + sw, ty + 1, 2, 4, shirtD); R(6 + sw, ty + 5, 2, 1, L.skin);
    } else { R(6, ty + 1, 2, 4, shirtD); R(6, ty + 5, 2, 1, L.skin); }
  }

  /* ============================================================== layout */
  const RW = 160, RH = 84, SLAB = 6, P = 4, SHAFT = 30, WALL = 6, ROOF = 20, SKY = 40, GROUND = 34, DOOR_H = 30;
  const FH = RH + SLAB;
  const LAYOUTS = {
    // top floor first; "|" is the elevator shaft
    wide: [["prospector", "demo_builder", "|", "outreach", "closer"],
      ["quality_coach", "account_manager", "|", "daily_brief", "ops_monitor"],
      ["lobby", "strategist", "|", "you", "billing_guardian"]],
    narrow: [["prospector", "|", "demo_builder"], ["outreach", "|", "closer"], ["quality_coach", "|", "account_manager"],
      ["daily_brief", "|", "ops_monitor"], ["strategist", "|", "billing_guardian"], ["lobby", "|", "you"]],
  };
  const ROOM_NAMES = {
    prospector: "Research", demo_builder: "Workshop", outreach: "Mail room", closer: "Sales", quality_coach: "Training",
    account_manager: "Client lounge", daily_brief: "Newsroom", ops_monitor: "Server room", billing_guardian: "Vault",
    strategist: "Strategy room", lobby: "Lobby", you: "Your office",
  };
  const ROOM_STYLE = {
    prospector: ["#d8c7a3", "wood"], demo_builder: ["#c9d1d9", "concrete"], outreach: ["#e6d3b3", "wood"],
    closer: ["#b9c6e4", "carpet:#5d6fa3"], quality_coach: ["#d6c9ec", "carpet:#7d6aae"], account_manager: ["#c6e3dc", "wood"],
    daily_brief: ["#e9e2cf", "tiles:#d8d2bf"], ops_monitor: ["#2d3a4f", "grate"], billing_guardian: ["#cfe0c3", "tiles:#b9cdb0"],
    strategist: ["#d5dbe8", "carpet:#4a5878"], lobby: ["#efe6d8", "tiles:#e3dccb"], you: ["#d9cfc3", "wood:dark"],
  };
  const AGENT_ROOMS = ["prospector", "demo_builder", "outreach", "closer", "quality_coach", "account_manager", "daily_brief", "ops_monitor", "billing_guardian", "strategist"];

  function build(name) {
    const rows = LAYOUTS[name];
    const margin = name === "wide" ? 30 : 10;
    const slots = rows[0];
    const inner = slots.reduce((w, s) => w + (s === "|" ? SHAFT : RW), 0) + P * (slots.length - 1);
    const bw = inner + WALL * 2, n = rows.length;
    const W = bw + margin * 2, H = SKY + ROOF + n * FH + GROUND;
    const groundY = H - GROUND, bx = margin;
    const rooms = {}, floors = [], doors = [];
    let shaftX = 0;
    rows.forEach((row, ri) => {
      const f = n - 1 - ri;
      const floorY = groundY - f * FH - SLAB;
      floors[f] = { f, floorY, top: floorY - RH };
      let x = bx + WALL;
      row.forEach((slot, i) => {
        if (i) { doors.push({ f, x }); x += P; }
        if (slot === "|") { shaftX = x + SHAFT / 2; x += SHAFT; return; }
        rooms[slot] = { key: slot, x, w: RW, top: floorY - RH, floorY, f, name: ROOM_NAMES[slot] };
        x += RW;
      });
    });
    return { name, W, H, bx, bw, groundY, roofY: floors[n - 1].top - SLAB, rooms, shaftX, floors, doors, n };
  }
  const feet = (f) => S.L.floors[f].floorY - 2;

  /* ================================================================= sky */
  const SKY_KEYS = [
    [0, "#0b1026", "#1b2748", 1], [5.2, "#0b1026", "#1b2748", 1], [6.4, "#2b3a6b", "#f0a46a", 0.5], [7.7, "#5aa7e0", "#bfe3f5", 0],
    [17.2, "#5aa7e0", "#bfe3f5", 0], [18.5, "#3b3f78", "#f08a5d", 0.45], [19.7, "#0b1026", "#1b2748", 1], [24, "#0b1026", "#1b2748", 1],
  ];
  function localHour() {
    try {
      const parts = new Intl.DateTimeFormat("en-US", { timeZone: S.tz || undefined, hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(new Date());
      return +parts.find((p) => p.type === "hour").value + +parts.find((p) => p.type === "minute").value / 60;
    } catch (_) { const d = new Date(); return d.getHours() + d.getMinutes() / 60; }
  }
  function skyAt(h) {
    for (let i = 0; i < SKY_KEYS.length - 1; i++) {
      const a = SKY_KEYS[i], b = SKY_KEYS[i + 1];
      if (h >= a[0] && h <= b[0]) { const t = (h - a[0]) / (b[0] - a[0] || 1); return { top: mix(a[1], b[1], t), bottom: mix(a[2], b[2], t), night: lerp(a[3], b[3], t) }; }
    }
    return { top: SKY_KEYS[0][1], bottom: SKY_KEYS[0][2], night: 1 };
  }
  function fmtTime(iso) {
    if (!iso) return "";
    try { return new Intl.DateTimeFormat("en-US", { timeZone: S.tz || undefined, hour: "numeric", minute: "2-digit" }).format(new Date(iso)); }
    catch (_) { return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
  }
  function ago(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }

  /* ========================================================= static layers */
  function makeCanvas(w, h) { const c = document.createElement("canvas"); c.width = w; c.height = h; return c; }

  function drawSkyLayer(sky) {
    const L = S.L, cv = makeCanvas(L.W, L.H); ctx = cv.getContext("2d");
    const g = ctx.createLinearGradient(0, 0, 0, L.groundY);
    g.addColorStop(0, sky.top); g.addColorStop(1, sky.bottom);
    ctx.fillStyle = g; ctx.fillRect(0, 0, L.W, L.groundY);
    const rnd = seeded(11);
    if (sky.night > 0.2) for (let i = 0; i < 70; i++) { ctx.globalAlpha = sky.night * (0.4 + rnd() * 0.6); R(rnd() * L.W, rnd() * L.groundY * 0.7, 1, 1, "#ffffff"); }
    ctx.globalAlpha = 1;
    const h = S.hour;
    if (sky.night < 0.85) {
      const t = clamp((h - 6) / 13, 0, 1), sx = lerp(L.W * 0.06, L.W * 0.94, t), sy = 30 + Math.abs(t - 0.5) * 70;
      disc(sx, sy, 7, "#ffe9a3"); disc(sx, sy, 5, "#fff4c8");
    } else {
      const t = ((h + 24 - 19.5) % 24) / 11, mx = lerp(L.W * 0.9, L.W * 0.1, clamp(t, 0, 1)), my = 26 + Math.abs(t - 0.5) * 50;
      disc(mx, my, 6, "#e9edf5"); disc(mx + 3, my - 2, 5, sky.top);
    }
    // skyline
    const far = mix(sky.bottom, "#3b4660", 0.55), near = mix(sky.bottom, "#2a3245", 0.7);
    for (const [col, seed, hmin, hmax] of [[far, 3, 40, 110], [near, 5, 20, 70]]) {
      const r = seeded(seed); let x = -10;
      while (x < L.W) {
        const w = 18 + r() * 34, hh = hmin + r() * (hmax - hmin);
        R(x, L.groundY - hh, w, hh, col);
        if (sky.night > 0.3) for (let wy = L.groundY - hh + 4; wy < L.groundY - 4; wy += 6) for (let wx = x + 3; wx < x + w - 3; wx += 5) if (r() < 0.3) R(wx, wy, 2, 2, mix(col, "#ffd27a", 0.7 * sky.night));
        x += w + 2 + r() * 8;
      }
    }
    return cv;
  }

  function drawBuildingLayer(d, sky) {
    const L = S.L, cv = makeCanvas(L.W, L.H); ctx = cv.getContext("2d");
    const ext = (c) => mix(c, "#0b1026", 0.4 * sky.night);
    // ground
    R(0, L.groundY, L.W, 8, ext("#b9bec6"));
    for (let x = 0; x < L.W; x += 16) R(x, L.groundY, 1, 8, ext("#a3a9b2"));
    R(0, L.groundY + 8, L.W, 2, ext("#8a9099"));
    R(0, L.groundY + 10, L.W, GROUND - 10, ext("#3a3f47"));
    for (let x = 4; x < L.W; x += 24) R(x, L.groundY + 21, 10, 1, ext("#c9c48a"));
    // rooms
    for (const rm of Object.values(L.rooms)) drawRoom(rm, d);
    // elevator lobby per floor
    for (const fl of L.floors) drawShaft(fl);
    // partitions with doorways
    for (const dr of L.doors) {
      const fl = L.floors[dr.f], doorTop = fl.floorY - 8 - DOOR_H;
      R(dr.x, fl.top, P, doorTop - fl.top, "#8d939c");
      R(dr.x, doorTop - 2, P, 2, "#5b4636");
      R(dr.x, doorTop, P, fl.floorY - 8 - doorTop, "#2a2f38");
      R(dr.x, fl.floorY - 8, P, 8, "#6f7682");
    }
    // slabs and outer walls
    for (const fl of L.floors) {
      R(L.bx, fl.floorY, L.bw, SLAB, ext("#9aa1ab")); R(L.bx, fl.floorY, L.bw, 1, ext("#c3c8cf")); R(L.bx, fl.floorY + SLAB - 1, L.bw, 1, ext("#6f7682"));
    }
    R(L.bx, L.roofY, L.bw, SLAB, ext("#9aa1ab")); R(L.bx, L.roofY + SLAB - 1, L.bw, 1, ext("#6f7682"));
    for (const x of [L.bx, L.bx + L.bw - WALL]) {
      R(x, L.roofY, WALL, L.groundY - L.roofY, ext("#8a4f3d"));
      for (let y = L.roofY + 3; y < L.groundY; y += 4) R(x, y, WALL, 1, ext("#734033"));
    }
    // entrance on the lobby's outer wall
    const lobby = L.rooms.lobby;
    const doorX = lobby.x <= L.bx + WALL + 1 ? L.bx : L.bx + L.bw - WALL;
    R(doorX - 1, lobby.floorY - 34, WALL + 2, 34, "#5b6470"); R(doorX, lobby.floorY - 32, WALL, 32, "#9fd3ea"); R(doorX + 1, lobby.floorY - 30, 1, 20, "#d8f0fa");
    const awX = doorX === L.bx ? L.bx - 12 : L.bx + L.bw - 6;
    for (let i = 0; i < 18; i += 3) R(awX + i, lobby.floorY - 40, 3, 4, i % 6 ? "#f2f2ea" : "#c43b3b");
    // roof: parapet, sign, antenna, AC unit
    R(L.bx - 2, L.roofY - 8, L.bw + 4, 8, ext("#8a4f3d")); R(L.bx - 3, L.roofY - 10, L.bw + 6, 2, ext("#c3c8cf"));
    const sw = clamp(Math.round(L.bw * 0.42), 120, 230), sx = Math.round(L.bx + (L.bw - sw) / 2), sy = L.roofY - 34;
    S.sign = { x: sx, y: sy, w: sw, h: 18 };
    R(sx + 10, sy + 18, 2, 6, ext("#5b6470")); R(sx + sw - 12, sy + 18, 2, 6, ext("#5b6470"));
    R(sx - 1, sy - 1, sw + 2, 20, "#3a4a6e"); R(sx, sy, sw, 18, "#1f2a44");
    for (let x = sx + 2; x < sx + sw - 1; x += 4) { R(x, sy + 1, 1, 1, "#f2c230"); R(x, sy + 16, 1, 1, "#f2c230"); }
    const ax = L.bx + L.bw - 34;
    R(ax, L.roofY - 40, 2, 30, ext("#6b7682")); R(ax - 4, L.roofY - 32, 10, 1, ext("#6b7682")); R(ax - 3, L.roofY - 24, 8, 1, ext("#6b7682"));
    S.antenna = { x: ax, y: L.roofY - 42 };
    R(L.bx + 18, L.roofY - 21, 22, 11, ext("#c3c8cf")); R(L.bx + 20, L.roofY - 19, 8, 7, ext("#9aa1ab")); R(L.bx + 30, L.roofY - 19, 8, 7, ext("#9aa1ab"));
    // street furniture (wide layout has room for it)
    if (L.name === "wide") {
      const lx = 10; R(lx, L.groundY - 46, 2, 46, ext("#3a3f47")); R(lx, L.groundY - 46, 9, 2, ext("#3a3f47")); R(lx + 6, L.groundY - 45, 5, 3, sky.night > 0.4 ? "#ffe9a3" : "#d8d2a0");
      S.lamp = { x: lx + 8, y: L.groundY - 42 };
      const mbx = doorX === L.bx ? L.bx - 22 : L.bx + L.bw + 10;
      R(mbx, L.groundY - 13, 8, 9, ext("#2f5aa8")); R(mbx + 1, L.groundY - 14, 6, 1, ext("#2f5aa8")); R(mbx + 2, L.groundY - 4, 1, 4, ext("#333")); R(mbx + 5, L.groundY - 4, 1, 4, ext("#333"));
      const tx = L.bx + L.bw + 14;
      R(tx + 7, L.groundY - 18, 3, 18, ext("#6b4a2f"));
      for (const [ox, oy, r] of [[8, -26, 9], [3, -22, 6], [13, -21, 6], [8, -33, 6]]) disc(tx + ox, L.groundY + oy, r, ext(r > 8 ? "#3f8f4a" : "#4fa65a"));
    } else S.lamp = null;
    return cv;
  }

  function drawFloor(rm, style) {
    const y = rm.floorY - 8, x = rm.x;
    const [kind, arg] = style.split(":");
    if (kind === "wood") {
      const base = arg === "dark" ? "#7a5236" : "#b5895a", alt = shade(base, -0.12);
      R(x, y, rm.w, 8, base);
      for (let i = 0; i < rm.w; i += 12) { R(x + i, y, 1, 8, alt); R(x + i + 6, y + 4, 1, 4, alt); }
      R(x, y + 4, rm.w, 1, alt);
    } else if (kind === "tiles" || kind === "checker") {
      const a = arg || "#f4efe6", b = kind === "checker" ? "#c96f5a" : shade(a, -0.08);
      for (let i = 0; i < rm.w; i += 8) for (let j = 0; j < 8; j += 4) R(x + i, y + j, 8, 4, ((i / 8 + j / 4) | 0) % 2 ? a : b);
    } else if (kind === "carpet") { R(x, y, rm.w, 8, arg); for (let i = 3; i < rm.w; i += 7) R(x + i, y + 2 + (i % 3), 1, 1, shade(arg, 0.12)); }
    else if (kind === "grate") { R(x, y, rm.w, 8, "#3a4558"); for (let i = 0; i < rm.w; i += 3) R(x + i, y, 1, 8, "#2a3242"); }
    else { R(x, y, rm.w, 8, "#a9adb3"); for (let i = 5; i < rm.w; i += 9) R(x + i, y + 3, 2, 1, "#9a9ea5"); }
  }

  function windowPane(x, y, w, h) {
    R(x - 2, y - 2, w + 4, h + 4, "#e9eef5");
    const g = ctx.createLinearGradient(0, y, 0, y + h); g.addColorStop(0, S.sky.top); g.addColorStop(1, S.sky.bottom);
    ctx.fillStyle = g; ctx.fillRect(x, y, w, h);
    R(x + 2, y + 2, 2, h - 4, "rgba(255,255,255,.25)");
    R(x + Math.floor(w / 2), y, 1, h, "#e9eef5"); R(x, y + Math.floor(h / 2), w, 1, "#e9eef5");
    R(x - 3, y + h + 2, w + 6, 2, "#cfd6de");
  }
  function plant(x, fy) {
    R(x + 1, fy - 6, 7, 6, "#b5653a"); R(x, fy - 7, 9, 2, "#c97a4a");
    R(x + 3, fy - 16, 3, 9, "#3f8f4a"); R(x, fy - 14, 3, 5, "#4fa65a"); R(x + 6, fy - 15, 3, 6, "#4fa65a"); R(x + 2, fy - 19, 2, 4, "#5fbf6a"); R(x + 5, fy - 18, 2, 3, "#3f8f4a");
  }
  function bookshelf(x, fy, w, h, seed) {
    R(x, fy - h, w, h, "#6b4a2f"); R(x + 2, fy - h + 2, w - 4, h - 4, "#4a321f");
    const r = seeded(seed), cols = ["#c0563b", "#2f6fa8", "#e6b422", "#3f8f4a", "#7b5bd6", "#d9d3c3", "#8b3b3b"];
    for (let sy = fy - h + 2; sy + 9 <= fy - 2; sy += 10) {
      let bx = x + 3;
      while (bx < x + w - 4) { const bw = 2 + (r() * 2 | 0), bh = 5 + (r() * 3 | 0); R(bx, sy + 9 - bh, bw, bh, cols[r() * cols.length | 0]); bx += bw + (r() < 0.2 ? 2 : 0); }
      R(x + 2, sy + 9, w - 4, 1, "#6b4a2f");
    }
  }
  function chairSmall(x, fy, col) { R(x, fy - 8, 9, 2, col); R(x, fy - 16, 2, 8, col); R(x + 1, fy - 6, 1, 6, "#3a3f47"); R(x + 7, fy - 6, 1, 6, "#3a3f47"); }

  // The agent's workstation: chair, desk, monitor (screen is drawn live), plus a role prop.
  function workstation(rm) {
    const x = rm.x, fy = rm.floorY - 2;
    const chair = rm.key === "closer" ? "#2b2f3a" : "#3c4656";
    R(x + 43, fy - 16, 2, 13, chair); R(x + 43, fy - 4, 11, 2, chair); R(x + 47, fy - 2, 2, 1, "#3a3f47"); R(x + 44, fy - 1, 9, 1, "#3a3f47");
    const top = rm.key === "ops_monitor" ? "#5b6470" : "#8b5e34", side = shade(top, -0.2);
    const deskEnd = rm.key === "ops_monitor" ? 100 : 90;
    R(x + 53, fy - 8, deskEnd - 53, 2, top); R(x + 54, fy - 6, 2, 6, side); R(x + deskEnd - 3, fy - 6, 2, 6, side);
    R(x + 76, fy - 6, 10, 5, side); R(x + 80, fy - 4, 2, 1, "#c9cfd6");
    R(x + 56, fy - 9, 8, 1, "#c9cfd6");
    R(x + 68, fy - 21, 14, 10, "#2b2f36"); R(x + 74, fy - 11, 2, 2, "#3a3f47"); R(x + 71, fy - 9, 8, 1, "#3a3f47");
    S.screens[rm.key] = [{ x: x + 69, y: fy - 20, w: 12, h: 8 }];
    if (rm.key === "ops_monitor") { R(x + 84, fy - 21, 14, 10, "#2b2f36"); R(x + 90, fy - 11, 2, 2, "#3a3f47"); S.screens[rm.key].push({ x: x + 85, y: fy - 20, w: 12, h: 8, chart: true }); }
    const p = {
      prospector: () => { R(x + 84, fy - 12, 3, 3, "#c9a14a"); R(x + 85, fy - 11, 1, 1, "#cfe8f5"); R(x + 87, fy - 10, 2, 1, "#6b4a2f"); },
      demo_builder: () => { R(x + 84, fy - 13, 3, 5, "#2b2f3a"); R(x + 85, fy - 12, 1, 3, "#59d18a"); },
      outreach: () => { R(x + 83, fy - 10, 6, 2, "#fbfbf3"); R(x + 83, fy - 10, 6, 1, "#d8d2c0"); },
      closer: () => { R(x + 82, fy - 11, 6, 3, "#c43b3b"); R(x + 81, fy - 12, 8, 1, "#9e2b2b"); },
      quality_coach: () => { R(x + 85, fy - 15, 1, 7, "#333"); R(x + 83, fy - 16, 5, 2, "#333"); },
      account_manager: () => { R(x + 84, fy - 11, 3, 3, "#f4f1ea"); R(x + 87, fy - 10, 1, 1, "#f4f1ea"); },
      daily_brief: () => { R(x + 82, fy - 9, 7, 1, "#e9e4d4"); R(x + 83, fy - 10, 5, 1, "#e9e4d4"); },
      billing_guardian: () => { R(x + 81, fy - 12, 7, 4, "#9aa3ad"); R(x + 82, fy - 15, 3, 3, "#fbfbf3"); R(x + 82, fy - 11, 5, 1, "#5b6470"); },
      strategist: () => { R(x + 82, fy - 9, 7, 1, "#fbfbf7"); R(x + 83, fy - 10, 1, 1, "#4cc38a"); R(x + 85, fy - 11, 1, 2, "#4cc38a"); R(x + 87, fy - 12, 1, 3, "#4cc38a"); },
    }[rm.key];
    if (p) p();
  }

  function drawRoom(rm, d) {
    const [wall, floor] = ROOM_STYLE[rm.key];
    const x = rm.x, top = rm.top, fy = rm.floorY - 2;
    R(x, top, rm.w, RH - 8, wall);
    R(x, rm.floorY - 22, rm.w, 14, shade(wall, -0.07)); R(x, rm.floorY - 22, rm.w, 1, shade(wall, -0.2));
    R(x, top, rm.w, 3, shade(wall, -0.25));
    drawFloor(rm, floor);
    R(x, rm.floorY - 9, rm.w, 1, shade(wall, -0.3));
    for (const lx of [x + 36, x + 118]) { R(lx, top + 3, 10, 2, "#5b6470"); R(lx + 2, top + 5, 6, 1, "#fff7d6"); }
    const m = d.money, pipe = m.pipeline || {};
    switch (rm.key) {
      case "prospector": {
        bookshelf(x + 6, fy, 30, 46, 3);
        const mx = x + 104, my = top + 15;
        R(mx - 2, my - 2, 48, 32, "#6b4a2f"); R(mx, my, 44, 28, "#a8d4e6");
        R(mx + 2, my + 3, 18, 10, "#cfe3b0"); R(mx + 22, my + 2, 18, 14, "#d8e8b8"); R(mx + 6, my + 15, 30, 10, "#cfe3b0");
        R(mx + 2, my + 12, 40, 1, "#f6f2e6"); R(mx + 20, my + 2, 1, 24, "#f6f2e6");
        const r = seeded(7), n = Math.min(14, m.new_prospects_24h || 0); // one pin per lead found in the last 24 h
        for (let i = 0; i < n; i++) { const px = mx + 3 + (r() * 38 | 0), py = my + 3 + (r() * 21 | 0); R(px, py, 2, 2, "#d62d2d"); R(px, py + 2, 1, 1, "#7a1212"); }
        workstation(rm); plant(x + 148, fy); break;
      }
      case "demo_builder": {
        R(x + 6, top + 14, 40, 24, "#c79a5b");
        for (let i = 2; i < 40; i += 4) for (let j = 2; j < 24; j += 4) R(x + 6 + i, top + 14 + j, 1, 1, "#9c7440");
        R(x + 11, top + 18, 2, 14, "#6b4a2f"); R(x + 9, top + 17, 6, 3, "#8a929c");
        R(x + 21, top + 18, 2, 14, "#8a929c"); R(x + 20, top + 17, 4, 2, "#8a929c");
        R(x + 28, top + 20, 12, 5, "#b9c0c8"); R(x + 38, top + 19, 4, 7, "#a0522d");
        R(x + 4, fy - 14, 36, 3, "#9c6b3c"); R(x + 6, fy - 11, 3, 11, "#7a5230"); R(x + 34, fy - 11, 3, 11, "#7a5230"); R(x + 6, fy - 5, 30, 2, "#7a5230");
        R(x + 9, fy - 19, 12, 5, "#c43b3b"); R(x + 13, fy - 21, 4, 2, "#333"); R(x + 28, fy - 18, 6, 4, "#5b6470");
        R(x + 104, top + 34, 48, 2, "#8a6a4a"); R(x + 106, top + 36, 2, 4, "#8a6a4a"); R(x + 148, top + 36, 2, 4, "#8a6a4a");
        const n = Math.min(10, pipe.demo || 0); // one handset per live demo receptionist
        for (let i = 0; i < n; i++) { const px = x + 106 + i * 4.6; R(px, top + 24, 3, 10, "#2b2f3a"); R(px + 1, top + 25, 1, 6, "#59d18a"); }
        R(x + 126, fy - 12, 14, 12, "#c9a46b"); R(x + 138, fy - 8, 14, 8, "#b8935a"); R(x + 126, fy - 7, 14, 1, "#a07a42"); R(x + 132, fy - 12, 2, 12, "#e3cf9e");
        workstation(rm); break;
      }
      case "outreach": {
        R(x + 6, top + 14, 42, 40, "#7b5a3a");
        const n = Math.min(25, d.drafts_waiting || 0); // a letter per written email waiting to go out
        for (let r = 0; r < 5; r++) for (let c = 0; c < 5; c++) {
          const cx = x + 8 + c * 8, cy = top + 16 + r * 7.6; R(cx, cy, 7, 6, "#5a3f27");
          if (r * 5 + c < n) { R(cx + 1, cy + 2, 5, 3, "#fbfbf3"); R(cx + 1, cy + 2, 5, 1, "#d8d2c0"); }
        }
        windowPane(x + 112, top + 14, 30, 20);
        R(x + 106, fy - 17, 40, 2, "#3b5a88"); R(x + 108, fy - 15, 36, 12, "#4a6fa5");
        R(x + 110, fy - 3, 3, 3, "#222"); R(x + 139, fy - 3, 3, 3, "#222"); R(x + 146, fy - 25, 2, 10, "#888");
        pixelText(ctx, "OUT", x + 120, fy - 12, "#dfe8f5");
        const sent = Math.min(18, m.emails_today || 0); // an envelope per email sent in the last 24 h
        for (let i = 0; i < sent; i++) { const ex = x + 109 + (i % 6) * 6, ey = fy - 20 - ((i / 6) | 0) * 3; R(ex, ey, 5, 3, "#fbfbf3"); R(ex, ey, 5, 1, "#c9c2ad"); }
        workstation(rm); break;
      }
      case "closer": {
        const bx = x + 6, by = top + 14;
        R(bx - 2, by - 2, 52, 34, "#8a97a6"); R(bx, by, 48, 30, "#f4f6f8");
        const keys = ["new", "demo", "replied", "won"], cols = ["#8a97a6", "#e2a43c", "#4fc1d1", "#4cc38a"];
        const max = Math.max(1, ...keys.map((k) => pipe[k] || 0));
        keys.forEach((k, i) => { const h = Math.round(((pipe[k] || 0) / max) * 18); R(bx + 4 + i * 11, by + 22 - h, 7, h, cols[i]); pixelText(ctx, k[0], bx + 6 + i * 11, by + 24, "#5b6470"); });
        R(x + 116, top + 16, 14, 2, "#5b4636"); S.bell = { x: x + 123, y: top + 18 };
        R(x + 104, top + 46, 48, 2, "#7a5a3a");
        const n = Math.min(8, m.active_clients || 0); // a trophy per paying client
        for (let i = 0; i < n; i++) { const px = x + 106 + i * 6; R(px, top + 40, 4, 3, "#e6b422"); R(px + 1, top + 43, 2, 2, "#c9971a"); R(px, top + 45, 4, 1, "#8a6a1a"); }
        plant(x + 146, fy); workstation(rm); break;
      }
      case "quality_coach": {
        R(x + 4, top + 12, 50, 34, "#b0b7c3"); R(x + 6, top + 14, 46, 30, "#fdfdfb"); R(x + 8, top + 46, 42, 2, "#9aa3b2");
        R(x + 10, top + 18, 1, 22, "#9aa3b2"); R(x + 10, top + 40, 38, 1, "#9aa3b2");
        const pts = [[12, 36], [18, 33], [24, 34], [30, 28], [36, 26], [42, 21], [47, 19]];
        for (let i = 1; i < pts.length; i++) line(x + pts[i - 1][0], top + pts[i - 1][1], x + pts[i][0], top + pts[i][1], "#7b5bd6");
        for (const cx of [x + 104, x + 120, x + 136]) chairSmall(cx, fy, "#7b5bd6");
        workstation(rm); break;
      }
      case "account_manager": {
        R(x + 12, fy - 34, 2, 33, "#444"); R(x + 7, fy - 40, 12, 7, "#f2d38a"); R(x + 8, fy - 2, 10, 2, "#444");
        plant(x + 24, fy);
        const n = Math.min(4, m.active_clients || 0); // a framed photo per paying client
        for (let i = 0; i < Math.max(1, n); i++) {
          const fx = x + 104 + i * 12, fyy = top + 16;
          R(fx, fyy, 10, 12, "#8a6a3a");
          if (i < n) { R(fx + 1, fyy + 1, 8, 10, "#dfe9f2"); R(fx + 2, fyy + 6, 6, 4, "#c9a46b"); for (let s = 0; s < 6; s += 2) R(fx + 2 + s, fyy + 4, 2, 2, ["#c43b3b", "#2f6fa8", "#3f8f4a", "#e6b422"][(hash(i) + s) % 4]); }
          else { R(fx + 1, fyy + 1, 8, 10, wall); R(fx + 4, fyy + 5, 2, 2, "#8a6a3a"); }
        }
        R(x + 102, fy - 10, 50, 8, "#2c9a8f"); R(x + 104, fy - 18, 46, 8, "#26857b"); R(x + 100, fy - 14, 5, 12, "#21766d"); R(x + 149, fy - 14, 5, 12, "#21766d");
        R(x + 127, fy - 10, 1, 6, "#21766d"); R(x + 104, fy - 2, 2, 2, "#3b2b20"); R(x + 148, fy - 2, 2, 2, "#3b2b20");
        workstation(rm); break;
      }
      case "daily_brief": {
        R(x + 4, fy - 24, 34, 22, "#5b6470"); R(x + 6, fy - 30, 30, 6, "#4a525d"); R(x + 4, fy - 2, 34, 2, "#3a3f47");
        S.press = { x: x + 8, y: fy - 28 };
        R(x + 38, fy - 10, 8, 2, "#ddd");
        R(x + 100, top + 14, 16, 20, "#f4f1e6"); R(x + 102, top + 16, 12, 3, "#3a3f47"); for (let i = 0; i < 4; i++) R(x + 102, top + 21 + i * 3, 12, 1, "#9aa3b2");
        S.clock = { x: x + 134, y: top + 24 };
        R(x + 130, fy - 8, 18, 8, "#e9e4d4"); for (let i = 0; i < 4; i++) R(x + 130, fy - 7 + i * 2, 18, 1, "#cfc8b2");
        workstation(rm); break;
      }
      case "ops_monitor": {
        R(x + 96, top + 4, 60, 2, "#3a4558");
        S.racks = [];
        for (let i = 0; i < 3; i++) {
          const rx = x + 104 + i * 17;
          R(rx, fy - 52, 16, 52, "#1b2230"); R(rx + 2, fy - 50, 12, 48, "#11161f");
          for (let y = fy - 48; y < fy - 4; y += 6) { R(rx + 2, y, 12, 4, "#262f3f"); R(rx + 7, y + 1, 5, 1, "#1b2230"); R(rx + 7, y + 3, 5, 1, "#1b2230"); }
          S.racks.push({ x: rx + 3, y: fy - 48 });
        }
        R(x + 8, fy - 14, 20, 14, "#2a3242"); R(x + 10, fy - 12, 3, 1, "#59d18a"); R(x + 32, fy - 12, 4, 10, "#c43b3b"); R(x + 33, fy - 14, 2, 2, "#333");
        workstation(rm); break;
      }
      case "billing_guardian": {
        const cx = x + 126, cy = top + 40;
        disc(cx, cy, 23, "#6b7682"); disc(cx, cy, 21, "#9aa6b2"); disc(cx, cy, 15, "#b8c2cc"); disc(cx, cy, 4, "#5b6470");
        line(cx - 11, cy, cx + 11, cy, "#5b6470"); line(cx, cy - 11, cx, cy + 11, "#5b6470"); line(cx - 8, cy - 8, cx + 8, cy + 8, "#5b6470"); line(cx - 8, cy + 8, cx + 8, cy - 8, "#5b6470");
        for (let a = 0; a < 8; a++) R(cx + Math.round(Math.cos(a * Math.PI / 4) * 18), cy + Math.round(Math.sin(a * Math.PI / 4) * 18), 2, 2, "#7d8794");
        // gold grows with money actually collected in the last 30 days (log scale)
        const h = Math.round(clamp(3 * Math.log10(1 + (m.collected_30d || 0) / 100), 0, 16));
        S.gold = { x: x + 6, y: fy, h };
        [[x + 14, h], [x + 30, Math.round(h * 0.7)], [x + 4, Math.round(h * 0.45)]].forEach(([px, ph]) => {
          for (let j = 0; j < ph; j++) { const w = (ph - j) * 2 + 2; R(px - w / 2 + 4, fy - 1 - j, w, 1, j % 2 ? "#e6b422" : "#d4a017"); }
          if (ph) R(px + 3, fy - ph, 2, 1, "#fff2a8");
        });
        for (let i = 0; i < Math.min(3, m.active_clients || 0); i++) { const bx = x + 32 + i * 0; R(bx + i * 9 - 4, fy - 8, 8, 8, "#c9a46b"); R(bx + i * 9 - 2, fy - 10, 4, 2, "#a8864d"); pixelText(ctx, "$", bx + i * 9 - 1, fy - 7, "#6b4a2f"); }
        workstation(rm); break;
      }
      case "lobby": {
        R(x + 2, fy - 1, 16, 2, "#8a3b3b");
        R(x + 22, fy - 16, 36, 16, "#b98b5e"); R(x + 20, fy - 18, 40, 3, "#8b5e34"); R(x + 24, fy - 13, 32, 1, "#a07a4f");
        R(x + 50, fy - 21, 3, 3, "#e6b422"); plant(x + 25, fy - 18);
        R(x + 72, top + 10, 82, 32, "#22262e"); R(x + 74, top + 12, 78, 28, "#0b0e14");
        S.board = { x: x + 74, y: top + 12, w: 78, h: 28 };
        // coffee corner
        R(x + 92, fy - 14, 28, 14, "#c98b65"); R(x + 91, fy - 16, 30, 2, "#efe6d8"); R(x + 105, fy - 12, 1, 10, "#a8704f");
        R(x + 96, fy - 30, 12, 14, "#2b2f36"); R(x + 98, fy - 28, 8, 3, "#4a525d"); R(x + 101, fy - 22, 2, 2, "#111"); R(x + 100, fy - 19, 4, 3, "#f4f1ea");
        S.coffee = { x: x + 102, y: fy - 31, light: { x: x + 105, y: fy - 27 } };
        for (let i = 0; i < 2; i++) R(x + 110 + i * 5, fy - 19, 3, 3, ["#c43b3b", "#2f6fa8"][i]);
        R(x + 128, fy - 20, 10, 20, "#e8edf2"); R(x + 129, fy - 31, 8, 11, "#9fd3ea"); R(x + 130, fy - 30, 2, 8, "#d8f0fa"); R(x + 132, fy - 15, 2, 2, "#2f6fa8");
        plant(x + 146, fy); break;
      }
      case "strategist": {
        // one sticky note per real lesson: green = works or earns, red = doesn't or loses money
        R(x + 4, top + 12, 52, 36, "#8a6a3a"); R(x + 6, top + 14, 48, 32, "#c79a5b");
        for (let i = 3; i < 48; i += 5) for (let j = 3; j < 32; j += 5) R(x + 6 + i, top + 14 + j, 1, 1, "#b88a4f");
        const notes = (d.lessons || []).slice(0, 8);
        notes.forEach((l, i) => {
          const nx = x + 9 + (i % 4) * 11, ny = top + 17 + Math.floor(i / 4) * 14, good = l.verdict === "works" || l.verdict === "earns";
          R(nx, ny, 9, 10, good ? "#a7e3a1" : "#ffaaa5"); R(nx + 1, ny + 3, 7, 1, good ? "#5e9e58" : "#b35a55"); R(nx + 1, ny + 5, 5, 1, good ? "#5e9e58" : "#b35a55");
          R(nx + 4, ny - 1, 1, 2, "#c43b3b");
        });
        windowPane(x + 106, top + 14, 34, 22);
        R(x + 112, fy - 10, 20, 2, "#6b4a2f"); R(x + 121, fy - 8, 2, 8, "#4a321f"); R(x + 117, fy - 1, 10, 1, "#4a321f");
        chairSmall(x + 100, fy, "#4a5878"); chairSmall(x + 134, fy, "#4a5878");
        R(x + 147, fy - 2, 9, 2, "#3a3f47"); R(x + 151, fy - 13, 1, 11, "#3a3f47"); disc(x + 151, fy - 18, 5, "#4f8fd6"); R(x + 148, fy - 20, 3, 2, "#6fbf73"); R(x + 152, fy - 17, 3, 3, "#6fbf73");
        workstation(rm); break;
      }
      case "you": {
        bookshelf(x + 4, fy, 24, 50, 9);
        windowPane(x + 36, top + 14, 44, 28);
        R(x + 34, fy - 1, 74, 3, "#8f3b3b"); for (let i = 0; i < 74; i += 6) R(x + 34 + i, fy, 3, 1, "#b85c5c");
        R(x + 148, fy - 28, 8, 18, "#2b2f3a"); R(x + 146, fy - 12, 10, 3, "#2b2f3a");
        R(x + 96, fy - 12, 52, 3, "#5a3a22"); R(x + 98, fy - 9, 48, 9, "#6b4a2f"); R(x + 98, fy - 9, 48, 1, "#4a321f");
        R(x + 112, fy - 7, 17, 7, "#2b1d12"); pixelText(ctx, "YOU", x + 114, fy - 6, "#e6b422");
        R(x + 139, fy - 22, 2, 10, "#333"); R(x + 135, fy - 25, 9, 4, "#2f8f4e");
        R(x + 100, fy - 15, 14, 3, "#c9cfd6"); R(x + 100, fy - 16, 1, 1, "#c9cfd6"); R(x + 113, fy - 16, 1, 1, "#c9cfd6");
        const n = Math.min(12, d.pending_total || 0); // a sheet per item waiting for your approval
        for (let i = 0; i < n; i++) R(x + 101, fy - 16 - i, 12, 1, i % 2 ? "#fbfbf7" : "#e8e8e0");
        S.tray = { x: x + 99, y: fy - 16 - n, w: 16, h: n + 4 };
        break;
      }
    }
  }

  function drawShaft(fl) {
    const L = S.L, sx = L.shaftX, x = sx - SHAFT / 2, fy = fl.floorY - 2;
    R(x, fl.top, SHAFT, RH - 8, "#9aa3ad"); R(x, fl.top, SHAFT, 3, "#7d8590");
    R(x, fl.floorY - 8, SHAFT, 8, "#7d8590"); for (let i = 0; i < SHAFT; i += 5) R(x + i, fl.floorY - 8, 1, 8, "#727a85");
    R(sx - 12, fy - 36, 24, 36, "#5b6470"); R(sx - 10, fy - 34, 20, 34, "#2a2f38");
    R(sx - 6, fy - 43, 12, 6, "#111"); pixelText(ctx, fl.f === 0 ? "L" : String(fl.f), sx - 1, fy - 42, "#e2a43c");
  }

  /* ============================================================ live layer */
  function drawLive(t) {
    const L = S.L, d = S.snap;
    // elevator doors
    for (const fl of L.floors) {
      const open = clamp(S.doors[fl.f] || 0, 0, 1), sx = L.shaftX, fy = fl.floorY - 2, gap = Math.round(open * 9);
      R(sx - 10, fy - 34, 10 - gap, 34, "#c9d1d9"); R(sx + gap, fy - 34, 10 - gap, 34, "#c9d1d9");
      if (10 - gap > 1) { R(sx - 10 + (10 - gap) - 1, fy - 34, 1, 34, "#a9b2bc"); R(sx + gap, fy - 34, 1, 34, "#a9b2bc"); }
      if (open > 0) R(sx - 9, fy - 39, 2, 1, "#59d18a");
    }
    // monitors
    for (const key of AGENT_ROOMS) {
      const a = S.byKey[key]; if (!a || !S.screens[key]) continue;
      for (const sc of S.screens[key]) screen(sc, a.status, t, key);
    }
    // server LEDs: green while healthy, red for a failing check
    if (S.racks) {
      const health = Object.values(d.health || {});
      S.racks.forEach((rk, i) => {
        const bad = health.length && health[i % health.length] !== "ok";
        for (let j = 0; j < 7; j++) {
          const on = reduceMotion || (Math.floor(t * (3 + j % 3) + i * 7 + j) % 3) !== 0;
          R(rk.x, rk.y + j * 6 + 1, 2, 2, bad ? (on ? "#ff4d4d" : "#5a1a1a") : on ? "#59d18a" : "#1e4a33");
        }
      });
    }
    // coffee machine steam
    if (S.coffee && !reduceMotion) for (let i = 0; i < 3; i++) { const p = (t * 0.8 + i / 3) % 1; ctx.globalAlpha = 0.6 * (1 - p); R(S.coffee.x + Math.round(Math.sin(t * 3 + i * 2) * 1.5), S.coffee.y + 9 - p * 14, 1, 2, "#e8eef5"); }
    ctx.globalAlpha = 1;
    if (S.coffee) R(S.coffee.light.x, S.coffee.light.y, 1, 1, Math.floor(t * 1.5) % 2 ? "#ff5a5a" : "#7a2020");
    // printing press runs while the brief is being written
    if (S.press) {
      const running = S.byKey.daily_brief?.status === "running";
      for (let i = 0; i < 3; i++) {
        const rx = S.press.x + 4 + i * 9; R(rx, S.press.y, 5, 5, "#2f343c");
        const a = (running && !reduceMotion ? t * 6 : 0) + i; R(rx + 2 + Math.round(Math.cos(a) * 1.5), S.press.y + 2 + Math.round(Math.sin(a) * 1.5), 1, 1, "#c9d1d9");
      }
      if (running) { const p = reduceMotion ? 0.5 : (t * 0.7) % 1; R(S.press.x + 30 + p * 6, S.press.y + 18, 6, 1, "#fbfbf7"); }
    }
    // wall clock shows the agency's real local time
    if (S.clock) {
      const { x, y } = S.clock; disc(x, y, 8, "#3a3f47"); disc(x, y, 7, "#fbfbf7");
      const h = S.hour, ma = ((h % 1) * 60) / 60 * Math.PI * 2 - Math.PI / 2, ha = ((h % 12) / 12) * Math.PI * 2 - Math.PI / 2;
      line(x, y, x + Math.cos(ma) * 6, y + Math.sin(ma) * 6, "#2b2f36"); line(x, y, x + Math.cos(ha) * 4, y + Math.sin(ha) * 4, "#c43b3b");
    }
    // deal bell (rings only on a real new subscription)
    if (S.bell) {
      const ring = t < S.bellUntil, sw = ring && !reduceMotion ? Math.round(Math.sin(t * 22) * 2 * clamp((S.bellUntil - t) / 2, 0, 1)) : 0;
      const { x, y } = S.bell, bx = x + sw;
      R(x - 1, y, 2, 2, "#8b6914"); R(bx - 3, y + 2, 6, 2, "#e6b422"); R(bx - 4, y + 4, 8, 3, "#e6b422"); R(bx - 5, y + 7, 10, 2, "#c9971a"); R(bx - 1, y + 9, 2, 2, "#6b4a10"); R(bx - 2, y + 3, 1, 3, "#fff2a8");
      if (ring) for (const s of [-1, 1]) { R(x + s * 9, y + 2, 1, 2, "#f2c230"); R(x + s * 11, y + 5, 1, 2, "#f2c230"); }
    }
    // lobby scoreboard: real numbers, cycling
    if (S.board) {
      const m = d.money, items = [["BILLABLE THIS MONTH", money(m.billable_month)], ["COLLECTED 30 DAYS", money(m.collected_30d)],
        ["SUBSCRIPTIONS / MO", money(m.mrr)], ["CALLS TODAY", String(m.calls_today)], ["BOOKED TODAY", String(m.bookings_today)]];
      const [label, value] = items[Math.floor(t / 4) % items.length], b = S.board;
      pixelText(ctx, label, b.x + Math.round((b.w - pixelWidth(label)) / 2), b.y + 3, "#e2a43c");
      const s = pixelWidth(value, 2) <= b.w - 4 ? 2 : 1;
      pixelText(ctx, value, b.x + Math.round((b.w - pixelWidth(value, s)) / 2), b.y + 13, "#59d18a", s);
    }
    // your inbox glows while something waits for you
    if (S.tray && d.pending_total > 0) {
      const a = reduceMotion ? 0.8 : 0.45 + 0.45 * Math.sin(t * 4);
      ctx.globalAlpha = a; const tr = S.tray;
      R(tr.x - 1, tr.y - 1, tr.w + 2, 1, "#ffcf4d"); R(tr.x - 1, tr.y + tr.h, tr.w + 2, 1, "#ffcf4d"); R(tr.x - 1, tr.y, 1, tr.h, "#ffcf4d"); R(tr.x + tr.w, tr.y, 1, tr.h, "#ffcf4d");
      ctx.globalAlpha = 1;
    }
    if (S.antenna) R(S.antenna.x, S.antenna.y, 2, 2, Math.floor(t * 1.2) % 2 ? "#ff4d4d" : "#6b1a1a");
  }

  function screen(sc, status, t, key) {
    const base = { running: "#0f2a1c", idle: "#16202e", waiting: "#2a2410", blocked: "#2e2410", error: "#2e1212", off: "#07090c" }[status] || "#16202e";
    R(sc.x, sc.y, sc.w, sc.h, base);
    if (status === "off") return;
    if (status === "error") { if (Math.floor(t * 2) % 2) { R(sc.x + 5, sc.y + 1, 2, 4, "#ff5a5a"); R(sc.x + 5, sc.y + 6, 2, 1, "#ff5a5a"); } return; }
    if (status === "blocked") { pixelText(ctx, "?", sc.x + 4, sc.y + 1, "#f2a43c"); return; }
    if (sc.chart) { for (let i = 0; i < 10; i++) { const h = 1 + (hash(key + i + Math.floor(t * (status === "running" ? 2 : 0.2))) % 6); R(sc.x + 1 + i, sc.y + sc.h - 1 - h, 1, h, "#4fc1d1"); } return; }
    const off = Math.floor(t * (status === "running" ? 6 : 0.5));
    for (let i = 0; i < 4; i++) R(sc.x + 1, sc.y + 1 + i * 2, 2 + (hash(key + (i + off)) % 9), 1, status === "running" ? "#59d18a" : "#5d7896");
  }

  /* ============================================================== actors */
  const SPEED = 40;
  function home(key) { const rm = S.L.rooms[key]; return { kind: "desk", f: rm.f, x: rm.x + 48, face: 1 }; }
  function queueGoal(i) {
    const rm = S.L.rooms.you, step = S.queue.length > 7 ? 10 : 12;
    return { kind: "queue", f: rm.f, x: rm.x + 88 - i * step, face: 1 };
  }
  function coffeeGoal(i) { const rm = S.L.rooms.lobby; const spot = [[86, 1], [143, -1], [66, -1]][i % 3]; return { kind: "coffee", f: rm.f, x: rm.x + spot[0], face: spot[1] }; }
  function visitGoal(hostKey) { const rm = S.L.rooms[hostKey]; return { kind: "visit", f: rm.f, x: rm.x + 100, face: -1 }; }
  const sameGoal = (a, b) => a && b && a.kind === b.kind && a.f === b.f && Math.abs(a.x - b.x) < 0.5;
  function route(s, g) {
    const out = [];
    if (s.f !== g.f) { out.push({ f: s.f, x: S.L.shaftX }); out.push({ ride: g.f }); }
    out.push({ f: g.f, x: g.x });
    return out;
  }
  function desiredGoal(s, a) {
    if (s.convo) return s.convo;
    if (a.status === "waiting") { const i = S.queue.indexOf(s.key); if (i >= 0) return queueGoal(i); }
    if (s.coffee && a.status === "idle") return coffeeGoal(s.coffee.spot);
    s.coffee = null;
    return home(s.key);
  }
  function place(s, g) { s.f = g.f; s.x = g.x; s.path = []; s.ride = null; s.arrived = true; s.facing = g.face || 1; }
  function updateActor(s, dt, t) {
    const a = S.byKey[s.key]; if (!a) return;
    const g = desiredGoal(s, a);
    if (!sameGoal(g, s.goal)) {
      s.goal = g; s.arrived = false;
      if (reduceMotion || !s.placed) { place(s, g); s.placed = true; }
      else if (s.ride) s.reroute = true;
      else s.path = route(s, g);
    }
    if (s.ride) {
      s.ride.t -= dt;
      if (s.ride.t <= 0) { s.f = s.ride.to; s.x = S.L.shaftX; S.doors[s.f] = 1.2; s.ride = null; if (s.reroute) { s.reroute = false; s.path = route(s, s.goal); } }
      return;
    }
    const wp = s.path[0];
    if (!wp) { if (!s.arrived) { s.arrived = true; s.facing = s.goal.face || s.facing; s.arrivedAt = t; } return; }
    if (wp.ride !== undefined) { S.doors[s.f] = 1.2; s.ride = { to: wp.ride, t: 0.8 + 0.45 * Math.abs(wp.ride - s.f) }; s.path.shift(); return; }
    const dx = wp.x - s.x;
    if (Math.abs(dx) < 0.6) { s.x = wp.x; s.path.shift(); return; }
    s.facing = dx > 0 ? 1 : -1;
    s.x += Math.sign(dx) * Math.min(Math.abs(dx), SPEED * dt);
    s.walkT += dt;
  }
  function poseOf(s, a, t) {
    if (s.ride) return null;
    if (s.path.length || !s.arrived) return ["walk", Math.floor(s.walkT * 8) % 4];
    if (t < s.cheerUntil) return ["cheer", 0];
    const k = s.goal.kind;
    if (k === "desk") {
      if (a.status === "off") return ["sleep", 0];
      if (a.status === "running") return ["type", Math.floor(t * 7) % 2];
      if (a.status === "idle" && !reduceMotion && Math.sin(t * 0.6 + (hash(s.key) % 10)) > 0.6) return ["type", Math.floor(t * 5) % 2];
      return ["sit", 0];
    }
    if (k === "queue") return ["paper", 0];
    if (k === "coffee") return ["cup", 0];
    return ["stand", 0];
  }
  function drawActor(s, t) {
    const a = S.byKey[s.key]; if (!a) return;
    const pose = poseOf(s, a, t); if (!pose) return;
    const spr = sprite(s.key, pose[0], pose[1]);
    const x = Math.round(s.x) - 7, y = feet(s.f) - (SPR_H - 1);
    if (s.facing < 0) { ctx.save(); ctx.translate(x * 2 + SPR_W, 0); ctx.scale(-1, 1); ctx.drawImage(spr, x, y); ctx.restore(); }
    else ctx.drawImage(spr, x, y);
    s.seated = pose[0] === "sit" || pose[0] === "type" || pose[0] === "sleep";
    const hx = Math.round(s.x), hy = feet(s.f) - (s.seated ? 19 : 22);
    if (s.goal && s.goal.kind === "desk" && s.arrived) {
      if (a.status === "error") { const b = reduceMotion ? 0 : Math.round(Math.sin(t * 5)); R(hx + 4, hy - 6 + b, 2, 4, "#ff4d4d"); R(hx + 4, hy - 1 + b, 2, 1, "#ff4d4d"); }
      else if (a.status === "blocked") { const b = reduceMotion ? 0 : Math.round(Math.sin(t * 3)); pixelText(ctx, "?", hx + 3, hy - 6 + b, "#f2a43c"); }
    }
    if (S.selected === s.key || S.hover === s.key) {
      const b = reduceMotion ? 0 : Math.round(Math.sin(t * 6) * 1.5), ay = hy - 9 + b, col = S.selected === s.key ? "#ffffff" : "#ffe9a3";
      R(hx - 3, ay, 7, 1, "#1b2233"); R(hx - 2, ay + 1, 5, 1, col); R(hx - 3, ay + 1, 1, 1, "#1b2233"); R(hx + 3, ay + 1, 1, 1, "#1b2233");
      R(hx - 1, ay + 2, 3, 1, col); R(hx - 2, ay + 2, 1, 1, "#1b2233"); R(hx + 2, ay + 2, 1, 1, "#1b2233"); R(hx, ay + 3, 1, 1, col); R(hx - 1, ay + 3, 1, 1, "#1b2233"); R(hx + 1, ay + 3, 1, 1, "#1b2233"); R(hx, ay + 4, 1, 1, "#1b2233");
    }
  }
  function lighting() {
    const L = S.L, night = S.sky.night;
    for (const rm of Object.values(L.rooms)) {
      const a = S.byKey[rm.key];
      const lit = !a || a.mode !== "off";
      const off = !lit || (a && a.status === "off");
      if (off) { ctx.fillStyle = `rgba(8,12,24,${(0.38 + 0.28 * night).toFixed(2)})`; ctx.fillRect(rm.x, rm.top, rm.w, RH); }
      else if (night > 0.1) {
        ctx.fillStyle = `rgba(255,190,110,${(0.05 * night).toFixed(3)})`; ctx.fillRect(rm.x, rm.top, rm.w, RH);
        for (const lx of [rm.x + 41, rm.x + 123]) {
          const g = ctx.createLinearGradient(0, rm.top + 5, 0, rm.floorY);
          g.addColorStop(0, `rgba(255,236,170,${(0.22 * night).toFixed(3)})`); g.addColorStop(1, "rgba(255,236,170,0)");
          ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(lx - 4, rm.top + 6); ctx.lineTo(lx + 4, rm.top + 6); ctx.lineTo(lx + 22, rm.floorY - 8); ctx.lineTo(lx - 22, rm.floorY - 8); ctx.fill();
        }
      }
    }
    if (S.lamp && night > 0.3) {
      const g = ctx.createRadialGradient(S.lamp.x, S.lamp.y, 1, S.lamp.x, S.lamp.y + 20, 34);
      g.addColorStop(0, `rgba(255,233,163,${(0.35 * night).toFixed(2)})`); g.addColorStop(1, "rgba(255,233,163,0)");
      ctx.fillStyle = g; ctx.fillRect(S.lamp.x - 34, S.lamp.y - 6, 68, 60);
    }
  }

  /* =========================================================== particles */
  function emit(p) { if (!reduceMotion && S.particles.length < 160) S.particles.push(p); }
  function updateParticles(dt, t) {
    for (const s of S.actors) {
      const a = S.byKey[s.key];
      if (a && a.status === "off" && s.arrived && s.goal?.kind === "desk" && t > (s.nextZ || 0)) {
        s.nextZ = t + 1.6 + Math.random() * 0.6;
        emit({ kind: "z", x: s.x + 6, y: feet(s.f) - 15, vx: 4, vy: -6, life: 2.4, max: 2.4 });
      }
    }
    for (const p of S.particles) { p.life -= dt; p.x += p.vx * dt; p.y += p.vy * dt; if (p.g) p.vy += p.g * dt; }
    S.particles = S.particles.filter((p) => p.life > 0);
  }
  function drawParticles() {
    for (const p of S.particles) {
      ctx.globalAlpha = clamp(p.life / p.max, 0, 1);
      if (p.kind === "z") pixelText(ctx, "z", p.x, p.y, "#e8eefc");
      else if (p.kind === "spark") { R(p.x - 1, p.y, 3, 1, p.col); R(p.x, p.y - 1, 1, 3, p.col); }
      else R(p.x, p.y, 2, 2, p.col);
    }
    ctx.globalAlpha = 1;
  }
  function sparkle(key, col = "#ffe27a") {
    const s = S.actors.find((x) => x.key === key); if (!s) return;
    for (let i = 0; i < 10; i++) emit({ kind: "spark", x: s.x, y: feet(s.f) - 14, vx: (Math.random() - 0.5) * 40, vy: -20 - Math.random() * 30, g: 50, life: 1.1, max: 1.1, col });
  }
  function coins() {
    const v = S.L.rooms.billing_guardian, c = S.L.rooms.closer;
    for (const rm of [v, c]) for (let i = 0; i < 18; i++) emit({ kind: "coin", x: rm.x + 20 + Math.random() * 120, y: rm.top + 10, vx: (Math.random() - 0.5) * 20, vy: -10 - Math.random() * 20, g: 90, life: 1.6, max: 1.6, col: i % 3 ? "#e6b422" : "#fff2a8" });
  }

  /* ============================================================= bubbles */
  function bubbleFont() { return `600 ${Math.round(11.5 * S.dpr)}px Plex, ui-sans-serif, system-ui, sans-serif`; }
  function wrap(c, text, maxW, maxLines) {
    const words = String(text).replace(/\s+/g, " ").trim().split(" ");
    const lines = []; let cur = "";
    for (const w of words) {
      const next = cur ? cur + " " + w : w;
      if (c.measureText(next).width <= maxW || !cur) cur = next; else { lines.push(cur); cur = w; }
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && cur) lines.push(cur);
    const used = lines.join(" ").length;
    if (used < String(text).replace(/\s+/g, " ").trim().length) {
      let last = lines[lines.length - 1] || "";
      while (last && c.measureText(last + "…").width > maxW) last = last.slice(0, -1);
      lines[lines.length - 1] = last.replace(/[\s,.;:]+$/, "") + "…";
    }
    return lines.map((l) => (c.measureText(l).width > maxW ? l.slice(0, Math.max(1, Math.floor(l.length * maxW / c.measureText(l).width) - 1)) + "…" : l));
  }
  function anchorOf(key) {
    const s = S.actors.find((x) => x.key === key); if (!s || s.ride) return null;
    return { x: s.x * S.z, y: (feet(s.f) - (s.seated ? 19 : 22)) * S.z };
  }
  function layoutBubble(b) {
    const c = S.sctx, an = anchorOf(b.key); if (!an) return null;
    c.font = bubbleFont();
    const pad = 7 * S.dpr, lh = 15 * S.dpr;
    const maxW = Math.round(clamp(RW * S.z * 1.02, 132 * S.dpr, 236 * S.dpr)) - pad * 2;
    const lines = wrap(c, b.text, maxW, b.kind === "say" ? 4 : 3);
    const w = Math.ceil(Math.max(...lines.map((l) => c.measureText(l).width))) + pad * 2, h = lines.length * lh + pad * 2 - 3 * S.dpr;
    const x = clamp(Math.round(an.x - w / 2), 3 * S.dpr, S.cv.width - w - 3 * S.dpr);
    const y = Math.max(2 * S.dpr, Math.round(an.y - h - 9 * S.dpr));
    return { x, y, w, h, lines, an, lh, pad };
  }
  const overlaps = (a, b) => a.x < b.x + b.w + 4 && b.x < a.x + a.w + 4 && a.y < b.y + b.h + 4 && b.y < a.y + a.h + 4;
  function tryBubble(key, text, kind, dur, t) {
    if (!text || S.bubbles.has(key)) return false;
    if (S.bubbles.size >= (S.L.name === "narrow" ? 3 : 5)) return false;
    const b = { key, text, kind, start: t, end: t + dur };
    const box = layoutBubble(b); if (!box) return false;
    for (const o of S.bubbles.values()) if (o.box && overlaps(box, o.box)) return false;
    b.box = box; S.bubbles.set(key, b); return true;
  }
  function say(key, text, dur, t) {
    const b = { key, text, kind: "say", start: t, end: t + dur };
    b.box = layoutBubble(b); if (!b.box) return;
    for (const [k, o] of S.bubbles) if (k !== key && o.kind !== "say" && o.box && overlaps(b.box, o.box)) S.bubbles.delete(k);
    S.bubbles.set(key, b);
  }
  const BUBBLE_STYLE = {
    think: ["#f7f9ff", "#1b2233"], say: ["#ffffff", "#1b2233"], alert: ["#fff4f2", "#c03030"], ask: ["#fff8ea", "#b5650d"],
  };
  function drawBubble(b, t) {
    const box = layoutBubble(b); if (!box) return;
    b.box = box;
    const c = S.sctx, u = Math.max(1, Math.round(1.5 * S.dpr)), [fill, border] = BUBBLE_STYLE[b.kind] || BUBBLE_STYLE.think;
    const fade = reduceMotion ? 1 : clamp(Math.min((t - b.start) / 0.15, (b.end - t) / 0.25), 0, 1);
    c.globalAlpha = fade;
    const { x, y, w, h } = box;
    const stepped = (ox, oy, ow, oh, col) => { c.fillStyle = col; c.fillRect(ox + 2 * u, oy, ow - 4 * u, oh); c.fillRect(ox + u, oy + u, ow - 2 * u, oh - 2 * u); c.fillRect(ox, oy + 2 * u, ow, oh - 4 * u); };
    c.fillStyle = "rgba(10,14,28,.28)"; c.fillRect(x + 2 * u, y + h + u, w - 2 * u, u);
    stepped(x, y, w, h, border); stepped(x + u, y + u, w - 2 * u, h - 2 * u, fill);
    const ax = clamp(Math.round(box.an.x), x + 4 * u, x + w - 5 * u);
    if (b.kind === "think") {
      c.fillStyle = border; c.fillRect(ax - 2 * u, y + h + 2 * u, 4 * u, 3 * u); c.fillStyle = fill; c.fillRect(ax - u, y + h + 3 * u, 2 * u, u);
      c.fillStyle = border; c.fillRect(ax - u, y + h + 6 * u, 2 * u, 2 * u);
    } else {
      for (let i = 0; i < 3; i++) { c.fillStyle = border; c.fillRect(ax - (3 - i) * u, y + h - u + i * u, (6 - 2 * i) * u + u, u); c.fillStyle = fill; c.fillRect(ax - (2 - i) * u, y + h - u + i * u, Math.max(0, (4 - 2 * i) * u - u), u); }
    }
    c.fillStyle = b.kind === "alert" ? "#8a1c1c" : "#1b2233"; c.font = bubbleFont(); c.textBaseline = "top";
    box.lines.forEach((l, i) => c.fillText(l, x + box.pad, y + box.pad - 1 * S.dpr + i * box.lh));
    c.globalAlpha = 1;
  }

  /* ============================================================ director */
  // Decides who says what, using only snapshot data.
  function direct(t) {
    for (const [k, b] of S.bubbles) if (t > b.end) S.bubbles.delete(k);
    conversation(t);
    const d = S.snap;
    // agents waiting at your desk ask, one at a time
    if (t >= S.nextAsk) {
      const q = S.queue.filter((k) => { const s = actor(k); return s && s.arrived && s.goal?.kind === "queue"; });
      if (q.length) {
        const k = q[S.askIdx++ % q.length], a = S.byKey[k];
        if (a.pending.length) tryBubble(k, `Need your OK: ${a.pending[0].title}` + (a.pending_count > 1 ? ` (+${a.pending_count - 1} more)` : ""), "ask", 5.5, t);
      }
      S.nextAsk = t + 6;
    }
    // everyone else thinks out loud about their real work
    const order = S.actors.slice(S.rot % S.actors.length).concat(S.actors.slice(0, S.rot % S.actors.length)); S.rot++;
    for (const s of order) {
      if (S.bubbles.has(s.key) || !s.arrived || s.goal?.kind !== "desk" || (S.convo && (S.convo.from === s.key || S.convo.to === s.key))) continue;
      const a = S.byKey[s.key], last = S.lastLine[s.key] || -99, gap = t - last;
      let text = null, kind = "think", dur = 4.5;
      if (a.status === "running" && gap > 3.4) {
        const lines = a.log.length ? a.log : [a.summary].filter(Boolean);
        if (lines.length) { text = lines[(S.logIdx[s.key] = ((S.logIdx[s.key] ?? -1) + 1)) % lines.length]; dur = 3.4; }
      } else if (a.status === "error" && gap > 18) { text = a.summary; kind = "alert"; dur = 6; }
      else if (a.status === "blocked" && gap > 22 && a.blocker) { text = a.blocker.text; kind = "alert"; dur = 6; }
      else if (a.status === "idle" && gap > 28 + (hash(s.key) % 16)) {
        const flip = (S.logIdx[s.key] = ((S.logIdx[s.key] ?? -1) + 1)) % 2;
        text = flip && a.next_run ? `Next run at ${fmtTime(a.next_run)}.` : a.summary ? `Last run: ${a.summary}` : a.next_run ? `Next run at ${fmtTime(a.next_run)}.` : null;
      }
      if (text && tryBubble(s.key, text, kind, dur, t)) S.lastLine[s.key] = t;
    }
    // coffee runs for idle agents (movement only, no words)
    if (!reduceMotion && t > S.nextCoffee) {
      S.nextCoffee = t + 9 + Math.random() * 9;
      const idle = S.actors.filter((s) => S.byKey[s.key]?.status === "idle" && s.arrived && s.goal?.kind === "desk" && !s.convo && S.selected !== s.key && t > (s.coffeeAfter || 0));
      const busy = new Set(S.actors.filter((s) => s.coffee).map((s) => s.coffee.spot));
      const free = [0, 1, 2].filter((i) => !busy.has(i));
      if (idle.length && free.length && Math.random() < 0.5) {
        const s = idle[Math.floor(Math.random() * idle.length)];
        s.coffee = { spot: free[0], until: t + 999 }; s.coffeeAfter = t + 120 + Math.random() * 120;
      }
    }
    for (const s of S.actors) if (s.coffee && s.arrived && s.goal?.kind === "coffee") {
      if (s.coffee.until > t + 100) s.coffee.until = t + 6 + Math.random() * 5;
      else if (t > s.coffee.until) s.coffee = null;
    }
  }
  const actor = (k) => S.actors.find((s) => s.key === k);
  function conversation(t) {
    const c = S.convo;
    if (!c) {
      if (t < S.nextConvo || !S.snap.conversations) return;
      S.nextConvo = t + 12;
      const ready = (k) => { const s = actor(k), a = S.byKey[k]; return s && a && s.arrived && s.goal?.kind === "desk" && a.status !== "waiting" && !s.coffee; };
      const pick = S.snap.conversations.find((x) => !S.seen.has(x.id) && x.from !== x.to && ready(x.from) && ready(x.to) && S.byKey[x.from].status !== "off");
      if (!pick) return;
      S.seen.add(pick.id); saveSeen();
      S.convo = { ...pick, phase: "walk", i: 0, started: t, next: 0 };
      actor(pick.from).convo = visitGoal(pick.to);
      return;
    }
    const v = actor(c.from), h = actor(c.to);
    if (!v || !h) { endConvo(t); return; }
    if (c.phase === "walk") {
      if (v.arrived && v.goal?.kind === "visit") { c.phase = "talk"; c.next = t + 0.3; v.facing = -1; }
      else if (t - c.started > 45) endConvo(t);
      return;
    }
    if (t < c.next) return;
    if (c.i < c.lines.length) {
      const [who, text] = c.lines[c.i++];
      const dur = clamp(2.2 + text.length * 0.045, 3, 7);
      say(who, text, dur, t); c.next = t + dur + 0.35;
    } else endConvo(t);
  }
  function endConvo(t) { const v = S.convo && actor(S.convo.from); if (v) v.convo = null; S.convo = null; S.nextConvo = t + 16; }
  function saveSeen() { try { sessionStorage.setItem("office-seen", JSON.stringify([...S.seen].slice(-60))); } catch (_) { /* private mode */ } }
  function loadSeen() { try { return new Set(JSON.parse(sessionStorage.getItem("office-seen") || "[]")); } catch (_) { return new Set(); } }

  /* ============================================================ overlays */
  function drawOverlays(t) {
    const c = S.sctx, z = S.z, L = S.L;
    c.textBaseline = "top";
    // room plates
    c.font = `600 ${Math.max(8, Math.round(9 * S.dpr))}px Plex, ui-sans-serif, system-ui, sans-serif`;
    if ("letterSpacing" in c) c.letterSpacing = `${(0.6 * S.dpr).toFixed(1)}px`;
    for (const rm of Object.values(L.rooms)) {
      const label = rm.name.toUpperCase(), tw = c.measureText(label).width, px = (rm.x + 4) * z, py = (rm.top + 4) * z;
      const a = S.byKey[rm.key], dark = a && (a.mode === "off" || a.status === "off");
      c.fillStyle = dark ? "rgba(0,0,0,.55)" : "rgba(20,26,40,.72)";
      c.fillRect(Math.round(px), Math.round(py), Math.round(tw + 10 * S.dpr), Math.round(15 * S.dpr));
      c.fillStyle = dark ? "#8f99ab" : "#f2f5fa";
      c.fillText(label, Math.round(px + 5 * S.dpr), Math.round(py + 3 * S.dpr));
    }
    if ("letterSpacing" in c) c.letterSpacing = "0px";
    // roof sign
    if (S.sign && S.snap.brand) {
      const sg = S.sign, maxW = (sg.w - 10) * z;
      let fs = Math.round(sg.h * z * 0.56);
      c.font = `700 ${fs}px Plex, ui-sans-serif, system-ui, sans-serif`;
      while (fs > 8 && c.measureText(S.snap.brand).width > maxW) { fs -= 1; c.font = `700 ${fs}px Plex, ui-sans-serif, system-ui, sans-serif`; }
      c.fillStyle = S.sky.night > 0.4 ? "#ffe9a3" : "#f2f5fa"; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(S.snap.brand, (sg.x + sg.w / 2) * z, (sg.y + sg.h / 2 + 0.5) * z);
      c.textAlign = "left"; c.textBaseline = "top";
    }
    // paused banner across the building
    if (!S.snap.enabled) {
      const text = "ALL AGENTS PAUSED";
      c.font = `700 ${Math.round(13 * S.dpr)}px Plex, ui-sans-serif, system-ui, sans-serif`;
      const w = c.measureText(text).width + 24 * S.dpr, x = (S.cv.width - w) / 2, y = (L.roofY + 10) * z;
      c.fillStyle = "rgba(192,48,48,.92)"; c.fillRect(x, y, w, 24 * S.dpr); c.fillStyle = "#fff"; c.fillText(text, x + 12 * S.dpr, y + 5 * S.dpr);
    }
    for (const b of S.bubbles.values()) drawBubble(b, t);
    // hover name tag
    const hk = S.hover && S.hover !== S.selected ? S.hover : null;
    if (hk && !S.bubbles.has(hk)) {
      const an = anchorOf(hk), a = S.byKey[hk];
      if (an && a) {
        const text = `${a.title} · ${statusWord(a)}`;
        c.font = `600 ${Math.round(11 * S.dpr)}px Plex, ui-sans-serif, system-ui, sans-serif`;
        const w = c.measureText(text).width + 12 * S.dpr, h = 19 * S.dpr;
        const x = clamp(an.x - w / 2, 2, S.cv.width - w - 2), y = an.y - h - 14 * S.dpr;
        c.fillStyle = "rgba(20,26,40,.9)"; c.fillRect(x, y, w, h); c.fillStyle = "#fff"; c.fillText(text, x + 6 * S.dpr, y + 4 * S.dpr);
      }
    }
  }

  /* ================================================================ frame */
  function frame(ms) {
    if (!S || !S.root.isConnected) { teardown(); return; }
    S.raf = requestAnimationFrame(frame);
    if (document.hidden || !S.visible) { S.last = ms; return; }
    if (ms - S.lastRender < 30) return;
    const t = ms / 1000, dt = Math.min(0.1, (ms - (S.last || ms)) / 1000);
    S.last = ms; S.lastRender = ms;
    const h = localHour();
    if (Math.abs(h - S.hour) > 1 / 60 || !S.skyLayer) { S.hour = h; }
    ensureLayers();
    for (const s of S.actors) updateActor(s, dt, t);
    for (const f of Object.keys(S.doors)) S.doors[f] = Math.max(0, S.doors[f] - dt);
    if (t > S.nextDirect) { direct(t); S.nextDirect = t + 0.4; }
    updateParticles(dt, t);
    // world
    const L = S.L; ctx = S.wctx;
    ctx.drawImage(S.skyLayer, 0, 0);
    if (!reduceMotion || true) for (const cl of S.clouds) {
      const x = ((cl.x + t * cl.v) % (L.W + 80)) - 40;
      ctx.globalAlpha = S.sky.night > 0.5 ? 0.25 : 0.85;
      R(x, cl.y, cl.w, 4, "#ffffff"); R(x + 4, cl.y - 3, cl.w - 10, 3, "#ffffff"); R(x + 8, cl.y - 5, cl.w / 3, 2, "#ffffff");
      ctx.globalAlpha = 1;
    }
    ctx.drawImage(S.buildingLayer, 0, 0);
    drawLive(t);
    const ordered = [...S.actors].sort((a, b) => (a.goal?.kind === "desk") - (b.goal?.kind === "desk"));
    for (const s of ordered) drawActor(s, t);
    lighting();
    drawParticles();
    // screen
    const c = S.sctx; c.imageSmoothingEnabled = false;
    c.clearRect(0, 0, S.cv.width, S.cv.height);
    c.drawImage(S.world, 0, 0, L.W, L.H, 0, 0, S.cv.width, S.cv.height);
    drawOverlays(t);
  }

  function ensureLayers() {
    const d = S.snap;
    const sig = [S.L.name, Math.floor(S.hour * 12), d.money.new_prospects_24h, JSON.stringify(d.money.pipeline), d.drafts_waiting,
      d.money.emails_today, d.money.active_clients, d.money.collected_30d, d.pending_total].join("|");
    if (sig === S.layerSig) return;
    S.layerSig = sig;
    S.sky = skyAt(S.hour);
    S.screens = {};
    S.skyLayer = drawSkyLayer(S.sky);
    S.buildingLayer = drawBuildingLayer(d, S.sky);
  }

  /* =============================================================== sizing */
  function resize() {
    if (!S) return;
    const cssW = S.stage.clientWidth || 600;
    const name = cssW < 640 ? "narrow" : "wide";
    if (!S.L || S.L.name !== name) {
      S.L = build(name);
      S.world = makeCanvas(S.L.W, S.L.H); S.wctx = S.world.getContext("2d"); S.wctx.imageSmoothingEnabled = false;
      S.layerSig = null; S.doors = {};
      for (const s of S.actors) { s.placed = false; s.goal = null; s.path = []; s.ride = null; }
      S.clouds = Array.from({ length: 5 }, (_, i) => ({ x: (i * S.L.W) / 5 + 20, y: 12 + (i * 7) % 24, w: 18 + (i * 11) % 20, v: 2 + (i % 3) }));
    }
    S.dpr = Math.min(3, window.devicePixelRatio || 1);
    const cssH = Math.round(cssW * S.L.H / S.L.W);
    S.cv.style.height = cssH + "px";
    S.cv.width = Math.round(cssW * S.dpr); S.cv.height = Math.round(cssH * S.dpr);
    S.z = S.cv.width / S.L.W;
    for (const b of S.bubbles.values()) b.box = null;
  }

  /* ================================================================ input */
  function toWorld(e) { const r = S.cv.getBoundingClientRect(); return { x: (e.clientX - r.left) * (S.L.W / r.width), y: (e.clientY - r.top) * (S.L.H / r.height) }; }
  function hitTest(p) {
    for (const s of [...S.actors].reverse()) {
      if (s.ride) continue;
      const fy = feet(s.f);
      if (p.x >= s.x - 7 && p.x <= s.x + 8 && p.y >= fy - 22 && p.y <= fy + 2) return { type: "agent", key: s.key };
    }
    for (const b of S.bubbles.values()) {
      if (!b.box) continue;
      const bx = b.box.x / S.z, by = b.box.y / S.z, bw = b.box.w / S.z, bh = b.box.h / S.z;
      if (p.x >= bx && p.x <= bx + bw && p.y >= by && p.y <= by + bh) return b.kind === "ask" ? { type: "desk" } : { type: "agent", key: b.key };
    }
    for (const rm of Object.values(S.L.rooms)) {
      if (p.x >= rm.x && p.x <= rm.x + rm.w && p.y >= rm.top && p.y <= rm.floorY) {
        if (rm.key === "you") return { type: "desk" };
        if (rm.key === "lobby") return { type: "money" };
        return { type: "agent", key: rm.key };
      }
    }
    if (S.sign && p.y < S.L.roofY) return { type: "money" };
    return null;
  }
  function onMove(e) {
    const h = hitTest(toWorld(e));
    S.hover = h && h.type === "agent" ? h.key : null;
    S.cv.classList.toggle("is-pointer", !!h);
  }
  function onClick(e) { const h = hitTest(toWorld(e)); if (h) openPanel(h); }

  /* ================================================================ panel */
  const SVGNS = "http://www.w3.org/2000/svg";
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  function icon(name) { const s = document.createElementNS(SVGNS, "svg"); s.setAttribute("class", "icon"); s.setAttribute("aria-hidden", "true"); const u = document.createElementNS(SVGNS, "use"); u.setAttribute("href", `#i-${name}`); s.append(u); return s; }
  function statusWord(a) {
    if (!S.snap.enabled) return "Paused";
    return { running: "Working", waiting: `Needs you (${a.pending_count})`, blocked: "Blocked", error: "Error", off: "Off", idle: "Idle" }[a.status] || a.status;
  }
  const TONE = { running: "good", waiting: "wait", blocked: "bad", error: "bad", off: "neutral", idle: "neutral" };
  const MODES = [["off", "Off"], ["approve", "Ask me first"], ["auto", "Autopilot"]];
  const MODE_HINT = { off: "Doesn't run.", approve: "Anything that sends or spends waits at your desk for your OK.", auto: "Acts on its own, inside the limits in its settings." };
  const toast = (msg, tone = "good") => (window.deskline && window.deskline.toast ? window.deskline.toast(msg, tone) : console.log(msg));

  async function post(url, data = {}) {
    const body = new FormData();
    for (const [k, v] of Object.entries(data)) body.append(k, v);
    const r = await fetch(url, { method: "POST", body, credentials: "same-origin", headers: { Accept: "application/json", "X-Requested-With": "deskline" } });
    if (r.redirected && /\/login/.test(r.url)) { location.href = r.url; throw new Error("Signed out"); }
    const json = (r.headers.get("content-type") || "").includes("json") ? await r.json() : {};
    if (!r.ok) throw new Error(json.detail || `Request failed (${r.status})`);
    return json;
  }
  function busy(btn, on) { if (!btn) return; btn.disabled = on; if (on) btn.setAttribute("aria-busy", "true"); else btn.removeAttribute("aria-busy"); }

  async function decide(id, decision, btn, key) {
    busy(btn, true);
    try {
      const r = await post(`/agents/actions/${id}/${decision}`, { back: "/agents/office" });
      toast(r.message, r.ok ? "good" : "bad");
      if (r.ok && decision !== "reject") sparkle(key);
    } catch (e) { toast(e.message, "bad"); }
    S.panelSig = null; await refresh();
  }
  async function approveAll(btn) {
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1"; const was = btn.textContent; btn.textContent = "Click again to send everything";
      setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = was; } }, 4000);
      return;
    }
    busy(btn, true);
    let done = 0, failed = 0;
    for (const a of S.snap.agents) for (const p of a.pending) {
      try { const r = await post(`/agents/actions/${p.id}/approve`, { back: "/agents/office" }); if (r.ok) { done++; sparkle(a.key); } else failed++; }
      catch (_) { failed++; }
    }
    toast(`Approved ${done}.` + (failed ? ` ${failed} couldn't be done. Open the list view for details.` : ""), failed && !done ? "bad" : "good");
    S.panelSig = null; await refresh();
  }
  async function setMode(key, mode, btn) {
    busy(btn, true);
    try { const r = await post(`/agents/${key}/mode`, { mode, back: "/agents/office" }); toast(r.message); } catch (e) { toast(e.message, "bad"); }
    S.panelSig = null; await refresh();
  }
  async function runNow(key, btn) {
    busy(btn, true);
    try { const r = await post(`/agents/${key}/run`); toast(r.message); } catch (e) { toast(e.message, "bad"); }
    setTimeout(refresh, 900); setTimeout(refresh, 3500);
  }
  async function switchAll(btn) {
    busy(btn, true);
    try { const r = await post("/agents/switch"); toast(r.message); } catch (e) { toast(e.message, "bad"); }
    busy(btn, false); S.panelSig = null; await refresh();
  }
  async function markWon(p, btn) {
    if (btn.dataset.armed !== "1") {
      btn.dataset.armed = "1"; btn.textContent = "Confirm: they said yes";
      setTimeout(() => { if (btn.isConnected) { btn.dataset.armed = ""; btn.textContent = "Mark won"; } }, 4000);
      return;
    }
    busy(btn, true);
    try {
      const r = await post(`/prospects/${p.id}/status`, { status: "won" }); toast(r.message);
      const s = actor("closer"); if (s) s.cheerUntil = performance.now() / 1000 + 2.5; sparkle("closer");
    } catch (e) { toast(e.message, "bad"); }
    S.panelSig = null; await refresh(); setTimeout(refresh, 2500);
  }

  function pendingItem(p, key) {
    return el("div", { class: "office-item" },
      el("div", { class: "t" }, p.title),
      el("div", { class: "xs muted" }, p.kind),
      p.detail ? el("details", {}, el("summary", {}, "What exactly will happen"), el("div", { class: "log" }, p.detail)) : null,
      el("div", { class: "actions" },
        el("button", { type: "button", class: "btn ghost sm", onclick: (e) => decide(p.id, "reject", e.currentTarget, key) }, "Reject"),
        el("button", { type: "button", class: "btn primary sm", onclick: (e) => decide(p.id, "approve", e.currentTarget, key) }, icon("check"), "Approve")));
  }
  function repliedList() {
    const list = S.snap.replied || [];
    if (!list.length) return null;
    return el("section", {}, el("h3", {}, "Replied: close these"),
      el("p", { class: "small muted m0" }, "Talk to them. When they say yes, mark them won and the Closer sends the checkout link."),
      ...list.map((p) => el("div", { class: "office-item" },
        el("div", { class: "split" }, el("span", { class: "t" }, p.name), el("span", { class: "xs muted" }, `Score ${p.score}`)),
        p.email ? el("div", { class: "xs muted" }, p.email) : el("div", { class: "xs text-bad" }, "No email yet. Add one so the checkout link can be sent."),
        el("div", { class: "actions" },
          el("a", { class: "btn ghost sm", href: `/prospects/${p.id}` }, "Open"),
          el("button", { type: "button", class: "btn primary sm", onclick: (e) => markWon(p, e.currentTarget) }, "Mark won")))));
  }

  function panelContent(view) {
    const d = S.snap;
    if (view.type === "agent") {
      const a = S.byKey[view.key]; if (!a) return null;
      const kids = [];
      if (a.blocker && a.mode !== "off") kids.push(el("div", { class: "callout", "data-tone": "bad" }, icon("alert"), el("span", { class: "grow" }, a.blocker.text), el("a", { class: "btn sm", href: a.blocker.href }, a.blocker.cta)));
      kids.push(el("section", {}, el("h3", {}, "How it works"),
        el("div", { class: "segmented", role: "group", "aria-label": "Mode" }, ...MODES.map(([m, label]) =>
          el("button", { type: "button", "data-mode": m, "aria-pressed": String(a.mode === m), onclick: (e) => a.mode !== m && setMode(a.key, m, e.currentTarget) }, label))),
        el("p", { class: "small muted mt-s m0" }, MODE_HINT[a.mode] || "")));
      kids.push(el("div", { class: "cluster" },
        el("button", { type: "button", class: "btn sm", disabled: a.status === "running", onclick: (e) => runNow(a.key, e.currentTarget) }, icon("play"), a.status === "running" ? "Running…" : "Run now"),
        el("a", { class: "btn ghost sm", href: `/agents/${a.key}` }, icon("settings"), "Settings and history")));
      if (a.pending.length) kids.push(el("section", {}, el("h3", {}, `Waiting for you (${a.pending_count})`), ...a.pending.map((p) => pendingItem(p, a.key))));
      if (a.key === "closer") { const r = repliedList(); if (r) kids.push(r); }
      if (a.key === "strategist") {
        const ls = d.lessons || [];
        kids.push(el("section", {}, el("h3", {}, "What I've learned"),
          ls.length ? el("ul", { class: "office-lines" }, ...ls.map((l) => el("li", { "data-v": l.verdict }, l.statement)))
            : el("p", { class: "small muted m0" }, "Nothing proven yet. I only call a result once enough emails are behind it."),
          el("a", { class: "btn sm mt-s", href: "/agents/learning" }, icon("sparkles"), "What's working")));
      }
      const lines = a.log.length ? a.log : [];
      kids.push(el("section", {}, el("h3", {}, a.status === "running" ? "Doing right now" : "Last run"),
        el("p", { class: "small m0" }, a.summary || "Hasn't run yet."),
        lines.length ? el("ul", { class: "office-lines mt-s" }, ...lines.map((l) => el("li", {}, l))) : null,
        el("p", { class: "xs muted mt-s m0" }, [a.last_run ? `Ran ${ago(a.last_run)}` : "Never run", a.next_run && a.mode !== "off" && d.enabled ? `next run ${fmtTime(a.next_run)}` : null].filter(Boolean).join(" · "))));
      kids.push(el("section", {}, el("h3", {}, "Job"), el("p", { class: "small ink-2 m0" }, a.role)));
      return { title: a.title, sub: `${a.room} · ${statusWord(a)}`, kids, portrait: a.key };
    }
    if (view.type === "desk") {
      const kids = [], total = d.pending_total;
      if (total > 1) kids.push(el("button", { type: "button", class: "btn primary block", onclick: (e) => approveAll(e.currentTarget) }, icon("check"), `Approve all ${total}`));
      for (const a of d.agents) if (a.pending.length) kids.push(el("section", {}, el("h3", {}, `${a.title} (${a.pending_count})`), ...a.pending.map((p) => pendingItem(p, a.key))));
      const r = repliedList(); if (r) kids.push(r);
      if (!total && !r) kids.push(el("div", { class: "empty" }, el("p", { class: "small muted m0" }, "Nothing on your desk. When an agent on Ask me first wants to send an email or spend money, it walks it over here.")));
      return { title: "Your desk", sub: total ? `${total} waiting for your OK` : "All clear", kids };
    }
    const m = d.money, pipe = m.pipeline || {};
    const kv = (pairs) => el("dl", { class: "office-kv" }, ...pairs.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    return {
      title: "Scoreboard", sub: "Live numbers from your calls, billing and pipeline",
      kids: [
        el("section", {}, el("h3", {}, "Money"), kv([["Billable this month", money(m.billable_month)], ["Provider cost this month", money(m.cost_month)],
          ["Margin this month", money(m.billable_month - m.cost_month)], ["Subscriptions", `${money(m.mrr)}/mo`], ["Collected, last 30 days", money(m.collected_30d)], ["Paying clients", String(m.active_clients)]])),
        el("section", {}, el("h3", {}, "Today"), kv([["Calls answered", String(m.calls_today)], ["Bookings made", String(m.bookings_today)], ["Emails sent (24 h)", String(m.emails_today)], ["New leads (24 h)", String(m.new_prospects_24h)]])),
        el("section", {}, el("h3", {}, "Pipeline"), kv([["New", String(pipe.new || 0)], ["Contacted", String(pipe.contacted || 0)], ["Demo built", String(pipe.demo || 0)], ["Replied", String(pipe.replied || 0)], ["Won", String(pipe.won || 0)]])),
        el("div", { class: "cluster" }, el("a", { class: "btn sm", href: "/agents/learning" }, "What's working"), el("a", { class: "btn sm", href: "/billing" }, "Billing"), el("a", { class: "btn sm", href: "/leads" }, "Lead finder"), el("a", { class: "btn ghost sm", href: "/" }, "Dashboard")),
      ],
    };
  }
  function openPanel(view) {
    S.view = view; S.selected = view.type === "agent" ? view.key : null; S.panelSig = null;
    renderPanel(true);
    S.panel.hidden = false; S.root.classList.add("panel-open");
    S.panel.focus({ preventScroll: true });
    syncRoster();
    if (view.type === "agent") {
      const a = S.byKey[view.key], t = performance.now() / 1000;
      if (a && !S.bubbles.has(a.key)) {
        const text = a.status === "running" ? a.log[a.log.length - 1] : a.status === "blocked" && a.blocker ? a.blocker.text : a.status === "off" ? null : a.summary;
        if (text) tryBubble(a.key, text, a.status === "blocked" || a.status === "error" ? "alert" : "think", 5, t);
      }
    }
  }
  function closePanel() {
    S.panel.hidden = true; S.root.classList.remove("panel-open"); S.view = null; S.selected = null; syncRoster();
    S.cv.focus?.({ preventScroll: true });
  }
  function renderPanel(force) {
    if (!S.view || (!force && S.panel.hidden)) return;
    const content = panelContent(S.view);
    if (!content) { closePanel(); return; }
    const sig = JSON.stringify([S.view, S.view.type === "agent" ? S.byKey[S.view.key] : null, S.view.type !== "agent" ? [S.snap.money, S.snap.agents.map((a) => a.pending), S.snap.replied] : S.view.key === "closer" ? S.snap.replied : null]);
    if (!force && sig === S.panelSig) return;
    S.panelSig = sig;
    const body = S.panel.querySelector("[data-panel-body]"), y = body.scrollTop;
    const open = new Set([...body.querySelectorAll("details[open]")].map((d) => d.parentElement?.querySelector(".t")?.textContent));
    S.panel.querySelector("[data-panel-title]").textContent = content.title;
    S.panel.querySelector("[data-panel-sub]").textContent = content.sub;
    const pc = S.panel.querySelector(".office-portrait"), pctx = pc.getContext("2d");
    pctx.clearRect(0, 0, pc.width, pc.height);
    pc.style.visibility = content.portrait ? "visible" : "hidden";
    if (content.portrait) pctx.drawImage(sprite(content.portrait, "stand", 0), 0, 0);
    body.replaceChildren(...content.kids);
    body.querySelectorAll("details").forEach((d) => { if (open.has(d.parentElement?.querySelector(".t")?.textContent)) d.open = true; });
    body.scrollTop = y;
  }

  /* ========================================================= DOM chrome */
  function syncRoster() {
    const box = S.root.querySelector("[data-office-roster]");
    const want = S.snap.agents.map((a) => a.key).join(",");
    if (box.dataset.keys !== want) {
      box.dataset.keys = want;
      box.replaceChildren(...S.snap.agents.map((a) => el("button", { type: "button", role: "listitem", "data-key": a.key, onclick: () => openPanel({ type: "agent", key: a.key }) },
        el("span", { class: "lamp" }), el("span", { class: "nm" }), el("span", { class: "st" }))));
      const desk = el("button", { type: "button", role: "listitem", "data-desk": "1", onclick: () => openPanel({ type: "desk" }) }, el("span", { class: "lamp" }), el("span", { class: "nm" }, "Your desk"), el("span", { class: "st" }));
      box.append(desk);
    }
    for (const b of box.querySelectorAll("button[data-key]")) {
      const a = S.byKey[b.dataset.key]; if (!a) continue;
      b.querySelector(".lamp").dataset.tone = S.snap.enabled ? TONE[a.status] || "neutral" : "neutral";
      b.querySelector(".nm").textContent = a.title;
      b.querySelector(".st").textContent = statusWord(a);
      b.setAttribute("aria-pressed", String(S.selected === a.key));
    }
    const desk = box.querySelector("[data-desk]");
    desk.querySelector(".lamp").dataset.tone = S.snap.pending_total ? "wait" : "good";
    desk.querySelector(".st").textContent = S.snap.pending_total ? `${S.snap.pending_total} waiting` : "Clear";
    desk.setAttribute("aria-pressed", String(S.view?.type === "desk"));
  }
  function syncChrome() {
    const d = S.snap, m = d.money, pipe = m.pipeline || {};
    const set = (k, v) => { const n = S.root.querySelector(`[data-k="${k}"]`); if (n) n.textContent = v; };
    set("billable_month", money(m.billable_month)); set("billable_note", `${money(m.cost_month)} provider cost`);
    set("collected_30d", money(m.collected_30d)); set("mrr_note", `${money(m.mrr)}/mo in subscriptions`);
    set("calls_today", String(m.calls_today)); set("calls_note", `${m.bookings_today} booked · ${m.emails_today} emails sent`);
    set("pipeline_total", String(Object.values(pipe).reduce((a, b) => a + b, 0) - (pipe.lost || 0)));
    set("pipeline_note", `${pipe.demo || 0} demo · ${pipe.replied || 0} replied · ${pipe.won || 0} won`);
    // the one thing worth doing next
    const ns = d.next_step || {}, box = S.root.querySelector("[data-office-next]");
    box.dataset.tone = ns.tone === "bad" ? "bad" : ns.tone === "good" ? "" : "brand";
    box.querySelector("[data-text]").textContent = ns.text || "";
    box.querySelector("[data-next-cta]")?.remove();
    if (ns.cta) {
      const btn = ns.href ? el("a", { class: "btn sm", href: ns.href, "data-next-cta": "1" }, ns.cta)
        : el("button", { type: "button", class: "btn sm primary", "data-next-cta": "1", onclick: (e) => ns.action === "resume" ? switchAll(e.currentTarget) : ns.desk ? openPanel({ type: "desk" }) : ns.agent ? openPanel({ type: "agent", key: ns.agent }) : null }, ns.cta);
      box.append(btn);
    }
    const sw = S.root.querySelector("[data-office-switch]");
    if (sw) { sw.textContent = d.enabled ? "Pause all agents" : "Resume all agents"; sw.classList.toggle("danger", d.enabled); sw.classList.toggle("primary", !d.enabled); }
    const navCount = document.querySelector('.rail a[href="/agents"]');
    if (navCount) {
      let c = navCount.querySelector(".count");
      if (d.pending_total && !c) { c = el("span", { class: "count" }); navCount.append(c); }
      if (c) { if (d.pending_total) c.textContent = String(d.pending_total); else c.remove(); }
    }
    syncRoster();
    renderPanel(false);
  }

  /* ================================================================= data */
  function apply(snap, first) {
    const prevBell = S.snap && S.snap.bell_at;
    S.snap = snap; S.tz = snap.timezone;
    S.byKey = Object.fromEntries(snap.agents.map((a) => [a.key, a]));
    S.queue = S.queue.filter((k) => S.byKey[k]?.status === "waiting");
    for (const a of snap.agents) if (a.status === "waiting" && !S.queue.includes(a.key) && S.L.rooms[a.key]) S.queue.push(a.key);
    for (const a of snap.agents) if (S.L.rooms[a.key] && !actor(a.key)) S.actors.push({ key: a.key, f: 0, x: 0, facing: 1, path: [], walkT: 0, arrived: false, placed: false, cheerUntil: 0 });
    const t = performance.now() / 1000;
    const fresh = snap.bell_at && Date.now() - new Date(snap.bell_at).getTime() < 10 * 60 * 1000;
    if ((!first && snap.bell_at && snap.bell_at !== prevBell) || (first && fresh)) {
      S.bellUntil = t + 3; coins(); const s = actor("closer"); if (s) s.cheerUntil = t + 3;
      if (!first) toast("New subscription. Ding!");
    }
    syncChrome();
  }
  async function refresh() {
    if (!S || S.fetching) return;
    S.fetching = true;
    try {
      const r = await fetch("/agents/office.json", { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (r.redirected || r.status === 401) { S.stopped = true; return; }
      if (r.ok) apply(await r.json(), false);
    } catch (_) { /* offline for a moment; try again on the next tick */ }
    finally { if (S) { S.fetching = false; schedule(); } }
  }
  function schedule() {
    clearTimeout(S.pollT);
    if (S.stopped || !S.root.isConnected) return;
    const busyNow = S.snap.agents.some((a) => a.status === "running");
    S.pollT = setTimeout(refresh, document.hidden ? 30000 : busyNow ? 2500 : 7000);
  }

  /* ======================================================== mount/teardown */
  let S = null;
  function teardown() {
    if (!S) return;
    cancelAnimationFrame(S.raf); clearTimeout(S.pollT);
    S.ro?.disconnect(); S.io?.disconnect();
    removeEventListener("keydown", S.onKey); document.removeEventListener("visibilitychange", S.onVis);
    S = null;
  }
  function mount() {
    const root = document.querySelector("[data-office]");
    if (!root || root.dataset.mounted) return;
    teardown();
    root.dataset.mounted = "1";
    const dataEl = root.querySelector("[data-office-snapshot]");
    let snap; try { snap = JSON.parse(dataEl.textContent); } catch (_) { return; }
    const cv = root.querySelector(".office-canvas");
    S = {
      root, cv, stage: root.querySelector("[data-office-stage]"), panel: root.querySelector("[data-office-panel]"),
      sctx: cv.getContext("2d"), actors: [], queue: [], bubbles: new Map(), particles: [], doors: {}, screens: {},
      lastLine: {}, logIdx: {}, seen: loadSeen(), rot: 0, askIdx: 0, nextAsk: 0, nextDirect: 0, nextConvo: 4, nextCoffee: 20,
      bellUntil: 0, hour: 12, visible: true, lastRender: 0, view: null, selected: null, hover: null, tz: snap.timezone,
    };
    S.hour = localHour();
    resize();
    apply(snap, true);
    cv.tabIndex = 0;
    cv.addEventListener("pointermove", onMove);
    cv.addEventListener("pointerleave", () => { S && (S.hover = null); });
    cv.addEventListener("click", onClick);
    root.querySelector("[data-panel-close]").addEventListener("click", closePanel);
    root.querySelector("[data-office-switch]")?.addEventListener("click", (e) => switchAll(e.currentTarget));
    S.onKey = (e) => { if (e.key === "Escape" && S && !S.panel.hidden) closePanel(); };
    S.onVis = () => { if (S && !document.hidden) refresh(); };
    addEventListener("keydown", S.onKey);
    document.addEventListener("visibilitychange", S.onVis);
    S.ro = new ResizeObserver(() => resize()); S.ro.observe(S.stage);
    if ("IntersectionObserver" in window) { S.io = new IntersectionObserver(([e]) => { if (S) S.visible = e.isIntersecting; }); S.io.observe(cv); }
    root.querySelector("[data-office-loading]")?.remove();
    S.raf = requestAnimationFrame(frame);
    schedule();
  }
  window.DesklineOffice = { mount };
  mount();
})();
