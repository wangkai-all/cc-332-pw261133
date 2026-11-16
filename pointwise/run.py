#!/usr/bin/env python3
"""入口：生成虚构数据（如缺失）并启动分析页服务。

用法：
  python3 run.py [--port 8321] [--regen]
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from billing import dataset  # noqa: E402
from billing.server import serve  # noqa: E402
from billing.store import Store  # noqa: E402

DATA_DIR = ROOT / "data"


def main():
    port = 8321
    if "--regen" in sys.argv:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    dataset.ensure_dataset(DATA_DIR)
    store = Store(DATA_DIR)
    serve(store, port)


if __name__ == "__main__":
    main()
