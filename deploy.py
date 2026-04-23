#!/usr/bin/env python
from agentscope_runtime.engine.deployers.kubernetes_deployer import K8sConfig, KubernetesDeployManager
from agentscope_runtime.engine.deployers.utils.docker_image_utils.docker_image_builder import RegistryConfig
from asyncio import run
from main import app

async def deploy2K8s():
	'''将 AgentApp 部署到 Kubernetes'''
	deployer = KubernetesDeployManager(K8sConfig(k8s_namespace = 'agentscope-runtime', kubeconfig_path = None), RegistryConfig(registry_url = 'localhost'))
	return await app.deploy(deployer, base_image = 'python:3', platform = 'linux/amd64', port = 8080, runtime_config = {
		'image_pull_policy': 'Always',
	}), deployer

if __name__ == '__main__':
	run(deploy2K8s())
