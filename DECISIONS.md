# Décisions techniques — Passage hors ligne

## File locale avant envoi

Chaque scan est écrit dans IndexedDB et la transaction est terminée avant toute requête réseau. Un envoi direct sans file pourrait perdre un passage si l’onglet ou le réseau tombe juste après la lecture.

## Identifiant d’événement et déduplication

Chaque lecture crée un UUID d’événement; IndexedDB ignore aussi un QR déjà en attente. La base reste l’autorité grâce à l’unicité `(device_id, event_id)` : une suppression locale interrompue provoque un accusé `duplicate` lors du renvoi, sans seconde ligne.

## Accusés et suppression locale

Le serveur renvoie les accusés après la transaction de base de données. Le navigateur ne supprime que les événements explicitement acquittés avec `created` ou `duplicate`; un événement sans accusé reste en file.

## Journal de présence

Le journal est reconstruit depuis les passages acceptés, qui restent la source de vérité. L’insertion résistante au conflit garantit une seule ligne par élève et jour; les heures sont rapprochées avec `LEAST` pour l’entrée et `GREATEST` pour la sortie, car une synchronisation hors ligne peut arriver dans le désordre.

## Point et direction du passage

Le terminal authentifié fournit le point de contrôle via son affectation historisée. Le client ne décide ni du point ni de la direction : le serveur choisit l’affectation valide à l’heure estimée, puis applique les fenêtres et l’état d’entrée/sortie de l’élève.

## Horloge et QR tournant

Le QR change toutes les 15 secondes, conformément au choix produit retenu. L’heure estimée corrigée sert aux fenêtres et au journal; un écart important est marqué et signalé comme anomalie au lieu de rejeter automatiquement un passage.

## Service Worker natif

Le Service Worker est écrit à la main et ne met en cache que la coquille de l’écran. Les requêtes API, les données personnelles et les réponses de synchronisation ne sont jamais mises en cache; IndexedDB, hors du worker, porte la file durable.

## Pas de broker pour la synchronisation

La synchronisation des passages est traitée dans une transaction Django/PostgreSQL avec une unicité en base. Ajouter RabbitMQ, Redis ou Celery introduirait un autre point de panne sans contribuer à l’idempotence requise pour ce flux.
