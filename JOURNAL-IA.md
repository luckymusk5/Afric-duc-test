# JOURNAL IA

## Outils utilisés

- ChatGPT/Codex : analyse du sujet, architecture initiale et génération assistée du squelette Django/React, des modèles et des premiers tests.
- À compléter pendant le travail : outils et usages pour la production de la vidéo de communication (voix, sous-titres, montage).

## Refus et corrections

1. Le premier brouillon proposait un serveur Python standard avec SQLite. Je l’ai abandonné après clarification du choix attendu : Django, React, PostgreSQL et Docker. Ce prototype aurait divergé de la stack demandée et manquait de migrations/API robustes.
2. Le sujet autorise à considérer une simple signature de badge comme une protection. J’ai documenté que le QR HMAC seul ne résout pas la copie hors ligne; un code frais peut être rejoué avant la première utilisation. La version de démonstration est limitée et le vrai scénario requiert un appareil enrôlé et/ou une preuve cryptographique matérielle.
3. Les nombres de la synthèse ne sont jamais confiés au modèle : ils viennent de requêtes ORM, et le texte est rejeté si un chiffre non fourni apparaît. Les tentatives sont journalisées séparément, avec un plafond mensuel estimé.

## À renseigner à la livraison

Ajouter les prompts importants, les modifications manuelles, les résultats des vérifications et l’outil IA utilisé pour la capsule vidéo.

4. Pour les scans hors ligne, RabbitMQ n’est utilisé qu’après le retour de la connexion : le navigateur ne peut pas joindre un broker distant hors réseau. L’UUID reste stable et le serveur stocke un reçu avant publication afin de pouvoir reprendre sans perdre le scan.
