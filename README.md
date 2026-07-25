# frilivin-whatsapp-bot

Bot WhatsApp **auto-hébergé**, **gratuit**, en **Docker Compose**, conçu pour tourner
sur une instance **Ubuntu ARM64** (Oracle Cloud Free Tier, Ampere A1).

Un compte WhatsApp dédié reçoit les messages, un pipeline de handlers Python décide
quoi en faire, et le bot répond ou relaie vers des groupes.

- **~300 messages entrants/jour**, traitement **< 2 s** par message
- **Aucun service payant** : pas d'API WhatsApp Business, pas de cloud managé
- **Extensible** : ajouter un comportement = déposer un fichier dans `processing/handlers/`

---

## Table des matières

1. [Architecture](#architecture)
2. [Arborescence](#arborescence)
3. [Déploiement sur Oracle Cloud ARM64](#déploiement-sur-oracle-cloud-arm64)
4. [Scanner le QR code](#scanner-le-qr-code)
5. [Trouver les JID des groupes](#trouver-les-jid-des-groupes)
6. [Diffuser une liste avec `!envoi`](#diffuser-une-liste-avec-envoi)
7. [Ajouter un comportement](#ajouter-un-comportement)
8. [Configuration](#configuration)
9. [Exploitation](#exploitation)
10. [Développement et tests](#développement-et-tests)
11. [Dépannage](#dépannage)
12. [Limites connues](#limites-connues)

---

## Architecture

Quatre conteneurs, un seul réseau Docker privé. Rien n'est exposé publiquement.

```mermaid
flowchart LR
    WA([WhatsApp]) <-->|websocket| B[bridge<br/>Node + Baileys]
    B -->|POST /webhook| A[api<br/>FastAPI]
    A -->|RQ| R[(redis)]
    R --> W[worker<br/>pipeline de handlers]
    W -->|POST /send| B
    A <--> DB[(SQLite)]
    W <--> DB
```

**Chemin d'un message entrant**

1. `bridge` reçoit le message, en extrait `{id, from, chat_jid, is_group, timestamp, type,
   text, quoted_id}` et le POST à `api:8000/webhook`.
2. `api` valide, **déduplique sur `id`** (clé primaire SQLite), enfile dans Redis et
   répond `200` immédiatement. Aucun traitement synchrone, jamais.
3. `worker` dépile et exécute la chaîne de handlers, triée par `priority`, en s'arrêtant
   si l'un d'eux a `stop_propagation = True`.
4. Un handler qui veut répondre renvoie des `Outbound` ; le worker les passe au limiteur
   de débit, les écrit en base, puis appelle `bridge POST /send`.

**Chemin d'un message sortant**

`POST /send` répond **`202` immédiatement** et empile le message dans une file persistée
sur disque. C'est le bridge qui applique ensuite le délai aléatoire de **2 à 8 secondes**
avant l'envoi réel, puis rappelle l'api pour inscrire le statut final en base.

> Pourquoi ce découpage : le worker a un budget de **2 s par message**. S'il attendait le
> délai anti-spam, chaque envoi ferait exploser ce budget. Le délai appartient au
> transport, pas au traitement.

---

## Arborescence

```
.
├── docker-compose.yml          # redis + api + worker + bridge
├── .env.example                # toute la configuration, commentée
├── Makefile                    # raccourcis d'exploitation (make help)
├── bridge/                     # Node.js — transport WhatsApp
│   └── src/
│       ├── index.js            # démarrage et câblage
│       ├── wa.js               # socket Baileys, QR, reconnexion
│       ├── extract.js          # message WhatsApp → payload webhook (pur, testé)
│       ├── outbox.js           # file d'envoi persistée + délai 2-8 s
│       ├── quoted-cache.js     # LRU id → message, pour citer une réponse
│       ├── http.js             # /send /groups /health
│       └── api-client.js       # appels vers l'api, avec tampon de secours
└── app/                        # Python — api et worker (même image)
    ├── whatsapp_bot/
    │   ├── config.py           # réglages (pydantic-settings)
    │   ├── db.py               # schéma SQLite, déduplication, journal
    │   ├── ratelimit.py        # 500/jour et 30/minute, atomique (Lua)
    │   ├── sending.py          # LiveSender : l'unique chemin de sortie
    │   ├── api/main.py         # POST /webhook, GET /health, GET /groups
    │   ├── worker/
    │   │   ├── main.py         # boucle RQ + dead-letter queue
    │   │   └── tasks.py        # le job et l'exécution du pipeline
    │   └── processing/
    │       ├── base.py         # ABC Handler + Context
    │       ├── registry.py     # découverte automatique, tri par priority
    │       └── handlers/       # ← vos comportements vont ici
    └── tests/                  # pytest
```

---

## Déploiement sur Oracle Cloud ARM64

### 1. Créer l'instance

Dans la console Oracle Cloud : **Compute ▸ Instances ▸ Create instance**.

| Réglage | Valeur |
| --- | --- |
| Image | Canonical **Ubuntu 24.04** |
| Shape | **VM.Standard.A1.Flex** (Ampere, ARM64) |
| OCPU / RAM | 1 OCPU / 6 Go suffisent largement ; jusqu'à 4/24 en Always Free |
| Boot volume | 50 Go |
| Clé SSH | ajoutez votre clé publique |

> **Always Free** : la shape A1.Flex est gratuite dans la limite de 4 OCPU et 24 Go
> cumulés sur le tenancy. Vérifiez la mention « Always Free eligible » avant de valider.

### 2. Se connecter

```bash
ssh ubuntu@<IP_PUBLIQUE>
```

> **Aucun port à ouvrir.** Les services n'écoutent que sur `127.0.0.1` : ni la Security
> List Oracle ni `iptables` n'ont besoin d'être modifiés. Pour interroger l'api depuis
> votre poste, passez par un tunnel SSH (voir [Exploitation](#exploitation)).

### 3. Installer Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # ou déconnexion/reconnexion
docker --version && docker compose version
```

Le script officiel gère nativement `linux/arm64`.

### 4. Récupérer le projet et le configurer

```bash
git clone https://github.com/JunyiLi0/frilivin-whatsapp-bot.git
cd frilivin-whatsapp-bot
cp .env.example .env
```

Générez le secret partagé entre les services et inscrivez-le dans `.env` :

```bash
sed -i "s/^BOT_TOKEN=.*/BOT_TOKEN=$(openssl rand -hex 32)/" .env
```

Relisez `.env` : chaque variable y est commentée.

### 5. Construire et démarrer

```bash
docker compose build      # ~3-5 min sur 1 OCPU, la première fois
docker compose up -d
docker compose ps
```

---

## Scanner le QR code

Au tout premier démarrage, le bridge n'a pas de session : il affiche un QR code
dans ses logs.

```bash
docker compose logs -f bridge
```

Sur le **téléphone du numéro dédié** :

**WhatsApp ▸ Réglages ▸ Appareils liés ▸ Lier un appareil**, puis scannez le QR affiché
dans le terminal.

Le QR **expire au bout d'une vingtaine de secondes** ; s'il disparaît, un nouveau est
généré automatiquement — gardez simplement les logs ouverts.

Une fois lié :

```json
{"level":"info","timestamp":"...","service":"bridge","event":"wa_connected","user":"33612345678:1@s.whatsapp.net"}
```

La session est enregistrée dans le volume `wa_session` : **le scan n'est à faire
qu'une fois**, même après un redémarrage ou une mise à jour.

Vérifiez ensuite l'état général :

```bash
make health
```

```json
{"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
```

Envoyez enfin `!ping` au numéro du bot depuis un autre téléphone : vous recevez
`pong 🏓` après 2 à 8 secondes.

---

## Trouver les JID des groupes

Un JID de groupe ressemble à `120363000000000000@g.us`. Le bot ne peut écrire que dans
les groupes dont **le numéro dédié est membre**.

```bash
make groups
```

```json
{
  "count": 2,
  "groups": [
    {"jid": "120363000000000000@g.us", "subject": "Alertes techniques", "participants": 12}
  ]
}
```

Reportez le ou les JID voulus dans `.env` :

```dotenv
RELAY_KEYWORDS=urgent,alerte
RELAY_TARGET_JIDS=120363000000000000@g.us
```

puis `docker compose up -d worker` pour recharger.

---

## Diffuser une liste avec `!envoi`

Le comportement principal du bot : **un destinataire par ligne, chacun avec son propre
message**. Vous écrivez au numéro dédié, depuis votre téléphone habituel :

```
!envoi
33766793050; Bonjour, la réunion est déplacée à 15 h
33784828374; Peux-tu confirmer ta présence ?
120363000000000000@g.us; Compte rendu envoyé par mail
nord; Message pour l'alias « nord »
```

Le bot répond en citant votre demande :

```
📤 4 message(s) en file d'envoi :
  ✅ +33766793050
  ✅ +33784828374
  ✅ 120363000000000000
  ✅ 120363000000000042

⏳ Chaque message part avec 2-8 s d'écart.
```

### Activer la commande

**Sans allowlist, le handler est désactivé** : renseignez qui a le droit de diffuser,
sinon n'importe quel inconnu écrivant au numéro disposerait d'un relais de diffusion.

```dotenv
BROADCAST_ADMIN_JIDS=33612345678,33698765432
BROADCAST_ALIASES=nord=120363000000000000@g.us,sud=120363000000000042@g.us
```

puis `docker compose up -d worker`. Un expéditeur non autorisé n'obtient **aucune
réponse** : la tentative est seulement journalisée (`broadcast_refused`).

### Format accepté

| Écriture du destinataire | Résultat |
| --- | --- |
| `33766793050`, `+33 7 66 79 30 50`, `07-66-79-30-50` | message privé |
| `120363000000000000` ou `120363000000000000@g.us` | groupe |
| `nord` | alias, résolu via `BROADCAST_ALIASES` ou la table `state` |

- Le séparateur est le **premier** `;` de la ligne : le message peut en contenir d'autres.
- Ligne vide ignorée, ligne commençant par `#` traitée comme un commentaire.
- La liste peut commencer sur la ligne de la commande (`!envoi 33766793050; Salut`).
- Une ligne illisible n'annule pas les autres : elle est listée dans l'accusé, avec son
  numéro de ligne.
- `!envoi` seul affiche un mémo d'utilisation.

### Ajouter un alias sans redémarrer

```bash
docker compose exec api python -c "
from whatsapp_bot import db
with db.session('/data/bot.db') as c:
    db.set_state(c, 'broadcast:alias:nord', '120363000000000000@g.us')"
```

Les alias de la table `state` sont prioritaires sur ceux de `.env`.

### Le plafond, et pourquoi il existe

`BROADCAST_MAX_RECIPIENTS` (défaut `25`) limite le nombre de destinataires par envoi.
**Au-delà, rien n'est envoyé** — le lot entier est refusé et l'accusé vous invite à
découper votre liste. C'est volontaire : un envoi partiel serait pire, car le rate
limiter global consomme son quota **au moment de la mise en file**, et les envois refusés
sont abandonnés avec un simple avertissement dans les logs.

Pour la même raison, le plafond est automatiquement borné à `RATE_LIMIT_PER_MINUTE - 1`
(l'accusé de réception consomme lui aussi un envoi). Si vous montez
`BROADCAST_MAX_RECIPIENTS`, montez `RATE_LIMIT_PER_MINUTE` avec — et gardez en tête que
le bridge espace les envois de 2 à 8 s, soit environ 12 messages par minute en pratique.

---

## Ajouter un comportement

Trois étapes, aucun enregistrement manuel : le registre découvre les handlers tout seul.

### 1. Créer le fichier

`app/whatsapp_bot/processing/handlers/horaires.py` :

```python
"""Répond aux questions sur les horaires d'ouverture."""

from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler

HORAIRES = "Ouvert du lundi au vendredi, 9h-18h."


class HorairesHandler(Handler):
    priority = 40           # après les commandes (10), avant le fallback (1000)
    stop_propagation = True # personne d'autre n'a besoin de voir ce message

    def match(self, msg: InboundMessage) -> bool:
        return "horaire" in msg.body.lower()

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        ctx.logger.info("horaires_demandes", chat_jid=msg.chat_jid)
        return [Outbound(jid=msg.chat_jid, text=HORAIRES, quoted_id=msg.id)]
```

### 2. Le tester

`app/tests/test_horaires.py` :

```python
from factories import make_message

from whatsapp_bot.processing.handlers.horaires import HorairesHandler


def test_repond_aux_horaires(context):
    handler = HorairesHandler()
    msg = make_message("quels sont vos horaires ?")

    assert handler.match(msg)
    replies = handler.run(msg, context)

    assert replies is not None
    assert "9h-18h" in replies[0].text
```

```bash
make test
```

### 3. Déployer

```bash
docker compose up -d --build worker
docker compose logs worker | grep handlers_loaded
```

### Ce que le `Context` met à disposition

| Attribut | Usage |
| --- | --- |
| `ctx.send(out)` | Envoyer un message (compte dans le quota, journalisé en base) |
| `ctx.logger` | Logger JSON, déjà lié au `message_id` et au nom du handler |
| `ctx.config` | Les réglages (`Settings`) |
| `ctx.db` | La connexion SQLite, si vous avez besoin de requêtes libres |
| `ctx.get_state(k)` / `ctx.set_state(k, v)` | Mémoire clé/valeur persistante |
| `ctx.sent_count` | Nombre de réponses déjà envoyées pour ce message |

### Règles de priorité en vigueur

| Priorité | Rôle | Exemple fourni |
| --- | --- | --- |
| 10 | Commandes explicites | `PingHandler` (`!ping`) |
| 20 | Commandes d'opérateur | `BroadcastHandler` (`!envoi`) |
| 50 | Routage / relais | `GroupRelayHandler` |
| 1000 | Observation, jamais de réponse | `FallbackLogHandler` |

Un handler dont le module échoue à l'import est **signalé dans les logs sans faire
tomber le worker** : les autres comportements continuent de tourner.

---

## Configuration

Toutes les variables sont dans `.env` (modèle commenté : `.env.example`).

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `BOT_TOKEN` | — | **Obligatoire.** Secret partagé entre les services |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `SEND_DELAY_MIN_MS` / `MAX_MS` | `2000` / `8000` | Délai aléatoire avant chaque envoi |
| `BRIDGE_DRY_RUN` | `false` | `true` = rien n'est envoyé, tout est journalisé |
| `RATE_LIMIT_PER_DAY` | `500` | Plafond global d'envois par jour (UTC) |
| `RATE_LIMIT_PER_MINUTE` | `30` | Plafond global d'envois par minute |
| `WORKER_JOB_TIMEOUT` | `2` | Budget de traitement par message, en secondes |
| `WORKER_MAX_RETRIES` | `3` | Tentatives supplémentaires avant la dead-letter queue |
| `WORKER_RETRY_INTERVALS` | `2,8,32` | Backoff exponentiel entre les tentatives |
| `RELAY_KEYWORDS` | `urgent,alerte` | Mots-clés déclenchant le relais |
| `RELAY_TARGET_JIDS` | *(vide)* | Groupes cibles ; vide = handler désactivé |
| `PING_ENABLED` | `true` | Active `!ping` |
| `BROADCAST_ADMIN_JIDS` | *(vide)* | Qui peut diffuser ; vide = handler désactivé |
| `BROADCAST_COMMAND` | `!envoi` | Commande déclenchant une diffusion |
| `BROADCAST_MAX_RECIPIENTS` | `25` | Destinataires max ; borné à `RATE_LIMIT_PER_MINUTE - 1` |
| `BROADCAST_ALIASES` | *(vide)* | Alias `nom=cible`, séparés par des virgules |

Le **limiteur de débit est global** : les 500 envois/jour et 30/minute sont partagés par
tous les handlers, et comptés dans Redis de façon atomique. Un envoi refusé est
enregistré en base avec le statut `rate_limited` — il n'est jamais rejoué, pour ne pas
consommer le quota restant.

---

## Exploitation

```bash
make help          # liste toutes les cibles
make up            # démarrer
make logs          # suivre les logs de tous les services
make logs-bridge   # uniquement le bridge (QR, connexion WhatsApp)
make health        # état de l'api, de la base et de Redis
make groups        # lister les groupes et leurs JID
make db            # les 20 derniers messages, entrants et sortants
make smoke         # injecter un faux « !ping » sans passer par WhatsApp
make dlq           # jobs en échec définitif
make dlq-requeue   # les rejouer après correction
make backup        # archive de la session WhatsApp + de la base
```

### Accéder à l'api depuis votre poste

Les ports n'écoutent que sur la boucle locale du serveur. Utilisez un tunnel SSH :

```bash
ssh -L 8000:127.0.0.1:8000 ubuntu@<IP_PUBLIQUE>
curl -H "X-Bot-Token: <BOT_TOKEN>" http://127.0.0.1:8000/groups
```

### Journaux

Tout est en JSON sur une seule ligne, api, worker et bridge compris :

```json
{"level":"info","timestamp":"2026-07-25T10:31:44Z","service":"worker","event":"message_processed","message_id":"3EB0...","handlers":["PingHandler"],"sent":1,"duration_ms":18.4}
```

Filtrage rapide : `docker compose logs worker | jq 'select(.event=="message_processed")'`.

La rotation est configurée dans `docker-compose.yml` (10 Mo × 3 par service) : sur le
Free Tier, le disque est la ressource qui manque en premier.

### Sauvegarde et restauration

Deux choses méritent d'être sauvegardées : le volume `wa_session` (sa perte impose un
nouveau scan du QR) et la base `bot.db`.

```bash
make backup                       # → backups/backup-<date>.tar.gz
```

Restauration :

```bash
docker compose down
docker run --rm -v frilivin-whatsapp-bot_wa_session:/wa \
  -v frilivin-whatsapp-bot_sqlite_data:/db \
  -v "$PWD/backups:/backup" alpine \
  sh -c "tar xzf /backup/backup-<date>.tar.gz -C /"
docker compose up -d
```

### Mise à jour

```bash
git pull
docker compose build
docker compose up -d
```

La session WhatsApp et la base survivent : elles sont dans des volumes nommés.

---

## Développement et tests

En local (hors Docker) :

```bash
make install       # dépendances Python de dev, dans app/
make check         # ruff + mypy + pytest + eslint + node --test
```

Individuellement :

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy strict sur whatsapp_bot
make test          # pytest
make node-test     # tests du bridge
```

La CI GitHub Actions rejoue exactement ces vérifications sur chaque pull request, et
construit en plus les deux images en `linux/arm64` pour garantir la compatibilité avec
la cible Oracle.

### Tester le pipeline sans WhatsApp

```bash
# dans .env : BRIDGE_DRY_RUN=true
docker compose up -d
make smoke
make db
```

`make smoke` injecte un faux message `!ping` dans `/webhook`, puis rejoue le même `id`
pour vérifier que la déduplication répond bien `duplicate`.

---

## Dépannage

| Symptôme | Cause probable | Solution |
| --- | --- | --- |
| Aucun QR dans les logs | Une session existe déjà | `make health` : si `connected: true`, tout va bien |
| Ni QR ni erreur, logs silencieux | Sortie réseau bloquée | Un `wa_not_connected` apparaît au bout de 45 s et précise quoi vérifier |
| `wa_logged_out` en `fatal` | Session révoquée depuis le téléphone | Supprimer le volume et re-scanner (ci-dessous) |
| Le QR expire trop vite | Normal, ~20 s | Gardez `make logs-bridge` ouvert, un nouveau QR apparaît |
| `send_rate_limited` | Quota atteint | Relever `RATE_LIMIT_PER_*` ou attendre la fenêtre suivante |
| `quoted_message_unknown` | Message trop ancien pour le cache | Sans gravité : la réponse part sans citation |
| Messages en `dead` | 4 échecs consécutifs | `make dlq` pour la cause, corriger, `make dlq-requeue` |
| `bridge unreachable` côté worker | Bridge redémarre | Il se reconnecte seul ; RQ rejoue le message |
| Le worker timeout à 2 s | Handler trop lent | Déporter le travail long, ou relever `WORKER_JOB_TIMEOUT` |

**Repartir d'une session vierge** (impose un nouveau scan) :

```bash
docker compose down
docker volume rm frilivin-whatsapp-bot_wa_session
docker compose up -d && make logs-bridge
```

---

## Limites connues

- **Ce projet utilise une bibliothèque non officielle** ([Baileys](https://github.com/WhiskeySockets/Baileys)),
  qui pilote WhatsApp Web. **WhatsApp peut bannir le numéro** utilisé — c'est pour cela
  que le bot vise un numéro jetable dédié, et que chaque envoi est espacé de 2 à 8 s.
  N'utilisez pas ce bot pour du démarchage de masse.
- **Citation de messages** : Baileys a besoin du message d'origine, pas seulement de son
  identifiant. Le bridge en garde les 2000 derniers en mémoire ; au-delà, la réponse part
  sans citation (et le log le dit).
- **Un seul worker** : suffisant pour 300 messages/jour. Au-delà, lancez plusieurs
  répliques — SQLite est en WAL et le limiteur de débit est déjà partagé via Redis.
- **Les quotas sont calculés en UTC**, pas dans le fuseau local.
- **Pas de médias** : le bot lit les légendes des images et documents mais n'envoie que
  du texte.

---

## Licence

MIT — voir [LICENSE](LICENSE).
