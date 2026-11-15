/* Body view component (BODY-CUERPO-VISTA-1): cross-fades between a screen's own views, loads the
   active theme's image for the active view only -- the rest load on first use, never both themes
   -- and turns a horizontal swipe on the image into the next/previous view (the 2D stand-in for
   the mock's "drag to turn"). tema.js already sets `data-theme` on <html> before paint, so this
   deferred script reads it once; theme does not change without a page reload in this app
   (settings.html's own theme row navigates there, it never flips <html> on this page).
   BODY-CUERPO-NITIDEZ-1: a view the template marked "nitida" also carries a per-theme
   `data-claro-srcset`/`data-oscuro-srcset` (the 800w/1440w pair) alongside its plain `src`
   fallback -- set here the same way, right after `src`, for whichever view no such pair exists
   (vias-aereas) that attribute is simply absent and this is a no-op. */
(function(){
function tema(){return document.documentElement.dataset.theme==='dark'?'oscuro':'claro'}
function cargar(capa){
  var img=capa.querySelector('.cuerpo-vista-img')
  if(!img||img.getAttribute('src'))return
  var src=img.dataset[tema()]
  if(src)img.src=src
  var srcset=img.dataset[tema()+'Srcset']
  if(srcset)img.srcset=srcset
}
document.querySelectorAll('.cuerpo-vista').forEach(function(root){
  var capas=root.querySelectorAll('.cuerpo-vista-capa')
  root.querySelectorAll('.cuerpo-vista-glow').forEach(function(g){
    g.style.left=g.dataset.left+'%'
    g.style.top=g.dataset.top+'%'
    g.style.width=g.dataset.w+'%'
    g.style.height=g.dataset.h+'%'
  })
  var btns=root.querySelectorAll('.cuerpo-vista-btn')
  var activo=0
  cargar(capas[0])
  function mostrar(i){
    activo=i
    capas.forEach(function(c,j){
      c.classList.toggle('on',j===i)
      if(j===i)cargar(c)
    })
    btns.forEach(function(b,j){
      var on=j===i
      b.setAttribute('aria-pressed',on?'true':'false')
      b.classList.toggle('on',on)
    })
  }
  btns.forEach(function(b,i){b.addEventListener('click',function(){mostrar(i)})})
  var track=root.querySelector('.cuerpo-vista-track')
  var startX=null
  track.addEventListener('touchstart',function(e){startX=e.touches[0].clientX},{passive:true})
  track.addEventListener('touchend',function(e){
    if(startX===null)return
    var dx=e.changedTouches[0].clientX-startX
    startX=null
    if(Math.abs(dx)<40)return
    var n=capas.length
    mostrar(dx<0?(activo+1)%n:(activo-1+n)%n)
  },{passive:true})
})
})();
