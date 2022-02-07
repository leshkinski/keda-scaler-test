## Overview

This repo contains tests using Keda to scale a deployment according to a cron schedule and a custom metric.

## Setting Up

### Application Insights

In order to run the tests, you will need to create an Application Insights instance. The Terraform code in the deploy 
directory will create an Application Insights Instance.

Set up a service principal for Terraform. See [this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/guides/service_principal_client_secret) page
for details.

Create the Application Insights instance as follows:
```
make ENV=${SOME_UNIQUE_NAME} plan apply
```

When you're done, remove the instance with:
```
make ENV=${SOME_UNIQUE_NAME} destroy
```

### Kubernetes Cluster

A Kubernetes cluster is also required to run these tests. [Minikube](https://minikube.sigs.k8s.io/docs/) is an easy way to deploy a cluster
on your computer if you don't have access to a cluster.

Once you have a cluster, install Keda. See [this](https://keda.sh/docs/2.6/deploy/#install) page for details.

## Running the Tests

Ensure your kubectl context is set to the cluster to test against and run:
```
make test
```
