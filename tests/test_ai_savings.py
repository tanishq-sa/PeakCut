"""Keep caption polishing while sharing requests and reusing successful AI answers."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-ai-savings-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_FETCH_MODEL="0")
from studio import ai, brain, ctx, jobs, media, render


def words(text, start=0):
    return [{"w": w, "s": start + i * .5, "e": start + i * .5 + .4} for i, w in enumerate(text.split())]


def polish(prompt, schema=None, purpose=""):
    if purpose == "Caption translation":
        count = int(re.search(r"Lines \((\d+)\)", prompt)[1])
        return ["Hello my friend." for _ in range(count)], "test-model", 10
    return [m[1] for m in re.finditer(r"^\d+: (.+)$", prompt, re.M)], "test-model", 10


class AiSavingsTests(unittest.TestCase):
    def setUp(self):
        self.owner = "u_" + os.urandom(6).hex()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(ctx.as_user(self.owner))
        for obj, name, value in [(brain.config, "ai_ready", True), (ai, "models", ["test-model"]),
                                  (ai, "provider", "custom"), (ai, "base_url", "https://ai.example"),
                                  (ai, "key", "test-secret-for-cache")]:
            self.stack.enter_context(patch.object(obj, name, return_value=value))
        self.record = self.stack.enter_context(patch.object(brain.usage, "record"))

    def test_same_request_reuses_a_saved_answer_without_counting_another_call(self):
        with patch.object(ai, "call", return_value=(["Hello"], 8, 2)) as call:
            first = brain.ask_ai("A unique prompt", {"type": "array", "items": {"type": "string"}})
            first[0].append("changed by caller")
            second = brain.ask_ai("A unique prompt", {"type": "array", "items": {"type": "string"}})
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.record.call_count, 1)
        self.assertEqual(second, (["Hello"], "test-model", 0))
        self.assertTrue(any(Path(data.name).rglob("ai-cache.sqlite3")), "answers must survive a service restart")

    def test_users_and_changed_model_settings_do_not_share_saved_answers(self):
        with patch.object(ai, "call", return_value=(["Hello"], 8, 2)) as call:
            brain.ask_ai("Same prompt", brain.FIX_SCHEMA)
            brain.ask_ai("Same prompt", brain.FIX_SCHEMA)
            with ctx.as_user("u_another_owner"):
                brain.ask_ai("Same prompt", brain.FIX_SCHEMA)
            with patch.object(ai, "models", return_value=["another-model"]):
                brain.ask_ai("Same prompt", brain.FIX_SCHEMA)
            with patch.object(ai, "key", return_value="another-test-secret"):
                brain.ask_ai("Same prompt", brain.FIX_SCHEMA)
        self.assertEqual(call.call_count, 4)

    def test_concurrent_identical_requests_share_one_ai_call(self):
        entered, release = threading.Event(), threading.Event()
        def answer(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return ["Hello"], 8, 2
        with patch.object(ai, "call", side_effect=answer) as call, ThreadPoolExecutor(2) as pool:
            first = pool.submit(ctx.bind(brain.ask_ai), "Concurrent prompt", brain.FIX_SCHEMA)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(ctx.bind(brain.ask_ai), "Concurrent prompt", brain.FIX_SCHEMA)
            finally:
                release.set()
            results = [first.result(3), second.result(3)]
        self.assertEqual(call.call_count, 1)
        self.assertEqual(sorted(result[2] for result in results), [0, 10])

    def test_failed_requests_and_wrong_caption_lengths_are_not_saved(self):
        with patch.object(ai, "call", side_effect=[RuntimeError("offline"), (["Hello"], 8, 2)]) as call:
            with self.assertRaises(RuntimeError):
                brain.ask_ai("Retry a failed request")
            brain.ask_ai("Retry a failed request")
        self.assertEqual(call.call_count, 2)
        with patch.object(ai, "call", return_value=(["wrong length"], 8, 2)) as call:
            for _ in range(2):
                brain.ask_ai("Words (3):\n0: hello\n1: my\n2: friend", brain.FIX_SCHEMA, "Caption wording fix")
        self.assertEqual(call.call_count, 2)

    def test_four_clips_are_polished_in_one_request_and_keep_their_times(self):
        groups = [words("Hello my friend.", i * 10) for i in range(4)]
        with patch.object(brain, "ask_ai", side_effect=polish) as ask:
            result = brain.english_caption_batch(groups, "en", "Test")
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(result, groups)
        self.assertTrue(all(set(w) == {"w", "s", "e"} for group in result for w in group))

    def test_four_translations_share_one_request_and_keep_each_clips_clock(self):
        groups = [words("Hola mi amigo.", i * 10) for i in range(4)]
        with patch.object(brain, "ask_ai", side_effect=polish) as ask:
            result = brain.english_caption_batch(groups, "es", "Test")
        self.assertEqual(ask.call_count, 1)
        for i, group in enumerate(result):
            self.assertEqual(" ".join(w["w"] for w in group), "Hello my friend.")
            self.assertEqual(group[0]["s"], i * 10)
            self.assertLessEqual(group[-1]["e"], groups[i][-1]["e"])

    def test_large_caption_batches_are_split_before_the_request_gets_too_long(self):
        groups = [words("Hello my friend.", i * 10) for i in range(4)]
        with patch.object(brain, "CAPTION_BATCH_WORDS", 5), patch.object(brain, "ask_ai", side_effect=polish) as ask:
            self.assertEqual(brain.english_caption_batch(groups, "en", "Test"), groups)
        self.assertEqual(ask.call_count, 4)

    def test_video_pipeline_batches_caption_polishing_before_rendering(self):
        root = Path(tempfile.mkdtemp(dir=data.name))
        (root / "clips").mkdir()
        job = {"id": "batch-pipeline", "dir": str(root), "user": self.owner, "creator": None,
               "options": dict(jobs.DEFAULTS, max_clips=4), "source": {"type": "upload", "path": str(root / "source.mp4")},
               "title": "Test", "uploader": "", "duration": 0, "progress": 0, "clips": []}
        transcript = [word for i in range(4) for word in words("Hello my friend this is a great moment.", i * 20)]
        candidates = [{"start": i * 20, "end": i * 20 + 6, "score": 90, "hook": "Hello", "title": "Test",
                       "description": "", "hashtags": []} for i in range(4)]
        def encode(source, target, name, *args):
            for ext in ("mp4", "jpg", "ass"):
                (target / f"{name}.{ext}").touch()
            return {"file": name + ".mp4", "cover": name + ".jpg"}
        from studio import notices
        with patch.object(jobs.transcribe, "model_ready", return_value=True), \
                patch.object(media, "probe", return_value={"duration": 80, "width": 320, "height": 240}), \
                patch.object(media, "save_thumb", return_value=False), patch.object(media, "extract_audio"), \
                patch.object(media, "load_audio", return_value=np.zeros(80 * 16000, dtype=np.int16)), \
                patch.object(jobs.transcribe, "transcribe", return_value=transcript), \
                patch.object(brain, "find_clips", return_value=(candidates, {"model": "test-model"})), \
                patch.object(brain, "ask_ai", side_effect=polish) as ask, \
                patch.object(render, "render_clip", side_effect=encode), patch.object(notices, "add"):
            jobs._run_job(job)
            self.assertEqual([c["status"] for c in job["clips"]], ["done"] * 4)
            self.assertEqual(ask.call_count, 1)
            jobs.render_one(job, job["clips"][0], transcript)
            self.assertEqual(ask.call_count, 1, "a new layout must reuse the polished captions")


if __name__ == "__main__":
    unittest.main()
