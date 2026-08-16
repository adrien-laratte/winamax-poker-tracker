#!/bin/bash
# Déploiement : récupère le code, reconstruit l'image, redémarre.
# Le code vient de git ; la configuration et la base, elles, restent sur le NAS
# et ne sont jamais touchées par un pull.
set -e
cd /volume1/docker/poker-tracker

# La config de prod n'est pas dans le dépôt. Sans elle, compose démarrerait
# avec des variables vides : Postgres créerait une base neuve, et le watcher
# n'aurait ni dossier à surveiller ni dossier où écrire.
if [ ! -f .env ]; then
    echo "Abandon : .env manquant. Le modèle est env.example (cp env.example .env)."
    exit 1
fi

# Où vit la base, ./pgdata par défaut. Un dossier de données PostgreSQL porte
# toujours un PG_VERSION : son absence veut dire base vide.
data=$(grep -E '^PGDATA_DIR=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
data=${data:-./pgdata}

# Démarrer sur un dossier vide initialise une base neuve — ce qui est juste au
# tout premier lancement, et une catastrophe silencieuse après un déménagement
# du projet : l'historique reste dans l'ancien pgdata pendant que le tracker
# repart de zéro. Le cas légitime doit donc être demandé explicitement.
if [ ! -f "$data/PG_VERSION" ] && [ "$1" != "--init-db" ]; then
    echo "Aucune base dans $data."
    echo "  • déménagement : conteneurs arrêtés, copier l'ancienne avec"
    echo "      sudo cp -a /chemin/ancien/pgdata $data"
    echo "  • premier démarrage : relancer avec --init-db"
    exit 1
fi

git pull --ff-only
docker compose up -d --build
docker image prune -f
