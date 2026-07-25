"""Print the last messages from the ledger — ``make db``."""

from __future__ import annotations

import argparse
import sys

from whatsapp_bot import db as db_module
from whatsapp_bot.config import get_settings

COLUMNS = ("created_at", "direction", "status", "handler", "chat_jid", "text")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show recent messages")
    parser.add_argument("-n", "--limit", type=int, default=20)
    args = parser.parse_args(argv)

    settings = get_settings()
    with db_module.session(settings.database_path) as conn:
        rows = db_module.recent_messages(conn, args.limit)

    if not rows:
        print("(aucun message)")
        return 0

    for row in reversed(rows):
        text = (row["text"] or "").replace("\n", " ")[:60]
        error = f"  error={row['error']}" if row["error"] else ""
        print(
            f"{row['created_at']}  {row['direction']:<3} {row['status']:<12} "
            f"{(row['handler'] or '-'):<24} {row['chat_jid']:<28} {text}{error}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
