# Talos Deploy — One-Click K8s on ESXi

Python script deploys Talos Linux Kubernetes cluster on ESXi.

**Now with full zero-touch ESXi integration via `govc`:**
- Creates VMs (CPU/RAM/disk configurable)
- Uploads Talos ISO to datastore
- Attaches ISO, sets boot order, powers on
- Waits for DHCP IP assignment
- Then deploys Talos cluster

Or use manual mode — VMs already exist. Same script, no govc needed.

---

## Quick Start

### Full zero-touch (create VMs + deploy in one command)

```bash
python3 talos-deploy.py all-in-one \
  --cp 10.0.1.50 --cluster prod --k8s 1.35 \
  --esxi-host 10.0.1.10 \
  --esxi-user root --esxi-pass mypassword \
  --esxi-datastore datastore1 \
  --esxi-network "VM Network" \
  --metallb-range 10.0.1.240-10.0.1.250
```

### Manual (VMs already booted from ISO — no ESXi args needed)

```bash
python3 talos-deploy.py all-in-one \
  --cp 10.0.1.50 --cluster prod --k8s 1.35
```

### Multi-node (zero-touch)

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

## ESXi VM Creation (govc mode)

When `--esxi-host` is provided, the script:

1. **Downloads govc** (`v0.40.0`, SHA256 verified) — VMware CLI for ESXi automation
2. **Uploads Talos ISO** to ESXi datastore (downloads from `factory.talos.dev` if needed)
3. **Creates VMs** with specified CPU/RAM/disk, vmxnet3 NIC, pvscsi controller
4. **Attaches ISO** as CDROM, sets boot order `cdrom → disk`
5. **Injects guestinfo** for Talos static IP (`guestinfo.talos.ip`)
6. **Powers on** + **waits for IP** (via `govc vm.ip` polling)
7. **Proceeds to deploy** — config generation, apply, bootstrap, MetalLB

### govc environment variables

As an alternative to CLI flags, set these env vars:

```bash
export GOVC_URL=https://root:password@10.0.1.10/sdk
export GOVC_USERNAME=root
export GOVC_PASSWORD=secret
export GOVC_DATASTORE=datastore1
export GOVC_NETWORK="VM Network"
export GOVC_INSECURE=true
```

Then run without `--esxi-*` flags — govc picks them up.

---

## CLI Reference

| Flag | Required | Default | Description |
|---|---|---|---|
| `--cp` | Yes | — | Controlplane node IP |
| `--cluster` | Yes | — | Cluster name (lowercase, RFC1123) |
| `--k8s` | No | `1.35` | K8s version: `1.34` / `1.35` / `1.36` |
| `--metallb-range` | No | `192.168.1.240-192.168.1.250` | MetalLB L2 IP pool |
| `--esxi-host` | No | — | ESXi IP/hostname → enables govc VM creation |
| `--esxi-user` | No | `root` | ESXi username |
| `--esxi-pass` | No | `$ESXI_PASSWORD` | ESXi password (or set env) |
| `--esxi-datastore` | No | `datastore1` | Datastore name |
| `--esxi-network` | No | `VM Network` | Network/portgroup name |
| `--vcpu` | No | `2` | vCPU per VM |
| `--ram-gb` | No | `4` | RAM per VM (GB) |
| `--disk-gb` | No | `20` | Disk per VM (GB) |
| `--workers` | Yes (multi) | — | Worker IPs, comma-separated |

---

## Architecture

```
Your machine (any Linux) ──govc──▶ ESXi Host
                                    │
    ┌─ download govc                │  govc vm.create (2 vCPU / 4 GB / 20 GB)
    └─ download Talos ISO            │  govc datastore.upload ISO
                                    │  govc device.cdrom.add ISO
                                    │  govc vm.power -on
                                    │  govc vm.ip → get DHCP IP
                                    ▼
                               ┌─────────────┬──────────────────┐
                               │  CP node    │  Worker 1,2,...  │
                               │  (Talos)    │  (Talos)         │
                               └─────────────┴──────────────────┘
                                    │
  ┌─ talosctl gen config            │
  ├─ talosctl apply-config ─────────┤
  ├─ talosctl bootstrap ────────────┘
  ├─ talosctl kubeconfig
  ├─ kubectl wait nodes Ready
  └─ kubectl apply MetalLB pool
```

---

## K8s ↔ Talos ↔ govc

| `--k8s` | Talos Release | govc | Notes |
|---|---|---|---|
| `1.34` | `v1.8` | `v0.40.0` | Stable |
| `1.35` | `v1.9` | `v0.40.0` | Default |
| `1.36` | `v1.10` | `v0.40.0` | Bleeding edge |

---

## Resume / State

Progress saved to `/tmp/talos-deploy-state.json`:

```
init → vms_created → configs_applied → bootstrapped → nodes_ready → done
```

**Crash mid-run?** Re-run exact same command. Resumes from last phase.

---

## Pre-Requisites

| Tool | How to get |
|---|---|
| Python 3.8+ | Built-in |
| `kubectl` | `snap install kubectl --classic` |
| `govc` | **Auto-downloaded** by script (v0.40.0, SHA256 verified) |
| `talosctl` | **Auto-downloaded** by script (SHA256 verified) |
| ESXi 7.0+ | Must be reachable on HTTPS 443 |
| Internet | github.com, factory.talos.dev, k8s.io |

---

## Post-Deploy Test

```bash
export KUBECONFIG=/tmp/talos-XXXXXX/kubeconfig

kubectl create deployment nginx --image=nginx --replicas=2
kubectl expose deployment nginx --port=80 --type=LoadBalancer
kubectl get svc nginx
# EXTERNAL-IP = MetalLB IP from pool
curl http://<EXTERNAL-IP>
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `govc: command not found` | Auto-downloaded. Check `~/.local/bin/` in PATH |
| `Cannot connect to ESXi` | Check `--esxi-host`, firewall, TLS. Add `--esxi-insecure` for self-signed certs |
| VM creation fails | Check datastore has space (`govc datastore.info`). Network name must match exactly |
| `vm.ip` never returns | VM needs DHCP on that network. Talos prints IP on console once booted |
| ISO upload slow | Download happens once locally + upload once per deploy. ~80 MB ISO |
| `bootstrap failed` | VM RAM < 2 GB. Increase `--ram-gb 4` |
| MetalLB IPs unreachable | Must be on same L2 as VMs. Talos uses host network, no overlay |

---

## Cleanup

```bash
# Destroy VMs
govc vm.destroy talos-prod-cp
govc vm.destroy talos-prod-worker-1
govc vm.destroy talos-prod-worker-2

# Delete state (start fresh)
rm /tmp/talos-deploy-state.json
```

---

## License

MIT