#!/usr/bin/env python3

"""Enable cross-region replication for block and boot volumes via OCI Python SDK.

Supports both interactive usage and CLI flags. Prompts for source/destination
regions, compartment selection, lists available volumes, and creates replication
resources using the OCI Python SDK.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Tuple

try:
    import oci
except ImportError:
    sys.exit("Error: 'oci' Python package not found. Please run: pip install oci")


class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'



def get_config() -> dict[str, str]:
    """Retrieve default OCI configuration."""
    return oci.config.from_file()


def list_compartments(config: dict[str, str]) -> Tuple[str, str]:
    """List child compartments within the current tenancy root."""
    identity = oci.identity.IdentityClient(config)
    tenancy = config["tenancy"]
    
    response = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        tenancy,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
        lifecycle_state="ACTIVE"
    )
    compartments = response.data
    
    entries = sorted(compartments, key=lambda c: c.name)
    if not entries:
        raise SystemExit("No compartments available.")
        
    for idx, comp in enumerate(entries, start=1):
        print(f"{idx}) {comp.name} | {comp.id}")
        
    choice = input("Select compartment number or enter OCID: ").strip()
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(entries):
            comp = entries[idx]
            return comp.name, comp.id
    return choice, choice


def get_child_compartments(config: dict[str, str], root_ocid: str) -> List[str]:
    """Get all child compartments of the specified root recursively."""
    identity = oci.identity.IdentityClient(config)
    tenancy = config["tenancy"]
    
    response = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        tenancy,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
        lifecycle_state="ACTIVE"
    )
    compartments = response.data
    
    children = []
    parents = {c.id: c.compartment_id for c in compartments}
    
    def walk(current: str):
        children.append(current)
        for cid, parent_id in parents.items():
            if parent_id == current and cid not in children:
                walk(cid)
                
    walk(root_ocid)
    return children


def list_volumes(config: dict[str, str], compartment: str, region: str) -> List[Tuple[str, str, str]]:
    """List available block and boot volumes in a specific compartment and region."""
    core_client = oci.core.BlockstorageClient(config)
    core_client.base_client.set_region(region)
    
    identity = oci.identity.IdentityClient(config)
    identity.base_client.set_region(region)
    
    volumes: List[Tuple[str, str, str]] = []
    
    # Block volumes
    try:
        block_vols = oci.pagination.list_call_get_all_results(
            core_client.list_volumes,
            compartment_id=compartment
        ).data
        for v in block_vols:
            if v.lifecycle_state == "AVAILABLE":
                volumes.append(("Block", v.id, v.display_name))
    except oci.exceptions.ServiceError:
        pass
        
    # Boot volumes (listing per AD is required for boot volumes)
    try:
        ads = identity.list_availability_domains(config["tenancy"]).data
        for ad in ads:
            try:
                boot_vols = oci.pagination.list_call_get_all_results(
                    core_client.list_boot_volumes,
                    availability_domain=ad.name,
                    compartment_id=compartment
                ).data
                for v in boot_vols:
                    if v.lifecycle_state == "AVAILABLE":
                        volumes.append(("Boot", v.id, v.display_name))
            except oci.exceptions.ServiceError:
                pass
    except oci.exceptions.ServiceError:
        pass
            
    return volumes


def preview(volumes: List[Tuple[str, str, str]], region: str) -> None:
    """Print preview of the volumes to be replicated."""
    print(f"\n{Colors.HEADER}{Colors.BOLD}--- Volume preview (source region: {region}) ---{Colors.ENDC}")
    print(f"{Colors.OKCYAN}{'TYPE':<12} {'OCID':<80} NAME{Colors.ENDC}")
    for vol_type, vol_id, name in volumes:
        print(f"{vol_type:<12} {Colors.OKBLUE}{vol_id:<80}{Colors.ENDC} {name}")


def create_replication(config: dict[str, str], vol_id: str, vol_name: str, src_region: str, dest_region: str, dest_ad: str, vol_type: str) -> None:
    """Apply replication to the specified volume using the Python SDK."""
    core_client = oci.core.BlockstorageClient(config)
    core_client.base_client.set_region(src_region)
    
    print(f"\n{Colors.BOLD}{Colors.OKBLUE}▶ Executing SDK: Updating {vol_type} volume '{vol_name}' ({vol_id}) to replicate to {dest_region} ({dest_ad}){Colors.ENDC}")
    
    try:
        if vol_type == "Block":
            # Check for existing replicas first
            existing_vol = core_client.get_volume(volume_id=vol_id).data
            replicas = getattr(existing_vol, 'block_volume_replicas', []) or []
            if any(r.availability_domain == dest_ad for r in replicas):
                print(f"{Colors.OKGREEN}✔ [{vol_type}] Volume '{vol_name}' is already replicated to {dest_ad}. Skipping update.{Colors.ENDC}")
                return
                
            # API Update for Block Volume
            update_details = oci.core.models.UpdateVolumeDetails(
                block_volume_replicas=[
                    oci.core.models.BlockVolumeReplicaDetails(
                        availability_domain=dest_ad,
                        display_name=f"{vol_type}Replica"
                    )
                ]
            )
            print(f"{Colors.WARNING}--- API Call ---\ncore_client.update_volume(\n    volume_id='{vol_id}',\n    update_volume_details={update_details}\n)\n----------------{Colors.ENDC}")
            response = core_client.update_volume(
                volume_id=vol_id,
                update_volume_details=update_details
            )
            print(f"{Colors.OKGREEN}✔ [{vol_type}] Updated replication for {vol_id}: {response.data.id} ({response.data.lifecycle_state}){Colors.ENDC}")
            print(f"{Colors.OKCYAN}--- API Response Data ---\n{response.data}\n-------------------------{Colors.ENDC}")
            
            
        elif vol_type == "Boot":
            # Check for existing replicas first
            existing_vol = core_client.get_boot_volume(boot_volume_id=vol_id).data
            replicas = getattr(existing_vol, 'boot_volume_replicas', []) or []
            if any(r.availability_domain == dest_ad for r in replicas):
                print(f"{Colors.OKGREEN}✔ [{vol_type}] Volume '{vol_name}' is already replicated to {dest_ad}. Skipping update.{Colors.ENDC}")
                return
                
            # API Update for Boot Volume
            update_details = oci.core.models.UpdateBootVolumeDetails(
                boot_volume_replicas=[
                    oci.core.models.BootVolumeReplicaDetails(
                        availability_domain=dest_ad,
                        display_name=f"{vol_type}Replica"
                    )
                ]
            )
            print(f"{Colors.WARNING}--- API Call ---\ncore_client.update_boot_volume(\n    boot_volume_id='{vol_id}',\n    update_boot_volume_details={update_details}\n)\n----------------{Colors.ENDC}")
            response = core_client.update_boot_volume(
                boot_volume_id=vol_id,
                update_boot_volume_details=update_details
            )
            print(f"{Colors.OKGREEN}✔ [{vol_type}] Updated replication for {vol_id}: {response.data.id} ({response.data.lifecycle_state}){Colors.ENDC}")
            print(f"{Colors.OKCYAN}--- API Response Data ---\n{response.data}\n-------------------------{Colors.ENDC}")
            
    except oci.exceptions.ServiceError as exc:
        print(f"{Colors.FAIL}✖ [ERROR] Failed to update {vol_type} replication for {vol_id}:\n{exc.message}{Colors.ENDC}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enable OCI cross-region replication via Native OCI SDK.")
    parser.add_argument("--src", help="Source region (e.g., me-abudhabi-1)")
    parser.add_argument("--dest", help="Destination region (e.g., eu-zurich-1)")
    parser.add_argument("--compartment", help="Compartment OCID")
    parser.add_argument("--yes", action="store_true", help="Automatically confirm")
    args = parser.parse_args()

    src_region = args.src or input("Enter source region (e.g., me-abudhabi-1): ").strip()
    dest_region = args.dest or input("Enter destination region (e.g., eu-zurich-1): ").strip()
    if src_region == dest_region:
        raise SystemExit("Source and destination must differ.")

    config = get_config()

    if args.compartment:
        comp_name = args.compartment
        comp_ocid = args.compartment
    else:
        name, comp_ocid = list_compartments(config)
        comp_name = name
    print(f"Selected compartment: {comp_name} ({comp_ocid})")

    compartments = get_child_compartments(config, comp_ocid)
    print("Compartment(s) to process:")
    for cid in compartments:
        print(f"  {cid}")

    volumes = []
    for cid in compartments:
        volumes.extend(list_volumes(config, cid, src_region))

    if not volumes:
        print("No available volumes to replicate.")
        return

    preview(volumes, src_region)
    if not args.yes:
        confirm = input(f"{Colors.WARNING}{Colors.BOLD}Proceed with replication? (y/n): {Colors.ENDC}").strip().lower()
        if confirm != "y":
            print(f"{Colors.FAIL}Aborted by user.{Colors.ENDC}")
            return

    # To replicate, we need the first availability domain in the destination region
    identity = oci.identity.IdentityClient(config)
    identity.base_client.set_region(dest_region)
    try:
        ads = identity.list_availability_domains(config["tenancy"]).data
        if not ads:
            raise SystemExit(f"[ERROR] No availability domains found in destination region {dest_region}")
        dest_ad = ads[0].name
    except oci.exceptions.ServiceError as exc:
        raise SystemExit(f"[ERROR] Failed to get availability domains for {dest_region}: {exc.message}")

    for vol_type, vol_id, vol_name in volumes:
        create_replication(config, vol_id, vol_name, src_region, dest_region, dest_ad, vol_type)


if __name__ == "__main__":
    main()
