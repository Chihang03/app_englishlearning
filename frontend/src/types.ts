export type User = {
  id: number;
  username: string;
  timezone: string;
  role: "learner" | "admin";
};

export type Card = {
  attempt_id: string;
  needs_correction: boolean;
  pronunciation_used: boolean;
  answer_exposed: boolean;
  known_candidate: boolean;
  confirmations: number;
  is_relearning: boolean;
  id: number;
  sense_id: number;
  learning_unit_id: number;
  example_id: number;
  answer_form: string;
  answer_form_label?: string | null;
  is_new_word: boolean;
  word: string;
  part_of_speech: string;
  definition_cn: string;
  definition_en?: string | null;
  cloze_sentence: string;
  example_sentence: string;
  example_translation_cn?: string | null;
  status: "New" | "Learning" | "Reviewing" | "Mature";
  remaining_today: number;
};

export type Sense = {
  id: number;
  learning_unit_id: number;
  part_of_speech: string;
  definition_cn: string;
  definition_en?: string | null;
  status: Card["status"];
  next_review_date?: string | null;
  examples: { id: number; sentence: string; translation_cn?: string | null; target_form: string }[];
};

export type LexicalClassification = "INFLECTION" | "DERIVED" | "LEXICALIZED_FORM" | "INDEPENDENT" | "UNKNOWN";

export type LexicalPeer = {
  lexical_unit_id: number;
  headword: string;
  pos_group: string;
  relation_type: string;
  direction: "incoming" | "outgoing";
  learnable: boolean;
};

export type LexicalMetadata = {
  inflected_forms: { spelling: string; form_types: string[]; label: string; scope_sense_id: number | null }[];
  derived_words: LexicalPeer[];
  related_words: LexicalPeer[];
};

export type LexicalEntry = {
  lexical_unit_id: number;
  word_id: number | null;
  headword: string;
  pos_group: string;
  classification: LexicalClassification;
  learnable: boolean;
  matched_form?: { spelling: string; form_types: string[] };
  senses: Sense[];
  morphology: LexicalMetadata;
};

export type StudyDetails = {
  word: string;
  senses: Sense[];
  entries: LexicalEntry[];
  morphology: LexicalMetadata;
  exposed_sense_ids: number[];
};

export type ReviewResult = {
  sense_id: number;
  learning_unit_id: number;
  example_id: number;
  word: string;
  other_senses: Sense[];
  is_correct: boolean;
  is_independent: boolean;
  outcome: "independent" | "assisted" | "corrected" | "incorrect";
  is_blank: boolean;
  correct_answer: string;
  example_sentence: string;
  memory: {
    known_candidate: boolean;
    confirmations: number;
    stability_days: number;
    difficulty: number;
    predicted_recall_probability: number | null;
    audit_recall_probability: number;
    target_retention: number;
    personalized: boolean;
  };
  srs_state: {
    correct_count: number;
    interval_days: number;
    next_review_date: string;
    status: Card["status"];
  };
};

export type Stats = {
  total_study_time_ms: number;
  memory_model?: {
    target_retention: number;
    personalized: boolean;
    sample_count: number;
    minimum_samples: number;
    evaluated_at: string | null;
  };
  today_learning: number;
  today_completed_cards: number;
  today_accuracy: number;
  today_success: number;
  today_success_senses: number;
  today_independent_accuracy: number | null;
  pending_relearning: number;
  pending_relearning_senses: number;
  next_relearning_at: string | null;
  total_learned: number;
  due_review: number;
  new_words: number;
  learning: number;
  learning_due: number;
  lapse_words: number;
  due_lapses: number;
  mastered: number;
  mature: number;
  streak_days: number;
  learned_senses: number;
  new_senses: number;
  due_senses: number;
  due_words: number;
  mastered_senses: number;
  legacy_unmapped_words: number;
};

export type Settings = {
  show_sentence_translation: boolean;
  skip_basic_600: boolean;
  selected_word_list_ids: string[];
  speech_rate: SpeechRate | null;
};

export type WordList = {
  list_id: string;
  title: string;
  description: string;
  source_url: string;
  source_word_count: number;
  word_count: number;
  learned_word_count: number;
  mastered_word_count: number;
  selected: boolean;
};

export type SpeechRate = 90 | 120 | 175;

export type SpeechSettings = {
  voiceURI: string;
  rate: SpeechRate;
};
