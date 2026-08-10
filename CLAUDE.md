# frilivin-whatsapp-bot — état du projet

Bot WhatsApp auto-hébergé, Docker Compose, traitement métier en Python.
**Ce fichier est le point d'entrée d'une nouvelle session.** Le README est la doc
utilisateur ; ce fichier est la doc du contributeur.

---

## 1. Où ça tourne

| | |
| --- | --- |
| Serveur | Hetzner Cloud, **167.233.137.207**, Ubuntu 24.04 |
| Accès | `ssh bot@167.233.137.207` (clé uniquement, `root` interdit) |
| Répertoire | `~/frilivin-whatsapp-bot` |
| Branche | `claude/whatsapp-bot-docker-compose-9gy7dq` — **développer et pousser ici, jamais ailleurs** |
| Numéro du bot | `33766988586` (compte WhatsApp dédié, session déjà appairée) |
| Numéro opérateur | `33766660673` |

> L'IP figure ici pour éviter de la redemander à chaque session. Si le dépôt est
> public, c'est une information exposée : rien de critique (aucun port ouvert
> hors SSH par clé, pare-feu Hetzner en amont), mais à savoir.

Oracle Cloud a été abandonné : capacité épuisée sur A1.Flex **et** E2.1.Micro, et
les ressources Always Free n'existent que dans la région d'origine du compte —
changer de région ne contourne rien.

## 2. Architecture

```
WhatsApp ─ Baileys ─→ bridge (Node) ─POST /webhook→ api (FastAPI)
                          ↑                              │ dédup SQLite + RQ
                          │ POST /send (202 immédiat)     ↓
                          └──────────────────────── worker (RQ) → pipeline de handlers
```

- **bridge** (`bridge/`, Node 22, `@whiskeysockets/baileys` **6.7.23** — pas la 7.x, encore en RC)
- **api** (`app/whatsapp_bot/api/`, FastAPI) : valide, déduplique sur l'id, enfile, répond 200. Jamais de traitement synchrone.
- **worker** (`app/whatsapp_bot/worker/`, RQ) : exécute la chaîne de handlers.
- **redis** : file RQ + compteurs du rate limiter.

Services et volumes dans `docker-compose.yml` : `wa_session`, `sqlite_data`,
`media_data`, `redis_data`, plus le bind-mount `./sage-data:/data/sage:ro`.

## 3. Invariants à ne pas casser

Chacun a coûté un aller-retour ou un bug ; ils sont tous couverts par des tests.

1. **`ctx.send()` ne bloque jamais.** Le délai humain de 2-8 s est appliqué côté
   bridge, après le `202`. Le worker a 2 s par job.
2. **Le rate limiter consomme un jeton à la *mise en file*, pas à l'envoi**
   (`sending.py`). Un envoi refusé est *abandonné* avec un simple warning. C'est
   pourquoi `BroadcastHandler` plafonne à `RATE_LIMIT_PER_MINUTE - 1` : sans ça
   un gros lot partirait à moitié, silencieusement.
3. **La dédup se fait par `INSERT OR IGNORE`** sur la clé primaire (`db.py`), pas
   par SELECT-puis-INSERT : aucune fenêtre de course.
4. **Le registre ne découvre que `processing/handlers/`.** Un module utilitaire
   posé ailleurs dans `processing/` (ex. `jids.py`) n'est jamais pris pour un
   handler. Un handler dont l'import échoue est loggué sans tuer le worker.
5. **Les messages `fromMe` sont écartés** (`extract.js`) : sinon le bot réagit à
   ses propres envois et boucle.
6. **`run_pipeline` n'estampille `handler` que sur les `Outbound` *retournés*.**
   Un handler qui appelle `ctx.send()` directement doit le renseigner lui-même.
7. **Le watchdog du bridge relance la connexion, il ne fait pas que la
   constater.** `superviseAction` (`wa.js`) lit « pas connecté + aucune
   tentative en vol + aucun timer en attente » comme *chaîne morte* et relance.
   Le drapeau `#connecting` est ce qui empêche une relance de doubler un socket
   en cours de construction : ne pas le retirer.
8. **`/health` du bridge renvoie 503 quand WhatsApp est déconnecté.** Un 200
   portant `degraded` passe pour sain partout où ça compte (Docker, `curl -f`,
   `make health`). Rien ne dépend du bridge en `service_healthy`, donc ce 503
   ne bloque aucun démarrage — vérifié.
9. **Les exports Sage ne sont jamais versionnés** (`sage-data/` gitignoré) : ils
   contiennent 5 441 clients réels avec adresses et numéros de TVA. Les fixtures
   de test sont synthétiques (`app/tests/sage_fixtures.py`).

## 4. Handlers livrés

| Priorité | Handler | Déclencheur | `stop_propagation` |
| --- | --- | --- | --- |
| 5 | `SageImportHandler` | tout `.xlsx` / `.xlsm` reçu | ✅ |
| 10 | `PingHandler` | `!ping` | ✅ |
| 20 | `BroadcastHandler` | `!envoi` + CSV `destinataire; message` | ✅ |
| 50 | `GroupRelayHandler` | mot-clé en message privé → groupes figés | ❌ |
| 1000 | `FallbackLogHandler` | tout ; logue si personne n'a répondu | ❌ |

Priorité croissante = exécuté plus tôt. Ajouter un comportement = déposer un
fichier dans `app/whatsapp_bot/processing/handlers/`, rien à enregistrer.

`GroupRelayHandler` et `BroadcastHandler` se désactivent seuls sans configuration
(pas de cible / pas d'admin) — c'est voulu, pas un bug.

## 5. Les deux fonctions métier

### `!envoi` — diffusion CSV

```
!envoi
33766793050; Bonjour
120363000000000000@g.us; Compte rendu
nord; Message via alias
```

Le **premier** `;` sépare. Destination : numéro (formaté ou non), id de groupe,
JID complet, ou alias (`BROADCAST_ALIASES`, ou table `state` sous
`broadcast:alias:<nom>`, qui gagne). Discrimination dans
`processing/jids.py::resolve_jid` : **plus de 15 chiffres (limite E.164) = groupe**.
Lot trop grand = refus intégral, jamais d'envoi partiel.

### Import Sage 50

Un `.xlsx` envoyé au bot revient en `import_sage_<3 derniers chiffres de la
commande>.txt` (commande `1104999` → `import_sage_999.txt`). Le nom du fichier
envoyé n'a aucune importance ; seul le numéro *à l'intérieur* compte.

- Générateur vendorisé dans `app/whatsapp_bot/sage/generator.py` — **exempté de
  mypy strict et du formateur ruff**, volontairement : il est validé contre de
  vrais imports Sage, et le reformater rendrait illisible toute comparaison avec
  l'original de l'exploitant. Modifications apportées : interface Tkinter
  retirée, `fabriquer()` accepte des index pré-construits.
- `sage/service.py` est la frontière typée devant lui (`generator.pyi` déclare
  les fonctions appelées). Les index clients/articles sont mis en cache, clé =
  chemin + mtime + taille : remplacer un export l'invalide sans redémarrage.
- **Un message porteur d'un fichier reçoit `WORKER_DOCUMENT_JOB_TIMEOUT` (60 s)**
  au lieu de 2 s, décidé dans `api/main.py` sur `msg.has_file`. Mesuré sur les
  vrais fichiers : 528 ms à froid, 226 ms cache chaud.
- Échec → réponse avec la raison, jamais de silence.
- **Client/article introuvable n'interrompt rien** : valeurs de repli (code article
  nettoyé + TVA 20 %, code client vide) et avertissements dans la légende.
- **Le rapprochement client est flou et peut se tromper de compte.** Deux garde-fous
  ajoutés dans `generator.py::score_client` après avoir constaté qu'une fiche Sage
  d'un seul mot obtenait 1.000 sur toute commande contenant ce mot (`« ...QUI N
  EXISTE PAS SARL »` → fiche `PAS`) : dénominateur du recouvrement plancherné à
  `MIN_MOTS_RECOUVREMENT`, et `MOTS_VIDES` écarte les formes juridiques. Vérifié :
  400/400 clients réels se retrouvent eux-mêmes. La légende affiche le code, le nom
  et le score pour rendre un mauvais rattachement visible ; `SAGE_CLIENT_MATCH_THRESHOLD`
  règle l'exigence (le score dépasse 1 quand les bonus CP/ville s'appliquent).

## 6. Commandes

```bash
make check          # tout ce que la CI exécute — à lancer avant chaque commit
make up / down / logs / ps
make health         # état api + redis + bridge
make groups         # JID des groupes dont le bot est membre
make smoke          # injecte un faux !ping
make smoke-broadcast
make db             # 20 derniers messages du ledger
make dlq            # dead-letter queue
make backup         # wa_session + base
```

`make check` = ruff + ruff format + mypy strict + pytest (**195**) + eslint +
`node --test` (**89**). Tout doit rester vert.

`make health` interroge désormais **l'api *et* le bridge**, et sort en erreur si
la connexion WhatsApp est tombée. Avant, il ne curlait que l'api, malgré ce que
promettait cette page.

Attention : `make health` interroge `127.0.0.1`. Depuis un poste local il faut un
tunnel : `ssh -L 8000:127.0.0.1:8000 bot@167.233.137.207`.

## 7. Pièges rencontrés (déjà réglés, à ne pas re-diagnostiquer)

- **`stream:error 515` juste après le scan du QR** : poignée de main normale,
  reconnexion immédiate gérée (`wa.js`, `DisconnectReason.restartRequired`).
- **`Timed Out` sur `fetchProps` / init queries** : bruit Baileys, la connexion
  tient. Vérifier avec `make groups`, pas avec les logs.
- **`PreKeyError` / `No session record` avec `fromMe: true`** : bruit, ces
  messages sont ignorés de toute façon.
- **Adressage `@lid`** : WhatsApp identifie de plus en plus les correspondants par
  LID plutôt que par numéro. Le bridge préfère `key.senderPn` / `participantPn`
  quand le serveur les fournit (`extract.js`), sinon retombe sur le LID. `@lid`
  est une famille d'adresses reconnue dans `jids.py`, donc un admin peut être
  déclaré par LID si aucun numéro n'est transmis — le log `broadcast_refused`
  affiche la valeur exacte à recopier.
- **Citation d'un message** : Baileys exige l'objet complet, pas un id. Le bridge
  garde un LRU id → clé (`quoted-cache.js`) ; id inconnu = envoi sans citation.
- **« Le fichier n'a pas pu être téléchargé » alors que WhatsApp va bien** : le
  volume `media_data` est monté sur `/media` en `root:root`, or le bridge tourne
  en uid 1000 et l'api/worker en uid 10001 — aucun ne pouvait créer son
  sous-dossier. Le service `media-init` du `docker-compose.yml` pose désormais
  `/media/in` (bridge) et `/media/out` (worker) avant le démarrage. Le message
  d'erreur accuse WhatsApp, la cause est locale : vérifier `media_download_failed`
  dans les logs du bridge, qui porte l'`EACCES` réel.
- **Pièce jointe reçue en `.bin`** : WhatsApp nomme le fichier d'après son
  *mimetype*, pas d'après `fileName`. Tout ce qui part en
  `application/octet-stream` arrive en `.bin`, inouvrable. `mimetypeFor()`
  (`wa.js`) déduit le type de l'extension ; une extension absente de la table
  retombe sur octet-stream et reproduira le symptôme.
- **Bot muet alors que `docker compose ps` affiche tout en `healthy`** : arrivé
  le 2026-07-28, découvert **12 jours plus tard**. WhatsApp a refusé le socket
  (`503` puis `405` en rafale), le bridge a retenté 5 fois, puis la chaîne de
  reconnexion s'est arrêtée sans un mot — ni `wa_reconnecting`, ni erreur, ni
  `wa_logged_out`. Le process est resté vivant et totalement inerte : aucun
  socket sortant, threadpool libuv au repos. Deux choses ont été corrigées :
  le watchdog relance maintenant la chaîne (§3.7) et `/health` renvoie 503
  (§3.8). **Ne pas croire le healthcheck Docker seul** : `restart:
  unless-stopped` ne réagit qu'à la *sortie du process*, jamais à l'état
  `unhealthy`. C'est pourquoi le filet de sécurité est une sortie volontaire
  (`BRIDGE_DISCONNECT_EXIT_MS`, 15 min) et pas un simple 503.
- **La cause exacte de cette panne est un bug de Baileys 6.7.23**, reproduit et
  corrigé côté bridge. `ws` n'émet `error` sur un upgrade refusé *que si*
  personne n'écoute `unexpected-response`. Or `Socket/Client/websocket.js`
  réémet cet événement — ce qui supprime l'`error` — et `Socket/socket.js` ne
  câble `end()` que sur `error` et `close`. Un 405 de WhatsApp n'atterrit donc
  **nulle part** : socket bloqué en `CONNECTING`, plus aucun
  `connection.update`, chaîne de reconnexion morte. `handshakeTimeout` ne sert à
  rien ici : le serveur a répondu, simplement 405. `onUpgradeRefused` (`wa.js`)
  écoute l'événement orphelin et appelle `end()` nous-mêmes. **Ne pas retirer
  cet écouteur en montant de version sans revérifier** que le bug amont est
  corrigé — reproduction : un serveur local qui répond 405 à l'upgrade.
- **Deux défenses, pas une** : `onUpgradeRefused` traite la cause connue et
  rend la main en quelques millisecondes ; le superviseur (§3.7) rattrape en
  45 s *n'importe quelle* autre façon dont la chaîne pourrait mourir. Garder les
  deux : la seconde ne suppose rien de la cause.
- **Exports Sage intervertis** : `sage-data/clients.txt` contenait en fait
  l'export *articles* (déposé sous le mauvais nom). L'import échoue alors sur la
  résolution des clients. Contrôle rapide : l'en-tête de `clients.txt` commence
  par `Code<TAB>Nom<TAB>Société`, celui d'`articles.txt` par
  `Code<TAB>Désignation courte`.

## 8. Reste à faire

- Pas de purge des pièces jointes : `MEDIA_RETENTION_DAYS` existe dans la config
  mais rien ne l'applique encore.
- Aucune PR ouverte, par choix de l'utilisateur. Push direct sur la branche.
- `BROADCAST_ADMIN_JIDS` et `SAGE_ADMIN_JIDS` sont à renseigner dans le `.env`
  **du serveur** ; sans le premier, `!envoi` reste désactivé.

## 9. Conventions

- **Documentation et messages destinés à l'humain en français ; code,
  identifiants, docstrings, commentaires et logs en anglais.**
- Logs JSON structurés (structlog côté Python, pino côté Node).
- Messages de commit : sujet impératif, corps expliquant le *pourquoi*.
- Ne pas ouvrir de PR sans demande explicite.
