# K8s Spot Rescheduler

## Table of contents
- [K8s Spot Rescheduler](#k8s-spot-rescheduler)
  - [Table of contents](#table-of-contents)
  - [Introduction](#introduction)
  - [Motivation](#motivation)
  - [Usage](#usage)
      - [Requirements](#requirements)
  - [Scope of the project](#scope-of-the-project)
    - [Does](#does)
    - [Does not](#does-not)
  - [Operating logic](#operating-logic)

## Introduction
k8s-spot-rescheduler will reschedule pods that are already running on on-demand instances but actually don't REQUIRE running on on-demand instances. Based on worker labels it will evict pods from on-demand instances.

## Motivation
We used to use [pusher/k8s-spot-rescheduler](https://github.com/pusher/k8s-spot-rescheduler) but it is updated for a while and now it is not compatible with Kubernetes 1.16 or later version, so it is a pod rescheduler that porting some [pusher/k8s-spot-rescheduler](https://github.com/pusher/k8s-spot-rescheduler) features in pythin to replace it.

## Usage
#### Requirements

For the k8s Pod Rescheduler to process nodes as expected; you will need identifying labels which can be passed to the program to allow it to distinguish which nodes it should consider as on-demand and which it should consider as spot instances.

For instance you could add labels `k8s-node-role/on-demand-worker` and `k8s-node-role/spot-worker` to your on-demand and spot instances respectively.

You should also add the `PreferNoSchedule` taint to your on-demand instances to ensure that the scheduler prefers spot instances when making it's scheduling decisions.

For example you could add the following flags to your Kubelet:
```
--register-with-taints="k8s-node-role/on-demand-worker=true:PreferNoSchedule"
--node-labels="k8s-node-role/on-demand-worker=true"
```

## Scope of the project
### Does
* Look for Pods on on-demand instances
* Look for space for Pods on spot instances
* Checks whether there is enough capacity to move all pods on the on-demand node to spot nodes
* Evicts all pods on the node if the previous check passes
* Leaves the node in a schedulable state - in case it's capacity is required again


### Does not
* Schedule pods (The default scheduler handles this)
* Scale down empty nodes on your cloud provider (Try the [Cluster Autoscaler](https://github.com/kubernetes/autoscaler/tree/master/cluster-autoscaler))

## Operating logic

The rescheduler logic roughly follows the below:

1. Gets a list of on-demand and spot nodes and their respective Pods
  * Builds a map of nodeInfo structs
    * Add node to struct
    * Add pods for that node to struct
    * Add requested and free CPU fields to struct
  * Map these structs based on whether they are on-demand or spot instances.
  * Sort on-demand instances by least requested CPU
  * Sort spot instances by most free CPU
2. Iterate through each on-demand node and try to drain it
  * Iterate through each pod
    * Determine if a spot node has space for the pod
    * Add the pod to the prospective spot node
    * Move onto next node if no spot node space available
  * Drain the node
    * Iterate through pods and evict them in turn
      * Evict pod
      * Wait for deletion and reschedule
    * Cancel all further processing

This process is repeated every `housekeeping-interval` seconds.

The effect of this algorithm should be, that we take the emptiest nodes first and empty those before we empty a node which is busier, thus resulting in the highest number of 'empty' nodes that can be removed from the cluster.
