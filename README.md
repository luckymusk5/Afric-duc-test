# Afric’Éduc — SchoolLogs Passage

MVP du module de suivi des passages d’élèves par badge, avec file hors ligne, API à rôles, anomalies et synthèse assistée.

## Stack retenue

- **Django + Django REST Framework** : API métier et permissions serveur.
- **React + Vite** : interface portail responsive avec file locale persistante.
- **PostgreSQL** : stockage relationnel, accusés de réception de synchronisation et contraintes d’idempotence.
- **RabbitMQ + Celery** : file durable et worker asynchrone pour écrire les passages en base après reconnexion.
- **Docker Compose** : environnement local reproductible avec RabbitMQ Management.

## Démarrage

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Génère BADGE_SECRET_KEY avec la commande ci-dessous, puis démarre les services.
docker compose up --build
```

Avant la première migration des badges, génère `BADGE_SECRET_KEY` comme clé URL-safe de 32 octets et garde-la sauvegardée: sous PowerShell, exécute `[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).TrimEnd('=').Replace('+','-').Replace('/','_')`, puis colle le résultat dans `.env`. Ne régénère pas cette clé tant que les badges chiffrés par l’ancienne valeur sont utilisés.

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
- `POST /api/students/{id}/badges/reissue/` (admin; émet une fois le secret d’enrôlement)
- `GET /api/badges/` et `PATCH /api/badges/{uuid}/state/` (admin; gestion du cycle de vie)
- `POST /api/devices/time-sync/` (agent ou responsable; calibre l’horloge d’un terminal)
- `POST /api/scans/sync/` (agent ou responsable; confirme la publication durable dans RabbitMQ)
- `GET /api/scans/sync/status/?eventIds=uuid,...` (état du traitement jusqu’à l’écriture PostgreSQL)
- `GET /api/scans/`, `GET /api/anomalies/`, `GET /api/outbox/`
- `GET /api/summaries/daily/?class=6e%20A&date=YYYY-MM-DD` (responsable)
- `POST /api/anomalies/close-windows/` (à déclencher par un ordonnanceur)

## Sécurité

Les mots de passe sont hachés par Django (PBKDF2 par défaut). Chaque badge possède un UUID aléatoire sans donnée élève et un secret aléatoire de 256 bits. Le secret est chiffré en base par AES-256-GCM avec un nonce aléatoire et l’UUID comme donnée associée; la clé `BADGE_SECRET_KEY` doit être une clé Base64 URL-safe de 32 octets, conservée hors base. La migration `0003` chiffre les anciens secrets: définir cette même clé avant `migrate`, la sauvegarder et la conserver pour les futures lectures; une perte de clé empêche de valider les badges. Le secret d’enrôlement n’est renvoyé qu’une fois lors de l’émission/remplacement, puis le navigateur élève le conserve localement et ne l’affiche plus. Le QR tournant comprend UUID, compteur de 30 secondes et HMAC-SHA256 tronqué à 128 bits; il ne contient pas l’identité. Le format est volontairement spécifique au projet et utilise SHA-256, tandis que le test RFC 6238 vérifie séparément le vecteur normatif TOTP-SHA1. Les compteurs consommés ont une contrainte unique en base: réutiliser un code pour un autre événement échoue même si deux workers traitent en parallèle. Les réponses de refus des badges sont génériques et les tentatives sont limitées à 60 par terminal et par minute. Les jetons ont une tolérance de compteur d’environ une minute autour de l’heure estimée; une file hors ligne très ancienne peut donc être rejetée. Une horloge monotone du navigateur sert à estimer l’heure après calibration. Après redémarrage/rechargement, le terminal doit se recalibrer en ligne. Les identifiants d’appareil stockés côté navigateur ne constituent pas une identité matérielle inviolable. TLS, sauvegarde/rotation organisée de la clé et enrôlement/authentification matériel des terminaux sont nécessaires avant production.

## Hors ligne et anomalies

Le navigateur agent conserve chaque scan avec UUID dans localStorage tant que l’API ne confirme pas son dépôt dans RabbitMQ. À la reconnexion, Django vérifie le badge et persiste un reçu PostgreSQL avant publication. RabbitMQ utilise une file quorum durable, des messages persistants et les publisher confirms; le worker Celery acquitte après traitement et réessaie les erreurs transitoires. L’interface interroge le statut jusqu’à l’écriture PostgreSQL. La contrainte unique sur `event_id` rend la livraison idempotente. L’heure du scan est construite depuis l’ancre serveur et `performance.now()`; le terminal garde aussi son heure civile déclarée. Une dérive supérieure à cinq minutes est signalée. Un jeton authentique peut encore être copié et relayé en direct avant sa première utilisation: rotation courte et anti-rejeu n’empêchent pas à eux seuls ce relais. Un PIN/NFC ou un protocole défi-réponse avec terminal authentifié serait nécessaire pour le bloquer.

## Synthèse IA

Le backend calcule les compteurs, puis fournit au modèle uniquement ces agrégats. La réponse est contrainte au JSON `{"text":"..."}`, la phrase est bornée et ses chiffres sont comparés aux faits fournis. Une tentative est renouvelée une fois puis le gabarit exact est utilisé. `AI_API_KEY`, `AI_API_URL`, `AI_MODEL` et `AI_MONTHLY_BUDGET` configurent l’intégration. Une synthèse calculée est cachée par empreinte pour éviter une double facturation. Le plafond atteint ou l’indisponibilité du fournisseur est renvoyé avec un motif lisible. Chaque appel et tentative est journalisé dans `AIUsageLog` avec modèle, volume estimé, coût et résultat; le coût estimé et le modèle sont aussi stockés avec la synthèse. Sans clé, le gabarit fonctionne.

## Tests et CI

```bash
docker compose exec backend python manage.py test passage
```

GitHub Actions démarre PostgreSQL et exécute les tests API/métier à chaque push et pull request. Les tests de synchronisation vérifient le statut en file et le maintien du reçu quand la publication échoue.

## Limites à terminer

Voir [DECISIONS.md](DECISIONS.md) pour le raisonnement sécurité détaillé et les limites de la solution.
