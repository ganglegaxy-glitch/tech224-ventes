# TECH224 Ventes — version déployable

Outil de gestion pour commerçants en Guinée : comptes par téléphone + code PIN,
boutiques multiples, produits et stock, caisse (POS), clients, tableau de bord
et rapports 7 / 30 / 90 jours. En français, prix en francs guinéens (GNF).
La base de production démarre **vide** : aucun produit, vente ou client de démonstration.

## Fonctionnement

- Inscription : téléphone + code PIN (le PIN est haché, jamais stocké en clair).
- Essai gratuit de 14 jours, puis formules : Essentiel 79 200 GNF/mois,
  Croissance 199 200 GNF/mois, Entreprise sur devis.
- Paiements en caisse : espèces, Mobile Money, virement, carte.
  Attention : l'outil enregistre le moyen de paiement, il ne débite pas
  réellement Orange Money / Wave — aucun paiement n'est simulé.
- Une vente décrémente le stock automatiquement. Supprimer une vente
  restaure le stock. Le total est toujours calculé côté serveur.
- Chaque commerçant ne voit que ses propres boutiques, produits, ventes
  et clients. Accéder à la boutique d'un autre commerçant renvoie « introuvable ».

## Lancer en local

```bash
cd tech224-ventes-deploy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="une-cle-longue-en-local"
python app.py
```

Ouvrir http://127.0.0.1:5000 — créer un compte, une boutique, un produit,
puis encaisser une vente dans « Caisse ».

## Déployer sur Render (gratuit, étape par étape)

1. Mettre ce dossier dans un dépôt GitHub (privé ou public).
2. Sur https://dashboard.render.com, cliquer **New → Web Service**,
   choisir le dépôt. Render détecte `render.yaml` et le `Procfile`.
3. Vérifier : Build `pip install -r requirements.txt`, Start `gunicorn app:app`.
4. Dans **Environment**, vérifier que `SECRET_KEY` est générée
   (Render la génère depuis `render.yaml`). Ne jamais mettre une clé
   dev / connue en production.
5. Cliquer **Create Web Service**, attendre le déploiement,
   ouvrir l'URL Render fournie (ex. `https://tech224-ventes.onrender.com`).
6. Créer un premier compte commerçant sur cette URL pour vérifier.

> ⚠️ Données persistantes — avertissement important
> Sur l'offre gratuite, le système de fichiers est éphémère : sans disque
> persistant ni base Postgres, les données (comptes, stock, ventes)
> peuvent être perdues à chaque redéploiement ou redémarrage.
> Ce projet inclut un disque persistant dans `render.yaml`
> (`/opt/render/project/src/data`), mais les disques persistants ne sont
> pas toujours disponibles sur l'offre gratuite selon la région et le plan.
> **Pour une vraie mise en production, utilisez Postgres** :
> créer une base Postgres sur Render, puis définir la variable
> `DATABASE_URL` dans le service web. Dans ce cas, ne définissez pas
> `DATABASE_PATH` : Postgres a la priorité dès que `DATABASE_URL`
> commence par `postgres://` ou `postgresql://`.

## Déployer sur un VPS (Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-venv git nginx
git clone <votre-depot> tech224-ventes-deploy && cd tech224-ventes-deploy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# Lancer en arrière-plan (ou via systemd, recommandé) :
gunicorn -b 127.0.0.1:8000 app:app
```

Puis configurer Nginx en proxy inverse vers `127.0.0.1:8000` et activer
HTTPS avec Certbot. Sauvegarder régulièrement le dossier `data/`
(SQLite) ou la base Postgres. Ne jamais exposer le fichier de base
ni le `.env` en téléchargement public.

## Variables d'environnement

| Variable | Rôle |
| --- | --- |
| `SECRET_KEY` | Obligatoire en production, longue et aléatoire |
| `DATABASE_PATH` | Fichier SQLite (défaut : `./data/tech224.db`) |
| `DATABASE_URL` | Si Postgres, ex. `postgresql://...` (prioritaire) |
| `PORT` | Port d'écoute (défaut : 5000) |

## API (pour tests / intégrations)

`POST /api/register`, `POST /api/login` (retourne un token),
`POST /api/shops`, `GET /api/shops`, `POST /api/shops/<id>/products`,
`POST /api/customers`, `POST /api/sales`, `GET /api/sales/<id>`,
`DELETE /api/sales/<id>`, `GET /api/dashboard`, `GET /api/reports?days=7|30|90`.
Authentification : en-tête `Authorization: Bearer <token>`.
