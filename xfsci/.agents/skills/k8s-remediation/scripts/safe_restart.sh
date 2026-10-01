#!/bin/bash
# ==============================================================================
# safe_restart.sh - Rolling restart aman di dalam sandbox
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"

# Guardrails check: Dilarang sentuh kube-system atau monitoring
if [[ "${NAMESPACE}" == "kube-system" || "${NAMESPACE}" == "monitoring" || "${NAMESPACE}" == "kube-public" ]]; then
    echo "❌ ERROR: Namespace '${NAMESPACE}' dilarang oleh SRE Guardrails!"
    exit 2
fi

echo "============================================================"
echo "🔄 Menjalankan Rolling Restart: ${DEPLOYMENT} (${NAMESPACE})"
echo "============================================================"

# Jalankan rollout restart bergulir
kubectl rollout restart deployment/"${DEPLOYMENT}" -n "${NAMESPACE}"

echo "⏳ Menunggu rollout selesai..."
if kubectl rollout status deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=120s; then
    echo "✅ Rolling restart berhasil diselesaikan untuk ${DEPLOYMENT}."
    exit 0
else
    echo "⚠️ Rollout status timed out atau mengalami kendala."
    exit 1
fi
