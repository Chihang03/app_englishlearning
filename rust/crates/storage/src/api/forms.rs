use super::*;
use regex::Regex;

pub fn occurrences(sentence: &str, target: &str) -> Vec<std::ops::Range<usize>> {
    if target.is_empty() {
        return Vec::new();
    }
    let Ok(regex) = Regex::new(&format!("(?i){}", regex::escape(target))) else {
        return Vec::new();
    };
    let boundary = |c: char| c.is_ascii_alphabetic() || "'-".contains(c);
    regex
        .find_iter(sentence)
        .filter(|m| {
            !sentence[..m.start()]
                .chars()
                .next_back()
                .is_some_and(boundary)
                && !sentence[m.end()..].chars().next().is_some_and(boundary)
        })
        .map(|m| m.range())
        .collect()
}
pub fn cloze(sentence: &str, target: &str) -> String {
    let mut result = String::new();
    let mut previous = 0;
    for range in occurrences(sentence, target) {
        result.push_str(&sentence[previous..range.start]);
        result.push_str("_______");
        previous = range.end;
    }
    result.push_str(&sentence[previous..]);
    result
}
fn family(pos: &str) -> &'static str {
    for (family, cn, en) in [
        ("adverb", "副词", "adverb"),
        ("verb", "动词", "verb"),
        ("pronoun", "代词", "pronoun"),
        ("numeral", "数词", "numeral"),
        ("noun", "名词", "noun"),
        ("adjective", "形容词", "adjective"),
    ] {
        if pos.contains(cn) || Regex::new(&format!(r"(?i)\b{en}\b")).unwrap().is_match(pos) {
            return family;
        }
    }
    ""
}
pub fn label(c: &Connection, word: &str, form: &str, pos: &str, sentence: &str) -> Result<Value> {
    let (word, form) = (word.to_lowercase(), form.to_lowercase());
    if word == form {
        return Ok(Value::Null);
    }
    let kinds = rows(
        c,
        "SELECT DISTINCT f.form_type FROM word_forms f JOIN lexical_units u ON u.id=f.lexical_unit_id WHERE lower(u.headword)=? AND f.normalized_form=? AND u.pos_group=? AND f.active=1 ORDER BY f.id",
        &[json!(word), json!(form), json!(family(pos))],
    )?;
    let mut kinds = kinds
        .iter()
        .map(|v| s(v, "form_type").to_owned())
        .collect::<Vec<_>>();
    if kinds.is_empty() && !word.is_empty() {
        let family = family(pos);
        let vowels = "aeiou";
        let last = word.chars().next_back().unwrap();
        let prefix = word.strip_suffix(last).unwrap();
        let mut s_forms = vec![format!("{word}s"), format!("{word}es")];
        if last == 'y'
            && !prefix
                .chars()
                .next_back()
                .is_some_and(|c| vowels.contains(c))
        {
            s_forms.push(format!("{prefix}ies"));
        }
        if ["noun", "pronoun", "numeral", "verb"].contains(&family) && s_forms.contains(&form) {
            kinds.push(
                if family == "verb" {
                    "third_person_singular"
                } else {
                    "plural"
                }
                .into(),
            );
        } else if family == "verb" {
            let mut ing = vec![format!("{word}ing")];
            let mut past = vec![format!("{word}ed")];
            if last == 'e' {
                past.push(format!("{word}d"));
                if !word.ends_with("ee") {
                    ing.push(format!("{prefix}ing"));
                }
            }
            if let Some(prefix) = word.strip_suffix("ie") {
                ing.push(format!("{prefix}ying"));
            }
            if last == 'y'
                && !prefix
                    .chars()
                    .next_back()
                    .is_some_and(|c| vowels.contains(c))
            {
                past.push(format!("{prefix}ied"));
            }
            if !"aeiouwxy".contains(last) {
                ing.push(format!("{word}{last}ing"));
                past.push(format!("{word}{last}ed"));
            }
            if ing.contains(&form) {
                kinds.push("ing".into());
            } else if past.contains(&form) {
                kinds.extend(["past".into(), "past_participle".into()]);
            }
        } else if ["adjective", "adverb"].contains(&family) {
            for (suffix, kind) in [("er", "comparative"), ("est", "superlative")] {
                let mut forms = vec![format!("{word}{suffix}")];
                if last == 'e' {
                    forms.push(format!("{word}{}", &suffix[1..]));
                }
                if last == 'y' {
                    forms.push(format!("{prefix}i{suffix}"));
                }
                if !"aeiouwxy".contains(last) {
                    forms.push(format!("{word}{last}{suffix}"));
                }
                if forms.contains(&form) {
                    kinds.push(kind.into());
                    break;
                }
            }
        }
    }
    if kinds == ["ing"] && !sentence.is_empty() {
        return Ok(json!(ing_label(sentence, &form, &word)));
    }
    let label = kinds
        .iter()
        .map(|k| super::lexicon::form_label(k))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("／");
    Ok(if label.is_empty() {
        Value::Null
    } else {
        json!(label)
    })
}
fn ing_label(sentence: &str, target: &str, word: &str) -> String {
    let sentence = sentence.to_lowercase().replace('’', "'");
    let target = target.to_lowercase().replace('’', "'");
    let adverbs = r"(?:(?:not|never|always|already|still|just|only|really|now|currently|constantly|continually|often|also)\s+){0,4}";
    let be = r"(?:am|is|are|was|were|be|been|being|isn't|aren't|wasn't|weren't)";
    let subject = r"(?:i|you|he|she|it|we|they)";
    let adjectives = [
        "interest",
        "bore",
        "excite",
        "amaze",
        "surprise",
        "charm",
        "alarm",
        "confuse",
        "satisfy",
        "tire",
        "disappoint",
        "disgust",
        "please",
        "shock",
    ];
    let matches = |pattern: &str, text: &str| Regex::new(pattern).is_ok_and(|r| r.is_match(text));
    let mut labels = std::collections::BTreeSet::new();
    for range in occurrences(&sentence, &target) {
        let before = &sentence[..range.start];
        let after = &sentence[range.end..];
        let progressive = matches(&format!(r"\b{be}\s+{adverbs}$"), before)
            || matches(
                &format!(
                    r"\b(?:i'm|(?:he|she|it|that|there|what|who|how)'s|(?:you|we|they)'re)\s+{adverbs}$"
                ),
                before,
            )
            || matches(&format!(r"\b{be}\s+{subject}\s+{adverbs}$"), before);
        let gerund = matches(
            &format!(
                r"\b(?:of|for|by|without|despite|after|before|in|on|at|with|from|about|against|upon|besides)\s+{adverbs}$"
            ),
            before,
        ) || matches(
            &format!(
                r"\b(?:enjoy(?:s|ed)?|avoid(?:s|ed)?|finish(?:es|ed)?|consider(?:s|ed)?|suggest(?:s|ed)?|mind(?:s|ed)?|admit(?:s|ted)?|deny|denies|denied|postpone(?:s|d)?|risk(?:s|ed)?|practi[cs](?:e|es|ed)|quit(?:s)?|keep|keeps|kept|stop(?:s|ped)?|miss(?:es|ed)?|recommend(?:s|ed)?|resist(?:s|ed)?|imagine(?:s|d)?|appreciate(?:s|d)?|tolerate(?:s|d)?|like(?:s|d)?|love(?:s|d)?|hate(?:s|d)?)\s+{adverbs}$"
            ),
            before,
        ) || matches(
            r"\b(?:look(?:s|ed)? forward to|object(?:s|ed)? to|accustomed to|used to|no point|no use|no sense|no)\s+$",
            before,
        ) || (matches(r"^\s*(?:(?:my|your|his|her|our|their|its)\s+)?$", before)
            && matches(
                r"^\s+(?:is|was|has|had|can|will|would|makes|takes|helps)\b",
                after,
            ));
        let participle = matches(
            r"\b(?:saw|see|sees|seen|hear|hears|heard|watch|watches|watched|find|finds|found|catch|catches|caught)\s+(?:me|you|him|her|us|them)\s+$",
            before,
        ) || matches(
            r"\b(?:came|come|comes|went|go|goes|ran|run|runs|rushed)\s+$",
            before,
        ) || (before.trim().is_empty()
            && matches(
                &format!(r",\s*{subject}\s+(?:{be}|have|has|had|can|could|will|would)\b"),
                after,
            ));
        labels.insert(if progressive && !adjectives.contains(&word) {
            "现在分词·进行时"
        } else if gerund {
            "动名词"
        } else if participle {
            "现在分词"
        } else {
            "-ing 形式"
        });
    }
    if labels.len() == 1 {
        labels.into_iter().next().unwrap().into()
    } else {
        "-ing 形式".into()
    }
}
