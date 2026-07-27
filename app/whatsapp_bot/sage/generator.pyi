"""Types for the part of the vendored generator that service.py actually calls.

Declaring them here keeps service.py under `strict` — the untyped body stays
excluded, but the boundary between the two is checked.
"""

from typing import Any

SEUIL_CLIENT: float
MIN_MOTS_RECOUVREMENT: int
MOTS_VIDES: set[str]

def build_clients_index(path: str) -> list[dict[str, str]]: ...
def build_articles_index(path: str) -> tuple[dict[str, Any], dict[str, Any]]: ...
def charger_commandes(
    cmd_path: str | list[str], fmt: str = ...
) -> tuple[list[dict[str, Any]], str, list[str]]: ...
def fabriquer(
    cmd_path: str | list[str],
    clients_path: str,
    articles_path: str,
    sortie: str,
    seuil: float = ...,
    garder_couleur: bool = ...,
    validee: str = ...,
    date_piece: str | None = ...,
    fmt: str = ...,
    commandes: list[dict[str, Any]] | None = ...,
    clients: list[dict[str, str]] | None = ...,
    articles: tuple[dict[str, Any], dict[str, Any]] | None = ...,
) -> str: ...
def match_client(
    cli: dict[str, Any], clients: list[dict[str, str]], seuil: float = ...
) -> tuple[dict[str, str] | None, float]: ...
def score_client(a: str, b: str) -> float: ...
def detect_format(path: str) -> str: ...
def read_xlsx(path: str) -> list[list[str]]: ...
