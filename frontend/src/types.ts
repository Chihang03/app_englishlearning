export type User = {
  id: number;
  username: string;
  timezone: string;
};

export type Card = {
  id: number;
  part_of_speech: string;
  definition_cn: string;
  definition_en?: string | null;
  cloze_sentence: string;
  example_sentence: string;
  example_translation_cn?: string | null;
  status: "New" | "Learning" | "Reviewing" | "Mature";
  remaining_today: number;
};

export type ReviewResult = {
  is_correct: boolean;
  is_blank: boolean;
  correct_answer: string;
  example_sentence: string;
  srs_state: {
    correct_count: number;
    interval_days: number;
    next_review_date: string;
    status: Card["status"];
  };
};

export type Stats = {
  today_learning: number;
  today_accuracy: number;
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
};

export type Settings = {
  show_sentence_translation: boolean;
};

export type SpeechSettings = {
  voiceURI: string;
  rate: number;
};
