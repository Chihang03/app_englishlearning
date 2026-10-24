import type { Sense } from "./types";

// Source IDs stay in the cache and answer payloads. Only the visual list groups
// reviewed sources; independent meanings keep separate sections.
export function learningMeanings(senses: Sense[]): Sense[] {
  const groups = new Map<number, Sense>();
  const seen = new Map<number, Set<string>>();
  const result: Sense[] = [];
  for (const sense of senses) {
    if (!sense.core_definition_cn) { result.push(sense); continue; }
    const existing = groups.get(sense.learning_unit_id);
    if (existing) {
      const sentences = seen.get(sense.learning_unit_id)!;
      existing.examples.push(...sense.examples.filter((example) => {
        const key = example.sentence.trim().toLowerCase();
        if (sentences.has(key)) return false;
        sentences.add(key); return true;
      }));
    } else {
      const group = { ...sense, definition_cn: sense.core_definition_cn,
        definition_en: sense.core_definition_en, examples: [...sense.examples] };
      groups.set(sense.learning_unit_id, group);
      seen.set(sense.learning_unit_id, new Set(group.examples.map((example) => example.sentence.trim().toLowerCase())));
      result.push(group);
    }
  }
  return result;
}

export function chineseMeaning(chinese: string | null | undefined): string {
  return chinese && /[\u3400-\u9fff]/.test(chinese) ? chinese : "待核查";
}
