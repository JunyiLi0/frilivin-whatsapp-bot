"""The spreadsheet round trip: in an .xlsx, out an import_sage_<nnn>.txt."""

from __future__ import annotations

from pathlib import Path

import pytest
from factories import FakeSender, make_message
from sage_fixtures import write_references, write_xlsx

from whatsapp_bot.config import Settings
from whatsapp_bot.processing.base import Context
from whatsapp_bot.processing.handlers.fallback import FallbackLogHandler
from whatsapp_bot.processing.handlers.sage_import import COUNTER_KEY, SageImportHandler
from whatsapp_bot.sage import service
from whatsapp_bot.worker.tasks import run_pipeline

SENDER = "33612345678@s.whatsapp.net"


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    service.reset_cache()


@pytest.fixture
def sage_settings(settings: Settings, tmp_path: Path) -> Settings:
    clients, articles = write_references(tmp_path / "sage")
    return settings.model_copy(
        update={
            "sage_clients_path": str(clients),
            "sage_articles_path": str(articles),
            "media_outbox_dir": str(tmp_path / "out"),
        }
    )


def spreadsheet_message(
    tmp_path: Path,
    filename: str = "Bost_1104999.xlsx",
    *,
    from_jid: str = SENDER,
    message_id: str = "msg-1",
):
    path = write_xlsx(tmp_path / filename)
    return make_message(
        "",
        message_id=message_id,
        msg_type="document",
        from_jid=from_jid,
        filename=filename,
        media_path=str(path),
    )


# --- matching -----------------------------------------------------------------


class TestMatching:
    def test_matches_any_spreadsheet(self, sage_settings: Settings, tmp_path: Path) -> None:
        handler = SageImportHandler(sage_settings)
        assert handler.match(spreadsheet_message(tmp_path))
        assert handler.match(spreadsheet_message(tmp_path, "AUTRE.XLSX"))
        assert handler.match(spreadsheet_message(tmp_path, "macro.xlsm"))

    def test_ignores_other_documents_and_text(
        self, sage_settings: Settings, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        assert not handler.match(spreadsheet_message(tmp_path, "facture.pdf"))
        assert not handler.match(make_message("!ping"))

    def test_can_be_switched_off(self, sage_settings: Settings) -> None:
        handler = SageImportHandler(sage_settings.model_copy(update={"sage_enabled": False}))
        assert handler.enabled is False

    def test_runs_before_the_commands(self, sage_settings: Settings) -> None:
        assert SageImportHandler(sage_settings).priority < 10


# --- the happy path -----------------------------------------------------------


class TestGeneration:
    def test_returns_the_import_file(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        replies = handler.run(spreadsheet_message(tmp_path), context)

        assert replies is not None and len(replies) == 1
        out = replies[0]
        assert out.filename == "import_sage_999.txt"
        assert out.is_document
        assert Path(str(out.document_path)).is_file()
        assert out.jid == SENDER

    def test_caption_names_the_order(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        replies = handler.run(spreadsheet_message(tmp_path), context)

        assert replies is not None
        assert "import_sage_999.txt" in replies[0].text
        assert "1104999" in replies[0].text

    def test_caption_names_the_customer_and_its_score(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        """A wrong fuzzy match must be visible before the file is imported."""
        handler = SageImportHandler(sage_settings)
        replies = handler.run(spreadsheet_message(tmp_path), context)

        assert replies is not None
        assert "CL0295" in replies[0].text
        assert "score" in replies[0].text

    def test_caption_lists_which_article_is_missing(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        rows = [
            ["TaylormanCommande", ""],
            ["Numéro", "1104333"],
            ["Client", "SAS BOST"],
            ["Adresse", "1 cour ga 75002 paris FRANCE"],
            ["N°", "Référence", "Prix", "Quantité", "Colisage"],
            ["1", "# ZZ999999-1 PANTALON", "5.00", "1", "1"],
        ]
        path = write_xlsx(tmp_path / "manquant.xlsx", rows)
        msg = make_message("", msg_type="document", filename="manquant.xlsx", media_path=str(path))

        replies = SageImportHandler(sage_settings).run(msg, context)

        assert replies is not None
        assert "ZZ999999" in replies[0].text

    def test_processes_any_filename(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        """The output is named after the order inside, not after the file."""
        handler = SageImportHandler(sage_settings)
        replies = handler.run(spreadsheet_message(tmp_path, "n_importe_quoi.xlsx"), context)

        assert replies is not None
        assert replies[0].filename == "import_sage_999.txt"

    def test_counts_generations(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        handler.run(spreadsheet_message(tmp_path), context)
        handler.run(spreadsheet_message(tmp_path, "deuxieme.xlsx"), context)

        assert context.get_state(COUNTER_KEY) == "2"


# --- failures still get an answer ---------------------------------------------


class TestFailures:
    def test_reports_a_missing_download(self, sage_settings: Settings, context: Context) -> None:
        handler = SageImportHandler(sage_settings)
        msg = make_message("", msg_type="document", filename="cmd.xlsx", media_path=None)

        replies = handler.run(msg, context)

        assert replies is not None
        assert "n'a pas pu être téléchargé" in replies[0].text

    def test_reports_an_unreadable_file(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"pas un classeur")
        msg = make_message("", msg_type="document", filename="broken.xlsx", media_path=str(broken))

        replies = SageImportHandler(sage_settings).run(msg, context)

        assert replies is not None
        assert "Import impossible" in replies[0].text
        assert "lecture du fichier impossible" in replies[0].text

    def test_reports_missing_reference_files(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(
            sage_settings.model_copy(update={"sage_clients_path": "/nulle/part/clients.txt"})
        )

        replies = handler.run(spreadsheet_message(tmp_path), context)

        assert replies is not None
        assert "liste des clients introuvable" in replies[0].text

    def test_reports_a_spreadsheet_without_orders(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        path = write_xlsx(tmp_path / "vide.xlsx", [["TaylormanCommande", ""], ["", ""]])
        msg = make_message("", msg_type="document", filename="vide.xlsx", media_path=str(path))

        replies = SageImportHandler(sage_settings).run(msg, context)

        assert replies is not None
        assert "aucune commande détectée" in replies[0].text

    def test_a_failure_quotes_the_file(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"x")
        msg = make_message(
            "",
            message_id="abc",
            msg_type="document",
            filename="broken.xlsx",
            media_path=str(broken),
        )

        replies = SageImportHandler(sage_settings).run(msg, context)

        assert replies is not None
        assert replies[0].quoted_id == "abc"


# --- authorisation ------------------------------------------------------------


class TestAuthorisation:
    def test_open_to_everyone_by_default(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        msg = spreadsheet_message(tmp_path, from_jid="33999999999@s.whatsapp.net")

        assert handler.run(msg, context) is not None

    def test_can_be_restricted(
        self, sage_settings: Settings, context: Context, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(
            sage_settings.model_copy(update={"sage_admin_jids": "33612345678"})
        )
        stranger = spreadsheet_message(tmp_path, from_jid="33999999999@s.whatsapp.net")

        assert handler.run(stranger, context) is None
        assert handler.run(spreadsheet_message(tmp_path), context) is not None


# --- inside the pipeline ------------------------------------------------------


class TestInPipeline:
    def test_sends_the_document_and_stops_the_chain(
        self, sage_settings: Settings, context: Context, sender: FakeSender, tmp_path: Path
    ) -> None:
        handler = SageImportHandler(sage_settings)
        msg = spreadsheet_message(tmp_path)

        result = run_pipeline(msg, context, [handler, FallbackLogHandler()])

        assert result.executed == ["SageImportHandler"]
        assert len(sender.messages) == 1
        assert sender.messages[0].filename == "import_sage_999.txt"
