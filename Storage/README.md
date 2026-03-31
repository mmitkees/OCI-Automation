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

Example 1: Interactive mode (shows prompts + compartment menu)

```bash
python3 Enable_Object_Storage_replication.py
```

Example 2: Silent mode (non-interactive)

```bash
python3 Enable_Object_Storage_replication.py --src me-abudhabi-1 --dest eu-zurich-1 --compartment <COMPARTMENT_OCID> --yes
```

#### Easy Run (from fresh clone)

From repository root:

```bash
git clone https://github.com/mmitkees/OCI-Automation.git
```

```bash
cd OCI-Automation
```

```bash
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

Example 1: Interactive mode

```bash
python3 enable_cross_region_replication_sdk.py
```

Example 2: Silent mode (non-interactive)

```bash
python3 enable_cross_region_replication_sdk.py --src me-abudhabi-1 --dest eu-zurich-1 --compartment <COMPARTMENT_OCID> --yes
```

---

## 📂 Troubleshooting

- **403 Forbidden**: Usually occurs right after a new IAM policy is created. The script includes a 10-second pause, but IAM changes can sometimes take 1-2 minutes to propagate fully across all OCI regions.
- **TenantCapacityExceeded**: OCI has a limit of 300 IAM policies per tenancy. If reached, the script will prompt you to use an existing policy using the `--policy` flag.
