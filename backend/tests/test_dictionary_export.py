from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))


@unittest.skipUnless(importlib.util.find_spec('lxml'), 'optional Mac export dependencies are not installed')
class DictionaryExportTests(unittest.TestCase):
    def test_plural_variant_inside_sense_does_not_hide_headword(self):
        from dictionary_senses import parse_record
        markup = '''<div><span class="gramb"><span class="ps">noun</span>
          <span class="semb" lexid="lodging"><span class="frmg">
            <span class="frm">accommodations</span></span>
            <span class="trans">住所</span><span class="exg">
            <span class="ex">The office had accommodation for many clerks</span>
            </span></span></span></div>'''
        senses = parse_record(markup, 'accommodation', 'zh', ['accommodation', 'accommodations'])['senses']
        self.assertEqual(senses[0]['key'], 'zh:lodging')
        self.assertEqual(senses[0]['definition_cn'], '住所')
        self.assertEqual(len(senses[0]['examples']), 1)

    def test_grammatical_block_for_phrasal_verb_is_still_excluded(self):
        from dictionary_senses import parse_record
        markup = '''<div><span class="gramb"><span class="ps">verb</span>
          <span class="frm">take off</span><span class="semb" lexid="flight">
          <span class="trans">起飞</span></span></span></div>'''
        self.assertEqual(parse_record(markup, 'take', 'zh', ['take'])['senses'], [])

    def test_traditional_groups_keep_only_own_gloss_and_examples(self):
        from traditional_dictionary_senses import parse_traditional_record
        markup = '''<div><span class="se1"><span class="pos">n.</span>
          <span class="se2"><span class="msDict"><span class="trans">廣告</span></span>
            <span class="eg"><span class="ex">I saw the ad yesterday.</span>
              <span class="trans">我昨天看到了廣告。</span></span></span></span>
          <span class="subEntry"><span class="se1"><span class="pos">n.</span>
            <span class="se2"><span class="trans">不應加入的片語</span></span>
          </span></span></div>'''
        senses = parse_traditional_record(markup, 'ad', ['ad', 'ads'])
        self.assertEqual(len(senses), 1)
        self.assertEqual(senses[0]['definition_cn'], '廣告')
        self.assertEqual(senses[0]['part_of_speech'], '名词')
        self.assertEqual(senses[0]['examples'][0]['sentence'], 'I saw the ad yesterday.')

    def test_bilingual_group_keeps_its_own_gloss_pos_and_translation(self):
        from dictionary_senses import parse_record
        markup='''<div><span class="gramb"><span class="ps">noun</span>
          <span class="semb" lexid="place"><span class="trgg"><span class="trg">
            <span class="trans">地址</span><span class="trans ty_pinyin">dizhi</span></span></span>
            <span class="exg"><span class="ex">Please give me your address</span>
              <span class="trg"><span class="trans">请告诉我你的地址。</span></span></span></span></span>
          <span class="gramb"><span class="ps">transitive verb</span>
          <span class="semb" lexid="tackle"><span class="trg"><span class="trans">处理</span></span>
            <span class="exg"><span class="ex">We must address this problem</span>
              <span class="trg"><span class="trans">我们必须处理这个问题。</span></span></span></span></span></div>'''
        senses=parse_record(markup,'address','zh',['address','addressed'])['senses']
        self.assertEqual([s['definition_cn'] for s in senses],['地址','处理'])
        self.assertEqual([s['part_of_speech'] for s in senses],['名词','及物动词'])
        self.assertEqual(senses[0]['examples'][0]['translation_cn'],'请告诉我你的地址。')
        self.assertEqual(senses[1]['examples'][0]['sentence'],'We must address this problem.')

    def test_english_subsenses_and_inflections_do_not_mix(self):
        from dictionary_senses import parse_record
        markup='''<div><span class="se1"><span class="pos">verb</span>
          <span class="se2"><span class="msDict" id="tackle"><span class="df">deal with a problem</span>
          <span class="eg"><span class="ex">The problem has been addressed</span>.</span></span>
          <span class="msDict" id="speech"><span class="df">speak to an audience</span>
          <span class="eg"><span class="ex">Please address the audience</span>.</span></span></span></span>
          <span class="subEntry"><span class="msDict" id="phrase"><span class="df">wrong phrase</span></span></span></div>'''
        senses=parse_record(markup,'address','en',['address','addressed','addresser'])['senses']
        self.assertEqual(len(senses),2);self.assertEqual(senses[0]['definition_en'],'deal with a problem')
        self.assertEqual(senses[0]['examples'][0]['target_form'],'addressed')
        self.assertEqual(senses[1]['examples'][0]['target_form'],'address')

    def test_construction_templates_derivatives_and_proper_names_are_excluded(self):
        from dictionary_senses import example
        self.assertIsNone(example('to address sth to sb','address',['address'],'verb'))
        self.assertIsNone(example('He is a useful addresser.','address',['address','addresser'],'verb'))
        self.assertIsNone(example('This is the Bank of England.','bank',['bank'],'noun'))
        self.assertEqual(example('The boy ran home quickly.','run',['run','ran'],'verb')['target_form'],'ran')

    def test_uncertain_cross_dictionary_pair_does_not_receive_chinese_gloss(self):
        from build_vocab_bundle import aligned_senses
        entry={'word':'bank','mac_zh':{'senses':[
            {'key':'zh:financial','part_of_speech':'名词','definition_cn':'银行','definition_en':'finance','examples':[]}]},
            'mac_en':{'senses':[
            {'key':'en:river','part_of_speech':'名词','definition_cn':'','definition_en':'land beside a river',
             'examples':[{'sentence':'Willows lined the bank.','target_form':'bank'}]}]}}
        chosen=aligned_senses(entry,{})
        self.assertEqual(chosen[0]['definition_cn'],'');self.assertEqual(chosen[0]['definition_en'],'land beside a river')
