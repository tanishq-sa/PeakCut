"""Cancellation, live history, pooled AI, dashboard changes and clip editing."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import asyncio
import httpx
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-next-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_ACCOUNTS="0",
                  PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_FETCH_MODEL="0")
from fastapi.testclient import TestClient
from studio import ai, auth, brain, ctx, db, jobs, live, render, server, transcript

db.init()
OWNER = auth.local_user().id
client = TestClient(server.app, base_url="http://127.0.0.1:8765")
ORIGIN = {"Origin": "http://127.0.0.1:8765"}


class NextTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / "clips").mkdir()
        self.job = {"id": "next-job-123", "user": OWNER, "creator": None, "dir": str(self.root),
                    "source": {"type": "upload", "name": "Test"}, "source_path": str(self.root / "source.mp4"),
                    "title": "Test", "uploader": "", "status": "done", "stage": "Done", "duration": 30,
                    "kind": "video", "created": time.time(), "progress": 1, "ai": {}, "width": 320, "height": 240, "clips": []}
        self.clip = {"id": "clip01", "start": 2, "end": 8, "status": "done", "version": 1, "score": 90,
                     "title": "Test", "hook": "Hello", "description": "", "hashtags": [], "style": "clean", "layout": "blur"}
        self.job["clips"].append(self.clip)
        jobs._jobs[self.job["id"]] = self.job
        jobs._by_user.setdefault(OWNER, {})[self.job["id"]] = self.job
        self.addCleanup(lambda: jobs._jobs.pop(self.job["id"], None))
        self.addCleanup(lambda: jobs._by_user[OWNER].pop(self.job["id"], None))

    def test_cancelling_work_terminates_real_subprocess(self):
        from studio import work
        entered, finished = threading.Event(), threading.Event()
        outcomes = []
        marker = self.root / "finished"

        def run():
            try:
                with work.task(("cancel-test", None)):
                    entered.set()
                    work.run([sys.executable, "-c", "import time,pathlib; time.sleep(3); pathlib.Path(__import__('sys').argv[1]).touch()", str(marker)], timeout=10)
            except work.Cancelled:
                outcomes.append("cancelled")
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            work.cancel(("cancel-test", None))
            self.assertTrue(finished.wait(1), "stopping must release the worker promptly")
        finally:
            thread.join(4)
        self.assertEqual(outcomes, ["cancelled"])
        self.assertFalse(marker.exists())

    def test_processing_logs_reach_sync_and_stay_with_the_owner(self):
        from studio import joblog
        joblog.write("Download timed out; trying a clip part", "warning", job=self.job)
        response = client.get("/api/sync").json()
        job = next(j for j in response["jobs"] if j["id"] == self.job["id"])
        self.assertEqual(job["logs"][-1]["message"], "Download timed out; trying a clip part")
        self.job["user"] = "another-owner"
        jobs._by_user[OWNER].pop(self.job["id"])
        self.assertEqual(client.get(f"/api/jobs/{self.job['id']}").status_code, 404)
        self.assertNotIn(self.job["id"], [j["id"] for j in client.get("/api/sync").json()["jobs"]])

    def test_late_render_cannot_replace_a_cancelled_clip(self):
        entered, release = threading.Event(), threading.Event()
        self.clip["fixed_words"] = [{"w": "hello", "s": 3, "e": 4}]
        self.clip["fixed_for"] = [2, 8, "en2"]
        (self.root / "clips" / "clip01.mp4").write_text("original")

        def slow(src, out, name, *args, **kwargs):
            out.mkdir(exist_ok=True, parents=True)
            (out / "clip01.mp4").write_text("late")
            entered.set()
            release.wait(3)
            return {"file": "clip01.mp4", "cover": "clip01.jpg", "encoder": "libx264"}

        with ctx.as_user(OWNER), patch.object(render, "render_clip", side_effect=slow), patch.object(jobs.creators, "log"):
            worker = threading.Thread(target=ctx.bind(lambda: jobs.render_one(self.job, self.clip, [])))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                jobs.cancel(self.job["id"], self.clip["id"])
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(self.clip["status"], "error")
        self.assertEqual(self.clip["version"], 1)
        self.assertEqual((self.root / "clips" / "clip01.mp4").read_text(), "original")

    def test_live_store_reads_time_ranges_and_survives_reopen(self):
        store = transcript.LiveStore(self.root)
        words = [{"w": str(i), "s": i, "e": i + .5} for i in range(10000)]
        store.append(0, words)
        self.assertEqual(store.count(), 10000)
        self.assertEqual(store.between(9000, 9002), words[9000:9003])
        self.assertEqual(transcript.LiveStore(self.root).between(9999, 10000), words[-1:])
        self.assertEqual(transcript.read(self.root), words)

    def test_live_recent_history_is_bounded_but_old_words_remain_available(self):
        session = live.Session(self.job, {"buffer_min": 1, "ai_every_min": 1})
        words = [{"w": str(i), "s": i, "e": i + .5} for i in range(10000)]
        session._remember_words(words, 10000)
        self.assertLess(len(session.words), 1000)
        self.assertEqual(session.word_total, 10000)
        self.assertEqual(session._words_between(20, 22), words[20:23])

    def test_performance_response_reports_encoder_without_other_users_jobs(self):
        self.job["timings"] = {"render": 2, "render_queue": 1}
        with patch.object(render, "encoder_status", return_value={"preferred": "h264_videotoolbox", "active": 0, "last": "h264_videotoolbox"}):
            response = client.get("/api/performance")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["encoder"]["last"], "h264_videotoolbox")
        self.assertEqual(body["jobs"][0]["timings"]["render"], 2)
        self.assertNotIn(str(self.root), json.dumps(body))

    def test_ai_reuses_one_client_and_cancels_slow_requests_at_deadline(self):
        cancelled = threading.Event()

        async def reply(request):
            if request.url.path == "/slow":
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return httpx.Response(200, json={"ok": True})

        pool = ai.ConnectionPool(transport=httpx.MockTransport(reply))
        self.addCleanup(pool.close)
        with patch.object(ai, "_pool", pool):
            with ai.operation(1):
                self.assertEqual(ai._post("https://test/fast").status_code, 200)
                self.assertEqual(ai._post("https://test/fast").status_code, 200)
            self.assertEqual(pool.clients_created, 1)
            started = time.monotonic()
            with self.assertRaises(TimeoutError), ai.operation(.05):
                ai._post("https://test/slow")
            self.assertLess(time.monotonic() - started, .5)
            self.assertTrue(cancelled.wait(1), "a timed-out request must release its connection")

    def test_ai_retry_respects_server_delay_with_one_operation_budget(self):
        error = ai.AIError("HTTP 429 busy", retry_after=2.5)
        with patch.object(brain.config, "ai_ready", return_value=True), \
                patch.object(ai, "models", return_value=["test"]), \
                patch.object(ai, "call", side_effect=[error, ({"ok": True}, 1, 2)]), \
                patch.object(ai, "pause") as pause, patch.object(brain.usage, "record"):
            result = brain.ask_ai("test")
        self.assertEqual(result[0], {"ok": True})
        pause.assert_called_once_with(2.5)

    def test_sync_delta_includes_only_changes_and_deletions(self):
        with ctx.as_user(OWNER):
            ctx.bump()
        first = client.get("/api/sync?delta=1").json()
        self.assertIn(self.job["id"], [j["id"] for j in first["jobs"]])
        self.job["stage"] = "Changed"
        jobs.save(self.job)
        second = client.get("/api/sync", params={"delta": 1, "v": first["v"]}).json()
        self.assertTrue(second["delta"])
        self.assertEqual([j["id"] for j in second["jobs"]], [self.job["id"]])
        self.assertNotIn("accounts", second)
        jobs._jobs.pop(self.job["id"])
        jobs._by_user[OWNER].pop(self.job["id"])
        with ctx.as_user(OWNER):
            ctx.bump()
        third = client.get("/api/sync", params={"delta": 1, "v": second["v"]}).json()
        self.assertEqual(third["removed_jobs"], [self.job["id"]])

    def test_caption_edits_are_validated_and_kept_when_retrimmed(self):
        words = [{"w": "Corrected", "s": 3, "e": 4}]
        with ctx.as_user(OWNER), patch.object(jobs._q, "put"):
            jobs.request_render(self.job["id"], "clip01", {"words": words})
            self.assertEqual(self.clip["caption_words"], words)
            jobs.request_render(self.job["id"], "clip01", {"start": 1, "end": 9})
            self.assertEqual(self.clip["caption_words"], words)
            with self.assertRaises(ValueError):
                jobs.request_render(self.job["id"], "clip01", {"words": [{"w": "bad", "s": float("nan"), "e": 4}]})

    def test_editor_and_cancel_routes_do_not_allow_another_users_video(self):
        self.job["user"] = "another-owner"
        jobs._by_user[OWNER].pop(self.job["id"])
        for method, route in [("GET", "editor"), ("GET", "source"), ("POST", "preview")]:
            response = client.request(method, f"/api/jobs/{self.job['id']}/clips/clip01/{route}", headers=ORIGIN)
            self.assertEqual(response.status_code, 404)
        response = client.post(f"/api/jobs/{self.job['id']}/cancel", json={}, headers=ORIGIN)
        self.assertEqual(response.status_code, 404)

    def test_editor_returns_waveform_and_words_without_internal_paths(self):
        (self.root / "source.mp4").touch()
        transcript.checkpoint(self.root, [{"w": "hello", "s": 3, "e": 4}])
        with patch.object(server, "_editor_waveform", return_value=[.2, .8]):
            response = client.get(f"/api/jobs/{self.job['id']}/clips/clip01/editor")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["peaks"], [.2, .8])
        self.assertEqual(body["words"][0]["w"], "hello")
        self.assertNotIn(str(self.root), json.dumps(body))

    def test_preview_uses_edits_and_never_changes_the_saved_clip(self):
        (self.root / "source.mp4").touch()
        original = dict(self.clip)

        def preview(src, folder, name, edited, words, *args):
            self.assertTrue(edited["_preview"])
            self.assertLessEqual(edited["end"] - edited["start"], 8)
            self.assertEqual(words[0]["w"], "Corrected")
            (folder / (name + ".mp4")).write_bytes(b"preview")
            return {"file": name + ".mp4"}

        with patch.object(render, "render_clip", side_effect=preview):
            response = client.post(f"/api/jobs/{self.job['id']}/clips/clip01/preview",
                json={"start": 1, "end": 15, "words": [{"w": "Corrected", "s": 3, "e": 4}]}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["seconds"], 8)
        self.assertEqual(self.clip, original)
        self.assertEqual(client.get(response.json()["file"]).content, b"preview")

    def test_invalid_caption_edits_do_not_change_saved_state(self):
        original = dict(self.clip)
        response = client.post(f"/api/jobs/{self.job['id']}/clips/clip01/render",
            json={"words": [{"w": "bad", "s": 5, "e": 2}]}, headers=ORIGIN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.clip, original)

    def test_cancel_route_marks_queued_job_and_refuses_live_recording(self):
        self.job["status"] = "queued"
        self.clip["status"] = "queued"
        response = client.post(f"/api/jobs/{self.job['id']}/cancel", json={}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.job["status"], "cancelled")
        self.assertEqual(self.clip["status"], "error")
        self.job.update(kind="live", status="live")
        self.assertEqual(client.post(f"/api/jobs/{self.job['id']}/cancel", json={}, headers=ORIGIN).status_code, 409)

    def test_late_cancelled_render_cannot_mark_a_new_render_failed(self):
        first_entered, second_entered, first_release, second_release = (threading.Event() for _ in range(4))
        self.clip["fixed_for"] = [2, 8, "en2"]
        self.clip["fixed_words"] = [{"w": "hello", "s": 3, "e": 4}]
        calls = 0

        def encode(src, out, name, *args):
            nonlocal calls
            calls += 1
            first = calls == 1
            (first_entered if first else second_entered).set()
            (first_release if first else second_release).wait(3)
            (out / "clip01.mp4").write_text("old" if first else "new")
            return {"file": "clip01.mp4"}

        with ctx.as_user(OWNER), patch.object(render, "render_clip", side_effect=encode), patch.object(jobs.creators, "log"):
            one = threading.Thread(target=ctx.bind(lambda: jobs.render_one(self.job, self.clip, [])))
            two = threading.Thread(target=ctx.bind(lambda: jobs.render_one(self.job, self.clip, [])))
            one.start()
            try:
                self.assertTrue(first_entered.wait(1))
                jobs.cancel(self.job["id"], "clip01")
                two.start()
                self.assertTrue(second_entered.wait(1))
                first_release.set()
                one.join(1)
                self.assertEqual(self.clip["status"], "rendering")
            finally:
                first_release.set(); second_release.set()
                one.join(3)
                if two.ident:
                    two.join(3)
        self.assertEqual(self.clip["status"], "done")
        self.assertEqual((self.root / "clips" / "clip01.mp4").read_text(), "new")


if __name__ == "__main__":
    unittest.main()
