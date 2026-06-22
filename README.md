# Climate Manager — LOKRIS

**Fork « boulot »** de [`climate-manager`](https://github.com/delormejonathan/climate-manager)
(version maison Daikin), adapté à l'instance Home Assistant du bureau : clims
**Hitachi/Modbus**, gestion **par zone**, contrôle simplifié pour les collègues
et **reset quotidien** au désarmement de l'alarme.

Différences clés avec la version maison :

| | Maison (Daikin) | LOKRIS (Hitachi) |
|---|---|---|
| Matériel | Daikin (windnice, quiet, 1..5) | Hitachi/Modbus (auto/low/medium/high/top, pas 1.0) |
| Zone | 1 zone = 1 split | **1 zone = N splits** (Openspace, Cuisine, Espace Détente = 2 splits) |
| Contrôle exposé | seuils + Auto/Off/Boost | **Marche/Arrêt + Intensité (Doux/Normal/Frais)**, zéro chiffre |
| Gating / reset | planning + présence | **alarme AJAX désarmée = ON**, front de désarmement = reset du jour |
| Override collègue | timer (30 min) | **tient jusqu'au reset du matin** |
| Config | manuelle | **9 zones pré-câblées** à l'installation (seed) |

Conserve de la version maison : la **technique du pendule** (consigne = sonde
interne du split ± offset signé), la **state machine** par zone (IDLE → STARTING
→ RUNNING → STABILIZING → COOLDOWN), la détection d'override via `context`, le
journal des cycles et la carte Lovelace.

### Le cœur du besoin boulot

- Les collègues pilotent **par zone** : un interrupteur Marche/Arrêt + un curseur
  d'intensité (Doux ⟷ Frais). Aucune température à régler (les seuils sont en
  config admin, masqués).
- Chaque matin, quand le 1ᵉʳ arrivant **désarme l'alarme**, toutes les zones
  repassent **ON + Normal** et les overrides de la veille sont effacés → on ne
  reste jamais avec des clims éteintes.
- Le soir, **alarme armée** → toutes les zones coupées (le gating s'en charge).

## Installation

À l'ajout de l'intégration, **les 9 zones du bureau sont créées automatiquement**
(mapping repris de l'ancienne automation `climatisation_regulation_zones`) ainsi
que le gating sur `alarm_control_panel.ajax_zone_1_alarm`. Tout reste éditable
via _Configurer_.

## Structure

```
climate_manager/
├── custom_components/climate_manager/   # le composant HA (Python)
├── lovelace/                              # la carte Lovelace custom (JS/TS)
├── docs/                                  # documentation
├── tests/                                 # tests pytest
└── .beads/                                # DAG des tâches (beads)
```

## Installation via HACS (recommandé)

1. HACS → menu ⋮ → **Custom repositories**
2. Repository : `https://github.com/lokris-dev/climate-manager-lokris`
3. Category : **Integration**
4. **Add** → tu retrouves "Climate Manager — LOKRIS" dans la liste HACS → **Download**
5. Redémarre HA
6. _Paramètres → Appareils & services → Ajouter une intégration → Climate Manager_

Les futures versions remontent automatiquement comme update disponible dans HACS — 1 clic pour installer.

La carte Lovelace est embarquée dans le composant : elle est servie automatiquement à `/climate_manager/climate-manager-card.js` et enregistrée comme ressource Lovelace dès que l'intégration démarre.

### Cartes Lovelace disponibles

Carte complète historique :

```yaml
type: custom:climate-manager-card
zone: rdc
title: RDC
climate_entity: climate.salon
```

Widgets séparés, plus faciles à intégrer dans un dashboard clair / éditorial :

```yaml
- type: custom:climate-manager-status-card
  zone: rdc
  title: État actuel
  climate_entity: climate.salon

- type: custom:climate-manager-pilotage-card
  zone: rdc
  title: Pilotage
  climate_entity: climate.salon

- type: custom:climate-manager-profiles-card
  zone: rdc
  title: Profils
  climate_entity: climate.salon

- type: custom:climate-manager-sessions-card
  zone: rdc
  title: Sessions
  climate_entity: climate.salon
```

Alternative équivalente avec la carte principale : `widget: status`, `widget: pilotage`, `widget: profiles` ou `widget: sessions`.

## Migration depuis l'automation actuelle

Une fois le composant en production stable, supprimer :

- L'automation `automation.climatisation_controleur_unique` et toutes les automations climat désactivées
- Les helpers `input_number.climatisation_*`, `input_boolean.climatisation_*`, `input_datetime.climatisation_*`, `input_select.climatisation_*`

À **conserver** :

- Les capteurs agrégés `sensor.temperature_moyenne_rdc`, `_etage`, `_moyenne` (utilisés ailleurs dans HA)
