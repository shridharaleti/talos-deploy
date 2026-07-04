# Talos Deploy

One-click Kubernetes cluster deployment on ESXi using [Talos Linux](https://www.talos.dev/).

Everything pulled on-the-fly — no pre-staged ISOs, no manual `kubeadm`, no SSH into nodes.

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────────┐
│  You run    │────▶│  This script │────▶│  Talos K8s cluster      │
│  1 command  │     │  (this host) │     │  on ESXi VMs            │
└─────────────┘     └──────────────┘     │  MetalLB + Calico ready │
                                         └─────────────────────────┘
```

## Features

- **One command** — handles everything from talosctl download to MetalLB config
- **Two modes** — single-node (all-in-one) or multi-node (cp + N workers)
- **K8s 1.34 / 1.35 / 1.36** — choose your version
- **MetalLB included** — L2 IP pool auto-configured
- **NetworkPolicy** — Calico baked into Talos by default
- **Resumable** — if script crashes, rerun resumes from last phase
- **SHA256 verified** — talosctl binary verified on download
- **Zero dependencies** — Python 3.8+ stdlib only
- **Arch-aware** — auto-selects arm64 or amd64 ISO + talosctl

---

## Prerequisites

### On this machine (where you run the script)

| Requirement | Install |
|---|---|
| Python 3.8+ | Usually pre-installed |
| `kubectl` | `snap install kubectl --classic` or `apt install kubectl` |
| Internet | Access to `github.com`, `factory.talos.dev`, `k8s.io` |

### On ESXi

| Requirement | Notes |
|---|---|
| ESXi 7.0+ with web UI | For ISO upload + VM creation |
| VMs created | 2+ vCPU, 4 GB RAM, 20 GB disk per node |
| Network | VMs on same L2 segment, reachable from this machine on TCP 50000 + 6443 |

---

## Step-by-Step Instructions

### 1. Create VMs on ESXi

Open ESXi web UI → **Virtual Machines** → **Create / Register VM**

For each node (controlplane + workers):
```
Guest OS family:  Linux
Guest OS version: Ubuntu Linux (64-bit)
Hardware:         2 vCPU, 4 GB RAM, 20 GB disk
Network:          VM Network (bridged, same L2)
CD/DVD:           Store ISO image → upload the Talos ISO (script provides URL)
```

> **Tip:** Clone a template VM for faster worker provisioning.

### 2. Boot VMs from ISO

1. Power on each VM → it boots from the Talos ISO
2. Wait for the Talos maintenance mode prompt:
   ```
   Talos Linux ...
   maintenance: login on console
   ```
3. Note the IP displayed (DHCP) or configure static via `ip=` kernel param

### 3. Run the script

**Single-node (all-in-one):**
```bash
python3 ~/talos-deploy/talos-deploy.py all-in-one \
  --cp 10.0.1.50 \
  --cluster mycluster \
  --k8s 1.35 \
  --metallb-range 10.0.1.240-10.0.1.250
```

**Multi-node (1 cp + N workers):**
```bash
python3 ~/talos-deploy/talos-deploy.py multi \
  --cp 10.0.1.50 \
  --workers 10.0.1.51,10.0.1.52,10.0.1.53 \
  --cluster mycluster \
  --k8s 1.36
```

The script will:
1. Download `talosctl` (SHA256 verified)
2. Print the ISO URL (for step 1 above)
3. Wait for you to confirm VMs are booted
4. Generate + apply Talos machine configs
5. Bootstrap the cluster
6. Wait for all nodes Ready
7. Install MetalLB IP pool
8. Print `export KUBECONFIG=...`

### 4. Verify

```bash
export KUBECONFIG=/tmp/talos-XXXXXX/kubeconfig
kubectl get nodes -o wide
kubectl get pods -A
```

### 5. Test LoadBalancer

```bash
kubectl create deployment nginx --image=nginx --replicas=2
kubectl expose deployment nginx --port=80 --type=LoadBalancer
kubectl get svc nginx
# → EXTERNAL-IP = one of your MetalLB pool IPs
curl http://<EXTERNAL-IP>
```

---

## CLI Reference

```
usage: talos-deploy.py [-h] {all-in-one,multi} ...

Options (shared):
  --cp CP               Controlplane node IP (required)
  --cluster CLUSTER     Cluster name, lowercase RFC1123 (required)
  --k8s {1.34,1.35,1.36}  K8s version (default: 1.35)
  --metallb-range       MetalLB L2 IP pool (default: 192.168.1.240-192.168.1.250)

Subcommands:
  all-in-one            Single VM = controlplane + worker
  multi                 1 controlplane + N workers

Multi-only:
  --workers             Comma-separated worker IPs (required for multi)
```

---

## K8s ↔ Talos Version Map

| `--k8s` | Talos | Status |
|---|---|---|
| `1.34` | v1.8 | Stable |
| `1.35` | v1.9 | **Default** — latest stable |
| `1.36` | v1.10 | Pre-release |

---

## Architecture

```
This machine (Jetson / laptop / CI)
  │
  │  talosctl apply-config --insecure
  │  talosctl bootstrap
  │  talosctl kubeconfig
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│                    ESXi Hypervisor                       │
│                                                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │ CP (Talos)  │  │ Worker 1    │  │ Worker 2    │     │
│  │ 10.0.1.50   │  │ 10.0.1.51   │  │ 10.0.1.52   │     │
│  │             │  │             │  │             │     │
│  │ kube-apisrv │  │ kubelet     │  │ kubelet     │     │
│  │ etcd        │  │ containerd  │  │ containerd  │     │
│  │ kube-sched  │  │             │  │             │     │
│  │ kube-ctrl   │  │             │  │             │     │
│  │ Calico      │  │ Calico      │  │ Calico      │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
│                                                          │
│  MetalLB: 10.0.1.240-10.0.1.250 (L2 advertisement)      │
└──────────────────────────────────────────────────────────┘
```

---

## What Gets Installed

| Component | Version | Purpose |
|---|---|---|
| **Talos Linux** | v1.8 / v1.9 / v1.10 | Immutable OS, no SSH, API-driven |
| **containerd** | Ships with Talos | Container runtime |
| **Calico** | v3.29 (via Talos) | CNI + NetworkPolicy enforcement |
| **MetalLB** | v0.14.9 | LoadBalancer service IPs (L2 mode) |
| **Kubernetes** | 1.34 / 1.35 / 1.36 | Cluster orchestration |

---

## Resume / State

The script saves progress to `/tmp/talos-deploy-state.json`.

Phases saved:
```
init → booted → configs_applied → bootstrapped → kube_ready → nodes_ready → done
```

**If the script crashes:** re-run the same command. It resumes from the last saved phase.

**To start over:** `rm /tmp/talos-deploy-state.json`

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `talosctl: command not found` | Auto-downloaded. Check `~/.local/bin/` in PATH |
| VMs don't get IPs | Check ESXi network config — VMs need DHCP or static IP via kernel param |
| `bootstrap failed` | VM RAM too low (need 2GB+), or API server not up. Wait 2 min, re-run |
| MetalLB IPs don't work | Check `kubectl get pods -n metallb-system`, all should be Running |
| `kubectl not found` | Install: `snap install kubectl --classic` |
| Script hangs at boot | Network unreachable. Check firewall on TCP 50000 |
| SHA256 mismatch | Corrupted download. Delete `~/.local/bin/talosctl`, re-run |
| Nodes not Ready | `kubectl describe node <name>` for events. Calico images may still be pulling |

---

## Cleanup

Talos is immutable — no `kubeadm reset` needed.

**Option 1:** `talosctl reset --nodes <IP>` from maintenance mode
**Option 2:** Power off VMs — Talos has no persistent state (except etcd on controlplane disk)
**Option 3:** Reimage — Talos wipes clean on boot if configured for `disk wipe`

---

## Post-Deploy Examples

```bash
# Deploy an app with LoadBalancer
kubectl create deployment myapp --image=nginx --replicas=3
kubectl expose deployment myapp --port=80 --type=LoadBalancer

# Check MetalLB assignment
kubectl get svc myapp
# NAME    TYPE           CLUSTER-IP     EXTERNAL-IP    PORT(S)        AGE
# myapp   LoadBalancer   10.96.45.123   10.0.1.241     80:31234/TCP   12s

# Access from any machine on the network
curl http://10.0.1.241

# Apply NetworkPolicy (Calico enforces)
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all
spec:
  podSelector: {}
  policyTypes:
  - Ingress
EOF

# All ingress blocked except what you allow
```

---

## License

MIT
