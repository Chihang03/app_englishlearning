WITH selected AS MATERIALIZED (
 SELECT DISTINCT m.learning_unit_id FROM user_vocabulary_lists v JOIN word_list_learning_units m ON m.list_id=v.list_id
 WHERE v.user_id=:user AND v.selected=1
), touched AS MATERIALIZED (
 SELECT word_id FROM srs_state WHERE user_id=:user
 UNION SELECT u.word_id FROM sense_srs_state p JOIN perf_units u ON u.id=p.learning_unit_id
 WHERE p.user_id=:user AND p.retired_at IS NULL
)
SELECT u.id AS learning_unit_id FROM perf_units u JOIN perf_words w ON w.id=u.word_id
JOIN learning_units original ON original.id=u.id
LEFT JOIN word_frequencies f ON f.headword=w.normalized
WHERE u.usable=1 AND (w.owner_id=:user OR (w.owner_id IS NULL AND u.id IN (SELECT learning_unit_id FROM selected)))
AND NOT EXISTS(SELECT 1 FROM sense_srs_state p WHERE p.user_id=:user AND p.learning_unit_id=u.id AND p.retired_at IS NULL)
AND NOT (:skip AND u.basic)
AND NOT EXISTS(SELECT 1 FROM user_muted_words t WHERE t.user_id=:user AND t.word=w.normalized)
ORDER BY u.word_id IN (SELECT word_id FROM touched),f.zipf_cent IS NULL,f.zipf_cent DESC,f.tie_break,original.position,w.id,u.id LIMIT 1
