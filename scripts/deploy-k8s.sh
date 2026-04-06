#!/usr/bin/env bash
# Deploy unified-brain to RONE K8s cluster.
# Usage: ./scripts/deploy-k8s.sh [--build] [--namespace NAME]
#
# Prerequisites:
#   - kubectl configured for RONE cluster
#   - Docker (or buildah) for --build
#   - RONE registry access for image push

set -euo pipefail
cd "$(dirname "$0")/.."

NAMESPACE="${NAMESPACE:-hackathon-teams-poller}"
REGISTRY="${REGISTRY:-}"
IMAGE_NAME="unified-brain"
IMAGE_TAG="${IMAGE_TAG:-latest}"
BUILD=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --build)    BUILD=true; shift ;;
        --namespace) NAMESPACE="$2"; shift 2 ;;
        --registry) REGISTRY="$2"; shift 2 ;;
        --tag)      IMAGE_TAG="$2"; shift 2 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

IMAGE="${IMAGE_NAME}:${IMAGE_TAG}"
if [[ -n "$REGISTRY" ]]; then
    IMAGE="${REGISTRY}/${IMAGE}"
fi

echo "=== unified-brain K8s deploy ==="
echo "Namespace: $NAMESPACE"
echo "Image:     $IMAGE"

# Build and push if requested
if $BUILD; then
    echo "--- Building Docker image ---"
    docker build -t "$IMAGE" .
    if [[ -n "$REGISTRY" ]]; then
        echo "--- Pushing to registry ---"
        docker push "$IMAGE"
    fi
fi

# Apply manifests
echo "--- Applying K8s manifests ---"
kubectl apply -k k8s/ -n "$NAMESPACE"

# Patch image if using a registry
if [[ -n "$REGISTRY" ]]; then
    kubectl set image deployment/unified-brain \
        unified-brain="$IMAGE" -n "$NAMESPACE"
fi

# Wait for rollout
echo "--- Waiting for rollout ---"
kubectl rollout status deployment/unified-brain -n "$NAMESPACE" --timeout=120s

# Verify
echo "--- Verifying ---"
kubectl get pods -l app=unified-brain -n "$NAMESPACE"
echo ""
echo "Health check:"
kubectl exec -n "$NAMESPACE" \
    "$(kubectl get pod -l app=unified-brain -n "$NAMESPACE" -o jsonpath='{.items[0].metadata.name}')" \
    -- wget -qO- http://localhost:8790/healthz 2>/dev/null || echo "(waiting for startup)"

echo ""
echo "=== Deploy complete ==="
