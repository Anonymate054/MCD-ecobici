#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — One-time setup: AWS profile + Terraform backend S3 bucket
# =============================================================================
# Run this ONCE before the first `terraform apply`.
# Prerequisites:
#   1. An IAM user with programmatic access keys (Access Key ID + Secret).
#      The console username/password in access_aws.txt are for the AWS web
#      console only. To get programmatic keys:
#        → Log in to: https://console.aws.amazon.com
#        → IAM → Users → ecobici-de-01 → Security credentials
#        → Create access key → Download CSV
#   2. AWS CLI v2 installed (already confirmed ✅)
#   3. Terraform >= 1.7 in PATH (installed at ~/.local/bin/terraform ✅)
#
# Usage:
#   chmod +x bootstrap.sh
#   ./bootstrap.sh
# =============================================================================

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

PROFILE="ecobici-de-01"
REGION="us-east-1"
PROJECT_PREFIX="ecobici"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Ecobici Data Lake — Bootstrap Setup                ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: AWS CLI profile ───────────────────────────────────────────────────
echo "▶ Step 1/4: Configure AWS CLI profile '$PROFILE'"
echo "  (You need your IAM Access Key ID and Secret Access Key)"
echo "  Get them from: AWS Console → IAM → Users → ecobici-de-01 → Security credentials"
echo ""

if aws sts get-caller-identity --profile "$PROFILE" &>/dev/null; then
    echo "  ✅ Profile '$PROFILE' already configured."
    ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
else
    aws configure --profile "$PROFILE"
    ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
    echo "  ✅ Profile configured. Account ID: $ACCOUNT_ID"
fi

# ── Step 2: Create tfvars ─────────────────────────────────────────────────────
echo ""
echo "▶ Step 2/4: Create terraform.tfvars"

BUCKET_NAME="${PROJECT_PREFIX}-datalake-${ACCOUNT_ID}"
TFVARS_FILE="terraform/terraform.tfvars"

if [[ -f "$TFVARS_FILE" ]]; then
    echo "  ✅ $TFVARS_FILE already exists — skipping."
else
    read -rp "  Alert email for SNS notifications: " ALERT_EMAIL
    cat > "$TFVARS_FILE" <<EOF
aws_region     = "${REGION}"
project_prefix = "${PROJECT_PREFIX}"
s3_bucket_name = "${BUCKET_NAME}"
alert_email    = "${ALERT_EMAIL}"
EOF
    echo "  ✅ Created $TFVARS_FILE"
fi

# ── Step 3: Bootstrap Terraform backend (S3 + DynamoDB) ──────────────────────
echo ""
echo "▶ Step 3/4: Bootstrap Terraform remote state backend"
echo "  Bucket : $BUCKET_NAME"
echo "  Table  : ${PROJECT_PREFIX}-tf-locks"
echo ""

# Create the bucket (used for both data lake AND state — same bucket, different prefix)
if aws s3api head-bucket --bucket "$BUCKET_NAME" --profile "$PROFILE" 2>/dev/null; then
    echo "  ✅ S3 bucket '$BUCKET_NAME' already exists."
else
    echo "  Creating S3 bucket '$BUCKET_NAME' in $REGION..."
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket \
            --bucket "$BUCKET_NAME" \
            --region "$REGION" \
            --profile "$PROFILE"
    else
        aws s3api create-bucket \
            --bucket "$BUCKET_NAME" \
            --region "$REGION" \
            --create-bucket-configuration LocationConstraint="$REGION" \
            --profile "$PROFILE"
    fi

    # Enable versioning (required for Terraform state)
    aws s3api put-bucket-versioning \
        --bucket "$BUCKET_NAME" \
        --versioning-configuration Status=Enabled \
        --profile "$PROFILE"

    # Block all public access
    aws s3api put-public-access-block \
        --bucket "$BUCKET_NAME" \
        --public-access-block-configuration \
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
        --profile "$PROFILE"

    echo "  ✅ S3 bucket created and secured."
fi

# Create DynamoDB table for state locking
LOCK_TABLE="${PROJECT_PREFIX}-tf-locks"
if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$REGION" --profile "$PROFILE" &>/dev/null; then
    echo "  ✅ DynamoDB lock table '$LOCK_TABLE' already exists."
else
    echo "  Creating DynamoDB lock table '$LOCK_TABLE'..."
    aws dynamodb create-table \
        --table-name "$LOCK_TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --region "$REGION" \
        --profile "$PROFILE"
    echo "  ✅ DynamoDB lock table created."
fi

# ── Step 4: Terraform init with real backend ──────────────────────────────────
echo ""
echo "▶ Step 4/4: Initialize Terraform with remote backend"
cd terraform

# Export credentials for terraform
export AWS_PROFILE="$PROFILE"
export AWS_REGION="$REGION"

terraform init \
    -backend-config="bucket=${BUCKET_NAME}" \
    -backend-config="region=${REGION}" \
    -backend-config="dynamodb_table=${LOCK_TABLE}" \
    -reconfigure \
    -input=false

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Bootstrap complete!                              ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║   Next steps:                                        ║"
echo "║   1. Populate API secrets in Secrets Manager:        ║"
echo "║      aws secretsmanager put-secret-value \\           ║"
echo "║        --secret-id ecobici/gbfs_api \\                ║"
echo "║        --secret-string '{\"url\":\"...\",\"api_key\":\"...\"}'║"
echo "║                                                      ║"
echo "║   2. Run terraform plan:                             ║"
echo "║      cd terraform && terraform plan                  ║"
echo "║                                                      ║"
echo "║   3. Run terraform apply:                            ║"
echo "║      terraform apply                                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
