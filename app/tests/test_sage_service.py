"""Generating the Sage import file: naming, caching, and refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
from sage_fixtures import write_references, write_xlsx

from whatsapp_bot.sage import generator, service
from whatsapp_bot.sage.service import SageError, generate, order_suffix


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    service.reset_cache()


@pytest.fixture
def refs(tmp_path: Path) -> tuple[Path, Path]:
    return write_references(tmp_path / "sage")


class TestOrderSuffix:
    @pytest.mark.parametrize(
        ("order", "expected"),
        [
            ("1104999", "999"),
            ("1104000", "000"),
            ("42", "42"),  # shorter than three digits: used whole
            ("CMD-1104999", "999"),
            ("", "sans-numero"),
            (None, "sans-numero"),
            ("ABC", "sans-numero"),
        ],
    )
    def test_takes_the_last_three_digits(self, order: str | None, expected: str) -> None:
        assert order_suffix(order) == expected


class TestGenerate:
    def test_names_the_file_after_the_order(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "Bost_1104999.xlsx")

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        assert result.filename == "import_sage_999.txt"
        assert result.output_path.is_file()
        assert result.orders == ["1104999"]
        assert result.invoices == 1
        assert result.lines == 2

    def test_writes_a_sage_file(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        # cp1252, CRLF, ";" separated, 92 columns, one E header then one L per
        # line. Read as bytes: read_text would translate the CRLF away and the
        # line ending is part of what Sage expects.
        content = result.output_path.read_bytes().decode("cp1252")
        assert "\r\n" in content
        rows = [r for r in content.split("\r\n") if r]
        assert len(rows) == 3
        assert rows[0].startswith("E;")
        assert all(row.count(";") == 91 for row in rows)
        assert rows[1].startswith("L;")

    def test_resolves_the_client(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        assert "CL0295" in result.report
        assert result.output_path.read_bytes().decode("cp1252").split(";")[8] == "CL0295"

    def test_creates_the_output_directory(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "a" / "b"
        )

        assert result.output_path.parent.is_dir()

    def test_reports_warnings(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        rows = [
            ["TaylormanCommande", ""],
            ["Numéro", "1104111"],
            ["Client", "PARFAIT INCONNU SARL"],  # matches no client in the fixture
            ["Adresse", "1 rue nulle part 99999 ailleurs"],
            ["N°", "Référence", "Couleur", "Taille", "Remise", "Prix", "Quantité", "Colisage"],
            ["1", "# ZZ9999-1 noir", "BLACK", "", "", "5.00", "1", "1"],
        ]
        source = write_xlsx(tmp_path / "inconnu.xlsx", rows)

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        assert result.warnings
        assert any("CLIENT" in w for w in result.warnings)
        assert any("ARTICLE" in w for w in result.warnings)


class TestRefusals:
    def test_missing_spreadsheet(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        with pytest.raises(SageError, match="Le fichier reçu introuvable"):
            generate(
                tmp_path / "absent.xlsx",
                clients_path=clients,
                articles_path=articles,
                output_dir=tmp_path / "out",
            )

    def test_missing_reference_export(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        _, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        with pytest.raises(SageError, match="liste des clients introuvable"):
            generate(
                source,
                clients_path=tmp_path / "nope.txt",
                articles_path=articles,
                output_dir=tmp_path / "out",
            )

    def test_unreadable_spreadsheet(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        broken = tmp_path / "broken.xlsx"
        broken.write_bytes(b"ceci n'est pas un classeur")

        with pytest.raises(SageError, match="lecture du fichier impossible"):
            generate(
                broken, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
            )

    def test_spreadsheet_without_any_order(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "vide.xlsx", [["TaylormanCommande", ""], ["", ""]])

        with pytest.raises(SageError, match="aucune commande détectée"):
            generate(
                source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
            )


class TestClientMatching:
    def test_reports_the_customer_an_invoice_was_attached_to(
        self, tmp_path: Path, refs: tuple[Path, Path]
    ) -> None:
        clients, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        assert len(result.clients) == 1
        match = result.clients[0]
        assert match.attached
        assert match.code == "CL0295"
        assert "CL0295" in match.describe()

    def test_reports_an_unattached_customer(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, articles = refs
        rows = [
            ["TaylormanCommande", ""],
            ["Numéro", "1104111"],
            ["Client", "PARFAIT INCONNU"],
            ["Adresse", "1 rue nulle part 99999 ailleurs"],
            ["N°", "Référence", "Prix", "Quantité", "Colisage"],
            ["1", "# M631-1", "5.00", "1", "1"],
        ]
        source = write_xlsx(tmp_path / "inconnu.xlsx", rows)

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        match = result.clients[0]
        assert not match.attached
        assert match.code is None
        assert "non rattaché" in match.describe()

    def test_a_single_shared_word_no_longer_attaches(self) -> None:
        """A one-word Sage record used to score a perfect 1.0 on any order
        containing that word, silently invoicing the wrong customer."""
        assert generator.score_client("ENTREPRISE QUI N EXISTE PAS SARL", "PAS") < 0.60
        assert generator.score_client("BOULANGERIE DU COIN", "DU") < 0.60

    @pytest.mark.parametrize(
        ("order", "record"),
        [
            ("Bost", "SAS BOST"),
            ("CUBIK", "CUBIK"),
            ("CUBIK SARL", "CUBIK"),
            ("TEXAS DIFFUSION", "TEXAS DIFFUSION"),
        ],
    )
    def test_genuine_matches_survive(self, order: str, record: str) -> None:
        """Short legitimate names match through the sequence/prefix score."""
        assert generator.score_client(order, record) >= 0.60


class TestWarnings:
    def test_detail_lines_are_kept(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        """The heading alone says "1 article missing" without saying which."""
        clients, articles = refs
        rows = [
            ["TaylormanCommande", ""],
            ["Numéro", "1104222"],
            ["Client", "SAS BOST"],
            ["Adresse", "1 cour ga 75002 paris FRANCE"],
            ["N°", "Référence", "Prix", "Quantité", "Colisage"],
            ["1", "# ZZ999999-1 PANTALON", "5.00", "1", "1"],
        ]
        source = write_xlsx(tmp_path / "article.xlsx", rows)

        result = generate(
            source, clients_path=clients, articles_path=articles, output_dir=tmp_path / "out"
        )

        joined = "\n".join(result.warnings)
        assert "ARTICLES NON TROUVÉS" in joined
        assert "ZZ999999" in joined, "le détail doit accompagner l'en-tête"


class TestCache:
    def test_reference_indexes_are_reused(self, tmp_path: Path, refs: tuple[Path, Path]) -> None:
        clients, _ = refs
        first = service.load_clients(clients)
        assert service.load_clients(clients) is first

    def test_replacing_an_export_invalidates_it(
        self, tmp_path: Path, refs: tuple[Path, Path]
    ) -> None:
        """A new clients.txt must be picked up without restarting the worker."""
        clients, _ = refs
        first = service.load_clients(clients)

        clients.write_text(
            "Code\tNom\tCode Postal\tVille\tPays\tMode TVA\n"
            "CL0999\tAUTRE\t75001\tParis\tFrance\tLocal",
            encoding="utf-8",
        )
        second = service.load_clients(clients)

        assert second is not first
        assert [c["code"] for c in second] == ["CL0999"]


class TestThreshold:
    def test_a_stricter_threshold_leaves_the_invoice_unattached(
        self, tmp_path: Path, refs: tuple[Path, Path]
    ) -> None:
        """The knob the operator turns when a wrong attachment is worse than none."""
        clients, articles = refs
        source = write_xlsx(tmp_path / "cmd.xlsx")

        attached = generate(
            source,
            clients_path=clients,
            articles_path=articles,
            output_dir=tmp_path / "a",
            threshold=0.60,
        )
        # Above 1 on purpose: the postcode and city bonuses are *added* to the
        # name score, so a well-matched customer can exceed 1.0.
        strict = generate(
            source,
            clients_path=clients,
            articles_path=articles,
            output_dir=tmp_path / "b",
            threshold=1.5,
        )

        assert attached.clients[0].attached
        assert attached.clients[0].score > 1.0
        assert not strict.clients[0].attached
