/* goal_new.html's metric-select change (BODY-SEGURIDAD-2, moved out of its inline <script> and
   inline onchange= attribute). Loaded with defer so #metric-select already exists; still runs
   once on load, as before. */
function bodyMetricChange(){
  var sel = document.getElementById('metric-select');
  var opt = sel && sel.selectedOptions[0];
  if(!opt) return;
  document.getElementById('direction-select').value = opt.dataset.direction;
  if(opt.dataset.baseline){document.getElementById('baseline-input').value = opt.dataset.baseline}
}
document.getElementById('metric-select') && document.getElementById('metric-select').addEventListener('change', bodyMetricChange);
bodyMetricChange();
