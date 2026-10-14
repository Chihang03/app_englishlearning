import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRequire } from "node:module";

const directory = mkdtempSync(join(tmpdir(), "cvt-semantic-display-"));
try {
  execFileSync("./node_modules/.bin/tsc", ["src/learningMeanings.ts", "--target", "ES2020", "--module", "commonjs", "--skipLibCheck", "--outDir", directory]);
  const { learningMeanings, chineseMeaning } = createRequire(import.meta.url)(join(directory, "learningMeanings.js"));
  const a = { id: 1, learning_unit_id: 8, part_of_speech: "形容词", definition_cn: "内行的",
    core_definition_cn: "专业的", core_definition_en: "competent or skilled", status: "New",
    semantic_notes: "可形容人的能力或工作的水准。", examples: [{ id: 1, sentence: "A professional job.", translation_cn: "专业的工作。", target_form: "professional" }] };
  const b = { ...a, id: 2, definition_cn: "精通业务的", examples: [{ ...a.examples[0], id: 2 }] };
  const independent = { ...a, id: 3, learning_unit_id: 9, part_of_speech: "名词", core_definition_cn: "专业人士" };
  const original = JSON.stringify([a,b,independent]);
  const shown = learningMeanings([a,b,independent]);
  assert.equal(shown.length, 2);
  assert.equal(shown[0].definition_cn, "专业的");
  assert.equal(shown[0].definition_en, "competent or skilled");
  assert.equal(shown[0].examples.length, 1);
  assert.equal(shown[0].semantic_notes, a.semantic_notes);
  assert.equal(JSON.stringify([a,b,independent]), original, "Cached source data must not be mutated");
  assert.equal(chineseMeaning("a person who works"), "待核查");
  assert.equal(chineseMeaning("专业的"), "专业的");
  assert.equal(learningMeanings([{ ...a, core_definition_cn: null }, b]).length, 2);
  console.log("PASS core groups, independent POS, example deduplication, bilingual display, source cache preservation");
} finally { rmSync(directory, { recursive: true, force: true }); }
