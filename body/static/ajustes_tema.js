/* settings.html's theme buttons (BODY-SEGURIDAD-2, moved out of its inline <script>). Loaded with
   defer so #theme's buttons already exist in the DOM when this runs. */
(function(){var cur;try{cur=localStorage.getItem('body.theme')||'auto'}catch(e){cur='auto'}
var bs=document.querySelectorAll('#theme .btn');function paint(){bs.forEach(function(b){b.classList.toggle('on',b.dataset.v===cur)})}
bs.forEach(function(b){b.onclick=function(){cur=b.dataset.v;try{localStorage.setItem('body.theme',cur)}catch(e){}
var dark=cur==='dark'||(cur==='auto'&&matchMedia('(prefers-color-scheme: dark)').matches);
if(dark)document.documentElement.dataset.theme='dark';else delete document.documentElement.dataset.theme;paint()}});paint()})();
