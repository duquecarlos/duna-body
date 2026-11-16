/* BODY-SYNC-VIVO-2 ruling 3: while Hoy's own "Tu reloj" card shows a pull running right now
   (data-sincronizando="1"), polls /garmin/estado-vivo every 10s, for at most 2 minutes, and
   swaps the card's own markup in place with each response -- stopping as soon as a response no
   longer carries that attribute (the pull finished, whichever way) or the 2-minute ceiling is
   reached, whichever comes first. Loaded only while that attribute is present (today.html's own
   condition); a page with nothing syncing never loads this script at all. */
(function(){
  var SELECTOR = '[data-sincronizando]';
  var POLL_MS = 10000;
  var MAX_MS = 120000;
  var el = document.querySelector(SELECTOR);
  if (!el) return;
  var inicio = Date.now();
  var id = setInterval(function(){
    if (Date.now() - inicio >= MAX_MS) { clearInterval(id); return; }
    fetch('/garmin/estado-vivo', { credentials: 'same-origin' }).then(function(r){
      return r.ok ? r.text() : null;
    }).then(function(html){
      if (html == null) return;
      var actual = document.querySelector(SELECTOR);
      if (!actual || !actual.parentNode) { clearInterval(id); return; }
      var tmp = document.createElement('div');
      tmp.innerHTML = html;
      var nuevo = tmp.firstElementChild;
      if (!nuevo) return;
      actual.parentNode.replaceChild(nuevo, actual);
      if (!nuevo.hasAttribute('data-sincronizando')) clearInterval(id);
    }).catch(function(){});
  }, POLL_MS);
})();
