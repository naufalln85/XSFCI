#!/bin/bash
# ==============================================================================
# inspect_pod.sh - Safe cluster diagnostic probe
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"

echo "============================================================"
echo "🔍 Inspecting Deployment: ${DEPLOYMENT} in Namespace: ${NAMESPACE}"
echo "============================================================"

# Check deployment existence
if ! kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "❌ Deployment ${DEPLOYMENT} not found in namespace ${NAMESPACE}"
    exit 1
fi

echo "--- 1. Deployment Status ---"
kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o wide

echo ""
echo "--- 2. Pod List & Restarts ---"
kubectl get pods -n "${NAMESPACE}" -l "app=${DEPLOYMENT}" -o wide

echo ""
echo "--- 3. Recent Warning Events ---"
kubectl get events -n "${NAMESPACE}" --field-selector type=Warning --sort-by='.lastTimestamp' | tail -n 10 || true

echo ""
echo "--- 4. Container Logs (Last 25 lines) ---"
kubectl logs -n "${NAMESPACE}" -l "app=${DEPLOYMENT}" --tail=25 --all-containers=true || true

echo "============================================================"
echo "✅ Diagnostic probe completed."
