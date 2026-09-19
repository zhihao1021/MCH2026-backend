"""官方價格資料源 extension 框架。

這個套件底下每一個子資料夾就是一個資料來源；框架本身只有
`base`（契約）、`registry`（探索）與 `http`（共用 client）三個模組。
"""

from app.extensions.base import (
    ExtensionConfig,
    FetchWindow,
    PriceSource,
    RawMarket,
    RawPrice,
    SourceContext,
    SourceManifest,
)
from app.extensions.registry import ExtensionRegistry, LoadedExtension, registry

__all__ = [
    "ExtensionConfig",
    "ExtensionRegistry",
    "FetchWindow",
    "LoadedExtension",
    "PriceSource",
    "RawMarket",
    "RawPrice",
    "SourceContext",
    "SourceManifest",
    "registry",
]
