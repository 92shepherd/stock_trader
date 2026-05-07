"""Shared utilities for DART collectors.

Centralized so all DART collectors (corp_codes, disclosures, financials,
indicators) get the same import-resolution behavior and API-key check.
"""
from __future__ import annotations

from src.config import get_app_config


def get_dart_client():
    """Lazy import + construct OpenDartReader.

    Lazy because OpenDartReader's constructor downloads the corp_code
    ZIP on first call (and caches it under ./docs_cache/), so we only
    want to pay that cost when a DART collector actually runs.

    Import compatibility:
        OpenDartReader's package layout has been inconsistent across
        versions — sometimes the class is exposed at the package root,
        sometimes only in a `dart` submodule, and case-sensitivity on
        Windows can further confuse things. We try the known patterns
        in order and use the first one that yields the class.

    Raises:
        RuntimeError: if DART_API_KEY is unset.
        ImportError:  if no known import path resolves to the class.
    """
    cfg = get_app_config()
    api_key = cfg.dart.api_key
    if not api_key:
        raise RuntimeError(
            "DART_API_KEY is not set. Get a key from https://opendart.fss.or.kr/ "
            "and add it to .env as DART_API_KEY=..."
        )

    _ODR = None
    _errors: list[str] = []
    for import_path in (
        "OpenDartReader.dart",   # newer layouts: class in dart submodule
        "OpenDartReader",        # older layouts: class at package root
        "opendartreader.dart",   # all-lowercase variant
        "opendartreader",
    ):
        try:
            module = __import__(import_path, fromlist=["OpenDartReader"])
            _ODR = getattr(module, "OpenDartReader", None)
            if _ODR is not None:
                break
        except ImportError as e:
            _errors.append(f"{import_path}: {e}")

    if _ODR is None:
        raise ImportError(
            "Could not locate the OpenDartReader class in any known import "
            "path. Reinstall with `pip install -U opendartreader` and verify "
            "with `python -c \"import OpenDartReader; print(dir(OpenDartReader))\"`. "
            f"Tried paths: {_errors}"
        )
    return _ODR(api_key)
