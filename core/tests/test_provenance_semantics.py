"""Provenance must not become factual content or a changed model/port value."""
import unittest
import copy
import tempfile
from types import SimpleNamespace
from soulcore.config import DEFAULTS
from soulcore.conflicts import core_text, find_superseded
from soulcore.engine import Engine
from soulcore.events import Event
from soulcore.extractor import format_transcript, heuristic_extract
from soulcore.llm import MockLLM
from soulcore.textutil import strip_source_prefix


class ProvenanceSemanticsTest(unittest.TestCase):
    def test_rule_extraction_preserves_speaker_with_source_prefix(self):
        turns=[SimpleNamespace(id=42,speaker="主人",role="user",
                               content="记一下，定位改成用 Cartographer 了，不用 AMCL 了")]
        plain=heuristic_extract(format_transcript(turns),1800000000.)
        sourced=heuristic_extract(format_transcript(turns,sources=True),1800000000.)
        self.assertEqual(sourced,plain)
        self.assertNotIn("来源编号",sourced[0]["content"])

    def test_existing_prefixed_content_has_same_semantic_core(self):
        text="主人说：记一下，定位改成用 Cartographer 了，不用 AMCL 了"
        self.assertEqual(core_text("[来源编号#42] "+text),core_text(text))

    def test_source_id_change_is_not_a_knowledge_update(self):
        text="主人说：记一下，定位改成用 Cartographer 了，不用 AMCL 了"
        self.assertEqual(find_superseded("[来源编号#8] "+text,[(7,"[来源编号#7] "+text,"fact")]),[])

    def test_real_model_update_remains_detectable_with_provenance(self):
        old="[来源编号#7] 主人说：记一下，小车主控板是 RDK X3"
        new="[来源编号#8] 主人说：记一下，小车主控板换成 RDK X5 了"
        self.assertEqual(find_superseded(new,[(7,old,"fact")]),[7])

    def test_only_generated_leading_numeric_markers_are_removed(self):
        for text in ("说明中提及[来源编号#42]", "[来源编号#abc] 主人: 记住部署地址", "[其他标记#42] 主人: 记住部署地址"):
            self.assertEqual(strip_source_prefix(text),text)
        self.assertEqual(strip_source_prefix("[来源编号#1] [来源编号#2] 主人: 记住部署地址"),"主人: 记住部署地址")

    def test_reextraction_does_not_invalidate_current_localization_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[1800000000.]
            cfg=copy.deepcopy(DEFAULTS)
            cfg["data_dir"]=tmp
            eng=Engine(cfg,tmp,llm=MockLLM(),clock=lambda:now[0])
            try:
                for day,text in ((35,"记一下，小车导航用的是 Nav2，定位用 AMCL"),
                                 (150,"记一下，定位改成用 Cartographer 了，不用 AMCL 了"),
                                 (190,"导航卡住了"), (199,"雷达数据好乱")):
                    now[0]=1800000000.+day*86400
                    ev=Event("local","project","robot","owner","主人",text,ts=now[0])
                    eng.context_block(ev,include_stable=False)
                    eng.after(ev,"好的，我看看。")
                    eng.flush_event(ev)
                rows=eng.store.db.execute("SELECT content,invalid_at,source_ids FROM items WHERE kind='fact'").fetchall()
                current=[r for r in rows if "Cartographer" in r["content"] and r["invalid_at"] is None]
                self.assertTrue(current)
                self.assertTrue(any(r["source_ids"]!="[]" for r in current))
                self.assertTrue(all(r["invalid_at"] is not None for r in rows if "定位用 AMCL" in r["content"]))
                self.assertTrue(all("来源编号" not in r["content"] for r in rows))
            finally:
                eng.close()


if __name__=="__main__":
    unittest.main()
