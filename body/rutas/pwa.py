"""The PWA service worker (BODY-WEB-ESTRUCTURA-8), moved out of body/rutas/comun.py: comun is
shared plumbing and should not itself hold a route (the review item that opened this move).
Builds its cache name from comun.STATIC_HASH, the same value every static asset's own URL
already carries (comun.static_url()), so the cache-busting value and the route that publishes it
to the service worker still agree even though comun owns neither the route nor a router of its
own any more.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from . import comun

router = APIRouter()

SW_TEMPLATE = """// Body service worker: caches only static files, never health pages.
const C='body-static-HASHPLACEHOLDER';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==C).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);
 if(u.origin===location.origin&&u.pathname.startsWith('/static/')){
  e.respondWith(caches.open(C).then(c=>c.match(e.request).then(r=>r||fetch(e.request).then(n=>{c.put(e.request,n.clone());return n}))));}});
"""

SW = SW_TEMPLATE.replace("HASHPLACEHOLDER", comun.STATIC_HASH)


@router.get("/sw.js")
def sw():
    return Response(SW, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
