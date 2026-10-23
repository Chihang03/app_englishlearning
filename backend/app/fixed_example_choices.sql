-- One-time repair of formerly rotating units. Prefer a base-form sentence,
-- then the earliest valid encounter; never borrow from another learning unit.
WITH rotating AS (
    SELECT id, representative_sense_id, word_id FROM learning_units
    WHERE active=1 AND example_strategy='rotate'
), encounters AS (
    SELECT user_id, learning_unit_id, example_id, MIN(julianday(seen_at)) AS first_seen
    FROM (
        SELECT a.user_id, b.learning_unit_id, a.example_id, a.created_at AS seen_at
        FROM study_attempts a JOIN learning_unit_senses b ON b.sense_id=a.sense_id
        JOIN rotating r ON r.id=b.learning_unit_id
        UNION ALL
        SELECT h.user_id, b.learning_unit_id, h.example_id, h.review_time
        FROM review_history h JOIN learning_unit_senses b ON b.sense_id=h.sense_id
        JOIN rotating r ON r.id=b.learning_unit_id
    ) GROUP BY user_id, learning_unit_id, example_id
), learners AS (
    SELECT p.user_id, p.learning_unit_id FROM user_sense_examples p
    JOIN rotating r ON r.id=p.learning_unit_id WHERE p.retired_at IS NULL
    UNION
    SELECT p.user_id, p.learning_unit_id FROM sense_srs_state p
    JOIN rotating r ON r.id=p.learning_unit_id WHERE p.retired_at IS NULL
    UNION
    SELECT user_id, learning_unit_id FROM encounters
), ranked AS (
    SELECT l.user_id, r.id AS learning_unit_id, r.representative_sense_id AS sense_id,
           e.id AS example_id, p.example_id AS previous_example_id,
           ROW_NUMBER() OVER (PARTITION BY l.user_id,r.id ORDER BY
               lower(trim(e.target_form))!=lower(trim(w.word)),
               h.first_seen IS NULL, h.first_seen,
               CASE WHEN e.id=p.example_id THEN 0 ELSE 1 END,s.position,e.id) AS rank
    FROM learners l JOIN rotating r ON r.id=l.learning_unit_id
    JOIN words w ON w.id=r.word_id
    JOIN learning_unit_senses b ON b.learning_unit_id=r.id
    JOIN word_senses s ON s.id=b.sense_id
    JOIN sense_examples e ON e.sense_id=s.id
    LEFT JOIN encounters h ON h.user_id=l.user_id AND h.learning_unit_id=r.id AND h.example_id=e.id
    LEFT JOIN user_sense_examples p ON p.user_id=l.user_id AND p.learning_unit_id=r.id AND p.retired_at IS NULL
    WHERE s.active=1 AND s.learning_enabled=1 AND e.active=1
      AND (w.owner_id IS NULL OR w.owner_id=l.user_id)
)
SELECT user_id,learning_unit_id,sense_id,example_id,previous_example_id
FROM ranked WHERE rank=1 ORDER BY user_id,learning_unit_id
