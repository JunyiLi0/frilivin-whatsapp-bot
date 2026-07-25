# Raccourcis d'exploitation. `make` seul affiche l'aide.
.DEFAULT_GOAL := help
.PHONY: help build up down restart ps logs logs-bridge qr health groups smoke \
        db dlq dlq-requeue backup install test lint fmt typecheck check \
        node-install node-test node-lint

# Charge .env s'il existe, pour disposer de BOT_TOKEN et API_PORT dans les cibles.
-include .env
export

COMPOSE ?= docker compose
API ?= http://127.0.0.1:$(or $(API_PORT),8000)
AUTH := -H "X-Bot-Token: $(BOT_TOKEN)"

help: ## Affiche cette aide
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Cycle de vie -----------------------------------------------------------

build: ## Construit les images
	$(COMPOSE) build

up: ## Démarre la pile en arrière-plan
	$(COMPOSE) up -d

down: ## Arrête la pile (les volumes sont conservés)
	$(COMPOSE) down

restart: ## Redémarre la pile
	$(COMPOSE) restart

ps: ## État des conteneurs
	$(COMPOSE) ps

logs: ## Suit les logs de tous les services
	$(COMPOSE) logs -f --tail=100

logs-bridge: ## Suit les logs du bridge
	$(COMPOSE) logs -f --tail=100 bridge

qr: ## Affiche le QR code à scanner (premier démarrage)
	@echo "Ouvrez WhatsApp > Appareils liés > Lier un appareil, puis scannez :"
	$(COMPOSE) logs -f --tail=200 bridge

# --- Exploitation -----------------------------------------------------------

health: ## Vérifie l'état de l'api et du bridge
	@curl -fsS $(API)/health | python3 -m json.tool

groups: ## Liste les groupes WhatsApp et leurs JID
	@curl -fsS $(AUTH) $(API)/groups | python3 -m json.tool

smoke: ## Injecte un faux message « !ping » dans le pipeline
	@id="smoke-$$(date +%s)"; \
	echo "→ envoi de $$id"; \
	curl -fsS -X POST $(API)/webhook $(AUTH) \
		-H 'Content-Type: application/json' \
		-d "{\"id\":\"$$id\",\"from\":\"33600000000@s.whatsapp.net\",\"chat_jid\":\"33600000000@s.whatsapp.net\",\"is_group\":false,\"timestamp\":$$(date +%s),\"type\":\"text\",\"text\":\"!ping\",\"quoted_id\":null}" \
		| python3 -m json.tool; \
	echo "→ rejeu du même id (doit répondre \"duplicate\")"; \
	curl -fsS -X POST $(API)/webhook $(AUTH) \
		-H 'Content-Type: application/json' \
		-d "{\"id\":\"$$id\",\"from\":\"33600000000@s.whatsapp.net\",\"chat_jid\":\"33600000000@s.whatsapp.net\",\"is_group\":false,\"timestamp\":$$(date +%s),\"type\":\"text\",\"text\":\"!ping\",\"quoted_id\":null}" \
		| python3 -m json.tool

db: ## Affiche les 20 derniers messages en base
	$(COMPOSE) exec -T api python -m whatsapp_bot.tools.dump_messages

dlq: ## Liste les jobs de la dead-letter queue
	$(COMPOSE) exec -T worker python -m whatsapp_bot.tools.dlq list

dlq-requeue: ## Rejoue tous les jobs de la dead-letter queue
	$(COMPOSE) exec -T worker python -m whatsapp_bot.tools.dlq requeue

backup: ## Sauvegarde la session WhatsApp et la base dans ./backups
	@mkdir -p backups
	@ts=$$(date +%Y%m%d-%H%M%S); \
	docker run --rm \
		-v frilivin-whatsapp-bot_wa_session:/wa:ro \
		-v frilivin-whatsapp-bot_sqlite_data:/db:ro \
		-v "$$PWD/backups:/backup" \
		alpine sh -c "tar czf /backup/backup-$$ts.tar.gz -C / wa db"; \
	echo "→ backups/backup-$$ts.tar.gz"

# --- Développement ----------------------------------------------------------

install: ## Installe les dépendances Python de développement
	cd app && python3 -m pip install -e '.[dev]'

test: ## Lance les tests Python
	cd app && python3 -m pytest -q

lint: ## Analyse statique (ruff)
	cd app && python3 -m ruff check . && python3 -m ruff format --check .

fmt: ## Reformate le code Python
	cd app && python3 -m ruff format . && python3 -m ruff check --fix .

typecheck: ## Vérification de types (mypy)
	cd app && python3 -m mypy whatsapp_bot

node-install: ## Installe les dépendances du bridge
	cd bridge && npm ci

node-lint: ## Analyse statique du bridge (eslint)
	cd bridge && npx eslint .

node-test: ## Tests unitaires du bridge
	cd bridge && npm test

check: lint typecheck test node-lint node-test ## Tout ce que la CI exécute
