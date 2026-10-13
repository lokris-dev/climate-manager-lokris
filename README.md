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

### Mode observation (démarrage sûr)

À la première installation, l'intégration démarre **en observation** : elle lit
les températures et l'état réel des splits et **alimente la carte, mais n'envoie
aucune commande** aux clims (elle ne se bat donc pas avec une automation
existante). La carte affiche une bannière **⏸ Mode observation** + un bouton
**Activer le pilotage**.

Quand tu es prêt à piloter pour de vrai : clique **Activer le pilotage** (ou
bascule l'entité `switch.…_pilotage_actif`), puis **↻ Réinitialiser** pour mettre
toutes les zones en Marche + Normal. Pour revenir en lecture seule : bouton
**⏸ Observation** dans l'en-tête.

### Pré-refroidissement matinal

Le gating par l'alarme coupe **tout** le bâtiment de ~18h à ~8h. Sur les
bâtiments mal exposés, certaines pièces passent la nuit 3 °C au-dessus des
autres et les collègues arrivent le matin dans un bureau déjà chaud : la
climatisation passe alors la matinée à rattraper l'inertie de la veille.

Le pré-refroidissement est une **exception de gating ciblée** : pendant une
fenêtre horaire, les zones concernées voient `system_enabled` repasser à `True`
même bâtiment armé, et régulent normalement (pendule inclus) vers
`precool_target_temp` — ou leur propre cible si elle est laissée vide. Hors
fenêtre, le gating reprend et coupe les splits.

Le réglage est **coupé en deux**, chaque moitié là où elle a du sens :

| | Où | Pourquoi là |
|---|---|---|
| **Quand** — fenêtre, jours, cible | **Système** (Configurer → System) | Un seul groupe extérieur : démarrer les zones à des heures différentes ne ferait que multiplier les cycles du compresseur |
| **Qui** — le périmètre | **Par zone** | C'est une propriété de la pièce : ce bureau chauffe, la salle de réunion non |

Trois chemins pilotent la même clé de zone :

- l'interrupteur **`switch.…_pre_refroidissement_matinal`** de la zone (catégorie
  config, donc hors du dashboard collègues) ;
- le toggle dans le panneau **« Options »** du widget de zone de la carte ;
- la case dans le formulaire d'édition de zone du flow d'options.

Réglages système :

| Réglage | Rôle |
|---|---|
| `precool_enabled` | Interrupteur maître de la fonctionnalité |
| `precool_start_hour` / `precool_end_hour` | Fenêtre en heure **locale** (fin exclue) |
| `precool_target_temp` | Cible imposée ; vide = chaque zone garde la sienne |
| `precool_weekdays` | 0 = lundi ; pour une fenêtre à cheval sur minuit, c'est le jour d'**ouverture** |

Deux garde-fous :

- **Jamais de pré-chauffage.** Le groupe extérieur étant mono-mode, quand le sens
  du système est `heat` (hiver) la fonctionnalité se neutralise d'elle-même —
  sinon on chaufferait les bureaux à 4h du matin.
- **Reset au front d'ouverture.** Une zone qu'un collègue a coupée la veille
  (mode OFF, qui tient « jusqu'au reset du matin ») serait restée éteinte
  pendant toute la fenêtre. L'ouverture de la fenêtre *est* le matin : elle
  déclenche donc le reset quotidien, **restreint aux zones concernées**. Une
  coupure faite *pendant* la fenêtre, elle, est respectée.

### Maintien (relâchement du pendule)

**Froid : inchangé.** Cible atteinte, le split reste allumé avec une consigne
`max(sonde du split, cible) + release`, bornée par la cible depuis v1.4.23.
Si la pièce descend de 1 °C sous sa cible, la consigne passe en butée à 32 °C.
La décision attaque/relâchement reste individuelle pour les cibles par split.

**Chaud : extinction réelle de toute la zone (v1.4.24).** Le 13/10, les pièces
ont continué à monter à 24–25,5 °C malgré les consignes de maintien à 20 puis
18 °C : sur ce groupe Hitachi mono-mode, une consigne basse ne met pas le split
en veille tant que le groupe chauffe. Le maintien envoie donc `hvac_mode: off`
à **tous** les splits de la zone. La reprise en `heat` intervient quand la
température de pièce redescend **strictement sous la cible de zone**.

- Minimum allumé et minimum éteint : **15 minutes chacun**, définis par
  `HEAT_MIN_ON_SECONDS` / `HEAT_MIN_OFF_SECONDS`. Le chronomètre est persisté
  et reste valable après reload, redémarrage HA et reset quotidien.
- À cible atteinte avant la fin du minimum allumé, la chauffe est adoucie
  jusqu'à la coupure. Un dépassement de **1 °C** force OFF immédiatement ;
  zone OFF, fenêtre ouverte et gating fermé restent également prioritaires.
- La **cible de zone gouverne la coupure de tous les splits**, même si un
  secondaire a une cible individuelle plus haute. Puissance et swing propres
  restent appliqués pendant la chauffe ; une cible individuelle plus basse
  adoucit son approche. Les états/consignes réels sont lus par split pour
  resynchroniser un secondaire resté allumé, éteint ou à une ancienne consigne.
- L'attaque reste habituelle loin de la cible. Dans les **2 °C** d'approche
  (`HEAT_APPROACH_BAND`), sa consigne diminue progressivement vers
  **cible + 1 °C** (`HEAT_APPROACH_HEADROOM`), avec arrondi vers le bas au pas
  du split. L'approche est appliquée dès le premier tick de démarrage.
- L'intention OFF est enregistrée avant les commandes. Les anciennes valeurs
  de consigne et ventilation sont ignorées en OFF par la détection d'override ;
  une remise en marche manuelle reste une prise en main.

Le cycle logique reste en « Maintien » avec la direction chauffage verrouillée
pendant l'arrêt, pour reprendre sans attendre le seuil de début de chauffage.
Si le capteur de pièce devient indisponible pendant l'arrêt, aucune reprise
n'est demandée. Le boost explicite conserve son fonctionnement prioritaire.

Validation locale ciblée :

```bash
.venv/bin/python -m pytest tests/test_heat_maintenance.py tests/test_release_drift.py tests/test_pendulum.py tests/test_override_debounce.py tests/test_runtime_persistence.py tests/test_boot_recovery.py tests/test_system_direction.py tests/test_season_flip.py -o addopts=-ra -q -W ignore::DeprecationWarning
.venv/bin/ruff check custom_components/climate_manager/ tests/test_heat_maintenance.py tests/test_release_drift.py tests/test_pendulum.py tests/test_override_debounce.py
```

### Changement de saison

À chaque vraie bascule froid ↔ chaud du groupe (sélecteur Été/Hiver, ou
bascule automatique), **toutes les cibles de zone repassent à 22 °C** et les
cibles par split repassent en « hérité ». Sans ce reset, une cible d'été à
25 °C devenait une cible de chauffe en hiver. Repasser en Auto dans le même sens
ne touche à rien, le hors-gel non plus.

## Migration depuis l'automation actuelle

Une fois le composant en production stable, supprimer :

- L'automation `automation.climatisation_controleur_unique` et toutes les automations climat désactivées
- Les helpers `input_number.climatisation_*`, `input_boolean.climatisation_*`, `input_datetime.climatisation_*`, `input_select.climatisation_*`

À **conserver** :

- Les capteurs agrégés `sensor.temperature_moyenne_rdc`, `_etage`, `_moyenne` (utilisés ailleurs dans HA)
