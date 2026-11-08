/* scroll restore + service-worker registration (BODY-SEGURIDAD-2, moved out of base.html's
   bottom inline <script>). Loaded with defer: it still runs after the document is parsed, once
   per page, in the same relative order as before -- location.pathname and the pagehide listener
   do not depend on DOMContentLoaded having fired yet. */
(function(){var k='body.scroll:'+location.pathname;try{var y=sessionStorage.getItem(k);if(y)scrollTo(0,+y)}catch(e){}
addEventListener('pagehide',function(){try{sessionStorage.setItem(k,scrollY)}catch(e){}})})();
if('serviceWorker' in navigator&&isSecureContext){navigator.serviceWorker.register('/sw.js').catch(function(){})}
