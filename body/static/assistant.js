/* Body's assistant panel (BODY-ASISTENTE-PANEL-1, agent-ui-v1). Plain hand-written JS, no build
   step. Talks to POST /asistente/preguntar and reads its text/event-stream body with a
   ReadableStream reader (not EventSource -- it cannot POST). The model's answer is never written
   as raw markup: every answer is built as DOM nodes via textContent. */
(function () {
  "use strict";

  var btnEl = document.getElementById("asis-btn");
  var panelEl = document.getElementById("asis-panel");
  if (!btnEl || !panelEl) return;

  var dotEl = btnEl.querySelector(".asis-dot");
  var resizeEl = document.getElementById("asis-resize");
  var sizeBtn = document.getElementById("asis-size");
  var nuevaBtn = document.getElementById("asis-nueva");
  var cerrarBtn = document.getElementById("asis-cerrar");
  var mensajesEl = document.getElementById("asis-mensajes");
  var formEl = document.getElementById("asis-form");
  var inputEl = document.getElementById("asis-input");

  var i18n = { herramientas: {}, errores: {} };
  try {
    var raw = document.getElementById("asis-i18n");
    if (raw) i18n = JSON.parse(raw.textContent || "{}");
  } catch (e) {}
  i18n.herramientas = i18n.herramientas || {};
  i18n.errores = i18n.errores || {};
  i18n.pensando = i18n.pensando || "…";
  i18n.como = i18n.como || "{n}|{n}";
  i18n.reintentar = i18n.reintentar || "";

  // ---- small storage helpers: every read/write in try/catch, the panel works without either ----
  function leerLS(clave) {
    try { return localStorage.getItem(clave); } catch (e) { return null; }
  }
  function guardarLS(clave, valor) {
    try { localStorage.setItem(clave, valor); } catch (e) {}
  }
  function leerConversacionId() {
    try {
      var v = sessionStorage.getItem("body.asis.conversacion_id");
      return v ? Number(v) : null;
    } catch (e) { return null; }
  }
  function guardarConversacionId(id) {
    try { sessionStorage.setItem("body.asis.conversacion_id", String(id)); } catch (e) {}
  }
  function limpiarConversacionId() {
    try { sessionStorage.removeItem("body.asis.conversacion_id"); } catch (e) {}
  }

  // ---- state -------------------------------------------------------------------------------
  var panelAbierto = false;
  var trabajando = false;
  var sinVer = false;
  var scrollGuardado = 0;
  var TAMANOS = ["lado", "ancho", "completo"];
  var tamano = leerLS("body.asis.tamano") || "lado";
  if (TAMANOS.indexOf(tamano) === -1) tamano = "lado";
  var anchoPx = Number(leerLS("body.asis.ancho")) || 400;

  function esMovil() {
    return window.matchMedia("(max-width:767.98px)").matches;
  }
  function clampAncho(px) {
    var max = Math.max(window.innerWidth * 0.5, 400);
    return Math.min(Math.max(px, 400), max);
  }
  function anchoCompleto() {
    return window.innerWidth;
  }

  function actualizarDot() {
    if (dotEl) dotEl.hidden = !(trabajando || sinVer);
  }

  function medirBoton() {
    try {
      var h = btnEl.getBoundingClientRect().height || 52;
      document.documentElement.style.setProperty("--asis-pad", (h + 16) + "px");
    } catch (e) {}
  }

  function aplicarTamano() {
    if (esMovil()) {
      if (sizeBtn) sizeBtn.hidden = true;
      if (resizeEl) resizeEl.hidden = true;
      document.documentElement.style.setProperty("--asis-mr", "0px");
      return;
    }
    if (sizeBtn) {
      sizeBtn.hidden = false;
      sizeBtn.textContent = tamano === "lado" ? "◫" : tamano === "ancho" ? "▭" : "⛶";
    }
    var esCompleto = tamano === "completo";
    if (resizeEl) resizeEl.hidden = esCompleto;
    var px = esCompleto ? anchoCompleto() : clampAncho(anchoPx);
    document.documentElement.style.setProperty("--asis-w", px + "px");
    // "completo" overlays the whole window above the rail; the main column keeps its own width
    // behind it, so it never gets the margin-right reflow the side/wide sizes use.
    document.documentElement.style.setProperty("--asis-mr", panelAbierto && !esCompleto ? px + "px" : "0px");
  }

  function abrir() {
    panelAbierto = true;
    panelEl.hidden = false;
    btnEl.setAttribute("aria-expanded", "true");
    sinVer = false;
    actualizarDot();
    if (esMovil()) {
      scrollGuardado = window.scrollY;
      document.body.style.overflow = "hidden";
    }
    aplicarTamano();
    try { inputEl.focus(); } catch (e) {}
  }

  function cerrar() {
    panelAbierto = false;
    panelEl.hidden = true;
    btnEl.setAttribute("aria-expanded", "false");
    document.documentElement.style.setProperty("--asis-mr", "0px");
    if (esMovil()) {
      document.body.style.overflow = "";
      try { window.scrollTo(0, scrollGuardado); } catch (e) {}
    }
  }

  btnEl.addEventListener("click", function () { panelAbierto ? cerrar() : abrir(); });
  if (cerrarBtn) cerrarBtn.addEventListener("click", cerrar);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && panelAbierto) cerrar();
  });
  window.addEventListener("resize", function () {
    medirBoton();
    if (panelAbierto) aplicarTamano();
  });
  medirBoton();

  if (sizeBtn) {
    sizeBtn.addEventListener("click", function () {
      var idx = TAMANOS.indexOf(tamano);
      tamano = TAMANOS[(idx + 1) % TAMANOS.length];
      if (tamano !== "completo") anchoPx = tamano === "ancho" ? clampAncho(window.innerWidth * 0.5) : clampAncho(400);
      guardarLS("body.asis.tamano", tamano);
      guardarLS("body.asis.ancho", String(anchoPx));
      aplicarTamano();
    });
  }

  if (nuevaBtn) {
    nuevaBtn.addEventListener("click", function () {
      limpiarConversacionId();
      mensajesEl.textContent = "";
    });
  }

  // ---- the draggable inner edge: 400 px .. 50% of the viewport ------------------------------
  if (resizeEl) {
    var arrastrando = false, inicioX = 0, inicioAncho = 0;
    resizeEl.addEventListener("pointerdown", function (e) {
      if (tamano === "completo" || esMovil()) return;
      arrastrando = true;
      inicioX = e.clientX;
      inicioAncho = anchoPx;
      try { resizeEl.setPointerCapture(e.pointerId); } catch (err) {}
    });
    resizeEl.addEventListener("pointermove", function (e) {
      if (!arrastrando) return;
      anchoPx = clampAncho(inicioAncho + (inicioX - e.clientX));
      document.documentElement.style.setProperty("--asis-w", anchoPx + "px");
      if (panelAbierto) document.documentElement.style.setProperty("--asis-mr", anchoPx + "px");
    });
    function terminarArrastre() {
      if (!arrastrando) return;
      arrastrando = false;
      guardarLS("body.asis.ancho", String(anchoPx));
      guardarLS("body.asis.tamano", tamano);
    }
    resizeEl.addEventListener("pointerup", terminarArrastre);
    resizeEl.addEventListener("pointercancel", terminarArrastre);
  }

  // ---- rendering an answer safely: DOM nodes built with textContent only, never raw markup ----
  function inlineNodes(texto) {
    var frag = document.createDocumentFragment();
    var partes = texto.split(/(\*\*[^*]+\*\*)/g);
    for (var i = 0; i < partes.length; i++) {
      var parte = partes[i];
      if (!parte) continue;
      if (parte.length > 4 && parte.slice(0, 2) === "**" && parte.slice(-2) === "**") {
        var strong = document.createElement("strong");
        strong.textContent = parte.slice(2, -2);
        frag.appendChild(strong);
      } else {
        frag.appendChild(document.createTextNode(parte));
      }
    }
    return frag;
  }

  function esSeparadorTabla(celdas) {
    for (var i = 0; i < celdas.length; i++) {
      if (!/^:?-{2,}:?$/.test(celdas[i])) return false;
    }
    return true;
  }

  function filaTabla(linea) {
    var t = linea.trim();
    if (t.charAt(0) === "|") t = t.slice(1);
    if (t.charAt(t.length - 1) === "|") t = t.slice(0, -1);
    var celdas = t.split("|");
    for (var i = 0; i < celdas.length; i++) celdas[i] = celdas[i].trim();
    return celdas;
  }

  function renderTabla(lineas) {
    var wrap = document.createElement("div");
    wrap.className = "asis-table-wrap";
    var filas = [];
    for (var i = 0; i < lineas.length; i++) {
      var celdas = filaTabla(lineas[i]);
      if (!esSeparadorTabla(celdas)) filas.push(celdas);
    }
    if (!filas.length) return wrap;
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var trh = document.createElement("tr");
    for (var c = 0; c < filas[0].length; c++) {
      var th = document.createElement("th");
      th.appendChild(inlineNodes(filas[0][c]));
      trh.appendChild(th);
    }
    thead.appendChild(trh);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    for (var r = 1; r < filas.length; r++) {
      var tr = document.createElement("tr");
      for (var c2 = 0; c2 < filas[r].length; c2++) {
        var td = document.createElement("td");
        td.appendChild(inlineNodes(filas[r][c2]));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // A line starting with "- ", "* " or "N. " starts or continues a list even when no blank line
  // sets it off from the text before it (models write "Texto:\n- a\n- b" as one block).
  function tipoLinea(l) {
    if (/^[-*]\s+/.test(l)) return "ul";
    if (/^\d+[.)]\s+/.test(l)) return "ol";
    return "p";
  }

  function renderLineas(lineas, contenedor) {
    var i = 0;
    while (i < lineas.length) {
      var tipo = tipoLinea(lineas[i]);
      var grupo = [lineas[i]];
      i++;
      while (i < lineas.length && tipoLinea(lineas[i]) === tipo) {
        grupo.push(lineas[i]);
        i++;
      }
      if (tipo === "p") {
        var p = document.createElement("p");
        p.appendChild(inlineNodes(grupo.join(" ")));
        contenedor.appendChild(p);
      } else {
        var marcador = tipo === "ul" ? /^[-*]\s+/ : /^\d+[.)]\s+/;
        var listEl = document.createElement(tipo === "ul" ? "ul" : "ol");
        grupo.forEach(function (l) {
          var li = document.createElement("li");
          li.appendChild(inlineNodes(l.replace(marcador, "")));
          listEl.appendChild(li);
        });
        contenedor.appendChild(listEl);
      }
    }
  }

  function renderMD(texto, contenedor) {
    contenedor.textContent = "";
    var bloques = texto.split(/\n\s*\n/);
    for (var b = 0; b < bloques.length; b++) {
      var lineas = bloques[b].split("\n").filter(function (l) { return l.length > 0; });
      if (!lineas.length) continue;
      if (lineas.length === 1 && /^#{1,2}\s+/.test(lineas[0])) {
        var nivel = lineas[0].charAt(1) === "#" ? "h4" : "h3";
        var hEl = document.createElement(nivel);
        hEl.appendChild(inlineNodes(lineas[0].replace(/^#{1,2}\s+/, "")));
        contenedor.appendChild(hEl);
        continue;
      }
      if (lineas.every(function (l) { return l.trim().charAt(0) === "|"; })) {
        contenedor.appendChild(renderTabla(lineas));
        continue;
      }
      renderLineas(lineas, contenedor);
    }
  }

  // i18n.como carries both forms, singular and plural, separated by "|" (base.html forwards it
  // as one opaque string -- no template change needed for the plural to be correct).
  function formatComo(n) {
    var partes = String(i18n.como || "{n}").split("|");
    var tmpl = (n === 1 && partes.length > 1) ? partes[0] : partes[partes.length - 1];
    return tmpl.replace("{n}", String(n));
  }

  // ---- the question/answer turn ---------------------------------------------------------
  function nuevoTurno(pregunta) {
    var turno = document.createElement("div");
    turno.className = "asis-turno";
    var q = document.createElement("p");
    q.className = "asis-msg-q";
    q.textContent = pregunta;
    var status = document.createElement("div");
    status.className = "asis-status";
    var spin = document.createElement("span");
    spin.className = "asis-spin";
    spin.setAttribute("aria-hidden", "true");
    var statusTxt = document.createElement("span");
    statusTxt.textContent = i18n.pensando;
    status.appendChild(spin);
    status.appendChild(statusTxt);
    turno.appendChild(q);
    turno.appendChild(status);
    mensajesEl.appendChild(turno);
    try { turno.scrollIntoView({ block: "start" }); } catch (e) {}
    return { turno: turno, status: status, statusTxt: statusTxt };
  }

  function agregarPasos(turno, pasos, answerEl) {
    if (!pasos.length) return;
    var det = document.createElement("details");
    det.className = "asis-steps";
    var sum = document.createElement("summary");
    sum.textContent = formatComo(pasos.length);
    det.appendChild(sum);
    var ul = document.createElement("ul");
    pasos.forEach(function (nombre) {
      var li = document.createElement("li");
      li.textContent = i18n.herramientas[nombre] || nombre;
      ul.appendChild(li);
    });
    det.appendChild(ul);
    // R5 order: the folded steps sit above the answer, under the question.
    if (answerEl && answerEl.parentNode === turno) {
      turno.insertBefore(det, answerEl);
    } else {
      turno.appendChild(det);
    }
  }

  function mostrarError(turno, codigo, pregunta) {
    var div = document.createElement("div");
    div.className = "asis-error";
    var msg = document.createElement("span");
    msg.textContent = i18n.errores[codigo] || i18n.errores.red || codigo;
    var retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn";
    retry.textContent = i18n.reintentar;
    retry.addEventListener("click", function () {
      div.remove();
      preguntar(pregunta);
    });
    div.appendChild(msg);
    div.appendChild(retry);
    turno.appendChild(div);
  }

  // ---- proposal cards (BODY-ASISTENTE-PROPUESTAS-1): nothing changes until the person taps
  // Aplicar here. Built with textContent only, like every other answer node. ------------------
  function etiquetaEstado(etiquetas, estado) {
    var mapa = {
      aplicada: etiquetas.aplicada, descartada: etiquetas.descartada, deshecha: etiquetas.deshecha,
      expirada: etiquetas.no_vigente, ya_resuelta: etiquetas.no_vigente,
      propuesta_desactualizada: etiquetas.desactualizada, sin_deshacer: etiquetas.sin_deshacer,
    };
    return mapa[estado] || estado;
  }

  function actualizarTarjeta(card, estado, etiquetas, puedeDeshacer) {
    card.acciones.textContent = "";
    card.resultado.textContent = etiquetaEstado(etiquetas, estado);
    card.resultado.hidden = false;
    if (estado === "aplicada" && puedeDeshacer) {
      var btnDeshacer = document.createElement("button");
      btnDeshacer.type = "button";
      btnDeshacer.className = "btn";
      btnDeshacer.textContent = etiquetas.deshacer;
      btnDeshacer.addEventListener("click", function () {
        llamarPropuesta(card, card.id, "deshacer", etiquetas, false);
      });
      card.acciones.appendChild(btnDeshacer);
    }
  }

  function llamarPropuesta(card, id, ruta, etiquetas, puedeDeshacer) {
    fetch("/asistente/propuestas/" + id + "/" + ruta, { method: "POST", credentials: "same-origin" })
      .then(function (resp) { return resp.json(); })
      .then(function (datos) { actualizarTarjeta(card, datos.estado, etiquetas, puedeDeshacer); })
      .catch(function () {});
  }

  function crearTablaSeries(series, etiquetas) {
    // A manual session has no watch set at any order -- the "Reloj"/"Watch" column is dropped
    // entirely rather than shown full of "—" (BODY-ENTRENO-TOOLS-1-M fix 2).
    var tieneReloj = series.some(function (fila) { return fila.reloj != null; });
    var tabla = document.createElement("table");
    tabla.className = "asis-propuesta-series";
    var thead = document.createElement("thead");
    var trh = document.createElement("tr");
    var encabezados = tieneReloj ? ["", etiquetas.entreno_reloj || "", etiquetas.entreno_tu || ""]
                                  : ["", etiquetas.entreno_tu || ""];
    encabezados.forEach(function (texto) {
      var th = document.createElement("th");
      th.textContent = texto;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    tabla.appendChild(thead);
    var tbody = document.createElement("tbody");
    series.forEach(function (fila) {
      var tr = document.createElement("tr");
      var valores = tieneReloj ? [fila.orden, fila.reloj || "—", fila.tu || "—"] : [fila.orden, fila.tu || "—"];
      valores.forEach(function (valor) {
        var td = document.createElement("td");
        td.textContent = valor;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tabla.appendChild(tbody);
    return tabla;
  }

  function crearTarjetaPropuesta(datos) {
    var etiquetas = datos.etiquetas || {};
    var wrap = document.createElement("div");
    wrap.className = "asis-propuesta";
    var titulo = document.createElement("strong");
    titulo.textContent = datos.titulo;
    wrap.appendChild(titulo);
    if (datos.origen_etiqueta) {
      var origen = document.createElement("p");
      origen.className = "asis-propuesta-resultado";
      origen.textContent = datos.origen_etiqueta;
      wrap.appendChild(origen);
    }
    if (datos.antes) {
      var antes = document.createElement("p");
      antes.className = "asis-propuesta-antes";
      antes.textContent = datos.antes;
      wrap.appendChild(antes);
    }
    var despues = document.createElement("p");
    despues.className = "asis-propuesta-despues";
    despues.textContent = datos.despues;
    wrap.appendChild(despues);
    if (datos.series && datos.series.length) {
      wrap.appendChild(crearTablaSeries(datos.series, etiquetas));
    }
    var acciones = document.createElement("div");
    acciones.className = "asis-propuesta-acciones";
    var resultado = document.createElement("p");
    resultado.className = "asis-propuesta-resultado";
    resultado.hidden = true;
    var card = { id: datos.id, acciones: acciones, resultado: resultado };
    if (datos.estado === "propuesta") {
      var btnAplicar = document.createElement("button");
      btnAplicar.type = "button";
      btnAplicar.className = "btn";
      btnAplicar.textContent = etiquetas.aplicar;
      btnAplicar.addEventListener("click", function () {
        llamarPropuesta(card, datos.id, "aplicar", etiquetas, datos.deshacer);
      });
      var btnDescartar = document.createElement("button");
      btnDescartar.type = "button";
      btnDescartar.className = "btn";
      btnDescartar.textContent = etiquetas.descartar;
      btnDescartar.addEventListener("click", function () {
        llamarPropuesta(card, datos.id, "descartar", etiquetas, datos.deshacer);
      });
      acciones.appendChild(btnAplicar);
      acciones.appendChild(btnDescartar);
    } else {
      resultado.hidden = false;
      resultado.textContent = etiquetaEstado(etiquetas, datos.estado);
      if (datos.estado === "aplicada" && datos.deshacer) {
        var btnDeshacer2 = document.createElement("button");
        btnDeshacer2.type = "button";
        btnDeshacer2.className = "btn";
        btnDeshacer2.textContent = etiquetas.deshacer;
        btnDeshacer2.addEventListener("click", function () {
          llamarPropuesta(card, datos.id, "deshacer", etiquetas, false);
        });
        acciones.appendChild(btnDeshacer2);
      }
    }
    wrap.appendChild(acciones);
    wrap.appendChild(resultado);
    return wrap;
  }

  // ---- reading the SSE body: a POST response, so EventSource cannot be used -----------------
  function leerSSE(response, onEvento) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function paso() {
      return reader.read().then(function (r) {
        if (r.done) return;
        buffer += decoder.decode(r.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          var crudo = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          var evento = "message", datos = "";
          crudo.split("\n").forEach(function (linea) {
            if (linea.indexOf("event:") === 0) evento = linea.slice(6).trim();
            else if (linea.indexOf("data:") === 0) datos += linea.slice(5).trim();
          });
          if (datos) {
            try { onEvento(evento, JSON.parse(datos)); } catch (e) {}
          }
        }
        return paso();
      });
    }
    return paso();
  }

  function preguntar(pregunta) {
    trabajando = true;
    actualizarDot();
    var turno = nuevoTurno(pregunta);
    var pasos = [];
    var textoAcum = "";
    var answerEl = null;
    var huboError = false;
    var propuestasPendientes = [];

    function onEvento(nombre, datos) {
      if (nombre === "herramienta") {
        pasos.push(datos.nombre);
        turno.statusTxt.textContent = i18n.herramientas[datos.nombre] || i18n.pensando;
      } else if (nombre === "texto") {
        textoAcum += datos.delta || "";
        turno.status.hidden = true;
        if (!answerEl) {
          answerEl = document.createElement("div");
          answerEl.className = "asis-answer";
          turno.turno.appendChild(answerEl);
        }
        renderMD(textoAcum, answerEl);
        try { answerEl.scrollIntoView({ block: "start" }); } catch (e) {}
      } else if (nombre === "propuesta") {
        // Queued, not appended here: this event can arrive before the turn's answer text does
        // (a proposal tool call's round precedes the model's final round), and the card must sit
        // under the answer, not between the question and the folded steps.
        propuestasPendientes.push(datos);
      } else if (nombre === "fin") {
        turno.status.hidden = true;
        guardarConversacionId(datos.conversacion_id);
        agregarPasos(turno.turno, pasos, answerEl);
        propuestasPendientes.forEach(function (p) {
          turno.turno.appendChild(crearTarjetaPropuesta(p));
        });
      } else if (nombre === "error") {
        huboError = true;
        turno.status.hidden = true;
        mostrarError(turno.turno, datos.codigo, pregunta);
      }
    }

    return fetch("/asistente/preguntar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        pregunta: pregunta,
        conversacion_id: leerConversacionId(),
        lugar: location.pathname,
      }),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) throw new Error("http_" + resp.status);
      return leerSSE(resp, onEvento);
    }).catch(function () {
      if (!huboError) {
        turno.status.hidden = true;
        mostrarError(turno.turno, "red", pregunta);
      }
    }).then(function () {
      trabajando = false;
      if (!panelAbierto) sinVer = true;
      actualizarDot();
    });
  }

  if (formEl) {
    formEl.addEventListener("submit", function (e) {
      e.preventDefault();
      var texto = (inputEl.value || "").trim();
      if (!texto) return;
      inputEl.value = "";
      preguntar(texto);
    });
  }

  // On a computer (>=768px) Enter sends and Shift+Enter inserts a line; on a phone Enter inserts
  // a line (the on-screen keyboard has no Shift) and the send button submits.
  if (inputEl && formEl) {
    inputEl.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" || e.shiftKey || esMovil()) return;
      e.preventDefault();
      if (formEl.requestSubmit) formEl.requestSubmit();
      else formEl.dispatchEvent(new Event("submit", { cancelable: true }));
    });
  }

  // ---- restoring a conversation after a page change: the id lives in sessionStorage, the turns
  // themselves do not (so a GET of another person's id, or a 404, cannot leak anything) ---------
  function renderTurnoCargado(contenedor, t) {
    if (t.rol === "persona") {
      var turno = document.createElement("div");
      turno.className = "asis-turno";
      var q = document.createElement("p");
      q.className = "asis-msg-q";
      q.textContent = t.texto;
      turno.appendChild(q);
      contenedor.appendChild(turno);
      return turno;
    }
    return null;
  }

  function cargarConversacion(id) {
    fetch("/asistente/conversacion/" + id, { credentials: "same-origin" }).then(function (resp) {
      if (resp.status === 404) {
        limpiarConversacionId();
        return null;
      }
      return resp.ok ? resp.json() : null;
    }).then(function (datos) {
      if (!datos) return;
      mensajesEl.textContent = "";
      var turnoActual = null;
      (datos.turnos || []).forEach(function (t) {
        if (t.rol === "persona") {
          turnoActual = renderTurnoCargado(mensajesEl, t);
          return;
        }
        if (!turnoActual) return;
        if (t.pasos) {
          var det = document.createElement("details");
          det.className = "asis-steps";
          var sum = document.createElement("summary");
          sum.textContent = formatComo(t.pasos);
          det.appendChild(sum);
          turnoActual.appendChild(det);
        }
        var answerEl = document.createElement("div");
        answerEl.className = "asis-answer";
        renderMD(t.texto || "", answerEl);
        turnoActual.appendChild(answerEl);
      });
      (datos.propuestas || []).forEach(function (p) {
        mensajesEl.appendChild(crearTarjetaPropuesta(p));
      });
    }).catch(function () {});
  }

  var idAlCargar = leerConversacionId();
  if (idAlCargar) cargarConversacion(idAlCargar);
})();
