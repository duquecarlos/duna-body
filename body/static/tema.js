/* theme before paint: no flash (BODY-SEGURIDAD-2, moved out of <head> inline so script-src can drop
   'unsafe-inline' -- loaded as a plain blocking <script src>, not defer/async, so it still runs
   before first paint on every page that includes it: base.html and the four standalone pages. */
try{var th=localStorage.getItem('body.theme');var dark=th==='dark'||(th!=='light'&&matchMedia('(prefers-color-scheme: dark)').matches);if(dark)document.documentElement.dataset.theme='dark'}catch(e){}
try{if(localStorage.getItem('body.ancho')==='lectura')document.documentElement.dataset.ancho='lectura'}catch(e){}
