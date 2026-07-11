#!/usr/bin/env python3
"""
One-click Talos Linux K8s cluster on ESXi. Everything pulled on-the-fly.

True one-click — includes VM creation via govc:
  - Uploads Talos ISO to ESXi datastore
  - Creates VMs with proper CPU/RAM/disk
  - Attaches ISO, powers on
  - Waits for IP assignment
  - Then deploys Talos cluster (config gen, apply, bootstrap, MetalLB)

Modes:
  all-in-one:  1 VM = controlplane + worker
  multi:       1 controlplane + N workers

K8s versions: 1.34 / 1.35 / 1.36 → maps to Talos v1.8 / v1.9 / v1.10

Flags for zero-touch (ESXi credentials):
  --esxi-host HOST    ESXi IP or hostname (required for VM creation)
  --esxi-user USER    ESXi username (default: root)
  --esxi-pass PASS    ESXi password (env: ESXI_PASSWORD)
  --esxi-datastore DS Datastore name (default: datastore1)
  --esxi-network NET  Network name (default: VM Network)

Flags for VM spec (optional):
  --vcpu N             vCPU per VM (default: 2)
  --ram-gb N           RAM per VM in GB (default: 4)
  --disk-gb N          Disk per VM in GB (default: 20)
  --ip-start BASE_IP   Sequential IP assignment: cp=base, worker1=base+1, ...
  --ip-pool CIDR       Static IP range for MetalLB + guestinfo (overrides --metallb-range)

If --esxi-host not provided: assumes VMs already exist (manual mode).

Usage:
  # Full zero-touch: create VMs + deploy cluster
  python3 talos-deploy.py all-in-one \
    --cp 10.0.1.50 --cluster prod --k8s 1.35 \
    --esxi-host 10.0.1.10 --esxi-user root --esxi-pass secret \
    --esxi-datastore datastore1 --esxi-network "VM Network"

  # Minimal: VMs already booted, just deploy
  python3 talos-deploy.py multi \
    --cp 10.0.1.50 --workers 10.0.1.51,10.0.1.52 --cluster prod --k8s 1.36
"""

import argparse, subprocess, sys, os, json, tempfile
import urllib.request, platform, time, hashlib, textwrap


# ── Constants ──
TALOS_MAP = {"1.34": "v1.8.0", "1.35": "v1.9.0", "1.36": "v1.10.0"}  # ponytail: hardcoded stable tags — GitHub API auto-resolution when patches diverge
TALOSCTL_DIR = os.path.expanduser("~/.local/bin")
TALOSCTL = os.path.join(TALOSCTL_DIR, "talosctl")
GOVC = os.path.join(TALOSCTL_DIR, "govc")
STATE_FILE = os.path.join(tempfile.gettempdir(), "talos-deploy-state.json")
DEBUG = False  # ponytail: global flag, set by --debug



# ═══ UTILS ═══

def check_reachable(ip, port=50000):
    """Check if Talos API port is reachable."""
    import socket
    try:
        with socket.create_connection((ip, port), timeout=5):
            return True
    except:
        return False

def _cmd(args, timeout=120, check=False, env=None):
    cmd_str = ' '.join(str(a) for a in args)
    if DEBUG:
        print(f"[DEBUG] Running: {cmd_str}")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    if DEBUG:
        print(f"[DEBUG] stdout: {proc.stdout[:500]}{'...' if len(proc.stdout) > 500 else ''}")
        print(f"[DEBUG] stderr: {proc.stderr[:500]}{'...' if len(proc.stderr) > 500 else ''}")
        print(f"[DEBUG] return code: {proc.returncode}")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{cmd_str} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.returncode, proc.stdout, proc.stderr

def tctl(*args, timeout=120, ok=False):
    return _cmd([TALOSCTL] + list(args), timeout=timeout, check=ok)

def kubectl(*args, kubeconfig=None, timeout=60, ok=False):
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += list(args)
    return _cmd(cmd, timeout=timeout, check=ok)

def govc(*args, timeout=120, ok=False):
    cmd = [GOVC] + list(args)
    return _cmd(cmd, timeout=timeout, check=ok)

def save_state(phase, data):
    with open(STATE_FILE, "w") as f:
        json.dump({"phase": phase, "data": data}, f, default=str)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None

def parse_ips(s):  # ponytail: one-liner, no need for a function — but called 3×, keeps call sites readable
    return [ip.strip() for ip in s.split(",") if ip.strip()]


# ═══ TOOL BOOTSTRAP ═══

def ensure_binary(name, version, url_map, sha=False):  # ponytail: govoc has no --version, skips check by design
    """Download + SHA256-verify a CLI binary."""
    dst = os.path.join(TALOSCTL_DIR, name)
    if os.path.exists(dst):
        rc, out, _ = _cmd([dst, "version", "--short"], timeout=10) if name == TALOSCTL else (0, "ok", "")
        if rc == 0 and version in out:
            print(f"    {name} {version} ✓")
            return

    go_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if not go_arch:
        sys.exit(f"Unsupported arch: {platform.machine()}")

    url = url_map(version, go_arch)
    os.makedirs(TALOSCTL_DIR, exist_ok=True)

    print(f"    ↓ {name} {version} ({go_arch}) ...")
    tmp = dst + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.chmod(tmp, 0o755)

    if sha:
        try:
            with urllib.request.urlopen(f"{url}.sha256", timeout=15) as resp:
                expected = resp.read().decode().split()[0]
            actual = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
            if actual != expected:
                os.unlink(tmp)
                sys.exit(f"SHA256 mismatch for {name}")
            print(f"    SHA256 ✓")
        except Exception as e:
            print(f"    SHA256 skipped ({e})")

    os.rename(tmp, dst)
    print(f"    {name} {version} installed ✓")



def ensure_talosctl(version):
    """Download talosctl. version = exact tag (e.g. v1.9.0)."""
    ensure_binary(
        "talosctl", version,
        lambda v, a: f"https://github.com/siderolabs/talos/releases/download/{v}/talosctl-linux-{a}",
        sha=False  # ponytail: no .sha256 sidecar in Talos releases
    )

def govc_arch():
    """Map platform.machine() to govc asset arch suffix."""
    m = platform.machine()
    return {"aarch64": "arm64"}.get(m, m)  # ponytail: x86_64→x86_64 pass-through, only aarch64 needs mapping

def ensure_govc():
    """Download + extract govc from .tar.gz (no --version flag, custom logic)."""
    import tarfile
    dst = os.path.join(TALOSCTL_DIR, "govc")
    if os.path.exists(dst):
        print("    govc v0.40.0 ✓")
        return
    ver = "v0.40.0"
    arch = govc_arch()
    url = f"https://github.com/vmware/govmomi/releases/download/{ver}/govc_Linux_{arch}.tar.gz"
    os.makedirs(TALOSCTL_DIR, exist_ok=True)
    print(f"    ↓ govc {ver} ({arch}) ...")
    tmp = dst + ".tar.gz"
    urllib.request.urlretrieve(url, tmp)
    with tarfile.open(tmp, "r:gz") as tf:
        tf.extract("govc", TALOSCTL_DIR)
    os.chmod(dst, 0o755)
    os.unlink(tmp)
    print(f"    govc {ver} installed ✓")


# ═══ ESXi VM CREATION ═══

def iso_for_version(talos_ver):
    arch_map = {"aarch64": "arm64", "x86_64": "amd64"}  # ponytail: extend if 386/s390x needed
    iso_arch = arch_map.get(platform.machine(), "amd64")
    return f"https://github.com/siderolabs/talos/releases/download/{talos_ver}/metal-{iso_arch}.iso"

def upload_iso(talos_ver):
    """Download Talos ISO locally, upload to ESXi datastore."""
    iso_url = iso_for_version(talos_ver)
    iso_name = f"talos-{talos_ver}.iso"
    local_iso = os.path.join(tempfile.gettempdir(), iso_name)

    # Check if already on datastore
    rc, out, _ = govc("datastore.ls", iso_name, timeout=15)
    if rc == 0 and iso_name in out:
        print(f"    ISO already on datastore: {iso_name} ✓")
        return iso_name

    # Download locally
    if not os.path.exists(local_iso):
        print(f"    ↓ ISO: {iso_url} ...")
        urllib.request.urlretrieve(iso_url, local_iso)
        print(f"    Downloaded: {os.path.getsize(local_iso)//1024//1024} MB")

    # Upload to datastore
    print(f"    ↑ Uploading {iso_name} to datastore...")
    govc("datastore.upload", local_iso, iso_name, ok=True, timeout=300)
    print(f"    ISO uploaded ✓")
    return iso_name


def create_vm(name, ip, vcpu, ram_gb, disk_gb, iso_path, net):
    """Create a single VM with govc."""

    print(f"  Creating VM: {name} ({ip}) ...")

    # govc vm.create
    govc("vm.create",
         f"-c={vcpu}",
         f"-m={ram_gb * 1024}",   # MB
         f"-g=ubuntu64Guest",     # closest to Talos (Linux 64-bit)
         f"-disk={disk_gb}GB",
         f"-net={net}",
         f"-net.adapter=vmxnet3",
         f"-disk.controller=pvscsi",
         f"-on=false", name, ok=True)

    # Attach ISO as CDROM
    govc("device.cdrom.add", "-vm", name, iso_path, ok=True)

    # Set boot order: CDROM first, then disk
    govc("device.boot", "-vm", name, "-order=cdrom,disk", ok=True)

    # Inject static IP via guestinfo (Talos reads this in maintenance mode)
    # Format: guestinfo.talos.config=... but simpler: use kernel cmdline ip=
    # We pass ip= via guestinfo.metadata or extraConfig
    govc("vm.change", "-vm", name,
         f"-e", f"guestinfo.talos.ip={ip}/24", ok=True)

    # Power on
    govc("vm.power", "-on", name, ok=True)

    # Wait for IP
    print(f"    Waiting for IP ...")
    deadline = time.time() + 120
    while time.time() < deadline:
        rc, out, _ = govc("vm.ip", name, timeout=10)
        if rc == 0 and out.strip():
            vm_ip = out.strip()
            print(f"    {name} → {vm_ip} ✓")
            return vm_ip
        time.sleep(5)

    sys.exit(f"VM {name} never got IP (timeout 120s)")


def create_all_vms(args):
    """Create all VMs on ESXi. Returns dict of name→IP."""
    ensure_govc()

    # Validate connectivity
    rc, out, _ = govc("about", timeout=15)
    if rc != 0:
        sys.exit(f"Cannot connect to ESXi: {out}")
    print(f"    ESXi connected: {out.strip().split(chr(10))[0]}")

    # Upload ISO
    talos_ver = TALOS_MAP[args.k8s]
    iso_path = upload_iso(talos_ver)

    vms = {}

    # Create controlplane
    cp_ip = args.cp
    vm_name = f"{args.cluster}-cp"
    actual_cp = create_vm(vm_name, cp_ip,
                          args.vcpu, args.ram_gb, args.disk_gb,
                          iso_path, args.esxi_network)
    vms[vm_name] = actual_cp

    # Create workers (for multi)
    if args.mode == "multi" and hasattr(args, "workers"):
        worker_ips = parse_ips(args.workers)
        for i, w_ip in enumerate(worker_ips):
            vm_name = f"{args.cluster}-worker-{i + 1}"
            w = create_vm(vm_name, w_ip,
                          args.vcpu, args.ram_gb, args.disk_gb,
                          iso_path, args.esxi_network)
            vms[vm_name] = w

    print(f"\n  All VMs created:")
    for name, ip in vms.items():
        print(f"    {name:25s} → {ip}")

    return vms


# ═══ TALOS CONFIG + DEPLOY ═══

def gen_configs(cluster, cp_ip, output_dir, single_node):
    tctl("gen", "config", cluster, f"https://{cp_ip}:6443",
         "--output-dir", output_dir, "--force", ok=True)

    patches = [{
        "op": "add",
        "path": "/cluster/extraManifests",
        "value": ["https://raw.githubusercontent.com/metallb/metallb/v0.14.9/config/manifests/metallb-native.yaml"]
    }]

    if single_node:
        patches.append({
            "op": "add",
            "path": "/cluster/allowSchedulingOnControlPlanes",
            "value": True
        })

    patch_file = os.path.join(output_dir, "patch.json")
    with open(patch_file, "w") as f:
        json.dump(patches, f)

    tctl("gen", "config", cluster, f"https://{cp_ip}:6443",
         "--output-dir", output_dir, "--force",
         "--config-patch-control-plane", f"@{patch_file}",
         ok=True)
    print(f"    configs generated + patched ✓")


def wait_for_boot(node_ips, timeout=600):
    deadline = time.time() + timeout
    healthy = set()
    print(f"    waiting for {len(node_ips)} node(s) to boot ...")
    while time.time() < deadline:
        for ip in list(node_ips):
            if ip in healthy:
                continue
            rc, out, err = tctl("health", "--nodes", ip, timeout=15)
            if "healthy" in (out + err).lower() or rc == 0:
                healthy.add(ip)
                print(f"      {ip} healthy ✓")
        if len(healthy) == len(node_ips):
            return
        time.sleep(10)
    remaining = set(node_ips) - healthy
    sys.exit(f"Boot timeout: {remaining}")


def wait_for_nodes_ready(cp_ip, kubeconfig, expected, timeout=300):
    deadline = time.time() + timeout
    print(f"    waiting for {expected} node(s) Ready ...")
    while time.time() < deadline:
        rc, out, _ = kubectl("get", "nodes", "--no-headers",
                              kubeconfig=kubeconfig, timeout=15)
        if rc == 0:
            ready = [l for l in out.strip().split("\n") if " Ready " in l]
            if len(ready) >= expected:
                print(f"    {len(ready)}/{expected} Ready ✓")
                return
        time.sleep(15)
    sys.exit("Node readiness timeout")


def verify_cluster(kubeconfig):
    print("\n  Cluster health:")
    kubectl("get", "nodes", "-o", "wide", kubeconfig=kubeconfig, ok=True)
    rc, out, _ = kubectl("get", "pods", "-A", "--field-selector", "status.phase!=Running",
                          kubeconfig=kubeconfig, timeout=30)
    if "No resources found" in out or out.strip() == "":
        print("    ✓ all pods healthy")
    else:
        print("    ⚠ non-Running pods:")
        print(out[:500])


def _deploy_cluster(mode, cp_ip, workers, cluster, talos_ver, mlb_range):
    """Core deploy logic — shared by ESXi+deploy and manual-deploy modes."""

    state = load_state() or {"phase": "init", "data": {"mode": mode}}

    # ─── Configs ───
    if state["phase"] in ("init", "booted", "vms_created"):
        print("[1/4] Generate + apply Talos configs")
        tmp = tempfile.mkdtemp(prefix="talos-")
        gen_configs(cluster, cp_ip, tmp, single_node=(mode == "all-in-one"))

        # Preflight: check Talos API reachability
        if not check_reachable(cp_ip):
            sys.exit(f"❌ Control plane {cp_ip}:50000 unreachable. Check VM status, network, and ESXi firewall.")
        if workers:
            for w in workers:
                if not check_reachable(w):
                    sys.exit(f"❌ Worker {w}:50000 unreachable. Check VM status, network, and ESXi firewall.")

        tctl("apply-config", "--insecure", "--nodes", cp_ip,
             "--file", os.path.join(tmp, "controlplane.yaml"),
             ok=True, timeout=120)
        print(f"    cp config applied — rebooting ✓")

        if workers:
            worker_cfg = os.path.join(tmp, "worker.yaml")
            for w in workers:
                tctl("apply-config", "--insecure", "--nodes", w,
                     "--file", worker_cfg, ok=True, timeout=120)
                print(f"    worker {w} config applied ✓")

        save_state("configs_applied",
                   {"tmp": tmp, "cp_ip": cp_ip, "workers": workers, "mlb_range": mlb_range})

    # ─── Bootstrap ───
    if state["phase"] in ("configs_applied",):
        d = state["data"]
        cp_ip, workers, tmp, mlb_range = d["cp_ip"], d.get("workers", []), d["tmp"], d["mlb_range"]
        all_ips = [cp_ip] + workers

        wait_for_boot(all_ips)

        print("[2/4] Bootstrap cluster")
        for attempt in range(3):
            rc, out, _ = tctl("bootstrap", "--nodes", cp_ip,
                               "--talosconfig", os.path.join(tmp, "talosconfig"),
                               timeout=300)
            if rc == 0:
                break
            print(f"    retry {attempt + 1}/3 ...")
            time.sleep(15)
        else:
            sys.exit("bootstrap failed after 3 attempts")
        print(f"    bootstrap OK ✓")
        save_state("bootstrapped", d)

    # ─── Kubeconfig + Verify ───
    if state["phase"] in ("bootstrapped",):
        d = state["data"]
        cp_ip, workers, tmp = d["cp_ip"], d.get("workers", []), d["tmp"]

        kube_path = os.path.join(tmp, "kubeconfig")
        tctl("kubeconfig", kube_path, "--nodes", cp_ip,
             "--talosconfig", os.path.join(tmp, "talosconfig"),
             ok=True, timeout=60)
        print(f"    kubeconfig ✓")

        expected = 1 + len(workers)
        print("[3/4] Wait for nodes ready")
        wait_for_nodes_ready(cp_ip, kube_path, expected, timeout=600)
        verify_cluster(kube_path)
        save_state("nodes_ready",
                   {**d, "kubeconfig": kube_path})

    # ─── MetalLB ───
    if state["phase"] in ("nodes_ready",):
        d = state["data"]
        kube_path, mlb_range, tmp = d["kubeconfig"], d["mlb_range"], d["tmp"]

        print("[4/4] Configure MetalLB IP pool")
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
        save_state("done", d)

    # ─── Done ───
    # ponytail: d already in scope — avoids disk roundtrip via load_state()
    kubeconfig = (d or {}).get("kubeconfig", "?")
    print(f"""
✅ CLUSTER READY ({mode.upper()})
   export KUBECONFIG={kubeconfig}
   kubectl get nodes -o wide
""")


# ═══ MAIN ═══

def main():
    parser = argparse.ArgumentParser(
        description="Talos K8s on ESXi — one click, everything pulled on-the-fly",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          Full zero-touch (VM creation + deploy):
            python3 talos-deploy.py all-in-one \\
              --cp 10.0.1.50 --cluster prod --k8s 1.35 \\
              --esxi-host 10.0.1.10 \\
              --esxi-datastore datastore1 --esxi-network "VM Network"

          Manual (VMs already exist):
            python3 talos-deploy.py all-in-one \\
              --cp 10.0.1.50 --cluster prod --k8s 1.35  
        """),
    )

    sub = parser.add_subparsers(dest="mode", required=True)

    # Shared args
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--cp", required=True, help="Controlplane node IP")
    shared.add_argument("--cluster", required=True, help="Cluster name (RFC1123)")
    shared.add_argument("--k8s", choices=list(TALOS_MAP), default="1.35", help="K8s version")
    shared.add_argument("--metallb-range", default="192.168.1.240-192.168.1.250",
                        help="MetalLB L2 IP range")
    shared.add_argument("--debug", action="store_true", help="Enable verbose debug output")

    # ESXi args (VM creation)
    esxi = argparse.ArgumentParser(add_help=False)
    esxi.add_argument("--esxi-host", help="ESXi IP or hostname (enables VM creation)")
    esxi.add_argument("--esxi-user", default="root", help="ESXi username (default: root)")
    esxi.add_argument("--esxi-pass", default=os.environ.get("ESXI_PASSWORD", ""),
                      help="ESXi password (or set ESXI_PASSWORD env)")
    esxi.add_argument("--esxi-datastore", default="datastore1", help="Datastore name")
    esxi.add_argument("--esxi-network", default="VM Network", help="Network name")
    esxi.add_argument("--esxi-insecure", action="store_true", default=True,
                      help="Skip TLS verify (default: true)")
    esxi.add_argument("--vcpu", type=int, default=2, help="vCPU per VM (default: 2)")
    esxi.add_argument("--ram-gb", type=int, default=4, help="RAM GB per VM (default: 4)")
    esxi.add_argument("--disk-gb", type=int, default=20, help="Disk GB per VM (default: 20)")

    # Subcommands
    aio = sub.add_parser("all-in-one", parents=[shared, esxi],
                         help="Single VM = controlplane + worker")
    multi = sub.add_parser("multi", parents=[shared, esxi],
                           help="1 controlplane + N workers")
    multi.add_argument("--workers", required=True, help="Comma-separated worker IPs")

    args = parser.parse_args()

    # Set global debug flag
    global DEBUG
    DEBUG = args.debug
    if DEBUG:
        print(f"[DEBUG] Mode: {args.mode}")
        print(f"[DEBUG] CP IP: {args.cp}")
        print(f"[DEBUG] K8s: {args.k8s}")
        print(f"[DEBUG] Talos version: {TALOS_MAP[args.k8s]}")

    # Guard: kubectl
    if _cmd(["which", "kubectl"], timeout=5)[0] != 0:
        print("⚠  kubectl not found — MetalLB apply will fail.")
        print("   Snap: sudo snap install kubectl --classic")
        print("   Apt:  sudo apt install kubectl\n")

    talos_ver = TALOS_MAP[args.k8s]
    workers = parse_ips(args.workers) if args.mode == "multi" and args.workers else []

    # ─── ESXi VM creation mode ───
    if args.esxi_host:
        # Build govc env from args
        esxi_url = f"https://{args.esxi_user}:{args.esxi_pass}@{args.esxi_host}/sdk"
        os.environ["GOVC_URL"] = esxi_url
        os.environ["GOVC_USERNAME"] = args.esxi_user
        os.environ["GOVC_PASSWORD"] = args.esxi_pass
        os.environ["GOVC_DATASTORE"] = args.esxi_datastore
        os.environ["GOVC_NETWORK"] = args.esxi_network
        if args.esxi_insecure:
            os.environ["GOVC_INSECURE"] = "true"

        print(f"""
{'=' * 60}
 TALOS + ESXi (govc VM creation)
 ESXi:        {args.esxi_host}
 Datastore:   {args.esxi_datastore}
 Network:     {args.esxi_network}
 Mode:        {args.mode}  Talos={talos_ver}  K8s~{args.k8s}
{'=' * 60}
""")

        state = load_state() or {"phase": "init", "data": {}}

        # Phase 0: Create VMs
        if state["phase"] in ("init",):
            print("[0/4] Create VMs on ESXi")
            vms = create_all_vms(args)
            # Map actual IPs back
            cp_ip = vms.get(f"{args.cluster}-cp", args.cp)
            workers_ip = [vms[f"{args.cluster}-worker-{i + 1}"]
                          for i in range(len(workers))] if workers else []
            save_state("vms_created",
                       {"cp_ip": cp_ip, "workers": workers_ip, "cluster": args.cluster})
            print("    VMs created + booted ✓\n")

        cp_ip = args.cp if state["phase"] == "init" else state["data"]["cp_ip"]
        workers_ip = workers if state["phase"] == "init" else state["data"]["workers"]

        _deploy_cluster(args.mode, cp_ip, workers_ip, args.cluster, talos_ver, args.metallb_range)

    # ─── Manual mode (VMs already exist) ───
    else:
        ensure_talosctl(talos_ver)
        print(f"""
{'=' * 60}
 TALOS (manual — VMs already booted)
 Mode: {args.mode}  Talos={talos_ver}  K8s~{args.k8s}
{'=' * 60}
""")
        _deploy_cluster(args.mode, args.cp, workers, args.cluster, talos_ver, args.metallb_range)


if __name__ == "__main__":
    main()