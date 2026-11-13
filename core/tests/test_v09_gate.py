"""v0.9 R2：门控补漏（转述 / 状态变化 / 时间词陈述句）和"咖啡因"误判。

两部分都只加规则，不改原有断言：
  - extractor.worth_extracting：短句里没有"我"、没有常见关键词，但明显是信息的句子
    （"明天不上班""导师说中期推迟""老板通知周五开会"）要能进提炼；敷衍和应答不能进。
  - conflicts._rule_b：喜好反转里，X 后面跟 因/机/店/馆/杯/豆/粉/味 时不是同一个对象，
    "我喜欢喝咖啡"不能被"我晚上不喝咖啡因饮料"取代。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from soulcore.conflicts import find_superseded
from soulcore.extractor import worth_extracting


class T:
    def __init__(self, content, role="user", created=1.8e9):
        self.content, self.role, self.created = content, role, created


def worth(*texts):
    return worth_extracting([T(t) for t in texts])


class GateV09Test(unittest.TestCase):
    def test_reported_and_status_change(self):
        for text in ("明天不上班", "导师说中期推迟", "老板通知周五开会",
                     "学院通知下周放假", "我妈说明天出院", "会议改期到下周三"):
            with self.subTest(text=text):
                self.assertTrue(worth(text), f"{text} 应该值得提炼")

    def test_chatter_stays_out(self):
        for text in ("明天再说吧", "好的明天", "嗯嗯", "好的", "这个报错怎么看", "帮我改一下这个函数"):
            with self.subTest(text=text):
                self.assertFalse(worth(text), f"{text} 不该触发提炼")

    def test_v08_positive_still_works(self):
        for text in ("我女朋友叫小雨", "我在深圳", "我换工作了", "我下周去日本", "我妈住院了",
                     "以后用中文回复我", "我有点感冒", "论文被拒了", "记一下，我住在北京"):
            with self.subTest(text=text):
                self.assertTrue(worth(text))

    def test_question_with_time_word_not_extracted(self):
        # 只是问时间安排，没有新信息
        self.assertFalse(worth("下周有空吗"))
        self.assertFalse(worth("今天怎么样"))


class CaffeineTest(unittest.TestCase):
    def test_caffeine_is_not_coffee(self):
        old = "我喜欢喝咖啡"
        for new in ("我晚上不喝咖啡因饮料", "我不喝咖啡因了", "现在受不了咖啡机的噪音"):
            with self.subTest(new=new):
                self.assertEqual(find_superseded(new, [(1, old, "fact")]), [])

    def test_real_reversal_still_detected(self):
        for new in ("我戒咖啡了，现在只喝茶", "我不喝咖啡了", "我讨厌咖啡"):
            with self.subTest(new=new):
                self.assertEqual(find_superseded(new, [(1, "我喜欢喝咖啡", "fact")]), [1])

    def test_other_suffixes(self):
        for new in ("我晚上不喝咖啡因饮料", "我不去咖啡馆了", "我不买咖啡豆了", "我不吃咖啡味的东西"):
            with self.subTest(new=new):
                self.assertEqual(find_superseded(new, [(1, "我喜欢喝咖啡", "fact")]), [])

    def test_v08_conflict_cases_unchanged(self):
        yes = [
            ("小车主控板是地平线 RDK X3", "小车主控板换成 RDK X5 了"),
            ("我在读大三，专业是自动化", "我现在升到大四了"),
            ("博客每天凌晨3点自动备份", "博客备份改成每周日凌晨4点了"),
            ("我喜欢喝咖啡", "我戒咖啡了，现在只喝茶"),
            ("博客部署在香橙派5上", "博客已经迁到阿里云服务器上了"),
            ("小车导航用的是 Nav2，定位用 AMCL", "定位改成用 Cartographer 了，不用 AMCL 了"),
        ]
        no = [
            ("我喜欢猫", "我也喜欢狗"),
            ("博客部署在香橙派5上", "博客每天凌晨3点备份"),
            ("以后每次提交前提醒我跑测试", "记住论文图表用 matplotlib 画"),
            ("我在读大四", "我室友在读大三"),
            ("我喜欢喝咖啡", "我不再喜欢熬夜了"),
            ("我在读大四", "我弟弟现在大一了"),
        ]
        for old, new in yes:
            with self.subTest(new=new):
                self.assertEqual(find_superseded(new, [(1, old, "fact")]), [1])
        for old, new in no:
            with self.subTest(new=new):
                self.assertEqual(find_superseded(new, [(1, old, "fact")]), [])


if __name__ == "__main__":
    unittest.main()
