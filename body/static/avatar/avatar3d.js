/* avatar3d.js -- Body's 3D body view, WebGL2, no third-party code.
 *
 * One figure, three layers: a translucent skin shell, named muscle regions, and the
 * organs (heart, brain). Every region's state is ONE number in a uniform array, so
 * "trained", "in today's plan" and "limited" are data, never geometry.
 *
 * The interim figure (figura-v0.json) is built at load from signed-distance primitives
 * with surface nets. The real mesh replaces `construirFigura` and nothing else.
 *
 * Battery: it draws only when something changes, caps the pixel ratio at 2, and never
 * spins on its own. Reduced motion jumps instead of turning.
 */
(function () {
  "use strict";

  // ---------- small vector and matrix helpers ----------
  function norm(v) { var l = Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function base(dir) {
    var y = norm(dir), t = Math.abs(y[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
    var x = norm(cross(y, t)), z = cross(x, y);
    return [x, y, z];
  }
  function mul(a, b) {
    var o = new Float32Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      var s = 0; for (var k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s;
    }
    return o;
  }
  function perspectiva(fovy, asp, n, f) {
    var t = 1 / Math.tan(fovy / 2), o = new Float32Array(16);
    o[0] = t / asp; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = 2 * f * n / (n - f);
    return o;
  }
  function traslada(x, y, z) { var o = new Float32Array(16); o[0] = o[5] = o[10] = o[15] = 1; o[12] = x; o[13] = y; o[14] = z; return o; }
  function giraY(a) { var o = traslada(0, 0, 0), c = Math.cos(a), s = Math.sin(a); o[0] = c; o[2] = -s; o[8] = s; o[10] = c; return o; }

  // ---------- signed distances ----------
  function smin(a, b, k) { if (k <= 0) return Math.min(a, b); var h = Math.max(k - Math.abs(a - b), 0) / k; return Math.min(a, b) - h * h * k * 0.25; }
  function prepararElipsoide(e, espejado) {
    var c = e.c.slice(), d = e.dir ? e.dir.slice() : null;
    if (espejado) { c[0] = -c[0]; if (d) d[0] = -d[0]; }
    return { c: c, r: e.r, b: d ? base(d) : null };
  }
  function dElipsoide(x, y, z, e) {
    var dx = x - e.c[0], dy = y - e.c[1], dz = z - e.c[2], qx = dx, qy = dy, qz = dz, b = e.b;
    if (b) { qx = dx * b[0][0] + dy * b[0][1] + dz * b[0][2]; qy = dx * b[1][0] + dy * b[1][1] + dz * b[1][2]; qz = dx * b[2][0] + dy * b[2][1] + dz * b[2][2]; }
    var rx = e.r[0], ry = e.r[1], rz = e.r[2];
    var ax = qx / rx, ay = qy / ry, az = qz / rz, bx = ax / rx, by = ay / ry, bz = az / rz;
    var k0 = Math.sqrt(ax * ax + ay * ay + az * az), k1 = Math.sqrt(bx * bx + by * by + bz * bz);
    return k1 < 1e-9 ? -Math.min(rx, ry, rz) : k0 * (k0 - 1) / k1;
  }
  function dCono(x, y, z, p) { // round cone, after Inigo Quilez
    var a = p.a, b = p.b, r1 = p.r1, r2 = p.r2;
    var bax = b[0] - a[0], bay = b[1] - a[1], baz = b[2] - a[2];
    var l2 = bax * bax + bay * bay + baz * baz, rr = r1 - r2, a2 = l2 - rr * rr, il2 = 1 / l2;
    var pax = x - a[0], pay = y - a[1], paz = z - a[2];
    var yy = pax * bax + pay * bay + paz * baz, zz = yy - l2;
    var xvx = pax * l2 - bax * yy, xvy = pay * l2 - bay * yy, xvz = paz * l2 - baz * yy;
    var x2 = xvx * xvx + xvy * xvy + xvz * xvz, y2 = yy * yy * l2, z2 = zz * zz * l2;
    var k = Math.sign(rr) * rr * rr * x2;
    if (Math.sign(zz) * a2 * z2 > k) return Math.sqrt(x2 + z2) * il2 - r2;
    if (Math.sign(yy) * a2 * y2 < k) return Math.sqrt(x2 + y2) * il2 - r1;
    return (Math.sqrt(x2 * a2 * il2) + yy * rr) * il2 - r1;
  }
  function prepararPiel(lista) {
    return lista.map(function (p) {
      var q = { t: p.t, k: p.k || 0, espejo: !!p.espejo };
      if (p.t === "elipsoide") q.e = prepararElipsoide(p, false);
      else { q.a = p.a; q.b = p.b; q.r1 = p.r1; q.r2 = p.r2; }
      return q;
    });
  }
  function dPiel(x, y, z, prims) {
    var d = 1e9, ax = Math.abs(x);
    for (var i = 0; i < prims.length; i++) {
      var p = prims[i], px = p.espejo ? ax : x;
      var v = p.t === "elipsoide" ? dElipsoide(px, y, z, p.e) : dCono(px, y, z, p);
      d = i === 0 ? v : smin(d, v, p.k);
    }
    return d;
  }

  // ---------- surface nets ----------
  var ARISTAS = [[0, 1], [2, 3], [4, 5], [6, 7], [0, 2], [1, 3], [4, 6], [5, 7], [0, 4], [1, 5], [2, 6], [3, 7]];
  function redes(campo, nx, ny, nz, o, h, region, sal) {
    var cx = nx - 1, cy = ny - 1, cz = nz - 1, vid = new Int32Array(cx * cy * cz).fill(-1), v = new Float32Array(8);
    for (var k = 0; k < cz; k++) for (var j = 0; j < cy; j++) for (var i = 0; i < cx; i++) {
      var mask = 0;
      for (var c = 0; c < 8; c++) {
        var f = campo[(i + (c & 1)) + nx * ((j + ((c >> 1) & 1)) + ny * (k + ((c >> 2) & 1)))];
        v[c] = f; if (f < 0) mask |= 1 << c;
      }
      if (mask === 0 || mask === 255) continue;
      var sx = 0, sy = 0, sz = 0, n = 0;
      for (var e = 0; e < 12; e++) {
        var A = ARISTAS[e][0], B = ARISTAS[e][1], fa = v[A], fb = v[B];
        if ((fa < 0) !== (fb < 0)) {
          var t = fa / (fa - fb);
          sx += (A & 1) + ((B & 1) - (A & 1)) * t;
          sy += ((A >> 1) & 1) + (((B >> 1) & 1) - ((A >> 1) & 1)) * t;
          sz += ((A >> 2) & 1) + (((B >> 2) & 1) - ((A >> 2) & 1)) * t;
          n++;
        }
      }
      vid[i + cx * (j + cy * k)] = sal.pos.length / 3;
      sal.pos.push(o[0] + (i + sx / n) * h, o[1] + (j + sy / n) * h, o[2] + (k + sz / n) * h);
      sal.reg.push(region);
    }
    function celda(i, j, k) { return vid[i + cx * (j + cy * k)]; }
    function quad(a, b, c, d, eje, dentro) {
      if (a < 0 || b < 0 || c < 0 || d < 0) return;
      var P = sal.pos;
      var e1 = [P[b * 3] - P[a * 3], P[b * 3 + 1] - P[a * 3 + 1], P[b * 3 + 2] - P[a * 3 + 2]];
      var e2 = [P[c * 3] - P[a * 3], P[c * 3 + 1] - P[a * 3 + 1], P[c * 3 + 2] - P[a * 3 + 2]];
      var nn = cross(e1, e2), s = dentro ? 1 : -1;
      if (nn[eje] * s < 0) sal.idx.push(a, c, b, a, d, c); else sal.idx.push(a, b, c, a, c, d);
    }
    for (k = 0; k < nz; k++) for (j = 0; j < ny; j++) for (i = 0; i < nx; i++) {
      var dentro = campo[i + nx * (j + ny * k)] < 0;
      if (i < cx && j > 0 && k > 0 && j < cy && k < cz && dentro !== (campo[(i + 1) + nx * (j + ny * k)] < 0))
        quad(celda(i, j - 1, k - 1), celda(i, j, k - 1), celda(i, j, k), celda(i, j - 1, k), 0, dentro);
      if (j < cy && i > 0 && k > 0 && i < cx && k < cz && dentro !== (campo[i + nx * ((j + 1) + ny * k)] < 0))
        quad(celda(i - 1, j, k - 1), celda(i, j, k - 1), celda(i, j, k), celda(i - 1, j, k), 1, dentro);
      if (k < cz && i > 0 && j > 0 && i < cx && j < cy && dentro !== (campo[i + nx * (j + ny * (k + 1))] < 0))
        quad(celda(i - 1, j - 1, k), celda(i, j - 1, k), celda(i, j, k), celda(i - 1, j, k), 2, dentro);
    }
  }
  function normales(sal) {
    var P = sal.pos, I = sal.idx, N = new Float32Array(P.length);
    for (var t = 0; t < I.length; t += 3) {
      var a = I[t] * 3, b = I[t + 1] * 3, c = I[t + 2] * 3;
      var nn = cross([P[b] - P[a], P[b + 1] - P[a + 1], P[b + 2] - P[a + 2]], [P[c] - P[a], P[c + 1] - P[a + 1], P[c + 2] - P[a + 2]]);
      for (var q = 0; q < 3; q++) { var v = I[t + q] * 3; N[v] += nn[0]; N[v + 1] += nn[1]; N[v + 2] += nn[2]; }
    }
    for (var i = 0; i < N.length; i += 3) { var l = Math.sqrt(N[i] * N[i] + N[i + 1] * N[i + 1] + N[i + 2] * N[i + 2]) || 1; N[i] /= l; N[i + 1] /= l; N[i + 2] /= l; }
    return N;
  }
  function suavizar(sal, veces) { // one-ring Laplacian, removes the grid's stair steps
    var P = sal.pos, I = sal.idx, n = P.length / 3;
    for (var it = 0; it < veces; it++) {
      var acc = new Float32Array(P.length), cnt = new Uint16Array(n);
      for (var t = 0; t < I.length; t += 3) for (var q = 0; q < 3; q++) {
        var a = I[t + q], b = I[t + (q + 1) % 3];
        acc[a * 3] += P[b * 3]; acc[a * 3 + 1] += P[b * 3 + 1]; acc[a * 3 + 2] += P[b * 3 + 2]; cnt[a]++;
      }
      for (var v = 0; v < n; v++) if (cnt[v]) for (var d = 0; d < 3; d++) P[v * 3 + d] = P[v * 3 + d] * 0.5 + 0.5 * acc[v * 3 + d] / cnt[v];
    }
  }

  // ---------- the interim figure ----------
  function construirFigura(fig) {
    var h = fig.rejilla_m, mn = fig.caja.min, mx = fig.caja.max;
    var nx = Math.ceil((mx[0] - mn[0]) / h) + 1, ny = Math.ceil((mx[1] - mn[1]) / h) + 1, nz = Math.ceil((mx[2] - mn[2]) / h) + 1;
    var prims = prepararPiel(fig.piel), piel = new Float32Array(nx * ny * nz);
    for (var k = 0; k < nz; k++) for (var j = 0; j < ny; j++) for (var i = 0; i < nx; i++)
      piel[i + nx * (j + ny * k)] = dPiel(mn[0] + i * h, mn[1] + j * h, mn[2] + k * h, prims);

    var capa = { piel: { pos: [], reg: [], idx: [] }, cuerpo: { pos: [], reg: [], idx: [] } };
    redes(piel, nx, ny, nz, mn, h, 0, capa.piel);

    // the core: unlabelled muscle under everything, cut below the head so the skull stays clear
    var dentro = fig.nucleo.dentro_m, corte = fig.nucleo.corte_y_m, nucleo = new Float32Array(piel.length);
    for (k = 0; k < nz; k++) for (j = 0; j < ny; j++) for (i = 0; i < nx; i++) {
      var g = i + nx * (j + ny * k); nucleo[g] = Math.max(piel[g] + dentro, (mn[1] + j * h) - corte);
    }
    redes(nucleo, nx, ny, nz, mn, h, 0, capa.cuerpo);

    // the muscles: each blob is cut to just under the skin, so it follows the body's shape
    var regiones = fig.regiones, blobs = [];
    fig.musculos.forEach(function (m) {
      blobs.push({ region: regiones.indexOf(m.region), e: prepararElipsoide(m, false), organo: false });
      if (m.espejo) blobs.push({ region: regiones.indexOf(m.region), e: prepararElipsoide(m, true), organo: false });
    });
    // named regions first, so where two blobs meet, the named one sits on top
    blobs.sort(function (a, b) { return (a.region === 0) - (b.region === 0); });
    fig.organos.forEach(function (m) { blobs.push({ region: regiones.indexOf(m.region), e: prepararElipsoide(m, false), organo: true }); });
    blobs.forEach(function (bl, n) {
      var e = bl.e, rmax = Math.max(e.r[0], e.r[1], e.r[2]) + 2 * h;
      var i0 = Math.max(0, Math.floor((e.c[0] - rmax - mn[0]) / h)), i1 = Math.min(nx - 1, Math.ceil((e.c[0] + rmax - mn[0]) / h));
      var j0 = Math.max(0, Math.floor((e.c[1] - rmax - mn[1]) / h)), j1 = Math.min(ny - 1, Math.ceil((e.c[1] + rmax - mn[1]) / h));
      var k0 = Math.max(0, Math.floor((e.c[2] - rmax - mn[2]) / h)), k1 = Math.min(nz - 1, Math.ceil((e.c[2] + rmax - mn[2]) / h));
      var lx = i1 - i0 + 1, ly = j1 - j0 + 1, lz = k1 - k0 + 1, campo = new Float32Array(lx * ly * lz);
      var hueco = 0.003 + 0.0001 * n;
      for (var kk = 0; kk < lz; kk++) for (var jj = 0; jj < ly; jj++) for (var ii = 0; ii < lx; ii++) {
        var X = mn[0] + (i0 + ii) * h, Y = mn[1] + (j0 + jj) * h, Z = mn[2] + (k0 + kk) * h;
        var d = dElipsoide(X, Y, Z, e);
        if (!bl.organo) d = Math.max(d, piel[(i0 + ii) + nx * ((j0 + jj) + ny * (k0 + kk))] + hueco);
        // the grid's edge closes the surface
        if (ii === 0 || jj === 0 || kk === 0 || ii === lx - 1 || jj === ly - 1 || kk === lz - 1) d = Math.max(d, h);
        campo[ii + lx * (jj + ly * kk)] = d;
      }
      redes(campo, lx, ly, lz, [mn[0] + i0 * h, mn[1] + j0 * h, mn[2] + k0 * h], h, bl.region, capa.cuerpo);
    });

    suavizar(capa.piel, 2);
    return capa;
  }

  // ---------- shaders ----------
  var VS = "#version 300 es\n" +
    "in vec3 aP; in vec3 aN; in float aR;\n" +
    "uniform mat4 uProy, uVista;\n" +
    "out vec3 vN; out vec3 vO; out vec3 vON; flat out int vR;\n" +
    "void main(){ vec4 p = uVista*vec4(aP,1.0); vN = mat3(uVista)*aN; vO = aP; vON = aN; vR = int(aR+0.5); gl_Position = uProy*p; }";
  var FS_CUERPO = "#version 300 es\nprecision highp float;\n" +
    "in vec3 vN; in vec3 vO; in vec3 vON; flat in int vR;\n" +
    "uniform sampler2D uMat; uniform vec3 uMatCR; uniform sampler2D uFib;\n" +
    "uniform float uEst[13]; uniform float uOrg[13];\n" +
    "uniform vec3 uGlow, uRaya, uOrgano; uniform float uPiso, uAlfaM, uDpr; uniform int uSel;\n" +
    "out vec4 o;\n" +
    "float luma(vec3 c){ return dot(c, vec3(0.299,0.587,0.114)); }\n" +
    "void main(){\n" +
    "  vec3 n = normalize(vN);\n" +
    "  vec3 c = texture(uMat, uMatCR.xy + vec2(n.x,-n.y)*uMatCR.z).rgb;\n" +
    "  c = uPiso + c*(1.0-uPiso);\n" +
    "  vec3 w = pow(abs(normalize(vON)), vec3(4.0)); w /= (w.x+w.y+w.z);\n" +
    "  float f = texture(uFib, vO.zy*9.0).r*w.x + texture(uFib, vO.xz*9.0).r*w.y + texture(uFib, vO.xy*9.0).r*w.z;\n" +
    "  float org = uOrg[vR];\n" +
    "  c *= mix(0.86 + 0.28*f, 1.0, org);\n" +
    "  c = mix(c, uOrgano*(0.55+0.6*luma(c)), org*0.8);\n" +
    "  float e = uEst[vR];\n" +
    "  float rim = pow(1.0 - abs(n.z), 2.0);\n" +
    "  float diag = fract((gl_FragCoord.x + gl_FragCoord.y)/(7.0*uDpr));\n" +
    "  if (org < 0.5 && uAlfaM < 0.02) discard;\n" +
    "  if (e > 0.75 && org > 0.5) { c += uGlow*rim*0.9; }\n" +
    "  else if (e > 0.75) { c = mix(c, uGlow*(0.55+0.7*luma(c)), 0.62); c += uGlow*rim*0.35; }\n" +
    "  else if (e > 0.25) { float pun = step(0.5, fract(gl_FragCoord.y/(5.0*uDpr))); c = mix(c, uGlow*(0.6+0.6*luma(c)), 0.28 + 0.18*pun); }\n" +
    "  else if (e < -0.25) { float s = step(0.55, diag); c = mix(vec3(luma(c)), uRaya, 0.35 + 0.35*s); }\n" +
    "  if (vR == uSel && vR > 0) c += vec3(0.07) + rim*0.25;\n" +
    "  float a = org > 0.5 ? 1.0 : uAlfaM;\n" +
    "  o = vec4(c*a, a);\n" +
    "}";
  var FS_PIEL = "#version 300 es\nprecision highp float;\n" +
    "in vec3 vN; in vec3 vO; in vec3 vON; flat in int vR;\n" +
    "uniform sampler2D uMat; uniform vec3 uMatCR; uniform float uAlfaP;\n" +
    "out vec4 o;\n" +
    "void main(){\n" +
    "  vec3 n = normalize(vN);\n" +
    "  vec3 c = texture(uMat, uMatCR.xy + vec2(n.x,-n.y)*uMatCR.z).rgb;\n" +
    "  float fr = pow(1.0 - abs(n.z), 1.6);\n" +
    "  float a = mix(0.10, 0.62, fr) * uAlfaP;\n" +
    "  o = vec4(c*a, a);\n" +
    "}";
  var FS_ID = "#version 300 es\nprecision highp float;\n" +
    "flat in int vR; in vec3 vN; in vec3 vO; in vec3 vON; out vec4 o;\n" +
    "void main(){ o = vec4(float(vR)/255.0, 0.0, 0.0, 1.0); }";

  function programa(gl, fs) {
    function sh(t, s) { var x = gl.createShader(t); gl.shaderSource(x, s); gl.compileShader(x); if (!gl.getShaderParameter(x, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(x)); return x; }
    var p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
    gl.bindAttribLocation(p, 0, "aP"); gl.bindAttribLocation(p, 1, "aN"); gl.bindAttribLocation(p, 2, "aR");
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (var i = 0; i < n; i++) { var nom = gl.getActiveUniform(p, i).name.replace("[0]", ""); u[nom] = gl.getUniformLocation(p, nom); }
    return { p: p, u: u };
  }
  function malla(gl, capa) {
    var vao = gl.createVertexArray(); gl.bindVertexArray(vao);
    function buf(datos, loc, tam) { var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, datos, gl.STATIC_DRAW); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, tam, gl.FLOAT, false, 0, 0); }
    buf(new Float32Array(capa.pos), 0, 3); buf(normales(capa), 1, 3); buf(new Float32Array(capa.reg), 2, 1);
    var ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(capa.idx), gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    return { vao: vao, n: capa.idx.length };
  }
  function textura(gl, img, repetir) {
    var t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    var w = repetir ? gl.REPEAT : gl.CLAMP_TO_EDGE;
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, w); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, w);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    return t;
  }
  function hex(c) { c = c.trim().replace("#", ""); return [parseInt(c.slice(0, 2), 16) / 255, parseInt(c.slice(2, 4), 16) / 255, parseInt(c.slice(4, 6), 16) / 255]; }

  var ESTADO = { entrenado: 1, pendiente: 0.5, limitada: -1 };

  function soportado() { try { return !!document.createElement("canvas").getContext("webgl2"); } catch (e) { return false; } }

  /* opts: figura (figura-v0.json), imagenes {claro:{musculo,piel}, oscuro:{musculo,piel}, fibras} as loaded
     <img> elements, marcos {claro:{musculo:[cx,cy,r],piel:[..]}, oscuro:{..}}, colores() -> {glow, raya, organo},
     tema() -> "claro"|"oscuro", alRegion(nombre|null) */
  function crear(lienzo, opts) {
    var gl = lienzo.getContext("webgl2", { antialias: true, premultipliedAlpha: true, alpha: true });
    if (!gl) return null;
    var fig = opts.figura, regiones = fig.regiones;
    var capa = construirFigura(fig);
    var mPiel = malla(gl, capa.piel), mCuerpo = malla(gl, capa.cuerpo);
    var pC = programa(gl, FS_CUERPO), pP = programa(gl, FS_PIEL), pI = programa(gl, FS_ID);
    var tex = {
      claro: { musculo: textura(gl, opts.imagenes.claro.musculo), piel: textura(gl, opts.imagenes.claro.piel) },
      oscuro: { musculo: textura(gl, opts.imagenes.oscuro.musculo), piel: textura(gl, opts.imagenes.oscuro.piel) },
      fibras: textura(gl, opts.imagenes.fibras, true)
    };
    var est = new Float32Array(13), org = new Float32Array(13);
    org[regiones.indexOf("corazon")] = 1; org[regiones.indexOf("cerebro")] = 1;
    var reduce = !!opts.sinAnimacion || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var cam = { giro: 0, giroObj: 0, obj: fig.vistas.musculos.objetivo.slice(), dist: fig.vistas.musculos.distancia, alfa: 1 };
    var meta = { obj: cam.obj.slice(), dist: cam.dist, alfa: 1 };
    var sel = -1, pedido = false, fbo = null, fboTam = [0, 0];

    function tam() {
      var dpr = Math.min(window.devicePixelRatio || 1, 2), w = Math.round(lienzo.clientWidth * dpr), h = Math.round(lienzo.clientHeight * dpr);
      if (lienzo.width !== w || lienzo.height !== h) { lienzo.width = w; lienzo.height = h; }
      return dpr;
    }
    function matrices() {
      var asp = lienzo.width / Math.max(1, lienzo.height);
      var fov = 30 * Math.PI / 180, dist = cam.dist * Math.max(1, 0.8 / asp);
      var v = mul(traslada(0, 0, -dist), mul(giraY(cam.giro), traslada(-cam.obj[0], -cam.obj[1], -cam.obj[2])));
      return { proy: perspectiva(fov, asp, 0.1, 20), vista: v };
    }
    function comunes(p, M) { gl.useProgram(p.p); gl.uniformMatrix4fv(p.u.uProy, false, M.proy); gl.uniformMatrix4fv(p.u.uVista, false, M.vista); }
    function dibujar() {
      pedido = false;
      var dpr = tam(), M = matrices(), tema = opts.tema(), col = opts.colores(), marco = opts.marcos[tema];
      gl.viewport(0, 0, lienzo.width, lienzo.height);
      gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

      comunes(pC, M);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex[tema].musculo); gl.uniform1i(pC.u.uMat, 0);
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, tex.fibras); gl.uniform1i(pC.u.uFib, 1);
      gl.uniform3fv(pC.u.uMatCR, marco.musculo); gl.uniform1fv(pC.u.uEst, est); gl.uniform1fv(pC.u.uOrg, org);
      gl.uniform3fv(pC.u.uGlow, hex(col.glow)); gl.uniform3fv(pC.u.uRaya, hex(col.raya)); gl.uniform3fv(pC.u.uOrgano, hex(col.organo));
      gl.uniform1f(pC.u.uPiso, tema === "oscuro" ? 0.16 : 0.0); gl.uniform1f(pC.u.uDpr, dpr); gl.uniform1i(pC.u.uSel, sel);
      gl.uniform1f(pC.u.uAlfaM, cam.alfa);
      gl.depthMask(cam.alfa > 0.99);
      gl.bindVertexArray(mCuerpo.vao); gl.drawElements(gl.TRIANGLES, mCuerpo.n, gl.UNSIGNED_INT, 0);

      comunes(pP, M);
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, tex[tema].piel); gl.uniform1i(pP.u.uMat, 0);
      gl.uniform3fv(pP.u.uMatCR, marco.piel); gl.uniform1f(pP.u.uAlfaP, 1.0 + 0.5 * (1.0 - cam.alfa));
      gl.depthMask(false);
      gl.bindVertexArray(mPiel.vao); gl.drawElements(gl.TRIANGLES, mPiel.n, gl.UNSIGNED_INT, 0);
      gl.depthMask(true); gl.bindVertexArray(null);

      if (animar()) pedir();
    }
    function animar() {
      var sigue = false;
      function hacia(a, b, f) { var d = b - a; if (Math.abs(d) < 1e-3) return b; sigue = true; return a + d * f; }
      var f = reduce ? 1 : 0.16;
      cam.giro = hacia(cam.giro, cam.giroObj, f);
      cam.dist = hacia(cam.dist, meta.dist, f); cam.alfa = hacia(cam.alfa, meta.alfa, f);
      for (var i = 0; i < 3; i++) cam.obj[i] = hacia(cam.obj[i], meta.obj[i], f);
      return sigue;
    }
    function pedir() { if (!pedido) { pedido = true; requestAnimationFrame(dibujar); } }

    function elegir(px, py) {
      tam();
      var w = lienzo.width, h = lienzo.height;
      if (!fbo || fboTam[0] !== w || fboTam[1] !== h) {
        fbo = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
        var t = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, t); gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8, w, h);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
        var rb = gl.createRenderbuffer(); gl.bindRenderbuffer(gl.RENDERBUFFER, rb); gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, w, h);
        gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, rb);
        fboTam = [w, h];
      }
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, w, h); gl.clearColor(1, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.disable(gl.BLEND); gl.enable(gl.DEPTH_TEST); gl.depthMask(true);
      var M = matrices(); comunes(pI, M);
      gl.bindVertexArray(mCuerpo.vao); gl.drawElements(gl.TRIANGLES, mCuerpo.n, gl.UNSIGNED_INT, 0);
      var px8 = new Uint8Array(4), dpr = lienzo.width / lienzo.clientWidth;
      gl.readPixels(Math.round(px * dpr), Math.round(h - py * dpr), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px8);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.bindVertexArray(null);
      var r = px8[0];
      return r > 0 && r < regiones.length ? r : -1;
    }

    // drag turns the body around its own axis; vertical swipes still scroll the page
    var abajo = null;
    lienzo.style.touchAction = "pan-y";
    lienzo.addEventListener("pointerdown", function (ev) { abajo = { x: ev.clientX, y: ev.clientY, giro: cam.giroObj, movido: false }; });
    lienzo.addEventListener("pointermove", function (ev) {
      if (!abajo) return;
      var dx = ev.clientX - abajo.x;
      if (Math.abs(dx) > 6) abajo.movido = true;
      if (abajo.movido) { cam.giroObj = abajo.giro + dx * 0.012; cam.giro = cam.giroObj; pedir(); }
    });
    function soltar(ev) {
      if (!abajo) return;
      var fue = abajo.movido; abajo = null;
      if (fue) return;
      var b = lienzo.getBoundingClientRect(), r = elegir(ev.clientX - b.left, ev.clientY - b.top);
      sel = r; pedir();
      if (opts.alRegion) opts.alRegion(r > 0 ? regiones[r] : null);
    }
    lienzo.addEventListener("pointerup", soltar);
    lienzo.addEventListener("pointercancel", function () { abajo = null; });
    window.addEventListener("resize", pedir);

    pedir();
    return {
      estados: function (mapa) { // {region: "entrenado"|"pendiente"|"limitada"}
        est.fill(0);
        Object.keys(mapa).forEach(function (k) { var i = regiones.indexOf(k); if (i > 0) est[i] = ESTADO[mapa[k]] || 0; });
        pedir();
      },
      lado: function (cual) { // "frente" | "espalda", taking the shortest turn
        var base = cual === "espalda" ? Math.PI : 0, actual = cam.giroObj;
        cam.giroObj = base + Math.round((actual - base) / (2 * Math.PI)) * 2 * Math.PI; pedir();
      },
      vista: function (nombre) {
        var v = fig.vistas[nombre]; meta.obj = v.objetivo.slice(); meta.dist = v.distancia; meta.alfa = v.musculos_alfa; pedir();
      },
      elegirRegion: function (nombre) { sel = nombre ? regiones.indexOf(nombre) : -1; pedir(); },
      redibujar: pedir,
      triangulos: function () { return (mPiel.n + mCuerpo.n) / 3; }
    };
  }

  window.Avatar3D = { soportado: soportado, crear: crear };
})();
