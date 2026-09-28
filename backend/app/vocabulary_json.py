"""Readable vocabulary files and fingerprints independent of JSON whitespace."""
from __future__ import annotations

import hashlib
import json


def readable_json(value, *, sort_keys: bool = False) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"


def json_content_bytes(document: bytes) -> bytes:
    # Keep object and array order. Only serialization whitespace is removed.
    if not document:
        return b""
    return json.dumps(json.loads(document), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def json_fingerprint(*documents: bytes) -> str:
    return hashlib.sha256(b"\0".join(json_content_bytes(document) for document in documents)).hexdigest()


def legacy_catalog_fingerprints(catalog: bytes, seed: bytes) -> set[str]:
    """Recognize the previous compact catalog and indented starter export."""
    compact = json_content_bytes(catalog)
    catalogs = {catalog, compact, compact + b"\n"}
    seeds = {seed, readable_json(json.loads(seed)).encode("utf-8")}
    return {hashlib.sha256(old_catalog + b"\0" + old_seed).hexdigest()
            for old_catalog in catalogs for old_seed in seeds}


def legacy_morphology_fingerprints(bundle: bytes, catalog: bytes) -> set[str]:
    compact_bundle, compact_catalog = json_content_bytes(bundle), json_content_bytes(catalog)
    bundles = {bundle, compact_bundle, compact_bundle + b"\n"} if bundle else {b""}
    catalogs = {catalog, compact_catalog, compact_catalog + b"\n"}
    return {hashlib.sha256(old_bundle + b"\0" + old_catalog).hexdigest()
            for old_bundle in bundles for old_catalog in catalogs}


def catalog_hashes(catalog: bytes) -> set[str]:
    """Accept old raw catalog bindings only for the same JSON content."""
    compact = json_content_bytes(catalog)
    return {hashlib.sha256(document).hexdigest() for document in (catalog, compact, compact + b"\n")}
