"""YouTube must hold the publishing time itself; these tests never contact a platform."""
from datetime import datetime, timezone
import json
import os
import secrets
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
data = tempfile.TemporaryDirectory(prefix="pc-youtube-schedule-")
os.environ.update(PEAKCUT_DATA=data.name, DATABASE_URL="", PEAKCUT_PUBLIC_URL="http://testserver",
                  PEAKCUT_ACCOUNTS="0", PEAKCUT_FETCH_MODEL="0", PEAKCUT_UPDATE_CHECK="0")
from studio import accounts, auth, config, ctx, db, jobs, posting, server
from fastapi.testclient import TestClient

db.init()


class YouTubeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "clip.mp4"
        self.path.write_bytes(b"test-video")
        self.when = time.time() + 7200
        self.clip = {"id": "clip01", "status": "done", "file": "clip.mp4", "title": "A moment",
                     "description": "A highlight", "hashtags": [], "score": 80}
        self.job = {"id": "job01", "creator": None, "user": "owner", "dir": self.folder.name, "clips": [self.clip]}
        self.acc = {"id": "yt1", "platform": "youtube", "name": "My channel"}
        self.connections = {"yt1": self.acc, "ig1": {"id": "ig1", "platform": "instagram", "name": "Instagram"}}
        for target, value in (("_FILE", Path(self.folder.name) / "posts.json"), ("_posts", [])):
            p = patch.object(posting, target, value)
            p.start(); self.addCleanup(p.stop)
        for target, name, value in ((accounts, "get", self.connections.get), (posting.creators, "get", lambda _: None),
                                    (jobs, "get", lambda _: self.job), (jobs, "save", lambda _: None),
                                    (posting.creators, "log", lambda *a: None), (posting.alerts, "notify", lambda *a, **k: None)):
            p = patch.object(target, name, side_effect=value)
            p.start(); self.addCleanup(p.stop)
        user = ctx.as_user("owner")
        user.__enter__(); self.addCleanup(lambda: user.__exit__(None, None, None))

    def queue(self, when=None, selected=None):
        try:
            return posting.enqueue(self.job, self.clip, selected or ["yt1"], publish_at=self.when if when is None else when)
        except TypeError as error:
            self.fail("YouTube scheduling is missing: " + str(error))

    def upload(self, status=None):
        calls = []
        def post(url, **kwargs):
            calls.append(kwargs)
            return httpx.Response(200, headers={"Location": "https://upload.example/session"}, request=httpx.Request("POST", url))
        def put(url, **kwargs):
            body = {"id": "remote01", "status": status if status is not None else calls[0]["json"]["status"]}
            return httpx.Response(200, json=body, request=httpx.Request("PUT", url))
        with patch.object(accounts, "_yt_token", return_value="test-token"), \
                patch.object(accounts.httpx, "post", side_effect=post), patch.object(accounts.httpx, "put", side_effect=put):
            try:
                result = accounts.youtube_upload(self.acc, self.path, "Title", "Description", [], "public", publish_at=self.when)
            except TypeError as error:
                self.fail("YouTube upload cannot carry a publishing time: " + str(error))
        return result, calls[0]["json"]

    def test_upload_is_private_with_a_utc_publishing_time(self):
        result, body = self.upload()
        self.assertEqual(result[0], "remote01")
        self.assertEqual(body["status"]["privacyStatus"], "private")
        actual = datetime.fromisoformat(body["status"]["publishAt"].replace("Z", "+00:00"))
        self.assertAlmostEqual(actual.timestamp(), self.when, places=3)
        self.assertEqual(actual.utcoffset().total_seconds(), 0)

    def test_youtube_must_confirm_the_schedule(self):
        with self.assertRaisesRegex(ValueError, "YouTube Studio"):
            self.upload(status={"privacyStatus": "private"})

    def test_upload_starts_now_instead_of_waiting_for_publication(self):
        p = self.queue()[0]
        self.assertLess(p["due_at"], time.time() + 2)
        self.assertEqual(p["publish_at"], self.when)
        self.assertEqual(p["status"], "scheduled")

    def test_invalid_or_past_times_leave_no_posts(self):
        for value in ("tomorrow", "", True, [], {}, float("nan"), float("inf"), -1, time.time() - 1, time.time() + 10):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.queue(when=value)
        self.assertEqual(posting._posts, [])

    def test_mixed_platforms_are_rejected_before_anything_is_queued(self):
        with self.assertRaisesRegex(ValueError, "YouTube"):
            self.queue(selected=["yt1", "ig1"])
        self.assertEqual(posting._posts, [])

    def test_disconnected_account_is_rejected(self):
        with self.assertRaises(ValueError):
            self.queue(selected=["someone-elses-account"])
        self.assertEqual(posting._posts, [])

    def test_success_is_marked_as_scheduled_on_youtube_and_saved(self):
        p = self.queue()[0]
        with patch.object(accounts, "youtube_upload", return_value=("remote01", "https://youtube.com/watch?v=remote01")) as upload:
            posting._post(p)
        self.assertEqual(upload.call_args.kwargs["publish_at"], self.when)
        self.assertEqual(p["status"], "youtube_scheduled")
        self.assertEqual(p["remote_id"], "remote01")
        self.assertEqual(self.clip["posts"][p["id"]]["publish_at"], self.when)
        self.assertEqual(json.loads(posting._FILE.read_text())[0]["status"], "youtube_scheduled")
        self.assertEqual(self.queue(), [])  # closing/reopening must not cause a second upload
        with self.assertRaises(ValueError):
            posting.cancel(p["id"])  # local Cancel must not pretend to remove a YouTube schedule

    def test_scheduled_upload_failure_is_not_retried_automatically(self):
        p = self.queue()[0]
        with patch.object(accounts, "youtube_upload", side_effect=ValueError("Upload failed")):
            posting._post(p)
        self.assertEqual(p["status"], "failed")
        self.assertIn("YouTube Studio", p["error"])

    def test_expired_schedule_is_refused_before_an_upload(self):
        p = self.queue()[0]
        p["publish_at"] = time.time() - 60
        with patch.object(accounts, "youtube_upload") as upload:
            posting._post(p)
        upload.assert_not_called()
        self.assertEqual(p["status"], "failed")

    def test_other_users_cannot_see_scheduled_uploads(self):
        p = self.queue()[0]
        with ctx.as_user("other"):
            self.assertEqual(posting._mine(), [])
            with self.assertRaises(ValueError):
                posting.cancel(p["id"])

    def test_worker_saves_uploading_state_before_contacting_youtube(self):
        p = self.queue()[0]
        observed = []
        def upload(_):
            observed.append(json.loads(posting._FILE.read_text())[0]["status"])
        with patch.object(posting.time, "sleep", side_effect=[None, InterruptedError]), \
                patch.object(accounts, "settings", return_value={"paused": False}), patch.object(posting, "_post", side_effect=upload):
            with self.assertRaises(InterruptedError):
                posting._run()
        self.assertEqual(observed, ["posting"])

    def test_uploaded_but_unconfirmed_video_needs_review_and_cannot_be_uploaded_again(self):
        p = self.queue()[0]
        with patch.object(accounts, "youtube_upload", side_effect=accounts.YouTubeScheduleUnconfirmed("remote01")):
            posting._post(p)
        self.assertEqual(p["status"], "needs_review")
        self.assertEqual(p["remote_id"], "remote01")
        self.assertIn("studio.youtube.com", p["url"])
        self.assertEqual(self.queue(), [])

    def test_restart_preserves_remote_schedule_and_does_not_upload_again(self):
        p = self.queue()[0]
        p.update(status="youtube_scheduled", remote_id="remote01")
        with patch.object(posting.threading, "Thread"), patch.object(posting.jobs, "on_clip_ready", []):
            posting.start()
        self.assertEqual(p["status"], "youtube_scheduled")

    def test_restart_requires_review_of_interrupted_upload(self):
        p = self.queue()[0]
        p["status"] = "posting"
        with patch.object(posting.threading, "Thread"), patch.object(posting.jobs, "on_clip_ready", []):
            posting.start()
        self.assertEqual(p["status"], "failed")
        self.assertIn("Check", p["error"])

    def test_existing_queue_keeps_its_timing_and_privacy(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"])[0]
        self.assertNotIn("publish_at", p)
        with patch.object(accounts, "youtube_upload", return_value=("id", "url")) as upload:
            posting._post(p)
        self.assertNotIn("publish_at", upload.call_args.kwargs)
        self.assertEqual(p["status"], "youtube_processing")

    def test_upload_progress_reaches_the_clip_and_is_saved(self):
        p = self.queue()[0]
        p["status"] = "posting"
        def upload(*args, **kwargs):
            kwargs["upload_state"].update(session="https://upload.example/private-session", size=100)
            kwargs["on_progress"](40, 100, "uploading")
            mirrored = self.clip["posts"][p["id"]]
            self.assertEqual(mirrored["upload_bytes"], 40)
            self.assertEqual(mirrored["upload_total"], 100)
            self.assertNotIn("upload_state", mirrored)
            self.assertEqual(json.loads(posting._FILE.read_text())[0]["upload_bytes"], 40)
            return "remote01", "https://youtube.com/watch?v=remote01"
        with patch.object(accounts, "youtube_upload", side_effect=upload):
            posting._post(p)

    def test_connection_failure_never_creates_an_automatic_second_upload(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"], now=True)[0]
        with patch.object(accounts, "youtube_upload", side_effect=httpx.ReadTimeout("No reply")):
            posting._post(p)
        self.assertEqual(p["status"], "failed")
        self.assertIn("YouTube Studio", p["error"])

    def test_youtube_processing_becomes_posted_only_after_confirmation(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"], now=True)[0]
        p.update(status="youtube_processing", remote_id="remote01")
        with patch.object(accounts, "youtube_status", return_value={"remote01": {
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "processingDetails": {"processingStatus": "succeeded"}}}):
            posting._check_youtube([p])
        self.assertEqual(p["status"], "posted")
        self.assertEqual(self.clip["posts"][p["id"]]["status"], "posted")

    def test_processing_failure_keeps_the_video_id_and_reason(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"], now=True)[0]
        p.update(status="youtube_processing", remote_id="remote01")
        with patch.object(accounts, "youtube_status", return_value={"remote01": {
                "status": {"uploadStatus": "failed", "failureReason": "invalidFile"},
                "processingDetails": {"processingStatus": "failed"}}}):
            posting._check_youtube([p])
        self.assertEqual(p["status"], "needs_review")
        self.assertIn("invalidFile", p["error"])
        self.assertEqual(p["remote_id"], "remote01")

    def test_processing_without_an_estimate_is_not_marked_as_posted(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"], now=True)[0]
        p.update(status="youtube_processing", remote_id="remote01")
        with patch.object(accounts, "youtube_status", return_value={"remote01": {
                "status": {"uploadStatus": "uploaded"}, "processingDetails": {
                    "processingStatus": "processing", "processingProgress": {"partsTotal": "0"}}}}):
            posting._check_youtube([p])
        self.assertEqual(p["status"], "youtube_processing")
        self.assertNotIn("posted_at", p)

    def test_processing_check_connection_error_is_visible_and_does_not_reupload(self):
        p = posting.enqueue(self.job, self.clip, ["yt1"], now=True)[0]
        p.update(status="youtube_processing", remote_id="remote01")
        with patch.object(accounts, "youtube_status", side_effect=httpx.ReadTimeout("No reply")), \
                patch.object(accounts, "youtube_upload") as upload:
            posting._check_youtube([p])
        self.assertEqual(p["status"], "youtube_processing")
        self.assertIn("Will check again", p["error"])
        upload.assert_not_called()

    def test_upload_session_never_appears_in_the_activity_response(self):
        p = self.queue()[0]
        p["upload_state"] = {"session": "https://upload.example/private-session"}
        with patch.object(posting.creators, "all_creators", return_value=[]), \
                patch.object(jobs, "list_jobs", return_value=[]), \
                patch.object(accounts, "settings", return_value=dict(accounts.SETTING_DEFAULTS)), \
                patch.object(posting.live, "active_mine", return_value=[]):
            self.assertNotIn("private-session", json.dumps(posting.overview()))


class YouTubeUploadTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "clip.mp4"
        self.path.write_bytes(b"video-content")
        self.acc = {"id": "yt1"}
        token = patch.object(accounts, "_yt_token", return_value="test-token")
        token.start(); self.addCleanup(token.stop)

    def response(self, code, **kwargs):
        return httpx.Response(code, request=httpx.Request("PUT", "https://upload.example/session"), **kwargs)

    def upload(self, state, progress):
        return accounts.youtube_upload(self.acc, self.path, "Title", "Description", [], "public",
                                       upload_state=state, on_progress=lambda *args: progress.append(args))

    def test_chunks_report_only_bytes_youtube_has_acknowledged(self):
        self.path.write_bytes(b"x" * (2 * 1024 * 1024 + 9))
        responses = [self.response(308, headers={"Range": "bytes=0-1048575"}),
                     self.response(308, headers={"Range": "bytes=0-2097151"}),
                     self.response(201, json={"id": "remote01"})]
        progress, state = [], {}
        with patch.object(accounts.httpx, "post", return_value=self.response(200, headers={"Location": "https://upload.example/session"})), \
                patch.object(accounts.httpx, "put", side_effect=responses) as put:
            self.assertEqual(self.upload(state, progress)[0], "remote01")
        self.assertEqual([call.kwargs["headers"]["Content-Range"] for call in put.call_args_list],
                         ["bytes 0-1048575/2097161", "bytes 1048576-2097151/2097161", "bytes 2097152-2097160/2097161"])
        self.assertTrue(any(p[0] == 1048576 for p in progress))
        self.assertEqual(progress[-1][0], self.path.stat().st_size)

    def test_timeout_queries_the_same_session_and_resumes_from_server_offset(self):
        responses = [httpx.ReadTimeout("No reply"), self.response(308, headers={"Range": "bytes=0-4"}),
                     self.response(201, json={"id": "remote01"})]
        progress, state = [], {}
        with patch.object(accounts.httpx, "post", return_value=self.response(200, headers={"Location": "https://upload.example/session"})) as start, \
                patch.object(accounts.httpx, "put", side_effect=responses) as put:
            self.upload(state, progress)
        self.assertEqual(start.call_count, 1)
        self.assertEqual(put.call_args_list[1].kwargs["headers"]["Content-Range"], "bytes */13")
        self.assertEqual(put.call_args_list[2].kwargs["content"], b"-content")

    def test_restart_uses_saved_session_instead_of_creating_another_video(self):
        state = {"session": "https://upload.example/session", "size": 13}
        with patch.object(accounts.httpx, "post") as start, patch.object(accounts.httpx, "put", side_effect=[
                self.response(308, headers={"Range": "bytes=0-4"}), self.response(201, json={"id": "remote01"})]) as put:
            self.upload(state, [])
        start.assert_not_called()
        self.assertEqual(put.call_args_list[1].kwargs["content"], b"-content")

    def test_lost_final_reply_recovers_video_id_without_resending_file(self):
        with patch.object(accounts.httpx, "post", return_value=self.response(200, headers={"Location": "https://upload.example/session"})), \
                patch.object(accounts.httpx, "put", side_effect=[httpx.ReadTimeout("No reply"),
                    self.response(201, json={"id": "remote01"})]) as put:
            self.assertEqual(self.upload({}, [])[0], "remote01")
        self.assertEqual(put.call_count, 2)
        self.assertEqual(put.call_args_list[1].kwargs["content"], b"")

    def test_stalled_connection_is_bounded_and_keeps_its_session(self):
        state = {}
        with patch.object(accounts.httpx, "post", return_value=self.response(200, headers={"Location": "https://upload.example/session"})) as start, \
                patch.object(accounts.httpx, "put", side_effect=httpx.ReadTimeout("No reply")) as put:
            with self.assertRaisesRegex(RuntimeError, "paused|resume|YouTube Studio"):
                self.upload(state, [])
        self.assertEqual(start.call_count, 1)
        self.assertLessEqual(put.call_count, 5)
        self.assertTrue(state.get("session"))

    def test_changed_clip_is_not_spliced_into_an_existing_upload(self):
        state = {"session": "https://upload.example/session", "size": 13, "mtime": -1}
        with patch.object(accounts.httpx, "post") as post, patch.object(accounts.httpx, "put") as put:
            with self.assertRaisesRegex(ValueError, "clip changed"):
                self.upload(state, [])
        post.assert_not_called()
        put.assert_not_called()


class ScheduleApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=data.name)
        self.addCleanup(self.folder.cleanup)
        self.owner, _ = auth._create(f"schedule-{secrets.token_hex(5)}@example.com", "Owner", password_hash="x")
        self.client = TestClient(server.app, base_url="http://testserver")
        self.client.cookies.set(auth.COOKIE, auth.start_session(self.owner.id))
        clip = {"id": "clip01", "status": "done", "title": "Clip"}
        self.job = {"id": "job01", "user": self.owner.id, "creator": None, "dir": self.folder.name, "clips": [clip]}
        for obj, attr, value in ((config, "ACCOUNTS", True), (jobs, "_jobs", {"job01": self.job}),
                                 (posting, "_posts", []), (posting, "_FILE", Path(self.folder.name) / "posts.json")):
            p = patch.object(obj, attr, value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(accounts, "get", side_effect=lambda aid: {"id": "yt1", "name": "YouTube", "platform": "youtube"}
                         if aid == "yt1" and ctx.uid() == self.owner.id else None)
        p.start(); self.addCleanup(p.stop)

    def post(self, **extra):
        return self.client.post("/api/posts", json={"job": "job01", "clip": "clip01", "accounts": ["yt1"], **extra},
                                headers={"Origin": "http://testserver"})

    def test_endpoint_saves_absolute_device_time(self):
        # An absolute time from the device must not be interpreted in the server's time zone.
        selected = datetime.now(timezone.utc).replace(microsecond=0).timestamp() + 86400
        reply = self.post(publish_at=selected)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(posting._posts[0]["publish_at"], selected)
        self.assertEqual(posting._posts[0]["user"], self.owner.id)

    def test_bad_time_is_a_clear_client_error(self):
        reply = self.post(publish_at="tomorrow")
        self.assertEqual(reply.status_code, 400)
        self.assertEqual(posting._posts, [])

    def test_account_list_must_be_a_list(self):
        self.assertEqual(self.post(accounts="yt1", publish_at=time.time() + 3600).status_code, 400)

    def test_cannot_schedule_another_users_video(self):
        self.job["user"] = "another-user"
        self.assertEqual(self.post(publish_at=time.time() + 3600).status_code, 404)
        self.assertEqual(posting._posts, [])

    def test_local_queue_still_works_without_a_publishing_time(self):
        reply = self.post()
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertNotIn("publish_at", posting._posts[0])


if __name__ == "__main__":
    unittest.main()
