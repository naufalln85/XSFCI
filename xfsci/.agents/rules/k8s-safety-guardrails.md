# Kubernetes SRE Safety Guardrails for Antigravity Agent

These rules govern the autonomous execution of the Antigravity Agent when operating on the Kubernetes cluster. These guardrails are enforced on all reasoning, tool calls, and automated actions.

---

## 1. Scope & Namespace Isolation
- **PERMITTED WORKLOAD NAMESPACES**: The agent is ONLY permitted to modify resources in the `demo` and `xfsci-system` namespaces.
- **ABSOLUTE FORBIDDEN NAMESPACES**: The agent MUST NEVER delete, restart, scale, or modify any resource in:
  - `kube-system`
  - `kube-public`
  - `kube-node-lease`
  - `monitoring` (Prometheus, Grafana, Loki)
- Any attempt to target a resource in forbidden namespaces must be immediately aborted and escalated.

---

## 2. Resource & Scaling Limits
- **Replica Bounds**:
  - Maximum replicas for any microservice deployment: **10**.
  - Minimum replicas for any microservice deployment: **1**.
- **Scale Delta Limits**:
  - Scale out: Maximum **+3** replicas per single remediation step.
  - Scale in: Maximum **-2** replicas per single remediation step.
- Never scale beyond cluster node resource capacity.

---

## 3. Non-Destructive Operation Policy
- **No Resource Deletions**: The agent MUST NEVER run:
  - `kubectl delete namespace ...`
  - `kubectl delete deployment ...`
  - `kubectl delete service ...`
  - `kubectl delete statefulset ...`
- **Safe Restart Policy**: Pod restarts MUST ALWAYS be performed as a rolling restart:
  - Use: `kubectl rollout restart deployment/<target> -n <namespace>`
  - Never execute bulk pod deletions (`kubectl delete pods --all`).
- **Scale Before Restart**: When addressing critical memory leaks or CPU exhaustion, ALWAYS scale out first (+1 to +2 replicas) before initiating a rolling restart, ensuring zero downtime for end-users.

---

## 4. Anti-Flapping & Cooldown Enforcements
- **Anti-Flapping Interval**: Never trigger a remediation action on the same deployment within **180 seconds (3 minutes)** of the previous action.
- Pods require bootstrap and warm-up time. Always allow stabilization before taking secondary actions.
- If a deployment remains unhealthy after **2 consecutive remediation attempts**, CEASE autonomous action and issue an **ESCALATE** alert to human SRE operators.

---

## 5. Terminal Sandbox Policy
- All terminal operations MUST run within the Antigravity Sandbox environment.
- Only whitelisted CLI binaries are allowed:
  - `kubectl` (read commands + rollout restart + scale)
  - `curl` (health probes)
  - `cat`, `grep`, `awk`, `head`, `tail` (log inspection)
- Destructive system commands (`rm -rf`, `mkfs`, `fdisk`, `dd`, `shutdown`, `reboot`) are strictly forbidden and blocked by sandbox policy.
