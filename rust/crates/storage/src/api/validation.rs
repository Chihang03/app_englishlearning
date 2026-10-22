//! Request normalization follows the frozen OpenAPI models and field order.
use super::*;
use std::sync::OnceLock;
fn schema() -> &'static Value {
    static DATA: OnceLock<Value> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../fixtures/openapi.json"))
            .expect("frozen schema")
    })
}
fn orders() -> &'static Value {
    static DATA: OnceLock<Value> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("../../../../fixtures/field-order.json"))
            .expect("frozen field order")
    })
}
fn problem(
    errors: &mut Vec<Value>,
    kind: &str,
    msg: String,
    loc: &[Value],
    input: &Value,
    ctx: Option<Value>,
) {
    let mut value = json!({"type":kind,"loc":loc,"msg":msg,"input":input});
    if let Some(ctx) = ctx {
        value["ctx"] = ctx;
    }
    errors.push(value);
}
fn item(value: &Value, rule: &Value, loc: &[Value], errors: &mut Vec<Value>) -> Value {
    if let Some(reference) = rule["$ref"].as_str() {
        return model(value, reference.rsplit('/').next().unwrap(), loc, errors);
    }
    if let Some(options) = rule["anyOf"].as_array() {
        if value.is_null() && options.iter().any(|r| r["type"] == "null") {
            return Value::Null;
        }
        if let Some(rule) = options.iter().find(|r| r["type"] != "null") {
            return item(value, rule, loc, errors);
        }
    }
    if let Some(allowed) = rule["enum"].as_array() {
        if let Some(candidate) = allowed.iter().find(|candidate| {
            **candidate == *value
                || (candidate.is_number()
                    && value.is_number()
                    && candidate.as_f64() == value.as_f64())
        }) {
            return candidate.clone();
        }
        let options = allowed
            .iter()
            .map(|v| {
                if let Some(s) = v.as_str() {
                    format!("'{s}'")
                } else {
                    v.to_string()
                }
            })
            .collect::<Vec<_>>();
        let expected = if options.len() > 1 {
            format!(
                "{} or {}",
                options[..options.len() - 1].join(", "),
                options.last().unwrap()
            )
        } else {
            options.join("")
        };
        problem(
            errors,
            "literal_error",
            format!("Input should be {expected}"),
            loc,
            value,
            Some(json!({"expected":expected})),
        );
        return value.clone();
    }
    match rule["type"].as_str().unwrap_or("") {
        "string" => {
            let Some(text) = value.as_str() else {
                problem(
                    errors,
                    "string_type",
                    "Input should be a valid string".into(),
                    loc,
                    value,
                    None,
                );
                return value.clone();
            };
            let length = text.chars().count();
            for (key, kind, word, ctx) in [
                ("minLength", "string_too_short", "at least", "min_length"),
                ("maxLength", "string_too_long", "at most", "max_length"),
            ] {
                if let Some(limit) = rule[key].as_u64() {
                    if (key == "minLength" && length < limit as usize)
                        || (key == "maxLength" && length > limit as usize)
                    {
                        problem(
                            errors,
                            kind,
                            format!(
                                "String should have {word} {limit} character{}",
                                if limit == 1 { "" } else { "s" }
                            ),
                            loc,
                            value,
                            Some(json!({ctx:limit})),
                        );
                        return value.clone();
                    }
                }
            }
            if let Some(pattern) = rule["pattern"].as_str() {
                if !regex::Regex::new(pattern).is_ok_and(|regex| regex.is_match(text)) {
                    problem(
                        errors,
                        "string_pattern_mismatch",
                        format!("String should match pattern '{pattern}'"),
                        loc,
                        value,
                        Some(json!({"pattern":pattern})),
                    );
                }
            }
            value.clone()
        }
        "integer" => {
            let parsed = value
                .as_i64()
                .or_else(|| value.as_bool().map(i64::from))
                .or_else(|| {
                    value
                        .as_f64()
                        .filter(|n| n.is_finite() && n.fract() == 0.0)
                        .map(|n| n as i64)
                })
                .or_else(|| {
                    value.as_str().and_then(|s| {
                        s.trim().parse::<i64>().ok().or_else(|| {
                            s.trim()
                                .parse::<f64>()
                                .ok()
                                .filter(|n| n.is_finite() && n.fract() == 0.0)
                                .map(|n| n as i64)
                        })
                    })
                });
            let Some(number) = parsed else {
                let (kind, msg) = if value.is_string() {
                    (
                        "int_parsing",
                        "Input should be a valid integer, unable to parse string as an integer",
                    )
                } else if value.is_number() {
                    (
                        "int_from_float",
                        "Input should be a valid integer, got a number with a fractional part",
                    )
                } else {
                    ("int_type", "Input should be a valid integer")
                };
                problem(errors, kind, msg.into(), loc, value, None);
                return value.clone();
            };
            for (key, kind, comparison, ctx) in [
                (
                    "minimum",
                    "greater_than_equal",
                    "greater than or equal to",
                    "ge",
                ),
                ("exclusiveMinimum", "greater_than", "greater than", "gt"),
                ("maximum", "less_than_equal", "less than or equal to", "le"),
            ] {
                if let Some(limit) = rule[key].as_i64() {
                    let invalid = match key {
                        "minimum" => number < limit,
                        "exclusiveMinimum" => number <= limit,
                        _ => number > limit,
                    };
                    if invalid {
                        problem(
                            errors,
                            kind,
                            format!("Input should be {comparison} {limit}"),
                            loc,
                            value,
                            Some(json!({ctx:limit})),
                        );
                        return json!(number);
                    }
                }
            }
            json!(number)
        }
        "boolean" => {
            let parsed = value
                .as_bool()
                .or_else(|| {
                    value
                        .as_i64()
                        .filter(|n| [0, 1].contains(n))
                        .map(|n| n == 1)
                })
                .or_else(|| {
                    value
                        .as_str()
                        .and_then(|s| match s.to_ascii_lowercase().as_str() {
                            "true" | "1" | "yes" | "y" | "on" | "t" => Some(true),
                            "false" | "0" | "no" | "n" | "off" | "f" => Some(false),
                            _ => None,
                        })
                });
            if let Some(value) = parsed {
                json!(value)
            } else {
                let (kind, msg) = if value.is_string() || value.is_number() {
                    (
                        "bool_parsing",
                        "Input should be a valid boolean, unable to interpret input",
                    )
                } else {
                    ("bool_type", "Input should be a valid boolean")
                };
                problem(errors, kind, msg.into(), loc, value, None);
                value.clone()
            }
        }
        "array" => {
            let Some(items) = value.as_array() else {
                problem(
                    errors,
                    "list_type",
                    "Input should be a valid list".into(),
                    loc,
                    value,
                    None,
                );
                return value.clone();
            };
            if let Some(max) = rule["maxItems"].as_u64() {
                if items.len() > max as usize {
                    problem(
                        errors,
                        "too_long",
                        format!(
                            "List should have at most {max} items after validation, not {}",
                            items.len()
                        ),
                        loc,
                        value,
                        Some(
                            json!({"field_type":"List","max_length":max,"actual_length":items.len()}),
                        ),
                    );
                    return value.clone();
                }
            }
            json!(
                items
                    .iter()
                    .enumerate()
                    .map(|(i, v)| {
                        let mut loc = loc.to_vec();
                        loc.push(json!(i));
                        item(v, &rule["items"], &loc, errors)
                    })
                    .collect::<Vec<_>>()
            )
        }
        "object" => {
            if !value.is_object() {
                problem(
                    errors,
                    "dict_type",
                    "Input should be a valid dictionary".into(),
                    loc,
                    value,
                    None,
                );
            }
            value.clone()
        }
        _ => value.clone(),
    }
}
fn model(value: &Value, name: &str, loc: &[Value], errors: &mut Vec<Value>) -> Value {
    let rule = &schema()["components"]["schemas"][name];
    if !value.is_object() {
        problem(
            errors,
            "model_attributes_type",
            "Input should be a valid dictionary or object to extract fields from".into(),
            loc,
            value,
            None,
        );
        return value.clone();
    }
    let mut result = Map::new();
    let required = rule["required"].as_array().cloned().unwrap_or_default();
    for key in orders()[name]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        let field = &rule["properties"][key];
        let mut loc = loc.to_vec();
        loc.push(json!(key));
        if let Some(input) = value.get(key) {
            result.insert(key.into(), item(input, field, &loc, errors));
        } else if required.contains(&json!(key)) {
            problem(
                errors,
                "missing",
                "Field required".into(),
                &loc,
                value,
                None,
            );
        } else if let Some(default) = field.get("default") {
            result.insert(key.into(), default.clone());
        }
    }
    Value::Object(result)
}
pub fn normalize(path: &str, method: &str, input: &Value) -> Result<Value> {
    let name = match (method, path) {
        ("POST", "/api/auth/register") => "RegisterInput",
        ("POST", "/api/auth/login") => "LoginInput",
        ("PATCH", "/api/auth/me") => "ProfileInput",
        ("PATCH", "/api/auth/password") => "PasswordInput",
        ("PATCH", "/api/settings") => "SettingsInput",
        ("POST", "/api/review") => "ReviewInput",
        ("POST", "/api/study/hint") => "HintInput",
        ("POST", "/api/study/related-exposure") => "ExposureInput",
        ("POST", "/api/study/meanings") => "StudyDetailsInput",
        ("POST", "/api/content-reports") => "ContentReportInput",
        ("POST", "/api/auth/passkeys/register/options") => "RegistrationInput",
        ("POST", "/api/auth/passkeys/register/verify" | "/api/auth/passkeys/login/verify") => {
            "CredentialInput"
        }
        ("DELETE", p) if p.starts_with("/api/auth/passkeys/") => "ManageInput",
        _ => return Ok(input.clone()),
    };
    let mut errors = Vec::new();
    let result = model(input, name, &[json!("body")], &mut errors);
    if errors.is_empty() {
        Ok(result)
    } else {
        Err(Fault {
            status: 422,
            detail: "Validation failed".into(),
            validation: Some(json!(errors)),
        })
    }
}
