"""Prepare an atomic Cloud Run deployment, migrating only allowlisted configuration."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys


def gcloud(*args):
    result = subprocess.run(
        ["gcloud", *args, "--quiet"], capture_output=True, text=True, check=True
    )
    return result.stdout


def prepare(service, image, config_names, read_secret):
    # Export only writable metadata; keep the service's runtime configuration.
    manifest = {
        "apiVersion": "serving.knative.dev/v1",
        "kind": "Service",
        "metadata": {
            key: copy.deepcopy(service["metadata"][key])
            for key in ("name", "namespace", "labels", "annotations")
            if key in service["metadata"]
        },
        "spec": copy.deepcopy(service["spec"]),
    }
    annotations = manifest["metadata"].get("annotations", {})
    for key in (
        "serving.knative.dev/creator", "serving.knative.dev/lastModifier",
        "run.googleapis.com/operation-id", "run.googleapis.com/ingress-status",
        "run.googleapis.com/urls", "run.googleapis.com/client-name",
        "run.googleapis.com/client-version",
    ):
        annotations.pop(key, None)
    template = manifest["spec"]["template"]
    template.setdefault("metadata", {}).pop("name", None)
    containers = template["spec"]["containers"]
    if len(containers) != 1:
        raise ValueError("Expected one container; refusing to change another container")
    container = containers[0]
    env = {entry["name"]: entry for entry in container.get("env", [])}
    for name, expected_secret in config_names.items():
        entry = env.get(name)
        if not entry:
            raise ValueError(f"Missing existing configuration: {name}")
        if "value" in entry:
            continue  # Subsequent deployments no longer read Secret Manager.
        ref = entry.get("valueFrom", {}).get("secretKeyRef", {})
        if ref.get("name") != expected_secret:
            raise ValueError(f"Unexpected secret reference for {name}; refusing conversion")
        value = read_secret(expected_secret, ref["key"])
        if not value:
            raise ValueError(f"Empty configuration: {name}")
        env[name] = {"name": name, "value": value}
    container["env"] = list(env.values())
    container["image"] = image
    # The new revision must receive traffic before the old secrets can be retired.
    manifest["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]
    return manifest


def main():
    service_name, region, image = (os.environ[k] for k in ("SERVICE_NAME", "GCP_REGION", "IMAGE"))
    config_names = json.loads(os.environ["NON_SECRET_CONFIG"])
    service = json.loads(gcloud("run", "services", "describe", service_name,
                                "--region", region, "--format=json"))
    project = service["metadata"]["namespace"]

    def read_secret(name, version):
        return gcloud("secrets", "versions", "access", version,
                      "--secret", name, "--project", project)

    manifest = prepare(service, image, config_names, read_secret)
    path = Path(os.environ["RUNNER_TEMP"]) / "cloud-run-service.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    path.chmod(0o600)
    try:
        # Replacing the manifest switches secretKeyRef to value in one revision,
        # avoiding an intermediate deployment with missing configuration.
        gcloud("run", "services", "replace", str(path), "--region", region)
        deployed = json.loads(gcloud("run", "services", "describe", service_name,
                                    "--region", region, "--format=json"))
        status = deployed["status"]
        ready = status.get("latestReadyRevisionName")
        if not any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in status.get("conditions", [])):
            raise ValueError("The service is not ready")
        if not ready:
            raise ValueError("A ready revision was not confirmed")
        if ready != status.get("latestCreatedRevisionName"):
            raise ValueError("The latest revision is not ready")
        if not any(t.get("revisionName") == ready and t.get("percent") == 100
                   for t in status.get("traffic", [])):
            raise ValueError("The new revision is not serving all traffic")
        current = {e["name"]: e for e in deployed["spec"]["template"]["spec"]["containers"][0]["env"]}
        expected = {e["name"]: e for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
        if current != expected:
            raise ValueError("Deployed configuration differs from the prepared values")
        print(f"Previous ready revision: {service.get('status', {}).get('latestReadyRevisionName')}")
        print(f"Verified ready revision {ready}: configuration uses ordinary environment variables.")
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Never print captured command output, which may contain configuration.
        sys.exit(f"gcloud failed (exit {error.returncode}); no secret values were logged.")
