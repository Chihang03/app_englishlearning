from __future__ import annotations

import ctypes
import sys
from ctypes import util


K_CF_STRING_ENCODING_UTF8 = 0x08000100


class CFRange(ctypes.Structure):
    _fields_ = [("location", ctypes.c_long), ("length", ctypes.c_long)]


def lookup_system_definition(word: str) -> str | None:
    """Look a word up in the macOS system dictionary.

    DictionaryServices only exists on macOS, so on any other platform this
    returns None and the caller reports the lookup as unavailable.
    """
    if sys.platform != "darwin":
        return None

    query = word.strip()
    if not query:
        return None

    core_foundation_path = util.find_library("CoreFoundation")
    core_services_path = util.find_library("CoreServices")
    if not core_foundation_path or not core_services_path:
        return None

    try:
        core_foundation = ctypes.CDLL(core_foundation_path)
        core_services = ctypes.CDLL(core_services_path)
        core_foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        core_foundation.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        core_foundation.CFStringGetCString.restype = ctypes.c_bool
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        core_services.DCSCopyTextDefinition.argtypes = [ctypes.c_void_p, ctypes.c_void_p, CFRange]
        core_services.DCSCopyTextDefinition.restype = ctypes.c_void_p

        text_ref = core_foundation.CFStringCreateWithCString(
            None,
            query.encode("utf-8"),
            K_CF_STRING_ENCODING_UTF8,
        )
        if not text_ref:
            return None
        definition_ref = core_services.DCSCopyTextDefinition(None, text_ref, CFRange(0, len(query)))
        core_foundation.CFRelease(text_ref)
        if not definition_ref:
            return None

        buffer = ctypes.create_string_buffer(32768)
        ok = core_foundation.CFStringGetCString(
            definition_ref,
            buffer,
            len(buffer),
            K_CF_STRING_ENCODING_UTF8,
        )
        core_foundation.CFRelease(definition_ref)
        if not ok:
            return None
        definition = buffer.value.decode("utf-8", errors="ignore").strip()
        return definition or None
    except Exception:
        return None
