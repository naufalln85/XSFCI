#!/bin/bash
# ==============================================================================
# safe_scale.sh - Scaler with bounded guardrails [1 to 10 replicas]
# ==============================================================================
set -e

NAMESPACE="${1:-demo}"
DEPLOYMENT="${2:-cartservice}"
SCALE_SPEC="${3:-+2}"

# Guardrail check: forbidden namespaces
if [[ "${NAMESPACE}" == "kube-system" || "${NAMESPACE}" == "monitoring" || "${NAMESPACE}" == "kube-public" ]]; then
    echo "❌ ERROR: Target namespace '${NAMESPACE}' is forbidden by SRE Guardrails!"
    exit 2
fi

CURRENT_REPLICAS=$(kubectl get deployment "${DEPLOYMENT}" -n "${NAMESPACE}" -o jsonpath='{.spec.replicas}')
if [[ -z "${CURRENT_REPLICAS}" ]]; then
    CURRENT_REPLICAS=1
fi

echo "Current replicas for ${DEPLOYMENT}: ${CURRENT_REPLICAS}"

if [[ "${SCALE_SPEC}" == +* ]]; then
    DELTA=${SCALE_SPEC#+}
    TARGET_REPLICAS=$(( CURRENT_REPLICAS + DELTA ))
elif [[ "${SCALE_SPEC}" == -* ]]; then
    DELTA=${SCALE_SPEC#-}
    TARGET_REPLICAS=$(( CURRENT_REPLICAS - DELTA ))
else
    TARGET_REPLICAS=${SCALE_SPEC}
fi

# Guardrail bounds
MAX_REPLICAS=10
MIN_REPLICAS=1

if (( TARGET_REPLICAS > MAX_REPLICAS )); then
    echo "⚠️ Target replicas (${TARGET_REPLICAS}) exceeds MAX (${MAX_REPLICAS}). Clamping to ${MAX_REPLICAS}."
    TARGET_REPLICAS=${MAX_REPLICAS}
fi

if (( TARGET_REPLICAS < MIN_REPLICAS )); then
    echo "⚠️ Target replicas (${TARGET_REPLICAS}) below MIN (${MIN_REPLICAS}). Clamping to ${MIN_REPLICAS}."
    TARGET_REPLICAS=${MIN_REPLICAS}
fi

echo "Scaling ${DEPLOYMENT} to ${TARGET_REPLICAS} replicas..."
kubectl scale deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --replicas="${TARGET_REPLICAS}"

echo "⏳ Waiting for scale stabilization..."
kubectl rollout status deployment/"${DEPLOYMENT}" -n "${NAMESPACE}" --timeout=90s
echo "✅ Scaled successfully to ${TARGET_REPLICAS} replicas."
