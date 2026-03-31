# 🚀 OCI Automation Toolkit

Automating OCI Hub-and-Spoke Networking, Security, and Storage Replication for production-ready cloud environments. This repository provides a unified framework for deploying scalable, secure, and disaster-recovery-ready infrastructure on Oracle Cloud Infrastructure.

---

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

-   **[Networking/create_hub_vcn.py](Networking/create_hub_vcn.py)**: Provisions the Hub infrastructure in a passive state.
-   **[Networking/go_live_hubspoke.py](Networking/go_live_hubspoke.py)**: Activates traffic interception and routing.
-   **[Networking/destroy_hub_vcn.py](Networking/destroy_hub_vcn.py)**: Safely decommissions the Hub and restores original spoke routing.

> [!NOTE]
> Review the [Networking/README.md](Networking/README.md) for detailed architecture and advanced configuration options.

---

## ☁️ Object Storage & Volume Replication

This module automates the setup of cross-region replication for disaster recovery. It handles both Block Store (Volume) and Object Store (Bucket) replication with automated IAM policy creation.

-   **Object Storage**: Automates bucket replication policies and required service permissions for cross-region data transfer.
-   **Block/Boot Volumes**: Uses the OCI SDK to enable native volume replication across regions.

### 📍 Quick Start

```bash
git clone https://github.com/mmitkees/OCI-Automation.git
```

```bash
cd OCI-Automation
```

```bash
python3 Storage/Enable_Object_Storage_replication.py
```

```bash
# Example: Silent mode
python3 Storage/Enable_Object_Storage_replication.py --src me-abudhabi-1 --dest eu-zurich-1 --compartment <OCID> --yes
```

```bash
# Example: Enable Block Volume replication via SDK
python3 Storage/enable_cross_region_replication_sdk.py --src me-abudhabi-1 --dest eu-zurich-1 --compartment <OCID> --yes
```

> [!TIP]
> Check out the [Storage/README.md](Storage/README.md) for full usage instructions, CLI flags, and SDK code examples.

---

## 📂 Project Structure

```text
├── Networking/          # Hub-and-Spoke automation scripts
│   ├── README.md       # Detailed networking documentation
│   ├── create_hub_vcn.py
│   ├── go_live_hubspoke.py
│   └── destroy_hub_vcn.py
├── Storage/             # Object Storage & Volume replication
│   ├── README.md       # Detailed storage documentation & API usage
│   ├── Enable_Object_Storage_replication.py
│   └── enable_cross_region_replication_sdk.py
├── keys/                # Secure storage for API/SSH keys (Ignored by Git)
└── README.md            # This file
```

---

## 📄 Documentation Index

-   **[Networking/README.md](Networking/README.md)**: Detailed Hub architecture, network flow diagrams, and advanced parameters.
-   **[Storage/README.md](Storage/README.md)**: Object Storage and Block Volume replication detailed guide.
