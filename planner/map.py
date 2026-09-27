"""Mapa HTML autocontenido del plan (Leaflet + teselas de OpenStreetMap)."""
import html
import json
from pathlib import Path

TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Plan de cargas</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --ok:#1a7f37; --est:#b35900; --toll:#6e40c9; }
  @media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#f2f2f7; --muted:#a1a1a6; --line:#2c2c2e; } }
  body { margin:0; font:14px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:16px; border-bottom:1px solid var(--line); }
  h1 { font-size:18px; margin:0 0 4px; } .muted { color:var(--muted); }
  #map { height:55vh; }
  .wrap { padding:16px; overflow-x:auto; }
  table { border-collapse:collapse; width:100%; min-width:640px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { font-weight:600; color:var(--muted); } td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .ok { color:var(--ok); } .est { color:var(--est); }
</style></head><body>
<header><h1>__TITLE__</h1><div class="muted">__SUBTITLE__</div></header>
<div id="map"></div>
<div class="wrap"><table><thead><tr><th>Km</th><th>Hora</th><th>Estación</th><th>Localidad</th>
<th class="num">Llega con</th><th class="num">Cargar</th><th class="num">Precio</th></tr></thead><tbody>__ROWS__</tbody></table>
<p class="muted">__FOOTER__</p></div>
<script>
const data = __DATA__;
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 18,
  attribution: '&copy; colaboradores de OpenStreetMap'}).addTo(map);
const line = L.polyline(data.route, {color: '#0a66c2', weight: 4}).addTo(map);
map.fitBounds(line.getBounds(), {padding: [20, 20]});
L.marker(data.origin).addTo(map).bindPopup('Origen');
L.marker(data.dest).addTo(map).bindPopup('Destino');
data.stops.forEach(s => L.circleMarker([s.lat, s.lon], {radius: 9, color: s.ok ? '#1a7f37' : '#b35900', fillOpacity: .85})
  .addTo(map).bindPopup(s.label));
data.tolls.forEach(t => L.circleMarker([t.lat, t.lon], {radius: 5, color: '#6e40c9', fillOpacity: .7})
  .addTo(map).bindPopup(t.label));
</script></body></html>"""


def write_map(path: Path, res: dict, origin: dict, dest: dict, truck: dict):
    r, eb = res["route"], res["eval_best"] or {}
    step = max(len(r.lat) // 1500, 1)
    rows = []
    for s in res["plan_rows"]:
        cls, tag = ("ok", "confirmado") if s["precio_confirmado"] else ("est", "estimado")
        rows.append(
            f"<tr><td class='num'>{s['km']:.0f}</td><td>{s['hora']:%d/%m %H:%M}</td><td>{html.escape(str(s['estacion']))} "
            f"({html.escape(str(s['bandera']))})</td><td>{html.escape(s['localidad'])}</td>"
            f"<td class='num'>{s['llega_con_l']} L</td><td class='num'><b>{s['cargar_l']} L</b></td>"
            f"<td class='num {cls}'>${s['precio']:,} {tag}</td></tr>")
    data = {
        "route": [[round(a, 5), round(b, 5)] for a, b in zip(r.lat[::step], r.lon[::step])],
        "origin": [origin["lat"], origin["lon"]], "dest": [dest["lat"], dest["lon"]],
        "stops": [{"lat": s["lat"], "lon": s["lon"], "ok": s["precio_confirmado"],
                   "label": f"km {s['km']:.0f}: cargar {s['cargar_l']} L en {s['estacion']} a ${s['precio']:,}"}
                  for s in res["plan_rows"]],
        "tolls": [{"lat": t["lat"], "lon": t["lon"],
                   "label": f"{t['nombre'] or 'Peaje'} ({t['operador'] or 'operador desconocido'}): ${t['importe']:,.0f}"}
                  for t in res["tolls"]],
    }
    tolls = sum(t["importe"] for t in res["tolls"])
    ep = res["eval_base"]
    saving = (f" Frente a cargar al bajar del {truck['threshold'] * 100:.0f}% en la primera estación: "
              f"ahorro de ${ep['total'] - eb['total']:,.0f}.") if ep and eb else ""
    page = (TEMPLATE
            .replace("__TITLE__", html.escape(f"{origin['name']} → {dest['name']}"))
            .replace("__SUBTITLE__", html.escape(
                f"{truck['id']} · {r.length_km:.0f} km · combustible ${eb.get('combustible', 0):,.0f} "
                f"en {eb.get('paradas', 0)} paradas · peajes ${tolls:,.0f} ({len(res['tolls'])} cabinas)"))
            .replace("__ROWS__", "".join(rows) or "<tr><td colspan='7'>No hace falta cargar.</td></tr>")
            .replace("__FOOTER__", html.escape(
                "Precios: Secretaría de Energía (Res. 314/2016); los estimados corresponden a estaciones que no "
                "informan hace más de 60 días. Peajes: cabinas de OpenStreetMap con tarifas aproximadas." + saving))
            .replace("__DATA__", json.dumps(data)))
    path.write_text(page, encoding="utf-8")
