"""k3s deployment artifacts (ADR-0005 D4).

Two layers, no fakes:
- Static: the manifest in deploy/k3s/ is parsed and asserted field-by-field —
  probes, resources, env wiring, volumes, NodePort. Runs everywhere (CI).
- Live: when a Kubernetes control plane is reachable (KUBECONFIG pointing at
  e.g. `deploy/k3s/dev-server.sh`), the manifest is applied for real and the
  controller chain is verified: Deployment → ReplicaSet → Pod creation,
  PVC binding, Service allocation. Pods stop at ContainerCreating on a
  kubelet-less dev control plane — that is the documented, honest limit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from whirlwind.config import load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy" / "k3s"
MANIFEST = DEPLOY_DIR / "manifest.yaml"


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def docs() -> dict[str, dict]:
    text = MANIFEST.read_text()
    parsed = [d for d in yaml.safe_load_all(text) if d]
    return {d["kind"]: d for d in parsed}


def _container(docs: dict[str, dict]) -> dict:
    return docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _kubectl_json(args: list[str]) -> dict:
    result = _run(["kubectl", *args])
    return json.loads(result.stdout)


def _kubectl_ok() -> bool:
    if shutil.which("kubectl") is None:
        return False
    import os

    if not os.environ.get("KUBECONFIG"):
        return False
    try:
        _run(["kubectl", "get", "--raw=/readyz"], check=True)
        return True
    except subprocess.CalledProcessError:
        return False


# ------------------------------------------------------- static: the manifest


def test_manifest_contains_expected_kinds(docs: dict[str, dict]) -> None:
    assert set(docs) == {"Namespace", "ConfigMap", "Secret", "PersistentVolumeClaim", "Deployment", "Service"}


def test_namespace_and_config(docs: dict[str, dict]) -> None:
    assert docs["Namespace"]["metadata"]["name"] == "whirlwind"
    # Operator config ships as a mounted whirlwind.toml (ADR-0009 D1), not key/value env plumbing
    config = docs["ConfigMap"]["data"]["whirlwind.toml"]
    assert "[sandbox]" in config and "max_live_sessions = 64" in config  # D2 gate is wired in
    assert 'llm_upstream = "https://api.deepseek.com"' in config
    secret = docs["Secret"]["stringData"]
    assert "DEEPSEEK_API_KEY" in secret


def test_pvc_is_rwo_local_path(docs: dict[str, dict]) -> None:
    pvc = docs["PersistentVolumeClaim"]["spec"]
    assert pvc["accessModes"] == ["ReadWriteOnce"]
    assert pvc["storageClassName"] == "local-path"
    assert pvc["resources"]["requests"]["storage"] == "2Gi"


def test_deployment_shape(docs: dict[str, dict]) -> None:
    deploy = docs["Deployment"]["spec"]
    assert deploy["replicas"] == 1  # single replica: in-process bus + memory backends
    assert deploy["strategy"]["type"] == "Recreate"  # RWO PVC + 1 replica: rolling would deadlock
    assert deploy["selector"]["matchLabels"] == deploy["template"]["metadata"]["labels"]


def test_container_args_and_env(docs: dict[str, dict]) -> None:
    container = _container(docs)
    assert container["command"] == ["whirlwind"]
    assert container["args"] == ["serve", "--config", "/etc/whirlwind/whirlwind.toml"]

    # The only env vars are credentials — values never enter config files (ADR-0009 D5);
    # WHIRLWIND_SECRET_KEY seals agent-defined env secrets at rest (ADR-0010 D3)
    env = {e["name"]: e for e in container["env"]}
    assert set(env) == {"DEEPSEEK_API_KEY", "WHIRLWIND_SECRET_KEY"}
    assert env["DEEPSEEK_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "whirlwind-secrets"
    assert env["WHIRLWIND_SECRET_KEY"]["valueFrom"]["secretKeyRef"]["name"] == "whirlwind-secrets"

    volumes = {v["name"]: v for v in docs["Deployment"]["spec"]["template"]["spec"]["volumes"]}
    assert volumes["config"]["configMap"]["name"] == "whirlwind-config"
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["config"]["mountPath"] == "/etc/whirlwind"
    assert mounts["config"]["readOnly"] is True


def test_probes_and_resources(docs: dict[str, dict]) -> None:
    container = _container(docs)
    for probe in ("readinessProbe", "livenessProbe"):
        http = container[probe]["httpGet"]
        assert http["path"] == "/healthz" and http["port"] == "http"
    assert container["livenessProbe"]["failureThreshold"] >= container["readinessProbe"]["failureThreshold"]

    resources = container["resources"]
    assert resources["requests"]["memory"] == "256Mi"
    assert resources["limits"]["memory"] == "1Gi"  # pod-level ceiling (D1 is per-sandbox, separate)
    assert "cpu" in resources["requests"] and "cpu" in resources["limits"]


def test_volume_wiring(docs: dict[str, dict]) -> None:
    pod_spec = docs["Deployment"]["spec"]["template"]["spec"]
    mount = pod_spec["containers"][0]["volumeMounts"][0]
    assert mount["name"] == "data" and mount["mountPath"] == "/data"
    volume = pod_spec["volumes"][0]
    assert volume["name"] == "data"
    assert volume["persistentVolumeClaim"]["claimName"] == "whirlwind-data"


def test_container_needs_no_privileges(docs: dict[str, dict]) -> None:
    security = _container(docs)["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["capabilities"]["drop"] == ["ALL"]
    assert not security.get("privileged", False)


def test_service_is_nodeport_30841(docs: dict[str, dict]) -> None:
    service = docs["Service"]["spec"]
    assert service["type"] == "NodePort"
    port = service["ports"][0]
    assert port["port"] == 8410 and port["targetPort"] == "http" and port["nodePort"] == 30841
    assert service["selector"] == {"app": "whirlwind"}


def test_dockerfile_keeps_repo_root_for_image_builds() -> None:
    """The echo image build pip-installs `whirlwind @ file://{repo_root}` into
    every sandbox — the repo must exist on disk inside the image."""
    dockerfile = (DEPLOY_DIR / "Dockerfile").read_text()
    assert "COPY src ./src" in dockerfile
    assert "pip install" in dockerfile


def test_manifest_toml_passes_the_real_loader(docs: dict[str, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dogfood ADR-0009: the ConfigMap's embedded whirlwind.toml must load
    cleanly through load_settings (schema + types), so a manifest edit with a
    typo fails in CI, not at pod boot."""
    for name, value in os.environ.items():
        if name.startswith("WHIRLWIND_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "whirlwind.toml"
    cfg.write_text(docs["ConfigMap"]["data"]["whirlwind.toml"])
    settings = load_settings(config_path=cfg)
    assert settings.server.host == "0.0.0.0" and settings.server.port == 8410
    assert settings.runtime.data_dir == Path("/data")
    assert settings.runtime.max_live_sessions == 64


# --------------------------------------------------------- live: apply to k3s


@pytest.mark.skipif(not _kubectl_ok(), reason="no reachable Kubernetes control plane (start deploy/k3s/dev-server.sh)")
class TestLiveApply:
    """Applies the real manifest to the real control plane and verifies the
    controller chain. On a kubelet-less dev control plane (dev-server.sh) pods
    schedule but never leave ContainerCreating — expected, documented."""

    NS = "whirlwind"

    @pytest.fixture(autouse=True, scope="class")
    def apply_manifest(self) -> None:
        import time

        # Without a kubelet, pods stick in Terminating forever (graceful
        # deletion waits for kubelet ack) and block namespace deletion —
        # force-delete them first. Documented in ADR-0005 D4.
        _run(["kubectl", "-n", self.NS, "delete", "pods", "--all", "--force", "--grace-period=0"],
             check=False)
        _run(["kubectl", "delete", "namespace", self.NS, "--ignore-not-found", "--wait=true", "--timeout=60s"],
             check=False)
        # The static dev PV (dev-server.sh) is Retain + claimRef-pinned to the
        # deleted PVC: release it so the fresh PVC can bind the same volume.
        _run(["kubectl", "patch", "pv", "whirlwind-data-pv", "--type", "json",
              "-p", '[{"op": "remove", "path": "/spec/claimRef"}]'], check=False)
        _run(["kubectl", "apply", "-f", str(MANIFEST)])
        # give controllers a beat
        time.sleep(5)

    def test_deployment_reconciles_to_replicaset_and_pod(self) -> None:
        deploy = _kubectl_json(["-n", self.NS, "get", "deployment", "whirlwind", "-o", "json"])
        assert deploy["spec"]["replicas"] == 1
        assert deploy["status"]["observedGeneration"] == deploy["metadata"]["generation"]

        rs = _kubectl_json(["-n", self.NS, "get", "rs", "-o", "json"])["items"]
        assert len(rs) == 1 and rs[0]["spec"]["replicas"] == 1

        pods = _kubectl_json(["-n", self.NS, "get", "pods", "-o", "json"])["items"]
        assert len(pods) == 1
        pod = pods[0]
        assert pod["status"]["phase"] in ("Pending", "Running")  # Pending until a real node joins
        assert pod["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"] == "whirlwind-data"
        assert pod["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] == "/data"

    def test_pvc_binds_when_a_volume_exists(self) -> None:
        pvc = _kubectl_json(["-n", self.NS, "get", "pvc", "whirlwind-data", "-o", "json"])
        pv_names = _kubectl_json(["get", "pv", "-o", "json"])["items"]
        has_matching = any(pv["metadata"]["name"] == "whirlwind-data-pv" for pv in pv_names)
        if has_matching:  # dev-server.sh --fake-node registered a pre-bound PV
            assert pvc["status"]["phase"] == "Bound"
            assert pvc["spec"]["volumeName"] == "whirlwind-data-pv"
        else:  # stock control plane without the local-path provisioner
            assert pvc["status"]["phase"] in ("Pending", "Bound")

    def test_service_allocates_nodeport(self) -> None:
        svc = _kubectl_json(["-n", self.NS, "get", "svc", "whirlwind", "-o", "json"])
        assert svc["spec"]["type"] == "NodePort"
        assert svc["spec"]["ports"][0]["nodePort"] == 30841

    def test_pod_scheduled_when_node_registered(self) -> None:
        nodes = _kubectl_json(["get", "nodes", "-o", "json"])["items"]
        if not nodes:
            pytest.skip("no node registered (control-plane-only validation)")
        pods = _kubectl_json(["-n", self.NS, "get", "pods", "-o", "json"])["items"]
        assert pods[0]["spec"]["nodeName"] in {n["metadata"]["name"] for n in nodes}
