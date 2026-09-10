---
name: automated-install-vulnerableapp-kubernetes
description: Install or resume the VulnerableApp Kubernetes lab end to end from a local installation-config.yaml using a sudo-capable account, including Tetragon and Hubble Enterprise Splunk HEC export. Use when the user explicitly wants unattended execution; use install-vulnerableapp-kubernetes for the guided workflow.
---

# Automated VulnerableApp Kubernetes installation

This is the unattended counterpart to
`skills/install-vulnerableapp-kubernetes/SKILL.md`. Keep that guided skill and
its behavior unchanged. Treat the repository `installation_readme.md` as the
authoritative detailed installation guide and use the project `README.md` only
for orientation and workflow selection.
execute its phases in order, using the local `installation-config.yaml` for
host settings, application credentials, Tetragon allowlist values, and Splunk
HEC configuration.

## Required inputs

Before starting, require:

1. A Linux account named by `sudo.username` that can run `sudo`.
2. A completed, local `installation-config.yaml` copied from
   `installation-config.yaml.example` with mode `600`.
3. A deliberate choice of `cluster.allow_existing_cluster_resume` and
   `cluster.allow_cluster_reconfiguration`.

Never store a sudo password in YAML. If `sudo` prompts for a password, the user
must enter it locally; do not request or record it in chat. Never print,
serialize, or include API keys, database passwords, HEC tokens, or Secret data
in logs or the completion report.

## Configuration validation

Read and validate the entire config before changing the host or cluster:

- `schema_version` is `1`.
- The configured sudo account exists and passes `sudo -n -u root true`, or the
  user has been told that one local password prompt will be required.
- On a fresh cluster, use the README's default pod CIDR `192.168.0.0/16` only
  after checking that it does not overlap host, LAN, VPN, or existing routes.
  On an existing cluster, discover and preserve the cluster's actual pod CIDR.
- `db_password` is non-placeholder and is the same value used for the database
  and application Secret.
- `openai_api_key` is either a valid configured key or explicitly empty.
- `openai_base_url` and `openai_model`, if set, point at a reachable
  OpenAI-compatible endpoint and its model name; if empty, the assistant
  defaults to OpenAI's hosted API and `gpt-4o-mini`.
- `hec_token` is non-placeholder when Splunk is enabled. Do not echo it.
- HEC host, port, index, SSL mode, Hubble chart repository, and version are
  present.
- The config file is owned by the sudo account and has mode `600`.

If a required value is missing, mark the run `deferred-user-action` and stop
before modifying the host or cluster.

## Safety and resume behavior

- Preserve unrelated files, workloads, firewall rules, images, PVs, and
  namespaces.
- Never run `kubeadm reset`, delete namespaces/PVs, erase PostgreSQL data, or
  execute README removal commands during installation.
- Never reboot automatically.
- Resume healthy phases after read-only validation; do not reinstall healthy
  components merely because the run is automated.
- If another cluster, overlapping CIDR, claimed unrelated PV, unexplained port
  listener, or materially different runtime is found, stop as `failed` unless
  the config explicitly permits the specific non-destructive resume.
- `allow_cluster_reconfiguration` never authorizes cluster destruction,
  namespace deletion, PV deletion, or replacement of unrelated workloads.

## Execution order

Run the README phases sequentially and require each phase postcondition before
starting the next:

1. Host prerequisites and network plan.
2. containerd and Docker.
3. Kubernetes tools.
4. kubeadm initialization or validation.
5. Calico.
6. PostgreSQL local storage.
7. Application credentials.
8. Image build and containerd import.
9. PostgreSQL, application, and database NetworkPolicy deployment.
10. Application health, readiness, database lookup, UI, and assistant checks.
11. Requested host-only or trusted-LAN access.
12. Maintenance and reboot-readiness validation.
13. Tetragon and Hubble Enterprise Splunk integration.

Maintain a checklist with `phase`, `status`, `evidence`, and `next_action`.
Allowed statuses are `pending`, `running`, `passed`, `failed`,
`skipped-optional`, and `deferred-user-action`. Never have two phases marked
`running` and never continue after a required postcondition fails.

## Config-driven secret handling

Use the config values to create or update Kubernetes Secrets through stdin or
the Kubernetes API. Do not put them into tracked manifests, Helm values files,
shell command arguments, shell history, or reports.

Create application Secrets with the configured database and optional API keys,
then verify only key names and object existence. Treat repository credentials
as compromised until replaced by the config values.

`openai_base_url` and `openai_model` are not secrets; set them as
`OPENAI_BASE_URL`/`OPENAI_MODEL` in the application ConfigMap (leave empty to
keep the OpenAI defaults) rather than in the Secret.

Do not apply the tracked Secret objects from `k8s/10-postgres.yaml` or
`k8s/20-app.yaml` directly. Create temporary sanitized manifest copies with
their Secret documents removed, apply the non-Secret resources, and create the
database/application Secrets from `installation-config.yaml`. Delete the
temporary copies after deployment; never edit the guided workflow's tracked
manifests.

For Splunk, create `hubble-enterprise-splunk-hec` in `kube-system` with the
configured HEC token. Generate a temporary ignored
`hubble-enterprise-values.yaml` containing only the HEC host, port, index, SSL
setting, and a Secret-backed `SPLUNK_HEC_TOKEN` environment reference. Remove
the temporary values file after the Helm operation unless the user explicitly
requests it be retained.

## Tetragon and Hubble Enterprise

Follow README Phase 15. The only repository tracing policy is
`k8s/43-tetragon-db-file-tracingpolicy.yaml`; do not recreate the removed
`k8s/40-tetragon-db-tracingpolicy.yaml`.

Before installing Hubble Enterprise:

- Validate kernel and BTF.
- Validate the Tetragon DaemonSet, operator, JSON export, and local `tetra`
  events.
- Render Hubble Enterprise `1.12.22` and inspect resource names for overlap
  with the standalone `tetragon` Helm release.
- Discover the current application Service ClusterIP and pod IP, then set the
  database network policy's `NotSAddr` allowlist from those discovered values.
  Refresh the pod IP after an application rollout and reapply a temporary
  config-rendered copy of the policy. Never overwrite the tracked policy with
  environment-specific IPs.

Install or upgrade exactly one Hubble release:

```bash
helm upgrade --install hubble-enterprise isovalent/hubble-enterprise \
  --version 1.12.22 \
  --namespace kube-system \
  --values hubble-enterprise-values.yaml \
  --wait
```

Require the Hubble Enterprise DaemonSet and `export-fluentd` container to be
ready. Require Fluentd logs to contain `fluentd worker is now running` and to
contain no HEC authentication, TLS, queue, or export errors. Generate a fresh
Tetragon event and verify arrival in the configured Splunk index; a running pod
alone is not success.

## Failure and completion

On failure, capture only non-secret diagnostics, stop progression, and retry an
in-scope reversible repair once. After three failures for the same cause, stop
as `failed` and report the required user action.

The final report must include host and Kubernetes versions, node readiness,
application/database/PVC state, image tag and digest, health checks, access URL,
Tetragon status, Hubble/Fluentd status, and Splunk event-delivery status. Never
include credentials or Secret contents.
