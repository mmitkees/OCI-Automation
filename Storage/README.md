# 📦 OCI Storage Automation

This directory contains Python scripts to automate cross-region replication for OCI Block Storage and Object Storage.

## 🚀 Overview

The scripts in this directory are designed to simplify the process of setting up disaster recovery (DR) for your storage resources. They handle both the resource configuration and the necessary IAM policy automation.

### 📋 Prerequisites

-   **OCI CLI**: Installed and configured (`oci setup config`).
-   **Python 3.10+**: With the `oci` package installed (`pip install oci`).
-   **Permissions**: Your user must have permissions to manage:
    -   Volumes/Boot Volumes in the source and destination regions.
    -   Object Storage buckets in the source and destination regions.
    -   IAM Policies at the root tenancy level (to automate cross-region service permissions).

---

## 🏗 Key Scripts

### 1. Object Storage Replication (`Enable_Object_Storage_replication.py`)

This script automates the creation of cross-region replication policies for Object Storage buckets. It ensures that any file uploaded to a source bucket is automatically copied to a destination bucket in a different region.

#### Features:
-   **Interactive Selection**: Lists your compartments and buckets for easy selection.
-   **IAM Policy Automation**: Automatically creates the mandatory service policies:
    ```bash
    Allow service objectstorage-me-abudhabi-1 to manage object-family in tenancy
    Allow service objectstorage-eu-zurich-1 to manage object-family in tenancy
    ```
-   **Bucket Setup**: Ensures the destination bucket exists and that versioning is correctly configured (Enabled on source, Suspended on destination).

#### Usage:
```bash
# Example 1: Interactive mode (shows prompts + compartment menu)
python3 Enable_Object_Storage_replication.py

# Example 2: Silent mode (non-interactive)
python3 Enable_Object_Storage_replication.py \
    --src me-abudhabi-1 \
    --dest eu-zurich-1 \
    --compartment <COMPARTMENT_OCID> \
    --yes
```

#### Easy Run (from fresh clone)

From repository root:

```bash
git clone https://github.com/mmitkees/OCI-Automation.git
cd OCI-Automation
python3 Storage/Enable_Object_Storage_replication.py
```

---

### 2. Block/Boot Volume Replication (`enable_cross_region_replication_sdk.py`)

This Python script uses the OCI SDK to enable native cross-region replication for storage volumes. Unlike Object Storage, volume replication occurs at the block level.

#### Features:
-   **Native SDK Implementation**: Uses `oci.core.BlockstorageClient` for reliable updates.
-   **Mass Update**: Can process all volumes within a compartment and its sub-compartments recursively.
-   **Status Awareness**: Skips volumes that already have active replicas to the target AD.

#### Usage:
```bash
# Interactive mode
python3 enable_cross_region_replication_sdk.py

# CLI mode
python3 enable_cross_region_replication_sdk.py \
    --src me-abudhabi-1 \
    --dest eu-zurich-1 \
    --compartment <COMPARTMENT_OCID> \
    --yes
```

---

## 🛠 API & SDK Implementation Details

### Object Storage API (Python SDK)
The script uses OCI Python SDK methods such as:
- `ObjectStorageClient.create_replication_policy`: To bind the source bucket to destination.
- `ObjectStorageClient.update_bucket`: To enforce source/destination versioning requirements.
- `IdentityClient.create_policy` / `IdentityClient.update_policy`: To manage service IAM policy statements.

### Volume Replication (Python SDK)
The script utilizes the `update_volume` and `update_boot_volume` methods:

```python
# Example for Block Volume using SDK
update_details = oci.core.models.UpdateVolumeDetails(
    block_volume_replicas=[
        oci.core.models.BlockVolumeReplicaDetails(
            availability_domain=dest_ad,
            display_name="MyReplica"
        )
    ]
)
response = core_client.update_volume(volume_id=vol_id, update_volume_details=update_details)
```

---

## 📂 Troubleshooting

- **403 Forbidden**: Usually occurs right after a new IAM policy is created. The script includes a 10-second pause, but IAM changes can sometimes take 1-2 minutes to propagate fully across all OCI regions.
- **TenantCapacityExceeded**: OCI has a limit of 300 IAM policies per tenancy. If reached, the script will prompt you to use an existing policy using the `--policy` flag.
