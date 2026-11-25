# DECISIONS

1. **Stack** — Django/DRF, React, PostgreSQL et Docker; écartés: framework full-stack monolithique et SQLite; raisons: séparation API/UI et base multi-utilisateurs reproductible.
2. **Authentification** — JWT 30 min + refresh 8 h avec liste noire; écartées: session navigateur et jeton de longue durée; choix adapté à la SPA avec révocation du refresh.
3. **Code badge** — HMAC tournant chaque minute; écarté: identifiant statique; un code photographié expire, et l’agent doit obtenir un code frais. Limite: l’agent doit avoir synchronisé le code pendant le réseau; vraie résistance hors ligne exige badge cryptographique/NFC ou enrôlement d’appareil.
4. **Synchronisation** — UUID créé côté client et contrainte unique en PostgreSQL; écartée: dédoublonnage par heure/élève qui peut fusionner deux vrais passages.
5. **Horloge erronée** — garder l’heure locale pour audit, signaler >5 min d’écart et utiliser l’heure serveur pour règles/rapports; écarté: rejeter le passage qui priverait l’élève d’une preuve de présence.
6. **Détection du passage manquant** — endpoint idempotent de clôture des fenêtres, prévu pour cron; écarté: calcul lors d’une consultation car l’anomalie doit exister sans ouverture d’écran.
7. **Cinquième anomalie** — horloge incohérente et badge rejoué; retenues car elles couvrent intégrité temporelle et tentative de fraude.
8. **Notifications** — table outbox avec interface API et plafond par parent/jour; écarté: fournisseur SMS intégré directement pour garder l’envoi remplaçable.
9. **IA** — gabarit exact sans API, schéma JSON borné, contrôle des chiffres, cache et budget; écartée: génération libre des nombres par le modèle.
10. **Synchronisation réseau** — file locale `localStorage` garde les scans hors ligne; au retour réseau, PostgreSQL enregistre un reçu puis RabbitMQ quorum durable livre le travail à Celery; UUID stable et contrainte unique assurent l’idempotence. Écarté: publier directement depuis le navigateur dans RabbitMQ, ce qui exposerait le broker et ne fonctionnerait pas hors ligne. À migrer vers IndexedDB si la file locale grandit ou contient des pièces jointes.
