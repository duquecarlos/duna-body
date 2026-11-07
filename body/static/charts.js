/* Body's chart readout (BODY-PULIDO-2, DESIGN rule 9): enhances every <figure class="chart" data-points="...">
   with a tap/drag-to-the-nearest-point readout and a thin selection marker. Plain hand-written JS, no build
   step, no dependencies, no network call -- every value it shows was already written into data-points by the
   server (html-escaped JSON), and the only hrefs it ever builds are /day/<day> and the existing month link.
   Without this script the figure still works: its svg keeps the per-day/per-month <a><title><rect> hit
   regions that were already there (the no-JS path). */
(function () {
  "use strict";

  var figures = document.querySelectorAll("figure.chart[data-points]");
  if (!figures.length) return;

  Array.prototype.forEach.call(figures, enhance);

  function enhance(fig) {
    var points;
    try {
      points = JSON.parse(fig.getAttribute("data-points") || "[]");
    } catch (e) {
      return;
    }
    if (!points || !points.length) return;
    var svg = fig.querySelector("svg");
    if (!svg) return;

    var linkLabel = fig.getAttribute("data-link-label") || "";
    var key = fig.getAttribute("data-key") || "";

    var marker = document.createElement("div");
    marker.className = "chart-marker";
    marker.hidden = true;
    fig.appendChild(marker);

    var caption = document.createElement("figcaption");
    caption.className = "chart-readout";
    caption.setAttribute("aria-live", "polite");
    fig.appendChild(caption);

    fig.tabIndex = 0;
    var selected = -1;

    function hrefFor(p) {
      if (p.day) return "/day/" + p.day;
      if (p.m) return "/trends?m=" + p.m + "#" + key;
      return null;
    }

    function show(i) {
      if (i < 0 || i >= points.length) return;
      selected = i;
      var p = points[i];
      marker.style.left = (p.x * 100) + "%";
      marker.style.top = svg.offsetTop + "px";
      marker.style.height = svg.offsetHeight + "px";
      marker.hidden = false;
      while (caption.firstChild) caption.removeChild(caption.firstChild);
      caption.appendChild(document.createTextNode(p.label));
      var href = hrefFor(p);
      if (href && linkLabel) {
        caption.appendChild(document.createTextNode(" "));
        var a = document.createElement("a");
        a.className = "taplink";
        a.href = href;
        a.textContent = linkLabel;
        caption.appendChild(a);
      }
    }

    function fracFor(clientX) {
      var rect = svg.getBoundingClientRect();
      if (!rect.width) return 0;
      return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    }

    function nearest(frac) {
      var best = 0, bestDist = Infinity;
      for (var i = 0; i < points.length; i++) {
        var dist = Math.abs(points[i].x - frac);
        if (dist < bestDist) {
          bestDist = dist;
          best = i;
        }
      }
      return best;
    }

    var dragging = false;
    fig.addEventListener("pointerdown", function (e) {
      dragging = true;
      show(nearest(fracFor(e.clientX)));
    });
    fig.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      show(nearest(fracFor(e.clientX)));
    });
    fig.addEventListener("pointerup", function () {
      dragging = false;
    });
    fig.addEventListener("pointercancel", function () {
      dragging = false;
    });
    fig.addEventListener("keydown", function (e) {
      if (e.key === "ArrowRight") {
        e.preventDefault();
        show(selected < 0 ? 0 : Math.min(points.length - 1, selected + 1));
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        show(selected < 0 ? points.length - 1 : Math.max(0, selected - 1));
      }
    });
  }
})();
