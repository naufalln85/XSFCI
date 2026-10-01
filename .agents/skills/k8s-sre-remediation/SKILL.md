---
name: k8s-sre-remediation
description: Autonomous Kubernetes incident diagnosis and remediation skill for XFSCI SRE Agent. Activates when GNN detects cluster anomalies and root-cause candidates to inspect, remediate via safe scripts in sandbox, and verify recovery.
---

# Kubernetes SRE Self-Healing & Remediation Skill

This skill guides the autonomous investigation, diagnosis, and remediation of microservice anomalies in the XFSCI Kubernetes cluster.

## 1. Zero-Hallucination Diagnosis Workflow

When an alert is received, GNN (DualHeadGATv2) provides a mathematically computed **Root Cause Analysis (RCA)**:
- **Tersangka Utama (Primary Root Cause)**: Rank 1 candidate identified by GNN topology analysis.
- **Top-3 Candidates**: Ranked list of contributing microservices with their probability scores.
- **Fault Type**: Classified failure mode (`MEMORY_LEAK`, `CPU_OVERLOAD`, `POD_CRASH_LOOP`, `NETWORK_LATENCY`).

### Golden Rule:
**NEVER GUESS OR BLAME AN ARBITRARY POD.**
Always anchor your remediation on the GNN-identified **Primary Root Cause** service. If the symptom shows HTTP 500 in the frontend, but GNN identifies `redis-cart` as Root Cause (Rank 1, 94%), your remediation target is `redis-cart`, NOT frontend!

---

## 2. Investigation Protocol (Sandbox)

Use the helper script to inspect the target microservice:

```bash
./scripts/inspect_pod.sh <namespace> <deployment_name>
```

The script gathers:
1. Pod health status, restart counts, and OOMKilled events.
2. Recent error logs from containers.
3. Current replica count and resource constraints (limits/requests).

---

## 3. SOP Remediation Playbook

Execute the appropriate remediation based on the confirmed GNN fault type:

### Skenario A: Memory Leak (`MEMORY_LEAK`)
- **Action**: Scale-out followed by rolling restart.
- **Execution**:
  ```bash
  # Step 1: Scale out +2 replicas to absorb traffic during restart
  ./scripts/safe_scale.sh demo <deployment_name> +2
  # Step 2: Trigger rolling restart
  ./scripts/safe_rollout.sh demo <deployment_name>
  ```

### Skenario B: CPU Overload (`CPU_OVERLOAD`)
- **Action**: Scale out if request traffic is high, or rolling restart if process is hung/stuck.
- **Execution**:
  ```bash
  ./scripts/safe_scale.sh demo <deployment_name> +2
  ```

### Skenario C: Pod Crash Loop (`POD_CRASH_LOOP`)
- **Action**: Check exit code (OOM vs application crash). If OOM, scale out or trigger rollout restart to pick clean state.
- **Execution**:
  ```bash
  ./scripts/safe_rollout.sh demo <deployment_name>
  ```

### Skenario D: Network Latency / Bottleneck (`NETWORK_LATENCY`)
- **Action**: Rolling restart the network bottleneck service or scale out replicas.
- **Execution**:
  ```bash
  ./scripts/safe_rollout.sh demo <deployment_name>
  ```

---

## 4. Post-Remediation Verification

After applying any remediation command:
1. Wait for stabilization (30 seconds).
2. Run the verification script:
   ```bash
   ./scripts/verify_recovery.sh demo <deployment_name>
   ```
3. Ensure:
   - All pods in the deployment have `Ready: 1/1` (or N/N).
   - Pod restart delta is 0.
   - HTTP error rates return below SLA threshold (< 1%).
