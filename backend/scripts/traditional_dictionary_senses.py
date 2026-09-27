"""Read Dr. Eye meaning groups and convert their glosses with macOS ICU."""
from __future__ import annotations

import ctypes
import hashlib

from lxml import html
from dictionary_senses import ancestor, example, nodes, owned, text

POS = {"n.": "名词", "vt.": "及物动词", "vi.": "不及物动词", "v.": "动词",
       "a.": "形容词", "adj.": "形容词", "ad.": "副词", "adv.": "副词",
       "pron.": "代词", "prep.": "介词", "conj.": "连词", "interj.": "感叹词", "num.": "数词"}


def parse_traditional_record(markup: str, word: str, aliases: list[str]) -> list[dict]:
    root = html.fromstring(markup)
    senses = []
    for group in nodes(root, "se2"):
        block = ancestor(group, ("se1",))
        if block is None or ancestor(group, ("subEntry",)) is not None:
            continue
        labels = owned(block, "pos", ("se1",))
        pos = POS.get(text(labels[0]).casefold()) if len(labels) == 1 else None
        if not pos:
            continue
        translations = [text(n) for n in nodes(group, "trans")
                        if ancestor(n, ("se2",)) is group and ancestor(n, ("eg",)) is None]
        gloss = "、".join(dict.fromkeys(translations))
        if not gloss:
            continue
        examples = []
        for eg in owned(group, "eg", ("se2",)):
            sentence = nodes(eg, "ex")
            if sentence:
                item = example(text(sentence[0]), word, aliases, "verb" if "动词" in pos else "noun")
                if item:
                    examples.append(item)
        senses.append({"key": "zh_tw:" + hashlib.sha256(f"{word}|{pos}|{gloss}".encode()).hexdigest()[:24],
                       "part_of_speech": pos, "definition_cn": gloss, "definition_en": None, "examples": examples})
    return senses


def simplify_chinese(values: list[str]) -> list[str]:
    """Use the OS's Traditional-Simplified transliteration, without translation."""
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = pointer
    cf.CFStringCreateMutableCopy.argtypes = [pointer, ctypes.c_long, pointer]
    cf.CFStringCreateMutableCopy.restype = pointer
    cf.CFStringTransform.argtypes = [pointer, pointer, pointer, ctypes.c_bool]
    cf.CFStringTransform.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [pointer]
    utf8 = 0x08000100
    transform = cf.CFStringCreateWithCString(None, b"Traditional-Simplified", utf8)
    output = []
    try:
        for value in values:
            original = cf.CFStringCreateWithCString(None, value.encode(), utf8)
            mutable = cf.CFStringCreateMutableCopy(None, 0, original)
            try:
                buffer = ctypes.create_string_buffer(len(value.encode()) * 4 + 64)
                if not cf.CFStringTransform(mutable, None, transform, False) or not cf.CFStringGetCString(mutable, buffer, len(buffer), utf8):
                    raise ValueError("macOS Chinese character conversion failed")
                output.append(buffer.value.decode())
            finally:
                cf.CFRelease(mutable)
                cf.CFRelease(original)
    finally:
        cf.CFRelease(transform)
    return output
