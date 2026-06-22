# Architecture — climate_manager

> Document de référence des décisions prises avant l'implémentation. Toute déviation doit être justifiée et le doc mis à jour.

## 1. Objectif

Remplacer l'automation `climatisation_controleur_unique` (294 lignes de YAML, plusieurs bugs confirmés, état "stabilisation" stocké dans un `input_datetime` avec sentinelle epoch 0) par un custom_component Python testable, avec une carte Lovelace dédiée.

Bug critique de l'automation actuelle (à ne **pas** reproduire) : le calcul d'offset ajoute toujours l'offset à `T°_interne_clim` quel que soit le mode. En cool, l'offset reste positif → consigne envoyée > T°_interne → clim neutralisée → **ne refroidit jamais**.

## 2. Concepts

### 2.1. Source de vérité de la température

- **Capteur pièce** (moyenne de N capteurs externes configurés par zone) : sert à **décider** si la clim doit chauffer/refroidir
- **Capteur interne clim** (`current_temperature` de l'entité `climate.*`) : sert à **piloter** la clim. Faussé par le flux d'air de la clim elle-même, mais c'est ce que la clim "voit" et auquel elle réagit

### 2.2. Technique du pendule

La clim ne réagit qu'à son propre capteur interne. Donc pour la piloter :

| Mode | Consigne envoyée | Effet |
|---|---|---|
| Cool | `< T°_interne` | refroidit activement |
| Cool | `= T°_interne` | neutralisée (compresseur en pause) |
| Cool | `> T°_interne` | totalement inactive |
| Heat | `> T°_interne` | chauffe activement |
| Heat | `= T°_interne` | neutralisée |
| Heat | `< T°_interne` | totalement inactive |

La consigne **envoyée** à la clim n'est **jamais** la consigne réelle souhaitée. C'est un "offset signé" autour du capteur interne.

### 2.3. Inertie thermique

Quand on coupe la clim au seuil cible, la chaleur stockée dans murs/meubles ressort dans les ~30 min suivantes → la T° pièce remonte. D'où la phase de **stabilisation** (consigne proche de T°_interne mais avec un léger offset de maintien, ventilation quiet) avant le `turn_off` complet.

### 2.4. Modes Daikin pertinents

- `swing_modes`: `windnice` = mode confort natif (oriente vers le **haut** en froid, vers le **bas** en chaud). On le force toujours, on ne le pilote pas.
- `fan_modes`: `auto`, `quiet`, `1`...`5`
- `hvac_modes`: `off`, `heat`, `cool`, `heat_cool`, `dry`, `fan_only`

## 3. State machine par zone

```
                          ┌─────────────┐
                          │   IDLE      │ ◄────────┐
                          │ (schedule   │          │
                          │  on, T° OK) │          │
                          └──────┬──────┘          │
                                 │ T° hors plage   │
                                 ▼                 │
                          ┌─────────────┐          │
                ┌────────►│  STARTING   │          │
                │         └──────┬──────┘          │
                │                ▼                 │
                │         ┌─────────────┐          │
                │         │  RUNNING    │          │ cooldown
                │         │  (3 régimes)│          │ expiré
                │         └──────┬──────┘          │
                │                │ seuil_fin      │
                │                │ atteint        │
                │                ▼                 │
                │         ┌─────────────┐          │
                │         │STABILIZING  │          │
                │         │ (pendule)   │          │
                │         └──────┬──────┘          │
                │                │ N min écoulées  │
                │                ▼                 │
                │         ┌─────────────┐          │
                │         │  COOLDOWN   │ ─────────┘
                │         │ (turn_off)  │
                │         └─────────────┘
                │
                │      [override externe détecté]
                │              │
                │              ▼
                │       ┌──────────────────┐
                │       │ MANUAL_OVERRIDE  │
                └───────┤  (timed / free)  │
       fin override     └──────────────────┘
       (timer ou         
       schedule rouvre + 
       option A)
```

### 3.1. Hors schedule

État spécial **`SCHEDULE_OFF`** : la zone ne démarre jamais en auto, mais accepte un override libre sans timeout. Si une transition externe `off → on` (utilisateur allume) survient pendant `SCHEDULE_OFF`, on entre en `MANUAL_OVERRIDE` mode "free" (pas de timer).

À la transition `SCHEDULE_OFF → IDLE` (schedule rouvre) avec clim encore allumée par l'utilisateur :
**option A choisie : auto reprend immédiatement** (peut couper la clim si T° dans la plage).

### 3.2. Override manuel — détection

À chaque appel de service vers l'entité clim, le composant utilise un `Context` HA dédié et stocke son `id` dans une fenêtre glissante (deque, 30s).

Sur chaque `state_changed` reçu :

- Si `event.context.id` ∈ fenêtre → c'est nous, on ignore
- Sinon → **override détecté** :
  - Pendant schedule on → `MANUAL_OVERRIDE_TIMED` (timer N min, défaut 30, configurable par zone via entité `number`)
  - Pendant schedule off → `MANUAL_OVERRIDE_FREE` (pas de timer)

À la fin du timer (timed), le composant reprend la main. Un bouton "Reprendre auto" expose un raccourci pour court-circuiter.

## 4. Algorithme de pilotage

### 4.1. Décision (basée sur capteur pièce)

À chaque tick (toutes les 30s, ou sur événement de changement de température pièce / état clim) :

```
T_piece = moyenne des capteurs configurés
mode_zone = etat machine actuel

si schedule_off:
    si clim allumée et c'est nous → climate.turn_off
    return

si une fenêtre listée est ouverte:
    si clim allumée et c'est nous → climate.turn_off
    return

si IDLE:
    si T_piece < seuil_debut_chauffage → transition STARTING(heat)
    si T_piece > seuil_debut_refroidissement → transition STARTING(cool)

si RUNNING:
    si en cool et T_piece ≤ seuil_fin_refroidissement → transition STABILIZING
    si en heat et T_piece ≥ seuil_fin_chauffage → transition STABILIZING

si STABILIZING:
    si depuis_entrée > duree_stabilisation → transition COOLDOWN

si COOLDOWN:
    si depuis_entrée > duree_cooldown → transition IDLE
```

### 4.2. Pilotage (basé sur T°_interne clim)

Une fois la décision prise (heat/cool actif), choix du régime selon l'écart :

```
ecart = abs(T_piece - seuil_fin)   # positif = encore à faire

si STARTING ou (RUNNING et ecart > 2°C):
    régime = ATTAQUE
    consigne_envoyée = T_int ± 5   (- en cool, + en heat)
    fan = auto

sinon si RUNNING et ecart > 0.5°C:
    régime = CROISIERE
    consigne_envoyée = T_int ± 2
    fan = auto

sinon si RUNNING et ecart > 0:
    régime = APPROCHE
    consigne_envoyée = T_int ± 1
    fan = quiet

sinon si STABILIZING:
    régime = STABILISATION (maintien doux)
    consigne_envoyée = T_int ± offset_stabilisation
    fan = quiet
```

Toujours : `swing_mode = windnice` (mode confort natif Daikin).

### 4.3. Rate limiting

- Pas plus d'**un changement de consigne par 60s** par clim
- Si consigne calculée ≈ consigne actuelle (delta < 0.5°C) → pas d'appel à `set_temperature`
- Si fan_mode déjà OK → pas d'appel à `set_fan_mode`

Évite le spam de commandes vers la Daikin (autre bug de l'automation actuelle).

### 4.4. Mode Boost (preset)

Activation manuelle : consigne = `T_int ± 5`, fan = `4`, swing = `swing` (au lieu de `windnice`) **pendant 15 min**, puis retour au régime auto. Override conscient et limité, expose le flux d'air directement vers les occupants.

## 5. ConfigFlow / UI

### 5.1. Niveau intégration (1 seule instance)

À l'ajout de l'intégration :

- `presence_entity` : entité `alarm_control_panel.*` (dropdown)
- `presence_absent_states` : liste d'états considérés comme "absent" (multi-select, ex: `armed_away`, `armed_vacation`)

### 5.2. Niveau zone (N instances)

Bouton "Ajouter une zone" depuis l'écran d'intégration :

| Champ | Type | Note |
|---|---|---|
| `name` | string | "RDC", "Étage"... |
| `climate_entity` | entity selector | filtre `climate.*` |
| `temperature_sensors` | multi entity selector | filtre `sensor.*` avec `device_class=temperature` |
| `seuil_debut_chauffage` | number | défaut 19.5 |
| `seuil_fin_chauffage` | number | défaut 21.0 |
| `seuil_debut_refroidissement` | number | défaut 26.5 |
| `seuil_fin_refroidissement` | number | défaut 25.0 |
| `window_sensors` | multi entity selector | filtre `binary_sensor.*` avec `device_class=window` (optionnel) |
| `aggressive_when_absent` | bool | défaut true |
| `duree_stabilisation_min` | number | défaut 60 |
| `duree_cooldown_min` | number | défaut 10 |
| `override_duree_min` | number | défaut 30 |

À la création de zone, le composant crée automatiquement `schedule.climate_manager_<zone>` avec une plage par défaut (06:00–23:00) que l'utilisateur peut éditer depuis l'éditeur de planning HA.

### 5.3. Reconfiguration

Toute zone est éditable post-création via le flow "Configure" de l'entrée d'intégration.

## 6. Entités exposées par zone

| Type | Entity_id | Rôle |
|---|---|---|
| `sensor` | `<zone>_temperature_moyenne` | Moyenne calculée des capteurs T° de la zone |
| `sensor` | `<zone>_etat` | Valeur de la state machine (IDLE, RUNNING, STABILIZING…) |
| `sensor` | `<zone>_regime` | Régime actuel (ATTAQUE, CROISIERE, APPROCHE, STABILISATION) |
| `sensor` | `<zone>_consigne_envoyee` | Dernière consigne envoyée à la clim (= valeur "pendule") |
| `sensor` | `<zone>_override_actif_jusqu_a` | Datetime fin override (vide si pas d'override) |
| `switch` | `<zone>_auto` | Active/désactive le pilotage auto |
| `select` | `<zone>_mode` | Auto / Off / Boost |
| `number` | `<zone>_seuil_debut_chauffage` | Modifiable depuis l'UI |
| `number` | `<zone>_seuil_fin_chauffage` | idem |
| `number` | `<zone>_seuil_debut_refroidissement` | idem |
| `number` | `<zone>_seuil_fin_refroidissement` | idem |
| `number` | `<zone>_override_duree_min` | Délai override (5–240 min) |
| `button` | `<zone>_reprendre_auto` | Court-circuite l'override en cours |
| `button` | `<zone>_boost` | Active le boost 15 min |

Auto-créée : `schedule.climate_manager_<zone>`.

## 7. Services HA

| Service | Args | Effet |
|---|---|---|
| `climate_manager.set_mode` | `zone`, `mode` (auto/off/boost) | Change le mode global d'une zone |
| `climate_manager.force_off` | `zone` | Stop forcé immédiat |
| `climate_manager.reset_override` | `zone` | Sort de l'override en cours |
| `climate_manager.reload_zones` | — | Recharge la config sans redémarrer HA |

## 8. Carte Lovelace

Card custom `climate-manager-card` (Lit-element TypeScript).

Une instance = une zone. Affichage :

- Badge d'état avec couleur (vert RUNNING, orange APPROCHE/STAB, gris IDLE, rouge OVERRIDE)
- T° pièce (gros) + consigne souhaitée (seuils) + consigne envoyée (= pendule) + T°_interne clim
- Sliders inline pour les 4 seuils (lien direct vers les entités `number`)
- Sélecteur preset (Auto / Off / Boost)
- Indicateur fenêtres ouvertes (si configuré)
- Indicateur "override jusqu'à HH:MM" + bouton "Reprendre auto"
- Mini-graphe T° pièce + consigne envoyée sur 24h

## 9. Structure du repo

```
climate_manager/
├── custom_components/climate_manager/
│   ├── __init__.py            # entry point, setup_entry / unload_entry
│   ├── manifest.json
│   ├── const.py               # constantes (DOMAIN, états, régimes, defaults)
│   ├── config_flow.py         # ConfigFlow + OptionsFlow
│   ├── coordinator.py         # DataUpdateCoordinator + state machine
│   ├── zone.py                # Logique d'une zone (algo décision + pilotage)
│   ├── context_tracker.py     # Détection override via context HA
│   ├── sensor.py              # plateforme sensor
│   ├── switch.py              # plateforme switch
│   ├── select.py              # plateforme select
│   ├── number.py              # plateforme number
│   ├── button.py              # plateforme button
│   ├── services.yaml          # description des services
│   └── translations/fr.json   # libellés FR
├── lovelace/
│   ├── src/                   # TS sources
│   └── package.json
├── docs/
│   └── architecture.md        # ce doc
├── tests/
│   ├── test_zone.py           # algo décision + pilotage
│   ├── test_context_tracker.py
│   └── conftest.py
├── .beads/                    # DAG des tâches
├── README.md
└── .gitignore
```

## 10. Migration

Une fois `climate_manager` stable :

**Supprimer :**
- `automation.climatisation_controleur_unique` (1760779888860)
- Les 14 automations climat désactivées (`automation.climatisation_*`, `automation.thermostat_gestion_*`)
- Tous les `input_number.climatisation_*`, `input_boolean.climatisation_*`, `input_datetime.climatisation_*`, `input_select.climatisation_*`

**Garder :**
- `sensor.temperature_moyenne_rdc`, `sensor.temperature_moyenne_etage`, `sensor.temperature_moyenne` (utilisés ailleurs)

**Plus tard :**
- Le `climate.chauffage_salle_de_bain` peut devenir une zone supplémentaire dans le composant (heat-only) ; ses automations actuelles seront alors supprimées.
- Pré-conditionnement basé sur `sensor.temps_de_trajet_jonathan_vers_maison` (Waze) — reporté en v2.
