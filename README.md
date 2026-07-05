# Talos Deploy — One-Click K8s on ESXi

Python script deploys Talos Linux Kubernetes cluster on ESXi. Everything pulled on-the-fly.

**Now with full zero-touch ESXi integration via `govc`** — creates VMs, uploads ISO, powers on, deploys cluster. One command.

Or manual mode if your VMs already exist. Same script, no govc needed.

```
┌──────────────────┐    govc     ┌──────────────┐   talosctl   ┌─────────────────────────┐
│  You run         │───────────▶│  ESXi Host    │◀────────────│  Talos K8s cluster      │
│  1 command       │ VM create  │  upload ISO   │ config+init │  MetalLB + Calico ready │
│  (this machine)  │ ISO upload │  power on     │             │                         │
└──────────────────┘            │  wait for IP  │             └─────────────────────────┘
                                └──────────────┘
```

## Features

- **True one-click** — creates VMs via govc, uploads ISO, deploys cluster
- **Two modes** — single-node (all-in-one) or multi-node (cp + N workers)
- **K8s 1.34 / 1.35 / 1.36** — Talos v1.8 / v1.9 / v1.10
- **MetalLB** — L2 IP pool auto-configured
- **NetworkPolicy** — Calico baked into Talos (default CNI)
- **Resumable** — if script crashes, rerun resumes from last phase
- **SHA256 verified** — talosctl + govc binaries verified on download
- **Zero dependencies** — Python 3.8+ stdlib only
- **Arch-aware** — auto-selects arm64 or amd64 ISO + CLI binaries

---

## Quick Start

### Zero-touch (full ESXi VM creation + deploy)

```bash
python3 talos-deploy.py all-in-one \
  --cp 10.0.1.50 --cluster prod --k8s 1.35 \
  --esxi-host 10.0.1.10 \
  --esxi-user root --esxi-pass mypassword \
  --esxi-datastore datastore1 \
  --esxi-network "VM Network" \
  --metallb-range 10.0.1.240-10.0.1.250
```

### Manual (VMs already booted, no ESXi args)

```bash
python3 talos-deploy.py all-in-one \
  --cp 10.0.1.50 --cluster prod --k8s 1.35
```

### Multi-node zero-touch

```bash
python3 talos-deploy.py multi \
  --cp 10.0.1.50 --workers 10.0.1.51,10.0.1.52,10.0.1.53 \
  --cluster prod --k8s 1.36 \
  --esxi-host 10.0.1.10 \
  --esxi-user root --esxi-pass mypassword \
  --esxi-datastore datastore1 \
  --esxi-network "VM Network"
```

---

## CLI Reference

| Flag | Required | Default | Description |
|---|---|---|---|
| `--cp` | Yes | — | Controlplane node IP |
| `--cluster` | Yes | — | Cluster name (lowercase, RFC1123) |
| `--k8s` | No | `1.35` | K8s version: `1.34` / `1.35` / `1.36` |
| `--metallb-range` | No | `192.168.1.240–250` | MetalLB L2 IP pool |
| **ESXi (govc VM creation)** | | | |
| `--esxi-host` | No | — | ESXi IP/hostname → enables govc mode |
| `--esxi-user` | No | `root` | ESXi username |
| `--esxi-pass` | No | `$ESXI_PASSWORD` | ESXi password (or set env var) |
| `--esxi-datastore` | No | `datastore1` | Datastore name |
| `--esxi-network` | No | `VM Network` | Network/portgroup name |
| `--vcpu` | No | `2` | vCPU per VM |
| `--ram-gb` | No | `4` | RAM per VM (GB) |
| `--disk-gb` | No | `20` | Disk per VM (GB) |
| `--workers` | Yes (multi) | — | Worker IPs, comma-separated |

### govc environment variables (alternative to CLI flags)

```bash
export GOVC_URL=https://root:password@10.0.1.10/sdk
export GOVC_USERNAME=root
export GOVC_PASSWORD=secret
export GOVC_DATASTORE=datastore1
export GOVC_NETWORK="VM Network"
export GOVC_INSECURE=true
```

Then omit `--esxi-*` flags — govc reads env vars.

---

## Step-by-Step: Full Zero-Touch Mode

### 1. Pre-flight checks

```bash
# Install kubectl if missing
snap install kubectl --classic

# Verify ESXi is reachable
curl -k https://<ESXI_IP>/ui
```

### 2. Run the script

```bash
python3 talos-deploy.py all-in-one \
  --cp 10.0.1.50 --cluster prod --k8s 1.35 \
  --esxi-host 10.0.1.10 \
  --esxi-pass your-password \
  --esxi-datastore datastore1 \
  --esxi-network "VM Network"
```

### 3. Watch it happen

```
[0/4] Create VMs on ESXi
      ↓ govc v0.40.0 installed ✓
      ↓ ISO v1.9 → factory.talos.dev ... (80 MB)
      ↑ Uploading to datastore ... ✓
  Creating VM: prod-cp (10.0.1.50) ... → 10.0.1.50 ✓
      All VMs created + booted ✓

[1/4] Generate + apply Talos configs
      configs generated + patched ✓
      cp config applied — rebooting ✓

[2/4] Bootstrap cluster
      waiting for nodes to boot ... → healthy ✓
      bootstrap OK ✓

[3/4] Wait for nodes ready
      1/1 Ready ✓
      ✓ all pods healthy

[4/4] Configure MetalLB IP pool
      MetalLB pool 10.0.1.240-10.0.1.250 ✓

✅ CLUSTER READY
   export KUBECONFIG=/tmp/talos-XXXXXX/kubeconfig
```

### 4. Verify

```bash
export KUBECONFIG=/tmp/talos-XXXXXX/kubeconfig
kubectl get nodes -o wide
kubectl get pods -A

# Test LoadBalancer
kubectl create deployment nginx --image=nginx --replicas=2
kubectl expose deployment nginx --port=80 --type=LoadBalancer
kubectl get svc nginx
curl http://<EXTERNAL-IP>
```

---

## What govc Does

| govc command | Purpose |
|---|---|
| `govc datastore.upload` | Upload Talos ISO (~80 MB) to ESXi datastore |
| `govc vm.create` | Create VM: 2 vCPU, 4 GB RAM, 20 GB disk, vmxnet3 NIC, pvscsi controller |
| `govc device.cdrom.add` | Attach ISO as CDROM drive |
| `govc device.boot` | Set boot order: CDROM first, then disk |
| `govc vm.change -e guestinfo.talos.ip=` | Inject IP hint for Talos |
| `govc vm.power -on` | Power on the VM |
| `govc vm.ip` | Poll for DHCP IP, blocks until assigned |

Only runs if `--esxi-host` is provided. Otherwise manual mode — script assumes VMs already booted.

---

## Architecture

```
Your machine (Jetson / laptop / CI)
    │
    ├─ urllib ───▶ github.com (talosctl + govc binaries, SHA256 verified)
    ├─ urllib ───▶ factory.talos.dev (Talos ISO, ~80 MB)
    │
    ├─ govc ──────▶ ESXi host (HTTPS 443)
    │               ├─ vm.create (prod-cp, prod-worker-1, ...)
    │               ├─ datastore.upload ISO
    │               ├─ device.cdrom.add ISO
    │               ├─ vm.power -on
    │               └─ vm.ip → DHCP IP returned
    │
    ├─ talosctl ──▶ Talos nodes (TCP 50000)
    │               ├─ gen config (cluster.yaml, talosconfig)
    │               ├─ apply-config (node reboots into Talos)
    │               ├─ health (wait for boot)
    │               ├─ bootstrap (init controlplane)
    │               └─ kubeconfig (fetch for kubectl)
    │
    └─ kubectl ───▶ K8s API (TCP 6443)
                    ├─ get nodes (wait for Ready)
                    └─ apply MetalLB pool
```

---

## K8s ↔ Talos ↔ Components

| `--k8s` | Talos | govc | Calico | MetalLB | K8s API |
|---|---|---|---|---|---|
| `1.34` | v1.8 | v0.40.0 | v3.29 | v0.14.9 | ~1.34 |
| `1.35` | v1.9 | v0.40.0 | v3.29 | v0.14.9 | ~1.35 |
| `1.36` | v1.10 | v0.40.0 | v3.29 | v0.14.9 | ~1.36 |

---

## Resume / State

Progress saved to `/tmp/talos-deploy-state.json` after each phase:

```
init → vms_created → configs_applied → bootstrapped → nodes_ready → done
```

**Crash mid-run?** Re-run the exact same command. Resumes from last completed phase.

**Start fresh:** `rm /tmp/talos-deploy-state.json`

---

## Pre-Requisites

| Requirement | Check |
|---|---|
| Python 3.8+ | Built-in |
| `kubectl` | `snap install kubectl --classic` |
| Internet access | To `github.com`, `factory.talos.dev`, `k8s.io` |
| ESXi 7.0+ | Reachable on HTTPS 443 from this machine |
| ESXi credentials | Root or admin user with VM creation permissions |
| Datastore space | ~100 MB for ISO, 20 GB per VM disk |
| DHCP on target network | VMs must get IPs automatically |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `govc not found` | Auto-downloaded to `~/.local/bin/`. Check PATH includes it |
| `Cannot connect to ESXi` | Check IP, firewall TCP 443, TLS cert. Use `--esxi-insecure` for self-signed |
| Datastore upload fails | Check datastore name (`govc datastore.ls`). Must match exactly |
| `vm.ip` never returns | Network needs DHCP. Talos prints IP on console once booted |
| `bootstrap failed` | VM RAM < 2 GB. Increase `--ram-gb 4` |
| MetalLB IPs unreachable | Must be same L2 as VMs. Talos uses host network, not overlay |
| `govc` hang on ISO upload | Slow network. ~80 MB ISO. Timeout increased to 300s |
| Node stuck in `NotReady` | Calico pods pulling images. Wait 2-3 min, check `kubectl get pods -n calico-system` |

---

## Cleanup

```bash
# Destroy VMs via govc
govc vm.destroy prod-cp
govc vm.destroy prod-worker-1
govc vm.destroy prod-worker-2

# Or via ESXi web UI → delete VMs

# Delete state file
rm /tmp/talos-deploy-state.json
```

---

## Post-Deploy Examples

```bash
export KUBECONFIG=/tmp/talos-XXXXXX/kubeconfig

# LB deployment
kubectl create deployment myapp --image=nginx --replicas=3
kubectl expose deployment myapp --port=80 --type=LoadBalancer
kubectl get svc myapp
# NAME    TYPE           CLUSTER-IP     EXTERNAL-IP    PORT(S)
# myapp   LoadBalancer   10.96.45.123   10.0.1.241     80:31234/TCP

curl http://10.0.1.241

# NetworkPolicy (Calico enforces)
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all
spec:
  podSelector: {}
  policyTypes: [Ingress]
EOF
```

---

## License

MIT