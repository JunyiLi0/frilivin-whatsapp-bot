"""Synthetic Sage inputs.

Deliberately built from scratch rather than copied from a real export: the
genuine files hold thousands of customer addresses and VAT numbers, which have
no business being in a public repository.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

SHEET_PATH = "xl/worksheets/sheet1.xml"

# A single order, in the "TaylormanCommande" shape the generator detects.
TAYLORMAN_ROWS: list[list[str]] = [
    ["TaylormanCommande", "", "", "", "", "", "", ""],
    ["Numéro", "1104999", "", "", "", "", "", ""],
    ["Client", "Bost", "", "", "", "", "", ""],
    ["Adresse", "Sas bost 0683859931 1cour ga 75002 paris FRANCE", "", "", "", "", "", ""],
    ["N°", "Référence", "Couleur", "Taille", "Remise", "Prix", "Quantité", "Colisage"],
    ["1", "# BM24U32-15 白", "WHITE", "", "", "9.50", "1", "8"],
    ["2", "# M631-1 黑", "BLACK", "", "", "7.00", "2", "8"],
]

_CLIENT_HEADER = (
    "Code\tNom\tAdresse 1\tAdresse 2\tAdresse 3\t"
    "Code Postal\tVille\tPays\tN° TVA intracom\tMode TVA"
)

CLIENTS = "\n".join(
    [
        _CLIENT_HEADER,
        "CL0295\tSAS BOST\t1 cour ga\t\t\t75002\tParis\tFrance\tFR00000000000\tLocal",
        "CL0300\tCLIENT ITALIEN\tvia roma 2\t\t\t20100\tMilano\tItalie\tIT11111111111\tCEE",
    ]
)

ARTICLES = "\n".join(
    [
        "Code\tDésignation longue\tTaux TVA",
        "BM24U32\tSHORT COTON\t20",
        "M631\tTEE-SHIRT\t20",
    ]
)


def write_xlsx(path: Path, rows: list[list[str]] | None = None) -> Path:
    """Write the smallest workbook ``generator.read_xlsx`` can read.

    Inline strings only, so no shared-strings table is needed.
    """
    rows = TAYLORMAN_ROWS if rows is None else rows
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    )
    for r, row in enumerate(rows, start=1):
        xml.append(f'<row r="{r}">')
        for c, value in enumerate(row):
            ref = f"{_column_letter(c)}{r}"
            xml.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
        xml.append("</row>")
    xml.append("</sheetData></worksheet>")

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(SHEET_PATH, "".join(xml))
    return path


def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def write_references(directory: Path) -> tuple[Path, Path]:
    """Write clients.txt / articles.txt, returning both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    clients = directory / "clients.txt"
    articles = directory / "articles.txt"
    clients.write_text(CLIENTS, encoding="utf-8")
    articles.write_text(ARTICLES, encoding="utf-8")
    return clients, articles
