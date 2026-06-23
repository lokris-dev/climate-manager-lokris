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

### Carte Lovelace — widget unique multi-zones

Une **seule carte** affiche et gère **toutes les zones** (pas de config par zone) :

```yaml
type: custom:climate-manager-card
title: Climatisation          # optionnel
show_settings: true           # optionnel (défaut true) — section ⚙ Réglages
```

La carte découvre les zones automatiquement (via le registre d'entités) et, pour
chaque zone, propose :

- **Marche / Arrêt** + curseur d'**Intensité** (Doux / Normal / Frais) — zéro chiffre ;
- la T° de la pièce, l'état (Refroidit / Chauffe / En attente / Pris en main…),
  les splits pilotés et leur T° interne, la consigne envoyée ;
- un bandeau **« Pris en main »** + bouton *Reprendre auto* quand un collègue a
  la main ;
- un volet **⚙ Réglages** (admin) pour éditer les seuils (début/fin chaud & froid)
  et les durées ; les splits/capteurs y sont listés (modifiables via _Configurer_).

En haut : l'état du système (alarme désarmée = actif) et un bouton **↻ Réinitialiser**
(remet toutes les zones en Marche + Normal — équivalent du désarmement matinal,
via le service `climate_manager.reset_daily`).

## Migration depuis l'automation actuelle

Une fois le composant en production stable, supprimer :

- L'automation `automation.climatisation_controleur_unique` et toutes les automations climat désactivées
- Les helpers `input_number.climatisation_*`, `input_boolean.climatisation_*`, `input_datetime.climatisation_*`, `input_select.climatisation_*`

À **conserver** :

- Les capteurs agrégés `sensor.temperature_moyenne_rdc`, `_etage`, `_moyenne` (utilisés ailleurs dans HA)
