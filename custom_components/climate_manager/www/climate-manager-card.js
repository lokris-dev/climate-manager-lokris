/*
 * climate-manager-card — fork LOKRIS
 * --------------------------------------------------------------------------
 * Carte Lovelace UNIQUE et multi-zones : pilotage (Marche/Arrêt + Intensité)
 * et configuration (seuils, durées) de toutes les zones du composant.
 *
 * Aucune config requise : la carte découvre les zones via le registre
 * d'entités (platform === "climate_manager"), groupées par appareil (= zone),
 * et résout chaque entité par son translation_key. Robuste aux entity_id
 * traduits (on ne devine aucun nom d'entité).
 *
 *   type: custom:climate-manager-card
 *   title: Climatisation            # optionnel
 *   show_settings: true             # optionnel (défaut true) — section Réglages
 */

const POWER_LEVELS = [
  ["doux", "Doux"],
  ["normal", "Normal"],
  ["agressif", "Frais"],
];

const C = {
  cool: "#2f6fed",
  heat: "#e8743b",
  stab: "#37a169",
  idle: "#8a909a",
  off: "#5a5f6a",
  override: "#9b59b6",
  window: "#d9a441",
};

const STYLES = `
  .cm-root { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 12px; }
  .cm-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .cm-title { font-size: 1.3rem; font-weight: 600; }
  .cm-head-right { display: flex; align-items: center; gap: 8px; }
  .cm-sys { display: inline-flex; align-items: center; gap: 6px; font-size: .82rem; padding: 4px 10px; border-radius: 999px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .cm-sys.on { color: #fff; background: ${C.stab}; }
  .cm-sys .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
  .cm-reset { cursor: pointer; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); border-radius: 8px; padding: 6px 10px; font-size: .82rem; display: inline-flex; gap: 6px; align-items: center; }
  .cm-reset:hover { border-color: var(--primary-color); color: var(--primary-color); }

  .cm-zones { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .cm-zone { border: 1px solid var(--divider-color); border-left-width: 4px; border-radius: 12px; padding: 12px 12px 10px; background: var(--card-background-color); display: flex; flex-direction: column; gap: 9px; }
  .cm-zone.off { opacity: .72; }
  .cm-z-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }
  .cm-z-name { font-weight: 600; font-size: 1.02rem; line-height: 1.15; }
  .cm-z-sub { font-size: .72rem; color: var(--secondary-text-color); margin-top: 2px; }
  .cm-z-temp { font-size: 1.7rem; font-weight: 600; line-height: 1; white-space: nowrap; }
  .cm-z-temp small { font-size: .9rem; font-weight: 500; color: var(--secondary-text-color); }

  .cm-badge { align-self: flex-start; font-size: .74rem; font-weight: 600; color: #fff; padding: 3px 9px; border-radius: 999px; }

  .cm-row { display: flex; align-items: center; gap: 8px; }
  .cm-onoff { flex: 0 0 auto; border: none; cursor: pointer; border-radius: 8px; padding: 7px 12px; font-weight: 600; font-size: .82rem; color: #fff; }
  .cm-onoff.is-on { background: ${C.stab}; }
  .cm-onoff.is-off { background: var(--secondary-background-color); color: var(--secondary-text-color); }

  .cm-seg { display: inline-flex; flex: 1 1 auto; border: 1px solid var(--divider-color); border-radius: 8px; overflow: hidden; }
  .cm-seg button { flex: 1; border: none; cursor: pointer; background: transparent; color: var(--primary-text-color); padding: 7px 4px; font-size: .8rem; }
  .cm-seg button + button { border-left: 1px solid var(--divider-color); }
  .cm-seg button.sel { background: var(--primary-color); color: #fff; font-weight: 600; }
  .cm-seg[disabled] { opacity: .45; pointer-events: none; }

  .cm-override { display: flex; align-items: center; gap: 8px; font-size: .78rem; background: color-mix(in srgb, ${C.override} 14%, transparent); border: 1px solid color-mix(in srgb, ${C.override} 40%, transparent); color: var(--primary-text-color); border-radius: 8px; padding: 6px 8px; }
  .cm-override .lbl { flex: 1; }
  .cm-link { cursor: pointer; color: var(--primary-color); text-decoration: none; border: none; background: none; font-size: .78rem; padding: 0; }

  .cm-foot { font-size: .76rem; color: var(--secondary-text-color); }
  .cm-foot code { background: var(--secondary-background-color); padding: 1px 5px; border-radius: 4px; }

  details.cm-cfg { border-top: 1px dashed var(--divider-color); padding-top: 8px; }
  details.cm-cfg > summary { cursor: pointer; font-size: .8rem; color: var(--secondary-text-color); list-style: none; display: flex; align-items: center; gap: 6px; }
  details.cm-cfg > summary::-webkit-details-marker { display: none; }
  details.cm-cfg[open] > summary { color: var(--primary-text-color); margin-bottom: 8px; }
  .cm-cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; }
  .cm-cfg-grid .full { grid-column: 1 / -1; }
  .cm-fld { display: flex; flex-direction: column; gap: 3px; }
  .cm-fld label { font-size: .68rem; color: var(--secondary-text-color); }
  .cm-fld input { width: 100%; box-sizing: border-box; border: 1px solid var(--divider-color); border-radius: 6px; background: var(--card-background-color); color: var(--primary-text-color); padding: 5px 6px; font-size: .85rem; }
  .cm-cfg-sec { font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: var(--secondary-text-color); margin: 2px 0; }
  .cm-chips { display: flex; flex-wrap: wrap; gap: 4px; }
  .cm-chip { font-size: .72rem; background: var(--secondary-background-color); border-radius: 6px; padding: 2px 7px; }
  .cm-note { font-size: .72rem; color: var(--secondary-text-color); }

  .cm-empty { padding: 18px; text-align: center; color: var(--secondary-text-color); }
`;

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}
function fmtTemp(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n.toFixed(1) : "—";
}

class ClimateManagerCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._title = this._config.title || "Climatisation";
    this._showSettings = this._config.show_settings !== false;
  }
  static getStubConfig() {
    return { type: "custom:climate-manager-card", title: "Climatisation" };
  }
  getCardSize() { return 6; }
  getGridOptions() { return { columns: 12, rows: "auto" }; }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    // Ne pas re-render pendant qu'on tape dans un champ de réglage.
    const ae = document.activeElement;
    if (this._built && ae && this.contains(ae) && ae.tagName === "INPUT") return;
    this._update();
  }

  _build() {
    if (!this._open) this._open = new Set();
    const card = document.createElement("ha-card");
    const style = document.createElement("style");
    style.textContent = STYLES;
    card.appendChild(style);
    const body = document.createElement("div");
    body.className = "cm-root";
    card.appendChild(body);
    this.appendChild(card);
    this._body = body;

    // Délégation : posée une fois sur la ha-card, survit aux re-render du body.
    card.addEventListener("click", (e) => this._onClick(e));
    card.addEventListener("change", (e) => this._onChange(e));
    this._built = true;
  }

  /* ----------------------------------------------------- découverte des zones */

  _zones() {
    const hass = this._hass;
    const ents = hass.entities || {};
    const byDev = {};
    for (const eid in ents) {
      const e = ents[eid];
      if (e.platform !== "climate_manager" || !e.device_id) continue;
      const d = (byDev[e.device_id] ||= { keys: {} });
      if (e.translation_key) d.keys[e.translation_key] = eid;
    }
    const zones = [];
    for (const devId in byDev) {
      const k = byDev[devId].keys;
      const stEid = k.zone_state;
      if (!stEid) continue;
      const st = hass.states[stEid];
      if (!st) continue;
      const a = st.attributes || {};
      const sw = hass.states[k.zone_auto];
      const splits = (a.climate_entities || []).map((cid) => {
        const cs = hass.states[cid];
        return {
          id: cid,
          name: cs?.attributes?.friendly_name || cid,
          temp: cs?.attributes?.current_temperature,
          mode: cs?.state,
        };
      });
      zones.push({
        id: a.zone_id || devId,
        name: a.zone_name || (hass.devices?.[devId]?.name || "Zone").replace(/^Climate Manager\s*[·-]\s*/i, ""),
        state: st.state,
        dir: a.direction,
        inOverride: !!a.in_override,
        overrideUntilReset: !!a.override_until_reset,
        overrideUntil: hass.states[k.zone_override_until]?.state,
        houseAbsent: !!a.house_is_absent,
        on: sw ? sw.state === "on" : true,
        power: hass.states[k.zone_power]?.state,
        roomTemp: hass.states[k.zone_room_temperature]?.state,
        setpointSent: hass.states[k.zone_setpoint_sent]?.state,
        windowsOpen: a.windows_open || 0,
        splits,
        sensors: a.temperature_sensors || [],
        eids: {
          sw: k.zone_auto,
          power: k.zone_power,
          resetOverride: k.zone_reset_override,
          coolStart: k.seuil_debut_refroidissement,
          coolStop: k.seuil_fin_refroidissement,
          heatStart: k.seuil_debut_chauffage,
          heatStop: k.seuil_fin_chauffage,
          durStab: k.duree_stabilisation_min,
          durCooldown: k.duree_cooldown_min,
          durOverride: k.override_duree_min,
        },
      });
    }
    zones.sort((x, y) => x.name.localeCompare(y.name, "fr"));
    return zones;
  }

  /* --------------------------------------------------------------- rendu */

  _update() {
    const zones = this._zones();
    if (!zones.length) {
      this._body.innerHTML =
        `<div class="cm-empty">Aucune zone trouvée.<br>L'intégration « Climate Manager » est-elle configurée ?</div>`;
      return;
    }
    const absent = zones.some((z) => z.houseAbsent);
    const sys = absent
      ? `<span class="cm-sys"><span class="dot"></span>En veille · bâtiment fermé</span>`
      : `<span class="cm-sys on"><span class="dot"></span>Système actif</span>`;

    this._body.innerHTML = `
      <div class="cm-head">
        <div class="cm-title">${esc(this._title)}</div>
        <div class="cm-head-right">
          ${sys}
          <button class="cm-reset" data-act="reset-daily" title="Remet toutes les zones en Marche + Normal">↻ Réinitialiser</button>
        </div>
      </div>
      <div class="cm-zones">${zones.map((z) => this._zoneHtml(z)).join("")}</div>
      <div class="cm-foot">Réglages structurels (ajout de zone, splits, capteurs) : <em>Paramètres → Appareils &amp; services → Climate Manager → Configurer</em>.</div>
    `;
  }

  _zoneHtml(z) {
    const meta = this._stateMeta(z);
    const splitLine = z.splits.length
      ? z.splits
          .map((s) => `${esc(s.name)}${Number.isFinite(parseFloat(s.temp)) ? " " + fmtTemp(s.temp) + "°" : ""}`)
          .join(" · ")
      : "—";
    const seg = POWER_LEVELS.map(
      ([val, lbl]) =>
        `<button data-act="power" data-entity="${esc(z.eids.power)}" data-opt="${val}" class="${z.power === val ? "sel" : ""}">${lbl}</button>`
    ).join("");

    const override = z.inOverride
      ? `<div class="cm-override">
           <span class="lbl">✋ Pris en main ${z.overrideUntilReset ? "jusqu'au prochain reset" : this._untilTxt(z.overrideUntil)}</span>
           ${z.eids.resetOverride ? `<button class="cm-link" data-act="resume" data-entity="${esc(z.eids.resetOverride)}">Reprendre auto</button>` : ""}
         </div>`
      : "";

    const settings = this._showSettings ? this._settingsHtml(z) : "";
    const sentTxt = Number.isFinite(parseFloat(z.setpointSent))
      ? `<span class="cm-z-sub">Consigne clim&nbsp;: ${fmtTemp(z.setpointSent)}°</span>`
      : "";

    return `
      <div class="cm-zone ${z.on ? "" : "off"}" style="border-left-color:${meta.color}">
        <div class="cm-z-top">
          <div>
            <div class="cm-z-name">${esc(z.name)}</div>
            <div class="cm-z-sub">${esc(splitLine)}</div>
          </div>
          <div class="cm-z-temp">${fmtTemp(z.roomTemp)}<small>°C</small></div>
        </div>
        <span class="cm-badge" style="background:${meta.color}">${esc(meta.label)}${z.windowsOpen ? " · fenêtre" : ""}</span>
        <div class="cm-row">
          <button class="cm-onoff ${z.on ? "is-on" : "is-off"}" data-act="toggle" data-entity="${esc(z.eids.sw)}">${z.on ? "Marche" : "Arrêt"}</button>
          <div class="cm-seg" ${z.on ? "" : "disabled"}>${seg}</div>
        </div>
        ${override}
        ${sentTxt}
        ${settings}
      </div>`;
  }

  _settingsHtml(z) {
    const open = this._open.has(z.id) ? "open" : "";
    const num = (label, eid) => {
      const st = this._hass.states[eid];
      if (!st) return "";
      const at = st.attributes || {};
      const v = st.state;
      return `<div class="cm-fld">
          <label>${esc(label)}</label>
          <input type="number" data-act="number" data-entity="${esc(eid)}"
            value="${esc(v)}" step="${esc(at.step ?? 0.5)}"
            min="${esc(at.min ?? "")}" max="${esc(at.max ?? "")}">
        </div>`;
    };
    const chips = (arr) =>
      arr.length ? arr.map((x) => `<span class="cm-chip">${esc(this._fname(x))}</span>`).join("") : `<span class="cm-note">—</span>`;

    return `
      <details class="cm-cfg" ${open} data-zone="${esc(z.id)}">
        <summary data-act="toggle-cfg" data-zone="${esc(z.id)}">⚙ Réglages</summary>
        <div class="cm-cfg-grid">
          <div class="cm-cfg-sec full">Refroidissement (°C)</div>
          ${num("Démarrer au-dessus de", z.eids.coolStart)}
          ${num("S'arrêter à", z.eids.coolStop)}
          <div class="cm-cfg-sec full">Chauffage (°C)</div>
          ${num("Démarrer en-dessous de", z.eids.heatStart)}
          ${num("S'arrêter à", z.eids.heatStop)}
          <div class="cm-cfg-sec full">Durées (min)</div>
          ${num("Stabilisation", z.eids.durStab)}
          ${num("Repos", z.eids.durCooldown)}
          <div class="cm-cfg-sec full">Splits pilotés</div>
          <div class="cm-chips full">${chips(z.splits.map((s) => s.id))}</div>
          <div class="cm-cfg-sec full">Capteurs de température</div>
          <div class="cm-chips full">${chips(z.sensors)}</div>
        </div>
      </details>`;
  }

  _stateMeta(z) {
    if (!z.on) return { label: "Éteint", color: C.off };
    switch (z.state) {
      case "starting":
      case "running":
        if (z.dir === "cool") return { label: z.state === "starting" ? "Démarrage ❄" : "Refroidit ❄", color: C.cool };
        if (z.dir === "heat") return { label: z.state === "starting" ? "Démarrage 🔥" : "Chauffe 🔥", color: C.heat };
        return { label: "Actif", color: C.cool };
      case "stabilizing": return { label: "Stabilisation", color: C.stab };
      case "cooldown": return { label: "Repos", color: C.idle };
      case "idle": return { label: "En attente", color: C.idle };
      case "schedule_off": return { label: "Hors service", color: C.idle };
      case "window_open": return { label: "Fenêtre ouverte", color: C.window };
      case "manual_override_timed":
      case "manual_override_free": return { label: "Pris en main", color: C.override };
      default: return { label: z.state || "—", color: C.idle };
    }
  }

  _untilTxt(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    return "jusqu'à " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  }
  _fname(eid) {
    return this._hass.states[eid]?.attributes?.friendly_name || eid;
  }

  /* --------------------------------------------------------- interactions */

  _onClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "toggle-cfg") return; // <details> natif gère l'ouverture
    e.preventDefault();
    const ent = el.dataset.entity;
    switch (act) {
      case "toggle":
        this._call("switch", "toggle", { entity_id: ent });
        break;
      case "power":
        this._call("select", "select_option", { entity_id: ent, option: el.dataset.opt });
        break;
      case "resume":
        this._call("button", "press", { entity_id: ent });
        break;
      case "reset-daily":
        this._call("climate_manager", "reset_daily", {});
        break;
    }
  }

  _onClickDetailsTrack(zoneId, open) {
    if (open) this._open.add(zoneId);
    else this._open.delete(zoneId);
  }

  _onChange(e) {
    const el = e.target.closest('input[data-act="number"]');
    if (!el) return;
    const v = parseFloat(el.value);
    if (!Number.isFinite(v)) return;
    this._call("number", "set_value", { entity_id: el.dataset.entity, value: v });
  }

  connectedCallback() {
    // Mémorise l'état ouvert/fermé des panneaux Réglages.
    this.addEventListener("toggle", (e) => {
      const d = e.target;
      if (d.classList && d.classList.contains("cm-cfg")) {
        this._onClickDetailsTrack(d.dataset.zone, d.open);
      }
    }, true);
  }

  _call(domain, service, data) {
    this._hass.callService(domain, service, data);
  }
}

customElements.define("climate-manager-card", ClimateManagerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "custom:climate-manager-card",
  name: "Climate Manager — LOKRIS",
  description: "Pilotage et configuration de toutes les zones de climatisation (Marche/Arrêt + Intensité).",
  preview: false,
});
