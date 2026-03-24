# 🚀 OCI Automation Toolkit

Automating OCI Hub-and-Spoke Networking, Security, and Storage Replication for production-ready cloud environments.

## 🏢 Hub-and-Spoke Networking with Network Firewall (NGFW)

A production-ready Hub-and-Spoke architecture designed for **Zero Downtime**. This solution provisions an OCI Network Firewall to inspect all traffic moving between spoke VCNs and the internet/on-premises, without disrupting existing workloads during the long provisioning window.

### 🛠 Prerequisites

-   **OCI CLI & SDK**: Ensure you have the `oci` python package installed (`pip install oci`).
-   **Authentication**: Configure your OCI API keys or instance principal for authentication.
-   **IAM Permissions**: Ensure your user has the necessary permissions to manage Networking, Network Firewalls, and Compartments.

### 📍 Deployment Workflow

The deployment is split into two phases to ensure the Network Firewall is fully functional before any traffic is redirected.

1.  **Phase 1 (Provisioning)**: Creates the Hub infrastructure (Compartment, VCN, IGW, NAT, DRG, Firewall Policy, and Firewall Instance). **Important**: This step is non-disruptive.
    ```bash
    python3 Networking/create_hub_vcn.py \
        --parent-compartment <OCID> \
        --region <region> \
        --spoke-vcns <SPOKE_VCN_OCID1>,<SPOKE_VCN_OCID2>
    ```

2.  **Wait**: Wait for the Network Firewall to reach the **ACTIVE** state (approximately 20–40 minutes).

3.  **Phase 2 (Activation)**: Intercept traffic from spokes once the firewall is ready. This script updates DRG transit routing and Spoke VCN route tables to point to the firewall.
    ```bash
    python3 Networking/go_live_hubspoke.py \
        --hub-compartment <HUB_OCID> \
        --region <region> \
        --spoke-vcns <SPOKE_VCN_OCID1>,<SPOKE_VCN_OCID2>
    ```

### 📜 Key Scripts

-   **[create_hub_vcn.py](Networking/create_hub_vcn.py)**: Provisions the Hub infrastructure in a passive state.
-   **[go_live_hubspoke.py](Networking/go_live_hubspoke.py)**: Activates traffic interception and routing.
-   **[destroy_hub_vcn.py](Networking/destroy_hub_vcn.py)**: Safely decommissions the Hub and restores original spoke routing.

---

## ☁️ Object Storage Replication

Scripts for automating OCI Object Storage cross-region replication and the necessary IAM policy deployments are located in the `Storage/` directory. These are currently archived but functional for reference.

---

## 📂 Project Structure

```text
├── Networking/          # Hub-and-Spoke automation scripts
│   ├── README.md       # Detailed networking documentation
│   ├── create_hub_vcn.py
│   ├── go_live_hubspoke.py
│   └── destroy_hub_vcn.py
├── Storage/             # Object Storage replication scripts
│   ├── Enable_Object_Storage_replication.sh
│   └── enable_cross_region_replication_sdk.py
├── keys/                # Secure storage for API/SSH keys (Ignored by Git)
└── README.md            # This file
```

---

## 📄 Documentation

-   [Networking/README.md](Networking/README.md): Detailed Hub architecture, network flow diagrams, and advanced parameters.
