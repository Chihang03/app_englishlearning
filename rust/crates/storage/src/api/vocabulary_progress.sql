
            WITH touched_words AS (
                SELECT word_id FROM review_history WHERE user_id=?
                UNION SELECT word_id FROM srs_state WHERE user_id=?
                UNION SELECT s.word_id FROM sense_srs_state p JOIN word_senses s ON s.id=p.sense_id WHERE p.user_id=?
                UNION SELECT s.word_id FROM study_attempts a JOIN word_senses s ON s.id=a.sense_id WHERE a.user_id=?
            ), usable AS MATERIALIZED (
                SELECT u.id,u.word_id,MIN(CASE WHEN p.status='Mature' THEN 1 ELSE 0 END) AS mastered
                FROM learning_units u JOIN learning_unit_senses us ON us.learning_unit_id=u.id
                JOIN word_senses ds ON ds.id=us.sense_id JOIN sense_examples ex ON ex.sense_id=ds.id
                LEFT JOIN sense_srs_state p INDEXED BY idx_sense_srs_state_learning_unit ON p.learning_unit_id=u.id AND p.user_id=? AND p.retired_at IS NULL
                WHERE u.active=1 AND ds.active=1 AND ds.learning_enabled=1 AND ex.active=1
                GROUP BY u.id
            ), word_progress AS (
                SELECT m.list_id,u.word_id,MIN(u.mastered) AS mastered
                FROM word_list_learning_units m JOIN usable u ON u.id=m.learning_unit_id
                GROUP BY m.list_id,u.word_id
            )
            SELECT v.list_id,v.title,v.description,v.source_url,v.source_word_count,
                COUNT(p.word_id) AS word_count,
                COUNT(CASE WHEN p.word_id IS NOT NULL AND t.word_id IS NOT NULL THEN 1 END) AS learned_word_count,
                COALESCE(SUM(p.mastered),0) AS mastered_word_count
            FROM vocabulary_lists v
            LEFT JOIN word_progress p ON p.list_id=v.list_id
            LEFT JOIN touched_words t ON t.word_id=p.word_id
            GROUP BY v.list_id ORDER BY v.sort_order,v.title
            