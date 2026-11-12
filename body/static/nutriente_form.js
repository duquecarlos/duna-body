/* settings.html's nutrient-target form (BODY-NUTRIENTE-OBJETIVO-1-M fix 5): switches `sentido`'s
   default when `metric_key` changes, read from each option's own data-sentido-defecto attribute
   (body/nutriente_objetivo.py's SENTIDO_DEFAULT, never duplicated here). Without this script the
   form still works with the server-rendered default. */
function bodyNutrienteMetricChange(){
  var sel = document.getElementById('nutriente-metric-select');
  var opt = sel && sel.selectedOptions[0];
  if(!opt) return;
  var sentido = document.getElementById('nutriente-sentido-select');
  if(sentido && opt.dataset.sentidoDefecto){sentido.value = opt.dataset.sentidoDefecto}
}
document.getElementById('nutriente-metric-select') && document.getElementById('nutriente-metric-select').addEventListener('change', bodyNutrienteMetricChange);
