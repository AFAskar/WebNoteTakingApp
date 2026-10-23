import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("prepare_deploy", Path(__file__).with_name("prepare-deploy.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.service = {
            "metadata": {"name": "app", "namespace": "123", "uid": "readonly",
                         "annotations": {"run.googleapis.com/ingress": "all",
                                         "run.googleapis.com/operation-id": "readonly"}},
            "spec": {"template": {"metadata": {"name": "app-old"}, "spec": {
                "serviceAccountName": "runtime@example.com",
                "containers": [{"image": "old", "env": [
                    {"name": "HOST", "valueFrom": {"secretKeyRef": {"name": "CONFIG_HOST", "key": "latest"}}},
                    {"name": "PASSWORD", "valueFrom": {"secretKeyRef": {"name": "PASSWORD", "key": "1"}}},
                    {"name": "MODE", "value": "production"}]}]}},
                "traffic": [{"revisionName": "app-old", "percent": 100}]},
            "status": {"latestReadyRevisionName": "app-old"},
        }

    def test_only_allowlisted_configuration_is_converted_and_runtime_is_preserved(self):
        original = copy.deepcopy(self.service)
        reader = Mock(return_value="host,with=special\ncharacters")
        result = module.prepare(self.service, "new-image", {"HOST": "CONFIG_HOST"}, reader)
        env = result["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertEqual(env[0], {"name": "HOST", "value": "host,with=special\ncharacters"})
        self.assertEqual(env[1:], original["spec"]["template"]["spec"]["containers"][0]["env"][1:])
        self.assertEqual(result["spec"]["template"]["spec"]["serviceAccountName"], "runtime@example.com")
        self.assertEqual(result["spec"]["template"]["spec"]["containers"][0]["image"], "new-image")
        self.assertNotIn("name", result["spec"]["template"]["metadata"])
        self.assertNotIn("uid", result["metadata"])
        self.assertEqual(result["metadata"]["annotations"], {"run.googleapis.com/ingress": "all"})
        self.assertEqual(result["spec"]["traffic"], [{"latestRevision": True, "percent": 100}])
        self.assertEqual(self.service, original)
        reader.assert_called_once_with("CONFIG_HOST", "latest")

    def test_redeployment_never_reads_the_deleted_secret(self):
        first = module.prepare(self.service, "image", {"HOST": "CONFIG_HOST"}, lambda *_: "db.example")
        reader = Mock(side_effect=AssertionError("Secret must not be accessed again"))
        second = module.prepare(first, "new-image", {"HOST": "CONFIG_HOST"}, reader)
        self.assertEqual(second["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"], "db.example")
        reader.assert_not_called()

    def test_unexpected_secret_is_not_exposed(self):
        with self.assertRaisesRegex(ValueError, "Unexpected secret"):
            module.prepare(self.service, "image", {"HOST": "OTHER"}, Mock())

    def test_missing_or_empty_config_stops_before_deployment(self):
        with self.assertRaisesRegex(ValueError, "Missing existing"):
            module.prepare(self.service, "image", {"MISSING": "MISSING"}, Mock())
        with self.assertRaisesRegex(ValueError, "Empty configuration"):
            module.prepare(self.service, "image", {"HOST": "CONFIG_HOST"}, lambda *_: "")

    def test_multiple_containers_are_rejected(self):
        self.service["spec"]["template"]["spec"]["containers"].append({"image": "sidecar"})
        with self.assertRaisesRegex(ValueError, "one container"):
            module.prepare(self.service, "image", {"HOST": "CONFIG_HOST"}, Mock())

    def test_deployment_is_verified_and_temporary_manifest_is_removed(self):
        deployed = module.prepare(self.service, "new-image", {"HOST": "CONFIG_HOST"}, lambda *_: "db.example")
        deployed["status"] = {
            "conditions": [{"type": "Ready", "status": "True"}],
            "latestReadyRevisionName": "app-new", "latestCreatedRevisionName": "app-new",
            "traffic": [{"revisionName": "app-new", "percent": 100}],
        }
        with tempfile.TemporaryDirectory() as directory:
            settings = {"SERVICE_NAME": "app", "GCP_REGION": "region", "IMAGE": "new-image",
                        "NON_SECRET_CONFIG": '{"HOST":"CONFIG_HOST"}', "RUNNER_TEMP": directory}
            with patch.dict(os.environ, settings), patch.object(module, "gcloud", side_effect=[
                json.dumps(self.service), "db.example", "", json.dumps(deployed)
            ]) as cli:
                module.main()
            self.assertFalse((Path(directory) / "cloud-run-service.json").exists())
            self.assertEqual(cli.call_args_list[2].args[:3], ("run", "services", "replace"))

    def test_secret_access_failure_prevents_any_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {"SERVICE_NAME": "app", "GCP_REGION": "region", "IMAGE": "new-image",
                        "NON_SECRET_CONFIG": '{"HOST":"CONFIG_HOST"}', "RUNNER_TEMP": directory}
            with patch.dict(os.environ, settings), patch.object(module, "gcloud", side_effect=[
                json.dumps(self.service), RuntimeError("Access denied")
            ]) as cli:
                with self.assertRaisesRegex(RuntimeError, "Access denied"):
                    module.main()
            self.assertEqual(cli.call_count, 2)
            self.assertFalse((Path(directory) / "cloud-run-service.json").exists())

    def test_failure_diagnostics_do_not_expose_command_arguments_or_output(self):
        error = subprocess.CalledProcessError(1, ["gcloud", "secrets", "versions", "access", "latest", "--secret", "private-name"],
                                               output="private-value", stderr="PERMISSION_DENIED private-value")
        message = module.failure_message(error)
        self.assertIn("secrets versions access", message)
        self.assertIn("PERMISSION_DENIED", message)
        self.assertNotIn("private", message)


if __name__ == "__main__":
    unittest.main()
