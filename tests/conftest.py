"""Test configuration: coroutine tests run in a fresh event loop each, without a plugin."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments: dict[str, Any] = {
        name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(asyncio.wait_for(pyfuncitem.obj(**arguments), timeout=120))
    return True
