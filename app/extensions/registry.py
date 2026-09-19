"""Extension 探索與註冊。

載入方式只有一種：掃描 `app/extensions/` 底下的子套件。
一個合格的 extension 必須滿足：

1. 是個資料夾，含 `__init__.py`，名稱不以 `_` 或 `.` 開頭；
2. `__init__.py` 匯出名為 `SOURCE` 的屬性，且它是 `PriceSource` 的子類別；
3. `SOURCE.manifest.key` 必須與資料夾名稱完全相同。

第三點是刻意的：目錄名就是這個來源在 DB、API 與設定檔中的識別碼，
不讓兩者分岔，才不會出現「改了 manifest 但沒人發現」的情況。

任何一個 extension 載入失敗都不會讓整個服務起不來——錯誤會被收集到
`registry.errors`，並透過 `/v1/sources` 與啟動日誌暴露出來。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.core.errors import ExtensionError, NotFoundError
from app.extensions.base import (
    ExtensionConfig,
    PriceSource,
    SourceContext,
    SourceManifest,
)

logger = logging.getLogger(__name__)

# extension 套件必須匯出的屬性名稱
SOURCE_ATTR = "SOURCE"
PACKAGE_ROOT = "app.extensions"

# 掃描時要跳過的框架自身模組
_FRAMEWORK_MODULES = {"base", "registry", "http"}


@dataclass(frozen=True)
class ExtensionLoadError:
    """單一 extension 的載入失敗紀錄。"""

    key: str
    reason: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "reason": self.reason, "detail": self.detail}


@dataclass
class LoadedExtension:
    """一個成功載入、可被實例化的 extension。"""

    key: str
    source_cls: type[PriceSource]
    manifest: SourceManifest
    config: ExtensionConfig
    module: ModuleType
    path: Path
    enabled: bool = True

    def instantiate(self, http: httpx.AsyncClient) -> PriceSource:
        ctx = SourceContext(
            key=self.key,
            config=self.config,
            http=http,
            logger=logging.getLogger(f"{PACKAGE_ROOT}.{self.key}"),
        )
        return self.source_cls(ctx)


@dataclass
class ExtensionRegistry:
    """已載入 extension 的容器。全程序共用一份（見檔尾的 `registry`）。"""

    _items: dict[str, LoadedExtension] = field(default_factory=dict)
    _errors: list[ExtensionLoadError] = field(default_factory=list)
    _discovered: bool = False

    # -- 探索 -------------------------------------------------------------
    def discover(self, *, force: bool = False) -> "ExtensionRegistry":
        if self._discovered and not force:
            return self

        self._items.clear()
        self._errors.clear()

        # Python 會快取套件目錄的內容清單，不清掉的話剛丟進去的
        # extension 資料夾會 import 不到（/admin/sources/reload 就是這情況）。
        importlib.invalidate_caches()

        ext_dir = Path(settings.extensions_dir)
        if not ext_dir.is_dir():
            logger.warning("extensions 目錄不存在：%s", ext_dir)
            self._discovered = True
            return self

        for info in pkgutil.iter_modules([str(ext_dir)]):
            name = info.name
            if not info.ispkg or name.startswith("_") or name in _FRAMEWORK_MODULES:
                continue
            if not self._is_selected(name):
                logger.info("extension %s 已在設定中停用，略過", name)
                continue
            self._load_one(name, ext_dir / name)

        self._discovered = True
        logger.info(
            "extension 探索完成：載入 %d 個，失敗 %d 個 %s",
            len(self._items),
            len(self._errors),
            sorted(self._items),
        )
        return self

    def _is_selected(self, key: str) -> bool:
        if key in settings.extensions_disabled:
            return False
        if settings.extensions_enabled:
            return key in settings.extensions_enabled
        return True

    def _load_one(self, key: str, path: Path) -> None:
        module_name = f"{PACKAGE_ROOT}.{key}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            self._fail(key, "import_failed", f"{type(exc).__name__}: {exc}")
            return

        source_cls = getattr(module, SOURCE_ATTR, None)
        if source_cls is None:
            self._fail(key, "missing_source_attr", f"{module_name} 未匯出 {SOURCE_ATTR}")
            return
        if not (isinstance(source_cls, type) and issubclass(source_cls, PriceSource)):
            self._fail(key, "invalid_source_attr", f"{SOURCE_ATTR} 必須是 PriceSource 的子類別")
            return

        manifest = getattr(source_cls, "manifest", None)
        if not isinstance(manifest, SourceManifest):
            self._fail(key, "missing_manifest", "manifest 必須是 SourceManifest 實例")
            return
        if manifest.key != key:
            self._fail(
                key,
                "key_mismatch",
                f"資料夾名稱 {key!r} 與 manifest.key {manifest.key!r} 不一致",
            )
            return

        try:
            config = self._build_config(source_cls, key)
        except ValidationError as exc:
            self._fail(key, "invalid_config", exc.json(indent=None))
            return

        self._items[key] = LoadedExtension(
            key=key,
            source_cls=source_cls,
            manifest=manifest,
            config=config,
            module=module,
            path=path,
        )
        logger.info("已載入 extension %s (%s, v%s)", key, manifest.name, manifest.version)

    @staticmethod
    def _build_config(source_cls: type[PriceSource], key: str) -> ExtensionConfig:
        raw = settings.extensions_config.get(key, {})
        return source_cls.config_model.model_validate(raw)

    def _fail(self, key: str, reason: str, detail: str | None = None) -> None:
        err = ExtensionLoadError(key=key, reason=reason, detail=detail)
        self._errors.append(err)
        logger.error("extension %s 載入失敗（%s）：%s", key, reason, detail)

    # -- 查詢 -------------------------------------------------------------
    @property
    def errors(self) -> list[ExtensionLoadError]:
        return list(self._errors)

    def all(self) -> list[LoadedExtension]:
        self.discover()
        return sorted(self._items.values(), key=lambda e: e.key)

    def keys(self) -> list[str]:
        return [e.key for e in self.all()]

    def get(self, key: str) -> LoadedExtension:
        self.discover()
        try:
            return self._items[key]
        except KeyError:
            raise NotFoundError(f"Unknown extension {key!r}", code="unknown_extension") from None

    def require(self, key: str) -> LoadedExtension:
        ext = self.get(key)
        if not ext.enabled:
            raise ExtensionError(f"Extension {key!r} is disabled", code="extension_disabled")
        return ext

    # -- 測試用 -----------------------------------------------------------
    def register(self, source_cls: type[PriceSource], *, config: ExtensionConfig | None = None) -> None:
        """手動註冊一個 extension。正式流程不用，給測試與動態情境使用。"""
        manifest = source_cls.manifest
        self._items[manifest.key] = LoadedExtension(
            key=manifest.key,
            source_cls=source_cls,
            manifest=manifest,
            config=config or source_cls.config_model(),
            module=importlib.import_module(source_cls.__module__),
            path=Path(getattr(source_cls, "__file__", ".")),
        )
        self._discovered = True

    def reset(self) -> None:
        self._items.clear()
        self._errors.clear()
        self._discovered = False


registry = ExtensionRegistry()
