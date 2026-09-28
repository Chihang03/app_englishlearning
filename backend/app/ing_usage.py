"""Conservative traditional usage hints for the exact blank in a fixed sentence."""
from __future__ import annotations

import re


_ADVERBS = r"(?:(?:not|never|always|already|still|just|only|really|now|currently|constantly|continually|often|also)\s+){0,4}"
_BE = r"(?:am|is|are|was|were|be|been|being|isn't|aren't|wasn't|weren't)"
_CONTRACTION = r"(?:i'm|(?:he|she|it|that|there|what|who|how)'s|(?:you|we|they)'re)"
_SUBJECT = r"(?:i|you|he|she|it|we|they)"
_GERUND_VERBS = r"(?:enjoy(?:s|ed)?|avoid(?:s|ed)?|finish(?:es|ed)?|consider(?:s|ed)?|suggest(?:s|ed)?|mind(?:s|ed)?|admit(?:s|ted)?|deny|denies|denied|postpone(?:s|d)?|risk(?:s|ed)?|practi[cs](?:e|es|ed)|quit(?:s)?|keep|keeps|kept|stop(?:s|ped)?|miss(?:es|ed)?|recommend(?:s|ed)?|resist(?:s|ed)?|imagine(?:s|d)?|appreciate(?:s|d)?|tolerate(?:s|d)?|like(?:s|d)?|love(?:s|d)?|hate(?:s|d)?)"
_PREPOSITIONS = r"(?:of|for|by|without|despite|after|before|in|on|at|with|from|about|against|upon|besides)"
# A copula before these forms can describe an adjective rather than an action.
_ADJECTIVAL_VERBS = {"interest", "bore", "excite", "amaze", "surprise", "charm", "alarm",
                     "confuse", "satisfy", "tire", "disappoint", "disgust", "please", "shock"}


def _occurrence_label(sentence: str, match: re.Match, word: str) -> str:
    before, after = sentence[:match.start()], sentence[match.end():]
    progressive = (re.search(rf"\b{_BE}\s+{_ADVERBS}$",before) or
                   re.search(rf"\b{_CONTRACTION}\s+{_ADVERBS}$",before) or
                   re.search(rf"\b{_BE}\s+{_SUBJECT}\s+{_ADVERBS}$",before))
    if progressive and word not in _ADJECTIVAL_VERBS:
        return "现在分词·进行时"
    if (re.search(rf"\b{_PREPOSITIONS}\s+{_ADVERBS}$",before) or
            re.search(rf"\b{_GERUND_VERBS}\s+{_ADVERBS}$",before) or
            re.search(r"\b(?:look(?:s|ed)? forward to|object(?:s|ed)? to|accustomed to|used to|no point|no use|no sense|no)\s+$",before)):
        return "动名词"
    # A plain subject (or possessive subject), followed immediately by a finite
    # predicate, is clear. Longer phrases can be reduced participial clauses.
    if re.fullmatch(r"\s*(?:(?:my|your|his|her|our|their|its)\s+)?",before) and re.match(
            r"\s+(?:is|was|has|had|can|will|would|makes|takes|helps)\b",after):
        return "动名词"
    if re.search(r"\b(?:saw|see|sees|seen|hear|hears|heard|watch|watches|watched|find|finds|found|catch|catches|caught)\s+(?:me|you|him|her|us|them)\s+$",before):
        return "现在分词"
    if re.search(r"\b(?:came|come|comes|went|go|goes|ran|run|runs|rushed)\s+$",before):
        return "现在分词"
    if not before.strip() and re.search(rf",\s*{_SUBJECT}\s+(?:{_BE}|have|has|had|can|could|will|would)\b",after):
        return "现在分词"
    return "-ing 形式"


def ing_usage_label(sentence: str, target: str, word: str) -> str:
    sentence = sentence.casefold().replace("’", "'")
    target = target.casefold().replace("’", "'")
    labels = {_occurrence_label(sentence, match, word.casefold()) for match in
              re.finditer(rf"(?<![A-Za-z'-]){re.escape(target)}(?![A-Za-z'-])",sentence)}
    # The app blanks every matching occurrence. Do not claim a single usage
    # when the same spelling plays different roles in that sentence.
    return labels.pop() if len(labels) == 1 else "-ing 形式"
