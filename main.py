import kubernetes as k8s
from argparse import ArgumentParser
from pint import UnitRegistry
from collections import defaultdict
import json
import datetime
import time
import os

def evict_pod(data, gracePeriodSeconds, dry_run, evictAllPodsAtOnce):
    isPodEvicted = False
    for node in data["on-demand"]:
        for pod in node["pods"]:
            if pod.get("spot-schedulable") and pod.get("has-free-capacity"):
                print("[%s][%s][%s][%s] Evicting" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), node["spec"].metadata.name, pod["spec"].metadata.namespace, pod["spec"].metadata.name))
                if not dry_run:
                    # core_v1.delete_namespaced_pod(
                    #     name=pod["spec"].metadata.name,
                    #     namespace=pod["spec"].metadata.namespace,
                    #     grace_period_seconds=gracePeriodSeconds
                    # )
                    body = k8s.client.V1beta1Eviction(
                        metadata=k8s.client.V1ObjectMeta(
                            name=pod["spec"].metadata.name,
                            namespace=pod["spec"].metadata.namespace
                        ),
                        delete_options=k8s.client.V1DeleteOptions(
                            grace_period_seconds=gracePeriodSeconds
                        )
                    )
                    try:
                        core_v1.create_namespaced_pod_eviction(
                            name=pod["spec"].metadata.name,
                            namespace=pod["spec"].metadata.namespace,
                            body=body
                        )
                        isPodEvicted = True
                    except k8s.client.rest.ApiException as e:
                        errBody = json.loads(e.body)
                        if errBody["code"] == 429 and errBody["details"]["causes"][0]["reason"] == 'DisruptionBudget':
                            print("[%s][%s][%s][%s] %s" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), node["spec"].metadata.name, pod["spec"].metadata.namespace, pod["spec"].metadata.name, errBody["details"]["causes"][0]["message"]))
                        else:
                            print("[%s][%s][%s][%s] Exception when calling CoreV1Api->create_namespaced_pod_template: %s\n" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), node["spec"].metadata.name, pod["spec"].metadata.namespace, pod["spec"].metadata.name, e))
                            # exit(1)
        if isPodEvicted and not evictAllPodsAtOnce:
            return isPodEvicted
    if not isPodEvicted:
        print("[%s] No pod evicted" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ")))
    return isPodEvicted

def evaluate_spot_node_free_space(data):
    isOnDemandNodeCalculated = False
    for node in data["on-demand"]:
        for pod in node["pods"]:
            if pod.get("spot-schedulable"):
                for idx, spotNode in enumerate(data["spot"]):
                    if (spotNode["node-stats"]["cpu_free"] - pod["cpu_req"]).to('dimensionless') > 0 and \
                        (spotNode["node-stats"]["mem_free"] - pod["mem_req"]).to('dimensionless') > 0:
                        spotNode["node-stats"]["cpu_free"] = (spotNode["node-stats"]["cpu_free"] - pod["cpu_req"]).to('dimensionless')
                        spotNode["node-stats"]["mem_free"] = (spotNode["node-stats"]["mem_free"] - pod["mem_req"]).to('dimensionless')
                        pod["has-free-capacity"] = True
                        isOnDemandNodeCalculated = True
                        print("[%s][%s][%s][%s] can be scheduled to %s" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), node["spec"].metadata.name, pod["spec"].metadata.namespace, pod["spec"].metadata.name, spotNode["spec"].metadata.name))
                        break
                    elif idx == len(data["spot"]) - 1:
                        print("[%s][%s][%s] Unreschedulable due to insufficient free CPU (%s)/memory capacity (%s) in all spot nodes." % (spotNode["name"], pod["spec"].metadata.namespace, pod["spec"].metadata.name, pod["cpu_req"], pod["mem_req"]))
        if isOnDemandNodeCalculated == True:
            return data
    return data

def is_node_affinity_required_on_demand(nodeName, pod, onDemandWorkerNodeLabel, onDemandLifecycleNodeLabel):
    onDemandWorkerNodeLabelKey = onDemandWorkerNodeLabel.split('=')[0]
    onDemandWorkerNodeLabelValue = onDemandWorkerNodeLabel.split('=')[1]
    onDemandLifecycleNodeLabelKey = onDemandLifecycleNodeLabel.split('=')[0]
    onDemandLifecycleNodeLabelValue = onDemandLifecycleNodeLabel.split('=')[1]
    try:
        for nodeSelectorTerm in pod["spec"].spec.affinity.node_affinity.required_during_scheduling_ignored_during_execution.node_selector_terms:
            for matchExpression in nodeSelectorTerm.match_expressions:
                if 'In' in matchExpression.operator:
                    if 'metadata.name' in matchExpression.key and \
                        matchExpression.values == nodeName:
                        return True
                    elif onDemandWorkerNodeLabelKey in matchExpression.key and \
                        onDemandWorkerNodeLabelValue in matchExpression.values:
                        return True
                    elif onDemandLifecycleNodeLabelKey in matchExpression.key and \
                        onDemandLifecycleNodeLabelValue in matchExpression.values:
                        return True
                elif 'Exists' in matchExpression.operator and \
                    onDemandWorkerNodeLabelKey in matchExpression.key:
                    return True
        return False
    except AttributeError:
        # print("[%s][%s][%s] AttributeError" % (node["name"], pod["spec"].metadata.namespace, pod["spec"].metadata.name))
        return False


def evaluate_evict_Pod_Candidates(data, onDemandWorkerNodeLabel, onDemandLifecycleNodeLabel, SafeToEvictAnnotation, deleteNonReplicatedPods):
    onDemandWorkerNodeLabelKey = onDemandWorkerNodeLabel.split('=')[0]
    onDemandWorkerNodeLabelValue = onDemandWorkerNodeLabel.split('=')[1]
    onDemandLifecycleNodeLabelKey = onDemandLifecycleNodeLabel.split('=')[0]
    onDemandLifecycleNodeLabelValue = onDemandLifecycleNodeLabel.split('=')[1]
    for node in data["on-demand"]:
        if node["spec"].metadata.annotations.get(SafeToEvictAnnotation) == "false":
            continue
        for pod in node["pods"]:
            ### What types of pods can prevent CA from removing a node?
            ### [To Do]- Pods with restrictive PodDisruptionBudget.
            ### [To Do]- Kube-system pods that:
            ### [To Do]  - are not run on the node by default, *
            ### [To Do]  - don't have a pod disruption budget set or their PDB is too restrictive (since CA 0.6).
            ### - Pods that are not backed by a controller object (so not created by deployment, replica set, job, stateful set etc). *
            ### [To Do]- Pods with local storage. *
            ### - Pods that cannot be moved elsewhere due to various constraints (lack of resources, non-matching node selectors or affinity, matching anti-affinity, etc)
            ### - Pods that have annotation: "k8s-spot-rescheduler/safe-to-evict": "false"
            if getattr(pod["spec"].metadata, "owner_references", None) is None:
                if deleteNonReplicatedPods:
                    continue
            elif pod["spec"].metadata.owner_references[0].kind == "DaemonSet":
                continue
            if is_node_affinity_required_on_demand(
                nodeName=node["spec"].metadata.name,
                pod=pod,
                onDemandWorkerNodeLabel=onDemandWorkerNodeLabel,
                onDemandLifecycleNodeLabel=onDemandLifecycleNodeLabel
            ):
                continue
            if pod["spec"].spec.node_selector:
                if pod["spec"].spec.node_selector.get(onDemandWorkerNodeLabelKey) == onDemandWorkerNodeLabelValue or \
                    pod["spec"].spec.node_selector.get(onDemandLifecycleNodeLabelKey) == onDemandLifecycleNodeLabelValue or \
                    pod["spec"].spec.node_selector.get('kubernetes.io/hostname') == node["spec"].metadata.name:
                    continue
            if pod["spec"].metadata.annotations.get(SafeToEvictAnnotation) == "false":
                continue
            pod["spot-schedulable"] = True
            # print("[%s][%s][%s][%s] is eligible for spot instance" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), node["spec"].metadata.name, pod["spec"].metadata.namespace, pod["spec"].metadata.name))
    return data

def compute_allocated_resources(spotWorkerNodeLabel, onDemandWorkerNodeLabel):
    labelSelectors = {"spot": spotWorkerNodeLabel, "on-demand": onDemandWorkerNodeLabel}
    ureg = UnitRegistry()
    ureg.load_definitions('kubernetes_units.txt')

    Q_   = ureg.Quantity
    data = {}

    for instanceType in labelSelectors:
        data[instanceType] = []
        for node in core_v1.list_node(label_selector=labelSelectors[instanceType]).items:
            stats          = {}
            node_name      = node.metadata.name
            allocatable    = node.status.allocatable
            max_pods       = int(int(allocatable["pods"]) * 1.5)
            field_selector = ("status.phase!=Succeeded,status.phase!=Failed," +
                            "spec.nodeName=" + node_name)

            stats["cpu_alloc"] = Q_(allocatable["cpu"])
            stats["mem_alloc"] = Q_(allocatable["memory"])

            pods = core_v1.list_pod_for_all_namespaces(limit=max_pods,
                                                    field_selector=field_selector).items
            clusterOverprovisionerStats = {
                "cpu_req": 0,
                "mem_req": 0
            }
            # compute the allocated resources
            cpureqs,cpulmts,memreqs,memlmts = [], [], [], []
            podsStats = []
            for pod in pods:
                podCpuReqs = []
                podMemReqs = []
                for container in pod.spec.containers:
                    res  = container.resources
                    reqs = defaultdict(lambda: 0, res.requests or {})
                    lmts = defaultdict(lambda: 0, res.limits or {})
                    cpureqs.append(Q_(reqs["cpu"]))
                    memreqs.append(Q_(reqs["memory"]))
                    cpulmts.append(Q_(lmts["cpu"]))
                    memlmts.append(Q_(lmts["memory"]))
                    podCpuReqs.append(Q_(reqs["cpu"]))
                    podMemReqs.append(Q_(reqs["memory"]))
                podsStats.append({
                    "cpu_req": sum(podCpuReqs),
                    "mem_req": sum(podMemReqs),
                    "spec": pod,
                    "name": pod.metadata.name
                })
                if pod.metadata.labels and pod.metadata.labels.get("app.kubernetes.io/instance") == "cluster-overprovisioner":
                    clusterOverprovisionerStats = {
                        "cpu_req": clusterOverprovisionerStats["cpu_req"] + sum(podCpuReqs),
                        "mem_req": clusterOverprovisionerStats["mem_req"] + sum(podMemReqs)
                    }
            podsStats.sort(key=lambda x: x["cpu_req"], reverse=True)

            stats["cpu_req"]     = sum(cpureqs)
            stats["cpu_lmt"]     = sum(cpulmts)
            stats["cpu_req_per"] = (stats["cpu_req"] / stats["cpu_alloc"] * 100).to('dimensionless')
            stats["cpu_lmt_per"] = (stats["cpu_lmt"] / stats["cpu_alloc"] * 100).to('dimensionless')

            stats["mem_req"]     = sum(memreqs)
            stats["mem_lmt"]     = sum(memlmts)
            stats["mem_req_per"] = (stats["mem_req"] / stats["mem_alloc"] * 100).to('dimensionless')
            stats["mem_lmt_per"] = (stats["mem_lmt"] / stats["mem_alloc"] * 100).to('dimensionless')
            stats["cpu_free"] = stats["cpu_alloc"] - stats["cpu_req"] + clusterOverprovisionerStats["cpu_req"]
            stats["mem_free"] = stats["mem_alloc"] - stats["mem_req"] + clusterOverprovisionerStats["mem_req"]
            dataEntry = {
                "name": node_name,
                "spec": node,
                "node-stats": stats,
                "pods": podsStats
            }
            data[instanceType].append(dataEntry)

        ### The effect of this algorithm should be, that we take the emptiest on demand nodes first
        ### and empty those before we empty a node which is busier, thus resulting in the highest number
        ### of 'empty' nodes that can be removed from the cluster.

        ### Sort on-demand instances by least requested CPU
        if instanceType == "on-demand":
            data[instanceType].sort(key=lambda x: x["node-stats"]["cpu_req"])
        ### Sort spot instances by most free CPU
        else:
            data[instanceType].sort(key=lambda x: x["node-stats"]["cpu_free"], reverse=True)
    return data

if __name__ == '__main__':
    # parser = ArgumentParser()
    # parser.add_argument("--dry-run", help="(default: false) Dry run", action='store_true')
    # parser.add_argument("--housekeeping-interval", help=" (default: 60s): How often rescheduler takes actions (seconds).", default="60")
    # parser.add_argument("--cooldown-interval", help="Cool down interval if any pod evicted", default="600")
    # parser.add_argument("--spot-worker-node-label", help="(default: k8s-node-role/spot-worker=true) Name of label on nodes to be considered as targets for pods.", default='k8s-node-role/spot-worker=true')
    # parser.add_argument("--on-demand-worker-node-label", help="(default: k8s-node-role/on-demand-worker=true) Name of label on nodes to be considered for draining.", default='k8s-node-role/on-demand-worker=true')
    # parser.add_argument("--on-demand-lifecycle-node-label", help="(default: k8s-node-lifecycle=on-demand) Name of label on nodes to be considered for draining.", default='k8s-node-lifecycle=on-demand')
    # parser.add_argument("--max-graceful-termination", help="(default: 120s): How long should the rescheduler wait for pods to shutdown gracefully before failing the node drain attempt.", default='120')
    # parser.add_argument("--delete-non-replicated-pods", help="(default: false) Delete non-replicated pods running on on-demand instance. Note that some non-replicated pods will not be rescheduled.", action='store_true')
    # parser.add_argument("--safe-to-evict-annotation", help="(default: k8s-spot-rescheduler/safe-to-evict) Pods with value \"false\" of safe-to-evict annotation will not be evicted.", default='k8s-spot-rescheduler/safe-to-evict')
    # args = parser.parse_args()
    # parser.add_argument("--evict-all-pods-at-once", help="(default: false) Evict all pods on all on-demand instances at once instead of waiting cooldown interval between nodes", action='store_true')
    # args = parser.parse_args()

    dryRun = True if os.environ.get("DRY_RUN") == "true" else False
    housekeepingInterval = int(str(os.environ.get("HOUSEKEEPING_INTERVAL")).replace('s', '')) if os.environ.get("HOUSEKEEPING_INTERVAL") else 60
    cooldownInterval = int(str(os.environ.get("COOLDOWN_INTERVAL")).replace('s', '')) if os.environ.get("COOLDOWN_INTERVAL") else 600
    spotWorkerNodeLabel = os.environ.get("SPOT_WORKER_NODE_LABEL") if os.environ.get("SPOT_WORKER_NODE_LABEL") else 'k8s-node-role/spot-worker=true'
    onDemandWorkerNodeLabel = os.environ.get("ON_DEMAND_WORKER_NODE_LABEL") if os.environ.get("ON_DEMAND_WORKER_NODE_LABEL") else 'k8s-node-role/on-demand-worker=true'
    onDemandLifecycleNodeLabel = os.environ.get("ON_DEMAND_LIFECYCLE_NODE_LABEL") if os.environ.get("ON_DEMAND_LIFECYCLE_NODE_LABEL") else 'k8s-node-role/on-demand-worker=true'
    gracePeriodSeconds = int(str(os.environ.get("GRACEFUL_PERIOD_SECONDS")).replace('s', '')) if os.environ.get("GRACEFUL_PERIOD_SECONDS") else 120
    deleteNonReplicatedPods = True if os.environ.get("DELETE_NON_REPLICATED_PODS") == "true" else False
    safeToEvictAnnotation = os.environ.get("SAFE_TO_EVICT_ANNOATATION") if os.environ.get("SAFE_TO_EVICT_ANNOATATION") else 'k8s-spot-rescheduler/safe-to-evict'
    evictAllPodsAtOnce = True if os.environ.get("EVICT_ALL_PODS_AT_ONCE") == "true" else False

    # doing this computation within a k8s cluster
    k8s.config.load_incluster_config()
    core_v1 = k8s.client.CoreV1Api()

    while True:
        print("[%s] Scanning pods." % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ")))
        data = compute_allocated_resources(
            spotWorkerNodeLabel=spotWorkerNodeLabel,
            onDemandWorkerNodeLabel=onDemandWorkerNodeLabel
        )
        data = evaluate_evict_Pod_Candidates(
            data,
            onDemandWorkerNodeLabel=onDemandWorkerNodeLabel,
            onDemandLifecycleNodeLabel=onDemandLifecycleNodeLabel,
            SafeToEvictAnnotation=safeToEvictAnnotation,
            deleteNonReplicatedPods=deleteNonReplicatedPods
        )
        data = evaluate_spot_node_free_space(data)
        isPodEvicted = evict_pod(
            data,
            gracePeriodSeconds=gracePeriodSeconds,
            dry_run=dryRun,
            evictAllPodsAtOnce=evictAllPodsAtOnce
        )
        if isPodEvicted:
            print("[%s] Cooling down for %ss" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), cooldownInterval))
            time.sleep(cooldownInterval)
        else:
            print("[%s] Next housekeeping in %ss" % (datetime.datetime.now().strftime("%Y/%m/%dT%H:%M:%S.%fZ"), housekeepingInterval))