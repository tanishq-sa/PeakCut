"""A slow or refused video download must not hide what happened to the clips."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-finishing-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_FETCH_MODEL="0")
from studio import jobs, media


class FinishingTests(unittest.TestCase):
    def run_video(self, download_error, ready=0):
        folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        audio = root / "audio.m4a"
        audio.touch()
        job = {"id": "finishing-test", "dir": str(root), "user": "u_test01", "creator": None,
               "title": "Test", "uploader": "", "duration": 0, "progress": 0,
               "options": dict(jobs.DEFAULTS), "source": {"type": "url", "url": "https://youtu.be/test"}}
        words = [{"w": "word", "s": i, "e": i + .5} for i in range(20)]
        candidates = [{"start": i * 8, "end": i * 8 + 6, "score": 90} for i in range(2)]
        snapshots = []

        def piece(job, clip, *args, **kwargs):
            if int(clip["id"][-2:]) <= ready:
                return
            clip.update(status="error", error=media.YT_BLOCKED)

        with ExitStack() as stack:
            def mock(obj, name, **kwargs):
                return stack.enter_context(patch.object(obj, name, **kwargs))
            mock(jobs, "save", side_effect=lambda j: snapshots.append((j.get("stage"), j.get("progress"))))
            mock(jobs.usage, "set_context")
            mock(jobs.creators, "log")
            from studio import notices
            notice = mock(notices, "add")
            mock(jobs.transcribe, "model_ready", return_value=True)
            mock(jobs.transcribe, "transcribe", return_value=words)
            mock(media, "info", return_value={"kind": "video", "duration": 30, "title": "Test",
                                              "uploader": "Test", "is_live": False})
            mock(media, "save_thumb", return_value=False)
            download = mock(media, "download", side_effect=download_error)
            mock(media, "download_audio", return_value=audio)
            mock(media, "extract_audio")
            mock(media, "load_audio", return_value=np.zeros(30 * 16000, dtype=np.int16))
            mock(media, "youtube_words", return_value=None)
            mock(jobs.brain, "find_clips", return_value=(candidates, {"model": "test"}))
            mock(jobs, "_fetch_piece", side_effect=piece)
            mock(jobs, "render_one", side_effect=lambda j, c, w: c.update(status="done"))
            jobs._run_job(job)
        return job, download, notice, snapshots

    def test_failed_download_is_not_started_again_for_each_waiting_clip(self):
        job, download, _, _ = self.run_video(RuntimeError("The read operation timed out"))
        self.assertEqual(download.call_count, 1, "yt-dlp already retries; do not repeat the whole download")

    def test_no_ready_clips_is_an_error_with_the_download_reason(self):
        job, _, notice, _ = self.run_video(media.YouTubeBlocked(media.YT_BLOCKED))
        self.assertEqual(job["status"], "error")
        self.assertIn("YouTube", job["error"])
        self.assertNotIn("clips ready", notice.call_args.args[2])

    def test_partial_success_counts_only_finished_clips(self):
        job, _, notice, _ = self.run_video(RuntimeError("timeout"), ready=1)
        self.assertEqual(job["status"], "done")
        self.assertIn("1 clip ready", notice.call_args.args[2])
        self.assertIn("1 could not be made", job["stage"])

    def test_finishing_progress_moves_as_clips_finish(self):
        first, second = Future(), Future()
        first.set_result(None)
        clips = [{"id": "clip01", "status": "done"}, {"id": "clip02", "status": "queued"}]
        job = {"id": "test", "creator": None, "clips": clips, "stage": "Finishing the clips", "progress": .92}
        snapshots = []
        def finish(_):
            clips[1]["status"] = "done"
            second.set_result(None)
        with patch.object(jobs, "save", side_effect=lambda j: snapshots.append((j["stage"], j["progress"]))), \
                patch.object(jobs.time, "sleep", side_effect=finish):
            jobs._wait_for_clips(job, [(first, clips[0]), (second, clips[1])], {"clip02": time.time()}, 2)
        self.assertTrue(any("1 of 2" in stage and progress > .92 for stage, progress in snapshots), snapshots)

    def test_download_retries_are_limited(self):
        with patch("yt_dlp.YoutubeDL") as ydl:
            media._ydl({})
        options = ydl.call_args.args[0]
        for name in ("retries", "fragment_retries", "extractor_retries"):
            self.assertLessEqual(options.get(name, 10), 2, name)

    def test_restarting_corrects_an_old_job_with_only_failed_clips(self):
        job, _, _, _ = self.run_video(media.YouTubeBlocked(media.YT_BLOCKED))
        job.update(status="done", stage="Done", error=None)
        path = Path(job["dir"]) / "job.json"
        path.write_text(json.dumps(job))
        self.addCleanup(lambda: jobs._jobs.pop(job["id"], None))
        self.addCleanup(lambda: jobs._by_user.get(job["user"], {}).pop(job["id"], None))
        jobs._load_file(path, job["user"])
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded["status"], "error")
        self.assertIn("YouTube", loaded["error"])


if __name__ == "__main__":
    unittest.main()
