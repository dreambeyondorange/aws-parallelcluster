# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import re
from enum import Enum

from pcluster.config.cluster_config import QueueUpdateStrategy
from pcluster.config.update_policy_utils import SharedStorageChangeInfo
from pcluster.constants import (
    AWSBATCH,
    DEFAULT_MAX_COUNT,
    DEFAULT_MIN_COUNT,
    SLURM,
    STORAGE_TYPES_SUPPORTING_LIVE_UPDATES,
)


class UpdatePolicy:
    """Describes the policy that rules the update of a configuration parameter."""

    class CheckResult(str, Enum):
        """Valid results for change checks."""

        SUCCEEDED = "SUCCEEDED"
        ACTION_NEEDED = "ACTION NEEDED"
        FAILED = "FAILED"

    def __init__(
        self,
        base_policy=None,
        name=None,
        level=None,
        fail_reason=None,
        action_needed=None,
        condition_checker=None,
        print_succeeded=True,
    ):
        self.name = None
        self.fail_reason = None
        self.action_needed = None
        self.condition_checker = None
        self.print_succeeded = print_succeeded
        self.level = 0

        if base_policy:
            self.name = base_policy.name
            self.fail_reason = base_policy.fail_reason
            self.action_needed = base_policy.action_needed
            self.condition_checker = base_policy.condition_checker
            self.level = base_policy.level

        if name:
            self.name = name
        if level:
            self.level = level
        if fail_reason:
            self.fail_reason = fail_reason
        if action_needed:
            self.action_needed = action_needed
        if condition_checker:
            self.condition_checker = condition_checker

    def check(self, change, patch):
        """
        Check if the update can be safely performed.

        Based on the policy condition checker, the result can be FAILED, SUCCEEDED or ACTION_NEEDED.
        :param change: The change to check
        :param patch: The ConfigPatch the change belongs to
        :return: FAILED, SUCCEEDED or ACTION_NEEDED
        """
        if self.condition_checker:
            if self.condition_checker(change, patch):
                result = UpdatePolicy.CheckResult.SUCCEEDED
                fail_reason = "-"
                action_needed = None
                print_change = self.print_succeeded
            else:
                result = UpdatePolicy.CheckResult.ACTION_NEEDED
                fail_reason = self.fail_reason
                action_needed = self.action_needed
                print_change = True
        else:
            # No condition checker means no chance of getting a successful check result, so CheckResult.FAILED is
            # returned unconditionally
            result = UpdatePolicy.CheckResult.FAILED
            fail_reason = self.fail_reason
            action_needed = self.action_needed
            print_change = True

        if callable(action_needed):
            action_needed = action_needed(change, patch)

        if callable(fail_reason):
            fail_reason = fail_reason(change, patch)

        return result, fail_reason, action_needed, print_change

    def __eq__(self, other):
        if not isinstance(other, UpdatePolicy):
            # don't attempt to compare against unrelated types
            return NotImplemented

        return self.fail_reason == other.fail_reason and self.level == other.level


def actions_needed_queue_update_strategy(change, _):
    actions = "Stop the compute fleet with the pcluster update-compute-fleet command"
    # QueueUpdateStrategy can override UpdatePolicy of parameters under SlurmQueues
    if is_slurm_queues_change(change):
        actions += ", or set QueueUpdateStrategy in the configuration used for the 'update-cluster' operation"

    return actions


def actions_needed_resize_update_strategy_on_remove(*_):
    return (
        "Stop the compute fleet or set QueueUpdateStrategy:TERMINATE in config "
        "to remove nodes from the scheduler, terminate the associated instances & any running jobs"
    )


def actions_needed_managed_placement_group(change, patch):
    if is_managed_placement_group_deletion(change, patch):
        actions = "Stop the compute fleet with the pcluster update-compute-fleet command."
    else:
        actions = actions_needed_queue_update_strategy(change, patch)
    return actions


def condition_checker_compute_fleet_stop_on_remove(change, patch):
    result = not patch.cluster.has_running_capacity()
    # SlurmQueue or ComputeResource can be added but removal require compute fleet stop
    if change.is_list and (is_slurm_queues_change(change) or change.key == "SlurmQueues"):
        result = result or (change.old_value is None and change.new_value is not None)

    return result


def is_slurm_queues_change(change):
    return any(path.startswith("SlurmQueues[") for path in change.path)


def extract_type_and_name_from_path(path):
    # Example path = 'SlurmQueues[slurm-q-name]'
    # This function returns the type and name extracted like this: 'SlurmQueues', 'slurm-q-name'
    obj_type = re.search(".+(?=\\[)", path).group(0)  # Get the sub string before the '['
    obj_name = re.search("(?<=\\[).+(?=\\])", path).group(0)  # Get the sub string between '[' and ']'
    return obj_type, obj_name


def get_q_from_config(change, config):
    # Example path=['Scheduling', 'SlurmQueues[q-pg-enabled]', 'ComputeResources[cr-pg-enabled]']
    # This function would return the dictionary 'q-pg-enabled' from the config using the key from the change path
    q_type, q_name = None, None
    for path in change.path:
        if re.search("Queues\\[", path):
            q_type, q_name = extract_type_and_name_from_path(path)
    if q_type and q_name:
        for queue in config.get("Scheduling", {}).get(q_type, {}):
            if queue.get("Name", None) == q_name:
                return queue
    return {}


def get_cr_from_config(change, config):
    # Example path=['Scheduling', 'SlurmQueues[q-pg-enabled]', 'ComputeResources[cr-pg-enabled]']
    # This function would return the dictionary 'cr-pg-enabled' from the config using the key from the change path
    cr_type, cr_name = None, None
    for path in change.path:
        if re.search("ComputeResources\\[", path):
            cr_type, cr_name = extract_type_and_name_from_path(path)
    if cr_type and cr_name:
        queue = get_q_from_config(change, config)
        for compute_resource in queue.get(cr_type, {}):
            if compute_resource.get("Name", None) == cr_name:
                return compute_resource
    return {}


def is_placement_group_managed_for_compute_resource(queue_networking, compute_resource_networking):
    chosen_pg = compute_resource_networking.get("PlacementGroup") or queue_networking.get("PlacementGroup")
    if chosen_pg and chosen_pg.get("Enabled") and not (chosen_pg.get("Name") or chosen_pg.get("Id")):
        return True
    return False


def is_managed_placement_group_deletion(change, patch):
    base_q_networking = get_q_from_config(change, patch.base_config).get("Networking", {})
    base_cr_networking = get_cr_from_config(change, patch.base_config).get("Networking", {})
    target_q_networking = get_q_from_config(change, patch.target_config).get("Networking", {})
    target_cr_networking = get_cr_from_config(change, patch.target_config).get("Networking", {})
    return is_placement_group_managed_for_compute_resource(
        base_q_networking, base_cr_networking
    ) and not is_placement_group_managed_for_compute_resource(target_q_networking, target_cr_networking)


def get_managed_fsx_from_config(config):
    """Extract managed Fsx for Lustre shared storage entries in a cluster configuration."""
    managed_fsx_storage = [
        storage
        for storage in config.get("SharedStorage", [])
        if storage.get("StorageType") == "FsxLustre" and "FileSystemId" not in storage.get("FsxLustreSettings", {})
    ]
    return managed_fsx_storage


def unchanged_managed_fsx_lustre_names(_, patch):
    """Get list of managed Fsx for Lustre Shared Storage that hasn't changed between cluster configuration updates."""
    managed_fsx_names_before_update = {fsx.get("Name") for fsx in get_managed_fsx_from_config(patch.base_config)}
    managed_fsx_names_after_update = {fsx.get("Name") for fsx in get_managed_fsx_from_config(patch.target_config)}
    return managed_fsx_names_before_update.intersection(managed_fsx_names_after_update)


def is_slurm_scheduler(patch):
    return patch.cluster.stack.scheduler == SLURM


def is_awsbatch_scheduler(_, patch):
    return patch.cluster.stack.scheduler == AWSBATCH


def is_compute_fleet_stop_required_for_shared_storage_change(change, patch):
    return (
        patch.cluster.has_running_capacity()
        and not is_compute_fleet_update_supported_for_shared_storage(change)
        and not is_queue_update_strategy_set(patch)
    )


def is_login_fleet_stop_required_for_shared_storage_change(change, patch):
    return patch.cluster.has_running_login_nodes() and not is_login_fleet_update_supported_for_shared_storage(change)


def is_login_fleet_update_supported_for_shared_storage(change):
    """
    Check if the live update of the running login fleet is supported for the given change to SharedStorage.

    We are referring here to a live update, that does not require the replacement of login nodes.
    In particular, the live update is supported only in the following cases:
      1. mount/unmount of external EFS
      1. mount/unmount of external FSx
    """
    change_info = SharedStorageChangeInfo(change)

    return change_info.is_external and (change_info.storage_type in STORAGE_TYPES_SUPPORTING_LIVE_UPDATES)


def is_compute_fleet_update_supported_for_shared_storage(change):
    """
    Check if the live update of the running compute fleet is supported for the given change to SharedStorage.

    We are referring here to a live update, that does not require the replacement of compute nodes.
    In particular, the live update is supported only in the following cases:
      1. mount/unmount of external EFS
      1. mount/unmount of external FSx
    """
    change_info = SharedStorageChangeInfo(change)

    return change_info.is_external and (change_info.storage_type in STORAGE_TYPES_SUPPORTING_LIVE_UPDATES)


def is_home_change(change):
    if change.key == "SharedStorage":
        if (change.old_value and change.old_value.get("MountDir") in ["home", "/home"]) or (
            change.new_value and change.new_value.get("MountDir") in ["home", "/home"]
        ):
            return True
    return False


def fail_reason_shared_storage_update_policy(change, patch):
    if is_home_change(change):
        return "The /home directory cannot be changed during an update"
    if is_awsbatch_scheduler(change, patch):
        return f"Update actions are not currently supported for the '{change.key}' parameter"

    reasons = []
    if is_compute_fleet_stop_required_for_shared_storage_change(change, patch):
        reasons.append("All compute nodes must be stopped or QueueUpdateStrategy must be set.")
    if is_login_fleet_stop_required_for_shared_storage_change(change, patch):
        reasons.append("All login nodes must be stopped")

    return " ".join(reasons)


def fail_reason_queue_update_strategy(change, _):
    reason = "All compute nodes must be stopped"
    # QueueUpdateStrategy can override UpdatePolicy of parameters under SlurmQueues
    if is_slurm_queues_change(change):
        reason += " or QueueUpdateStrategy must be set"

    return reason


def fail_reason_resize_update_strategy_on_remove(*_):
    return "All compute nodes must be stopped or QueueUpdateStrategy must be set to TERMINATE"


def fail_reason_managed_placement_group(change, patch):
    if is_managed_placement_group_deletion(change, patch):
        reason = "All compute nodes must be stopped for a managed placement group deletion"
    else:
        reason = fail_reason_queue_update_strategy(change, patch)
    return reason


def fail_reason_managed_fsx(change, patch):
    managed_fsx_lustre_names = unchanged_managed_fsx_lustre_names(change, patch)
    if managed_fsx_lustre_names:
        reason = (
            f"{change.key} configuration cannot be updated when a managed FSx for Lustre file system is configured. "
            "Forcing an update would trigger a deletion of the existing file system and result in potential data loss"
        )
    else:
        reason = fail_reason_queue_update_strategy(change, patch)
    return reason


def is_queue_update_strategy_set(patch):
    return (
        patch.target_config.get("Scheduling")
        .get("SlurmSettings", {})
        .get("QueueUpdateStrategy", QueueUpdateStrategy.COMPUTE_FLEET_STOP.value)
        != QueueUpdateStrategy.COMPUTE_FLEET_STOP.value
    )


def is_resize_update_strategy_terminate(patch):
    # Return true if the update strategy is set to TERMINATE
    update_strategy = (
        patch.target_config.get("Scheduling")
        .get("SlurmSettings", {})
        .get("QueueUpdateStrategy", QueueUpdateStrategy.COMPUTE_FLEET_STOP.value)
    )
    return update_strategy == QueueUpdateStrategy.TERMINATE.value


def condition_checker_queue_update_strategy(change, patch):
    result = not patch.cluster.has_running_capacity()
    # QueueUpdateStrategy can override UpdatePolicy of parameters under SlurmQueues
    if is_slurm_queues_change(change):
        result = result or is_queue_update_strategy_set(patch)

    return result


def condition_checker_resize_update_strategy_on_remove(change, patch):
    # Check if fleet is stopped
    result = not patch.cluster.has_running_capacity()

    # Check if the change is inside a Queue section
    if not result and (is_slurm_queues_change(change) or change.key == "SlurmQueues"):
        # Check if QueueUpdateStrategy is TERMINATE
        result = is_resize_update_strategy_terminate(patch)

        # Queue or ComputeResource can be added
        if not result and change.is_list:
            result = change.old_value is None and change.new_value is not None

    # Check if MaxCount is increased
    if not result and change.key == "MaxCount":
        result = convert_value_to_int(change.new_value, DEFAULT_MAX_COUNT) >= convert_value_to_int(
            change.old_value, DEFAULT_MAX_COUNT
        )

    # Check if MinCount is increased and MaxCount is increased of at least the same amount
    if not result and change.key == "MinCount":
        path = change.path
        for other_change in patch.changes:
            # Check the value of MaxCount next to MinCount.
            # MinCount and MaxCount next to each other have the same path
            if path == other_change.path and other_change.key == "MaxCount":
                other_change_new_value = convert_value_to_int(other_change.new_value, DEFAULT_MAX_COUNT)
                other_change_old_value = convert_value_to_int(other_change.old_value, DEFAULT_MAX_COUNT)
                change_new_value = convert_value_to_int(change.new_value, DEFAULT_MIN_COUNT)
                change_old_value = convert_value_to_int(change.old_value, DEFAULT_MIN_COUNT)
                result = (other_change_new_value - other_change_old_value) >= (change_new_value - change_old_value)
                break

    return result


def convert_value_to_int(value, default):
    return int(value) if value is not None else default


def condition_checker_queue_update_strategy_on_remove(change, patch):
    result = not patch.cluster.has_running_capacity()
    # Update of list element value is possible if one of the following is verified:
    # - fleet is stopped
    # - queue update strategy is set (different from default)
    # - new list element is added
    if is_slurm_queues_change(change):
        result = (
            result
            or is_queue_update_strategy_set(patch)
            or (isinstance(change.old_value, list) and all(value in change.new_value for value in change.old_value))
        )

    return result


def condition_checker_managed_placement_group(change, patch):
    if is_managed_placement_group_deletion(change, patch) and patch.cluster.has_running_capacity():
        result = False
    else:
        result = condition_checker_queue_update_strategy(change, patch)
    return result


def condition_checker_managed_fsx(change, patch):
    if unchanged_managed_fsx_lustre_names(change, patch):
        result = False
    else:
        result = condition_checker_queue_update_strategy_on_remove(change, patch)
    return result


def actions_needed_shared_storage_update(change, patch):
    if is_home_change(change):
        return "Please revert any changes to the /home mount"
    if is_awsbatch_scheduler(change, patch):
        return (
            f"Restore '{change.key}' value to '{change.old_value}'"
            if change.old_value is not None
            else "{0} the parameter '{1}'".format("Restore" if change.is_list else "Remove", change.key)
            + ". If you need this change, please consider creating a new cluster instead of updating the existing one."
        )
    actions = []
    if is_compute_fleet_stop_required_for_shared_storage_change(change, patch):
        actions.append(f"{UpdatePolicy.ACTIONS_NEEDED['compute_nodes_stop'](change, patch)}")
    if is_login_fleet_stop_required_for_shared_storage_change(change, patch):
        actions.append(f"{UpdatePolicy.ACTIONS_NEEDED['login_nodes_stop'](change, patch)}")

    return " ".join(actions)


def actions_needed_managed_fsx(change, patch):
    fsx_lustre_names = unchanged_managed_fsx_lustre_names(change, patch)
    if fsx_lustre_names:
        return (
            "If you intend to preserve the same file system or you want to create a new one please refer to the "
            "shared storage section in ParallelCluster user guide."
        )
    else:
        return actions_needed_queue_update_strategy(change, patch)


def condition_checker_shared_storage_update_policy(change, patch):
    """
    Check different requirements for different schedulers.

    Compute and login fleet stop is required for plugin scheduler.
    Update for awsbatch scheduler is not supported.
    QueueUpdateStrategy can override UpdatePolicy of parameters under SlurmQueues for slurm scheduler.
    """
    if is_home_change(change):
        return False
    if is_awsbatch_scheduler(change, patch):
        return False
    if patch.cluster.has_running_login_nodes() and not is_login_fleet_update_supported_for_shared_storage(change):
        return False
    if (
        patch.cluster.has_running_capacity()
        and not is_compute_fleet_update_supported_for_shared_storage(change)
        and not is_queue_update_strategy_set(patch)
    ):
        return False

    return True


def condition_checker_login_nodes_pools_policy(change, patch):
    """Login pools can be added but removal require LoginNodes stop."""
    result = not patch.cluster.has_running_login_nodes()
    if change.is_list and change.key == "Pools":
        result = result or (change.old_value is None and change.new_value is not None)

    return result


def condition_checker_login_nodes_stop_policy(_, patch):
    return not patch.cluster.has_running_login_nodes()


def condition_checker_login_nodes_pool_stop_policy(change, patch):
    """Check if login nodes are running in the pool in which update was requested."""
    pool_name = get_pool_name_from_change_paths(change)
    return not patch.cluster.has_running_login_nodes(pool_name=pool_name)


def get_pool_name_from_change_paths(change):
    """
    Return the name of the pool in which update was requested.

    Example path=['LoginNodes', 'Pools[pool2]', 'Ssh'].
    """
    for path in change.path:
        if re.search("Pools\\[", path):
            _, pool_name = extract_type_and_name_from_path(path)
            return pool_name
    return ""


# Common fail_reason messages
UpdatePolicy.FAIL_REASONS = {
    "ebs_volume_resize": "Updating the file system after a resize operation requires commands specific to your "
    "operating system.",
    "cookbook_update": lambda change, patch: (
        "Updating cookbook related parameter is not supported because it only "
        "applies updates to compute nodes. If you still want to proceed, first stop the compute fleet with the "
        "pcluster update-compute-fleet command and then run an update with the --force-update flag"
    ),
    "login_nodes_running": lambda change, patch: "The update is not supported when login nodes are running",
    "login_nodes_pool_running": lambda change, patch: (
        f"The update is not supported when login nodes in {get_pool_name_from_change_paths(change)} are running"
    ),
    "compute_or_login_nodes_running": lambda change, patch: (
        "The update is not supported when compute or login nodes are running"
    ),
}

# Common action_needed messages
UpdatePolicy.ACTIONS_NEEDED = {
    "ebs_volume_update": "Follow the instructions at {0}#{1} to modify your volume from AWS Console.".format(
        "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/requesting-ebs-volume-modifications.html",
        "modify-ebs-volume",
    ),
    "pcluster_stop": lambda change, patch: "Stop the compute fleet with the pcluster update-compute-fleet command",
    "pcluster_stop_conditional": actions_needed_queue_update_strategy,
    "pcluster_resize_conditional": actions_needed_resize_update_strategy_on_remove,
    "managed_placement_group": actions_needed_managed_placement_group,
    "shared_storage_update_conditional": actions_needed_shared_storage_update,
    "managed_fsx": actions_needed_managed_fsx,
    "compute_nodes_stop": lambda change, patch: (
        "Stop the compute fleet with the pcluster update-compute-fleet command"
        ", or set QueueUpdateStrategy in the configuration used for the 'update-cluster' operation."
    ),
    "login_nodes_stop": lambda change, patch: (
        "Stop the login nodes by setting Count parameter to 0 "
        "and update the cluster with the pcluster update-cluster command"
    ),
}

# Base policies

# Update is ignored
UpdatePolicy.IGNORED = UpdatePolicy(
    name="IGNORED",
    level=-10,
    fail_reason="-",
    condition_checker=(lambda change, patch: True),
    # Ignored changes are not shown
    print_succeeded=False,
)

# Update supported
UpdatePolicy.SUPPORTED = UpdatePolicy(
    name="SUPPORTED", level=0, fail_reason="-", condition_checker=(lambda change, patch: True)
)

# Checks resize of max_vcpus in Batch Compute Environment
UpdatePolicy.AWSBATCH_CE_MAX_RESIZE = UpdatePolicy(
    name="AWSBATCH_CE_MAX_RESIZE",
    level=1,
    fail_reason=lambda change, patch: "Max vCPUs can not be lower than the current Desired vCPUs ({0})".format(
        patch.cluster.get_running_capacity()
    ),
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_stop"],
    condition_checker=lambda change, patch: patch.cluster.get_running_capacity()
    <= patch.target_config["Scheduling"]["AwsBatchQueues"][0]["ComputeResources"][0]["MaxvCpus"],
)

# Update supported only with all compute nodes down or with replacement policy set different from COMPUTE_FLEET_STOP
UpdatePolicy.QUEUE_UPDATE_STRATEGY = UpdatePolicy(
    name="QUEUE_UPDATE_STRATEGY",
    level=5,
    fail_reason=fail_reason_queue_update_strategy,
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_stop_conditional"],
    condition_checker=condition_checker_queue_update_strategy,
)

# Update supported with fleet stopped or with replacement policy set to TERMINATE
UpdatePolicy.RESIZE_UPDATE_STRATEGY_ON_REMOVE = UpdatePolicy(
    name="RESIZE_UPDATE_STRATEGY_ON_REMOVE",
    level=5,
    fail_reason=fail_reason_resize_update_strategy_on_remove,
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_resize_conditional"],
    condition_checker=condition_checker_resize_update_strategy_on_remove,
)

# We must force COMPUTE_FLEET_STOP for the deletion of managed groups, otherwise fall back to QUEUE_UPDATE_STRATEGY
UpdatePolicy.MANAGED_PLACEMENT_GROUP = UpdatePolicy(
    name="MANAGED_PLACEMENT_GROUP",
    level=5,
    fail_reason=fail_reason_managed_placement_group,
    action_needed=UpdatePolicy.ACTIONS_NEEDED["managed_placement_group"],
    condition_checker=condition_checker_managed_placement_group,
)

# Update policy for updating SharedStorage
UpdatePolicy.SHARED_STORAGE_UPDATE_POLICY = UpdatePolicy(
    name="SHARED_STORAGE_UPDATE_POLICY",
    level=6 if not is_awsbatch_scheduler else 1000,
    fail_reason=fail_reason_shared_storage_update_policy,
    action_needed=UpdatePolicy.ACTIONS_NEEDED["shared_storage_update_conditional"],
    condition_checker=condition_checker_shared_storage_update_policy,
)

# Update supported on new addition or on removal only with all compute nodes down
UpdatePolicy.COMPUTE_FLEET_STOP_ON_REMOVE = UpdatePolicy(
    name="COMPUTE_FLEET_STOP_ON_REMOVE",
    level=7,
    fail_reason="All compute nodes must be stopped",
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_stop"],
    condition_checker=condition_checker_compute_fleet_stop_on_remove,
)

# Update supported only with all compute nodes down
UpdatePolicy.COMPUTE_FLEET_STOP = UpdatePolicy(
    name="COMPUTE_FLEET_STOP",
    level=10,
    fail_reason="All compute nodes must be stopped",
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_stop"],
    condition_checker=lambda change, patch: not patch.cluster.has_running_capacity(),
)

# Update supported only with head node down
UpdatePolicy.HEAD_NODE_STOP = UpdatePolicy(
    name="HEAD_NODE_STOP",
    level=20,
    fail_reason="To perform this update action, the head node must be in a stopped state",
    action_needed=UpdatePolicy.ACTIONS_NEEDED["pcluster_stop"],
    condition_checker=lambda change, patch: patch.cluster.head_node_instance.state == "stopped",
)

# Expected Behavior:
# No bucket specified when create, no bucket specified when update: Display no diff, proceed with update
# For all other cases: Display diff and block, value will not be updated even if forced
UpdatePolicy.READ_ONLY_RESOURCE_BUCKET = UpdatePolicy(
    name="READ_ONLY_RESOURCE_BUCKET",
    level=30,
    fail_reason=lambda change, patch: (
        "'{0}' parameter is a read only parameter that cannot be updated. "
        "New value '{1}' will be ignored and old value '{2}' will be used if you force the update."
    ).format(change.key, change.new_value, change.old_value),
    action_needed=lambda change, patch: f"Restore the value of parameter '{change.key}' to '{change.old_value}'",
    condition_checker=lambda change, patch: False,
    # We don't want to show the change if allowed (e.g local value is empty)
    print_succeeded=False,
)

# Update effects are unknown.
# WARNING: This is the default value for all new configuration parameters. All parameters must be linked to a specific
# update policy instead of UNKNOWN to pass unit tests.
#
UpdatePolicy.UNKNOWN = UpdatePolicy(
    name="UNKNOWN",
    level=100,
    fail_reason="Update currently not supported",
    action_needed="Restore the previous parameter value for the unsupported changes.",
)

# Update not supported
UpdatePolicy.UNSUPPORTED = UpdatePolicy(
    name="UNSUPPORTED",
    level=1000,
    fail_reason=lambda change, patch: (f"Update actions are not currently supported for the '{change.key}' parameter"),
    action_needed=lambda change, patch: (
        f"Restore '{change.key}' value to '{change.old_value}'"
        if change.old_value is not None
        else "{0} the parameter '{1}'".format("Restore" if change.is_list else "Remove", change.key)
    )
    + ". If you need this change, please consider creating a new cluster instead of updating the existing one.",
)

# Block update if cluster has a managed Fsx for Lustre FileSystem, otherwise fallback to QueueUpdateStrategy
UpdatePolicy.MANAGED_FSX = UpdatePolicy(
    name="MANAGED_FSX",
    level=6,
    fail_reason=fail_reason_managed_fsx,
    action_needed=UpdatePolicy.ACTIONS_NEEDED["managed_fsx"],
    condition_checker=condition_checker_managed_fsx,
)

# Update policy for adding or removing a login node pool
UpdatePolicy.LOGIN_NODES_POOLS = UpdatePolicy(
    name="LOGIN_NODES_POOLS_UPDATE_POLICY",
    level=6,
    condition_checker=condition_checker_login_nodes_pools_policy,
    fail_reason=UpdatePolicy.FAIL_REASONS["login_nodes_running"],
    action_needed=UpdatePolicy.ACTIONS_NEEDED["login_nodes_stop"],
)

# Update supported only with all login nodes in the cluster down
UpdatePolicy.LOGIN_NODES_STOP = UpdatePolicy(
    name="LOGIN_NODES_STOP",
    level=10,
    condition_checker=condition_checker_login_nodes_stop_policy,
    fail_reason=UpdatePolicy.FAIL_REASONS["login_nodes_running"],
    action_needed=UpdatePolicy.ACTIONS_NEEDED["login_nodes_stop"],
)

# Update supported only with all login nodes in the pool down
UpdatePolicy.LOGIN_NODES_POOL_STOP = UpdatePolicy(
    name="LOGIN_NODES_POOL_STOP",
    level=10,
    condition_checker=condition_checker_login_nodes_pool_stop_policy,
    fail_reason=UpdatePolicy.FAIL_REASONS["login_nodes_pool_running"],
    action_needed=UpdatePolicy.ACTIONS_NEEDED["login_nodes_stop"],
)

# Update supported only with all computre and login nodes down
UpdatePolicy.COMPUTE_AND_LOGIN_NODES_STOP = UpdatePolicy(
    name="COMPUTE_AND_LOGIN_NODES_STOP",
    level=10,
    condition_checker=lambda change, patch: (
        UpdatePolicy.COMPUTE_FLEET_STOP.condition_checker(change, patch)
        and UpdatePolicy.LOGIN_NODES_STOP.condition_checker(change, patch)
    ),
    fail_reason=UpdatePolicy.FAIL_REASONS["compute_or_login_nodes_running"],
    action_needed=lambda change, patch: "{}. {}".format(
        UpdatePolicy.COMPUTE_FLEET_STOP.action_needed(change, patch),
        UpdatePolicy.LOGIN_NODES_STOP.action_needed(change, patch),
    ),
)
