// Выбор поставщиков для сравнения (до 4) — список живёт в localStorage, переживает пересчёт выдачи.
const KEY = "compare";
const read = () => { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { return []; } };
const write = (ids) => { try { localStorage.setItem(KEY, JSON.stringify(ids)); } catch {} };

function syncCompare() {
  const ids = read();
  document.querySelectorAll("input[data-cmp]").forEach((cb) => { cb.checked = ids.includes(cb.dataset.cmp); });
  const bar = document.getElementById("cmpbar");
  if (!bar) return;
  bar.classList.toggle("on", ids.length > 0);
  bar.querySelector("[data-count]").textContent = ids.length;
  bar.querySelector("[data-go]").href = "/compare?ids=" + ids.join(",") + (bar.dataset.qs ? "&" + bar.dataset.qs : "");
  bar.querySelector("[data-csv]").href = "/export.csv?ids=" + ids.join(",");
}

document.addEventListener("change", (e) => {
  const cb = e.target.closest("input[data-cmp]");
  if (!cb) return;
  let ids = read().filter((x) => x !== cb.dataset.cmp);
  if (cb.checked) ids.push(cb.dataset.cmp);
  write(ids.slice(-4));
  syncCompare();
});
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-clear]")) { write([]); syncCompare(); }
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    const el = document.querySelector(copy.dataset.copy);
    navigator.clipboard.writeText(el.value || el.textContent).then(() => { copy.textContent = "Скопировано ✓"; });
  }
});
document.addEventListener("htmx:afterSwap", syncCompare);
document.addEventListener("DOMContentLoaded", syncCompare);

// Подписи ползунков весов
document.addEventListener("input", (e) => {
  if (e.target.type === "range") {
    const out = document.getElementById("o_" + e.target.name);
    if (out) out.textContent = e.target.value;
  }
});

// Яндекс.Карты JS API 3.0. Координаты в v3 — [долгота, широта].
window.drawMap = async (id, pts) => {
  const el = document.getElementById(id);
  if (!window.ymaps3) { el.textContent = "Карта недоступна: проверьте YANDEX_MAPS_JS_KEY и ограничения по HTTP Referer"; return; }
  await ymaps3.ready;
  const {YMap, YMapDefaultSchemeLayer, YMapDefaultFeaturesLayer, YMapMarker} = ymaps3;
  const lons = pts.map((p) => p.lon), lats = pts.map((p) => p.lat);
  const location = pts.length > 1
    ? {bounds: [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]]}
    : {center: [pts[0].lon, pts[0].lat], zoom: 14};
  const map = new YMap(el, {location, margin: [40, 40, 40, 40]});
  map.addChild(new YMapDefaultSchemeLayer());
  map.addChild(new YMapDefaultFeaturesLayer());
  pts.forEach((p) => {
    const pin = document.createElement("div");
    pin.className = "pin";
    pin.title = p.name + (p.address ? "\n" + p.address : "");
    map.addChild(new YMapMarker({coordinates: [p.lon, p.lat]}, pin));
  });
};
