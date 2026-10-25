"""NL-to-SQL compiler: interactive entry point.

    python main.py                      pick a sample database from a menu
    python main.py samples/retail.db    start straight away on that database

Options: --dialect <sqlglot dialect>, --no-llm, --raw.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from nl2sql.repl import main as repl_main, read_line  # noqa: E402

SAMPLES = ROOT / "samples"
DEFAULT_SAMPLE = "university.db"
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".sql"}


def sample_databases() -> list[Path]:
    if not SAMPLES.is_dir():
        return []
    return sorted(p for p in SAMPLES.iterdir() if p.suffix.lower() in DATABASE_SUFFIXES)


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def choose_database() -> str | None:
    """Numbered menu of the bundled samples. Returns a path, or None to quit."""
    samples = sample_databases()
    default = SAMPLES / DEFAULT_SAMPLE
    if not default.exists():
        default = samples[0] if samples else None
    type_path = len(samples) + 1

    print("Choose a database:")
    for number, path in enumerate(samples, 1):
        mark = "  (default)" if path == default else ""
        print(f"  {number}. {path.name}{mark}")
    print(f"  {type_path}. Type a path")

    hint = f" [Enter = {default.name}]" if default else ""
    while True:
        try:
            choice = read_line(f"Select 1-{type_path}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if not choice and default:
            return _shown(default)
        if choice.isdigit() and 1 <= int(choice) <= len(samples):
            return _shown(samples[int(choice) - 1])
        if choice == str(type_path):
            try:
                typed = read_line("Path to a .db, .sqlite or .sql file: ").strip().strip('"')
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if typed and Path(typed).is_file():
                return typed
            print(f"No such file: {typed or '(empty)'}")
            continue
        print(f"Please enter a number from 1 to {type_path}.")


def main(argv: list[str] | None = None) -> int:
    return repl_main(argv, pick_database=choose_database)


if __name__ == "__main__":
    raise SystemExit(main())
