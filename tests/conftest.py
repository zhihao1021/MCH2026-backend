"""測試共用設定。

這裡的測試刻意不碰資料庫：需要 DB 的部分由 `scripts/smoke_test.py`
對真實服務跑一遍，兩者分工。
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.core.config import settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_extension(monkeypatch) -> Iterator[Callable[[str, str], Path]]:
    """在真實的 app/extensions/ 下建立臨時 extension 套件，測完刪掉。

    必須放在真實目錄，因為 registry 是用 `app.extensions.<name>` 匯入的；
    放到 tmp_path 只會得到 import_failed，測不到 manifest 與 SOURCE 的驗證。
    """
    ext_dir = Path(settings.extensions_dir)
    created: list[tuple[str, Path]] = []

    def make(name: str, source: str) -> Path:
        pkg = ext_dir / name
        pkg.mkdir(parents=True, exist_ok=False)
        (pkg / "__init__.py").write_text(source, encoding="utf-8")
        created.append((name, pkg))
        return pkg

    yield make

    for name, pkg in created:
        shutil.rmtree(pkg, ignore_errors=True)
        # 從 sys.modules 移掉，避免同一個 pytest session 的後續測試撿到快取
        for mod in [m for m in sys.modules if m.startswith(f"app.extensions.{name}")]:
            sys.modules.pop(mod, None)
