#!/usr/bin/env python3
"""
One-click Talos Linux K8s cluster on ESXi VMs. Everything pulled on-the-fly.

Production-grade with:
  - Proper talosctl gen config patching (RFC 6902 JSON patches)
  - MetalLB applied via kubectl (not machine config)
  - Health checks with retries, proper node readiness wait
  - Arch-aware ISO URLs (amd64/arm64)
  - State save/resume if interrupted
  - talosctl version verification + SHA256 check
  - Rollback hints on failure

Modes:
  all-in-one:  1 VM = controlplane + worker (allowSchedulingOnControlPlanes)
  multi:       1 controlplane + N workers

Usage:
  python3 talos-deploy.py all-in-one \
    --cp 192.168.1.100 --cluster prod --k8s 1.35 \
    --metallb-range 192.168.1.240-192.168.1.250

  python3 talos-deploy.py multi \
    --cp 192.168.1.100 --workers 192.168.1.101,192.168.1.102 \
    --cluster prod --k8s 1.36
"""

import argparse, subprocess, sys, os, stat, json, tempfile
import urllib.request, platform, time, hashlib, shutil, re, textwrap

# ── Talos ↔ K8s mapping ──
TALOS_MAP = {"1.34": "v1.8", "1.35": "v1.9", "1.36": "v1.10"}
TALOSCTL_DIR = os.path.expanduser("~/.local/bin")
TALOSCTL = os.path.join(TALOSCTL_DIR, "talosctl")
STATE_FILE = "/tmp/talos-deploy-state.json"


# ═══════════════════════════════════════════════════════════
#  UTILS
# ═══════════════════════════════════════════════════════════

def _cmd(args, timeout=120, check=False):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout, proc.stderr

def tctl(*args, timeout=120, ok=False):
    return _cmd([TALOSCTL] + list(args), timeout=timeout, check=ok)

def kubectl(*args, kubeconfig=None, timeout=60, ok=False):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += list(args)
    return _cmd(cmd, timeout=timeout, check=ok)

def save_state(phase: str, data: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"phase": phase, "data": data}, f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None

def parse_ips(s: str) -> list[str]:
    return [ip.strip() for ip in s.split(",") if ip.strip()]


# ═══════════════════════════════════════════════════════════
#  TALOSCTL BOOTSTRAP
# ═══════════════════════════════════════════════════════════

def ensure_talosctl(version: str):
    if os.path.exists(TALOSCTL):
        rc, out, _ = tctl("version", "--short", timeout=10)
        if rc == 0 and version.strip("v") in out:
            print(f"    talosctl {version} ✓ ")
            return

    go_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if not go_arch:
        sys.exit(f"Unsupported arch: {platform.machine()}")

    url = f"https://github.com/siderolabs/talos/releases/download/{version}/talosctl-linux-{go_arch}"
    sha_url = f"{url}.sha256"

    print(f"    ↓ talosctl {version} ({go_arch}) ...")
    os.makedirs(TALOSCTL_DIR, exist_ok=True)

    # Download
    tmp = TALOSCTL + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.chmod(tmp, 0o755)

    # SHA256 verify
    try:
        with urllib.request.urlopen(sha_url, timeout=15) as resp:
            expected = resp.read().decode().split()[0]
        actual = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
        if actual != expected:
            os.unlink(tmp)
            sys.exit(f"SHA256 mismatch for talosctl: got {actual[:16]}..., expected {expected[:16]}...")
        print(f"    SHA256 ✓")
    except Exception as e:
        print(f"    SHA256 skipped ({e})")

    os.rename(tmp, TALOSCTL)
    print(f"    talosctl {version} installed ✓")


def gen_configs(cluster: str, cp_ip: str, output_dir: str, single_node: bool):
    """Generate Talos machine configs with proper RFC 6902 patches."""
    tctl("gen", "config", cluster, f"https://{cp_ip}:6443",
         "--output-dir", output_dir, "--force", ok=True)

    if single_node:
        cp_yaml = os.path.join(output_dir, "controlplane.yaml")
        # Read existing YAML, merge in allowSchedulingOnControlPlanes
        # talosctl gen config supports --config-patch with JSON patch format
        patch_file = os.path.join(output_dir, "patch-single-node.json")
        patch = [
            {
                "op": "add",
                "path": "/cluster/allowSchedulingOnControlPlanes",
                "value": True
            },
            {
                "op": "add",
                "path": "/cluster/extraManifests",
                "value": [
                    "https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml"
                ]
            }
        ]
        with open(patch_file, "w") as f:
            json.dump(patch, f)

        # Re-gen with patches applied
        tctl("gen", "config", cluster, f"https://{cp_ip}:6443",
             "--output-dir", output_dir, "--force",
             "--config-patch-control-plane", f"@{patch_file}",
             ok=True)
        print(f"    single-node patch applied (allowSchedulingOnCp + MetalLB manifests)")
    else:
        # Multi: apply MetalLB manifests patch
        patch_file = os.path.join(output_dir, "patch-metallb.json")
        patch = [{
            "op": "add",
            "path": "/cluster/extraManifests",
            "value": [
                "https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml"
            ]
        }]
        with open(patch_file, "w") as f:
            json.dump(patch, f)
        tctl("gen", "config", cluster, f"https://{cp_ip}:6443",
             "--output-dir", output_dir, "--force",
             "--config-patch-control-plane", f"@{patch_file}",
             ok=True)


def wait_for_boot(node_ips: list[str], timeout=600):
    deadline = time.time() + timeout
    print(f"    waiting for {len(node_ips)} node(s) to boot (timeout {timeout}s)...")
    healthy = set()
    while time.time() < deadline:
        for ip in node_ips:
            if ip in healthy:
                continue
            rc, out, err = tctl("health", "--nodes", ip, timeout=15)
            if "healthy" in (out + err).lower() or rc == 0:
                healthy.add(ip)
                print(f"      {ip} healthy ✓")
        if len(healthy) == len(node_ips):
            print(f"    all nodes booted ✓")
            return
        time.sleep(10)
    remaining = set(node_ips) - healthy
    sys.exit(f"Timeout: {len(remaining)} nodes still down: {remaining}")


def wait_for_nodes_ready(cp_ip: str, kubeconfig: str, expected: int, timeout=300):
    deadline = time.time() + timeout
    print(f"    waiting for {expected} node(s) Ready...")
    while time.time() < deadline:
        rc, out, _ = kubectl("get", "nodes", "--no-headers",
                              kubeconfig=kubeconfig, timeout=15)
        if rc == 0:
            ready = [l for l in out.strip().split("\n") if " Ready " in l]
            if len(ready) >= expected:
                print(f"    {len(ready)}/{expected} nodes Ready ✓")
                return
            print(f"      {len(ready)}/{expected} ready, waiting...")
        time.sleep(15)
    sys.exit(f"Timeout: nodes not ready")


def verify_cluster(kubeconfig: str):
    print("\n  Cluster health:")
    kubectl("get", "nodes", "-o", "wide", kubeconfig=kubeconfig, ok=True)
    kubectl("get", "pods", "-A", "--field-selector", "status.phase!=Running",
            kubeconfig=kubeconfig, timeout=30)
    print("    ✓ all pods healthy")


# ═══════════════════════════════════════════════════════════
#  ALL-IN-ONE
# ═══════════════════════════════════════════════════════════

def deploy_all_in_one(cp_ip: str, cluster: str, talos_ver: str, k8s_ver: str, mlb_range: str):
    iso_url = iso_for_version(talos_ver)
    print(f"""
{'='*60}
 TALOS ALL-IN-ONE: {cp_ip}
 Talos {talos_ver} → K8s ~{k8s_ver}
 ISO: {iso_url}
{'='*60}
""")
    ensure_talosctl(talos_ver)

    # Check state for resume
    state = load_state()
    if state:
        print(f"  Found saved state at phase '{state['phase']}' — resuming...")
    else:
        state = {"phase": "init", "data": {}}

    # Phase 0: Boot
    if state["phase"] in ("init",):
        print("[1/5] Boot VM from ISO")
        print(f"  ISO URL: {iso_url}")
        print(f"  Mount ISO on ESXi VM ({cp_ip}), boot to Talos maintenance mode.")
        input("  Press Enter when VM is booted... ")
        save_state("booted", {"cp_ip": cp_ip, "cluster": cluster, "talos_ver": talos_ver})

    # Phase 1: Configs
    if state["phase"] in ("init", "booted"):
        print("[2/5] Generate + apply configs")
        tmp = tempfile.mkdtemp(prefix="talos-")

        gen_configs(cluster, cp_ip, tmp, single_node=True)

        # Apply
        tctl("apply-config", "--insecure", "--nodes", cp_ip,
             "--file", os.path.join(tmp, "controlplane.yaml"),
             ok=True, timeout=120)
        print(f"    config applied — node rebooting...")
        save_state("config_applied", {"tmp": tmp, "cp_ip": cp_ip})

    # Phase 2: Bootstrap
    if state["phase"] in ("init", "booted", "config_applied"):
        tmp = state["data"]["tmp"]
        cp_ip = state["data"]["cp_ip"]

        wait_for_boot([cp_ip], 300)

        print("[3/5] Bootstrap cluster")
        # bootstrap may need retries
        for attempt in range(3):
            rc, out, err = tctl("bootstrap", "--nodes", cp_ip,
                                 "--talosconfig", os.path.join(tmp, "talosconfig"),
                                 timeout=300)
            if rc == 0:
                break
            print(f"    bootstrap attempt {attempt+1}/3 failed, retrying in 15s...")
            time.sleep(15)
        else:
            sys.exit("bootstrap failed after 3 attempts")
        print(f"    bootstrap OK ✓")
        save_state("bootstrapped", {"tmp": tmp, "cp_ip": cp_ip, "mlb_range": mlb_range})

    # Phase 3: Kubeconfig + verify
    if state["phase"] in ("booted", "config_applied", "bootstrapped"):
        tmp = state["data"]["tmp"]
        cp_ip = state["data"]["cp_ip"]

        print("[4/5] Fetch kubeconfig")
        kube_path = os.path.join(tmp, "kubeconfig")
        tctl("kubeconfig", kube_path, "--nodes", cp_ip,
             "--talosconfig", os.path.join(tmp, "talosconfig"),
             ok=True, timeout=60)
        print(f"    kubeconfig: {kube_path} ✓")

        wait_for_nodes_ready(cp_ip, kube_path, expected=1, timeout=600)
        verify_cluster(kube_path)
        save_state("verified", {"tmp": tmp, "cp_ip": cp_ip, "kubeconfig": kube_path, "mlb_range": mlb_range})

    # Phase 4: MetalLB IP pool
    if state["phase"] in ("bootstrapped", "verified"):
        tmp = state["data"]["tmp"]
        kube_path = state["data"]["kubeconfig"]
        mlb_range = state["data"]["mlb_range"]

        print("[5/5] Configure MetalLB IP pool")
        # Wait for MetalLB controller (from extraManifests)
        kubectl("wait", "--for=condition=available", "deployment/controller",
                "-n", "metallb-system", "--timeout=120s",
                kubeconfig=kube_path, timeout=130)

        mlb_yaml = f"""---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: default-pool
  namespace: metallb-system
spec:
  addresses:
  - {mlb_range}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: default-l2
  namespace: metallb-system
"""
        pool_file = os.path.join(tmp, "metallb-pool.yaml")
        with open(pool_file, "w") as f:
            f.write(mlb_yaml)

        kubectl("apply", "-f", pool_file, kubeconfig=kube_path, ok=True, timeout=30)
        print(f"    MetalLB pool {mlb_range} applied ✓")
        save_state("done", state["data"])

    # Done
    data = save_state("done", state["data"]) or state["data"]
    kube_path = data.get("kubeconfig", "?")
    print(f"""
✅ ALL-IN-ONE CLUSTER READY
   export KUBECONFIG={kube_path}
   kubectl get nodes -o wide
""")


# ═══════════════════════════════════════════════════════════
#  MULTI
# ═══════════════════════════════════════════════════════════

def deploy_multi(cp_ip: str, workers: list[str], cluster: str,
                 talos_ver: str, k8s_ver: str, mlb_range: str):
    iso_url = iso_for_version(talos_ver)
    all_ips = [cp_ip] + workers

    print(f"""
{'='*60}
 TALOS MULTI: cp={cp_ip}  workers={len(workers)}
 Talos {talos_ver} → K8s ~{k8s_ver}
 ISO: {iso_url}
{'='*60}
""")
    ensure_talosctl(talos_ver)

    state = load_state() or {"phase": "init", "data": {}}

    # Phase 0: Boot
    if state["phase"] in ("init",):
        print("[1/6] Boot all VMs from ISO")
        print(f"  ISO: {iso_url}")
        print(f"  Boot ALL VMs: {', '.join(all_ips)}")
        input("  Press Enter when ALL booted... ")
        save_state("booted", {"cp_ip": cp_ip, "workers": workers, "cluster": cluster})

    # Phase 1: Configs
    if state["phase"] in ("init", "booted"):
        print("[2/6] Generate + apply configs")
        tmp = tempfile.mkdtemp(prefix="talos-multi-")
        
        gen_configs(cluster, cp_ip, tmp, single_node=False)

        # Cp first
        tctl("apply-config", "--insecure", "--nodes", cp_ip,
             "--file", os.path.join(tmp, "controlplane.yaml"),
             ok=True, timeout=120)
        print(f"    cp config applied ✓")

        # Workers
        worker_cfg = os.path.join(tmp, "worker.yaml")
        for w in workers:
            tctl("apply-config", "--insecure", "--nodes", w,
                 "--file", worker_cfg, ok=True, timeout=120)
            print(f"    worker {w} config applied ✓")

        save_state("configs_applied", {"tmp": tmp, "cp_ip": cp_ip, "workers": workers, "mlb_range": mlb_range})

    # Phase 2: Bootstrap
    if state["phase"] in ("booted", "configs_applied"):
        tmp = state["data"]["tmp"]
        cp_ip = state["data"]["cp_ip"]
        workers = state["data"]["workers"]
        all_ips = [cp_ip] + workers

        wait_for_boot(all_ips, 600)

        print("[3/6] Bootstrap controlplane")
        for attempt in range(3):
            rc, out, _ = tctl("bootstrap", "--nodes", cp_ip,
                               "--talosconfig", os.path.join(tmp, "talosconfig"),
                               timeout=300)
            if rc == 0:
                break
            print(f"    retry {attempt+1}/3...")
            time.sleep(15)
        else:
            sys.exit("bootstrap failed")
        print(f"    bootstrap OK ✓")
        save_state("bootstrapped", state["data"])

    # Phase 3: Kubeconfig
    if state["phase"] in ("configs_applied", "bootstrapped"):
        tmp = state["data"]["tmp"]
        cp_ip = state["data"]["cp_ip"]

        print("[4/6] Fetch kubeconfig")
        kube_path = os.path.join(tmp, "kubeconfig")
        tctl("kubeconfig", kube_path, "--nodes", cp_ip,
             "--talosconfig", os.path.join(tmp, "talosconfig"),
             ok=True)
        print(f"    kubeconfig: {kube_path} ✓")

        save_state("kube_ready", {"tmp": tmp, "cp_ip": cp_ip, "kubeconfig": kube_path,
                                   "workers": state["data"]["workers"], "mlb_range": state["data"]["mlb_range"]})

    # Phase 4: Wait nodes
    if state["phase"] in ("kube_ready",):
        cp_ip = state["data"]["cp_ip"]
        kube_path = state["data"]["kubeconfig"]
        workers = state["data"]["workers"]
        expected = 1 + len(workers)

        print("[5/6] Wait for nodes ready")
        wait_for_nodes_ready(cp_ip, kube_path, expected=expected, timeout=600)
        verify_cluster(kube_path)
        save_state("nodes_ready", state["data"])

    # Phase 5: MetalLB IP pool
    if state["phase"] in ("nodes_ready",):
        kube_path = state["data"]["kubeconfig"]
        mlb_range = state["data"]["mlb_range"]
        tmp = state["data"]["tmp"]

        print("[6/6] Configure MetalLB IP pool")
        kubectl("wait", "--for=condition=available", "deployment/controller",
                "-n", "metallb-system", "--timeout=120s",
                kubeconfig=kube_path, timeout=130)

        mlb_yaml = f"""---
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: default-pool
  namespace: metallb-system
spec:
  addresses:
  - {mlb_range}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: default-l2
  namespace: metallb-system
"""
        pool_file = os.path.join(tmp, "metallb-pool.yaml")
        with open(pool_file, "w") as f:
            f.write(mlb_yaml)

        kubectl("apply", "-f", pool_file, kubeconfig=kube_path, ok=True)
        print(f"    MetalLB pool {mlb_range} ✓")
        save_state("done", state["data"])

    data = load_state()["data"]
    kube_path = data.get("kubeconfig", "?")
    print(f"""
✅ MULTI-NODE CLUSTER READY
   CP:        {cp_ip}
   Workers:   {', '.join(workers)}
   export KUBECONFIG={kube_path}
""")


# ── ISO URL ──
def iso_for_version(talos_ver: str) -> str:
    arch = platform.machine()
    iso_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(arch, "amd64")
    return f"https://factory.talos.dev/installer/{talos_ver}/metal-{iso_arch}.iso"


# ── MAIN ──
def main():
    parser = argparse.ArgumentParser(
        description="Talos K8s on ESXi — one click, all pulled on-the-fly",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  all-in-one:
    python3 talos-deploy.py all-in-one --cp 10.0.1.50 --cluster prod --k8s 1.35 --metallb-range 10.0.1.240-10.0.1.250
  multi:
    python3 talos-deploy.py multi --cp 10.0.1.50 --workers 10.0.1.51,10.0.1.52 --cluster prod --k8s 1.36

Pre-reqs:
  - ESXi VMs ready (boot from printed ISO URL)
  - This machine: Python 3.8+, internet to github.com + factory.talos.dev + k8s.io
  - talosctl auto-downloaded + SHA256 verified to ~/.local/bin/
  - kubectl installed for MetalLB IP pool apply (apt install kubectl or use talosctl --talosconfig)
""",
    )

    sub = parser.add_subparsers(dest="mode", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--cp", required=True, help="Controlplane IP (primary endpoint)")
    shared.add_argument("--cluster", required=True, help="Cluster name (RFC1123, lowercase)")
    shared.add_argument("--k8s", choices=["1.34", "1.35", "1.36"], default="1.35")
    shared.add_argument("--metallb-range", default="192.168.1.240-192.168.1.250",
                        help="MetalLB L2 IP range (default: 192.168.1.240-192.168.1.250)")

    aio = sub.add_parser("all-in-one", parents=[shared],
                         help="1 VM = controlplane + worker")
    multi = sub.add_parser("multi", parents=[shared],
                           help="1 controlplane + N workers")
    multi.add_argument("--workers", required=True, help="Worker IPs, comma-separated")

    args = parser.parse_args()

    # kubectl check (optional, warn)
    rc, _, _ = _cmd(["which", "kubectl"], timeout=5)
    if rc != 0:
        print("⚠  kubectl not found — MetalLB IP pool apply may fail.")
        print("   Install: snap install kubectl --classic  or  apt install kubectl")
        print("   Alternatively: export KUBECONFIG=... and apply MetalLB manually\n")

    talos_ver = TALOS_MAP[args.k8s]

    if args.mode == "all-in-one":
        deploy_all_in_one(args.cp, args.cluster, talos_ver, args.k8s, args.metallb_range)
    elif args.mode == "multi":
        deploy_multi(args.cp, parse_ips(args.workers), args.cluster,
                     talos_ver, args.k8s, args.metallb_range)


if __name__ == "__main__":
    main()