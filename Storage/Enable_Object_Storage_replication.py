#!/usr/bin/env python3

"""Enable OCI Object Storage cross-region replication using Python SDK.

This script mirrors the behavior of Enable_Object_Storage_replication.sh:
- interactive or flag-based source/destination/compartment input
- recursive child-compartment processing
- IAM policy create/update for Object Storage service principals
- source bucket versioning enablement
- destination bucket existence/compatibility checks
- replication policy creation per bucket
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

try:
    import oci
except ImportError:
    sys.exit("Error: 'oci' Python package not found. Please run: pip install oci")


class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


@dataclass
class BucketEntry:
    name: str
    compartment_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enable OCI Object Storage cross-region replication")
    parser.add_argument("--src", help="Source region (e.g., me-abudhabi-1)")
    parser.add_argument("--dest", help="Destination region (e.g., eu-zurich-1)")
    parser.add_argument("--compartment", help="Compartment OCID")
    parser.add_argument("--policy", "--policy-name", dest="policy_name", default="ObjectStorageReplicationServicePolicy")
    parser.add_argument("--yes", "-y", action="store_true", help="Automatically confirm")
    return parser.parse_args()


def get_config() -> Dict[str, str]:
    return oci.config.from_file()


def get_active_compartments(identity: oci.identity.IdentityClient, tenancy_id: str):
    response = oci.pagination.list_call_get_all_results(
        identity.list_compartments,
        tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ACCESSIBLE",
        lifecycle_state="ACTIVE",
    )
    return sorted(response.data, key=lambda c: c.name.lower())


def select_compartment(compartments) -> Tuple[str, str]:
    for idx, comp in enumerate(compartments, 1):
        print(f"{idx}) {comp.name} | {comp.id}")

    choice = input("Select compartment number or enter OCID: ").strip()
    if choice.isdigit():
        pos = int(choice) - 1
        if 0 <= pos < len(compartments):
            selected = compartments[pos]
            return selected.name, selected.id
    return choice, choice


def collect_descendants(root_compartment_id: str, compartments) -> List[str]:
    children_by_parent: Dict[str, List[str]] = {}
    for comp in compartments:
        parent = comp.compartment_id
        children_by_parent.setdefault(parent, []).append(comp.id)

    result: List[str] = []

    def walk(comp_id: str):
        if comp_id in result:
            return
        result.append(comp_id)
        for child in children_by_parent.get(comp_id, []):
            walk(child)

    walk(root_compartment_id)
    return result


def get_namespace(config: Dict[str, str], region: str) -> str:
    os_client = oci.object_storage.ObjectStorageClient(config)
    os_client.base_client.set_region(region)
    return os_client.get_namespace().data


def ensure_iam_policy(identity: oci.identity.IdentityClient, tenancy_id: str, src_region: str, dest_region: str, policy_name: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}--- IAM Policy Setup ---{Colors.ENDC}")

    statements = [
        f"Allow service objectstorage-{src_region} to manage object-family in tenancy",
        f"Allow service objectstorage-{dest_region} to manage object-family in tenancy",
    ]

    print(f"{Colors.OKCYAN}Checking IAM Policy: {policy_name}...{Colors.ENDC}")
    existing = oci.pagination.list_call_get_all_results(
        identity.list_policies,
        compartment_id=tenancy_id,
        name=policy_name,
    ).data

    target = next((p for p in existing if p.lifecycle_state == "ACTIVE"), None)
    if target is None:
        print(f"{Colors.WARNING}Policy '{policy_name}' not found. Attempting to create...{Colors.ENDC}")
        try:
            identity.create_policy(
                oci.identity.models.CreatePolicyDetails(
                    compartment_id=tenancy_id,
                    name=policy_name,
                    description="Automated policy for cross-region object storage replication",
                    statements=statements,
                )
            )
            print(f"{Colors.OKGREEN}✔ Successfully created IAM policy '{policy_name}'{Colors.ENDC}")
        except oci.exceptions.ServiceError as exc:
            print(f"{Colors.FAIL}✖ [ERROR] Failed to create IAM policy:\n{exc.message}{Colors.ENDC}")
            if exc.code == "TenantCapacityExceeded":
                print(f"{Colors.WARNING}Your tenancy reached the IAM policy limit. Use --policy with an existing policy or delete unused ones.{Colors.ENDC}")
            raise
    else:
        print(f"{Colors.OKGREEN}✔ IAM policy '{policy_name}' already exists. Updating statements...{Colors.ENDC}")
        try:
            identity.update_policy(
                policy_id=target.id,
                update_policy_details=oci.identity.models.UpdatePolicyDetails(
                    statements=statements,
                ),
            )
            print(f"{Colors.OKGREEN}✔ Successfully updated IAM policy.{Colors.ENDC}")
        except oci.exceptions.ServiceError as exc:
            print(f"{Colors.FAIL}✖ [ERROR] Failed to update IAM policy:\n{exc.message}{Colors.ENDC}")
            raise


def list_source_buckets(config: Dict[str, str], namespace: str, source_region: str, compartments: List[str]) -> List[BucketEntry]:
    os_client = oci.object_storage.ObjectStorageClient(config)
    os_client.base_client.set_region(source_region)
    found: List[BucketEntry] = []

    print(f"Scanning for buckets in source region {source_region}...")
    for cid in compartments:
        try:
            buckets = oci.pagination.list_call_get_all_results(
                os_client.list_buckets,
                namespace_name=namespace,
                compartment_id=cid,
            ).data
            for b in buckets:
                found.append(BucketEntry(name=b.name, compartment_id=cid))
        except oci.exceptions.ServiceError:
            continue
    return found


def preview_buckets(entries: List[BucketEntry], source_region: str):
    print(f"\n{Colors.HEADER}{Colors.BOLD}--- Bucket preview (source region: {source_region}) ---{Colors.ENDC}")
    print(f"{Colors.OKCYAN}{'COMPARTMENT_OCID':<80} NAME{Colors.ENDC}")
    for e in entries:
        print(f"{e.compartment_id:<80} {Colors.OKBLUE}{e.name}{Colors.ENDC}")


def ensure_bucket_prereqs(config: Dict[str, str], namespace: str, bucket_name: str, compartment_id: str, src_region: str, dest_region: str):
    src_client = oci.object_storage.ObjectStorageClient(config)
    src_client.base_client.set_region(src_region)
    dest_client = oci.object_storage.ObjectStorageClient(config)
    dest_client.base_client.set_region(dest_region)

    print(f"{Colors.OKCYAN}Ensuring versioning is enabled on source bucket '{bucket_name}' in compartment {compartment_id}...{Colors.ENDC}")
    try:
        src_client.update_bucket(
            namespace_name=namespace,
            bucket_name=bucket_name,
            update_bucket_details=oci.object_storage.models.UpdateBucketDetails(versioning="Enabled"),
        )
    except oci.exceptions.ServiceError as exc:
        print(f"{Colors.FAIL}✖ [ERROR] Failed to enable source versioning for {bucket_name}: {exc.message}{Colors.ENDC}")

    print(f"{Colors.OKCYAN}Ensuring destination bucket '{bucket_name}' exists in {dest_region} (compartment {compartment_id})...{Colors.ENDC}")
    try:
        existing = dest_client.get_bucket(namespace_name=namespace, bucket_name=bucket_name).data
        if existing.versioning == "Enabled":
            dest_client.update_bucket(
                namespace_name=namespace,
                bucket_name=bucket_name,
                update_bucket_details=oci.object_storage.models.UpdateBucketDetails(versioning="Suspended"),
            )
    except oci.exceptions.ServiceError as exc:
        if exc.status == 404:
            dest_client.create_bucket(
                namespace_name=namespace,
                create_bucket_details=oci.object_storage.models.CreateBucketDetails(
                    name=bucket_name,
                    compartment_id=compartment_id,
                ),
            )
        else:
            print(f"{Colors.FAIL}✖ [ERROR] Failed to verify/create destination bucket {bucket_name}: {exc.message}{Colors.ENDC}")


def create_replication_policy(config: Dict[str, str], namespace: str, bucket_name: str, src_region: str, dest_region: str):
    policy_name = f"ReplicationTo-{dest_region}"
    os_client = oci.object_storage.ObjectStorageClient(config)
    os_client.base_client.set_region(src_region)

    print(f"\n{Colors.BOLD}{Colors.OKBLUE}▶ Executing: Setting up Object Storage replication for bucket '{bucket_name}' to {dest_region}{Colors.ENDC}")
    try:
        os_client.create_replication_policy(
            namespace_name=namespace,
            bucket_name=bucket_name,
            create_replication_policy_details=oci.object_storage.models.CreateReplicationPolicyDetails(
                destination_region_name=dest_region,
                destination_bucket_name=bucket_name,
                name=policy_name,
            ),
        )
        print(f"{Colors.OKGREEN}✔ [Bucket] Successfully created replication policy for {bucket_name}{Colors.ENDC}")
    except oci.exceptions.ServiceError as exc:
        print(f"{Colors.FAIL}✖ [ERROR] Failed to create replication policy for {bucket_name}:\n{exc.message}{Colors.ENDC}")
        if exc.status == 403:
            print(f"{Colors.WARNING}Hint: IAM policy propagation may take a few minutes. Retry shortly.{Colors.ENDC}")


def main() -> None:
    args = parse_args()

    src_region = args.src or input("Enter source region (e.g., me-abudhabi-1): ").strip()
    dest_region = args.dest or input("Enter destination region (e.g., eu-zurich-1): ").strip()

    if src_region == dest_region:
        raise SystemExit("Source and destination must differ.")

    config = get_config()
    tenancy_id = config.get("tenancy")
    if not tenancy_id:
        raise SystemExit("Unable to determine tenancy OCID from OCI config.")

    identity = oci.identity.IdentityClient(config)

    print("Fetching compartments...")
    all_compartments = get_active_compartments(identity, tenancy_id)
    if not all_compartments:
        raise SystemExit("No compartments available.")

    if args.compartment:
        comp_name, comp_id = args.compartment, args.compartment
    else:
        comp_name, comp_id = select_compartment(all_compartments)
    print(f"Selected compartment: {comp_name} ({comp_id})")

    print("Fetching child compartments...")
    process_compartments = collect_descendants(comp_id, all_compartments)
    print("Compartment(s) to process:")
    for cid in process_compartments:
        print(f"  {cid}")

    print(f"Fetching namespace for region {src_region}...")
    namespace = get_namespace(config, src_region)

    ensure_iam_policy(identity, tenancy_id, src_region, dest_region, args.policy_name)

    buckets = list_source_buckets(config, namespace, src_region, process_compartments)
    if not buckets:
        print("No available buckets to replicate in the given compartment(s).")
        return

    preview_buckets(buckets, src_region)
    if not args.yes:
        confirm = input(f"{Colors.WARNING}{Colors.BOLD}Proceed with replication? (y/n): {Colors.ENDC}").strip().lower()
        if confirm != "y":
            print(f"{Colors.FAIL}Aborted by user.{Colors.ENDC}")
            return

    print(f"{Colors.OKCYAN}Pausing for 10 seconds to allow IAM policy propagation...{Colors.ENDC}")
    time.sleep(10)

    for entry in buckets:
        ensure_bucket_prereqs(config, namespace, entry.name, entry.compartment_id, src_region, dest_region)
        time.sleep(2)
        create_replication_policy(config, namespace, entry.name, src_region, dest_region)


if __name__ == "__main__":
    main()
