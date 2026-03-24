# OCI Hub-and-Spoke Networking (Zero-Downtime)

This folder contains scripts to provision a Hub-and-Spoke network with an OCI Network Firewall (NGFW).

## Deployment Workflow

To avoid spoke downtime during the 40-minute firewall provisioning window, the deployment is split into two phases:

### Phase 1: Provisioning (Passive)
Run `create_hub_vcn.py` to build the infrastructure. This script creates the compartment, VCN, gateways, and the firewall instance, but **does not** redirect traffic.

```bash
python3 create_hub_vcn.py \
    --parent-compartment <OCID> \
    --region <region> \
    --hub-cidr 10.53.0.0/16 \
    --spoke-vcns <OCID_SPOKE> \
    --yes
```

### Phase 2: Activation (Active)
Run `go_live_hubspoke.py` once the firewall is **ACTIVE**. This script "flips the switch" by updating DRG transit routes and Spoke VCN route tables.

```bash
python3 go_live_hubspoke.py \
    --hub-compartment <HUB_OCID> \
    --region <region> \
    --spoke-vcns <OCID_SPOKE>
```

## Key Scripts

- [create_hub_vcn.py](create_hub_vcn.py): Provisions the Hub (Passive).
- [go_live_hubspoke.py](go_live_hubspoke.py): Activates 'Full Interception' (Active).
- [destroy_hub_vcn.py](destroy_hub_vcn.py): Safely cleans up all resources.

## Benefits of Two-Stage Deployment
1. **Zero Downtime**: Spoke VCNs keep their original internet behavior until the firewall is ready.
2. **Infrastructure Validation**: You can verify the Hub VCN and Firewall Policy before any traffic is routed.
3. **Safe Interception**: The enable script performs a health check on the firewall before updating routes.
