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
 *   zone: openspace                 # optionnel — n'affiche QUE cette zone (mode
 *   zones: [cuisine, openspace]     #   intégré : pas d'en-tête ni de pied, idéal
 *                                   #   pour poser le widget d'une zone dans une pièce)
 */

// Intensité = puissance de régulation (offset), indépendante du sens : en froid
// « Fort » pousse plus froid, en chaud plus chaud. D'où des libellés neutres
// (Doux ↔ Fort), pas « Frais » qui ne vaudrait que pour la clim.
const POWER_LEVELS = [
  ["doux", "Doux"],
  ["normal", "Normal"],
  ["agressif", "Fort"],
];

// Ventilation (select zone_fan_intensity) → libellés parlants.
const VENT_LEVELS = [
  ["doux", "Basse"],
  ["normal", "Auto"],
  ["fort", "Haute"],
];

// Balayage (swing_mode de la clim) → libellés parlants. "" = ne pas piloter.
const SWING_LABELS = { off: "Fixe", on: "Balayage", both: "Croisé", vertical: "Vertical", horizontal: "Horizontal" };

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
  /* Mode intégré (zone:) — le ha-card EST la carte ; le .cm-zone ne doit pas
     ajouter sa propre bordure/fond (sinon double cadre). */
  .cm-root:has(.cm-embedded) { padding: 0; }
  .cm-embedded { display: flex; flex-direction: column; gap: 0; }
  .cm-embedded .cm-zone { margin: 0; border: none; border-radius: 0; background: transparent; }
  .cm-embedded .cm-zone + .cm-zone { border-top: 1px solid var(--divider-color); }
  .cm-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .cm-title { font-size: 1.3rem; font-weight: 600; }
  .cm-head-right { display: flex; align-items: center; gap: 8px; }
  .cm-sys { display: inline-flex; align-items: center; gap: 6px; font-size: .82rem; padding: 4px 10px; border-radius: 999px; background: var(--secondary-background-color); color: var(--secondary-text-color); }
  .cm-sys.on { color: #fff; background: ${C.stab}; }
  .cm-sys .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
  .cm-reset { cursor: pointer; border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); border-radius: 8px; padding: 6px 10px; font-size: .82rem; display: inline-flex; gap: 6px; align-items: center; }
  .cm-reset:hover { border-color: var(--primary-color); color: var(--primary-color); }

  .cm-paused { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; background: ${C.window}1f; border: 1px solid ${C.window}66; border-radius: 10px; padding: 10px 12px; font-size: .86rem; }
  .cm-paused span { flex: 1; min-width: 220px; }
  .cm-go { cursor: pointer; border: none; background: ${C.stab}; color: #fff; font-weight: 600; border-radius: 8px; padding: 8px 14px; font-size: .86rem; white-space: nowrap; }
  .cm-go:hover { filter: brightness(1.08); }

  .cm-zones { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .cm-zone { border: 1px solid var(--divider-color); border-radius: 14px; padding: 14px 15px; background: var(--card-background-color); display: flex; flex-direction: column; gap: 12px; }
  .cm-zone.off { opacity: .6; }

  .cm-z-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .cm-z-name { font-weight: 700; font-size: 1.05rem; line-height: 1.2; }
  .cm-z-state { font-size: .78rem; color: var(--secondary-text-color); margin-top: 3px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .cm-state-main { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
  .cm-dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
  .cm-dot.pulse { animation: cm-pulse 1.5s ease-in-out infinite; }
  @keyframes cm-pulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.4); opacity: .45; } }
  .cm-delta { color: var(--secondary-text-color); }
  .cm-delta b { color: var(--primary-text-color); font-weight: 700; }
  .cm-z-temp { font-size: 1.85rem; font-weight: 700; line-height: .95; white-space: nowrap; letter-spacing: -.02em; }
  .cm-z-temp small { font-size: .78rem; font-weight: 600; color: var(--secondary-text-color); margin-left: 1px; }
  .cm-z-temp.cm-clickable { cursor: pointer; border-radius: 8px; padding: 2px 4px; margin: -2px -4px; transition: background .15s; }
  .cm-z-temp.cm-clickable:hover { background: var(--secondary-background-color); }

  /* Bloc de réglages symétrique : chaque ligne = libellé + contrôle aligné. */
  .cm-ctrls { display: flex; flex-direction: column; gap: 9px; }
  .cm-ctl { display: grid; grid-template-columns: 86px 1fr; align-items: center; gap: 10px; }
  .cm-ctl-lbl { font-size: .8rem; font-weight: 600; color: var(--secondary-text-color); }
  .cm-ctl[disabled] { opacity: .45; pointer-events: none; }

  /* Interrupteur ON/OFF coulissant (le fond glisse à droite = Marche). */
  .cm-switch { position: relative; display: grid; grid-template-columns: 1fr 1fr; background: var(--secondary-background-color); border-radius: 999px; padding: 4px; overflow: hidden; }
  .cm-switch::before { content: ""; position: absolute; top: 4px; bottom: 4px; left: 4px; width: calc(50% - 4px); border-radius: 999px; transition: transform .2s ease, background .2s ease; }
  .cm-switch.off::before { transform: translateX(0); background: ${C.off}; }
  .cm-switch.on::before { transform: translateX(100%); background: ${C.stab}; }
  .cm-switch button { position: relative; z-index: 1; border: none; background: transparent; cursor: pointer; padding: 9px 0; font-weight: 700; font-size: .85rem; color: var(--secondary-text-color); border-radius: 999px; transition: color .18s; }
  .cm-switch button.sel { color: #fff; }
  .cm-switch[disabled] { opacity: .5; pointer-events: none; }

  /* Cible (stepper) */
  .cm-target-right { display: flex; align-items: center; gap: 6px; }
  .cm-stepper { display: inline-flex; align-items: stretch; border: 1px solid var(--divider-color); border-radius: 9px; overflow: hidden; }
  .cm-stepper button { border: none; background: var(--card-background-color); cursor: pointer; width: 38px; font-size: 1.2rem; line-height: 1; color: var(--primary-text-color); }
  .cm-stepper button:hover { background: var(--secondary-background-color); }
  .cm-stepper .val { min-width: 54px; text-align: center; font-weight: 700; font-size: .98rem; padding: 7px 0; border-left: 1px solid var(--divider-color); border-right: 1px solid var(--divider-color); }
  .cm-reset-auto { border: none; background: none; cursor: pointer; color: var(--secondary-text-color); font-size: 1rem; padding: 2px 4px; line-height: 1; }
  .cm-reset-auto:hover { color: var(--primary-color); }

  .cm-seg { display: inline-flex; width: 100%; background: var(--secondary-background-color); border-radius: 9px; padding: 3px; gap: 2px; }
  .cm-seg button { flex: 1; border: none; cursor: pointer; background: transparent; color: var(--secondary-text-color); padding: 6px 4px; font-size: .8rem; font-weight: 600; border-radius: 7px; }
  .cm-seg button.sel { background: var(--card-background-color); color: var(--primary-text-color); box-shadow: 0 1px 2px rgba(0,0,0,.12); }
  .cm-seg[disabled] { opacity: .45; pointer-events: none; }

  .cm-link { cursor: pointer; color: var(--primary-color); border: none; background: none; font-size: .78rem; padding: 0; }

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

  .cm-frost { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; border-radius: 10px; padding: 9px 12px; font-size: .85rem; }
  .cm-frost.heat { background: ${C.heat}1f; border: 1px solid ${C.heat}66; }
  .cm-frost.cool { background: ${C.cool}1f; border: 1px solid ${C.cool}66; }
  .cm-frost b { font-weight: 700; }
  .cm-frost .cm-frost-when { color: var(--secondary-text-color); margin-left: auto; }

  /* Sens global du groupe extérieur (mono-mode) — auto / été / hiver */
  .cm-season { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; border-radius: 10px; padding: 8px 12px; font-size: .82rem; background: var(--secondary-background-color); }
  .cm-season.heat { background: ${C.heat}14; border: 1px solid ${C.heat}44; }
  .cm-season.cool { background: ${C.cool}14; border: 1px solid ${C.cool}44; }
  .cm-season-lbl { font-weight: 600; }
  .cm-season-lbl b { font-weight: 700; }
  .cm-season-auto { color: var(--secondary-text-color); font-weight: 600; }
  .cm-season-seg { width: auto; margin-left: auto; }
  .cm-season-seg button { padding: 6px 12px; flex: 0 0 auto; }
  .cm-season[disabled] { opacity: .5; pointer-events: none; }

  details.cm-opt { border-top: 1px solid var(--divider-color); padding-top: 10px; }
  details.cm-opt > summary { cursor: pointer; font-size: .78rem; color: var(--secondary-text-color); list-style: none; display: flex; align-items: center; gap: 6px; }
  details.cm-opt > summary::-webkit-details-marker { display: none; }
  details.cm-opt > summary::before { content: "›"; display: inline-block; transition: transform .15s; font-size: 1rem; }
  details.cm-opt[open] > summary::before { transform: rotate(90deg); }
  details.cm-opt[open] > summary { color: var(--primary-text-color); margin-bottom: 10px; }
  .cm-split { display: grid; grid-template-columns: 76px 1fr; gap: 7px 10px; align-items: center; }
  .cm-split + .cm-split { margin-top: 11px; padding-top: 11px; border-top: 1px dashed var(--divider-color); }
  .cm-split .nm { grid-column: 1 / -1; display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .cm-split .nm b { font-weight: 600; font-size: .86rem; }
  .cm-split .nm .meta { font-size: .73rem; color: var(--secondary-text-color); white-space: nowrap; }
  .cm-split > label { font-size: .76rem; color: var(--secondary-text-color); }
  .cm-split input[type="number"], .cm-split select { width: 100%; box-sizing: border-box; border: 1px solid var(--divider-color); border-radius: 7px; background: var(--card-background-color); color: var(--primary-text-color); padding: 6px 7px; font-size: .83rem; }
  .cm-seg-sm button { padding: 5px 3px; font-size: .74rem; }
  .cm-split[data-disabled] { opacity: .5; pointer-events: none; }
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
    // Mode intégré : `zone: <id>` (ou `zones: [...]`) -> n'affiche que ces zones,
    // sans l'en-tête global ni le pied de page. Idéal pour poser le widget d'une
    // zone dans une pièce du dashboard.
    const zc = this._config.zone ?? this._config.zones;
    this._zoneFilter = zc == null ? null : (Array.isArray(zc) ? zc.map(String) : [String(zc)]);
    // Mode `system: true` -> carte d'en-tête seule (statut système + actions +
    // bannière hors-gel), sans aucune zone. Pour coiffer une colonne de widgets
    // par zone sur le dashboard.
    this._systemOnly = this._config.system === true;
    this._embedded = !!this._zoneFilter && !this._systemOnly;
    // Réglages admin masqués par défaut en mode intégré (sauf show_settings: true).
    this._showSettings = this._embedded
      ? this._config.show_settings === true
      : this._config.show_settings !== false;
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
    if (!this._openSplits) this._openSplits = new Set();
    // Le custom element est inline par défaut -> il rétrécit au contenu dans une
    // cellule de grille (vue sections). On force le remplissage de la largeur.
    this.style.display = "block";
    this.style.width = "100%";
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
      // Splits enrichis (§3) : la liste vient du coordinator (a.splits) avec
      // cible/puissance/swing configurés + état réel. Repli sur climate_entities.
      const cfgSplits = Array.isArray(a.splits) ? a.splits : [];
      const rawSplits = cfgSplits.length
        ? cfgSplits
        : (a.climate_entities || []).map((id) => ({ entity_id: id }));
      const splits = rawSplits.map((s) => {
        const cid = s.entity_id;
        const cs = hass.states[cid];
        return {
          id: cid,
          name: cs?.attributes?.friendly_name || s.name || cid,
          temp: s.internal_temp ?? cs?.attributes?.current_temperature,
          mode: s.hvac_mode ?? cs?.state,
          setpoint: s.current_setpoint ?? cs?.attributes?.temperature,
          target: s.target ?? null,                       // null = hérité
          effectiveTarget: s.effective_target ?? null,
          power: s.power ?? null,                          // null = hérité
          swing: s.swing ?? null,                          // null = ne pas toucher
          currentSwing: s.current_swing ?? cs?.attributes?.swing_mode ?? null,
          swingModes: cs?.attributes?.swing_modes || [],
        };
      });
      zones.push({
        id: a.zone_id || devId,
        name: a.zone_name || (hass.devices?.[devId]?.name || "Zone").replace(/^Climate Manager\s*[·-]\s*/i, ""),
        state: st.state,
        dir: a.direction || a.active_direction,
        regime: a.regime,
        activeDirection: a.active_direction,
        targetTemp: a.target_temp ?? null,           // cible explicite (null = auto)
        targetDisplay: a.target_temperature ?? null, // cible effective affichée
        frost: a.frost || null,
        season: a.season || null,
        inOverride: !!a.in_override,
        overrideUntilReset: !!a.override_until_reset,
        overrideUntil: hass.states[k.zone_override_until]?.state,
        houseAbsent: !!a.house_is_absent,
        on: sw ? sw.state === "on" : true,
        power: hass.states[k.zone_power]?.state,
        fan: hass.states[k.zone_fan_intensity]?.state,
        swing: splits[0]?.swing ?? "",             // "" = Auto (ne pas piloter)
        swingModes: splits[0]?.swingModes || [],
        roomTemp: hass.states[k.zone_room_temperature]?.state,
        setpointSent: hass.states[k.zone_setpoint_sent]?.state,
        offset: a.offset,                         // offset pendule appliqué (°, signé)
        windowsOpen: a.windows_open || 0,
        splits,
        sensors: a.temperature_sensors || [],
        eids: {
          sw: k.zone_auto,
          power: k.zone_power,
          fan: k.zone_fan_intensity,
          roomTemp: k.zone_room_temperature,   // capteur T° zone (pour le more-info)
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
    let zones = this._zones();
    // En mode system, on garde toutes les zones (statut/frost global).
    if (this._zoneFilter && !this._systemOnly) {
      zones = zones.filter((z) => this._zoneFilter.includes(String(z.id)));
    }
    if (!zones.length) {
      this._body.innerHTML = this._embedded
        ? `<div class="cm-empty">Zone introuvable.</div>`
        : `<div class="cm-empty">Aucune zone trouvée.<br>L'intégration « Climate Manager » est-elle configurée ?</div>`;
      return;
    }
    const ctrl = this._controlSwitch();          // interrupteur maître (ou null)
    const observe = !!ctrl && !ctrl.on;          // mode observation = pilotage off
    // Mode intégré : juste la/les zone(s), sans en-tête ni pied.
    if (this._embedded) {
      this._body.innerHTML =
        `<div class="cm-zones cm-embedded">${zones.map((z) => this._zoneHtml(z, observe)).join("")}</div>`;
      return;
    }
    const absent = zones.some((z) => z.houseAbsent);

    let head;
    if (observe) {
      head = `
        <div class="cm-head">
          <div class="cm-title">${esc(this._title)}</div>
        </div>
        <div class="cm-paused">
          <span>⏸ <b>Mode observation</b> — aucune commande n'est envoyée aux clims. La carte affiche seulement les températures et l'état réel.</span>
          <button class="cm-go" data-act="enable-control" data-entity="${esc(ctrl.eid)}">Activer le pilotage</button>
        </div>`;
    } else {
      const sys = absent
        ? `<span class="cm-sys"><span class="dot"></span>En veille · bâtiment fermé</span>`
        : `<span class="cm-sys on"><span class="dot"></span>Système actif</span>`;
      const obsBtn = ctrl
        ? `<button class="cm-reset" data-act="disable-control" data-entity="${esc(ctrl.eid)}" title="Repasser en observation (ne plus piloter)">⏸ Observation</button>`
        : "";
      head = `
        <div class="cm-head">
          <div class="cm-title">${esc(this._title)}</div>
          <div class="cm-head-right">
            ${sys}
            ${obsBtn}
            <button class="cm-reset" data-act="reset-daily" title="Remet toutes les zones en Marche + Normal">↻ Réinitialiser</button>
          </div>
        </div>`;
    }

    // Mode system : en-tête + sélecteur saison + bannière hors-gel (coiffe les zones).
    if (this._systemOnly) {
      this._body.innerHTML = `${head}${this._seasonHtml(zones, observe)}${this._frostBanner(zones)}`;
      return;
    }

    this._body.innerHTML = `
      ${head}
      ${this._seasonHtml(zones, observe)}
      ${this._frostBanner(zones)}
      <div class="cm-zones">${zones.map((z) => this._zoneHtml(z, observe)).join("")}</div>
      <div class="cm-foot">Réglages structurels (zones, capteurs) et hors-gel : <em>Paramètres → Appareils &amp; services → Climate Manager → Configurer</em>.</div>
    `;
  }

  _frostBanner(zones) {
    // Statut système hors-gel — même valeur sur chaque zone, on lit la 1ère.
    const f = zones.find((z) => z.frost)?.frost;
    if (!f || !f.active) return "";
    const heat = f.direction === "heat";
    const icon = heat ? "🔥" : "❄️";
    const what = heat ? "Hors-gel — chauffage" : "Protection canicule — refroidissement";
    const until = f.ends_ts
      ? `jusqu'à ${new Date(f.ends_ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`
      : "";
    return `<div class="cm-frost ${heat ? "heat" : "cool"}">
        <span>${icon} <b>${esc(what)}</b> — toutes les zones tournent (régulation pendule) car le bâtiment est fermé.</span>
        <span class="cm-frost-when">${esc(until)}</span>
      </div>`;
  }

  _seasonHtml(zones, observe) {
    // Sens global du groupe extérieur (mono-mode) — un seul groupe = froid OU
    // chaud pour TOUTE la flotte. Même valeur sur chaque zone → on lit la 1ère.
    const s = zones.find((z) => z.season)?.season;
    if (!s) return "";
    const heat = s.direction === "heat";
    const dirTxt = heat ? "chaud 🔥" : "froid ❄️";
    const dis = observe ? "disabled" : "";
    const opts = [["auto", "Auto"], ["ete", "Été"], ["hiver", "Hiver"]];
    const seg = opts
      .map(([v, l]) =>
        `<button data-act="season" data-opt="${v}" class="${s.mode === v ? "sel" : ""}">${l}</button>`)
      .join("");
    const autoNote = s.mode === "auto" ? ` <span class="cm-season-auto">→ ${dirTxt} (auto)</span>` : "";
    return `<div class="cm-season ${heat ? "heat" : "cool"}" ${dis}>
        <span class="cm-season-lbl">Groupe extérieur<b>${s.mode === "auto" ? "" : " · " + dirTxt}</b>${autoNote}</span>
        <div class="cm-seg cm-season-seg">${seg}</div>
      </div>`;
  }

  _controlSwitch() {
    const ents = this._hass.entities || {};
    for (const eid in ents) {
      const e = ents[eid];
      if (e.platform === "climate_manager" && e.translation_key === "control_enabled") {
        return { eid, on: this._hass.states[eid]?.state === "on" };
      }
    }
    return null;
  }

  _zoneHtml(z, observe) {
    const meta = this._stateMeta(z);
    const dis = observe ? "disabled" : "";
    // Les réglages ne sont actifs que si la zone est en Marche et hors observation.
    const cdis = z.on && !observe ? "" : "disabled";

    const intSeg = POWER_LEVELS.map(
      ([val, lbl]) =>
        `<button data-act="power" data-entity="${esc(z.eids.power)}" data-opt="${val}" class="${z.power === val ? "sel" : ""}">${lbl}</button>`
    ).join("");
    const fanSeg = VENT_LEVELS.map(
      ([val, lbl]) =>
        `<button data-act="fan" data-entity="${esc(z.eids.fan)}" data-opt="${val}" class="${z.fan === val ? "sel" : ""}">${lbl}</button>`
    ).join("");
    const swSeg = this._swingSeg(z);

    // Statut sur UNE ligne : pastille colorée (qui pulse quand ça travaille) +
    // libellé coloré + écart à la cible. En prise en main : action « Reprendre auto ».
    const pulsing = z.on && (z.state === "running" || z.state === "starting") && z.regime !== "stabilisation";
    let status;
    if (z.inOverride) {
      const until = z.overrideUntilReset ? "jusqu'au reset" : this._untilTxt(z.overrideUntil);
      const resume = z.eids.resetOverride
        ? `· <button class="cm-link" data-act="resume" data-entity="${esc(z.eids.resetOverride)}" ${dis}>Reprendre auto</button>`
        : "";
      status =
        `<span class="cm-state-main" style="color:${C.override}"><span class="cm-dot" style="background:${C.override}"></span>✋ Pris en main</span>` +
        ` <span class="cm-delta">${esc(until)} ${resume}</span>`;
    } else {
      const delta = this._deltaTxt(z);
      status =
        `<span class="cm-state-main" style="color:${meta.color}">` +
          `<span class="cm-dot${pulsing ? " pulse" : ""}" style="background:${meta.color}"></span>${esc(meta.label)}` +
        `</span>` +
        (delta ? ` <span class="cm-delta">${delta}</span>` : "") +
        (z.windowsOpen ? ` <span class="cm-delta">· fenêtre ouverte</span>` : "");
    }

    const settings = this._showSettings ? this._settingsHtml(z) : "";
    // Interrupteur ON/OFF coulissant (le fond vert glisse à droite = Marche).
    const sw = `
      <div class="cm-switch ${z.on ? "on" : "off"}" ${dis}>
        <button data-act="zone-off" data-entity="${esc(z.eids.sw)}" class="${z.on ? "" : "sel"}">Arrêt</button>
        <button data-act="zone-on" data-entity="${esc(z.eids.sw)}" class="${z.on ? "sel" : ""}">Marche</button>
      </div>`;

    return `
      <div class="cm-zone ${z.on ? "" : "off"}">
        <div class="cm-z-head">
          <div>
            <div class="cm-z-name">${esc(z.name)}</div>
            <div class="cm-z-state">${status}</div>
          </div>
          ${z.eids.roomTemp
            ? `<div class="cm-z-temp cm-clickable" data-act="more-info" data-entity="${esc(z.eids.roomTemp)}" title="Voir l'historique des températures">${fmtTemp(z.roomTemp)}<small>°C</small></div>`
            : `<div class="cm-z-temp">${fmtTemp(z.roomTemp)}<small>°C</small></div>`}
        </div>
        ${sw}
        <div class="cm-ctrls">
          ${this._targetHtml(z, observe)}
          <div class="cm-ctl" ${cdis}><span class="cm-ctl-lbl">Intensité</span><div class="cm-seg">${intSeg}</div></div>
          <div class="cm-ctl" ${cdis}><span class="cm-ctl-lbl">Ventilation</span><div class="cm-seg">${fanSeg}</div></div>
          ${swSeg ? `<div class="cm-ctl" ${cdis}><span class="cm-ctl-lbl">Balayage</span><div class="cm-seg">${swSeg}</div></div>` : ""}
        </div>
        ${z.splits.length > 1 ? this._perSplitAdvanced(z, observe) : ""}
        ${settings}
      </div>`;
  }

  // Balayage au niveau zone : options réelles de la clim (swing_modes) + « Auto »
  // (= ne pas piloter). Appliqué à tous les splits de la zone au clic.
  _swingSeg(z) {
    if (!(z.swingModes && z.swingModes.length)) return "";
    const cur = z.swing || "";
    const opts = [["", "Auto"], ...z.swingModes.map((m) => [m, SWING_LABELS[m] || m])];
    return opts
      .map(
        ([val, lbl]) =>
          `<button data-act="zone-swing" data-zone="${esc(z.id)}" data-opt="${esc(val)}" class="${cur === val ? "sel" : ""}">${esc(lbl)}</button>`
      )
      .join("");
  }

  _targetHtml(z, observe) {
    // Valeur de base du stepper : cible explicite si définie, sinon cible
    // effective (dérivée des seuils), sinon 24.
    const base = z.targetTemp != null ? z.targetTemp : (z.targetDisplay != null ? z.targetDisplay : 24);
    const cdis = z.on && !observe ? "" : "disabled";
    // Petit ↺ discret pour revenir à la cible automatique (uniquement si une
    // cible explicite a été posée) — sans l'étiquette « auto » qui déroutait.
    const reset = z.targetTemp != null
      ? `<button class="cm-reset-auto" data-act="zone-target-auto" data-zone="${esc(z.id)}" title="Revenir à la cible automatique">↺</button>`
      : "";
    return `
      <div class="cm-ctl cm-target" ${cdis}>
        <span class="cm-ctl-lbl">Cible</span>
        <div class="cm-target-right">
          <div class="cm-stepper">
            <button data-act="zone-target-dec" data-zone="${esc(z.id)}" data-val="${esc(base)}">−</button>
            <span class="val">${fmtTemp(base)}°</span>
            <button data-act="zone-target-inc" data-zone="${esc(z.id)}" data-val="${esc(base)}">+</button>
          </div>
          ${reset}
        </div>
      </div>`;
  }

  // Réglage avancé PAR clim, uniquement pour les zones multi-splits (où
  // cible/puissance/balayage peuvent différer d'un split à l'autre). Les
  // contrôles zone (intensité, ventilation, balayage) restent au-dessus ; ceci
  // n'est qu'un panneau repliable pour affiner split par split.
  _perSplitAdvanced(z, observe) {
    if (z.splits.length <= 1) return "";
    const open = this._openSplits.has(z.id) ? "open" : "";
    const dis = observe ? 'data-disabled="1"' : "";
    const rows = z.splits.map((s) => this._splitRow(z, s, dis)).join("");
    return `<details class="cm-opt" ${open} data-zone="${esc(z.id)}">
        <summary data-act="toggle-splits" data-zone="${esc(z.id)}">Réglage par clim (${z.splits.length})</summary>
        ${rows}
      </details>`;
  }

  _splitRow(z, s, dis) {
    const mm = this._splitModeMeta(s);
    const it = Number.isFinite(parseFloat(s.temp)) ? `${fmtTemp(s.temp)}°` : "";
    const sp = Number.isFinite(parseFloat(s.setpoint)) ? `→ ${fmtTemp(s.setpoint)}°` : "";
    const powSeg = [["", "Auto"], ...POWER_LEVELS]
      .map(
        ([val, lbl]) =>
          `<button data-act="split-power" data-zone="${esc(z.id)}" data-entity="${esc(s.id)}" data-opt="${val}" class="${(s.power || "") === val ? "sel" : ""}">${lbl}</button>`
      )
      .join("");
    const tgt = s.target != null ? s.target : "";
    const ph = s.effectiveTarget != null ? `${fmtTemp(s.effectiveTarget)} (auto)` : "auto";
    return `
      <div class="cm-split" ${dis}>
        <div class="nm"><b>${esc(s.name)}</b><span class="meta">${esc(mm.label)} ${it} ${sp}</span></div>
        <label>Cible °C</label>
        <input type="number" step="0.5" min="16" max="32"
          data-act="split-target" data-zone="${esc(z.id)}" data-entity="${esc(s.id)}"
          value="${esc(tgt)}" placeholder="${esc(ph)}">
        <label>Puissance</label>
        <div class="cm-seg cm-seg-sm">${powSeg}</div>
        <label>Balayage</label>
        ${this._swingSelect(z, s)}
      </div>`;
  }

  _swingSelect(z, s) {
    if (!(s.swingModes && s.swingModes.length)) return `<span class="meta">non géré</span>`;
    return `<select data-act="split-swing" data-zone="${esc(z.id)}" data-entity="${esc(s.id)}">
        <option value="">Auto</option>
        ${s.swingModes.map((m) => `<option value="${esc(m)}" ${s.swing === m ? "selected" : ""}>${esc(m)}</option>`).join("")}
      </select>`;
  }

  _splitModeMeta(s) {
    switch (s.mode) {
      case "cool": return { label: "Froid", color: C.cool };
      case "heat": return { label: "Chaud", color: C.heat };
      case "off": return { label: "Éteint", color: C.off };
      case "fan_only": return { label: "Ventil.", color: C.idle };
      case "dry": return { label: "Sec", color: C.idle };
      case "unavailable": return { label: "Indispo", color: C.off };
      default: return { label: s.mode || "—", color: C.idle };
    }
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

  // Quand la zone travaille : l'OFFSET du pendule appliqué (décalage de la
  // consigne / sonde — attaque = négatif = plus froid ; relâchement = positif)
  // + l'écart restant à la cible de confort. La cible est déjà dans le stepper.
  _deltaTxt(z) {
    if (!z.on || (z.state !== "running" && z.state !== "starting")) return "";
    const off = z.offset;
    const offTxt =
      off != null && Number.isFinite(off)
        ? `offset <b>${off < 0 ? "−" : "+"}${(Math.abs(off) % 1 ? Math.abs(off).toFixed(1) : Math.abs(off).toFixed(0))}°</b>`
        : "";
    const join = (a, b) => (a && b ? `${a} · ${b}` : a || b);
    if (z.regime === "stabilisation") return join(offTxt, "maintien");
    const target = z.targetDisplay ?? z.targetTemp;
    const room = parseFloat(z.roomTemp);
    if (target == null || !Number.isFinite(room)) return offTxt;
    const d = room - target;
    const heat = z.dir === "heat";
    // Proche de la cible → pas d'écart trompeur (« ▼ 0,1° »).
    if ((heat && d >= -0.3) || (!heat && d <= 0.3)) return join(offTxt, "quasi à la cible");
    const arrow = heat ? "▲" : "▼";
    const verbe = heat ? "à gagner" : "à perdre";
    return join(offTxt, `${arrow} ${Math.abs(d).toFixed(1)}° ${verbe}`);
  }

  _stateMeta(z) {
    if (!z.on) return { label: "Éteint", color: C.off };
    switch (z.state) {
      case "starting":
      case "running": {
        // Pendule : en relâchement (regime stabilisation), le split reste allumé
        // mais idle → on l'affiche « Maintien » pour ne pas laisser croire qu'il
        // turbine en permanence.
        const maint = z.state === "running" && z.regime === "stabilisation";
        if (z.dir === "cool")
          return maint
            ? { label: "Maintien ❄", color: C.stab }
            : { label: z.state === "starting" ? "Démarrage ❄" : "Refroidit ❄", color: C.cool };
        if (z.dir === "heat")
          return maint
            ? { label: "Maintien 🔥", color: C.stab }
            : { label: z.state === "starting" ? "Démarrage 🔥" : "Chauffe 🔥", color: C.heat };
        return { label: "Actif", color: C.cool };
      }
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
    // Laisse le comportement natif : ouverture <details>, clic dans les champs
    // (placement curseur input nombre, ouverture du select).
    if (act === "toggle-cfg" || act === "toggle-splits" || act === "number"
        || act === "split-target" || act === "split-swing") return;
    e.preventDefault();
    const ent = el.dataset.entity;
    switch (act) {
      case "toggle":
        this._call("switch", "toggle", { entity_id: ent });
        break;
      case "zone-on":
        this._call("switch", "turn_on", { entity_id: ent });
        break;
      case "zone-off":
        this._call("switch", "turn_off", { entity_id: ent });
        break;
      case "power":
      case "fan":
        this._call("select", "select_option", { entity_id: ent, option: el.dataset.opt });
        break;
      case "zone-swing": {
        // Balayage au niveau zone → appliqué à chaque split ("" = ne pas piloter).
        const zone = this._zones().find((z) => String(z.id) === el.dataset.zone);
        const swing = el.dataset.opt || null;
        for (const s of zone?.splits || []) {
          this._call("climate_manager", "set_split", {
            zone_id: el.dataset.zone,
            climate_entity: s.id,
            swing,
          });
        }
        break;
      }
      case "split-power":
        this._call("climate_manager", "set_split", {
          zone_id: el.dataset.zone,
          climate_entity: ent,
          power: el.dataset.opt || null,   // "" → null = hérite de la zone
        });
        break;
      case "zone-target-dec":
      case "zone-target-inc": {
        const cur = parseFloat(el.dataset.val);
        if (!Number.isFinite(cur)) break;
        const step = act === "zone-target-inc" ? 0.5 : -0.5;
        const next = Math.min(30, Math.max(16, Math.round((cur + step) * 2) / 2));
        this._call("climate_manager", "set_zone_target", {
          zone_id: el.dataset.zone, target_temp: next,
        });
        break;
      }
      case "zone-target-auto":
        this._call("climate_manager", "set_zone_target", {
          zone_id: el.dataset.zone, target_temp: null,
        });
        break;
      case "resume":
        this._call("button", "press", { entity_id: ent });
        break;
      case "reset-daily":
        this._call("climate_manager", "reset_daily", {});
        break;
      case "season":
        this._call("climate_manager", "set_season_mode", { mode: el.dataset.opt });
        break;
      case "more-info":
        // Ouvre la fenêtre native Home Assistant (avec le graphe d'historique).
        this._moreInfo(ent);
        break;
      case "enable-control":
        this._call("switch", "turn_on", { entity_id: ent });
        break;
      case "disable-control":
        this._call("switch", "turn_off", { entity_id: ent });
        break;
    }
  }

  _onClickDetailsTrack(set, zoneId, open) {
    if (open) set.add(zoneId);
    else set.delete(zoneId);
  }

  _onChange(e) {
    // Réglages numériques de zone (seuils, durées)
    const numEl = e.target.closest('input[data-act="number"]');
    if (numEl) {
      const v = parseFloat(numEl.value);
      if (!Number.isFinite(v)) return;
      this._call("number", "set_value", { entity_id: numEl.dataset.entity, value: v });
      return;
    }
    // Cible par split (vide → null = hérite de la zone)
    const tgtEl = e.target.closest('input[data-act="split-target"]');
    if (tgtEl) {
      const raw = tgtEl.value.trim();
      const target = raw === "" ? null : parseFloat(raw);
      if (target !== null && !Number.isFinite(target)) return;
      this._call("climate_manager", "set_split", {
        zone_id: tgtEl.dataset.zone,
        climate_entity: tgtEl.dataset.entity,
        target,
      });
      return;
    }
    // Balayage par split (vide → null = ne pas piloter)
    const swEl = e.target.closest('select[data-act="split-swing"]');
    if (swEl) {
      this._call("climate_manager", "set_split", {
        zone_id: swEl.dataset.zone,
        climate_entity: swEl.dataset.entity,
        swing: swEl.value || null,
      });
    }
  }

  connectedCallback() {
    // Mémorise l'état ouvert/fermé des panneaux Réglages.
    this.addEventListener("toggle", (e) => {
      const d = e.target;
      if (!d.classList) return;
      if (d.classList.contains("cm-cfg")) {
        this._onClickDetailsTrack(this._open, d.dataset.zone, d.open);
      } else if (d.classList.contains("cm-opt")) {
        this._onClickDetailsTrack(this._openSplits, d.dataset.zone, d.open);
      }
    }, true);
  }

  _call(domain, service, data) {
    this._hass.callService(domain, service, data);
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    // Événement natif HA : composed:true pour franchir les shadow roots et
    // atteindre <home-assistant> qui ouvre la fiche (avec l'historique).
    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        detail: { entityId },
        bubbles: true,
        composed: true,
      })
    );
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
