"""Turning a received spreadsheet into a Sage import file.

Wraps the vendored generator with the two things a worker needs and a desktop
script does not: cached reference indexes, and a structured result instead of a
printed report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from whatsapp_bot.sage import generator

#: Name of the produced file, e.g. order 1104999 → import_sage_999.txt
OUTPUT_TEMPLATE = "import_sage_{suffix}.txt"
ORDER_SUFFIX_LENGTH = 3
FALLBACK_SUFFIX = "sans-numero"


class SageError(RuntimeError):
    """Generation failed for a reason worth telling the sender about."""


@dataclass(frozen=True)
class SageResult:
    output_path: Path
    filename: str
    orders: list[str]
    invoices: int
    lines: int
    report: str
    #: Things the operator should check in Sage afterwards.
    warnings: list[str]


def order_suffix(order_number: str | None) -> str:
    """Last three digits of an order number — what names the output file.

    ``"1104999"`` → ``"999"``. Shorter numbers are used whole; a number with no
    digit at all falls back to a fixed label rather than producing
    ``import_sage_.txt``.
    """
    digits = re.sub(r"\D", "", str(order_number or ""))
    if not digits:
        return FALLBACK_SUFFIX
    return digits[-ORDER_SUFFIX_LENGTH:]


def _index_key(path: Path) -> tuple[str, int, int]:
    """Identity of a reference export: path + mtime + size.

    Replacing clients.txt on the server therefore invalidates the cache on its
    own, with no restart.
    """
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


_clients_cache: tuple[tuple[str, int, int], list[dict[str, str]]] | None = None
_articles_cache: tuple[tuple[str, int, int], tuple[dict[str, Any], dict[str, Any]]] | None = None


def load_clients(path: Path) -> list[dict[str, str]]:
    global _clients_cache
    key = _index_key(path)
    if _clients_cache is None or _clients_cache[0] != key:
        _clients_cache = (key, generator.build_clients_index(str(path)))
    return _clients_cache[1]


def load_articles(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    global _articles_cache
    key = _index_key(path)
    if _articles_cache is None or _articles_cache[0] != key:
        _articles_cache = (key, generator.build_articles_index(str(path)))
    return _articles_cache[1]


def reset_cache() -> None:
    """Forget both indexes — used by tests."""
    global _clients_cache, _articles_cache
    _clients_cache = None
    _articles_cache = None


def _require(path: Path, label: str) -> None:
    if not path.is_file():
        raise SageError(
            f"{label} introuvable sur le serveur ({path}). Déposez l'export Sage à cet emplacement."
        )


def generate(
    spreadsheet: Path,
    *,
    clients_path: Path,
    articles_path: Path,
    output_dir: Path,
) -> SageResult:
    """Build the Sage import file for ``spreadsheet``.

    Raises :class:`SageError` with a message meant for the sender when the
    inputs are unusable; anything else that escapes is a genuine bug.
    """
    _require(spreadsheet, "Le fichier reçu")
    _require(clients_path, "La liste des clients")
    _require(articles_path, "La liste des articles")

    try:
        commandes, fmt, _ = generator.charger_commandes(str(spreadsheet))
    except Exception as exc:
        raise SageError(f"lecture du fichier impossible ({exc})") from exc

    if not commandes:
        raise SageError(
            "aucune commande détectée dans ce fichier. "
            "Vérifiez qu'il s'agit bien d'un export de commande."
        )

    orders = [str(cmd.get("cmd") or "").strip() for cmd in commandes]
    filename = OUTPUT_TEMPLATE.format(suffix=order_suffix(orders[0] if orders else None))
    output_path = output_dir / filename
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        report = generator.fabriquer(
            str(spreadsheet),
            str(clients_path),
            str(articles_path),
            str(output_path),
            fmt=fmt,
            commandes=commandes,
            clients=load_clients(clients_path),
            articles=load_articles(articles_path),
        )
    except Exception as exc:
        raise SageError(f"génération interrompue ({type(exc).__name__}: {exc})") from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise SageError("le fichier produit est vide — aucune ligne exploitable.")

    lines = sum(len(cmd.get("lignes") or []) for cmd in commandes)
    return SageResult(
        output_path=output_path,
        filename=filename,
        orders=[o for o in orders if o],
        invoices=len(commandes),
        lines=lines,
        report=report,
        warnings=_warnings(report),
    )


def _warnings(report: str) -> list[str]:
    """The report's "⚠" headings — what the operator has to check in Sage."""
    return [line.strip() for line in report.splitlines() if line.lstrip().startswith("⚠")]
