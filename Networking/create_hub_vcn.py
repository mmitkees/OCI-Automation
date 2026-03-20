#!/usr/bin/env python3
"""
create_hub_vcn.py
=================
Provisions an OCI Hub-and-Spoke network with a Network Firewall (NGFW).

What this script creates
------------------------
1.  Compartment  "hub_resources"  under --parent-compartment
2.  VCN_hub
3.  Subnets inside VCN_hub:
      - firewall-subnet  (private, 10.x.0.0/24 by default)
      - mgmt-subnet      (public,  10.x.1.0/24 by default)
4.  Internet Gateway, NAT Gateway
5.  OCI Network Firewall Policy  (allow-all rules)
6.  OCI Network Firewall  (in firewall-subnet)
7.  Route tables to steer traffic through the NGFW
8.  DRG  +  VCN_hub attachment
9.  For each spoke VCN OCID provided:
      - DRG attachment
      - DRG route table entry  (spoke CIDR → hub attachment – NGFW forwarding)
      - Default route update in the spoke VCN  (0.0.0.0/0 → DRG)

Usage
-----
python3 create_hub_vcn.py \\
    --parent-compartment ocid1.compartment.oc1..xxx \\
    --region me-abudhabi-1 \\
    [--hub-cidr 10.0.0.0/16] \\
    [--spoke-vcns ocid1.vcn.oc1..aaa ocid1.vcn.oc1..bbb] \\
    [--no-wait] \\
    [--dry-run] \\
    [--yes]
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
    """Poll until the resource reaches one of the desired lifecycle states."""
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
    raise TimeoutError(
        f"{resource_label} did not reach {desired_states} within {max_wait_secs}s"
    )


def _confirm(prompt: str, yes: bool) -> None:
    if yes:
        return
    answer = input(f"\n{prompt} [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)


def _get_or_create_compartment(identity_client, parent_id: str, name: str,
                                dry_run: bool):
    """Return existing compartment OCID or create a new one."""
    # Initial check for ACTIVE compartment
    compartments = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        parent_id,
        name=name,
        compartment_id_in_subtree=False,
        lifecycle_state="ACTIVE",
    ).data
    if compartments:
        ocid = compartments[0].id
        _log(f"  ✔  Compartment '{name}' already exists and is ACTIVE: {ocid}")
        return ocid

    # If not found ACTIVE, check if it's there in ANY state (to avoid 409)
    all_comps = oci.pagination.list_call_get_all_results(
        identity_client.list_compartments,
        parent_id,
        name=name,
        compartment_id_in_subtree=False,
    ).data
    
    if all_comps:
        comp = all_comps[0] # Take the first match
        _log(f"  ⚠  Compartment '{name}' exists but is {comp.lifecycle_state}: {comp.id}")
        if comp.lifecycle_state in ["DELETING", "DELETED"]:
             _log("     OCI is still purging the old compartment. Please wait a minute and retry.")
             sys.exit(1)
        return comp.id

    if dry_run:
        _log(f"  Would create compartment '{name}' under {parent_id}", dry_run=True)
        return "ocid1.compartment.oc1..DRY_RUN"

    try:
        resp = identity_client.create_compartment(
            oci.identity.models.CreateCompartmentDetails(
                compartment_id=parent_id,
                name=name,
                description="Hub resources compartment managed by create_hub_vcn.py",
            )
        )
        comp_id = resp.data.id
        _log(f"  ✔  Created compartment '{name}': {comp_id}")
    except oci.exceptions.ServiceError as e:
        if e.status == 409 and "AlreadyExists" in e.code:
             _log(f"  ⚠  409 Conflict: Compartment name '{name}' is still being purged by OCI. Retrying in 30s...")
             time.sleep(30)
             # Recursive call to retry
             return _get_or_create_compartment(identity_client, parent_id, name, dry_run)
        raise
    
    _log("  ⏳  Waiting for compartment IAM propagation (up to 90s) …")
    _wait_for_compartment_ready(identity_client, parent_id, comp_id)
    return comp_id


def _wait_for_compartment_ready(identity_client, parent_id: str, comp_id: str,
                                 max_wait: int = 120, poll: int = 10) -> None:
    """Wait until the compartment is ACTIVE and visible to the service APIs."""
    elapsed = 0
    while elapsed < max_wait:
        comps = oci.pagination.list_call_get_all_results(
            identity_client.list_compartments,
            parent_id,
            lifecycle_state="ACTIVE",
        ).data
        if any(c.id == comp_id for c in comps):
            _log(f"  ✔  Compartment propagated after {elapsed}s")
            # Extra buffer for networking APIs to see the compartment
            time.sleep(15)
            return
        time.sleep(poll)
        elapsed += poll
    # Give up waiting, try to proceed anyway
    _log("  ⚠  Compartment propagation wait timed out – proceeding anyway")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _subnet_cidr(hub_cidr: str, index: int) -> str:
    """Derive two equal-sized subnets from hub_cidr.

    For a /16 this gives /24 subnets; for a /24 it gives /25 subnets, etc.
    The minimum OCI subnet size is /29, so we cap the new prefix at 29.
    """
    network = ipaddress.ip_network(hub_cidr, strict=False)
    # Split hub CIDR once to get two halves
    base_prefix = network.prefixlen
    new_prefix  = min(base_prefix + 1, 29)
    halves = list(network.subnets(new_prefix=new_prefix))
    if index >= len(halves):
        raise ValueError(
            f"Hub CIDR {hub_cidr} ({base_prefix}) is too small to accommodate "
            f"subnet index {index} at prefix /{new_prefix}."
        )
    return str(halves[index])


# ---------------------------------------------------------------------------
# Main provisioning logic
# ---------------------------------------------------------------------------

def provision(
    parent_compartment_id: str,
    region: str,
    hub_cidr: str,
    spoke_vcn_ids: list,
    no_wait: bool,
    dry_run: bool,
    yes: bool,
    compartment_name: str = "hub_resources"
) -> dict:

    # ---- OCI clients -------------------------------------------------------
    config = oci.config.from_file()
    config["region"] = region

    identity_client  = oci.identity.IdentityClient(config)
    vcn_client       = oci.core.VirtualNetworkClient(config)
    nfw_client       = oci.network_firewall.NetworkFirewallClient(config)

    # ---- Summary printout --------------------------------------------------
    print("\n" + "=" * 60)
    print("  OCI Hub VCN + NGFW Provisioner")
    print("=" * 60)
    print(f"  Region             : {region}")
    print(f"  Parent compartment : {parent_compartment_id}")
    print(f"  Hub VCN CIDR       : {hub_cidr}")
    print(f"  Spoke VCNs         : {spoke_vcn_ids or '(none)'}")
    print(f"  Dry-run            : {dry_run}")
    print("=" * 60 + "\n")

    if dry_run:
        _log("DRY-RUN mode – no OCI resources will be created.", dry_run=True)

    _confirm("Proceed with provisioning?", yes or dry_run)

    resources = {}  # collects OCIDs of created resources for final summary

    # ========================================================================
    # STEP 1 – Compartment
    # ========================================================================
    _log(f"\n[1/9] Creating/verifying compartment '{compartment_name}' …")
    hub_compartment_id = _get_or_create_compartment(
        identity_client, parent_compartment_id, compartment_name, dry_run
    )
    resources["compartment_id"] = hub_compartment_id

    # ========================================================================
    # STEP 2 – VCN_hub
    # ========================================================================
    _log("\n[2/9] Creating VCN_hub …")

    # Check if already exists
    existing_vcns = oci.pagination.list_call_get_all_results(
        vcn_client.list_vcns,
        hub_compartment_id,
        display_name="VCN_hub",
    ).data if not dry_run else []

    if existing_vcns:
        vcn_id = existing_vcns[0].id
        _log(f"  ✔  VCN_hub already exists: {vcn_id}")
    elif dry_run:
        vcn_id = "ocid1.vcn.oc1..DRY_RUN"
        _log("  Would create VCN 'VCN_hub'", dry_run=True)
    else:
        resp = vcn_client.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=hub_compartment_id,
                display_name="VCN_hub",
                cidr_blocks=[hub_cidr],
                is_ipv6_enabled=False,
            )
        )
        vcn_id = resp.data.id
        _log(f"  ✔  Created VCN_hub: {vcn_id}")

    resources["vcn_hub_id"] = vcn_id

    # ========================================================================
    # STEP 3 – Subnets
    # ========================================================================
    _log("\n[3/9] Creating subnets …")

    firewall_cidr = _subnet_cidr(hub_cidr, 0)   # 10.x.0.0/24
    mgmt_cidr     = _subnet_cidr(hub_cidr, 1)   # 10.x.1.0/24

    def _get_or_create_subnet(display_name, cidr, prohibit_public, rt_id=None):
        if dry_run:
            _log(f"  Would create subnet '{display_name}' ({cidr})", dry_run=True)
            return f"ocid1.subnet.oc1..DRY_RUN_{display_name}"
        existing = oci.pagination.list_call_get_all_results(
            vcn_client.list_subnets,
            hub_compartment_id,
            vcn_id=vcn_id,
            display_name=display_name,
        ).data
        if existing:
            _log(f"  ✔  Subnet '{display_name}' already exists: {existing[0].id}")
            return existing[0].id
        kwargs = dict(
            compartment_id=hub_compartment_id,
            vcn_id=vcn_id,
            display_name=display_name,
            cidr_block=cidr,
            prohibit_public_ip_on_vnic=prohibit_public,
        )
        if rt_id:
            kwargs["route_table_id"] = rt_id
        resp = vcn_client.create_subnet(oci.core.models.CreateSubnetDetails(**kwargs))
        _log(f"  ✔  Created subnet '{display_name}': {resp.data.id}")
        return resp.data.id

    # Initially create subnets without custom RT (will update later after RT creation)
    firewall_subnet_id = _get_or_create_subnet("firewall-subnet", firewall_cidr, True)
    mgmt_subnet_id     = _get_or_create_subnet("mgmt-subnet",     mgmt_cidr,     False)
    resources["firewall_subnet_id"] = firewall_subnet_id
    resources["mgmt_subnet_id"]     = mgmt_subnet_id

    # ========================================================================
    # STEP 4 – Gateways
    # ========================================================================
    _log("\n[4/9] Creating Internet and NAT gateways …")

    def _get_or_create_igw():
        if dry_run:
            _log("  Would create Internet Gateway", dry_run=True)
            return "ocid1.internetgateway.oc1..DRY_RUN"
        existing = oci.pagination.list_call_get_all_results(
            vcn_client.list_internet_gateways,
            hub_compartment_id,
            vcn_id=vcn_id,
        ).data
        if existing:
            _log(f"  ✔  Internet Gateway already exists: {existing[0].id}")
            return existing[0].id
        resp = vcn_client.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=hub_compartment_id,
                vcn_id=vcn_id,
                display_name="hub-igw",
                is_enabled=True,
            )
        )
        _log(f"  ✔  Created Internet Gateway: {resp.data.id}")
        return resp.data.id

    def _get_or_create_nat():
        if dry_run:
            _log("  Would create NAT Gateway", dry_run=True)
            return "ocid1.natgateway.oc1..DRY_RUN"
        existing = oci.pagination.list_call_get_all_results(
            vcn_client.list_nat_gateways,
            hub_compartment_id,
            vcn_id=vcn_id,
        ).data
        if existing:
            _log(f"  ✔  NAT Gateway already exists: {existing[0].id}")
            return existing[0].id
        resp = vcn_client.create_nat_gateway(
            oci.core.models.CreateNatGatewayDetails(
                compartment_id=hub_compartment_id,
                vcn_id=vcn_id,
                display_name="hub-nat",
                block_traffic=False,
            )
        )
        _log(f"  ✔  Created NAT Gateway: {resp.data.id}")
        return resp.data.id

    igw_id = _get_or_create_igw()
    nat_id = _get_or_create_nat()
    resources["internet_gateway_id"] = igw_id
    resources["nat_gateway_id"]      = nat_id

    # ========================================================================
    # STEP 5 – Network Firewall Policy (allow-all)
    # ========================================================================
    _log("\n[5/9] Creating Network Firewall Policy (allow-all) …")

    if dry_run:
        nfw_policy_id = "ocid1.networkfirewallpolicy.oc1..DRY_RUN"
        _log("  Would create Network Firewall Policy 'hub-ngfw-allow-all'", dry_run=True)
    else:
        existing_policies = oci.pagination.list_call_get_all_results(
            nfw_client.list_network_firewall_policies,
            hub_compartment_id,
            display_name="hub-ngfw-allow-all",
        ).data
        existing_policies = [p for p in existing_policies
                             if p.lifecycle_state not in ("DELETED", "DELETING")]

        if existing_policies:
            nfw_policy_id = existing_policies[0].id
            _log(f"  ✔  Firewall policy already exists: {nfw_policy_id}")
        else:
            # 5a. Create the policy (no inline rules in this SDK version)
            resp = nfw_client.create_network_firewall_policy(
                oci.network_firewall.models.CreateNetworkFirewallPolicyDetails(
                    compartment_id=hub_compartment_id,
                    display_name="hub-ngfw-allow-all",
                    description="Allow-all policy managed by create_hub_vcn.py",
                )
            )
            nfw_policy_id = resp.data.id
            _log(f"  ✔  Created Firewall Policy (CREATING): {nfw_policy_id}")

            # 5b. Wait for policy to become ACTIVE before adding rules
            _log("  ⏳  Waiting for policy to become ACTIVE …")
            _wait_for_state(
                nfw_client,
                lambda ocid: nfw_client.get_network_firewall_policy(ocid),
                nfw_policy_id,
                ["ACTIVE"],
                resource_label="Firewall Policy",
            )
            _log("  ✔  Firewall Policy ACTIVE")

            # 5c. Add allow-all security rule
            _log("  Adding allow-all security rule …")
            existing_rules = oci.pagination.list_call_get_all_results(
                nfw_client.list_security_rules,
                nfw_policy_id,
            ).data
            if not any(r.name == "allow-all-traffic" for r in existing_rules):
                nfw_client.create_security_rule(
                    nfw_policy_id,
                    oci.network_firewall.models.CreateSecurityRuleDetails(
                        name="allow-all-traffic",
                        action="ALLOW",
                        condition=oci.network_firewall.models.SecurityRuleMatchCriteria(
                            source_address=[],
                            destination_address=[],
                            application=[],
                            service=[],
                            url=[],
                        ),
                        position=oci.network_firewall.models.RulePosition(
                            after_rule=None,
                            before_rule=None,
                        ),
                    ),
                )
                _log("  ✔  Allow-all security rule added")
            else:
                _log("  ✔  Allow-all security rule already exists")


    resources["nfw_policy_id"] = nfw_policy_id

    # ========================================================================
    # STEP 6 – Network Firewall instance
    # ========================================================================
    _log("\n[6/9] Creating Network Firewall instance …")

    if dry_run:
        nfw_id         = "ocid1.networkfirewall.oc1..DRY_RUN"
        nfw_private_ip = "10.0.0.10"
        _log("  Would create Network Firewall 'hub-ngfw' in firewall-subnet", dry_run=True)
    else:
        existing_nfw = oci.pagination.list_call_get_all_results(
            nfw_client.list_network_firewalls,
            hub_compartment_id,
            display_name="hub-ngfw",
        ).data
        existing_nfw = [f for f in existing_nfw
                        if f.lifecycle_state not in ("DELETED", "DELETING")]

        if existing_nfw:
            nfw_id         = existing_nfw[0].id
            nfw_private_ip = existing_nfw[0].ipv4_address
            _log(f"  ✔  NGFW already exists: {nfw_id}  IP: {nfw_private_ip}")
        else:
            nfw_details = oci.network_firewall.models.CreateNetworkFirewallDetails(
                compartment_id=hub_compartment_id,
                display_name="hub-ngfw",
                network_firewall_policy_id=nfw_policy_id,
                subnet_id=firewall_subnet_id,
            )
            resp = nfw_client.create_network_firewall(nfw_details)
            nfw_id = resp.data.id
            _log(f"  ✔  Created NGFW (CREATING): {nfw_id}")

            if not no_wait:
                _log("  ⏳  Waiting for NGFW to become ACTIVE (may take 20-40 min) …")
                _wait_for_state(
                    nfw_client,
                    lambda ocid: nfw_client.get_network_firewall(ocid),
                    nfw_id,
                    ["ACTIVE"],
                    max_wait_secs=3600,
                    resource_label="Network Firewall",
                )
                nfw_obj        = nfw_client.get_network_firewall(nfw_id).data
                nfw_private_ip = nfw_obj.ipv4_address
                _log(f"  ✔  NGFW ACTIVE – Private IP: {nfw_private_ip}")
            else:
                _log("  ⏳  Attempting to fetch Private IP from PROVISIONING state …")
                # Quick poll for IP assignment (often happens early)
                nfw_private_ip = None
                for i in range(5):
                    nfw_obj = nfw_client.get_network_firewall(nfw_id).data
                    if nfw_obj.ipv4_address:
                        nfw_private_ip = nfw_obj.ipv4_address
                        _log(f"  ✔  IP assigned early: {nfw_private_ip}")
                        break
                    time.sleep(10)
                
                if not nfw_private_ip:
                    nfw_private_ip = "<pending>"
                    _log("  ⚠  IP not yet assigned. Script will proceed with other tasks.")
                    _log("     NOTE: Hub Ingress Route Rule must be added manually once IP is ready.")

    resources["nfw_id"]         = nfw_id
    resources["nfw_private_ip"] = nfw_private_ip

    # ========================================================================
    # STEP 7 – Route Tables
    # ========================================================================
    _log("\n[7/9] Creating/updating route tables …")

    def _private_ip_route_rule(dst_cidr, private_ip):
        """Build a route rule via a private IP (NGFW or DRG)."""
        return oci.core.models.RouteRule(
            destination=dst_cidr,
            destination_type="CIDR_BLOCK",
            network_entity_id=private_ip,
        )

    def _gateway_route_rule(dst_cidr, gateway_id):
        return oci.core.models.RouteRule(
            destination=dst_cidr,
            destination_type="CIDR_BLOCK",
            network_entity_id=gateway_id,
        )

    def _get_or_create_route_table(display_name, rules):
        if dry_run:
            _log(f"  Would create route table '{display_name}'", dry_run=True)
            return f"ocid1.routetable.oc1..DRY_RUN_{display_name}"
        existing = oci.pagination.list_call_get_all_results(
            vcn_client.list_route_tables,
            hub_compartment_id,
            vcn_id=vcn_id,
            display_name=display_name,
        ).data
        if existing:
            # Update rules
            vcn_client.update_route_table(
                existing[0].id,
                oci.core.models.UpdateRouteTableDetails(route_rules=rules),
            )
            _log(f"  ✔  Updated route table '{display_name}': {existing[0].id}")
            return existing[0].id
        resp = vcn_client.create_route_table(
            oci.core.models.CreateRouteTableDetails(
                compartment_id=hub_compartment_id,
                vcn_id=vcn_id,
                display_name=display_name,
                route_rules=rules,
            )
        )
        _log(f"  ✔  Created route table '{display_name}': {resp.data.id}")
        return resp.data.id

    # 7a. Firewall subnet RT – egress from NGFW to internet via NAT
    fw_rt_rules = [
        oci.core.models.RouteRule(
            destination="0.0.0.0/0",
            destination_type="CIDR_BLOCK",
            network_entity_id=nat_id,
        )
    ]
    fw_rt_id = _get_or_create_route_table("hub-firewall-rt", fw_rt_rules)

    # 7b. Ingress route table – traffic entering VCN_hub from DRG goes to NGFW
    ingress_rt_rules = []
    if not dry_run and nfw_private_ip and not nfw_private_ip.startswith("<pending"):
        # Look up the private IP OCID from the NGFW IP address
        priv_ips = oci.pagination.list_call_get_all_results(
            vcn_client.list_private_ips,
            ip_address=nfw_private_ip,
            subnet_id=firewall_subnet_id,
        ).data
        nfw_ip_ocid = priv_ips[0].id if priv_ips else None
    else:
        nfw_ip_ocid = None

    if nfw_ip_ocid or dry_run:
        ingress_rt_rules = [
            oci.core.models.RouteRule(
                destination="0.0.0.0/0",
                destination_type="CIDR_BLOCK",
                network_entity_id=nfw_ip_ocid or "ocid1.privateip.oc1..DRY_RUN",
            )
        ]
        ingress_rt_id = _get_or_create_route_table("hub-ingress-rt", ingress_rt_rules)
    else:
        _log("  ⚠  NGFW private IP not available – hub-ingress-rt will have no rules (update after NGFW is ACTIVE).")
        ingress_rt_id = _get_or_create_route_table("hub-ingress-rt", [])

    # 7c. Management subnet RT – internet access via IGW
    mgmt_rt_rules = [
        oci.core.models.RouteRule(
            destination="0.0.0.0/0",
            destination_type="CIDR_BLOCK",
            network_entity_id=igw_id,
        )
    ]
    mgmt_rt_id = _get_or_create_route_table("hub-mgmt-rt", mgmt_rt_rules)

    resources["fw_rt_id"]      = fw_rt_id
    resources["ingress_rt_id"] = ingress_rt_id
    resources["mgmt_rt_id"]    = mgmt_rt_id

    # Attach route tables to subnets
    if not dry_run:
        _log("  Attaching route tables to subnets …")
        vcn_client.update_subnet(
            firewall_subnet_id,
            oci.core.models.UpdateSubnetDetails(route_table_id=fw_rt_id),
        )
        vcn_client.update_subnet(
            mgmt_subnet_id,
            oci.core.models.UpdateSubnetDetails(route_table_id=mgmt_rt_id),
        )
        _log("  ✔  Route tables attached to subnets")

    # ========================================================================
    # STEP 8 – DRG
    # ========================================================================
    _log("\n[8/9] Creating DRG and attaching VCN_hub …")

    if dry_run:
        drg_id         = "ocid1.drg.oc1..DRY_RUN"
        hub_attach_id  = "ocid1.drgattachment.oc1..DRY_RUN_hub"
        _log("  Would create DRG 'hub-drg'", dry_run=True)
    else:
        existing_drgs = oci.pagination.list_call_get_all_results(
            vcn_client.list_drgs,
            hub_compartment_id,
        ).data
        existing_drgs = [d for d in existing_drgs if d.display_name == "hub-drg"
                         and d.lifecycle_state == "AVAILABLE"]

        if existing_drgs:
            drg_id = existing_drgs[0].id
            _log(f"  ✔  DRG already exists: {drg_id}")
        else:
            resp   = vcn_client.create_drg(
                oci.core.models.CreateDrgDetails(
                    compartment_id=hub_compartment_id,
                    display_name="hub-drg",
                )
            )
            drg_id = resp.data.id
            _log(f"  ✔  Created DRG: {drg_id}")
            _wait_for_state(
                vcn_client,
                lambda ocid: vcn_client.get_drg(ocid),
                drg_id,
                ["AVAILABLE"],
                max_wait_secs=300,
                resource_label="DRG",
            )

        # Attach VCN_hub to DRG
        existing_attachments = oci.pagination.list_call_get_all_results(
            vcn_client.list_drg_attachments,
            hub_compartment_id,
            drg_id=drg_id,
            attachment_type="VCN",
        ).data
        hub_attach = next(
            (a for a in existing_attachments
             if hasattr(a, "network_details") and a.network_details and
             a.network_details.id == vcn_id),
            None,
        )
        if hub_attach:
            hub_attach_id = hub_attach.id
            _log(f"  ✔  Hub VCN already attached to DRG: {hub_attach_id}")
        else:
            resp = vcn_client.create_drg_attachment(
                oci.core.models.CreateDrgAttachmentDetails(
                    display_name="hub-vcn-attachment",
                    drg_id=drg_id,
                    network_details=oci.core.models.VcnDrgAttachmentNetworkCreateDetails(
                        id=vcn_id,
                        type="VCN",
                        route_table_id=ingress_rt_id,
                    ),
                )
            )
            hub_attach_id = resp.data.id
            _log(f"  ✔  Attached VCN_hub to DRG: {hub_attach_id}")

    resources["drg_id"]        = drg_id
    resources["hub_attach_id"] = hub_attach_id

    # ========================================================================
    # STEP 9 – Spoke VCN wiring
    # ========================================================================
    _log(f"\n[9/9] Wiring {len(spoke_vcn_ids)} spoke VCN(s) through NGFW …")
    spoke_attachments = []

    for spoke_vcn_id in spoke_vcn_ids:
        _log(f"\n  Spoke VCN: {spoke_vcn_id}")

        # -- Get spoke VCN CIDR
        if dry_run:
            spoke_cidr    = "192.168.0.0/16"
            spoke_attach_id = f"ocid1.drgattachment.oc1..DRY_RUN_{spoke_vcn_id[-6:]}"
            _log(f"  Would attach spoke VCN to DRG and add route table entries", dry_run=True)
        else:
            spoke_vcn_obj = vcn_client.get_vcn(spoke_vcn_id).data
            spoke_cidr    = spoke_vcn_obj.cidr_blocks[0]
            _log(f"  Spoke CIDR: {spoke_cidr}")

            # Attach spoke VCN to DRG
            existing_spoke_attachments = oci.pagination.list_call_get_all_results(
                vcn_client.list_drg_attachments,
                hub_compartment_id if hub_compartment_id else spoke_vcn_obj.compartment_id,
                drg_id=drg_id,
                attachment_type="VCN",
            ).data
            # Also check in spoke's own compartment
            existing_spoke_attachments += oci.pagination.list_call_get_all_results(
                vcn_client.list_drg_attachments,
                spoke_vcn_obj.compartment_id,
                drg_id=drg_id,
                attachment_type="VCN",
            ).data

            spoke_attach = next(
                (a for a in existing_spoke_attachments
                 if hasattr(a, "network_details") and a.network_details and
                 a.network_details.id == spoke_vcn_id),
                None,
            )
            if spoke_attach:
                spoke_attach_id = spoke_attach.id
                _log(f"  ✔  Spoke already attached to DRG: {spoke_attach_id}")
            else:
                resp = vcn_client.create_drg_attachment(
                    oci.core.models.CreateDrgAttachmentDetails(
                        display_name=f"spoke-{spoke_vcn_id[-8:]}",
                        drg_id=drg_id,
                        network_details=oci.core.models.VcnDrgAttachmentNetworkCreateDetails(
                            id=spoke_vcn_id,
                            type="VCN",
                        ),
                    )
                )
                spoke_attach_id = resp.data.id
                _log(f"  ✔  Attached spoke VCN to DRG: {spoke_attach_id}")

            # -- Update ALL route tables in spoke VCN: 0.0.0.0/0 → DRG
            spoke_rts = oci.pagination.list_call_get_all_results(
                vcn_client.list_route_tables,
                spoke_vcn_obj.compartment_id,
                vcn_id=spoke_vcn_id,
            ).data

            for rt in spoke_rts:
                rules = rt.route_rules
                # Find the 0.0.0.0/0 rule if it exists
                default_rule = next((r for r in rules if r.destination == "0.0.0.0/0"), None)
                
                if default_rule:
                    if default_rule.network_entity_id != drg_id:
                        # Update existing rule
                        default_rule.network_entity_id = drg_id
                        vcn_client.update_route_table(
                            rt.id,
                            oci.core.models.UpdateRouteTableDetails(route_rules=rules),
                        )
                        _log(f"  ✔  Updated spoke RT '{rt.display_name}' (0.0.0.0/0 → DRG)")
                    else:
                        _log(f"  ✔  Spoke RT '{rt.display_name}' already points to DRG")
                else:
                    # If no default rule, we don't automatically add it to avoid breaking subnets 
                    # that are intentionally disconnected from the internet.
                    # Exception: If it's the DEFAULT RT of the VCN, we should ensure it has the rule.
                    if rt.display_name == "Default Route Table for " + spoke_vcn_obj.display_name:
                        rules.append(
                            oci.core.models.RouteRule(
                                destination="0.0.0.0/0",
                                destination_type="CIDR_BLOCK",
                                network_entity_id=drg_id,
                            )
                        )
                        vcn_client.update_route_table(
                            rt.id,
                            oci.core.models.UpdateRouteTableDetails(route_rules=rules),
                        )
                        _log(f"  ✔  Created 0.0.0.0/0 → DRG in Spoke DEFAULT RT '{rt.display_name}'")
                    else:
                        _log(f"  ℹ  Skipped RT '{rt.display_name}' (no 0.0.0.0/0 rule found)")

        spoke_attachments.append(
            {"spoke_vcn_id": spoke_vcn_id, "attach_id": spoke_attach_id}
        )

    resources["spoke_attachments"] = spoke_attachments

    # ========================================================================
    # Final summary
    # ========================================================================
    print("\n" + "=" * 100)
    print(f"  PROVISIONING COMPLETE {' (DRY-RUN)' if dry_run else ''}")
    print("=" * 100)
    
    # Mapping for pretty printing
    mapping = {
        "compartment_id": "Compartment",
        "vcn_hub_id": "VCN (Hub)",
        "firewall_subnet_id": "Subnet (Firewall)",
        "mgmt_subnet_id": "Subnet (Management)",
        "internet_gateway_id": "Internet Gateway",
        "nat_gateway_id": "NAT Gateway",
        "nfw_policy_id": "Firewall Policy",
        "nfw_id": "Network Firewall",
        "nfw_private_ip": "Firewall Private IP",
        "fw_rt_id": "Route Table (Firewall)",
        "ingress_rt_id": "Route Table (Ingress)",
        "mgmt_rt_id": "Route Table (Management)",
        "drg_id": "DRG",
        "hub_attach_id": "DRG Attachment (Hub)"
    }

    print(f"  {'Type':<28} | {'Value / OCID'}")
    print("-" * 100)
    
    for k, v in resources.items():
        if k == "spoke_attachments":
            for sa in v:
                print(f"  {'DRG Attachment (Spoke)':<28} | {sa['attach_id']} (VCN: {sa['spoke_vcn_id'][-12:]}…)")
        else:
            display_name = mapping.get(k, k.replace("_", " ").title())
            print(f"  {display_name:<28} | {v}")
    
    print("=" * 100)

    if not dry_run and resources.get("nfw_private_ip") == "<pending>":
        print("\n  [!] ACTION REQUIRED: The Firewall is still provisioning.")
        print("      Once it becomes ACTIVE, run the following command to finalize routing:")
        print(f"      python3 create_hub_vcn.py --parent-compartment {parent_compartment_id} --region {region} --wait")
    
    print("\n")
    return resources


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Provision an OCI Hub VCN (VCN_hub) with Network Firewall "
                    "and spoke VCN routing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--parent-compartment", required=True, help="Parent compartment OCID.")
    parser.add_argument("--region", required=True, help="OCI region identifier (e.g. me-abudhabi-1).")
    parser.add_argument("--compartment-name", default="hub_resources", help="Name for the hub resources compartment.")
    parser.add_argument("--hub-cidr", default="10.0.0.0/16", help="Hub VCN CIDR (default 10.0.0.0/16).")
    parser.add_argument(
        "--spoke-vcns",
        nargs="*",
        default=[],
        metavar="OCID",
        help="Zero or more spoke VCN OCIDs to wire through the hub firewall.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the NGFW to become ACTIVE (20-40 min) before finishing routing.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        default=True,
        help="[DEFAULT] Do not wait for the NGFW to become ACTIVE (routing may be incomplete).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without making API calls.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip interactive confirmation prompt.",
    )

    args = parser.parse_args()

    provision(
        parent_compartment_id=args.parent_compartment,
        region=args.region,
        hub_cidr=args.hub_cidr,
        spoke_vcn_ids=args.spoke_vcns,
        no_wait=not args.wait,
        dry_run=args.dry_run,
        yes=args.yes,
        compartment_name=args.compartment_name
    )


if __name__ == "__main__":
    main()
