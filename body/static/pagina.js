/* scroll restore + service-worker registration (BODY-SEGURIDAD-2, moved out of base.html's
   bottom inline <script>). Loaded with defer: it still runs after the document is parsed, once
   per page, in the same relative order as before -- location.pathname and the pagehide listener
   do not depend on DOMContentLoaded having fired yet. */
(function(){var k='body.scroll:'+location.pathname;try{var y=sessionStorage.getItem(k);if(y)scrollTo(0,+y)}catch(e){}
addEventListener('pagehide',function(){try{sessionStorage.setItem(k,scrollY)}catch(e){}})})();
if('serviceWorker' in navigator&&isSecureContext){navigator.serviceWorker.register('/sw.js').catch(function(){})}

/* BODY-GARMIN-SYNC-CONSISTENTE-1 ruling 4: /garmin's connect and MFA-code forms carry no JS of
   their own, so this lives in the one script every page already loads -- guarded by the
   data-cargando attribute, present only on those two buttons, so it is a no-op everywhere else.
   Progressive enhancement: with JS off the forms still submit, with no in-progress state. A
   response that re-renders the page (an error) resets the button to normal through the plain
   navigation itself, with no extra code here. */
(function(){
  var forms = document.querySelectorAll('form[action="/garmin/conectar"],form[action="/garmin/codigo"]');
  for(var i=0;i<forms.length;i++){forms[i].addEventListener('submit',function(){
    var btn=this.querySelector('button[type="submit"]');
    var cargando=btn&&btn.getAttribute('data-cargando');
    if(!btn||btn.disabled||!cargando)return;
    btn.disabled=true;btn.setAttribute('aria-busy','true');btn.textContent='';
    var spin=document.createElement('span');spin.className='asis-spin';spin.setAttribute('aria-hidden','true');
    btn.appendChild(spin);btn.appendChild(document.createTextNode(cargando));
  })}
})();
