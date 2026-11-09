/* settings.html's theme and ancho (BODY-ANCHO-COMPLETO-1) button rows (BODY-SEGURIDAD-2, moved
   out of its inline <script>). Loaded with defer so #theme's and #ancho's buttons already exist
   in the DOM when this runs. One small `seg` helper since both rows are the same shape: a
   localStorage key, a `.seg .btn[data-v]` group, and an `apply` that sets or clears one dataset
   attribute on <html>. */
(function(){
function seg(key,selector,fallback,apply){
  var cur;try{cur=localStorage.getItem(key)||fallback}catch(e){cur=fallback}
  var bs=document.querySelectorAll(selector);
  function paint(){bs.forEach(function(b){b.classList.toggle('on',b.dataset.v===cur)})}
  bs.forEach(function(b){b.onclick=function(){cur=b.dataset.v;try{localStorage.setItem(key,cur)}catch(e){}apply(cur);paint()}});
  paint();
}
seg('body.theme','#theme .btn','auto',function(cur){
  var dark=cur==='dark'||(cur==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);
  if(dark)document.documentElement.dataset.theme='dark';else delete document.documentElement.dataset.theme;
});
seg('body.ancho','#ancho .btn','completo',function(cur){
  if(cur==='lectura')document.documentElement.dataset.ancho='lectura';else delete document.documentElement.dataset.ancho;
});
})();
