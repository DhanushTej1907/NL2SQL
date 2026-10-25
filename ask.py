"""Interactive NL-to-SQL terminal without the database menu (see main.py).

    python ask.py samples/university.db
    python ask.py samples/retail_dump.sql --dialect postgres --no-llm
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nl2sql.repl import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(prog="python ask.py"))
