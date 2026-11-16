import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import responder
import voice_gate as g

HERE = {"jessica": "Jessica", "qwen3": "qwen3"}


class Gate(unittest.TestCase):
    def setUp(self):
        g.reset()
        self.asked = []

    def ask_to(self, to):
        def ask(prompt):
            self.asked.append(prompt)
            return to if to is None or to.startswith("{") else f'{{"to": "{to}"}}'
        return ask

    def decide(self, text, ask, now=1000.0, here=HERE):
        return g.decide(text, "ch", here, ask, responder.named, now=now)

    def test_no_name_and_no_conversation_never_asks_the_model(self):
        r = self.decide("Turn off the desk lamp.", self.ask_to("jessica"))
        self.assertEqual((r["accepted"], r["reason"]), (False, "no_name"))
        self.assertEqual(self.asked, [])

    def test_named_anywhere_in_the_sentence_is_accepted(self):
        r = self.decide("Turn off the desk lamp, Jessica.", self.ask_to("jessica"))
        self.assertEqual((r["accepted"], r["to"], r["reason"], r["open"]), (True, "jessica", "named", "jessica"))
        self.assertIn("CURRENT utterance: Turn off the desk lamp, Jessica.", self.asked[0])

    def test_talking_about_an_agent_is_rejected(self):
        r = self.decide("Her name is Jessica and she is my assistant.", self.ask_to('{"to": null}'))
        self.assertEqual((r["accepted"], r["reason"], r["open"]), (False, "about", None))

    def test_followup_without_the_name_inside_the_window(self):
        self.decide("Jessica, set a timer for ten minutes.", self.ask_to("jessica"), now=1000)
        g.saw_reply("ch", "jessica", "Done. Anything else?", 1005)
        r = self.decide("Make it fifteen instead.", self.ask_to("jessica"), now=1020)
        self.assertEqual((r["accepted"], r["to"], r["reason"]), (True, "jessica", "followup"))
        self.assertIn("jessica: Done. Anything else?", self.asked[-1])

    def test_side_remark_during_a_conversation_is_rejected(self):
        self.decide("Jessica, dim the lights.", self.ask_to("jessica"), now=1000)
        r = self.decide("Honey, did you feed the dog?", self.ask_to('{"to": null}'), now=1010)
        self.assertEqual((r["accepted"], r["reason"], r["open"]), (False, "not_for_agent", "jessica"))

    def test_window_expires_after_30_seconds(self):
        self.decide("Jessica, dim the lights.", self.ask_to("jessica"), now=1000)
        r = self.decide("And the kitchen too.", self.ask_to("jessica"), now=1031)
        self.assertEqual(r["reason"], "no_name")

    def test_reply_and_playback_extend_the_window(self):
        self.decide("Jessica, read me the news.", self.ask_to("jessica"), now=1000)
        g.saw_reply("ch", "jessica", "A long reply…", 1020)
        g.played("ch", now=1045)  # long reading finished
        self.assertEqual(g.open_for("ch", now=1070), "jessica")
        self.assertIsNone(g.open_for("ch", now=1076))

    def test_stop_word_stops_the_open_agent_without_asking(self):
        self.decide("Jessica, tell me a long story.", self.ask_to("jessica"), now=1000)
        n = len(self.asked)
        r = self.decide("Stop.", self.ask_to("jessica"), now=1005)
        self.assertEqual((r["reason"], r["to"]), ("stopped", "jessica"))
        self.assertEqual(len(self.asked), n)
        self.assertEqual(self.decide("stop", self.ask_to("jessica"), now=2000)["reason"], "no_name")

    def test_model_failure_or_garbage_fails_closed(self):
        self.assertEqual(self.decide("Jessica, lights off.", lambda p: None)["reason"], "decision_unavailable")
        self.assertEqual(self.decide("Jessica, lights off.", lambda p: "sure!")["reason"], "decision_unavailable")

    def test_model_naming_someone_not_a_candidate_is_rejected(self):
        r = self.decide("Jessica, lights off.", self.ask_to("qwen3"))
        self.assertEqual((r["accepted"], r["reason"]), (False, "about"))

    def test_several_names_model_picks_one(self):
        r = self.decide("qwen3 ask Jessica to turn the lights off", self.ask_to("qwen3"))
        self.assertEqual(r["to"], "qwen3")
        self.assertIn("@jessica", self.asked[0])

    def test_think_tags_are_ignored(self):
        r = self.decide("Jessica, lights off.", lambda p: '<think>hmm {"to": null}</think>{"to": "jessica"}')
        self.assertEqual(r["to"], "jessica")

    def test_no_agents_in_channel(self):
        self.assertEqual(self.decide("Jessica, hi", self.ask_to("jessica"), here={})["reason"], "no_agents")


if __name__ == "__main__":
    unittest.main()
