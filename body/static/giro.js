/* The image spin itself (BODY-CUERPO-SPIN-HOY-1), ported from Carlos's approved mock
   (research/avatar-3d-mock/index.html v10) -- 16 views 22.5 degrees apart, six layers, the muscle
   layer tinted by its REAL training state (never the mock's own invented demo numbers). Loaded
   once, only after the person chooses 3D (giro-interruptor.js); `window.Giro.iniciar(card)` wires
   one card. REGION mirrors body/giro.py's own GIRO_REGIONES byte for byte -- the mask's own grey
   value, divided by 10, is the index into it (research/avatar-giro/regiones_render.py). */
(function () {
  "use strict";

  var N = 16, PASO = 22.5;
  var REGION = [null, "pecho", "hombros", "deltoides_post", "biceps", "triceps", "antebrazos", "abdomen",
                "oblicuos", "trapecio", "cuello", "espalda_alta", "dorsales", "lumbares", "gluteos",
                "cuadriceps", "aductores", "isquios", "gemelos", "tibiales"];
  var ORDEN_ESTADOS = ["en_rango", "recuperando", "subentrenado", "sobrecargado", "sin_datos", "no_medido"];
  var CAPA_TINTADA = "giro";

  function mod(a, n) { return ((a % n) + n) % n; }

  function leerJson(card, clase) {
    var el = card.querySelector("." + clase);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function iniciar(card) {
    var track = card.querySelector(".cuerpo3d-track");
    if (!track || track.dataset.girado) return;
    track.dataset.girado = "1";

    var datos = {
      estados: leerJson(card, "cuerpo3d-regiones") || {},
      colores: leerJson(card, "cuerpo3d-colores") || {},
      nombres: leerJson(card, "cuerpo3d-nombres") || {},
      estadosTexto: leerJson(card, "cuerpo3d-estados-texto") || {},
      objetivo: leerJson(card, "cuerpo3d-objetivo") || [],
      vistasTexto: leerJson(card, "cuerpo3d-vistas") || [],
      rutasCapas: leerJson(card, "cuerpo3d-rutas-capas") || {},
      rutasMascaras: leerJson(card, "cuerpo3d-rutas-mascaras") || [],
      anguloTexto: leerJson(card, "cuerpo3d-angulo-texto") || "{vista}",
    };
    var objetivoSet = {};
    datos.objetivo.forEach(function (r) { objetivoSet[r] = true; });

    var escena = track.querySelector(".giro-escena");
    var ca = track.querySelector(".giro-cuadro.a");
    var cb = track.querySelector(".giro-cuadro.b");
    var tinte = track.querySelector(".giro-tinte");
    var meta = track.querySelector(".giro-meta");
    var cargando = track.querySelector(".giro-cargando");
    var esfera = track.querySelector(".giro-esfera");
    var angulo = track.querySelector(".giro-angulo");
    var leyenda = track.querySelector(".giro-leyenda");
    var detalle = card.querySelector(".cuerpo3d-detalle");
    var objetivoCaption = track.querySelector(".giro-objetivo-caption");
    var botonesCapa = track.querySelectorAll(".giro-capa-btn");
    var botonesVista = track.querySelectorAll("[data-ir]");
    if (!escena || !ca || !cb || !tinte || !meta) return;

    var ctxT = tinte.getContext("2d"), ctxM = meta.getContext("2d");
    var quieto = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)");

    for (var j = 0; j < N; j++) esfera.appendChild(document.createElement("i"));

    var capa = CAPA_TINTADA;
    var cargadas = {}, etiquetas = {};
    var pos = 0, animando = null;

    function listaDe(c) { return datos.rutasCapas[c] || []; }

    function precargar(c) {
      if (!cargadas[c]) {
        cargadas[c] = Promise.all(listaDe(c).map(function (src) {
          return new Promise(function (ok) {
            var img = new Image();
            img.onload = function () { (img.decode ? img.decode() : Promise.resolve()).then(ok, ok); };
            img.onerror = ok;
            img.src = src;
          });
        }));
      }
      return cargadas[c];
    }

    function cargarMascara(k) {
      if (etiquetas[k]) return etiquetas[k];
      etiquetas[k] = new Promise(function (ok) {
        var src = datos.rutasMascaras[k];
        if (!src) { ok(null); return; }
        var im = new Image();
        im.onload = function () {
          var c = document.createElement("canvas");
          c.width = im.naturalWidth; c.height = im.naturalHeight;
          var x = c.getContext("2d");
          x.drawImage(im, 0, 0);
          var d = x.getImageData(0, 0, c.width, c.height).data, n = c.width * c.height, a = new Uint8Array(n);
          for (var i = 0; i < n; i++) a[i] = Math.round(d[i * 4] / 10);
          ok({ a: a, w: c.width, h: c.height });
        };
        im.onerror = function () { ok(null); };
        im.src = src;
      });
      return etiquetas[k];
    }

    var pedidoPintura = -1;
    function pintar(k) {
      var tintar = capa === CAPA_TINTADA;
      tinte.style.opacity = tintar ? "0.72" : "0";
      escena.classList.toggle("con-meta", tintar && datos.objetivo.length > 0);
      if (leyenda) leyenda.hidden = !tintar;
      if (objetivoCaption) objetivoCaption.hidden = !tintar;
      if (detalle) detalle.hidden = !tintar;
      if (!tintar) return;
      pedidoPintura = k;
      cargarMascara(k).then(function (L) {
        if (!L || pedidoPintura !== k) return;
        tinte.width = meta.width = L.w;
        tinte.height = meta.height = L.h;
        var img = ctxT.createImageData(L.w, L.h), m = ctxM.createImageData(L.w, L.h), d = img.data, dm = m.data;
        var colores = REGION.map(function (r) {
          if (!r) return null;
          var e = datos.estados[r];
          return e ? (datos.colores[e] || null) : null;
        });
        var objetivo = REGION.map(function (r) { return !!r && !!objetivoSet[r]; });
        var p, n = L.a.length, r, c;
        for (p = 0; p < n; p++) {
          r = L.a[p];
          if (!r) continue;
          c = colores[r];
          if (c) { d[p * 4] = c[0]; d[p * 4 + 1] = c[1]; d[p * 4 + 2] = c[2]; d[p * 4 + 3] = 255; }
        }
        var W = L.w, H = L.h, R = 4, x, y, j2;
        for (y = R; y < H - R; y++) {
          for (x = R; x < W - R; x++) {
            j2 = y * W + x;
            if (!objetivo[L.a[j2]]) continue;
            if (objetivo[L.a[j2 - R]] && objetivo[L.a[j2 + R]] && objetivo[L.a[j2 - R * W]] && objetivo[L.a[j2 + R * W]]) continue;
            dm[j2 * 4] = 34; dm[j2 * 4 + 1] = 40; dm[j2 * 4 + 2] = 52; dm[j2 * 4 + 3] = 235;
          }
        }
        ctxT.putImageData(img, 0, 0);
        ctxM.putImageData(m, 0, 0);
      });
    }

    function mostrar(p) {
      pos = p;
      var srcs = listaDe(capa);
      var q = mod(p, N), i = Math.floor(q), f = q - i;
      if (quieto && quieto.matches) { i = mod(Math.round(q), N); f = 0; }
      var s = mod(i + 1, N);
      if (srcs[i] && ca.getAttribute("src") !== srcs[i]) ca.src = srcs[i];
      if (srcs[s] && cb.getAttribute("src") !== srcs[s]) cb.src = srcs[s];
      var g = Math.min(1, Math.max(0, (f - 0.25) / 0.5));
      cb.style.opacity = (g * g * (3 - 2 * g)).toFixed(3);
      var cerca = mod(Math.round(q), N);
      [].forEach.call(esfera.children, function (el, k) { el.className = k === cerca ? "on" : ""; });
      var nombre = datos.vistasTexto[cerca] || "";
      if (angulo) angulo.textContent = nombre;
      var grados = Math.round(cerca * PASO);
      var texto = (cerca % 4 === 0) ? nombre
        : datos.anguloTexto.replace("{vista}", nombre).replace("{grados}", String(grados));
      escena.setAttribute("aria-valuenow", String(grados));
      escena.setAttribute("aria-valuetext", texto);
      botonesVista.forEach(function (b) {
        var on = +b.dataset.ir === cerca;
        b.setAttribute("aria-checked", String(on));
        b.classList.toggle("on", on);
      });
      pintar(cerca);
    }

    function ir(destino) {
      if (animando) cancelAnimationFrame(animando);
      if (quieto && quieto.matches) { mostrar(destino); return; }
      var desde = pos, t0 = performance.now(), dur = Math.min(520, 140 + 50 * Math.abs(destino - desde));
      (function paso(t) {
        var u = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - u, 3);
        mostrar(desde + (destino - desde) * e);
        animando = u < 1 ? requestAnimationFrame(paso) : null;
      })(t0);
    }

    function cercano(destino) {
      var d = mod(destino - pos, N);
      if (d > N / 2) d -= N;
      return pos + d;
    }

    var x0 = null, p0 = 0, id = null, toque = null;
    escena.addEventListener("pointerdown", function (e) {
      if (animando) { cancelAnimationFrame(animando); animando = null; }
      x0 = e.clientX; p0 = pos; id = e.pointerId;
      escena.setPointerCapture(id);
      escena.classList.add("arrastrando");
      toque = { x: e.clientX, y: e.clientY };
    });
    escena.addEventListener("pointermove", function (e) {
      if (x0 === null || e.pointerId !== id) return;
      var porPaso = Math.max(24, escena.clientWidth / 10);
      mostrar(p0 - (e.clientX - x0) / porPaso);
    });
    function soltar(e) {
      if (x0 === null || e.pointerId !== id) return;
      x0 = null;
      escena.classList.remove("arrastrando");
      if (toque && Math.abs(e.clientX - toque.x) < 6 && Math.abs(e.clientY - toque.y) < 6) tocar(e.clientX, e.clientY);
      toque = null;
      ir(Math.round(pos));
    }
    escena.addEventListener("pointerup", soltar);
    escena.addEventListener("pointercancel", soltar);
    escena.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") { ir(Math.round(pos) + 1); e.preventDefault(); }
      else if (e.key === "ArrowRight") { ir(Math.round(pos) - 1); e.preventDefault(); }
      else if (e.key === "Home") { ir(cercano(0)); e.preventDefault(); }
    });
    botonesVista.forEach(function (b) {
      b.addEventListener("click", function () { ir(cercano(+b.dataset.ir)); });
    });
    botonesCapa.forEach(function (b) {
      b.addEventListener("click", function () {
        var c = b.dataset.capa;
        if (c === capa) return;
        botonesCapa.forEach(function (x) {
          var on = x === b;
          x.classList.toggle("on", on);
          x.setAttribute("aria-checked", String(on));
        });
        precargar(c).then(function () { capa = c; mostrar(Math.round(pos)); });
      });
    });

    function construirLeyenda() {
      var cuenta = {};
      REGION.forEach(function (r) {
        if (!r) return;
        var e = datos.estados[r];
        if (e) cuenta[e] = (cuenta[e] || 0) + 1;
      });
      if (!leyenda) return;
      leyenda.innerHTML = "";
      ORDEN_ESTADOS.forEach(function (e) {
        if (!cuenta[e]) return;
        var c = datos.colores[e];
        var sp = document.createElement("span"), i = document.createElement("i");
        if (c) { i.style.background = "rgb(" + c.join(",") + ")"; }
        else { i.style.background = "transparent"; i.style.borderStyle = "dashed"; }
        sp.appendChild(i);
        sp.appendChild(document.createTextNode((datos.estadosTexto[e] || e) + " · " + cuenta[e]));
        leyenda.appendChild(sp);
      });
    }

    function tocar(cx, cy) {
      if (capa !== CAPA_TINTADA) return;
      var k = mod(Math.round(pos), N);
      var espera = etiquetas[k];
      if (!espera) return;
      espera.then(function (L) {
        if (!L) return;
        var b = tinte.getBoundingClientRect();
        var esc = Math.min(b.width / L.w, b.height / L.h);
        var ox = b.left + (b.width - L.w * esc) / 2, oy = b.top + (b.height - L.h * esc) / 2;
        var x = Math.floor((cx - ox) / esc), y = Math.floor((cy - oy) / esc);
        if (x < 0 || y < 0 || x >= L.w || y >= L.h) return;
        var votos = {}, mejor = 0, ganador = 0;
        for (var dy = -12; dy <= 12; dy += 3) {
          for (var dx = -12; dx <= 12; dx += 3) {
            var yy = y + dy, xx = x + dx;
            if (yy < 0 || xx < 0 || yy >= L.h || xx >= L.w) continue;
            var r = L.a[yy * L.w + xx];
            if (!r) continue;
            votos[r] = (votos[r] || 0) + 1;
            if (votos[r] > mejor) { mejor = votos[r]; ganador = r; }
          }
        }
        if (!ganador || !detalle) return;
        var clave = REGION[ganador];
        var estado = datos.estados[clave];
        var nombre = datos.nombres[clave] || clave;
        var textoEstado = estado ? (datos.estadosTexto[estado] || estado) : "";
        detalle.textContent = nombre + " — " + textoEstado;
      });
    }

    precargar(capa).then(function () {
      mostrar(0);
      if (cargando) cargando.hidden = true;
      cargarMascara(0);
      for (var k = 1; k < N; k++) cargarMascara(k);
      var resto = Object.keys(datos.rutasCapas).filter(function (c) { return c !== capa; });
      resto.reduce(function (p, c) { return p.then(function () { return precargar(c); }); }, Promise.resolve());
    });

    construirLeyenda();
  }

  window.Giro = { iniciar: iniciar };
})();
