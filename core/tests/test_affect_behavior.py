"""Affect continuity, semantic attribution and bounded social behavior."""
import json
import unittest
from types import SimpleNamespace

from soulcore.appraisal import appraise, appraise_detail
from soulcore.behavior import BehaviorPolicy, expression_profile
from soulcore.emotion import Emotion, Params
from soulcore.events import Event


class Clock:
    def __init__(self):
        self.t = 1800000000.0

    def __call__(self):
        return self.t


class TestAffectContinuity(unittest.TestCase):
    def test_event_after_offline_gap_does_not_change_past_mood(self):
        emo = Emotion(Params(noise_sigma=0, w_lonely=0), seed=7)
        emo.advance(600)
        previous = emo.mood, emo.arousal
        emo.event("success", t=600, amp=1.1)
        self.assertEqual((emo.mood, emo.arousal), previous)
        emo.advance(601)
        self.assertLessEqual(abs(emo.mood - previous[0]), emo.p.max_rate + 1e-12)
        self.assertLessEqual(abs(emo.arousal - previous[1]), emo.p.arousal_max_rate + 1e-12)

    def test_same_valence_can_be_activated_or_quiet(self):
        p = Params(noise_sigma=0, w_lonely=0)
        active, quiet = Emotion(p, seed=4), Emotion(p, seed=4)
        active.event("scold", amp=-1.2, arousal=0.45)
        quiet.event("scold", amp=-1.2, arousal=-0.30)
        active.advance(25)
        quiet.advance(25)
        self.assertAlmostEqual(active.mood, quiet.mood)
        self.assertGreater(active.arousal, quiet.arousal + 0.15)

    def test_arousal_recovers_without_new_messages(self):
        emo = Emotion(Params(noise_sigma=0, w_lonely=0), seed=2)
        emo.event("success", amp=1.1)
        emo.advance(25)
        peak = emo.arousal
        emo.advance(480)
        self.assertGreater(peak, emo.p.arousal_baseline + 0.10)
        self.assertAlmostEqual(emo.arousal, emo.p.arousal_baseline, delta=0.002)

    def test_restart_replays_noise_and_arousal(self):
        emo = Emotion(seed=17)
        emo.event("praise", t=5)
        emo.advance(24)
        restored = Emotion.from_dict(json.loads(json.dumps(emo.to_dict())))
        emo.advance(90)
        restored.advance(90)
        self.assertEqual(emo.mood, restored.mood)
        self.assertEqual(emo.arousal, restored.arousal)
        self.assertEqual(emo.event("gift"), restored.event("gift"))

    def test_legacy_saved_state_still_loads(self):
        original = Emotion(seed=3)
        original.event("scold", amp=-1.2)
        original.advance(40)
        saved = original.to_dict()
        for key in ("a", "arousal_drive", "rng_noise_state", "rng_event_state"):
            saved.pop(key)
        for key in ("arousal_baseline", "tau_arousal_drive", "tau_arousal", "arousal_max_rate"):
            saved["params"].pop(key)
        restored = Emotion.from_dict(json.loads(json.dumps(saved)))
        self.assertEqual(restored.mood, original.mood)
        self.assertEqual(restored.arousal, Params().arousal_baseline)
        restored.advance(41)
        self.assertTrue(0 <= restored.arousal <= 1)

    def test_repeated_events_and_long_gap_keep_arousal_bounded(self):
        emo = Emotion(seed=8)
        for minute in range(120):
            emo.event("scold", t=minute, amp=-10)
            self.assertTrue(0 <= emo.arousal <= 1)
        emo.advance(1000)
        self.assertTrue(0 <= emo.arousal <= 1)


class TestAppraisalAttribution(unittest.TestCase):
    def test_task_feedback_preserves_personal_relationship(self):
        for text in ("你这个方法错了", "你把配置改坏了", "这个检索结果不对", "这段代码太慢了"):
            with self.subTest(text=text):
                ap = appraise_detail(text)
                self.assertEqual((ap.event, ap.about_me, ap.target, ap.cause),
                                 ("chat", False, "task", "task_feedback"))
        self.assertEqual(appraise_detail("你这方法错了，你真蠢").event, "scold")
        self.assertEqual(appraise_detail("对不起，刚才把配置改坏了").event, "apology")

    def test_distress_and_quoted_insults_have_correct_target(self):
        ap = appraise_detail("我没拿到offer，好难过")
        self.assertEqual((ap.target, ap.cause, ap.about_me), ("user", "concern", False))
        quote = appraise_detail('电影里那句“你是废物”让我印象很深')
        self.assertTrue(quote.quoted)
        self.assertNotIn(quote.event, ("scold", "stranger_insult"))
        self.assertIsNone(quote.controllability)

    def test_local_attribution_guards_existing_model_classifier(self):
        class Misclassifies:
            calls = 0
            def chat_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"event": "scold", "about_me": True}
        model = Misclassifies()
        ap = appraise("你这个方案不对", False, model, "llm")
        self.assertEqual((ap.event, ap.about_me), ("chat", False))
        self.assertEqual(model.calls, 1)


class TestBehaviorBounds(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.low = SimpleNamespace(mood=0.0, arousal=0.10)
        self.cfg = {"quiet_enabled": True, "quiet_max_probability": 1,
                    "quiet_max_consecutive": 2, "quiet_duration_min": 10,
                    "quiet_cooldown_min": 20, "base_max_tokens": 200}
        self.policy = BehaviorPolicy(self.cfg, self.clock, seed=11)

    def event(self, text="哈哈", *, platform="qq", scene="private"):
        return Event(platform, scene, "42", "42", text=text, ts=self.clock())

    def test_quiet_is_off_by_default(self):
        policy = BehaviorPolicy(clock=self.clock, seed=2)
        self.assertTrue(policy.for_event(self.event(), self.low).respond)
        self.assertFalse(policy.status()["quiet_enabled"])

    def test_consecutive_quiet_and_cooldown_are_bounded(self):
        first = self.policy.for_event(self.event(), self.low)
        self.assertFalse(first.respond)
        self.clock.t += 60
        second = self.policy.for_event(self.event(), self.low)
        self.assertFalse(second.respond)
        self.assertEqual(first.quiet_until, second.quiet_until)
        self.assertTrue(self.policy.for_event(self.event(), self.low).respond)
        self.assertTrue(self.policy.for_event(self.event(), self.low).respond)
        self.assertEqual(self.policy.status()["quiet"], [])

    def test_quiet_expires_even_without_an_intervening_message(self):
        first = self.policy.for_event(self.event(), self.low)
        self.assertFalse(first.respond)
        restored = BehaviorPolicy(self.cfg, self.clock)
        restored.load_state(json.loads(json.dumps(self.policy.to_dict())))
        self.clock.t = first.quiet_until + 1
        self.assertEqual(restored.status()["quiet"], [])
        self.assertTrue(restored.for_event(self.event(), self.low).respond)

    def test_task_help_control_and_unclassified_text_always_respond(self):
        for text in ("帮我改代码", "你知道什么", "怎么继续项目", "记住这个决定", "不要再主动找我", "恢复主动聊天",
                     "救命", "我好难过", "hello please fix this", "哈哈，执行测试", "今天安排了一件事"):
            with self.subTest(text=text):
                policy = self.policy.for_event(self.event(text), self.low)
                self.assertTrue(policy.respond)
                self.assertEqual(policy.max_tokens, 200)
        for platform, scene in (("local", "project"), ("local", "session"), ("qq", "group")):
            self.assertTrue(self.policy.for_event(self.event(platform=platform, scene=scene), self.low).respond)

    def test_unknown_tasks_and_distress_keep_complete_budget_and_interrupt_quiet(self):
        self.assertFalse(self.policy.for_event(self.event(), self.low).respond)
        for text in ("把这段总结成三个要点", "刚看到我的猫死了", "刚看见车祸", "今天发生了糟糕的事情",
                     "今天天气让我想死", "写一段Python排序算法", "刚看到一件事情"):
            with self.subTest(text=text):
                policy = self.policy.for_event(self.event(text), self.low)
                self.assertTrue(policy.respond)
                self.assertEqual(policy.max_tokens, 200)
                self.assertEqual(policy.max_messages, 1)
                self.assertEqual(self.policy.status()["quiet"], [])

    def test_only_closed_light_social_phrases_can_shorten_sad_reply(self):
        no_quiet = BehaviorPolicy({**self.cfg, "quiet_enabled": False}, self.clock, seed=11)
        for text in ("你好", "哈哈", "今天天气不错", "今天阳光真好", "今天好安静"):
            with self.subTest(text=text):
                policy = no_quiet.for_event(self.event(text), self.low)
                self.assertTrue(policy.respond)
                self.assertLess(policy.max_tokens, 200)
        for text in ("今天天气不错但是家里出事了", "今天阳光下有人出事故", "你好，我需要你听我说"):
            with self.subTest(text=text):
                policy = no_quiet.for_event(self.event(text), self.low)
                self.assertTrue(policy.respond)
                self.assertEqual(policy.max_tokens, 200)

    def test_task_interrupts_existing_quiet_and_retains_budget(self):
        self.assertFalse(self.policy.for_event(self.event(), self.low).respond)
        task = self.policy.for_event(self.event("请把测试跑完"), self.low)
        self.assertTrue(task.respond)
        self.assertEqual(task.max_tokens, 200)
        self.assertEqual(self.policy.status()["quiet"], [])

    def test_recovery_clears_quiet(self):
        self.assertFalse(self.policy.for_event(self.event(), self.low).respond)
        happy = SimpleNamespace(mood=0.8, arousal=0.7)
        self.assertTrue(self.policy.for_event(self.event(), happy).respond)
        self.assertEqual(self.policy.status()["quiet"], [])

    def test_happiness_changes_expression_without_exceeding_limits(self):
        quiet = expression_profile(0.1, 0.1, {"base_max_tokens": 180, "max_messages": 2})
        happy = expression_profile(0.9, 0.8, {"base_max_tokens": 180, "max_messages": 2})
        self.assertLess(quiet.max_tokens, happy.max_tokens)
        self.assertLess(quiet.max_messages, happy.max_messages)
        self.assertLess(quiet.sharing, happy.sharing)
        self.assertLess(quiet.initiative_multiplier, happy.initiative_multiplier)
        for m in (0, 0.3, 0.5, 1):
            for a in (0, 0.5, 1):
                p = expression_profile(m, a, {"base_max_tokens": 40, "max_messages": 2})
                self.assertTrue(0 < p.max_tokens <= 40)
                self.assertTrue(1 <= p.max_messages <= 2)

    def test_saved_policy_replays_decisions(self):
        self.policy.cfg["quiet_max_probability"] = 0.5
        restored = BehaviorPolicy(self.policy.cfg, self.clock)
        restored.load_state(json.loads(json.dumps(self.policy.to_dict())))
        for _ in range(10):
            ev = self.event()
            left = self.policy.for_event(ev, self.low)
            right = restored.for_event(ev, self.low)
            self.assertEqual(left.respond, right.respond)
            self.assertEqual(left.quiet_until, right.quiet_until)
            self.clock.t += 60


if __name__ == "__main__":
    unittest.main()
