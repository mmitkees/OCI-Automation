# OCI Networking Automation – Hub & Spoke with NGFW

This folder contains Python scripts to automate the provisioning and destruction of a Hub-and-Spoke network architecture in Oracle Cloud Infrastructure (OCI), featuring an **OCI Network Firewall (NGFW)** for centralized traffic inspection.

## Architecture Highlights
- **Hub VCN**: Centralized VCN (`VCN_hub`) containing the Network Firewall, Internet Gateway, and NAT Gateway.
- **Spoke VCNs**: One or more workload VCNs connected via a **Dynamic Routing Gateway (DRG)**.
- **Traffic Interception**: All internet-bound (`0.0.0.0/0`) traffic from spoke subnets is redirected to the Hub VCN and inspected by the NGFW before exiting via NAT/Internet gateways.
- **NGFW Policy**: Default "Allow All" security policy (`hub-ngfw-allow-all`) created automatically.

---

## 🚀 Creation Script (`create_hub_vcn.py`)

Provisions the entire Hub environment and wires spoke VCNs.

### Prerequisites
- Python 3.x
- OCI Python SDK (`pip install oci`)
- Configured OCI credentials (typically in `~/.oci/config`)

### Usage
```bash
python3 create_hub_vcn.py \
    --parent-compartment <PARENT_OCID> \
    --region <REGION_NAME> \
    --compartment-name <COMP_NAME_OR_V2> \
    --spoke-vcns <SPOKE_VCN_OCID_1> <SPOKE_VCN_OCID_2> \
    --yes
```

### Key Features
- **Flexible Naming**: Use `--compartment-name` to deploy to a new compartment (e.g., `hub_resources_v2`) if the old one is still in OCI's `DELETING` status.
- **Resilient Creation**: Handles `409 Conflict` errors by checking for `DELETING` compartments and providing clear retry instructions.
- **Summary Table**: Outputs a clear table of all created resources and OCIDs.
- **Dry-Run**: Use `--dry-run` to preview actions without making API calls.

---

## 🗑️ Destruction Script (`destroy_hub_vcn.py`)

Cleans up all resources created by the creation script in the correct dependency order.

### Usage
```bash
python3 destroy_hub_vcn.py \
    --parent-compartment <PARENT_OCID> \
    --region <REGION_NAME> \
    --compartment-name <COMP_NAME> \
    --spoke-vcns <SPOKE_VCN_OCID_1> \
    --yes
```

### Key Features
- **Custom Compartment**: Targets the specific `--compartment-name` used during creation.
- **Safe Undo**: Only removes `0.0.0.0/0` routes if they point to the DRG, preserving pre-existing internet access.
- **Dependency Handling**: Clears route rules before deleting gateways and waits for long deletions (NGFW).
- **Summary Table**: Lists every resource deleted or reverted during the run.

---

## 🔍 Verification
1. Run the creation script.
2. Once the Firewall is **ACTIVE**, verify connectivity from a spoke instance:
   ```bash
   curl -sI https://www.google.com
   ```
3. Check the NGFW metrics in the OCI Console to see traffic hits.
