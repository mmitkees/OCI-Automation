# OCI Automation Toolkit

Automating OCI Hub-and-Spoke Networking, Security, and Storage Replication.

## 🏢 Hub-and-Spoke Networking with Network Firewall (NGFW)

A production-ready Hub-and-Spoke architecture designed for **Zero Downtime**.

### Deployment Workflow

1.  **Phase 1 (Provisioning)**: Creates the Hub infrastructure (Compartment, VCN, IGW, NAT, DRG, Firewall Policy, Firewall Instance). This is non-disruptive.
    ```bash
    python3 Networking/create_hub_vcn.py --parent-compartment <OCID> --region <region> --spoke-vcns <SPOKE_OCID>
    ```

2.  **Wait**: Wait for the Network Firewall to reach the **ACTIVE** state (approx. 20-40 min).

3.  **Phase 2 (Activation)**: Intercept traffic from spokes once the firewall is ready.
    ```bash
    python3 Networking/go_live_hubspoke.py --hub-compartment <HUB_OCID> --region <region> --spoke-vcns <SPOKE_OCID>
    ```

### Key Scripts

- [Networking/create_hub_vcn.py](Networking/create_hub_vcn.py): Provisions Hub (Passive).
- [Networking/go_live_hubspoke.py](Networking/go_live_hubspoke.py): Activates Interception (Active).
- [Networking/destroy_hub_vcn.py](Networking/destroy_hub_vcn.py): Destroys Hub and restores spoke routing.

---

## ☁️ Object Storage Replication (Retired/Archived)

Scripts for OCI Object Storage replication and IAM policy automation are located in the `Storage/` directory.

---

## 📄 Documentation

- [Networking/README.md](Networking/README.md): Detailed Hub architecture and parameters.
- [Networking/Manual_Steps.md](Networking/Manual_Steps.md): Manual verification guide.
