# Storage Replication Automation

This directory contains automation scripts for configuring cross-region replication in Oracle Cloud Infrastructure (OCI). The scripts are designed to simplify and automate disaster recovery setup for different storage layers.

## Installation

To get started, clone this repository to your local machine:

```bash
git clone https://github.com/mmitkees/OCI-Automation.git
cd OCI-Automation
```

## Scripts Overview

1. **`Enable_Object_Storage_replication.sh`**
   - **Type:** Bash Script (Uses OCI CLI)
   - **Purpose:** Automates cross-region replication for Object Storage buckets.
   - **Capabilities:**
     - Enforces bucket versioning prerequisites.
     - Automatically creates and configures destination buckets to be read-only (unversioned).
     - Automates IAM policy creation/updates.
     - Bypasses the 300-policy tenancy limit by appending to existing policies if necessary.
     - Supports automated confirmation for CI/CD pipelines.

2. **`enable_cross_region_replication_sdk.py`**
   - **Type:** Python Script (Uses Native OCI Python SDK)
   - **Purpose:** Automates cross-region replication for Block Volumes and Boot Volumes.
   - **Capabilities:**
     - Interacts natively with the OCI SDK for faster, programmable execution.
     - Iterates through availability domains for Boot Volumes.
     - Skips volumes that are already replicated to the target destination.

---

## 1. Object Storage Replication (`Enable_Object_Storage_replication.sh`)

### Prerequisites
- OCI CLI (`oci`) installed and configured.
- `jq` installed for JSON parsing.

### Parameters
| Parameter | Description | Required | Options/Example |
| :--- | :--- | :--- | :--- |
| `--src` | The source region OCID/name. | **Yes** | `me-abudhabi-1` |
| `--dest` | The destination region OCID/name. | **Yes** | `me-dubai-1` |
| `--compartment` | The OCID of the compartment containing the buckets to replicate. | No* | `ocid1.compartment.oc1..xxxx` |
| `--policy` / `--policy-name` | Explicit IAM policy name to use or update. Use this if your tenancy has reached the 300-policy cap. | No | `ObjectStorageReplicationServicePolicy` |
| `--yes` / `-y` | Bypasses the manual interactive confirmation prompt. | No | Flag |

*\*If `--compartment` is not provided, the script will launch an interactive menu allowing you to select from active compartments.*

### Usage Example
```bash
./Enable_Object_Storage_replication.sh --src me-abudhabi-1 --dest me-dubai-1 --compartment ocid1.compartment.oc1..example --yes
```

---

## 2. Block & Boot Volume Replication (`enable_cross_region_replication_sdk.py`)

### Prerequisites
- Python 3.x installed.
- Native OCI Python SDK installed (`pip install oci`).
- An active OCI config file (typically `~/.oci/config`).

### Parameters
| Parameter | Description | Required | Options/Example |
| :--- | :--- | :--- | :--- |
| `--src` | The source region containing the original volumes. | **Yes** | `me-abudhabi-1` |
| `--dest` | The destination region where volume replicas will be stored. | **Yes** | `me-dubai-1` |
| `--compartment` | The OCID of the compartment containing the volumes. | No* | `ocid1.compartment.oc1..xxxx` |
| `--yes` | Bypasses the manual interactive confirmation prompt. | No | Flag |

*\*If `--compartment` is not provided, the script will launch an interactive menu allowing you to select from active compartments.*

### Usage Example
```bash
python3 enable_cross_region_replication_sdk.py --src me-abudhabi-1 --dest me-dubai-1 --compartment ocid1.compartment.oc1..example --yes
```
