#!/usr/bin/env bash
# Deploy unified-brain to AWS EC2 spot instance via CloudFormation.
# Usage: ./scripts/deploy-ec2.sh --vpc-id VPC_ID --subnet-id SUBNET_ID
#
# Required env vars:
#   ANTHROPIC_API_KEY — for brain LLM backend
#   GITHUB_TOKEN     — for gh CLI / GitHub adapter
#
# Optional:
#   AWS_PROFILE (default: grobomo)
#   AWS_REGION  (default: us-east-1)

set -euo pipefail
cd "$(dirname "$0")/.."

STACK_NAME="unified-brain"
AWS_PROFILE="${AWS_PROFILE:-grobomo}"
AWS_REGION="${AWS_REGION:-us-east-1}"
INSTANCE_TYPE="t3.small"
VPC_ID=""
SUBNET_ID=""
KEY_NAME=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --vpc-id)        VPC_ID="$2"; shift 2 ;;
        --subnet-id)     SUBNET_ID="$2"; shift 2 ;;
        --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
        --key-name)      KEY_NAME="$2"; shift 2 ;;
        --stack-name)    STACK_NAME="$2"; shift 2 ;;
        --region)        AWS_REGION="$2"; shift 2 ;;
        --profile)       AWS_PROFILE="$2"; shift 2 ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

if [[ -z "$VPC_ID" || -z "$SUBNET_ID" ]]; then
    echo "Usage: $0 --vpc-id VPC_ID --subnet-id SUBNET_ID"
    exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" || -z "${GITHUB_TOKEN:-}" ]]; then
    echo "Error: ANTHROPIC_API_KEY and GITHUB_TOKEN must be set"
    exit 1
fi

echo "=== unified-brain EC2 deploy ==="
echo "Stack:    $STACK_NAME"
echo "Region:   $AWS_REGION"
echo "Instance: $INSTANCE_TYPE (spot)"
echo "VPC:      $VPC_ID"
echo "Subnet:   $SUBNET_ID"

aws cloudformation deploy \
    --template-file aws/cloudformation.yaml \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --capabilities CAPABILITY_NAMED_IAM \
    --parameter-overrides \
        InstanceType="$INSTANCE_TYPE" \
        VpcId="$VPC_ID" \
        SubnetId="$SUBNET_ID" \
        KeyName="$KEY_NAME" \
        AnthropicApiKey="$ANTHROPIC_API_KEY" \
        GitHubToken="$GITHUB_TOKEN" \
    --tags app=unified-brain

echo ""
echo "--- Stack outputs ---"
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --profile "$AWS_PROFILE" \
    --query 'Stacks[0].Outputs' \
    --output table

echo ""
echo "=== Deploy complete ==="
echo "View logs: aws logs tail /unified-brain --follow --region $AWS_REGION --profile $AWS_PROFILE"
