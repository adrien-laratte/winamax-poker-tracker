#!/bin/bash
# Déploiement : récupère le code, reconstruit l'image, redémarre.
# Le code vient de git ; la configuration et la base, elles, restent sur le NAS
# et ne sont jamais touchées par un pull.
set -e
cd /volume1/docker/winamax-poker-tracker

# Le nom de projet, épinglé plutôt que déduit du nom du dossier. C'est lui qui
# fait le lien avec le projet de Container Manager : à nom identique, l'interface
# et ce script pilotent les mêmes conteneurs. Sous un autre nom, compose voudrait
# en créer un second jeu et buterait sur les container_name déjà pris.
project=winamax-poker-tracker

# La config de prod n'est pas dans le dépôt. Sans elle, compose démarrerait
# avec des variables vides : Postgres créerait une base neuve, et le watcher
# n'aurait ni dossier à surveiller ni dossier où écrire.
if [ ! -f .env ]; then
    echo "Abandon : .env manquant. Le modèle est env.example (cp env.example .env)."
    exit 1
fi

# Où vit la base, ./pgdata par défaut. La valeur est débarrassée des retours
# chariot et des espaces de fin : un .env passé par Windows ou par l'éditeur de
# DSM en sème, et un chemin terminé par un \r invisible ne désigne rien.
data=$(grep -E '^[[:space:]]*PGDATA_DIR=' .env | tail -1 | cut -d= -f2- \
       | tr -d '\r"'\''' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
data=${data:-./pgdata}

# Démarrer sur un dossier vide initialise une base neuve — ce qui est juste au
# tout premier lancement, et une catastrophe silencieuse après un déménagement
# du projet : l'historique reste dans l'ancien pgdata pendant que le tracker
# repart de zéro. Le cas légitime doit donc être demandé explicitement.
#
# Un dossier de données PostgreSQL porte toujours un PG_VERSION, mais il
# appartient à l'utilisateur postgres du conteneur (uid 999) en 0700 : hors
# root, on ne peut pas regarder dedans. Ne pas voir le fichier ne veut donc pas
# dire qu'il n'y est pas — d'où le deuxième test, qui distingue « dossier
# absent » de « dossier fermé à clé ».
if [ -f "$data/PG_VERSION" ]; then
    :
elif [ -d "$data" ] && [ ! -r "$data" ]; then
    echo "Base présente dans $data — illisible par $(id -un), c'est normal :"
    echo "elle appartient à postgres. Relancer avec sudo pour lever le doute."
elif [ "$1" != "--init-db" ]; then
    echo "Aucune base dans $data  (depuis $(pwd), en tant que $(id -un))"
    echo "  • déménagement : conteneurs arrêtés, copier l'ancienne avec"
    echo "      sudo cp -a /chemin/ancien/pgdata $data"
    echo "  • premier démarrage : relancer avec --init-db"
    exit 1
fi

# git en tant que propriétaire du clone, docker en root : l'inverse fâche tout
# le monde. Un « sudo git pull » ferait apparaître des fichiers root dans une
# copie de travail qui appartient à l'utilisateur, et git refuse de son côté de
# travailler sur un dépôt dont le propriétaire n'est pas l'appelant. Si le
# script est lancé par le planificateur de tâches, déjà en root, les sudo
# ci-dessous ne coûtent rien.
git pull --ff-only
sudo docker compose -p "$project" up -d --build
sudo docker image prune -f

echo
echo "Conteneurs du projet $project :"
sudo docker compose -p "$project" ps --format '  {{.Name}}\t{{.Status}}'
