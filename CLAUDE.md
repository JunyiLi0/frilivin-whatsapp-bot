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
7. **Les exports Sage ne sont jamais versionnés** (`sage-data/` gitignoré) : ils
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

`make check` = ruff + ruff format + mypy strict + pytest (**184**) + eslint +
`node --test` (**68**). Tout doit rester vert.

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

## 8. Reste à faire

- Aucun test réel de l'import Sage **sur le serveur** : les exports doivent être
  déposés dans `~/frilivin-whatsapp-bot/sage-data/{clients,articles}.txt`
  (voir `sage-data/.gitkeep`). Vérifié en local sur les vrais fichiers, pas en
  production.
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
