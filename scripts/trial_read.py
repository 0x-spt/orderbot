"""Reads one article from 1C, with no orders and no confirmation: a manual check of the
screen layout on a real machine.

    uv run python scripts/trial_read.py "ART-0001(ART-0001X)"

The robot brings the RDP window to the front, empties the search field with its "x" button,
types the search key, presses Return, clicks the found row once, reads «Остатки» and «Цены»
and prints them. Before every input it waits until the mouse and keyboard have been idle
for IDLE_MIN_SECONDS.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from orderbot.main import load_env_file  # noqa: E402
from orderbot.screen.idle import UserActive  # noqa: E402
from orderbot.screen.onec import MacOneC  # noqa: E402
from orderbot.screen.reader import Layout  # noqa: E402


def main(article: str) -> int:
    load_env_file(ROOT / ".env")
    layout = Layout.load(ROOT / os.environ["LAYOUT_FILE"])
    idle = float(os.environ.get("IDLE_MIN_SECONDS") or 30)
    onec = MacOneC(layout, os.environ.get("RDP_WINDOW_TITLE_CONTAINS", ""), idle,
                   ROOT / "data" / "trial")
    print(f"Жду {idle:.0f} с без мыши и клавиатуры, затем читаю {article} …")
    onec.connect()
    try:
        deadline = time.monotonic() + 180
        while True:
            try:
                item = onec.read(article)
                break
            except UserActive as e:
                if time.monotonic() > deadline:
                    raise
                print("  жду:", e)
                time.sleep(5)
    except Exception as e:  # show the refusal as it is
        print("Отказ:", type(e).__name__, e)
        return 1
    finally:
        onec.disconnect()
    print("Остатки:")
    for name, row in item.stock.items():
        print(f"  {name}: кол-во {row.on_hand}, резерв {row.reserve}")
    print("EPR:", item.epr, " PPR рубли:", item.ppr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
