/* comida_productos.html's "Leer etiqueta" button (BODY-ETIQUETA-FOTO-1, ruling 2): re-encodes the
   picked photo on the device before it ever leaves it -- draws it to a canvas and exports JPEG,
   which drops EXIF/GPS by construction, with the long edge capped at MAX_LADO_PX and refused
   outright over MAX_BYTES. The visible preview uses a data: URL (CSP already allows it); the
   source image is decoded through FileReader too, so blob: never appears anywhere on this page.
   Posts the re-encoded bytes to the upload route, then navigates to the SAME picker page with the
   returned propuesta_id, which server-renders the editable confirm card (ruling 6) -- this script
   never builds that card itself. */
(function(){
  var MAX_LADO_PX = 1568;
  var MAX_BYTES = 1000000;
  var CALIDAD = 0.85;

  var input = document.getElementById('etiqueta-input');
  var boton = document.getElementById('etiqueta-boton');
  if (!input || !boton) return;
  var estado = document.getElementById('etiqueta-estado');
  var preview = document.getElementById('etiqueta-preview');

  function bodyEtiquetaEstado(mensaje){
    if (!estado) return;
    if (mensaje) { estado.textContent = mensaje; estado.classList.remove('hidden'); }
    else { estado.textContent = ''; estado.classList.add('hidden'); }
  }

  function bodyEtiquetaReencode(file, listo, fallo){
    var lector = new FileReader();
    lector.onerror = fallo;
    lector.onload = function(){
      var img = new Image();
      img.onerror = fallo;
      img.onload = function(){
        var escala = Math.min(1, MAX_LADO_PX / Math.max(img.width, img.height));
        var w = Math.max(1, Math.round(img.width * escala));
        var h = Math.max(1, Math.round(img.height * escala));
        var canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        var dataUrl = canvas.toDataURL('image/jpeg', CALIDAD);
        canvas.toBlob(function(blob){
          if (!blob) { fallo(); return; }
          listo(blob, dataUrl);
        }, 'image/jpeg', CALIDAD);
      };
      img.src = lector.result;
    };
    lector.readAsDataURL(file);
  }

  function bodyEtiquetaRedirigir(propuestaId){
    var params = new URLSearchParams(window.location.search);
    params.set('propuesta_id', propuestaId);
    window.location.href = '/comida/productos?' + params.toString();
  }

  function bodyEtiquetaSubir(blob, dataUrl){
    if (preview) { preview.src = dataUrl; preview.classList.remove('hidden'); }
    bodyEtiquetaEstado(input.dataset.msgLeyendo || '');
    fetch(input.dataset.url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'image/jpeg' }, body: blob,
    }).then(function(r){ return r.json().then(function(data){ return { ok: r.ok, data: data }; }); })
      .then(function(res){
        if (res.data && res.data.ok) { bodyEtiquetaRedirigir(res.data.propuesta_id); return; }
        var clave = res.data && res.data.error;
        var mensaje = (clave === 'muy_grande' && input.dataset.msgGrande)
          || (clave === 'no_imagen' && input.dataset.msgNoImagen)
          || input.dataset.msgNoLegible;
        bodyEtiquetaEstado(mensaje);
      })
      .catch(function(){ bodyEtiquetaEstado(input.dataset.msgNoLegible); });
  }

  boton.addEventListener('click', function(){ input.click(); });
  input.addEventListener('change', function(){
    var file = input.files && input.files[0];
    input.value = '';
    if (!file) return;
    bodyEtiquetaEstado('');
    bodyEtiquetaReencode(file, function(blob, dataUrl){
      if (blob.size > MAX_BYTES) { bodyEtiquetaEstado(input.dataset.msgGrande); return; }
      bodyEtiquetaSubir(blob, dataUrl);
    }, function(){ bodyEtiquetaEstado(input.dataset.msgNoImagen); });
  });
})();
