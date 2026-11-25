# Décisions — badge QR tournant

## Menaces traitées

Le protocole vise la copie d’un QR statique, la modification d’un jeton, le rejeu d’un passage, la lecture des secrets depuis la base, les doubles traitements concurrents et les dérives d’horloge accidentelles. Il ne peut pas, à lui seul, empêcher un relais en direct d’un QR authentique avant son premier usage ni protéger un téléphone compromis.

## Choix du format

Le jeton est `badge_uuid.counter.signature`, où le compteur est le nombre de périodes UTC de 30 secondes depuis l’époque Unix. La signature est HMAC-SHA256 du texte `uuid.compteur`, tronquée à 16 octets puis encodée en Base64 URL-safe sans remplissage; ce format de projet est inspiré de TOTP mais n’est pas l’encodage RFC 6238.

Le secret aléatoire a 256 bits; l’identifiant public est un UUID aléatoire indépendant et ne contient aucune information de l’élève. Le navigateur du badge calcule les jetons avec Web Crypto, tandis que l’agent ne reçoit que le jeton tournant.

## Chiffrement et stockage

Le secret est chiffré avec AES-256-GCM, un nonce aléatoire neuf de 96 bits et l’UUID du badge en donnée associée. `BADGE_SECRET_KEY` est une clé de chiffrement dédiée de 32 octets encodée en Base64 URL-safe; une rotation de cette clé nécessite une migration de rechiffrement planifiée, car une perte de clé rend les badges inutilisables.

Le secret d’enrôlement est renvoyé une seule fois par l’action d’émission et n’est pas inclus dans le sérialiseur ni dans l’interface d’administration. Le navigateur élève le garde dans son stockage local afin qu’il survive au rafraîchissement; le verrouillage du téléphone et une distribution privée du QR restent nécessaires.

La migration `0003` assigne un UUID à chaque ancien badge, décode son secret hexadécimal historique de 32 octets, le chiffre et supprime ensuite la colonne en clair. Elle est irréversible sans la clé historique pour l’opération inverse; sauvegarder la clé et la base avant la migration.

## Anti-rejeu et idempotence

La validation vérifie état actif, HMAC constant-time et compteur correspondant à l’heure estimée avec tolérance d’une période de part et d’autre. Une contrainte SQL unique `(badge, counter)` n’autorise ensuite qu’une seule consommation du jeton, y compris lorsque plusieurs workers tentent de traiter la même valeur.

`event_id` unique protège l’idempotence de la file et `BadgeUsage` protège l’usage du code; ce sont deux protections distinctes. Les erreurs de badge sont présentées sous un message générique afin de ne pas révéler si un identifiant existe, si le badge est suspendu ou si la signature échoue.

## Temps et mode hors ligne

Après calibration en ligne, l’agent avance l’heure estimée avec `performance.now()`, une horloge monotone qui ne saute pas lors d’une correction de l’heure système. Le terminal recalcule la calibration après rechargement ou redémarrage, et le backend signale un décalage de plus de cinq minutes entre temps civil local et estimation.

Une file locale conserve l’événement et son jeton jusqu’à l’accusé de réception de l’API; le scan attend aussi une validation finale du worker après dépôt RabbitMQ. Comme le jeton reste rattaché à un compteur de 30 secondes, les événements différés pendant une longue coupure peuvent sortir de la fenêtre acceptée et être refusés à la reprise; accepter des scans arbitrairement anciens affaiblirait le contrôle de fraîcheur.

Le compteur unique bloque un deuxième usage même si un code a été photographié, mais ne prouve pas que le téléphone était physiquement devant l’agent. Pour contrer le relais en direct, ajouter un terminal enrôlé avec clé matériellement protégée et défi aléatoire signé, ou utiliser NFC/présence locale.

## Limitation et tests

Le backend limite à 60 tentatives par terminal et par minute. Cette limite réduit le balayage d’identifiants et les erreurs répétées; un `deviceId` navigateur n’est pas une identité fiable face à un agent malveillant, donc la production devra enrôler les appareils côté serveur.

Les tests couvrent jeton valide, rejeu, jeton trop ancien, signature altérée, badges suspendu/perdu/révoqué, remplacement, absence de données personnelles dans le jeton, vecteur RFC 6238 Appendix B, horloge locale décalée de deux heures après calibration et chiffrement au repos. Le test RFC est un oracle de calcul TOTP SHA-1 indépendant et ne signifie pas que le format applicatif est interopérable avec les applications TOTP.
