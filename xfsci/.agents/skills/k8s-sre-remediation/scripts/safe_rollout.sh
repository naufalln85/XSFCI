#!/bin/bash
# ==============================================================================
# safe_rollout.sh - Non-destructive rolling restart with verification
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"

# Guardrail check: forbidden namespaces
if [[ "${NAMESPACE}" == "kube-system" || "${NAMESPACE}" == "monitoring" || "${NAMESPACE}" == "kube-public" ]]; then
    echo "❌ ERROR: Target namespace '${NAMESPACE}' is forbidden by SRE Guardrails!"
    exit 2
fi

echo "============================================================"
echo "🔄 Initiating Rolling Restart for: ${DEPLOYMENT} (${NAMESPACE})"
echo "============================================================"

# Trigger rollout restart
kubectl rollout restart deployment/"${DEPLOYMENT}" -n "${NAMESPACE}"

echo "⏳ Waiting for rollout status..."
if kubectl rollout status deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=120s; then
    echo "✅ Rolling restart succeeded for ${DEPLOYMENT}."
else
    echo "⚠️ Rollout status timed out or encountered issues."
    exit 1
fi
