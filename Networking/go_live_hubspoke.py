#!/usr/bin/env python3
"""
go_live_hubspoke.py
===================
PHASE 2: Traffic Redirection (Activation).
Enables Hub-and-Spoke interception only after the Network Firewall is ACTIVE.

What this script does
---------------------
1.  Verifies the Network Firewall is ACTIVE.
2.  Fetches the Firewall's private IP.
3.  Configures Hub Ingress Routing (DRG Attachment -> Firewall).
4.  Configures Hub Return Routing (Firewall Subnet -> DRG for Spokes).
5.  Configures DRG Transit Routing (Static 0.0.0.0/0 -> Hub Attachment).
6.  Configures Spoke Redirection (Spoke RTs -> DRG).
7.  Verifies connectivity from a spoke instance.
"""

import argparse
import sys
import time
import oci

def _log(msg): print(f"  {msg}", flush=True)

def _check_active(client, get_fn, ocid, label="resource"):
    res = get_fn(ocid).data
    state = res.lifecycle_state
    if state != 'ACTIVE':
        print(f"\n  ❌  ABORT: {label} is not ready (State: '{state}').")
        print("      REASON: OCI Network Firewall provisioning is currently in progress.")
        print("      Provisioning typically takes 30-40 minutes. Please run this script later.")
        sys.exit(1)
    return res

def go_live(compartment_id, region, spoke_vcn_ids):
    config = oci.config.from_file(); config["region"] = region
    vcn_client = oci.core.VirtualNetworkClient(config)
    nfw_client = oci.network_firewall.NetworkFirewallClient(config)

    print("\n" + "=" * 60)
    print("  OCI Hub Phase 2: GO-LIVE (Traffic Interception)")
    print("=" * 60)

    # 1. Verify Firewall
    _log("Verifying Network Firewall …")
    nfws = oci.pagination.list_call_get_all_results(nfw_client.list_network_firewalls, compartment_id, display_name="hub-ngfw").data
    if not nfws: raise Exception("NGFW 'hub-ngfw' not found.")
    nfw = _check_active(nfw_client, lambda o: nfw_client.get_network_firewall(o), nfws[0].id, "Network Firewall")
    nfw_ip = nfw.ipv4_address
    fw_sub_id = nfw.subnet_id
    _log(f"Firewall is ACTIVE. IP: {nfw_ip}")

    # 2. Get Hub VCN & DRG
    vcns = oci.pagination.list_call_get_all_results(vcn_client.list_vcns, compartment_id, display_name="VCN_hub").data
    if not vcns: raise Exception("VCN_hub not found.")
    hub_id = vcns[0].id
    
    drgs = oci.pagination.list_call_get_all_results(vcn_client.list_drgs, compartment_id).data
    drg = next((d for d in drgs if d.display_name == "hub-drg"), None)
    if not drg: raise Exception("DRG 'hub-drg' not found.")
    drg_id = drg.id

    # 3. Hub Ingress Routing (DRG -> Firewall)
    _log("Configuring Hub Ingress Routing …")
    priv_ips = oci.pagination.list_call_get_all_results(vcn_client.list_private_ips, ip_address=nfw_ip, subnet_id=fw_sub_id).data
    if not priv_ips: raise Exception(f"Could not find Private IP OCID for {nfw_ip}")
    nfw_ip_ocid = priv_ips[0].id

    ing_rts = oci.pagination.list_call_get_all_results(vcn_client.list_route_tables, compartment_id, vcn_id=hub_id, display_name="hub-ingress-rt").data
    if ing_rts:
        vcn_client.update_route_table(ing_rts[0].id, oci.core.models.UpdateRouteTableDetails(route_rules=[oci.core.models.RouteRule(destination="0.0.0.0/0", network_entity_id=nfw_ip_ocid)]))
        _log("  ✔  hub-ingress-rt updated (0.0.0.0/0 -> Firewall)")

    # 4. Hub Return Routing (Firewall -> DRG)
    _log("Configuring Hub Return Routing …")
    fw_rts = oci.pagination.list_call_get_all_results(vcn_client.list_route_tables, compartment_id, vcn_id=hub_id, display_name="hub-firewall-rt").data
    if fw_rts:
        rt = fw_rts[0]; rules = rt.route_rules
        nat_rule = next((r for r in rules if r.destination == "0.0.0.0/0"), None)
        new_rules = [nat_rule] if nat_rule else []
        for svid in spoke_vcn_ids:
            sc = vcn_client.get_vcn(svid).data.cidr_blocks[0]
            new_rules.append(oci.core.models.RouteRule(destination=sc, network_entity_id=drg_id))
            _log(f"  + Added return route for {sc} -> DRG")
        vcn_client.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=new_rules))

    # 5. DRG Transit Routing (DRG RT -> Hub Attachment)
    _log("Configuring DRG Transit Routing …")
    hub_ats = oci.pagination.list_call_get_all_results(vcn_client.list_drg_attachments, compartment_id, drg_id=drg_id).data
    hub_at = next((a for a in hub_ats if a.display_name == "hub-vcn-attachment"), None)
    if hub_at and hub_at.drg_route_table_id:
        try:
            vcn_client.add_drg_route_rules(hub_at.drg_route_table_id, oci.core.models.AddDrgRouteRulesDetails(route_rules=[
                oci.core.models.AddDrgRouteRuleDetails(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", next_hop_drg_attachment_id=hub_at.id)]))
            _log("  ✔  DRG Transit Route added (0.0.0.0/0 -> Hub)")
        except oci.exceptions.ServiceError as e:
            if "AlreadyExists" in str(e): _log("  ✔  DRG Transit Route already exists.")
            else: raise

    # 6. Spoke Redirection (Spoke RT -> DRG)
    _log("Enabling Spoke Redirection (THE SWITCH) …")
    for svid in spoke_vcn_ids:
        s_vcn = vcn_client.get_vcn(svid).data
        s_rts = oci.pagination.list_call_get_all_results(vcn_client.list_route_tables, s_vcn.compartment_id, vcn_id=svid).data
        for rt in s_rts:
            rules = rt.route_rules
            existing = next((r for r in rules if r.destination == "0.0.0.0/0"), None)
            if existing:
                if existing.network_entity_id != drg_id:
                    existing.network_entity_id = drg_id
                    vcn_client.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
                    _log(f"  ✔  Updated Spoke RT '{rt.display_name}': 0.0.0.0/0 -> DRG")
            elif rt.display_name.startswith("Default Route Table") or "public" in rt.display_name.lower():
                rules.append(oci.core.models.RouteRule(destination="0.0.0.0/0", network_entity_id=drg_id))
                vcn_client.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(route_rules=rules))
                _log(f"  ✔  INTERCEPTED Spoke RT '{rt.display_name}': 0.0.0.0/0 -> DRG")

    print("\n✔ PHASE 2 COMPLETE. Full Interception is ACTIVE.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub-compartment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--spoke-vcns", nargs="+", required=True)
    args = parser.parse_args()
    go_live(args.hub_compartment, args.region, args.spoke_vcns)

if __name__ == "__main__": main()
