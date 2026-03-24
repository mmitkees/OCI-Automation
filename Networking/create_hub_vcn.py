#!/usr/bin/env python3
"""
create_hub_vcn.py
=================
Provisions an OCI Hub-and-Spoke network with a Network Firewall (NGFW).
PHASE 1: Infrastructure creation only (Passive). 
Does NOT update spoke routing to avoid downtime during firewall provisioning.

What this script creates
------------------------
1.  Compartment  "hub_resources"  under --parent-compartment
2.  VCN_hub
3.  Subnets inside VCN_hub (Firewall & Management)
4.  Internet Gateway, NAT Gateway
5.  OCI Network Firewall Policy  (allow-all rules)
6.  OCI Network Firewall  (in firewall-subnet)
7.  DRG  +  VCN_hub attachment (with Ingress Routing)
8.  Route tables for INTERNAL hub steering
9.  Basic Spoke-to-DRG attachments (without routing updates)

Usage
-----
python3 create_hub_vcn.py \
    --parent-compartment ocid1.compartment.oc1..xxx \
    --region me-abudhabi-1 \
    [--hub-cidr 10.53.0.0/16] \
    [--spoke-vcns ocid1.vcn.oc1..aaa] \
    [--no-wait]
"""

import argparse
import ipaddress
import sys
import time
import oci

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str, dry_run: bool = False) -> None:
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}{msg}", flush=True)

def _wait_for_state(client, get_fn, ocid: str, desired_states: list,
                    max_wait_secs: int = 3600, poll_interval: int = 30,
                    resource_label: str = "resource") -> None:
    elapsed = 0
    while elapsed < max_wait_secs:
        resource = get_fn(ocid).data
        state = resource.lifecycle_state
        print(f"  ⏳  {resource_label} state: {state} (elapsed {elapsed}s) …", end="\r", flush=True)
        if state in desired_states:
            print()
            return
        time.sleep(poll_interval)
        elapsed += poll_interval
    raise TimeoutError(f"{resource_label} did not reach {desired_states} within {max_wait_secs}s")

def _confirm(prompt: str, yes: bool) -> None:
    if yes: return
    answer = input(f"\n{prompt} [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted."); sys.exit(0)

def _get_or_create_compartment(identity_client, parent_id: str, name: str, dry_run: bool):
    if not dry_run:
        try:
            parent = identity_client.get_compartment(parent_id).data
            if parent.name == name and parent.lifecycle_state == "ACTIVE":
                _log(f"  ✔  Parent ID is already the target compartment '{name}': {parent_id}")
                return parent_id
        except: pass

    compartments = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments, parent_id, name=name, lifecycle_state="ACTIVE").data
    if compartments:
        _log(f"  ✔  Compartment '{name}' already exists: {compartments[0].id}")
        return compartments[0].id

    if dry_run: return "ocid1.compartment.oc1..DRY_RUN"

    resp = identity_client.create_compartment(
        oci.identity.models.CreateCompartmentDetails(
            compartment_id=parent_id, name=name,
            description="Hub resources compartment managed by create_hub_vcn.py"
        )
    )
    _log(f"  ✔  Created compartment '{name}': {resp.data.id}")
    time.sleep(15) # propagation
    return resp.data.id

def _subnet_cidr(hub_cidr: str, index: int) -> str:
    network = ipaddress.ip_network(hub_cidr, strict=False)
    new_prefix = min(network.prefixlen + 1, 29)
    halves = list(network.subnets(new_prefix=new_prefix))
    return str(halves[index])

# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------

def provision(parent_compartment_id: str, region: str, hub_cidr: str, spoke_vcn_ids: list,
              no_wait: bool, dry_run: bool, yes: bool, compartment_name: str = "hub_resources"):
    config = oci.config.from_file(); config["region"] = region
    identity_client = oci.identity.IdentityClient(config)
    vcn_client = oci.core.VirtualNetworkClient(config)
    nfw_client = oci.network_firewall.NetworkFirewallClient(config)

    print("\n" + "=" * 60)
    print("  OCI Hub Phase 1: Infrastructure Provisioner")
    print("=" * 60)
    
    _confirm("Proceed with Phase 1 (Non-disruptive)?", yes or dry_run)
    resources = {}

    # 1. Compartment
    _log(f"\n[1/8] Creating/verifying compartment '{compartment_name}' …")
    hub_compartment_id = _get_or_create_compartment(identity_client, parent_compartment_id, compartment_name, dry_run)
    resources["compartment_id"] = hub_compartment_id

    # 2. VCN_hub
    _log("\n[2/8] Creating VCN_hub …")
    if dry_run: vcn_id = "ocid1.vcn.oc1..DRY"
    else:
        existing = oci.pagination.list_call_get_all_results(vcn_client.list_vcns, hub_compartment_id, display_name="VCN_hub").data
        if existing: vcn_id = existing[0].id; _log(f"  ✔  VCN_hub already exists: {vcn_id}")
        else:
            resp = vcn_client.create_vcn(oci.core.models.CreateVcnDetails(compartment_id=hub_compartment_id, display_name="VCN_hub", cidr_blocks=[hub_cidr]))
            vcn_id = resp.data.id; _log(f"  ✔  Created VCN_hub: {vcn_id}")
    resources["vcn_hub_id"] = vcn_id

    # 3. Subnets
    _log("\n[3/8] Creating subnets …")
    fw_cidr = _subnet_cidr(hub_cidr, 0); mgmt_cidr = _subnet_cidr(hub_cidr, 1)
    
    def _sub(name, cidr, pub):
        if dry_run: return "ocid1.subnet..DRY"
        ex = oci.pagination.list_call_get_all_results(vcn_client.list_subnets, hub_compartment_id, vcn_id=vcn_id, display_name=name).data
        if ex: return ex[0].id
        return vcn_client.create_subnet(oci.core.models.CreateSubnetDetails(compartment_id=hub_compartment_id, vcn_id=vcn_id, display_name=name, cidr_block=cidr, prohibit_public_ip_on_vnic=pub)).data.id

    fw_sub_id = _sub("firewall-subnet", fw_cidr, True); mgmt_sub_id = _sub("mgmt-subnet", mgmt_cidr, False)
    resources.update({"firewall_subnet_id": fw_sub_id, "mgmt_subnet_id": mgmt_sub_id})

    # 4. Gateways
    _log("\n[4/8] Creating Gateways …")
    if dry_run: igw_id = "dr1"; nat_id = "dr2"
    else:
        igw_id = vcn_client.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(compartment_id=hub_compartment_id, vcn_id=vcn_id, display_name="hub-igw", is_enabled=True)).data.id if not dry_run else "dr1"
        nat_id = vcn_client.create_nat_gateway(oci.core.models.CreateNatGatewayDetails(compartment_id=hub_compartment_id, vcn_id=vcn_id, display_name="hub-nat")).data.id if not dry_run else "dr2"

    # 5. Policy & Firewall
    _log("\n[5/8] Creating Firewall Policy & Instance …")
    if not dry_run:
        pol_resp = nfw_client.create_network_firewall_policy(oci.network_firewall.models.CreateNetworkFirewallPolicyDetails(compartment_id=hub_compartment_id, display_name="hub-ngfw-allow-all"))
        pol_id = pol_resp.data.id
        _wait_for_state(nfw_client, lambda o: nfw_client.get_network_firewall_policy(o), pol_id, ["ACTIVE"], resource_label="Policy")
        nfw_client.create_security_rule(pol_id, oci.network_firewall.models.CreateSecurityRuleDetails(name="allow-all", action="ALLOW", condition=oci.network_firewall.models.SecurityRuleMatchCriteria(source_address=[], destination_address=[], application=[], service=[], url=[]), position=oci.network_firewall.models.RulePosition()))
        
        nfw_resp = nfw_client.create_network_firewall(oci.network_firewall.models.CreateNetworkFirewallDetails(compartment_id=hub_compartment_id, display_name="hub-ngfw", network_firewall_policy_id=pol_id, subnet_id=fw_sub_id))
        nfw_id = nfw_resp.data.id; _log(f"  ✔  Firewall creating: {nfw_id}")
        if not no_wait: _wait_for_state(nfw_client, lambda o: nfw_client.get_network_firewall(o), nfw_id, ["ACTIVE"])

    # 6. Hub Internal Routing
    _log("\n[6/8] Hub Internal Route Tables …")
    if not dry_run:
        # Firewall Subnet RT
        fw_rt = vcn_client.create_route_table(oci.core.models.CreateRouteTableDetails(compartment_id=hub_compartment_id, vcn_id=vcn_id, display_name="hub-firewall-rt", route_rules=[oci.core.models.RouteRule(destination="0.0.0.0/0", network_entity_id=nat_id)])).data.id
        vcn_client.update_subnet(fw_sub_id, oci.core.models.UpdateSubnetDetails(route_table_id=fw_rt))

    # 7. DRG & Attach Hub
    _log("\n[7/8] DRG Setup …")
    if not dry_run:
        drg_id = vcn_client.create_drg(oci.core.models.CreateDrgDetails(compartment_id=hub_compartment_id, display_name="hub-drg")).data.id
        _wait_for_state(vcn_client, lambda o: vcn_client.get_drg(o), drg_id, ["AVAILABLE"])
        # We also need an Ingress RT for the Hub attachment (placeholder for now)
        ingress_rt = vcn_client.create_route_table(oci.core.models.CreateRouteTableDetails(compartment_id=hub_compartment_id, vcn_id=vcn_id, display_name="hub-ingress-rt", route_rules=[])).data.id
        vcn_client.create_drg_attachment(oci.core.models.CreateDrgAttachmentDetails(display_name="hub-vcn-attachment", drg_id=drg_id, network_details=oci.core.models.VcnDrgAttachmentNetworkCreateDetails(id=vcn_id, type="VCN", route_table_id=ingress_rt)))

    # 8. Attach Spokes (Infrastructure only)
    _log(f"\n[8/8] Attaching {len(spoke_vcn_ids)} spokes (No routing changes yet) …")
    for vid in spoke_vcn_ids:
        if not dry_run:
            _log(f"  Attaching spoke {vid[-8:]} to DRG …")
            vcn_client.create_drg_attachment(oci.core.models.CreateDrgAttachmentDetails(display_name=f"spoke-{vid[-8:]}", drg_id=drg_id, network_details=oci.core.models.VcnDrgAttachmentNetworkCreateDetails(id=vid, type="VCN")))

    print("\n✔ PHASE 1 COMPLETE. Spoke traffic is UNAFFECTED.")
    print("Run enable_hub_interception.py ONCE THE FIREWALL IS ACTIVE.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-compartment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--hub-cidr", default="10.53.0.0/16")
    parser.add_argument("--spoke-vcns", nargs="*", default=[])
    parser.add_argument("--compartment-name", default="hub_resources")
    parser.add_argument("--no-wait", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    args = parser.parse_args()
    provision(args.parent_compartment, args.region, args.hub_cidr, args.spoke_vcns, args.no_wait, args.dry_run, args.yes, args.compartment_name)

if __name__ == "__main__": main()
