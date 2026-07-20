# Gestionnaire de catalogue — application desktop

Outil desktop pour les vendeurs : récupérer leurs produits via l'API REST et préparer l'import vers le catalogue cible.

## Prérequis

- Python 3.11+
- macOS / Windows / Linux

## Installation

```bash
cd catalog-desktop
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Lancement

```bash
source .venv/bin/activate
python main.py
```

## Utilisation

1. **Connexion PFS** — email et mot de passe du compte vendeur 
2. **Connexion EFashion** — email et mot de passe du compte vendeur
3. **Liste produits** — `listProducts` + `listVariants` + détail `/products/{id}`
4. **Exporter JSON** — sauvegarde les produits enrichis + réponses API brutes

## Fonctionnement technique

- **Login PFS** : OAuth REST (`parisfashionshops.com/fr/loginform` → `client.parisfashionshops.com/api/v1/oauth/login`)
- **Produits PFS** : `listProducts` + `listVariants` (poids, paquets, type ITEM/PACK) puis `/products/{id}` (compositions, collection…)
- **EFashion** : GraphQL `login` sur `wapi.efashion-paris.com/graphql`
- **Pas de Playwright** — login OAuth REST + PySide6 uniquement

La session est enregistrée dans `.session/session.json` (dev) ou Application Support (exécutable).

## Créer un exécutable

```bash
source .venv/bin/activate
pip install -r requirements-build.txt
python scripts/build.py
```

Résultat macOS : `dist/GestionnaireCatalogue.app`

## Structure

```
catalog-desktop/
├── main.py
├── requirements.txt
├── scripts/build.py
└── catalog_import/
    ├── auth.py              # Login  (OAuth)
    ├── efashion_auth.py     # Placeholder EFashion
    ├── pfs_client.py        # Client API listProducts + products/{id}
    ├── product_service.py   # Fetch + export JSON
    ├── session_store.py
    ├── config.py
    └── gui.py
```
