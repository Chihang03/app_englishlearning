-- :user, :today, :now, :skip. Content eligibility is computed once per revision.
WITH eligible AS MATERIALIZED (
 SELECT u.id,u.word_id,w.owner_id FROM perf_units u JOIN perf_words w ON w.id=u.word_id
 WHERE u.usable=1 AND NOT (:skip AND u.basic)
 AND NOT EXISTS(SELECT 1 FROM user_muted_words t WHERE t.user_id=:user AND t.word=w.normalized)
), progress AS MATERIALIZED (
 SELECT p.*,e.word_id,e.owner_id FROM sense_srs_state p
 JOIN eligible e ON e.id=p.learning_unit_id WHERE p.user_id=:user AND p.retired_at IS NULL
), due AS MATERIALIZED (
 SELECT p.*,q.learning_unit_id IS NOT NULL AS is_relearning FROM progress p
 LEFT JOIN relearning_queue q ON q.user_id=:user AND q.learning_unit_id=p.learning_unit_id AND q.retired_at IS NULL
 LEFT JOIN adaptive_memory m ON m.user_id=:user AND m.learning_unit_id=p.learning_unit_id AND m.retired_at IS NULL
 WHERE (p.owner_id IS NULL OR p.owner_id=:user) AND (p.status!='Mature' OR m.learning_unit_id IS NOT NULL)
 AND ((q.learning_unit_id IS NULL AND p.next_review_date<=:today) OR q.ready_at<=:now)
), selected AS MATERIALIZED (
 SELECT DISTINCT m.learning_unit_id FROM user_vocabulary_lists v JOIN word_list_learning_units m ON m.list_id=v.list_id
 WHERE v.user_id=:user AND v.selected=1
), touched AS MATERIALIZED (
 SELECT word_id FROM srs_state WHERE user_id=:user
 UNION SELECT u.word_id FROM sense_srs_state p JOIN perf_units u ON u.id=p.learning_unit_id
 WHERE p.user_id=:user AND p.retired_at IS NULL
), new_units AS MATERIALIZED (
 SELECT e.* FROM eligible e
 WHERE (e.owner_id=:user OR (e.owner_id IS NULL AND e.id IN (SELECT learning_unit_id FROM selected)))
 AND NOT EXISTS(SELECT 1 FROM sense_srs_state p WHERE p.user_id=:user AND p.learning_unit_id=e.id AND p.retired_at IS NULL)
), learned_words AS (
 SELECT word_id FROM srs_state WHERE user_id=:user AND sense_migrated=0
 UNION SELECT u.word_id FROM sense_srs_state p JOIN perf_units u ON u.id=p.learning_unit_id
 JOIN perf_words w ON w.id=u.word_id WHERE p.user_id=:user AND p.retired_at IS NULL
 AND NOT (:skip AND u.basic)
 AND NOT EXISTS(SELECT 1 FROM user_muted_words t WHERE t.user_id=:user AND t.word=w.normalized)
), completed AS (
 SELECT e.word_id,COUNT(*) AS total,COUNT(p.learning_unit_id) AS learned,
 SUM(p.status IN ('Reviewing','Mature')) AS established,SUM(p.status='Mature') AS mature
 FROM eligible e LEFT JOIN sense_srs_state p ON p.user_id=:user AND p.learning_unit_id=e.id AND p.retired_at IS NULL
 GROUP BY e.word_id
)
SELECT
 (SELECT COUNT(*) FROM learned_words l JOIN perf_words w ON w.id=l.word_id
  WHERE NOT (:skip AND w.basic) AND NOT EXISTS(SELECT 1 FROM user_muted_words t WHERE t.user_id=:user AND t.word=w.normalized)) AS total_learned,
 (SELECT COUNT(DISTINCT word_id) FROM new_units WHERE word_id NOT IN (SELECT word_id FROM touched)) AS new_words,
 (SELECT COUNT(*) FROM new_units) AS new_senses,
 (SELECT COUNT(DISTINCT word_id) FROM new_units) AS words_with_new_senses,
 (SELECT COUNT(*) FROM progress) AS learned_senses,
 (SELECT COUNT(DISTINCT p.word_id) FROM progress p WHERE p.status IN ('Learning','Reviewing')
  AND p.word_id NOT IN (SELECT word_id FROM due)
  AND EXISTS(SELECT 1 FROM review_history h WHERE h.user_id=:user AND h.learning_unit_id=p.learning_unit_id AND h.is_correct=1)) AS learning,
 (SELECT COUNT(*) FROM progress WHERE status IN ('Reviewing','Mature')) AS mastered_senses,
 (SELECT COUNT(DISTINCT word_id) FROM progress WHERE wrong_count>0 AND status!='Mature') AS lapse_words,
 (SELECT COUNT(*) FROM due) AS due_senses,
 (SELECT COUNT(DISTINCT word_id) FROM due) AS due_words,
 (SELECT COUNT(DISTINCT CASE WHEN status='Learning' THEN word_id END) FROM due) AS learning_due,
 (SELECT COUNT(DISTINCT CASE WHEN wrong_count>0 THEN word_id END) FROM due) AS due_lapses,
 (SELECT COUNT(DISTINCT CASE WHEN is_relearning THEN word_id END) FROM due) AS pending_relearning,
 (SELECT COUNT(CASE WHEN is_relearning THEN 1 END) FROM due) AS pending_relearning_senses,
 (SELECT COUNT(*) FROM completed WHERE learned=total AND established=total) AS mastered,
 (SELECT COUNT(*) FROM completed WHERE learned=total AND mature=total) AS mature,
 (SELECT COUNT(*) FROM srs_state p JOIN perf_words w ON w.id=p.word_id WHERE p.user_id=:user AND p.sense_migrated=0
  AND NOT (:skip AND w.basic) AND NOT EXISTS(SELECT 1 FROM user_muted_words t WHERE t.user_id=:user AND t.word=w.normalized)) AS legacy_unmapped_words,
 (SELECT MIN(q.ready_at) FROM relearning_queue q JOIN progress p ON p.learning_unit_id=q.learning_unit_id
  WHERE q.user_id=:user AND q.retired_at IS NULL AND p.status!='Mature' AND q.ready_at>:now
  AND (p.owner_id IS NULL OR p.owner_id=:user)) AS next_relearning_at
