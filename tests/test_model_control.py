import json
import threading
import unittest
from unittest.mock import Mock, patch

from model_control import ControlError, ModelControl


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.control = ModelControl("atc", None, "http://127.0.0.1:1339", "secret-test-key")
        self.replies = {
            "engines.list": {"engines": [
                {"engine": "llamacpp-upstream", "latest": {"version": "1"}},
                {"engine": "atomic-prism", "latest": {"version": "1"}},
                {"engine": "vllm", "latest": {"version": "1"}},
                {"engine": "mlx", "active": {"version": "1"}},
                {"engine": "sglang"},
            ]},
            "models.installed": [],
            "artifacts.list": [],
            "setup.status": {"setups": []},
            "managed.list": [],
            "media.status": {"model": {"state": "unloaded"}},
        }
        self.control.call = Mock(side_effect=lambda name, *args, **kwargs: self.replies[name])

    def test_gguf_selects_file_and_retries_the_same_install_request(self):
        self.replies.update({
            "models.search": {"rows": [{"id": "owner/chat"}]},
            "models.files": [
                {"filename": "models/quant/Q4 chat.gguf", "size": 7091, "revision": "a" * 40},
                {"filename": "../../private.gguf"},
            ],
            "models.plan": {"digest": "b" * 64, "total_download_bytes": 7091},
            "models.install": {},
        })
        result = self.control.browse({"category": "text", "query": "chat"}, search=True)
        self.assertEqual(result["providers"], ["gguf", "vllm", "mlx"])
        selection = result["models"][0]["id"]
        variants = self.control.variants({"id": selection})["variants"]
        self.assertEqual([variant["label"] for variant in variants], ["Q4 chat.gguf"])
        variant = variants[0]
        review = self.control.review({"id": variant["id"]})
        self.assertEqual(review["bytes"], 7091)
        def transient(operation, *args, **kwargs):
            if operation == "models.install":
                raise ControlError(504, "model_control_timeout")
            return self.replies[operation]

        original_effect = self.control.call.side_effect
        self.control.call.side_effect = transient
        with self.assertRaises(ControlError):
            self.control.download(review)
        self.control.call.side_effect = original_effect
        first_args = self.control.call.call_args.args
        self.assertTrue(self.control.download(review)["accepted"])
        self.assertEqual(self.control.call.call_args.args, first_args)
        self.assertEqual(first_args[1]["request"], {
            "repo": "owner/chat", "file": "models/quant/Q4 chat.gguf",
            "revision": "a" * 40, "include_projector": True,
        })
        calls = self.control.call.call_count
        self.control.download(review)
        self.assertEqual(self.control.call.call_count, calls)

    def test_managed_search_and_catalog_and_refreshed_blockers(self):
        self.replies.update({
            "models.catalog": {"models": [{"repository": "owner/chat"}]},
            "models.search": {"rows": [{"repository": "owner/chat"}]},
            "managed.plan": {
                "digest": "c" * 64, "download_bytes": 500, "engine": "vllm",
                "verdict": {"compatibility": {"verdict": {"ok": True}}},
                "blockers": ["Not enough free space in /private/path"],
            },
            "managed.install": {},
        })
        data = {"category": "text", "provider": "vllm", "query": "chat"}
        result = self.control.browse(data)
        self.assertEqual(result["models"], self.control.browse(data, search=True)["models"])
        variant = self.control.variants(result["models"][0])["variants"][0]
        review = self.control.review(variant)
        self.assertTrue(review["blocked"])
        self.assertNotIn("/private", json.dumps(review))
        with self.assertRaises(ControlError):
            self.control.download(review)
        self.replies["managed.plan"]["blockers"].clear()
        fresh = self.control.review(variant)
        self.assertEqual(fresh["id"], review["id"])
        self.assertFalse(fresh["blocked"])
        self.assertTrue(self.control.download(fresh)["accepted"])
        self.control.call.assert_called_with("managed.install", {
            "plan_digest": "c" * 64, "request_id": self.control.reviews[fresh["id"]]["request_id"],
        })

    def test_image_repository_search_and_mlx_use_artifact_workflow(self):
        self.replies.update({
            "models.catalog": {"rows": [{"id": "example", "name": "Example", "source": {
                "engines": ["sdcpp"], "transformer": {"repo": "owner/image", "quants": [{"id": "q4"}]},
            }}]},
            "models.search": {"rows": [{"id": "owner/mlx"}]},
            "artifacts.plan": {"digest": "d" * 64},
            "artifacts.install": {},
        })
        for data, expected in (
            ({"category": "image", "query": "owner/image"},
             {"category": "image", "model_id": "example", "quant": "q4"}),
            ({"category": "text", "query": "mlx", "provider": "mlx"},
             {"category": "text", "model_id": "owner/mlx"}),
        ):
            row = self.control.browse(data, search=True)["models"][0]
            variant = self.control.variants(row)["variants"][0]
            review = self.control.review(variant)
            self.control.call.assert_called_with("artifacts.plan", expected, timeout=90)
            self.control.download(review)
            self.assertEqual(self.control.call.call_args.args[0], "artifacts.install")

    def test_inventory_and_load_failure_and_stale_selection(self):
        self.replies["models.installed"] = [
            {"id": "/private/file.gguf", "provider": "llamacpp"},
            {"id": "owner/mlx", "provider": "mlx"},
        ]
        self.replies["artifacts.list"] = [
            {"id": "image-id", "stage": "ready", "plan": {"category": "image", "name": "Image"}},
            {"id": "mlx-id", "stage": "ready", "plan": {
                "category": "text", "engine": "mlx", "model_id": "owner/mlx", "name": "MLX",
            }},
        ]
        models = self.control.installed()["models"]
        self.assertEqual(len(models), 3)
        self.assertNotIn("/private", json.dumps(models))
        with self.assertRaises(ControlError) as stale:
            self.control.load({"id": "removed"})
        self.assertEqual(stale.exception.status, 409)
        load = next(row for row in models if row["provider"] == "mlx")
        self.control.verify_instance = Mock(return_value={})
        completed = threading.Event()

        def fail(operation, *args, **kwargs):
            if operation == "artifacts.load":
                raise ControlError(502, "model_control_failed")
            return self.replies[operation]

        finish = self.control.finish_load
        def finish_and_signal(*args):
            finish(*args)
            completed.set()

        self.control.call.side_effect = fail
        with patch.object(self.control, "finish_load", finish_and_signal):
            self.assertEqual(self.control.load(load)["load"]["status"], "loading")
            self.assertTrue(completed.wait(2))
        self.control.call.assert_called_with("artifacts.load", {"id": "mlx-id"}, timeout=600)
        self.assertEqual(self.control.status({})["loads"][0]["status"], "failed")

    def test_status_uses_durable_jobs_and_native_task_progress(self):
        self.replies["models.installed"] = [{"id": "owner/chat", "provider": "llamacpp"}]
        self.replies["setup.status"]["setups"] = [{
            "setup_id": "setup", "task_ids": {"model": "task"}, "stage": "downloading",
            "plan": {"model_id": "owner/chat", "total_download_bytes": 20},
        }]
        instance = {"downloads": [{"taskId": "task", "transferred": 4, "total": 20}]}
        status = self.control.status(instance)
        self.assertEqual(status["downloads"], [
            {"category": "text", "label": "chat", "status": "downloading",
             "active": True, "bytes": 4, "total": 20},
        ])
        self.replies["setup.status"]["setups"][0]["stage"] = "ready"
        self.assertEqual(self.control.status({})["downloads"][0]["bytes"], 20)
        self.replies["models.installed"].clear()
        self.assertEqual(self.control.status({})["downloads"], [])

    def test_instance_verification_fails_closed(self):
        status = {"running": True, "api": {"running": True, "host": "127.0.0.1", "port": 1338}}
        with patch.object(self.control, "run", return_value=status):
            with self.assertRaises(ControlError) as error:
                self.control.dispatch("installed", "GET", {})
            self.assertEqual(error.exception.code, "model_control_instance")
            self.control.call.assert_not_called()
            status["api"]["port"] = 1339
            self.assertEqual(self.control.dispatch("installed", "GET", {}), {"models": []})

    def test_instance_verification_matches_wildcard_binds_to_loopback(self):
        for bind, hostname, allowed in (
            ("0.0.0.0", "127.0.0.1", True),
            ("0.0.0.0", "localhost", True),
            ("::", "[::1]", True),
            ("::", "127.0.0.1", False),
            ("0.0.0.0", "[::1]", False),
            ("172.16.120.22", "127.0.0.1", False),
        ):
            with self.subTest(bind=bind, hostname=hostname):
                control = ModelControl("atc", None, f"http://{hostname}:1339", "secret-test-key")
                status = {"running": True, "api": {"running": True, "host": bind, "port": 1339}}
                with patch.object(control, "run", return_value=status):
                    if allowed:
                        self.assertEqual(control.verify_instance(), status)
                    else:
                        with self.assertRaises(ControlError) as error:
                            control.verify_instance()
                        self.assertEqual(error.exception.code, "model_control_instance")

    def test_instance_verification_reports_when_atomic_is_stopped(self):
        with patch.object(self.control, "run", return_value={"running": False}):
            with self.assertRaises(ControlError) as error:
                self.control.verify_instance()
            self.assertEqual(error.exception.code, "model_control_stopped")

    def test_live_loaded_sessions_and_image_unload_use_exact_identity(self):
        instance = {"sessions": [
            {"provider": "llamacpp-upstream", "model_id": "/private/chat.gguf"},
            {"provider": "strata", "model_id": "owner/native"},
        ]}
        self.replies["media.status"] = {"model": {"state": "loaded", "loaded": {
            "modality": "image", "modelId": "image:first", "displayName": "First image",
        }}}
        self.replies["models.unload"] = {"success": True}
        self.replies["media.unload"] = {}
        with patch.object(self.control, "verify_instance", return_value=instance):
            status = self.control.status(instance)
            self.assertEqual(status["loaded_available"], {"text": True, "image": True})
            self.assertNotIn("/private", json.dumps(status))
            self.assertEqual(len(status["loaded"]), 3)
            chat, _, image = status["loaded"]
            self.assertEqual(self.control.unload(chat), {"category": "text"})
            self.control.call.assert_called_with(
                "models.unload", {"provider": "llamacpp-upstream", "model_id": "/private/chat.gguf"},
            )
            self.replies["models.unload"] = {"success": False, "error": "/private/process"}
            with self.assertRaises(ControlError) as failed:
                self.control.unload(chat)
            self.assertEqual(failed.exception.code, "model_control_failed")
            self.replies["media.status"]["model"]["loaded"]["modelId"] = "image:second"
            with self.assertRaises(ControlError) as stale:
                self.control.unload(image)
            self.assertEqual(stale.exception.code, "model_control_stale")
            self.assertFalse(any(call.args[0] == "media.unload" for call in self.control.call.call_args_list))
            image = self.control.status(instance)["loaded"][-1]
            self.assertEqual(self.control.unload(image), {"category": "image"})
            self.control.call.assert_called_with("media.unload")

    def test_deletion_routes_completed_downloads_to_atomic_owners(self):
        self.replies["models.installed"] = [
            {"id": "chat", "provider": "llamacpp"},
            {"id": "owner/managed", "provider": "vllm", "providers": []},
            {"id": "imported", "provider": "llamacpp"},
            {"id": "pending", "provider": "llamacpp"},
            {"id": "chat", "provider": "mlx"},
        ]
        self.replies["setup.status"]["setups"] = [
            {"setup_id": "chat-job", "stage": "ready", "plan": {"model_id": "chat"}},
            {"setup_id": "new-chat-job", "stage": "ready", "plan": {"model_id": "chat"}, "updated_at": 100},
            {"setup_id": "pending-job", "stage": "downloading", "plan": {"model_id": "pending"}},
        ]
        self.replies["managed.list"] = [
            {"id": "managed-job", "stage": "ready", "plan": {"repository": "owner/managed"}},
        ]
        self.replies["artifacts.list"] = [
            {"id": "mlx-job", "stage": "ready", "plan": {"model_id": "owner/mlx", "category": "text", "engine": "mlx"}},
            {"id": "image-job", "stage": "ready", "plan": {"model_id": "image:q4", "category": "image"}},
        ]
        self.replies["downloads.remove"] = {"removed": True}
        self.replies["models.remove"] = {"removed": True}
        instance = {"sessions": []}
        with patch.object(self.control, "verify_instance", return_value=instance):
            models = self.control.installed()["models"]
            for model, owner, job in zip(models, ("artifacts", "artifacts", "setup", "managed"),
                                         ("mlx-job", "image-job", "chat-job", "managed-job")):
                with self.subTest(owner=owner, job=job):
                    self.assertEqual(self.control.delete({"id": model["delete_id"]}), {"category": model["category"]})
                    if owner == "setup":
                        self.control.call.assert_called_with("models.remove", {"model_id": "chat"}, timeout=90)
                    else:
                        self.control.call.assert_called_with(
                            "downloads.remove", {"id": job, "owner": owner, "installed": True}, timeout=90,
                        )
            self.assertEqual([model["delete_id"] for model in models[-3:]], [None, None, None])
            self.assertFalse(models[3]["loadable"])
            with self.assertRaises(ControlError):
                self.control.load(models[3])

    def test_deletion_rechecks_loaded_state_and_serializes_mutations(self):
        self.replies["models.installed"] = [{"id": "chat", "provider": "llamacpp"}]
        self.replies["setup.status"]["setups"] = [
            {"setup_id": "chat-job", "stage": "ready", "plan": {"model_id": "chat"}},
        ]
        model = self.control.installed()["models"][0]
        request = {"id": model["delete_id"]}
        with self.control.model_mutation, self.assertRaises(ControlError) as busy:
            self.control.delete(request)
        self.assertEqual(busy.exception.code, "model_control_busy")
        for instance, expected in (
            ({"sessions": [{"provider": "strata", "model_id": "chat"}]}, "model_control_still_loaded"),
            ({"sessions": [{"model_id": "chat"}]}, "model_control_state_unknown"),
        ):
            with patch.object(self.control, "verify_instance", return_value=instance):
                with self.assertRaises(ControlError) as error:
                    self.control.delete(request)
                self.assertEqual(error.exception.code, expected)
        self.replies["setup.status"]["setups"].clear()
        with self.assertRaises(ControlError) as stale:
            self.control.delete(request)
        self.assertEqual(stale.exception.code, "model_control_stale")
        self.assertFalse(any(call.args[0] in {"downloads.remove", "models.remove"}
                             for call in self.control.call.call_args_list))

    def test_delete_blocks_active_install_and_invalidates_accepted_review(self):
        self.replies.update({
            "models.installed": [{"id": "chat", "provider": "llamacpp"}],
            "models.plan": {"digest": "a" * 64, "model_id": "chat"},
            "models.install": {},
            "models.remove": {"removed": True},
        })
        setups = self.replies["setup.status"]["setups"]
        setups.append({"setup_id": "ready-job", "stage": "ready", "plan": {"model_id": "chat"}})
        chosen = {"id": self.control.remember("gguf", {"repo": "owner/chat", "file": "chat.gguf"})}
        review = self.control.review(chosen)
        self.control.download(review)
        request_id = self.control.reviews[review["id"]]["request_id"]
        target = {"id": self.control.installed()["models"][0]["delete_id"]}
        setups.append({"setup_id": "new-job", "stage": "downloading", "plan": {"model_id": "chat"}})
        with self.assertRaises(ControlError) as stale:
            self.control.delete(target)
        self.assertEqual(stale.exception.code, "model_control_stale")
        setups.pop()
        with patch.object(self.control, "verify_instance", return_value={"sessions": []}):
            self.control.delete(target)
        review = self.control.review(chosen)
        self.control.download(review)
        self.assertNotEqual(self.control.call.call_args.args[1]["request_id"], request_id)
