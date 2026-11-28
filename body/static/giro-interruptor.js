/* Body's own seam for the image spin (BODY-CUERPO-SPIN-HOY-1): wires the 2D/3D switch and
   remembers the person's own choice per device in localStorage (every read/write in try/catch --
   a failure reads as 2D; same key BODY-AVATAR-3D-VISOR-1 used, "body.cuerpo3d", so a device that
   already chose 3D keeps that choice). The heavy engine (giro.js, its data-motor attribute) and
   the active layer's frames load ONLY once 3D is actually chosen -- a 2D visit to Hoy never pays
   for either (ruling 3). No feature-detection: the spin is images and canvas, not WebGL, so it
   works everywhere and the switch is never hidden. */
(function () {
  "use strict";

  function leerPreferencia() {
    try { return localStorage.getItem("body.cuerpo3d"); } catch (e) { return null; }
  }

  function guardarPreferencia(v) {
    try { localStorage.setItem("body.cuerpo3d", v); } catch (e) { /* 2D stays the default */ }
  }

  function cargarMotor(track, listo) {
    if (window.Giro) { listo(); return; }
    var script = document.createElement("script");
    script.src = track.dataset.motor;
    script.onload = listo;
    script.onerror = function () { listo(); };
    document.head.appendChild(script);
  }

  function wire(card) {
    var seg = card.querySelector(".cuerpo3d-seg");
    var track2d = card.querySelector(".cuerpo-vista-track");
    var track3d = card.querySelector(".cuerpo3d-track");
    if (!seg || !track2d || !track3d) return;
    var botones = seg.querySelectorAll(".cuerpo3d-btn");
    var iniciado = false;
    var modo = "2d";

    function pulsar() {
      botones.forEach(function (b) {
        var on = b.dataset.modo === modo;
        b.classList.toggle("on", on);
        b.setAttribute("aria-checked", on ? "true" : "false");
      });
    }

    function aplicar() {
      track2d.hidden = modo !== "2d";
      track3d.hidden = modo !== "3d";
      pulsar();
    }

    function activar() {
      if (iniciado) return;
      iniciado = true;
      cargarMotor(track3d, function () {
        if (window.Giro) window.Giro.iniciar(card);
      });
    }

    botones.forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.modo === modo) return;
        modo = b.dataset.modo;
        guardarPreferencia(modo);
        aplicar();
        if (modo === "3d") activar();
      });
    });

    if (leerPreferencia() === "3d") {
      modo = "3d";
      aplicar();
      activar();
    }
  }

  document.querySelectorAll(".cuerpo-card").forEach(wire);
})();
