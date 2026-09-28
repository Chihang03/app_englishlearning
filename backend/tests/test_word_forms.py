import unittest

from app.word_forms import answer_form_label


class WordFormTests(unittest.TestCase):
    def test_dictionary_irregular_and_regular_spellings(self):
        for word,form,pos,label in [
            ('say','say','动词',None),('say','says','动词','第三人称单数'),
            ('say','saying','动词','-ing 形式'),('say','said','动词','过去式／过去分词'),
            ('write','wrote','动词','过去式'),('write','written','动词','过去分词'),
            ('go','gone','不及物动词','过去分词'),('run','running','动词','-ing 形式'),
            ('child','children','名词','复数'),('good','better','形容词','比较级'),
            ('happy','happiest','形容词','最高级'),('stop','stopped','动词','过去式／过去分词'),
            ('lie','lying','动词','-ing 形式'),('study','studied','动词','过去式／过去分词')]:
            with self.subTest(word=word,form=form):
                self.assertEqual(answer_form_label(word,form,pos),label)

    def test_be_numerals_and_spelling_variants(self):
        for word,form,pos,label in [('be','am','copular verb','第一人称单数'),
                ('be','is','不及物动词','第三人称单数'),('be','are','动词','第二人称／复数'),
                ('be','was','动词','过去式'),('be','been','动词','过去分词'),
                ('travel','travelled','动词','过去式／过去分词'),
                ('billion','billions','cardinal number','复数'),('one','ones','代词','复数')]:
            with self.subTest(word=word,form=form):
                self.assertEqual(answer_form_label(word,form,pos),label)

    def test_homographs_and_derivatives_are_not_mislabelled(self):
        self.assertEqual(answer_form_label('go','goes','名词'),'复数')
        self.assertEqual(answer_form_label('go','goes','动词'),'第三人称单数')
        self.assertIsNone(answer_form_label('say','sayer','动词'))
        self.assertIsNone(answer_form_label('interest','interesting','形容词'))
        self.assertIsNone(answer_form_label('say','said','名词'))
        self.assertIsNone(answer_form_label('run','RUN','动词'))

    def test_ing_hints_use_the_fixed_sentence(self):
        for word,form,sentence,label in [
            ('say','saying',"It's just a way of saying thank you.",'动名词'),
            ('say','saying',"She is saying something.",'现在分词·进行时'),
            ('say','saying',"That isn't saying much.",'现在分词·进行时'),
            ('say','saying',"He's only saying hello.",'现在分词·进行时'),
            ('do','doing',"What are you doing with yourself these days?",'现在分词·进行时'),
            ('swim','swimming','I enjoy swimming in the pool.','动名词'),
            ('swim','swimming','Swimming is good exercise.','动名词'),
            ('run','running','I saw him running down the road.','现在分词'),
            ('run','running','He came running down the road.','现在分词'),
            ('read','reading','Reading the map, she was confused.','现在分词'),
            ('interest','interesting','The film was interesting.','-ing 形式'),
            ('run','running','The man running down the road smiled.','-ing 形式'),
            ('say','saying','Saying hello is easy, but she is saying goodbye.','-ing 形式')]:
            with self.subTest(sentence=sentence):
                self.assertEqual(answer_form_label(word,form,'动词',sentence),label)
