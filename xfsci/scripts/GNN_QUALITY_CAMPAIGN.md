# XFSCI GNN quality campaign (Linux VM)

This campaign collects complete, separately labeled sessions in Kubernetes
namespace `demo`, trains with a session-level train/validation/test split, and
stops when one held-out candidate passes every configured quality gate or the
session limit is reached. The default is seven sessions because that produces
two independent held-out sessions with the current 70/15/15 split. One session
takes about 65 minutes plus preprocessing and training, so the default run can
take more than 7.5 hours.

## Before the run

1. Publish this code change to the branch used by the VM, then pull it with
   `git pull --ff-only`. A pull does not fetch uncommitted local changes.
2. Verify that the code reports `xfsci-gnn-23f-online-v5` and 23 features.
   Existing v4 data, scaler, and checkpoint are stale. The campaign keeps an
   archive, collects new raw sessions, then creates a new scaler and dataset.
   Do not reuse old cleaned/processed sessions or resume a v4 campaign.
3. Stop the XFSCI orchestrator and any other metrics scraper. The campaign
   checks for these processes and refuses to start if it finds one.
4. Confirm the current Kubernetes context is the intended test cluster and
   namespace `demo` may be disrupted. The existing network-latency injector
   uses a privileged host-network pod and changes the Surabaya node's traffic
   control rules; `--allow-node-network-fault` is an explicit acknowledgement.
5. Run inside `tmux` so SSH disconnects do not stop collection:

   ```bash
   tmux new -s xfsci-quality
   cd ~/xfsci/xfsci
   source ~/venv-xfsci/bin/activate
   export TARGET_NAMESPACE=demo
   export TZ=UTC
   kubectl config current-context
   ```

   Read the context name printed above, then use that exact value below. Check
   that the Prometheus and frontend URLs are reachable from this VM.

## Start the bounded campaign

Replace the context name and URLs with the values verified on the VM:

```bash
python3 scripts/run_gnn_quality_campaign.py \
  --expected-context 'YOUR_VERIFIED_CONTEXT' \
  --prometheus-url 'http://172.20.0.104:30090' \
  --frontend-url 'http://172.20.0.104:30080' \
  --confirm-demo \
  --confirm-agent-stopped \
  --allow-node-network-fault \
  --max-sessions 7
```

The runner makes at most 24 checkout transactions per session, waits 15
seconds between transactions, and varies the capture-to-fault delay between
sessions. It writes UTC fault markers and automatically labels each session
with its own `session_id`. The crash injector deletes five individually chosen
pods at three-minute intervals and records service, pod name, UID, node, time,
and Kubernetes event reason. CPU, memory, and latency labels require measured
telemetry changes on the injector's actual node. A fault with no observed
signal receives no fault labels; the held-out support gate then prevents an
unsupported model from being accepted. The runner will not use `--merge-all`
or synthetic samples.

The read-only preflight checks the v5 telemetry sources, including service
readiness and trace latency, verifies fresh complete samples for every current
workload pod, and refuses partial-replica snapshots without running GNN inference.
The scaler is fitted on complete service aggregates so its ranges match the
replica aggregation consumed by the model. It may see an old
processed feature contract before collection; it archives that artifact and
builds a new v5 scaler only after independent sessions have been collected.

The default quality gates require at least 20 held-out node samples for every
label, cluster anomaly accuracy of at least 99%, Top-3 RCA of at least 98%, and
fault macro-F1 of at least 80%. If the candidate fails, the runner quarantines
it and collects another independent session. It does not retry training on
unchanged data. The old runtime checkpoint is archived before new preprocessing
starts. A passing offline candidate is kept outside the runtime model path.

## Logs and results

The runner prints its artifact directory at startup. Full logs are stored in
`artifacts/gnn_quality_campaigns/<campaign-id>/`:

- `campaign.log`: progress, decisions, and final status.
- `preflight.log`: Kubernetes context, permissions, and node checks.
- `<session-id>/scraper.log`, `faults.log`, `traffic.log`, `labeler.log`, and
  `cleaner.log`: complete output for each step.
- `feature_engineer_after_*.log` and `attempt_*/training.log`: preprocessing
  and each training attempt.
- `campaign_summary.json`: session IDs, test support, thresholds, metrics, and
  rejection reasons.
- `accepted_candidate/`: a passing offline model and its metrics. It is kept
  out of `models/gnn/weights/gnn_best.pt`; review and separately approve any
  runtime activation.

Follow progress from another shell:

```bash
tail -F artifacts/gnn_quality_campaigns/<campaign-id>/campaign.log
```

`Ctrl-C` requests cleanup of the fault manifests and asks the scraper to save
its partial CSV. Review the fault log and cluster state after an interruption.
Exit code `0` means all gates passed; `2` means the session cap was reached
without a passing candidate; `1` means a collection or pipeline step failed.

These GNN gates do not measure LLM hallucination rate. The claimed `<5%`
hallucination target needs a separate, labeled diagnosis/action evaluation.
