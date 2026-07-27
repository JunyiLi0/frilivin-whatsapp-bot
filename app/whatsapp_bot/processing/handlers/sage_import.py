"""Send a spreadsheet, get the Sage 50 import file back.

Any inbound ``.xlsx`` is processed, with no command to remember: the operator
forwards the order file to the bot and receives ``import_sage_<3 derniers
chiffres de la commande>.txt``. A failure is answered with its reason, never
with silence — a spreadsheet that vanishes is worse than one that is refused.
"""

from __future__ import annotations

import time
from pathlib import Path

from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler
from whatsapp_bot.processing.jids import normalise_jid
from whatsapp_bot.sage import SageError, SageResult, generate

SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm")
COUNTER_KEY = "sage:generated"
MAX_WARNING_LINES = 6


class SageImportHandler(Handler):
    # Ahead of the commands: a document carries no text to confuse them with,
    # and stopping here keeps the fallback from logging it as unhandled.
    priority = 5
    stop_propagation = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = self.settings.sage_enabled

        admins = (normalise_jid(entry) for entry in self.settings.sage_admin_list)
        # Empty means "anyone may submit", which is the documented default.
        self.admins = {jid for jid in admins if jid}

    def match(self, msg: InboundMessage) -> bool:
        return msg.type == "document" and msg.filename_suffix() in SPREADSHEET_SUFFIXES

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        if self.admins and normalise_jid(msg.from_jid) not in self.admins:
            ctx.logger.warning("sage_refused", from_jid=msg.from_jid, filename=msg.filename)
            return None

        if not msg.has_file:
            # The bridge saw the attachment but could not fetch it — its own
            # logs carry the cause, the sender only needs to know to retry.
            ctx.logger.error("sage_no_file", filename=msg.filename)
            return [self._reply(msg, "⚠️ Le fichier n'a pas pu être téléchargé. Renvoyez-le.")]

        started = time.perf_counter()
        try:
            result = generate(
                Path(str(msg.media_path)),
                clients_path=Path(self.settings.sage_clients_path),
                articles_path=Path(self.settings.sage_articles_path),
                output_dir=Path(self.settings.media_outbox_dir),
            )
        except SageError as exc:
            ctx.logger.warning("sage_failed", filename=msg.filename, reason=str(exc))
            return [self._reply(msg, f"❌ Import impossible pour « {msg.filename} »\n\n{exc}")]
        except Exception as exc:
            # Broad on purpose: whatever the vendored generator throws, the
            # sender is owed an answer rather than silence.
            ctx.logger.exception("sage_crashed", filename=msg.filename)
            return [
                self._reply(
                    msg,
                    f"❌ Erreur inattendue sur « {msg.filename} »\n\n{type(exc).__name__}: {exc}",
                )
            ]

        duration_ms = round((time.perf_counter() - started) * 1000)
        ctx.logger.info(
            "sage_generated",
            filename=msg.filename,
            output=result.filename,
            invoices=result.invoices,
            lines=result.lines,
            duration_ms=duration_ms,
        )

        previous = int(ctx.get_state(COUNTER_KEY) or "0")
        ctx.set_state(COUNTER_KEY, str(previous + 1))

        return [
            Outbound(
                jid=msg.chat_jid,
                text=self._caption(result),
                document_path=str(result.output_path),
                filename=result.filename,
                handler=self.name,
            )
        ]

    def _reply(self, msg: InboundMessage, text: str) -> Outbound:
        return Outbound(jid=msg.chat_jid, text=text, quoted_id=msg.id, handler=self.name)

    def _caption(self, result: SageResult) -> str:
        orders = ", ".join(result.orders) or "sans numéro"
        lines = [
            f"✅ {result.filename}",
            f"Commande {orders} — {result.invoices} facture(s), {result.lines} ligne(s).",
        ]
        if result.warnings:
            lines.append("")
            # The report's own "⚠" headings: clients to create, articles not
            # found, unknown countries. Truncated so a caption stays readable.
            lines.extend(result.warnings[:MAX_WARNING_LINES])
            lines.append("")
            lines.append("Détail complet dans les logs du worker.")
        return "\n".join(lines)
