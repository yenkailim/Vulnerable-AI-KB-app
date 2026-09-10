# Ubuntu Kubernetes Setup with kubeadm and kubectl

This guide installs the Client Lookup Demo from scratch on one brand-new Ubuntu
host. It creates a real, single-node Kubernetes cluster with `kubeadm` and
manages it with `kubectl`. It does **not** use Minikube, kind, MicroK8s, or
Docker Desktop Kubernetes.

The completed host runs:

```text
Ubuntu host
+-- kubeadm Kubernetes control plane and worker
+-- containerd Kubernetes runtime
+-- Calico pod networking and NetworkPolicy
+-- vulnerableapp namespace
|   +-- Node/Express application
+-- vulnerableapp-db namespace
    +-- PostgreSQL
    +-- local persistent volume
```

This application and its SQL endpoints are intentionally vulnerable. It uses
synthetic data, but it must still be deployed only on an isolated training
host or trusted lab network.

## 1. Prerequisites

### Host

- Ubuntu Server 22.04 LTS or 24.04 LTS, 64-bit
- One host with a stable IP address and unique hostname
- At least 2 CPU cores, 4 GB RAM, and 20 GB free disk space
  - 4 CPU cores and 8 GB RAM are recommended for comfortable lab use
- A user with `sudo` access
- Internet access for Ubuntu packages and container images
- SSH access on TCP `22`, if the host is administered remotely
- Kubernetes control-plane ports available on the host: TCP `6443`,
  `2379-2380`, `10250`, `10257`, and `10259`
- TCP port `3000` allowed from the trusted client network if the web interface
  will be opened from another computer
- The project copied or cloned onto the Ubuntu host

On this single-node installation, Kubernetes component traffic stays on the
same host. For a future multi-node cluster, also allow the required Kubernetes
traffic between nodes and Calico's selected data plane traffic (for example,
UDP `4789` for VXLAN). Do not expose etcd or kubelet ports to untrusted
networks.

The commands below assume the repository is located at:

```text
/home/<user>/VulnerableApp_v2
```

Change that path to match the actual location.

### Network planning

This guide assigns `192.168.0.0/16` to Kubernetes pods. Before continuing,
confirm that this range is not already used by the host, LAN, VPN, or another
route:

```bash
ip route
```

If it overlaps, choose an unused private CIDR and use the same replacement in
both the `kubeadm init` and Calico configuration steps.

### Software installed by this guide

- containerd: Kubernetes container runtime
- kubelet: node agent
- kubeadm: cluster bootstrap tool
- kubectl: Kubernetes command-line client
- Calico: pod networking and NetworkPolicy enforcement
- Docker Engine: builds the application image locally
- curl, gpg, and ca-certificates: package installation prerequisites

The commands use the Kubernetes v1.37 package repository and Calico v3.32.2.
Kubernetes uses a separate package repository for each minor release. Keep all
Kubernetes components on the same minor version when selecting another track.

## 2. Prepare Ubuntu

Update the operating system and install the base packages:

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates conntrack curl git gpg apt-transport-https socat
```

Set a unique hostname if the host does not already have one:

```bash
sudo hostnamectl set-hostname vulnerableapp-k8s
```

Log out and back in if the shell prompt or hostname does not update.

### Disable swap now and after reboot

Kubelet will fail to start with its default configuration if swap is enabled.

Disable active swap:

```bash
sudo swapoff -a
```

Then edit `/etc/fstab` and comment out every active swap entry by placing `#`
at the beginning of its line:

```bash
sudo nano /etc/fstab
```

Verify that no swap remains active:

```bash
swapon --show
```

The command should produce no output.

### Load kernel modules and configure forwarding

```bash
sudo tee /etc/modules-load.d/k8s.conf >/dev/null <<'EOF'
overlay
br_netfilter
EOF

sudo modprobe overlay
sudo modprobe br_netfilter

sudo tee /etc/sysctl.d/99-kubernetes-cri.conf >/dev/null <<'EOF'
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF

sudo sysctl --system
```

Confirm forwarding is enabled:

```bash
sysctl net.ipv4.ip_forward
```

Expected value:

```text
net.ipv4.ip_forward = 1
```

## 3. Install and configure containerd and Docker

Install the runtime and local image builder:

```bash
sudo apt-get install -y containerd docker.io
sudo systemctl enable --now containerd
sudo systemctl enable --now docker
```

Generate a clean containerd configuration and select the systemd cgroup
driver. Kubelet and containerd must use compatible cgroup drivers.

```bash
sudo mkdir -p /etc/containerd
containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
sudo sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
sudo systemctl restart containerd
```

Verify the setting and services:

```bash
grep SystemdCgroup /etc/containerd/config.toml
systemctl is-active containerd docker
```

Optional: permit the current user to run Docker without `sudo`:

```bash
sudo usermod -aG docker "$USER"
```

Log out and back in before using Docker without `sudo`. The remaining commands
use `sudo docker` so this optional change is not required.

## 4. Install kubeadm, kubelet, and kubectl

Create the Kubernetes package keyring:

```bash
sudo mkdir -p -m 755 /etc/apt/keyrings
curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.37/deb/Release.key \
  | sudo gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
```

Add the Kubernetes v1.37 repository:

```bash
echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.37/deb/ /' \
  | sudo tee /etc/apt/sources.list.d/kubernetes.list
```

Install and hold all three Kubernetes packages:

```bash
sudo apt-get update
sudo apt-get install -y kubelet kubeadm kubectl
sudo apt-mark hold kubelet kubeadm kubectl
```

Check the installed tools:

```bash
kubeadm version
kubectl version --client
kubelet --version
```

Kubelet may restart repeatedly at this stage. That is expected until the node
has been initialized with `kubeadm`.

## 5. Initialize the single-node cluster

Initialize the control plane and assign the Calico pod CIDR:

```bash
sudo kubeadm init --pod-network-cidr=192.168.0.0/16
```

Keep the `kubeadm join` command printed at the end if worker nodes may be added
later.

Configure `kubectl` for the current non-root user:

```bash
mkdir -p "$HOME/.kube"
sudo cp /etc/kubernetes/admin.conf "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"
```

Verify access to the API server:

```bash
kubectl cluster-info
kubectl get nodes
```

The node will remain `NotReady` until a CNI network plugin is installed.

## 6. Install Calico networking

Install the Calico CRDs and operator:

```bash
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/v1_crd_projectcalico_org.yaml
kubectl create -f https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/tigera-operator.yaml
```

Download the default Calico custom resources:

```bash
curl -fsSLO https://raw.githubusercontent.com/projectcalico/calico/v3.32.2/manifests/custom-resources.yaml
```

The downloaded file defaults to `192.168.0.0/16`. If a different pod CIDR was
chosen in Step 5, edit `custom-resources.yaml` and set the Calico IP pool CIDR
to that same value.

Create the Calico installation:

```bash
kubectl create -f custom-resources.yaml
```

Wait for Calico and the node:

```bash
kubectl get pods -n calico-system --watch
```

Press `Ctrl+C` when the Calico pods are running, then check the node:

```bash
kubectl get nodes -o wide
```

The node should now be `Ready`.

### Allow workloads on the control-plane node

A kubeadm control-plane node is tainted to prevent ordinary applications from
being scheduled on it. Because this guide intentionally uses one host for both
roles, remove that taint:

```bash
kubectl taint nodes --all node-role.kubernetes.io/control-plane-
```

Do not remove the taint on a production control plane with dedicated worker
nodes.

## 7. Create local PostgreSQL storage

The application manifest requests a 1 GiB PVC, but a fresh kubeadm cluster does
not include a dynamic storage provisioner. Create a local persistent volume for
this single-node lab:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolume
metadata:
  name: vulnerableapp-postgres-local-pv
  labels:
    app.kubernetes.io/part-of: vulnerableapp
spec:
  capacity:
    storage: 1Gi
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  hostPath:
    path: /var/lib/vulnerableapp/postgres
    type: DirectoryOrCreate
EOF
```

This `hostPath` volume is suitable only for a single-host lab. Use a real CSI
storage provider for a multi-node or production cluster.

## 8. Configure application secrets

Move to the repository root:

```bash
cd /home/<user>/VulnerableApp_v2
```

Before deployment, edit both manifests:

```bash
nano k8s/10-postgres.yaml
nano k8s/20-app.yaml
```

Required changes:

1. Choose a new PostgreSQL password.
2. Set `POSTGRES_PASSWORD` in `k8s/10-postgres.yaml` to that password.
3. Set `DB_PASSWORD` in `k8s/20-app.yaml` to the identical password.

Assistant integrations:

- Set `OPENAI_API_KEY` to a valid key to enable normal assistant questions.
- Optionally set `OPENAI_BASE_URL` to a self-hosted OpenAI-compatible
  endpoint's base URL (e.g. `http://llm.internal:8000/v1`) and `OPENAI_MODEL`
  to that endpoint's model name. Leave both empty to use OpenAI's hosted API
  with `gpt-4o-mini`.
- Leave `CISCO_AI_DEFENSE_API_KEY` empty unless a valid Cisco AI Defense key is
  available. Cisco inspection is optional.
- Leave the Microsoft 365 values empty unless email integration is required and
  valid tenant credentials are available.

Never commit real credentials to source control. The values supplied with a
training copy of this repository must be treated as placeholders or compromised
credentials and replaced before use.

## 9. Build and import the application image

Build the application image on the Kubernetes host:

```bash
sudo docker build -t vulnerableapp:latest .
```

Kubernetes uses containerd directly, so an image in Docker's private image
store is not automatically visible to kubelet. Export it and import it into the
`k8s.io` containerd namespace:

```bash
sudo docker save vulnerableapp:latest -o /tmp/vulnerableapp-latest.tar
sudo ctr --namespace k8s.io images import /tmp/vulnerableapp-latest.tar
```

Confirm that containerd has the image:

```bash
sudo ctr --namespace k8s.io images list | grep vulnerableapp
```

The manifest's short image name is normalized by Kubernetes to
`docker.io/library/vulnerableapp:latest`. Ensure that exact containerd tag
exists, adding it when the import produced only a short or different source
tag:

```bash
if ! sudo ctr --namespace k8s.io images list -q \
  | grep -qx 'docker.io/library/vulnerableapp:latest'; then
  IMPORTED_IMAGE="$(sudo ctr --namespace k8s.io images list -q \
    | grep -E '(^|/)vulnerableapp:latest$' | head -n 1)"
  test -n "$IMPORTED_IMAGE"
  sudo ctr --namespace k8s.io images tag \
    "$IMPORTED_IMAGE" docker.io/library/vulnerableapp:latest
fi
```

The deployment uses `imagePullPolicy: IfNotPresent`, so kubelet will use this
local image.

Remove the temporary archive after confirming the import:

```bash
sudo rm -f /tmp/vulnerableapp-latest.tar
```

For a multi-node cluster, import the image on every node that may run the app,
or push the image to a registry and change the deployment image reference.

## 10. Deploy the application with kubectl

Apply the manifests in order:

```bash
kubectl apply -f k8s/00-namespaces.yaml
kubectl apply -f k8s/10-postgres.yaml
kubectl apply -f k8s/20-app.yaml
kubectl apply -f k8s/30-db-networkpolicy.yaml
```

Wait for PostgreSQL before waiting for the app:

```bash
kubectl -n vulnerableapp-db rollout status deployment/vulnerableapp-postgres --timeout=180s
kubectl -n vulnerableapp rollout status deployment/vulnerableapp --timeout=180s
```

Inspect the completed deployment:

```bash
kubectl -n vulnerableapp-db get pods,services,pvc
kubectl -n vulnerableapp get pods,services
kubectl -n vulnerableapp-db get networkpolicy
```

Expected results:

- One PostgreSQL pod is `Running` and ready.
- `vulnerableapp-postgres-data` is `Bound`.
- One application pod is `Running` and ready.
- Both services are `ClusterIP` services.
- `allow-app-to-postgres` exists in `vulnerableapp-db`.

## 11. Open and verify the application

### Access from the Ubuntu host only

```bash
kubectl -n vulnerableapp port-forward service/vulnerableapp 3000:80
```

Open:

```text
http://127.0.0.1:3000
```

### Access from another computer on the trusted LAN

Bind the port-forward to all IPv4 interfaces:

```bash
kubectl -n vulnerableapp port-forward --address 0.0.0.0 service/vulnerableapp 3000:80
```

If UFW is enabled, allow only the trusted LAN or a specific client. Example for
a `172.16.249.0/24` lab network:

```bash
sudo ufw allow from 172.16.249.0/24 to any port 3000 proto tcp
```

Find the Ubuntu host's address:

```bash
hostname -I
```

Then open:

```text
http://<ubuntu-host-ip>:3000
```

Keep the port-forward terminal open. A port-forward is temporary and must be
started again after it is stopped, after the selected app pod is replaced, or
after the host reboots.

Confirm that LAN access is bound correctly:

```bash
ss -ltnp 'sport = :3000'
curl --fail http://<ubuntu-host-ip>:3000/healthz
curl --fail http://<ubuntu-host-ip>:3000/readyz
```

The listener must show `0.0.0.0:3000`. A listener on `127.0.0.1:3000` supports
only local access, even when the application and Kubernetes Service are
otherwise healthy.

### Health and database tests

From another terminal:

```bash
curl --fail http://127.0.0.1:3000/healthz
curl --fail http://127.0.0.1:3000/readyz
curl --fail 'http://127.0.0.1:3000/api/clients?q=Alice'
```

Expected health responses:

```json
{"status":"ok"}
{"status":"ready"}
```

Confirm that the app is using the cross-namespace PostgreSQL service:

```bash
kubectl -n vulnerableapp exec deployment/vulnerableapp -- \
  sh -c 'printf "DB_HOST=%s\nDB_PORT=%s\n" "$DB_HOST" "$DB_PORT"'
```

Expected host and port:

```text
DB_HOST=vulnerableapp-postgres.vulnerableapp-db.svc.cluster.local
DB_PORT=5432
```

## 12. Rebuild after source changes

Use a unique tag so Kubernetes cannot reuse an older cached image:

```bash
cd /home/<user>/VulnerableApp_v2
APP_TAG="vulnerableapp:$(date +%Y%m%d%H%M%S)"
sudo docker build -t "$APP_TAG" .
sudo docker save "$APP_TAG" -o /tmp/vulnerableapp-update.tar
sudo ctr --namespace k8s.io images import /tmp/vulnerableapp-update.tar
kubectl -n vulnerableapp set image deployment/vulnerableapp "web=$APP_TAG"
kubectl -n vulnerableapp rollout status deployment/vulnerableapp --timeout=180s
sudo rm -f /tmp/vulnerableapp-update.tar
```

Restart the `kubectl port-forward` command after a rollout because the old
forward may still reference the replaced pod.

## 13. Reboot verification

After rebooting the Ubuntu host:

```bash
swapon --show
systemctl is-active containerd kubelet
kubectl get nodes
kubectl get pods -A
```

Swap must remain disabled, both services should be active, and the node should
return to `Ready`. Start the port-forward again to expose the web interface.

## 14. Troubleshooting

### Kubernetes API is unavailable

```bash
systemctl status kubelet containerd --no-pager
journalctl -u kubelet -n 100 --no-pager
swapon --show
```

If the log says that running with swap is unsupported, disable swap and fix
`/etc/fstab`, then restart kubelet:

```bash
sudo swapoff -a
sudo systemctl restart kubelet
```

### Node remains NotReady

```bash
kubectl get pods -n calico-system -o wide
kubectl describe node "$(hostname)"
```

Check that Calico is installed and that the Calico CIDR matches the CIDR passed
to `kubeadm init`.

### PostgreSQL pod remains Pending

```bash
kubectl -n vulnerableapp-db get pvc
kubectl -n vulnerableapp-db describe pvc vulnerableapp-postgres-data
kubectl get pv vulnerableapp-postgres-local-pv
```

A `no persistent volumes available` event means Step 7 was skipped or the PV is
already claimed.

### Application reports ImagePullBackOff

```bash
kubectl -n vulnerableapp describe pod -l app.kubernetes.io/name=vulnerableapp
sudo ctr --namespace k8s.io images list | grep vulnerableapp
```

Reimport the image into the `k8s.io` containerd namespace. Importing it into the
default containerd namespace is not sufficient.

### Application is not ready

```bash
kubectl -n vulnerableapp logs deployment/vulnerableapp --tail=200
kubectl -n vulnerableapp describe pod -l app.kubernetes.io/name=vulnerableapp
kubectl -n vulnerableapp-db logs deployment/vulnerableapp-postgres --tail=200
```

Confirm that the two database passwords match and that the PostgreSQL pod and
service are ready.

### Assistant execution fails

Check the returned API details and application log:

```bash
kubectl -n vulnerableapp logs deployment/vulnerableapp --tail=200
```

Normal assistant questions require a valid `OPENAI_API_KEY`. An invalid Cisco
key can return `401 Unauthorized`; leave `CISCO_AI_DEFENSE_API_KEY` empty when
Cisco inspection is not being used. After changing a Kubernetes Secret, restart
the deployment:

```bash
kubectl apply -f k8s/20-app.yaml
kubectl -n vulnerableapp rollout restart deployment/vulnerableapp
kubectl -n vulnerableapp rollout status deployment/vulnerableapp
```

### LAN address does not open

First confirm that Kubernetes itself is healthy:

```bash
kubectl get nodes
kubectl -n vulnerableapp get pods,service,endpoints -o wide
```

The node should be `Ready`, the application pod should be ready, and the
`vulnerableapp` endpoint should contain the app pod IP and port `3000`.

Next check the host listener and both access paths:

```bash
ss -ltnp 'sport = :3000'
curl --connect-timeout 3 http://127.0.0.1:3000/healthz
curl --connect-timeout 3 http://<ubuntu-host-ip>:3000/healthz
```

If localhost works but the host IP returns `Connection refused`, the
port-forward is normally bound only to localhost. The `ss` output will show
`127.0.0.1:3000` and possibly `[::1]:3000` instead of `0.0.0.0:3000`.

Stop the old port-forward with `Ctrl+C` in the terminal where it is running,
then start the LAN-accessible form:

```bash
kubectl -n vulnerableapp port-forward \
  --address 0.0.0.0 service/vulnerableapp 3000:80
```

If the original terminal cannot be found, identify the exact listener before
stopping it:

```bash
ss -ltnp 'sport = :3000'
ps -fp <pid-shown-by-ss>
kill <pid-shown-by-ss>
```

Only stop the process after confirming that it is the old `kubectl
port-forward` command. Start the corrected command afterward and verify:

```bash
ss -ltnp 'sport = :3000'
curl --fail http://<ubuntu-host-ip>:3000/healthz
curl --fail http://<ubuntu-host-ip>:3000/readyz
```

Expected responses are `{"status":"ok"}` and `{"status":"ready"}`. If the
listener shows `0.0.0.0:3000` and the host-IP tests work locally but another
computer still cannot connect, check UFW and any upstream network firewall.

## 15. Tetragon and Hubble Enterprise Splunk integration

Tetragon is a required privileged eBPF agent for this security-observability
setup. It runs once on every Kubernetes node and produces process, network, and
file-access security events. Splunk export is provided through Isovalent Hubble
Enterprise's Fluentd exporter and the Splunk HEC plugin.

The integration described here uses this data path:

```text
Tetragon agent DaemonSet
  -> Hubble Enterprise export-fluentd container
  -> fluent-plugin-splunk
  -> HTTPS HEC
  -> Splunk Enterprise or Splunk Cloud index
```

This approach does not expose the Tetragon gRPC API over the network. Do not
make the gRPC listener externally reachable unless it is protected with TLS or
mTLS and strict network controls.

### 15.1 Tetragon prerequisites

- The base Kubernetes application setup in this guide is working.
- The Ubuntu host has Linux kernel 4.19 or newer. A current Ubuntu LTS kernel is
  recommended because newer kernels expose more eBPF features.
- BTF is available at `/sys/kernel/btf/vmlinux`.
- Helm 3 is installed.
- The Kubernetes user has cluster-admin access.
- The cluster can pull images from `quay.io` and the Cilium Helm repository.
- Tetragon may run privileged and access the host kernel, PID, cgroup, and
  networking facilities.

Check the kernel and BTF file:

```bash
uname -r
test -r /sys/kernel/btf/vmlinux && echo "BTF is available"
```

If BTF is missing, do not continue until the host kernel supplies BTF or a
matching external BTF file has been configured. Tetragon cannot load its CO-RE
eBPF programs without suitable BTF information.

### 15.2 Install Helm 3

Download and inspect the official Helm installer before running it:

```bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
  -o /tmp/get-helm-3.sh
less /tmp/get-helm-3.sh
chmod 700 /tmp/get-helm-3.sh
sudo /tmp/get-helm-3.sh
rm -f /tmp/get-helm-3.sh
helm version
```

### 15.3 Configure and install the Tetragon agent

The repository contains `k8s/tetragon-values.yaml`. It enables Kubernetes API
awareness, policy filtering, JSON event export, and the gRPC service used by the
local `tetra` client.

Create a small additional values file that preserves the Tetragon pod
annotation used by this lab and keeps the JSON sidecar available for local
validation. Hubble Enterprise is the Splunk exporter configured later:

```bash
cat > tetragon-splunk-values.yaml <<'EOF'
podAnnotations:
  splunk.com/include: "true"
EOF
```

Add the Cilium chart repository and install Tetragon:

```bash
helm repo add cilium https://helm.cilium.io
helm repo update cilium

helm upgrade --install tetragon cilium/tetragon \
  --namespace kube-system \
  --values k8s/tetragon-values.yaml \
  --values tetragon-splunk-values.yaml
```

Wait for the agent DaemonSet and operator:

```bash
kubectl rollout status -n kube-system daemonset/tetragon --timeout=300s
kubectl get pods -n kube-system \
  -l app.kubernetes.io/part-of=tetragon -o wide
```

There should be one ready Tetragon agent pod on every node. On this single-node
cluster there will be one agent pod.

If the agent does not start, inspect it before applying policies:

```bash
kubectl -n kube-system describe pod \
  -l app.kubernetes.io/name=tetragon
kubectl -n kube-system logs daemonset/tetragon -c tetragon --tail=200
```

Errors mentioning BTF, kernel lockdown, unsupported helpers, or failed eBPF
program loads indicate a host-kernel compatibility or security-policy problem.

### 15.4 Apply the database tracing policy

The remaining policy covers PostgreSQL file activity and database-initiated
outbound TCP connections:

```bash
kubectl apply -f k8s/43-tetragon-db-file-tracingpolicy.yaml
```

It watches PostgreSQL data-directory activity and outbound TCP connections from
the database pod to an IP other than the application Service or pod IP.
PostgreSQL continuously accesses its data directory, so this policy can
generate a high event volume. The application pod IP is dynamic; update the
allowlist after an application pod replacement. Normal application-to-database
traffic is inbound to PostgreSQL and is not a database-initiated
`tcp_v4_connect` event. No workload restart is required after applying this
policy.

### 15.5 Verify Tetragon JSON events locally

The Helm chart's `export-stdout` sidecar streams Tetragon's JSON export into the
Kubernetes container log. Confirm that it is producing data:

```bash
kubectl logs -n kube-system \
  -l app.kubernetes.io/name=tetragon \
  -c export-stdout --tail=10
```

For compact, human-readable events, use the `tetra` client inside the agent:

```bash
kubectl exec -n kube-system daemonset/tetragon -c tetragon -- \
  tetra getevents -o compact --namespace vulnerableapp
```

Leave that command running and trigger application activity in another shell:

```bash
curl http://127.0.0.1:3000/readyz
curl 'http://127.0.0.1:3000/api/clients?q=Alice'
```

The repository policies focus on abnormal network and file behavior, so normal
database traffic might not produce a policy event. Process execution events
should still confirm that the base Tetragon agent is operating.

### 15.6 Splunk and Hubble Enterprise prerequisites

This integration sends Tetragon Runtime Security events to Splunk Enterprise or
Splunk Cloud Platform through Hubble Enterprise's `export-fluentd` container
and `fluent-plugin-splunk`. Before installing it, obtain:

- Access to the Isovalent Hubble Enterprise Helm repository and chart version
  `1.12.22`.
- A Splunk index for Tetragon events, such as `tetragon`.
- An enabled HEC token authorized to write to that index.
- The HEC host and port. Splunk Enterprise commonly uses port `8088`; Splunk
  Cloud supplies a stack-specific HEC host and port.
- DNS, routing, firewall, and TLS trust from every Kubernetes node to HEC.

Create the index first, then create or select an HEC token and authorize it for
that index. Do not put the token in this README, a values file, shell history,
or source control.

Compatibility note: this section follows the requested Hubble Enterprise
`1.12.22` guide and its `fluent-plugin-splunk` exporter. Splunk's plugin
repository now marks that plugin end-of-support, so verify that it is available
and supported by your Hubble Enterprise entitlement before production use.

Before installing Hubble Enterprise, check whether a release already exists:

```bash
helm list -n kube-system
kubectl get ds -n kube-system
```

This lab has a standalone Helm release named `tetragon`. Do not install a
second chart that manages the same Tetragon DaemonSet or CRDs. Render and
inspect the Hubble chart first; if it owns overlapping resources, stop and
choose a migration plan before removing or replacing the existing release.

### 15.7 Store the HEC token in Kubernetes

Keep the token in a Kubernetes Secret. The Hubble values file references the
Secret through an environment variable and therefore contains no token value:

```bash
read -rsp 'Splunk HEC token: ' SPLUNK_HEC_TOKEN
echo

kubectl -n kube-system create secret generic hubble-enterprise-splunk-hec \
  --from-literal=token="$SPLUNK_HEC_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

unset SPLUNK_HEC_TOKEN
```

Confirm that the token is non-empty without displaying it:

```bash
kubectl get secret -n kube-system hubble-enterprise-splunk-hec \
  -o jsonpath='{.data.token}' | base64 -d | wc -c
```

The result must be greater than `0`. After changing the Secret, restart Hubble
Enterprise so the exporter receives the new environment variable.

A Kubernetes Secret is base64-encoded, not encrypted by default; enable
encryption at rest for the Kubernetes API data store when required.

### 15.8 Configure Hubble Enterprise's Splunk HEC exporter

Create a local, untracked `hubble-enterprise-values.yaml` file. Replace the
host, port, and index placeholders. The `export.extraEnv` block exposes the
Secret to the Fluentd container; the interpolation keeps the token out of the
values file:

```yaml
enabled: true
exportDirectory: "/var/run/cilium/tetragon"

export:
  mode: fluentd
  extraEnv:
    - name: SPLUNK_HEC_TOKEN
      valueFrom:
        secretKeyRef:
          name: hubble-enterprise-splunk-hec
          key: token
  fluentd:
    output: |-
      @type splunk_hec
      host <SPLUNK_HOST>
      port <SPLUNK_PORT>
      token "#{ENV['SPLUNK_HEC_TOKEN']}"
      default_index <SPLUNK_INDEX>
      # Keep this true when the HEC endpoint uses HTTPS.
      use_ssl true
  filenames:
    - tetragon.log
```

The `host`, `port`, `token`, and `use_ssl` settings follow the supplied Hubble
Enterprise setup guide. Use `use_ssl false` only for a deliberately secured
HTTP lab endpoint. `exportDirectory` matches the existing standalone Tetragon
host mount in this lab. Ensure the Isovalent repository is configured, then inspect
the chart and rendered output before applying it:

```bash
helm repo add isovalent https://helm.isovalent.com
helm repo update isovalent
helm show values isovalent/hubble-enterprise --version 1.12.22 | less
helm template hubble-enterprise isovalent/hubble-enterprise \
  --version 1.12.22 --namespace kube-system \
  --values hubble-enterprise-values.yaml | less
```

Confirm that the rendered `export-fluentd` container receives
`SPLUNK_HEC_TOKEN` from the Secret and that the Fluentd output contains no
literal token before continuing.

### 15.9 Install or upgrade Hubble Enterprise

Install or upgrade the same Hubble Enterprise release. Add or update the
Isovalent chart repository using the repository URL supplied with your Hubble
Enterprise entitlement if it is not already configured:

```bash
helm repo add isovalent https://helm.isovalent.com
helm repo update isovalent

helm upgrade --install hubble-enterprise isovalent/hubble-enterprise \
  --version 1.12.22 \
  --namespace kube-system \
  --values hubble-enterprise-values.yaml \
  --wait
```

If Hubble Enterprise is already installed, the same command updates it. Do not
create a second release. If rendered resources overlap the existing standalone
`tetragon` release, stop before uninstalling anything and resolve ownership.

Restart the DaemonSet after configuration changes, then wait for the rollout:

```bash
kubectl rollout restart -n kube-system ds/hubble-enterprise
kubectl rollout status -n kube-system ds/hubble-enterprise --timeout=300s
kubectl get pods -n kube-system \
  -l app.kubernetes.io/instance=hubble-enterprise -o wide
```

### 15.10 Validate Fluentd and find events in Splunk

The Hubble guide's primary validation is the Fluentd container log:

```bash
kubectl logs -n kube-system ds/hubble-enterprise \
  -c export-fluentd --tail=200
```

The log should contain `fluentd worker is now running`. Also confirm the Secret
and DaemonSet exist without printing Secret contents:

```bash
kubectl get secret -n kube-system hubble-enterprise-splunk-hec
kubectl get ds -n kube-system hubble-enterprise
```

Generate fresh activity after the exporter is ready:

```bash
kubectl -n vulnerableapp exec deployment/vulnerableapp -- \
  sh -c 'id && uname -a'
curl 'http://127.0.0.1:3000/api/clients?q=Alice'
```

The guide's initial search is:

```spl
index="<SPLUNK_INDEX>" | head 10
```

First discover the sourcetype assigned by Hubble's direct HEC exporter:

```spl
index="<SPLUNK_INDEX>"
| stats count by sourcetype, source, host
```

Then narrow the search using the sourcetype returned above:

```spl
index="<SPLUNK_INDEX>" sourcetype="<OBSERVED_SOURCETYPE>"
| spath
```

Field availability depends on the enabled Tetragon policies and event type.
Retain the raw JSON event while developing Splunk field extractions.

### 15.11 Hubble Enterprise/Splunk troubleshooting

If local Tetragon events exist but Splunk is empty, check the pipeline from left
to right:

```bash
# Tetragon is ready.
kubectl get pods -n kube-system \
  -l app.kubernetes.io/name=tetragon

# JSON events exist in the export sidecar.
kubectl logs -n kube-system \
  -l app.kubernetes.io/name=tetragon \
  -c export-stdout --tail=20

# Hubble Enterprise and its Fluentd exporter are ready.
kubectl get ds -n kube-system hubble-enterprise
kubectl logs -n kube-system ds/hubble-enterprise \
  -c export-fluentd --tail=200

# Confirm the Secret exists without exposing its value.
kubectl get secret -n kube-system hubble-enterprise-splunk-hec
```

Common causes are an incorrect HEC host, port, token, index, TLS setting, or
certificate; unreachable HEC networking from a node; a token not authorized for
the selected index; a values key that did not render into `export-fluentd`; or
a Splunk search using the wrong index or time range. Check for duplicate
Tetragon exporters if both standalone Tetragon and Hubble Enterprise manage
export paths.

### 15.12 Rotate the HEC token

Create a replacement token in Splunk, update the Secret without printing the
value, and restart Hubble Enterprise:

```bash
read -rsp 'New Splunk HEC token: ' SPLUNK_HEC_TOKEN
echo

kubectl -n kube-system create secret generic hubble-enterprise-splunk-hec \
  --from-literal=token="$SPLUNK_HEC_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

unset SPLUNK_HEC_TOKEN
kubectl rollout restart -n kube-system ds/hubble-enterprise
kubectl rollout status -n kube-system ds/hubble-enterprise --timeout=300s
```

After events arrive with the replacement token, disable the old token in
Splunk.

### 15.13 Remove Tetragon and Hubble Enterprise

Remove the lab tracing policies first:

```bash
kubectl delete -f k8s/43-tetragon-db-file-tracingpolicy.yaml \
  --ignore-not-found
```

Then uninstall Hubble Enterprise and Tetragon. Only remove the standalone
`tetragon` release after confirming it is not still required by another Hubble
Enterprise deployment:

```bash
helm uninstall hubble-enterprise -n kube-system
helm uninstall tetragon -n kube-system
```

Deleting the local values files does not remove cluster resources by itself.
Do not delete the Splunk index until its retention and investigation
requirements have been satisfied.

## 16. Remove the application

Stop any active port-forward with `Ctrl+C`, then delete the application:

```bash
kubectl delete namespace vulnerableapp
kubectl delete namespace vulnerableapp-db
```

The PV uses the `Retain` reclaim policy, so PostgreSQL data remains on the host.
Delete it only when the lab data is no longer needed:

```bash
kubectl delete pv vulnerableapp-postgres-local-pv
sudo rm -rf /var/lib/vulnerableapp/postgres
```

The final command permanently deletes the PostgreSQL files.

To completely remove the Kubernetes cluster configuration from the host:

```bash
sudo kubeadm reset
```

Review the `kubeadm reset` output for CNI cleanup steps before reusing the host.

## Official references

- Kubernetes kubeadm installation:
  https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/
- Kubernetes container runtime configuration:
  https://kubernetes.io/docs/setup/production-environment/container-runtimes/
- Calico on-premises installation:
  https://docs.tigera.io/calico/latest/getting-started/kubernetes/self-managed-onprem/onpremises
- Calico single-host Kubernetes tutorial:
  https://docs.tigera.io/calico/latest/getting-started/kubernetes/k8s-single-node
- Tetragon Kubernetes installation:
  https://tetragon.io/docs/installation/kubernetes/
- Tetragon Helm chart values:
  https://tetragon.io/docs/reference/helm-chart/
- Tetragon kernel and BTF requirements:
  https://tetragon.io/docs/installation/faq/
- Isovalent Hubble Enterprise and Splunk export guide:
  https://www.splunk.com/en_us/blog/security/splunking-isovalent-data.html
- Splunk Fluentd HEC plugin:
  https://github.com/splunk/fluentd-hec
