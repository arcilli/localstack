"""Module for localstack internal resources, such as health, graph, or _localstack/cloudformation/deploy. """
import json
import logging
import os
from collections import defaultdict
from typing import List, Optional

import requests
from werkzeug.exceptions import NotFound

from localstack import config, constants
from localstack.deprecations import deprecated_endpoint
from localstack.http import Request, Response, Router
from localstack.http.adapters import RouterListener
from localstack.http.dispatcher import resource_dispatcher
from localstack.services.infra import SHUTDOWN_INFRA, terminate_all_processes_in_docker
from localstack.utils.collections import merge_recursive
from localstack.utils.files import load_file
from localstack.utils.functions import call_safe
from localstack.utils.json import parse_json_or_yaml
from localstack.utils.server.http2_server import HTTP_METHODS

LOG = logging.getLogger(__name__)


class DeprecatedResource:
    """
    Resource class which wraps a given resource in the deprecated_endpoint (i.e. logs deprecation warnings on every
    invocation).
    """

    def __init__(self, resource, previous_path: str, deprecation_version: str, new_path: str):
        for http_method in HTTP_METHODS:
            fn_name = f"on_{http_method.lower()}"
            fn = getattr(resource, fn_name, None)
            if fn:
                wrapped = deprecated_endpoint(
                    fn,
                    previous_path=previous_path,
                    deprecation_version=deprecation_version,
                    new_path=new_path,
                )
                setattr(self, fn_name, wrapped)


class HealthResource:
    """
    Resource for the LocalStack /health endpoint. It provides access to the service states and other components of
    localstack. We support arbitrary data to be put into the health state to support things like the
    run_startup_scripts function in docker-entrypoint.sh which sets the status of the init scripts feature.
    """

    def __init__(self, service_manager) -> None:
        super().__init__()
        self.service_manager = service_manager
        self.state = {}

    def on_post(self, request: Request):
        data = request.get_json(True, True)
        if not data:
            return Response("invalid request", 400)

        # backdoor API to support restarting the instance
        if data.get("action") in ["kill", "restart"]:
            terminate_all_processes_in_docker()
            SHUTDOWN_INFRA.set()

        return Response("ok", 200)

    def on_get(self, request: Request):
        path = request.path

        reload = "reload" in path

        # get service state
        if reload:
            self.service_manager.check_all()
        services = {
            service: state.value for service, state in self.service_manager.get_states().items()
        }

        # build state dict from internal state and merge into it the service states
        result = dict(self.state)
        result = merge_recursive({"services": services}, result)
        result["version"] = constants.VERSION
        return result

    def on_head(self, _request: Request):
        return Response("ok", 200)

    def on_put(self, request: Request):
        data = request.get_json(True, True) or {}

        # keys like "features:initScripts" should be interpreted as ['features']['initScripts']
        state = defaultdict(dict)
        for k, v in data.items():
            if ":" in k:
                path = k.split(":")
            else:
                path = [k]

            d = state
            for p in path[:-1]:
                d = state[p]
            d[path[-1]] = v

        self.state = merge_recursive(state, self.state, overwrite=True)
        return {"status": "OK"}


class CloudFormationUi:
    def on_get(self, request):
        from localstack.utils.aws.aws_stack import get_valid_regions

        deploy_html_file = os.path.join(
            constants.MODULE_MAIN_PATH, "services", "cloudformation", "deploy.html"
        )
        deploy_html = load_file(deploy_html_file)
        req_params = request.values
        params = {
            "stackName": "stack1",
            "templateBody": "{}",
            "errorMessage": "''",
            "regions": json.dumps(sorted(list(get_valid_regions()))),
        }

        download_url = req_params.get("templateURL")
        if download_url:
            try:
                LOG.debug("Attempting to download CloudFormation template URL: %s", download_url)
                template_body = requests.get(download_url).text
                template_body = parse_json_or_yaml(template_body)
                params["templateBody"] = json.dumps(template_body)
            except Exception as e:
                msg = f"Unable to download CloudFormation template URL: {e}"
                LOG.info(msg)
                params["errorMessage"] = json.dumps(msg.replace("\n", " - "))

        # using simple string replacement here, for simplicity (could be replaced with, e.g., jinja)
        for key, value in params.items():
            deploy_html = deploy_html.replace(f"<{key}>", value)

        return Response(deploy_html, mimetype="text/html")


class DiagnoseResource:
    def on_get(self, request):
        from localstack.utils import diagnose

        return {
            "version": {
                "image-version": call_safe(diagnose.get_docker_image_details),
                "localstack-version": call_safe(diagnose.get_localstack_version),
                "host": {
                    "kernel": call_safe(diagnose.get_host_kernel_version),
                },
            },
            "services": call_safe(diagnose.get_service_stats),
            "config": call_safe(diagnose.get_localstack_config),
            "docker-inspect": call_safe(diagnose.inspect_main_container),
            "docker-dependent-image-hashes": call_safe(diagnose.get_important_image_hashes),
            "file-tree": call_safe(diagnose.get_file_tree),
            "important-endpoints": call_safe(diagnose.resolve_endpoints),
            "logs": call_safe(diagnose.get_localstack_logs),
        }


class PluginsResource:
    """
    Resource to list information about plux plugins.
    """

    def on_get(self, request):
        from plugin import PluginManager

        from localstack.runtime import hooks
        from localstack.services.plugins import SERVICE_PLUGINS

        plugin_managers: List[PluginManager] = [
            SERVICE_PLUGINS.plugin_manager,
            hooks.configure_localstack_container.manager,
            hooks.prepare_host.manager,
            hooks.on_infra_ready.manager,
            hooks.on_infra_start.manager,
        ]

        def get_plugin_details(_manager: PluginManager, _name: str):
            container = _manager.get_container(_name)

            details = {
                "name": _name,
                "is_initialized": container.is_init,
                "is_loaded": container.is_loaded,
            }

            return details

        return {
            manager.namespace: [get_plugin_details(manager, name) for name in manager.list_names()]
            for manager in plugin_managers
        }


class InitScriptsResource:
    def on_get(self, request):
        from localstack.runtime.init import init_script_manager

        manager = init_script_manager()

        return {
            "completed": {
                stage.name: completed for stage, completed in manager.stage_completed.items()
            },
            "scripts": [
                {
                    "stage": script.stage.name,
                    "name": os.path.basename(script.path),
                    "state": script.state.name,
                }
                for scripts in manager.scripts.values()
                for script in scripts
            ],
        }


class InitScriptsStageResource:
    def on_get(self, request, stage: str):
        from localstack.runtime.init import Stage, init_script_manager

        manager = init_script_manager()

        try:
            stage = Stage[stage.upper()]
        except KeyError as e:
            raise NotFound(f"no such stage {stage}") from e

        return {
            "completed": manager.stage_completed.get(stage),
            "scripts": [
                {
                    "stage": script.stage.name,
                    "name": os.path.basename(script.path),
                    "state": script.state.name,
                }
                for script in manager.scripts.get(stage)
            ],
        }


class LocalstackResources(Router):
    """
    Router for localstack-internal HTTP resources.
    """

    def __init__(self):
        super().__init__(dispatcher=resource_dispatcher(pass_response=False))
        self.add_default_routes()
        # TODO: load routes as plugins

    def add_default_routes(self):
        from localstack.services.plugins import SERVICE_PLUGINS

        health_resource = HealthResource(SERVICE_PLUGINS)
        plugins_resource = PluginsResource()

        # special route for legacy support (before `/_localstack` was introduced)
        super().add(
            "/health",
            DeprecatedResource(
                health_resource,
                previous_path="/health",
                deprecation_version="1.3.0",
                new_path="/_localstack/health",
            ),
        )

        self.add("/health", health_resource)
        self.add("/plugins", plugins_resource)
        self.add("/init", InitScriptsResource())
        self.add("/init/<stage>", InitScriptsStageResource())
        self.add("/cloudformation/deploy", CloudFormationUi())

        if config.DEBUG:
            LOG.warning(
                "Enabling diagnose endpoint, "
                "please be aware that this can expose sensitive information via your network."
            )
            self.add("/diagnose", DiagnoseResource())

    def add(self, path, *args, **kwargs):
        super().add(f"{constants.INTERNAL_RESOURCE_PATH}{path}", *args, **kwargs)


class LocalstackResourceHandler(RouterListener):
    """
    Adapter to serve LocalstackResources through the edge proxy.
    """

    resources: LocalstackResources

    def __init__(self, resources: LocalstackResources = None) -> None:
        super().__init__(resources or get_internal_apis(), fall_through=False)

    def forward_request(self, method, path, data, headers):
        try:
            return super().forward_request(method, path, data, headers)
        except NotFound:
            if not path.startswith(constants.INTERNAL_RESOURCE_PATH + "/"):
                # only return 404 if we're accessing an internal resource, otherwise fall back to the other listeners
                return True
            else:
                LOG.warning("Unable to find handler for path: %s", path)
                return 404


INTERNAL_APIS: Optional[LocalstackResources] = None


def get_internal_apis() -> LocalstackResources:
    """
    Get the LocalstackResources singleton.
    """
    global INTERNAL_APIS
    if not INTERNAL_APIS:
        INTERNAL_APIS = LocalstackResources()
    return INTERNAL_APIS
