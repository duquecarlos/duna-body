/* Body's own binding for the optional 3D body view (BODY-AVATAR-3D-VISOR-1): feature-detects
   WebGL2, shows the 2D/3D switch only when it is there, remembers the person's own choice per
   device in localStorage (every read/write in try/catch -- a failure reads as 2D), and fetches
   the viewer script, the stand-in figure and its five images ONLY once 3D is actually chosen, so
   a 2D visit to Hoy never pays for any of it. Hoy's own existing view buttons (Músculos/Espalda/
   Corazón) drive the 3D viewer too, once it exists -- no new buttons besides the switch. The
   ported viewer itself (avatar3d.js) carries no Body-specific wiring; this file is the seam. */
(function () {
  "use strict";

  function soportaWebgl2() {
    try { return !!document.createElement("canvas").getContext("webgl2"); } catch (e) { return false; }
  }

  function tema() { return document.documentElement.dataset.theme === "dark" ? "oscuro" : "claro"; }

  function leerPreferencia() {
    try { return localStorage.getItem("body.cuerpo3d"); } catch (e) { return null; }
  }

  function guardarPreferencia(v) {
    try { localStorage.setItem("body.cuerpo3d", v); } catch (e) { /* 2D stays the default */ }
  }

  function cargarImagen(src) {
    return new Promise(function (ok, mal) {
      var img = new Image();
      img.onload = function () { ok(img); };
      img.onerror = mal;
      img.src = src;
    });
  }

  function crearVisor(canvas, alRegion, listo) {
    var script = document.createElement("script");
    script.src = canvas.dataset.script;
    script.onload = function () {
      Promise.all([
        fetch(canvas.dataset.figura).then(function (r) { return r.json(); }),
        cargarImagen(canvas.dataset.matcapMusculoClaro),
        cargarImagen(canvas.dataset.matcapPielClaro),
        cargarImagen(canvas.dataset.matcapMusculoOscuro),
        cargarImagen(canvas.dataset.matcapPielOscuro),
        cargarImagen(canvas.dataset.fibras),
      ]).then(function (r) {
        listo(window.Avatar3D.crear(canvas, {
          figura: r[0],
          imagenes: { claro: { musculo: r[1], piel: r[2] }, oscuro: { musculo: r[3], piel: r[4] }, fibras: r[5] },
          // where the sphere sits in each matcap image: centre x, centre y, radius (the mock's own
          // measurement of these exact files, copied in unchanged by ruling 1).
          marcos: { claro: { musculo: [0.5, 0.515, 0.37], piel: [0.5, 0.5, 0.43] },
                    oscuro: { musculo: [0.5, 0.5, 0.42], piel: [0.5, 0.5, 0.43] } },
          tema: tema,
          colores: function () {
            var cs = getComputedStyle(document.documentElement);
            return { glow: cs.getPropertyValue("--body").trim(),
                     raya: cs.getPropertyValue("--cuerpo3d-raya").trim(),
                     organo: cs.getPropertyValue("--cuerpo3d-organo").trim() };
          },
          alRegion: alRegion,
        }));
      }).catch(function () { listo(null); });
    };
    script.onerror = function () { listo(null); };
    document.head.appendChild(script);
  }

  function wire(card) {
    var regionesEl = card.querySelector(".cuerpo3d-regiones");
    var nombresEl = card.querySelector(".cuerpo3d-nombres");
    var estadosTextoEl = card.querySelector(".cuerpo3d-estados-texto");
    var seg = card.querySelector(".cuerpo3d-seg");
    if (!regionesEl || !nombresEl || !estadosTextoEl || !seg || !soportaWebgl2()) return;

    // Three flat blobs, not one nested object (see the template's own comment) -- `regiones`
    // reads `null` on a day with no state at all, read here the same as `{}`.
    var datos = {
      regiones: JSON.parse(regionesEl.textContent) || {},
      nombres: JSON.parse(nombresEl.textContent),
      estados_texto: JSON.parse(estadosTextoEl.textContent),
    };
    var track2d = card.querySelector(".cuerpo-vista-track");
    var track3d = card.querySelector(".cuerpo3d-track");
    var canvas = track3d.querySelector(".cuerpo3d-lienzo");
    var detalle = card.querySelector(".cuerpo3d-detalle");
    var aviso = card.querySelector(".cuerpo3d-aviso");
    var textoInicial = detalle.textContent;
    var botonesModo = seg.querySelectorAll(".cuerpo3d-btn");
    var botonesVista = card.querySelectorAll(".cuerpo-vista-btn");
    var avatar = null;
    var creando = false;
    var modo = "2d";

    function mostrarDetalle(nombre) {
      if (!nombre) { detalle.textContent = textoInicial; return; }
      var estado = datos.regiones[nombre] || "sin_registro";
      detalle.textContent = (datos.nombres[nombre] || nombre) + " — " + (datos.estados_texto[estado] || "");
    }

    function pulsarModo() {
      botonesModo.forEach(function (b) {
        var on = b.dataset.modo === modo;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
      });
    }

    function aplicar() {
      track2d.hidden = modo !== "2d";
      track3d.hidden = modo !== "3d";
      detalle.hidden = modo !== "3d";
      aviso.hidden = modo !== "3d";
      pulsarModo();
    }

    function activar3d() {
      if (avatar || creando) return;
      creando = true;
      crearVisor(canvas, mostrarDetalle, function (av) {
        creando = false;
        avatar = av;
        if (avatar) avatar.estados(datos.regiones);
      });
    }

    botonesModo.forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.modo === modo) return;
        modo = b.dataset.modo;
        guardarPreferencia(modo);
        aplicar();
        if (modo === "3d") activar3d();
      });
    });

    botonesVista.forEach(function (b) {
      b.addEventListener("click", function () {
        if (modo !== "3d" || !avatar) return;
        var id = b.dataset.vista;
        if (id === "espalda") {
          avatar.vista("musculos"); avatar.lado("espalda"); avatar.elegirRegion(null); mostrarDetalle(null);
        } else if (id === "corazon") {
          avatar.vista("corazon"); avatar.lado("frente"); avatar.elegirRegion("corazon"); mostrarDetalle("corazon");
        } else {
          avatar.vista("musculos"); avatar.lado("frente"); avatar.elegirRegion(null); mostrarDetalle(null);
        }
      });
    });

    seg.hidden = false;
    if (leerPreferencia() === "3d") {
      modo = "3d";
      aplicar();
      activar3d();
    }
  }

  document.querySelectorAll(".cuerpo-card").forEach(wire);
})();
