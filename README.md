# Afric’Éduc — SchoolLogs Passage

MVP du module de suivi des passages d’élèves par badge, avec file hors ligne, API à rôles, anomalies et synthèse assistée.

## Stack retenue

- **Django + Django REST Framework** : API métier et permissions serveur.
- **React + Vite** : interface portail responsive avec file locale persistante.
- **PostgreSQL** : stockage relationnel, accusés de réception de synchronisation et contraintes d’idempotence.
- **RabbitMQ + Celery** : file durable et worker asynchrone pour écrire les passages en base après reconnexion.
- **Docker Compose** : environnement local reproductible avec RabbitMQ Management.

## Démarrage

```bash
Copy-Item .env.example .env
docker compose up --build
```

L’application web est sur http://localhost:5173, l’API sur http://localhost:8000/api et RabbitMQ Management sur http://localhost:15672. Les migrations versionnées dans le dépôt sont appliquées au démarrage, puis un jeu de données local est ajouté; le worker Celery consomme la file durable `scan_sync`. Les identifiants Management sont ceux de `RABBITMQ_DEFAULT_USER` et `RABBITMQ_DEFAULT_PASS` dans `.env`. Les évolutions doivent ensuite être livrées comme migrations versionnées.

Comptes de démonstration (mot de passe identique à l’identifiant de rôle + `Pass123!`):

- `admin@afric-educ.test` / `AdminPass123!`
- `agent@afric-educ.test` / `AgentPass123!`
- `parent1@afric-educ.test` / `ParentPass123!`
- `parent2@afric-educ.test` / `ParentPass123!`

## Lancement du backend sans Docker (Windows CMD)

Le fichier `.env` à la racine est chargé automatiquement par Django. Renseigne-y tes paramètres PostgreSQL locaux, puis démarre PostgreSQL. Dans `backend` :

```cmd
python manage.py makemigrations passage
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

`POSTGRES_HOST=localhost` est utilisé pour ce lancement direct. Avec Docker Compose, le conteneur backend rejoint PostgreSQL via le nom de service `db`.

## API principale

- `POST /api/auth/login/`, `POST /api/auth/refresh/`, `POST /api/auth/logout/`
- `GET /api/me/`
- `GET /api/students/` (admin et parent; portée filtrée côté serveur)
- `GET /api/students/{id}/badge-token/` (admin)
- `POST /api/scans/sync/` (agent ou responsable; confirme la publication durable dans RabbitMQ)
- `GET /api/scans/sync/status/?eventIds=uuid,...` (état du traitement jusqu’à l’écriture PostgreSQL)
- `GET /api/scans/`, `GET /api/anomalies/`, `GET /api/outbox/`
- `GET /api/summaries/daily/?class=6e%20A&date=YYYY-MM-DD` (responsable)
- `POST /api/anomalies/close-windows/` (à déclencher par un ordonnanceur)

## Sécurité

Les mots de passe sont hachés par Django (PBKDF2 par défaut) avec validation de longueur minimale de huit caractères. L’authentification JWT limite l’accès à 30 minutes, le renouvellement à huit heures; la déconnexion révoque immédiatement le jeton d’accès courant et met le jeton de renouvellement en liste noire. La route de connexion a une limite de cinq tentatives par minute et conserve l’erreur générique du mécanisme JWT. Les permissions et le cloisonnement parent/enfant sont appliqués dans les querysets/API. Les paramètres sont validés par DRF et les requêtes passent par l’ORM. React rend les champs comme texte échappé. Les secrets sont configurables par variables d’environnement; `.env.example` ne contient que des exemples. Les codes badge sont signés HMAC, expirent rapidement et ne peuvent être consommés qu’une fois. Pour la production, utiliser HTTPS, clés séparées et renouvelables, appareils enrôlés et authentification renforcée.

## Hors ligne et anomalies

Le navigateur conserve chaque scan avec UUID dans localStorage tant que l’API ne confirme pas son dépôt dans RabbitMQ. À la reconnexion, Django vérifie le badge et persiste un reçu dans PostgreSQL avant de publier le message. RabbitMQ utilise une file quorum durable, des messages persistants et les publisher confirms; le worker Celery acquitte après traitement et réessaie les erreurs transitoires. L’interface interroge le statut jusqu’à l’écriture PostgreSQL. La contrainte unique sur `event_id` rend le traitement idempotent même après une livraison répétée. Si le broker est inaccessible, l’API retourne une erreur et l’élément reste dans la file du navigateur. Le serveur garde l’heure locale déclarée, l’heure serveur et le terminal; une dérive supérieure à cinq minutes est signalée, l’heure serveur restant l’heure de référence. Les anomalies incluent passage manquant, hors fenêtre, point inattendu, double scan rapide, badge rejoué et horloge incohérente. `close-windows` doit être appelé régulièrement par un ordonnanceur externe pour détecter les absences sans visite de l’interface. Les messages restent dans une boîte sortante avec plafond quotidien configurable.

## Synthèse IA

Le backend calcule les compteurs, puis fournit au modèle uniquement ces agrégats. La réponse est contrainte au JSON `{"text":"..."}`, la phrase est bornée et ses chiffres sont comparés aux faits fournis. Une tentative est renouvelée une fois puis le gabarit exact est utilisé. `AI_API_KEY`, `AI_API_URL`, `AI_MODEL` et `AI_MONTHLY_BUDGET` configurent l’intégration. Une synthèse calculée est cachée par empreinte pour éviter une double facturation. Le plafond atteint ou l’indisponibilité du fournisseur est renvoyé avec un motif lisible. Chaque appel et tentative est journalisé dans `AIUsageLog` avec modèle, volume estimé, coût et résultat; le coût estimé et le modèle sont aussi stockés avec la synthèse. Sans clé, le gabarit fonctionne.

## Tests et CI

```bash
docker compose exec backend python manage.py test passage
```

GitHub Actions démarre PostgreSQL et exécute les tests API/métier à chaque push et pull request. Les tests de synchronisation vérifient le statut en file et le maintien du reçu quand la publication échoue.

## Limites à terminer

Ce dépôt constitue un MVP et non un déploiement complet. Le code QR actuel est un code tournant à courte durée de vie; la synchronisation longue hors ligne et la résistance au partage de photo nécessitent un protocole d’appareil enrôlé (défi signé à usage unique / NFC), à valider avant usage réel. La page agent permet d’entrer le code mais n’intègre pas encore la caméra. Les exceptions de calendrier, la gestion UI du cycle de vie de badge et la tâche planifiée système restent à compléter. Les vidéos de démonstration et de communication doivent être produites après lancement de l’application.
