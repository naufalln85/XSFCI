#!/bin/bash
# ==============================================================================
# check_health.sh - Cek status kesehatan pod dan kesiapan replika
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"

echo "============================================================"
echo "🩺 Memeriksa Kesehatan Deployment: ${DEPLOYMENT} (${NAMESPACE})"
echo "============================================================"

# Cek keberadaan deployment
if ! kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" > /dev/null 2>&1; then
    echo "❌ Deployment ${DEPLOYMENT} tidak ditemukan di namespace ${NAMESPACE}"
    exit 1
fi

DESIRED=$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')
READY=$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.status.readyReplicas}')
READY=${READY:-0}

echo "Target Replika : ${DESIRED}"
echo "Ready Replika  : ${READY}"

echo ""
echo "--- Daftar Pod & Restart Count ---"
kubectl get pods -n "${NAMESPACE}" -l "app=${DEPLOYMENT}" -o wide

if [ "$READY" -ge "$DESIRED" ] && [ "$READY" -gt 0 ]; then
    echo ""
    echo "✅ STATUS SEHAT: Semua pod ${DEPLOYMENT} siap melayani trafik (${READY}/${DESIRED})."
    exit 0
else
    echo ""
    echo "⚠️ STATUS BELUM SEHAT: Kesiapan pod ${READY}/${DESIRED}."
    exit 1
fi
