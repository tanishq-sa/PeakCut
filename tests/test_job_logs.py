"""Processing logs follow a video through background work and survive a page refresh."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-job-logs-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_FETCH_MODEL="0")
from studio import ctx, joblog, jobs, media


class JobLogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(folder.cleanup)
        self.job = {"id": "log-test", "dir": folder.name, "user": "u_test01", "creator": None,
                    "stage": "Waiting", "progress": 0, "clips": [], "source": {"type": "upload"}, "ai": {}}

    def test_stages_and_errors_are_saved_and_change_the_sync_counter(self):
        before = ctx.version(self.job["user"])
        jobs._set(self.job, stage="Downloading the video")
        jobs._set(self.job, progress=.2)
        jobs._set(self.job, stage="Stopped", error="Download timed out")
        saved = json.loads((Path(self.job["dir"]) / "job.json").read_text())
        self.assertEqual([entry["message"] for entry in saved["logs"]],
                         ["Downloading the video", "Stopped", "Download timed out"])
        self.assertEqual(saved["logs"][-1]["level"], "error")
        self.assertGreater(ctx.version(self.job["user"]), before)

    def test_downloader_logs_from_its_threads_stay_with_the_right_video(self):
        other = dict(self.job, id="other-video", logs=[])
        with joblog.capture(self.job), patch("yt_dlp.YoutubeDL") as ydl:
            media._ydl({})
        options = ydl.call_args.args[0]
        with joblog.capture(other):
            thread = threading.Thread(target=lambda: options["logger"].warning("Download timed out; retrying"))
            thread.start()
            thread.join(2)
        self.assertEqual(self.job["logs"][-1]["message"], "Download timed out; retrying")
        self.assertEqual(other["logs"], [])

    def test_download_progress_is_visible_without_logging_every_packet(self):
        with joblog.capture(self.job), patch("yt_dlp.YoutubeDL") as ydl:
            media._ydl({"format": "ba/worst"})
        hooks = ydl.call_args.args[0]["progress_hooks"]
        for amount in [10, 10, 11, 21, 100]:
            for hook in hooks:
                hook({"status": "downloading", "downloaded_bytes": amount, "total_bytes": 100})
        for hook in hooks:
            hook({"status": "finished"})
        lines = self.job["logs"]
        self.assertLess(len(lines), 5)
        self.assertTrue(any("audio" in line["message"].lower() and "10%" in line["message"] for line in lines))
        self.assertIn("finished", lines[-1]["message"].lower())

    def test_old_download_notes_and_clip_errors_are_available_in_the_ui(self):
        self.job["ai"]["log"] = ["Whole-video download timed out; fetching clip parts instead"]
        self.job["clips"] = [{"id": "clip01", "status": "error", "error": "YouTube refused the download"}]
        messages = [entry["message"] for entry in jobs.public(self.job)["logs"]]
        self.assertTrue(any("timed out" in message for message in messages))
        self.assertTrue(any("clip01" in message and "YouTube" in message for message in messages))

    def test_logs_are_bounded_and_credentials_are_removed_before_saving(self):
        for i in range(joblog.LIMIT + 2):
            joblog.write(f"Step {i}", job=self.job)
        joblog.write("\x1b[31mERROR\x1b[0m https://example.com/video?token=private-value Authorization: Bearer secret-value", job=self.job)
        saved = (Path(self.job["dir"]) / "job.json").read_text()
        self.assertEqual(len(self.job["logs"]), joblog.LIMIT)
        self.assertNotIn("private-value", saved)
        self.assertNotIn("secret-value", saved)
        self.assertNotIn("\\u001b", saved)


if __name__ == "__main__":
    unittest.main()
