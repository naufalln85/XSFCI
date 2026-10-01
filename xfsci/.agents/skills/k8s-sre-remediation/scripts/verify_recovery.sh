#!/bin/bash
# ==============================================================================
# verify_recovery.sh - Post-remediation health validator
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"
WAIT_SECS="${3:-15}"

echo "============================================================"
echo "🩺 Verifying Recovery for: ${DEPLOYMENT} in ${NAMESPACE}"
echo "============================================================"

echo "Waiting ${WAIT_SECS}s for pods to settle..."
sleep "${WAIT_SECS}"

DESIRED_REPLICAS=$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')
READY_REPLICAS=$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}')

if [[ -z "${READY_REPLICAS}" ]]; then
    READY_REPLICAS=0
fi

echo "Desired Replicas: ${DESIRED_REPLICAS} | Ready Replicas: ${READY_REPLICAS}"

if [[ "${READY_REPLICAS}" -ge "${DESIRED_REPLICAS}" && "${READY_REPLICAS}" -gt 0 ]]; then
    echo "✅ HEALTHY: All pods for deployment ${DEPLOYMENT} are READY (${READY_REPLICAS}/${DESIRED_REPLICAS})."
    exit 0
else
    echo "⚠️ UNHEALTHY: Not all pods are ready yet (${READY_REPLICAS}/${DESIRED_REPLICAS})."
    exit 1
fi
