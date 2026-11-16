"""Real captures of the operator's 1C window are not in the public repository.

They show the company's catalogue, prices and stock. Tests that read them are skipped
when the files are missing; everything else runs as usual.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PRIVATE = [(ROOT / "layouts" / "shots").resolve(), (ROOT / "tests" / "fixtures").resolve()]
REASON = "needs real 1C captures, kept out of the public repository"


def _missing_capture(exc: BaseException | None) -> bool:
    while exc is not None:
        if isinstance(exc, FileNotFoundError) and exc.filename:
            path = Path(exc.filename).resolve()
            if path.suffix == ".png" and any(p in path.parents for p in PRIVATE):
                return True
        exc = exc.__cause__ or exc.__context__
    return False


def _run():
    try:
        return (yield)
    except Exception as e:
        if _missing_capture(e):
            pytest.skip(REASON)
        raise


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    return (yield from _run())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    return (yield from _run())
