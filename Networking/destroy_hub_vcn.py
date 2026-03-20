#!/usr/bin/env python3
"""
Destroy OCI Hub VCN and all associated resources created by create_hub_vcn.py.
Undoes spoke routing and handles resource dependencies (DRG, NGFW, VCN).
"""

import oci
import sys
import time
import argparse
from datetime import datetime

# Helper for logging
def _log(msg, dry_run=False):
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"{prefix}{msg}")

def _wait_for_state(client_fn, get_fn, resource_id, target_states, max_wait_secs=1800, interval_secs=30, resource_label="Resource"):
    """Generic waiter for OCI resource lifecycle states."""
    start_time = time.time()
    while time.time() - start_time < max_wait_secs:
        try:
            res = get_fn(resource_id)
            state = res.data.lifecycle_state.upper()
            elapsed = int(time.time() - start_time)
            _log(f"  ⏳  {resource_label} state: {state} (elapsed {elapsed}s) …")
            if state in target_states:
                return True
            if state in ["FAILED", "TERMINATED", "DELETED"]:
                return False
        except (oci.exceptions.ServiceError, oci.exceptions.RequestException) as e:
            # Handle 404 (Resource deleted - success)
            if hasattr(e, 'status') and e.status == 404 and "DELETED" in target_states:
                return True
            # Handle transient network errors
            _log(f"  ⚠  Transient error polling {resource_label}: {e}. Retrying…")
        except Exception as e:
             _log(f"  ⚠  Unexpected error polling {resource_label}: {e}. Retrying…")
        
        time.sleep(interval_secs)
    return False

def destroy_all(parent_compartment_id, region, hub_cidr, spoke_vcn_ids=None, wait=True, dry_run=False, yes=False, compartment_name="hub_resources"):
    config = oci.config.from_file()
    config["region"] = region

    vcn_client = oci.core.VirtualNetworkClient(config)
    nfw_client = oci.network_firewall.NetworkFirewallClient(config)
    identity_client = oci.identity.IdentityClient(config)
    
    deleted_resources = []

    if not spoke_vcn_ids:
        spoke_vcn_ids = []

    _log(f"Parent Compartment: {parent_compartment_id}")
    _log(f"Region            : {region}")
    _log(f"Hub VCN CIDR      : {hub_cidr}")
    _log(f"Spoke VCNs        : {spoke_vcn_ids}")
    _log(f"Wait for cleanup  : {wait}")
    _log(f"Dry-run           : {dry_run}")
    
    if not dry_run and not yes:
        confirm = input(f"\nWARNING: This will DESTROY all resources in '{compartment_name}' and UNDO spoke routing. Proceed? [y/N] ")
        if confirm.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    # 1. Locate compartment
    hub_compartment_id = None
    if not dry_run:
        compartments = oci.pagination.list_call_get_all_results(
            identity_client.list_compartments,
            parent_compartment_id,
            name=compartment_name,
            lifecycle_state="ACTIVE"
        ).data
        if not compartments:
            _log(f"  ⚠  Compartment '{compartment_name}' not found. Nothing to destroy?")
            return
        hub_compartment_id = compartments[0].id
        _log(f"  ✔  Found compartment {compartment_name}: {hub_compartment_id}")

    # 2. Undo Spoke Routing
    _log("\n[1/8] Undoing spoke VCN routing …")
    for spoke_vcn_id in spoke_vcn_ids:
        if dry_run:
            _log(f"  Would remove 0.0.0.0/0 -> DRG rule from spoke {spoke_vcn_id}", dry_run=True)
        else:
            try:
                spoke_vcn = vcn_client.get_vcn(spoke_vcn_id).data
                rt_list = oci.pagination.list_call_get_all_results(
                    vcn_client.list_route_tables,
                    spoke_vcn.compartment_id,
                    vcn_id=spoke_vcn_id
                ).data
                for rt in rt_list:
                    # Only remove the rule if it points to the DRG
                    new_rules = [r for r in rt.route_rules if not (r.destination == "0.0.0.0/0" and r.network_entity_id.startswith("ocid1.drg"))]
                    if len(new_rules) != len(rt.route_rules):
                        vcn_client.update_route_table(
                            rt.id,
                            oci.core.models.UpdateRouteTableDetails(route_rules=new_rules)
                        )
                        _log(f"  ✔  Removed 0.0.0.0/0 rule from Spoke RT: {rt.id}")
                        deleted_resources.append(("Spoke Route Undo", spoke_vcn_id, "Removed 0.0.0.0/0 rule"))
            except Exception as e:
                _log(f"  ⚠  Could not undo routing for spoke {spoke_vcn_id}: {e}")

    # 3. Handle DRG and Attachments
    _log("\n[2/8] Deleting DRG Attachments and DRG …")
    if not dry_run:
        drgs = oci.pagination.list_call_get_all_results(vcn_client.list_drgs, hub_compartment_id).data
        hub_drg = next((d for d in drgs if d.display_name == "hub-drg" and d.lifecycle_state != "TERMINATED"), None)
        
        if hub_drg:
            # Detach all VCNs - Search in both Hub and Spoke compartments
            search_compartments = list(set([hub_compartment_id] + [vcn_client.get_vcn(sid).data.compartment_id for sid in spoke_vcn_ids]))
            
            attachments = []
            for comp_id in search_compartments:
                attachments += oci.pagination.list_call_get_all_results(
                    vcn_client.list_drg_attachments, 
                    comp_id,
                    drg_id=hub_drg.id
                ).data
            
            # Filter unique attachments by ID
            seen_attach = set()
            unique_attachments = []
            for a in attachments:
                if a.id not in seen_attach:
                    unique_attachments.append(a)
                    seen_attach.add(a.id)

            for attach in unique_attachments:
                _log(f"  Deleting attachment: {attach.id} …")
                vcn_client.delete_drg_attachment(attach.id)
                # Wait for each attachment to be gone
                _wait_for_state(
                    vcn_client,
                    lambda ocid: vcn_client.get_drg_attachment(ocid),
                    attach.id,
                    ["TERMINATED", "DELETED"],
                    max_wait_secs=300,
                    resource_label="DRG Attachment"
                )
            
            _log(f"  Deleting DRG: {hub_drg.id} …")
            # Retry loop for DRG deletion as it can still be 'busy' for a few seconds
            for i in range(5):
                try:
                    vcn_client.delete_drg(hub_drg.id)
                    deleted_resources.append(("DRG", hub_drg.display_name, hub_drg.id))
                    _log("  ✔  DRG deletion initiated")
                    break
                except oci.exceptions.ServiceError as e:
                    if e.status == 409 and i < 4:
                        _log(f"  ⏳  DRG still busy, retrying in 10s (attempt {i+1}/5) …")
                        time.sleep(10)
                    else:
                        raise
        else:
            _log("  ✔  No hub-drg found")

    # 4. Handle Network Firewall
    _log("\n[3/8] Deleting Network Firewall instance …")
    if not dry_run:
        nfws = oci.pagination.list_call_get_all_results(nfw_client.list_network_firewalls, hub_compartment_id).data
        hub_nfw = next((f for f in nfws if f.display_name == "hub-ngfw" and f.lifecycle_state != "DELETED"), None)
        
        if hub_nfw:
            _log(f"  Deleting NGFW: {hub_nfw.id} …")
            nfw_client.delete_network_firewall(hub_nfw.id)
            if wait:
                _log("  ⏳  Waiting for NGFW to be DELETED (20-30 min) …")
                _wait_for_state(
                    nfw_client,
                    lambda ocid: nfw_client.get_network_firewall(ocid),
                    hub_nfw.id,
                    ["DELETED"],
                    resource_label="Network Firewall"
                )
                deleted_resources.append(("Network Firewall", hub_nfw.display_name, hub_nfw.id))
                _log("  ✔  NGFW deleted")
            else:
                deleted_resources.append(("Network Firewall (DELETING)", hub_nfw.display_name, hub_nfw.id))
                _log("  ⚠  NGFW deletion in progress (background). Subnets/VCN cannot be deleted yet.")
        else:
            _log("  ✔  No hub-ngfw found")

    # 5. Handle Route Tables and Gateways
    _log("\n[4/8] Deleting Route Tables and Gateways …")
    if not dry_run:
        vcns = oci.pagination.list_call_get_all_results(vcn_client.list_vcns, hub_compartment_id).data
        hub_vcn = next((v for v in vcns if v.display_name == "VCN_hub" and v.lifecycle_state != "TERMINATED"), None)
        
        if hub_vcn:
            # Detach RTs from subnets first
            subnets = oci.pagination.list_call_get_all_results(vcn_client.list_subnets, hub_compartment_id, vcn_id=hub_vcn.id).data
            for s in subnets:
                _log(f"  Detaching RT from subnet: {s.display_name} …")
                vcn_client.update_subnet(s.id, oci.core.models.UpdateSubnetDetails(route_table_id=None))
            
            # Delete rules from ALL RTs (including Default) to free up Gateways
            rts = oci.pagination.list_call_get_all_results(vcn_client.list_route_tables, hub_compartment_id, vcn_id=hub_vcn.id).data
            for rt in rts:
                if rt.route_rules:
                    _log(f"  Clearing rules from Route Table: {rt.display_name} …")
                    vcn_client.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=[]))
            
            # Now delete custom RTs (skip Default RT as it cannot be deleted)
            for rt in rts:
                if rt.display_name != f"Default Route Table for {hub_vcn.display_name}":
                    _log(f"  Deleting Route Table: {rt.display_name} …")
                    try:
                        vcn_client.delete_route_table(rt.id)
                        deleted_resources.append(("Route Table", rt.display_name, rt.id))
                    except oci.exceptions.ServiceError as e:
                        _log(f"  ⚠  Could not delete RT {rt.display_name}: {e.message}")
            
            # Delete Gateways (now that rules are cleared)
            igs = oci.pagination.list_call_get_all_results(vcn_client.list_internet_gateways, hub_compartment_id, vcn_id=hub_vcn.id).data
            for ig in igs:
                _log(f"  Deleting Internet Gateway: {ig.display_name} …")
                vcn_client.delete_internet_gateway(ig.id)
                deleted_resources.append(("Internet Gateway", ig.display_name, ig.id))
            
            ngs = oci.pagination.list_call_get_all_results(vcn_client.list_nat_gateways, hub_compartment_id, vcn_id=hub_vcn.id).data
            for ng in ngs:
                _log(f"  Deleting NAT Gateway: {ng.display_name} …")
                vcn_client.delete_nat_gateway(ng.id)
                deleted_resources.append(("NAT Gateway", ng.display_name, ng.id))

    # 6. Handle Subnets and VCN
    _log("\n[5/8] Deleting Subnets and VCN …")
    if not dry_run and hub_vcn:
        subnets = oci.pagination.list_call_get_all_results(vcn_client.list_subnets, hub_compartment_id, vcn_id=hub_vcn.id).data
        for s in subnets:
            _log(f"  Deleting Subnet: {s.display_name} …")
            try:
                vcn_client.delete_subnet(s.id)
                deleted_resources.append(("Subnet", s.display_name, s.id))
            except oci.exceptions.ServiceError as e:
                _log(f"  ⚠  Could not delete subnet {s.display_name}: {e.message}")
                if "Network Firewall" in e.message:
                    _log("     (Still waiting for NGFW to clear its attachment)")

        if wait:
            _log("  ⏳  Waiting for subnets to clear …")
            time.sleep(10)
            _log(f"  Deleting VCN: {hub_vcn.id} …")
            try:
                vcn_client.delete_vcn(hub_vcn.id)
                deleted_resources.append(("VCN", hub_vcn.display_name, hub_vcn.id))
                _log("  ✔  VCN deleted")
            except:
                _log("  ⚠  VCN deletion failed (likely subnets or dependencies still exist).")
        else:
             _log("  ⚠  VCN deletion skipped because wait=False.")

    # 7. Handle Firewall Policy
    _log("\n[6/8] Deleting Firewall Policy …")
    if not dry_run:
        policies = oci.pagination.list_call_get_all_results(nfw_client.list_network_firewall_policies, hub_compartment_id).data
        for p in policies:
            if p.display_name == "hub-ngfw-allow-all":
                _log(f"  Deleting Policy: {p.id} …")
                try:
                    nfw_client.delete_network_firewall_policy(p.id)
                    deleted_resources.append(("Firewall Policy", p.display_name, p.id))
                    _log("  ✔  Policy deleted")
                except: pass

    # 7. Delete Compartment
    _log("\n[7/8] Deleting compartment 'hub_resources' …")
    if not dry_run:
        identity_client = oci.identity.IdentityClient(config)
        try:
            # Re-fetch to be sure of latest state
            comps = oci.pagination.list_call_get_all_results(identity_client.list_compartments, parent_compartment_id).data
            hub_comp = next((c for c in comps if c.name == "hub_resources" and c.lifecycle_state != "TERMINATED"), None)
            
            if hub_comp:
                _log(f"  Attempting to delete compartment: {hub_comp.id} …")
                identity_client.delete_compartment(hub_comp.id)
                deleted_resources.append(("Compartment", hub_comp.name, hub_comp.id))
                _log("  ✔  Compartment deletion initiated (OCI will finalize in background)")
            else:
                _log("  ✔  No 'hub_resources' compartment found to delete")
        except oci.exceptions.ServiceError as e:
            _log(f"  ⚠  Could not delete compartment: {e.message}")
            _log("      (Check if it still contains resources or is being cleared by OCI)")
    else:
        _log("  [DRY-RUN] Would delete compartment 'hub_resources'", dry_run=True)

    # 8. Summary Table
    if deleted_resources:
        print("\n" + "=" * 80)
        print(f"  DESTRUCTION SUMMARY {' (DRY-RUN)' if dry_run else ''}")
        print("=" * 80)
        print(f"  {'Type':<25} | {'Name':<25} | {'OCID / Details'}")
        print("-" * 80)
        for dtype, dname, docid in deleted_resources:
            print(f"  {dtype:<25} | {dname:<25} | {docid}")
        print("=" * 80)

    print("\n[8/8] Destruction routine complete.")
    return True

def main():
    parser = argparse.ArgumentParser(description="Destroy OCI Hub VCN and associated resources.")
    parser.add_argument("--parent-compartment", required=True, help="Parent compartment OCID.")
    parser.add_argument("--region", required=True, help="OCI region (e.g. me-abudhabi-1).")
    parser.add_argument("--compartment-name", default="hub_resources", help="Name of the hub resources compartment.")
    parser.add_argument("--hub-cidr", default="10.1.0.0/16", help="CIDR of the VCN to help identify it.")
    parser.add_argument("--spoke-vcns", nargs="*", help="List of spoke VCN OCIDs to undo routing for.")
    parser.add_argument("--no-wait", action="store_true", help="Do not wait for long deletions (NGFW).")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions only.")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    
    args = parser.parse_args()
    
    destroy_all(
        parent_compartment_id=args.parent_compartment,
        region=args.region,
        hub_cidr=args.hub_cidr,
        spoke_vcn_ids=args.spoke_vcns,
        wait=not args.no_wait,
        dry_run=args.dry_run,
        yes=args.yes,
        compartment_name=args.compartment_name
    )

if __name__ == "__main__":
    main()
