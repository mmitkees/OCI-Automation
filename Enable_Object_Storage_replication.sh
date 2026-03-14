#!/usr/bin/env bash

# Enable Object Storage cross-region replication

set -e

# Colors for output
HEADER='\033[95m'
OKBLUE='\033[94m'
OKCYAN='\033[96m'
OKGREEN='\033[92m'
WARNING='\033[93m'
FAIL='\033[91m'
ENDC='\033[0m'
BOLD='\033[1m'
IMPORTANT='\033[1m\033[91m'

# OCI CLI Options (Action commands will use this)
DEBUG_OPTS="--debug"

SRC_REGION=""
DEST_REGION=""
COMPARTMENT_ID=""
POLICY_NAME="ObjectStorageReplicationServicePolicy"
YES=0

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --src) SRC_REGION="$2"; shift ;;
        --dest) DEST_REGION="$2"; shift ;;
        --compartment) COMPARTMENT_ID="$2"; shift ;;
        --policy|--policy-name) POLICY_NAME="$2"; shift ;;
        --yes|-y) YES=1 ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$SRC_REGION" ]; then
    read -p "Enter source region (e.g., me-abudhabi-1): " SRC_REGION
fi

if [ -z "$DEST_REGION" ]; then
    read -p "Enter destination region (e.g., eu-zurich-1): " DEST_REGION
fi

if [ "$SRC_REGION" == "$DEST_REGION" ]; then
    echo "Source and destination must differ."
    exit 1
fi

set +e
TENANCY_OCID=$(oci iam tenancy get --query "data.id" --raw-output 2>/dev/null)
if [ -z "$TENANCY_OCID" ]; then
    TENANCY_OCID=$(grep -E "^tenancy=" ~/.oci/config | head -n 1 | cut -d'=' -f2 | tr -d ' ')
fi
set -e

if [ -z "$COMPARTMENT_ID" ]; then
    echo "Fetching compartments..."
    # Get all active compartments
    set +e
    COMP_DATA=$(oci iam compartment list --all --compartment-id "$TENANCY_OCID" --compartment-id-in-subtree true --query "data[?\"lifecycle-state\"=='ACTIVE'].{id:id,name:name}" 2>/dev/null)
    set -e
    
    if [ -z "$COMP_DATA" ] || [ "$COMP_DATA" == "[]" ]; then
        echo "No compartments available."
        exit 1
    fi
    
    # Extract arrays (macOS compatible without mapfile)
    declare -a comp_names=()
    while IFS= read -r line; do
        comp_names+=("$line")
    done < <(echo "$COMP_DATA" | jq -r '.[].name')
    
    declare -a comp_ids=()
    while IFS= read -r line; do
        comp_ids+=("$line")
    done < <(echo "$COMP_DATA" | jq -r '.[].id')
    
    for i in "${!comp_names[@]}"; do
        echo "$((i+1))) ${comp_names[$i]} | ${comp_ids[$i]}"
    done
    
    echo -n "Select compartment number or enter OCID: "
    read choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#comp_names[@]}" ]; then
        idx=$((choice-1))
        COMPARTMENT_ID="${comp_ids[$idx]}"
        echo "Selected compartment: ${comp_names[$idx]} ($COMPARTMENT_ID)"
    else
        COMPARTMENT_ID="$choice"
        echo "Selected compartment: $COMPARTMENT_ID"
    fi
fi

# Get child compartments
echo "Fetching child compartments..."
set +e
CHILD_DATA=$(oci iam compartment list --all --compartment-id "$TENANCY_OCID" --compartment-id-in-subtree true --query "data[?\"lifecycle-state\"=='ACTIVE'].{id:id,\"compartment-id\":\"compartment-id\"}" 2>/dev/null)
set -e

declare -a COMPARTMENTS_TO_PROCESS=("$COMPARTMENT_ID")

get_children() {
    local parent_id="$1"
    local children=$(echo "$CHILD_DATA" | jq -r ".[] | select(.\"compartment-id\" == \"$parent_id\") | .id")
    for child in $children; do
        COMPARTMENTS_TO_PROCESS+=("$child")
        get_children "$child"
    done
}

get_children "$COMPARTMENT_ID"

echo "Compartment(s) to process:"
for cid in "${COMPARTMENTS_TO_PROCESS[@]}"; do
    echo "  $cid"
done

echo "Fetching namespace for region $SRC_REGION..."
set +e
NAMESPACE=$(oci os ns get --region "$SRC_REGION" --query "data" --raw-output 2>/dev/null)
set -e

if [ -z "$NAMESPACE" ]; then
    echo "Failed to retrieve Object Storage Namespace. Check your credentials and region."
    exit 1
fi

# IAM Policy Automation
echo -e "\n${HEADER}${BOLD}--- IAM Policy Setup ---${ENDC}"
STMT1="Allow service objectstorage-${SRC_REGION} to manage object-family in tenancy"
STMT2="Allow service objectstorage-${DEST_REGION} to manage object-family in tenancy"


echo -e "${OKCYAN}Checking IAM Policy: $POLICY_NAME...${ENDC}"
set +e
EXISTING_POLICY_ID=$(oci iam policy list --compartment-id "$TENANCY_OCID" --name "$POLICY_NAME" --query "data[0].id" --raw-output 2>/dev/null)

if [ -z "$EXISTING_POLICY_ID" ] || [ "$EXISTING_POLICY_ID" == "None" ]; then
    echo -e "${WARNING}Policy '$POLICY_NAME' not found. Attempting to create...${ENDC}"
    echo -e "${IMPORTANT}--- API Call ---"
    echo "oci $DEBUG_OPTS iam policy create \\"
    echo "    --compartment-id '$TENANCY_OCID' \\"
    echo "    --name '$POLICY_NAME' \\"
    echo "    --statements '[\"$STMT1\", \"$STMT2\", \"$STMT3\"]'"
    echo -e "----------------${ENDC}"
    
    output=$(oci $DEBUG_OPTS iam policy create \
        --compartment-id "$TENANCY_OCID" \
        --name "$POLICY_NAME" \
        --description "Automated policy for cross-region object storage replication" \
        --statements "[\"$STMT1\", \"$STMT2\", \"$STMT3\"]" 2>&1)
    status=$?
    
    if [ $status -eq 0 ]; then
        echo -e "${OKGREEN}✔ Successfully created IAM policy '$POLICY_NAME'${ENDC}"
        # printf "${OKCYAN}--- API Response ---\n%s\n--------------------${ENDC}\n" "$output"
    else
        echo -e "${FAIL}✖ [ERROR] Failed to create IAM policy:${ENDC}"
        printf "%s\n" "$output"
        
        if echo "$output" | grep -q "TenantCapacityExceeded"; then
            echo -e "\n${FAIL}${BOLD}CRITICAL LIMIT REACHED:${ENDC}"
            echo -e "${WARNING}Your tenancy has reached the maximum of 300 IAM policies.${ENDC}"
            echo -e "${OKCYAN}To fix this, either:${ENDC}"
            echo -e "  1. Use an existing policy: ${BOLD}--policy <EXISTING_POLICY_NAME>${ENDC}"
            echo -e "  2. Delete an unused policy in your tenancy.${ENDC}"
            exit 1
        fi
        
        echo -e "${FAIL}⚠ Ensure your user has 'manage policies' permissions at the root tenancy level.${ENDC}"
    fi
else
    echo -e "${OKGREEN}✔ IAM policy '$POLICY_NAME' already exists. Updating statements...${ENDC}"
    echo -e "${IMPORTANT}--- API Call ---"
    echo "oci $DEBUG_OPTS iam policy update --policy-id '$EXISTING_POLICY_ID' --statements '[\"$STMT1\", \"$STMT2\", \"$STMT3\"]' --version-date ''"
    echo -e "----------------${ENDC}"
    
    output=$(oci $DEBUG_OPTS iam policy update \
        --policy-id "$EXISTING_POLICY_ID" \
        --statements "[\"$STMT1\", \"$STMT2\", \"$STMT3\"]" \
        --version-date "" \
        --force 2>&1)
    status=$?
    
    if [ $status -eq 0 ]; then
        echo -e "${OKGREEN}✔ Successfully updated IAM policy.${ENDC}"
        # printf "${OKCYAN}--- API Response ---\n%s\n--------------------${ENDC}\n" "$output"
    else
        echo -e "${FAIL}✖ [ERROR] Failed to update IAM policy:${ENDC}"
        printf "%s\n" "$output"
    fi
fi
set -e

# Store buckets with their compartment IDs
declare -a BUCKET_NAMES=()
declare -a BUCKET_COMPARTMENTS=()

echo "Scanning for buckets in source region $SRC_REGION..."
set +e
for cid in "${COMPARTMENTS_TO_PROCESS[@]}"; do
    buckets=$(oci os bucket list --compartment-id "$cid" --namespace-name "$NAMESPACE" --region "$SRC_REGION" --all --query "data[*].name" 2>/dev/null | jq -r '.[]?' 2>/dev/null)
    for b in $buckets; do
        BUCKET_NAMES+=("$b")
        BUCKET_COMPARTMENTS+=("$cid")
    done
done
set -e

if [ ${#BUCKET_NAMES[@]} -eq 0 ]; then
    echo "No available buckets to replicate in the given compartment(s)."
    exit 0
fi

echo -e "\n${HEADER}${BOLD}--- Bucket preview (source region: $SRC_REGION) ---${ENDC}"
echo -e "${OKCYAN}COMPARTMENT_OCID\tNAME${ENDC}"
for i in "${!BUCKET_NAMES[@]}"; do
    echo -e "${BUCKET_COMPARTMENTS[$i]}\t${OKBLUE}${BUCKET_NAMES[$i]}${ENDC}"
done

if [ "$YES" -eq 0 ]; then
    echo -ne "${WARNING}${BOLD}Proceed with replication? (y/n): ${ENDC}"
    read confirm
    confirm=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
    if [[ "$confirm" != "y" ]]; then
        echo -e "${FAIL}Aborted by user.${ENDC}"
        exit 0
    fi
fi

# Give IAM policy a moment to propagate
echo -e "${OKCYAN}Pausing for 10 seconds to allow IAM policy propagation...${ENDC}"
sleep 10

for i in "${!BUCKET_NAMES[@]}"; do
    b="${BUCKET_NAMES[$i]}"
    cid="${BUCKET_COMPARTMENTS[$i]}"
    POLICY_B_NAME="ReplicationTo-${DEST_REGION}"
    echo -e "\n${BOLD}${OKBLUE}▶ Executing: Setting up Object Storage replication for bucket '$b' to $DEST_REGION${ENDC}"
    
    # 1. Ensure Object Versioning is enabled on Source
    echo -e "${OKCYAN}Ensuring versioning is enabled on source bucket '$b' in compartment $cid...${ENDC}"
    set +e
    oci $DEBUG_OPTS os bucket update --namespace-name "$NAMESPACE" --bucket-name "$b" --versioning Enabled --region "$SRC_REGION" >/dev/null 2>&1
    set -e
    
    # 2. Ensure Destination Bucket exists in the SAME compartment (Versioning must NOT be enabled per OCI docs)
    echo -e "${OKCYAN}Ensuring destination bucket '$b' exists in $DEST_REGION (compartment $cid)...${ENDC}"
    set +e
    # Try to get it first
    oci os bucket get --namespace-name "$NAMESPACE" --bucket-name "$b" --region "$DEST_REGION" >/dev/null 2>&1
    if [ $? -ne 0 ]; then
        # Create it if it doesn't exist (versioning disabled by default)
        oci $DEBUG_OPTS os bucket create --namespace-name "$NAMESPACE" --name "$b" --compartment-id "$cid" --region "$DEST_REGION" >/dev/null 2>&1
    else
        # If it exists, ensure versioning is Suspended if it was previously enabled
        oci $DEBUG_OPTS os bucket update --namespace-name "$NAMESPACE" --bucket-name "$b" --versioning Suspended --region "$DEST_REGION" >/dev/null 2>&1
    fi
    set -e
    
    # Short sleep for bucket creation/update propagation
    sleep 2

    echo -e "${WARNING}--- API Call ---"
    echo "oci os replication create-replication-policy \\"
    echo "    --namespace-name '$NAMESPACE' \\"
    echo "    --bucket-name '$b' \\"
    echo "    --destination-region '$DEST_REGION' \\"
    echo "    --destination-bucket '$b' \\"
    echo "    --name '$POLICY_B_NAME' \\"
    echo "    --region '$SRC_REGION'"
    echo -e "----------------${ENDC}"

    set +e
    output=$(oci $DEBUG_OPTS os replication create-replication-policy \
        --namespace-name "$NAMESPACE" \
        --bucket-name "$b" \
        --destination-region "$DEST_REGION" \
        --destination-bucket "$b" \
        --name "$POLICY_B_NAME" \
        --region "$SRC_REGION" 2>&1)
    exit_code=$?
    
    if [ $exit_code -eq 0 ]; then
        echo -e "${OKGREEN}✔ [Bucket] Successfully created replication policy for $b${ENDC}"
    else
        echo -e "${FAIL}✖ [ERROR] Failed to create replication policy for $b:${ENDC}"
        printf "%s\n" "$output"
        
        if echo "$output" | grep -q "403"; then
            echo -e "${WARNING}Hint: Replication might take a few minutes for new IAM policies to fully propagate.${ENDC}"
        fi
    fi
done
