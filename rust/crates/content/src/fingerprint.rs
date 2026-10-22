//! Streaming Python-compatible compact JSON fingerprints, without a whole-document Value tree.
use anyhow::{Result, bail, ensure};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
};

struct Parser<R> {
    source: R,
    total: u64,
    keys_bytes: usize,
}
impl<R: BufRead> Parser<R> {
    fn take(&mut self) -> Result<Option<u8>> {
        let byte = self.source.fill_buf()?.first().copied();
        if byte.is_some() {
            self.source.consume(1);
            self.total += 1;
            ensure!(self.total <= 512 * 1024 * 1024, "JSON input exceeds budget");
        }
        Ok(byte)
    }
    fn peek(&mut self) -> Result<Option<u8>> {
        loop {
            let byte = self.source.fill_buf()?.first().copied();
            if byte.is_some_and(|b| b" \n\r\t".contains(&b)) {
                self.take()?;
            } else {
                return Ok(byte);
            }
        }
    }
    fn expect(&mut self, byte: u8) -> Result<()> {
        self.peek()?;
        ensure!(self.take()? == Some(byte), "invalid JSON delimiter");
        Ok(())
    }
    fn string(&mut self) -> Result<String> {
        self.expect(b'"')?;
        let mut raw = vec![b'"'];
        let mut escape = false;
        loop {
            let byte = self
                .take()?
                .ok_or_else(|| anyhow::anyhow!("truncated string"))?;
            raw.push(byte);
            ensure!(raw.len() <= 2 * 1024 * 1024, "JSON string exceeds budget");
            if !escape && byte == b'"' {
                break;
            }
            escape = !escape && byte == b'\\';
        }
        Ok(serde_json::from_slice(&raw)?)
    }
    fn value<W: Write>(&mut self, out: &mut W, depth: usize) -> Result<()> {
        ensure!(depth <= 64, "JSON nesting exceeds budget");
        match self
            .peek()?
            .ok_or_else(|| anyhow::anyhow!("missing JSON value"))?
        {
            b'{' => {
                self.expect(b'{')?;
                out.write_all(b"{")?;
                let mut seen = std::collections::BTreeSet::new();
                let mut count = 0;
                let mut local_keys = 0;
                if self.peek()? != Some(b'}') {
                    loop {
                        if count > 0 {
                            self.expect(b',')?;
                            out.write_all(b",")?;
                        }
                        let key = self.string()?;
                        self.keys_bytes += key.len();
                        local_keys += key.len();
                        ensure!(
                            self.keys_bytes <= 4 * 1024 * 1024,
                            "JSON keys exceed memory budget"
                        );
                        ensure!(seen.insert(key.clone()), "duplicate JSON key");
                        ensure!(seen.len() <= 100000, "too many object keys");
                        serde_json::to_writer(&mut *out, &key)?;
                        self.expect(b':')?;
                        out.write_all(b":")?;
                        self.value(out, depth + 1)?;
                        count += 1;
                        if self.peek()? != Some(b',') {
                            break;
                        }
                    }
                }
                self.expect(b'}')?;
                out.write_all(b"}")?;
                self.keys_bytes -= local_keys;
            }
            b'[' => {
                self.expect(b'[')?;
                out.write_all(b"[")?;
                let mut count = 0;
                if self.peek()? != Some(b']') {
                    loop {
                        if count > 0 {
                            self.expect(b',')?;
                            out.write_all(b",")?;
                        }
                        self.value(out, depth + 1)?;
                        count += 1;
                        if self.peek()? != Some(b',') {
                            break;
                        }
                    }
                }
                self.expect(b']')?;
                out.write_all(b"]")?;
            }
            b'"' => {
                serde_json::to_writer(&mut *out, &self.string()?)?;
            }
            _ => {
                let mut bytes = Vec::new();
                while self
                    .source
                    .fill_buf()?
                    .first()
                    .is_some_and(|b| !b" \n\r\t,:]}".contains(b))
                {
                    bytes.push(self.take()?.unwrap());
                    ensure!(bytes.len() <= 4096, "JSON token exceeds budget");
                }
                let token = std::str::from_utf8(&bytes)?;
                if ["true", "false", "null"].contains(&token) {
                    out.write_all(&bytes)?;
                } else {
                    static NUMBER: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
                    ensure!(
                        NUMBER
                            .get_or_init(|| regex::Regex::new(
                                r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$"
                            )
                            .unwrap())
                            .is_match(token),
                        "invalid JSON token"
                    );
                    if token.contains(['.', 'e', 'E']) {
                        let value = token.parse::<f64>()?;
                        ensure!(value.is_finite(), "non-finite JSON number");
                        out.write_all(python_float(value).as_bytes())?;
                    } else {
                        out.write_all(if token == "-0" { b"0" } else { &bytes })?;
                    }
                }
            }
        }
        Ok(())
    }
}
fn python_float(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    if value.abs() < 1e-4 || value.abs() >= 1e16 {
        let raw = format!("{value:e}");
        let (mantissa, exponent) = raw.split_once('e').expect("exponent format");
        let exponent: i32 = exponent.parse().expect("numeric exponent");
        format!("{mantissa}e{exponent:+03}")
    } else {
        let mut text = value.to_string();
        if !text.contains('.') {
            text.push_str(".0");
        }
        text
    }
}
struct Sink(Sha256);
impl Write for Sink {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
pub fn fingerprint(paths: &[PathBuf]) -> Result<String> {
    let mut sink = Sink(Sha256::new());
    for (index, path) in paths.iter().enumerate() {
        if index > 0 {
            sink.write_all(&[0])?;
        }
        let file = File::open(path)?;
        if file.metadata()?.len() == 0 {
            continue;
        }
        let mut reader = BufReader::with_capacity(65536, file);
        if reader.fill_buf()?.starts_with(&[0xef, 0xbb, 0xbf]) {
            reader.consume(3);
        }
        let mut parser = Parser {
            source: reader,
            total: 0,
            keys_bytes: 0,
        };
        parser.value(&mut sink, 0)?;
        if parser.peek()?.is_some() {
            bail!("unexpected trailing JSON data");
        }
    }
    Ok(format!("{:x}", sink.0.finalize()))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matches_python_float_notation() {
        for (value, text) in [
            (1e-6, "1e-06"),
            (1e16, "1e+16"),
            (1.0, "1.0"),
            (0.0001, "0.0001"),
            (-0.0, "-0.0"),
        ] {
            assert_eq!(python_float(value), text);
        }
    }
}
