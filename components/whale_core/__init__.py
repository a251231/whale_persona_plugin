"""whale_core: internal implementation package of the Dafeiyu (Whale Girl) plugin (v0.5.2 A-12).

``components/event_listeners/whale_listener.py`` and ``diana_features.py`` stay the public,
LangBot-discovered entry points. They are thin *compatibility facades* over these modules:

=================  ==========================================================================
``http_gateway``   pooled ``httpx.AsyncClient`` borrowing, WCF circuit breaker, SSRF-safe public
                   fetching, bot/user name caches, WCF messaging / moments / friend REST wrappers
``mood_memory``    8-dim mood + decay, intimacy, polarity-aware fact dedup, memory recall, archive
                   consolidation, temper state, daily schedule
``state_repo``     secrets cache, persistent state load/save (``_save_seq``), debounced atomic
                   writes, session locks/tasks, bounded LRU runtime caches
``media_parsers``  ZIP-safe documents + OCR, async PIL/GIF, link/live/HTML parsers, web search,
                   music, TTS/ASR, meme stealing
``prompts``        persona bases, tool schemas, 3-tier prefix-cache prompt sections, group
                   interjection prompts, anti-repetition builders
``outbound``       reply cleaning/anti-echo guard, bubble splitting, memes, voice and the unified
                   WCF-or-LangBot bubble dispatcher
``commands``       owner authorization, owner moments/friend commands, wake/mute routing and the
                   unified prefix command dispatcher
=================  ==========================================================================

Mock compatibility: the facades call :func:`install_facade`, which turns the facade module into a
:class:`_FacadeModule`. Assigning an attribute on a facade (which is what ``unittest.mock.patch.object``
does) also rebinds that name in every whale_core module that currently holds the *same* object, so
``patch.object(whale_listener, "STATE_FILE", ...)`` or ``patch.object(diana_features,
"_is_safe_public_url", ...)`` reaches the extracted code exactly as it reached the old monoliths.
Listener code that used to call ``df.xxx`` / ``ui.xxx`` still resolves those attributes at call time
through the :data:`df` / :data:`ui` proxies below.

This package intentionally contains no ``*.yaml`` so LangBot component discovery never treats it as a
component directory.
"""
from __future__ import annotations

import importlib
import types
from typing import Any

_MISSING = object()
_REGISTRY: dict[str, types.ModuleType] = {}


class _FacadeRef:
    """Late-bound stand-in for a facade module (``df`` / ``ui``).

    Attribute access is resolved on every call against the registered facade module, so patches
    applied to ``diana_features`` / ``interaction_settings`` are honoured by whale_core code.
    """

    __slots__ = ("_facade_name",)

    def __init__(self, facade_name: str) -> None:
        object.__setattr__(self, "_facade_name", facade_name)

    def _resolve(self) -> types.ModuleType:
        name = object.__getattribute__(self, "_facade_name")
        module = _REGISTRY.get(name)
        if module is None:
            module = _import_fallback(name)
        return module

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._resolve(), attr)

    def __setattr__(self, attr: str, value: Any) -> None:
        setattr(self._resolve(), attr, value)

    def __repr__(self) -> str:
        return f"<whale_core facade proxy {object.__getattribute__(self, '_facade_name')!r}>"


def _import_fallback(name: str) -> types.ModuleType:
    """Resolve a facade that has not registered itself yet (e.g. whale_core imported directly)."""
    candidates = []
    pkg = __name__.rpartition(".")[0]
    if pkg:
        candidates.append(f"{pkg}.event_listeners.{name}")
    candidates.append(name)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            module = importlib.import_module(candidate)
        except ImportError as exc:
            last_exc = exc
            continue
        _REGISTRY.setdefault(name, module)
        return _REGISTRY[name]
    raise ImportError(f"whale_core: facade module {name!r} is not available") from last_exc


df = _FacadeRef("diana_features")
ui = _FacadeRef("interaction_settings")

# Import order is a strict dependency DAG (each module only imports from earlier ones).
from . import http_gateway  # noqa: E402
from . import mood_memory  # noqa: E402
from . import state_repo  # noqa: E402
from . import media_parsers  # noqa: E402
from . import prompts  # noqa: E402
from . import outbound  # noqa: E402
from . import commands  # noqa: E402

_MODULES: tuple[types.ModuleType, ...] = (
    http_gateway,
    mood_memory,
    state_repo,
    media_parsers,
    prompts,
    outbound,
    commands,
)
_MODULES_BY_NAME = {m.__name__.rpartition(".")[2]: m for m in _MODULES}


def _propagate(name: str, old: Any, new: Any) -> None:
    for module in _MODULES:
        namespace = module.__dict__
        if namespace.get(name, _MISSING) is old:
            namespace[name] = new


class _FacadeModule(types.ModuleType):
    """Module type for the compatibility facades: attribute writes propagate into whale_core."""

    def __setattr__(self, name: str, value: Any) -> None:
        old = self.__dict__.get(name, _MISSING)
        if old is not _MISSING and old is not value and not name.startswith("__"):
            _propagate(name, old, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        old = self.__dict__.get(name, _MISSING)
        super().__delattr__(name)
        if old is not _MISSING and not name.startswith("__"):
            for module in _MODULES:
                if module.__dict__.get(name, _MISSING) is old:
                    del module.__dict__[name]


def register_module(name: str, module: types.ModuleType) -> None:
    """Register the module the ``df`` / ``ui`` proxies should resolve to."""
    _REGISTRY[name] = module


def install_facade(module: types.ModuleType, name: str, exports: dict[str, tuple[str, ...]]) -> None:
    """Populate ``module`` with ``exports`` (``{core_module: names}``) and make it a live facade."""
    namespace = module.__dict__
    for core_name, names in exports.items():
        source = _MODULES_BY_NAME[core_name]
        for attr in names:
            namespace[attr] = getattr(source, attr)
    if not isinstance(module, _FacadeModule):
        module.__class__ = _FacadeModule
    register_module(name, module)


__all__ = [
    "commands",
    "df",
    "http_gateway",
    "install_facade",
    "media_parsers",
    "mood_memory",
    "outbound",
    "prompts",
    "register_module",
    "state_repo",
    "ui",
]
