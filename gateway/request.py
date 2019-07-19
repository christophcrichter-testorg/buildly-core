import logging
import json
import uuid
import asyncio
from urllib.error import URLError
from typing import Any, Dict, Tuple, Union, List

import requests
import aiohttp
from bravado_core.spec import Spec
from django.http.request import QueryDict
from django.forms.models import model_to_dict
from rest_framework.request import Request
from rest_framework.authentication import get_authorization_header

from . import exceptions
from . import utils
from .models import LogicModule
from datamesh.models import LogicModuleModel, JoinRecord, Relationship
from datamesh import utils as datamesh_utils
from workflow import models as wfm

logger = logging.getLogger(__name__)


class GatewayResponse(object):
    """
    Response object used with GatewayRequest
    """

    def __init__(self, content: Any, status_code: int, headers: Dict[str, str]):
        self.content = content
        self.status_code = status_code
        self.headers = headers


class BaseGatewayRequest(object):
    """
    Base class for implementing gateway logic for redirecting incoming request to underlying micro-services.
    First it retrieves a Swagger specification of the micro-service
    to validate incoming request's operation against it.
    """

    SWAGGER_CONFIG = {
        'validate_requests': False,
        'validate_responses': False,
        'use_models': False,
        'validate_swagger_spec': False,
    }

    def __init__(self, request: Request, **kwargs):
        self.request = request
        self.url_kwargs = kwargs
        self._logic_modules = dict()
        self._specs = dict()
        self._data = dict()

    def perform(self):
        raise NotImplementedError('You need to implement this method')

    def _get_logic_module(self, service_name: str) -> LogicModule:
        """ Retrieve LogicModule by service name. """
        if service_name not in self._logic_modules:
            try:
                self._logic_modules[service_name] = LogicModule.objects.get(endpoint_name=service_name)
            except LogicModule.DoesNotExist:
                raise exceptions.ServiceDoesNotExist(f'Service "{service_name}" not found.')
        return self._logic_modules[service_name]

    def is_valid_for_cache(self) -> bool:
        """ Checks if request is valid for caching operations """
        return self.request.method.lower() == 'get' and not self.request.query_params

    def get_request_data(self) -> dict:
        """
        Create the data structure to be used in Swagger request. GET and  DELETE
        requests don't require body, so the data structure will have just
        query parameters if passed to swagger request.
        """
        if self.request.content_type == 'application/json':
            return json.dumps(self.request.data)

        method = self.request.META['REQUEST_METHOD'].lower()
        data = self.request.query_params.dict()

        data.pop('aggregate', None)
        data.pop('join', None)

        if method in ['post', 'put', 'patch']:
            query_dict_body = self.request.data if hasattr(self.request, 'data') else dict()
            body = query_dict_body.dict() if isinstance(query_dict_body, QueryDict) else query_dict_body
            data.update(body)

            # handle uploaded files
            if self.request.FILES:
                for key, value in self.request.FILES.items():
                    data[key] = {
                        'header': {
                            'Content-Type': value.content_type,
                        },
                        'data': value,
                        'filename': value.name,
                    }

        return data

    def get_headers(self) -> dict:
        """Get data and headers from the incoming request."""
        headers = {
            'Authorization': get_authorization_header(self.request).decode('utf-8'),
        }
        if self.request.content_type == 'application/json':
            headers['content-type'] = 'application/json'
        return headers

    def prepare_data(self, spec: Spec, **kwargs) -> Tuple[str, str]:
        """ Parse request URL, validates operation, and returns method and URL for outgoing request"""

        # Parse URL kwargs
        pk = kwargs.get('pk')
        model = kwargs.get('model', '').lower()
        path_kwargs = {}
        if kwargs.get('pk') is None:
            path = f'/{model}/'
        else:
            pk_name = 'uuid' if utils.valid_uuid4(pk) else 'id'
            path_kwargs = {pk_name: pk}
            path = f'/{model}/{{{pk_name}}}/'

        # Check that operation is valid according to spec
        operation = spec.get_op_for_request(self.request.method, path)
        if not operation:
            raise exceptions.EndpointNotFound(f'Endpoint not found: {self.request.method} {path}')
        method = operation.http_method.lower()
        path_name = operation.path_name

        # Build URL for the operation to request data from the service
        url = spec.api_url.rstrip('/') + path_name
        for k, v in path_kwargs.items():
            url = url.replace(f'{{{k}}}', v)

        return method, url

    def get_datamesh_relationships(self) -> Tuple[List[Tuple[Relationship, bool]], str]:
        """ Get DataMesh relationships and lookup field name for the top level model """
        service_name = self.url_kwargs['service']
        logic_module = self._get_logic_module(service_name)

        # find out forwards relations through logic module from request as origin
        padding = self.request.path.index(f'/{logic_module.endpoint_name}')
        endpoint = self.request.path[len(f'/{logic_module.endpoint_name}')+padding:]
        endpoint = endpoint[:endpoint.index('/', 1) + 1]
        logic_module_model = LogicModuleModel.objects.get(
            logic_module_endpoint_name=logic_module.endpoint_name, endpoint=endpoint)
        relationships = logic_module_model.get_relationships()
        origin_lookup_field = logic_module_model.lookup_field_name
        return relationships, origin_lookup_field


class GatewayRequest(BaseGatewayRequest):
    """
    Allows to perform synchronous requests to underlying services with requests package
    """

    def perform(self) -> GatewayResponse:
        """
        Make request to underlying service(s) and returns aggregated response.
        """
        # init swagger spec from the service swagger doc file
        try:
            spec = self._get_swagger_spec(self.url_kwargs['service'])
        except exceptions.ServiceDoesNotExist as e:
            return GatewayResponse(e.content, e.status, {'Content-Type': e.content_type})

        return self._get_data(spec)

    def _get_swagger_spec(self, endpoint_name: str) -> Spec:
        """Get Swagger spec of specified service."""
        logic_module = self._get_logic_module(endpoint_name)
        schema_url = utils.get_swagger_url_by_logic_module(logic_module)

        if schema_url not in self._specs:
            try:
                response = requests.get(schema_url)
                spec_dict = response.json()
            except URLError:
                raise URLError(f'Make sure that {schema_url} is accessible.')

            swagger_spec = Spec.from_dict(spec_dict, config=self.SWAGGER_CONFIG)
            self._specs[schema_url] = swagger_spec

        return self._specs[schema_url]

    def _get_data(self, spec: Spec) -> GatewayResponse:
        """
        Gets data from the service (multiple services in case of using DataMesh and aggregating data)
        """

        # create and perform a service data request
        content, status_code, headers = self._data_request(spec=spec, **self.url_kwargs)

        # aggregate/join with the JoinRecord-models
        if 'join' in self.request.query_params and status_code == 200 and type(content) in [dict, list]:
            try:
                self._join_response_data(resp_data=content)
            except exceptions.ServiceDoesNotExist as e:
                logger.error(e.content)

        # TODO: old DataMesh aggregation: remove after migrating to the new one
        if self.request.query_params.get('aggregate', '_none').lower() == 'true' and status_code == 200:
            try:
                self._aggregate_response_data(resp_data=content)
            except exceptions.ServiceDoesNotExist as e:
                logger.error(e.content)

        if type(content) in [dict, list]:
            content = json.dumps(content, cls=utils.GatewayJSONEncoder)

        return GatewayResponse(content, status_code, headers)

    def _data_request(self, spec: Spec, **kwargs) -> Tuple[Any, int, Dict[str, str]]:
        """
        Perform request to the service, use Swagger spec for validating operation
        """

        method, url = self.prepare_data(spec, **kwargs)

        # Check request cache if applicable
        if self.is_valid_for_cache() and url in self._data:
            logger.debug(f'Taking data from cache: {url}')
            return self._data[url]

        # Make request to the service
        method = getattr(requests, method)
        try:
            response = method(url,
                              headers=self.get_headers(),
                              params=self.request.query_params,
                              data=self.get_request_data(),
                              files=self.request.FILES)
        except Exception as e:
            error_msg = (f'An error occurred when redirecting the request to '
                         f'or receiving the response from the service.\n'
                         f'Origin: ({e.__class__.__name__}: {e})')
            raise exceptions.GatewayError(error_msg)

        try:
            content = response.json()
        except ValueError:
            content = response.content
        return_data = (content, response.status_code, response.headers)

        # Cache data if request is cache-valid
        if self.is_valid_for_cache():
            self._data[url] = return_data

        return return_data

    def _join_response_data(self, resp_data: Union[dict, list]) -> None:
        """
        Aggregates data from the requested service and from related services.
        Uses DataMesh relationship model for this.
        :param rest_framework.Request request: incoming request info
        :param resp_data: initial response data
        :param kwargs: extra arguments ['service', 'model', 'pk']
        """
        if isinstance(resp_data, dict):
            if 'results' in resp_data:
                # In case of pagination take 'results' as a items data
                resp_data = resp_data.get('results', None)

        relationships, origin_lookup_field = self.get_datamesh_relationships()

        if isinstance(resp_data, dict):
            # detailed view
            self._add_nested_data(resp_data, relationships, origin_lookup_field)
        elif isinstance(resp_data, list):
            # list view
            for data_item in resp_data:
                self._add_nested_data(data_item, relationships, origin_lookup_field)

    def _add_nested_data(self,
                         data_item: dict,
                         relationships: List[Tuple[Relationship, bool]],
                         origin_lookup_field: str) -> None:
        """
        Nest data retrieved from related services.
        """
        # remove query_params from original request
        self.request._request.GET = QueryDict(mutable=True)

        origin_pk = data_item.get(origin_lookup_field)
        if not origin_pk:
            raise exceptions.DataMeshError(
                f'DataMeshConfigurationError: lookup_field_name "{origin_lookup_field}" '
                f'not found in response.')

        for relationship, is_forward_lookup in relationships:
            data_item[relationship.key] = []
            join_records = JoinRecord.objects.get_join_records(origin_pk, relationship, is_forward_lookup)

            # now backwards get related objects through join_records
            if join_records:
                related_model, related_record_field = datamesh_utils.prepare_lookup_kwargs(
                    is_forward_lookup, relationship, join_records[0])
                spec = self._get_swagger_spec(related_model.logic_module_endpoint_name)

                for join_record in join_records:

                    request_kwargs = {
                        'pk': (str(getattr(join_record, related_record_field))),
                        'model': related_model.endpoint.strip('/'),
                        'method': self.request.META['REQUEST_METHOD'].lower(),
                        'service': related_model.logic_module_endpoint_name,
                    }

                    # create and perform a service request
                    content, _, _ = self._data_request(spec=spec, **request_kwargs)
                    if isinstance(content, dict):
                        data_item[relationship.key].append(dict(content))
                    else:
                        logger.error(f'No response data for join record (request params: {request_kwargs})')

    # ===================================================================
    # OLD DATAMESH METHODS (TODO: remove after migrating to new DataMesh)
    def _aggregate_response_data(self, resp_data: Union[dict, list]):
        """
        Aggregate data from first response
        """
        service_name = self.url_kwargs['service']

        if isinstance(resp_data, dict):
            if 'results' in resp_data:
                # DRF API payload structure
                resp_data = resp_data.get('results', None)

        logic_module = self._get_logic_module(service_name)

        if isinstance(resp_data, list):
            for data in resp_data:
                extension_map = self._generate_extension_map(
                    logic_module=logic_module,
                    model_name=self.url_kwargs['model'],
                    data=data
                )
                r = self._expand_data(extension_map)
                data.update(**r)
        elif isinstance(resp_data, dict):
            extension_map = self._generate_extension_map(
                logic_module=logic_module,
                model_name=self.url_kwargs['model'],
                data=resp_data
            )
            r = self._expand_data(extension_map)
            resp_data.update(**r)

    def _expand_data(self, extend_models: list):
        """
        Use extension maps to fetch data from different services and
        replace the relationship key by real data.
        """
        result = dict()
        for extend_model in extend_models:
            content = None
            if extend_model['service'] == 'bifrost':
                if hasattr(wfm, extend_model['model']):
                    cls = getattr(wfm, extend_model['model'])
                    uuid_name = self._get_bifrost_uuid_name(cls)
                    lookup = {
                        uuid_name: extend_model['pk']
                    }
                    try:
                        obj = cls.objects.get(**lookup)
                    except cls.DoesNotExist as e:
                        logger.info(e)
                    except ValueError:
                        logger.info(f' Not found: {extend_model["model"]} with uuid_name={extend_model["pk"]}')
                    else:
                        utils.validate_object_access(self.request, obj)
                        content = model_to_dict(obj)
            else:
                spec = self._get_swagger_spec(extend_model['service'])

                # remove query_params from original request
                self.request._request.GET = QueryDict(mutable=True)

                # create and perform a service request
                content, _, _ = self._data_request(spec=spec, **extend_model)

            if content is not None:
                result[extend_model['relationship_key']] = content

        return result

    def _generate_extension_map(self, logic_module: LogicModule, model_name: str, data: dict):
        """
        Generate a list of relationship map of a specific service model.
        """
        extension_map = []
        if not logic_module.relationships:
            logger.warning(f'Tried to aggregate but no relationship defined in {logic_module}.')
            return extension_map
        for k, v in logic_module.relationships[model_name].items():
            value = v.split('.')
            collection_args = {
                'service': value[0],
                'model': value[1],
                'pk': str(data[k]),
                'relationship_key': k
            }
            extension_map.append(collection_args)

        return extension_map

    def _get_bifrost_uuid_name(self, model):
        for field in model._meta.fields:
            if field.name.endswith('uuid') and field.unique and \
                    field.default == uuid.uuid4:
                return field.name
    # ==================================================================


class AsyncGatewayRequest(BaseGatewayRequest):
    """
    Allows to perform asynchronous requests to underlying services with asyncio and aiohttp package
    """

    def perform(self) -> GatewayResponse:
        """
        Override base class's method for asynchronous execution. Wraps async method.
        """
        result = {}
        asyncio.run(self.async_perform(result))
        if 'response' not in result:
            raise exceptions.GatewayError('Error performing asynchronous gateway request')
        return result['response']

    async def async_perform(self, result: dict):
        try:
            spec = await self._get_swagger_spec(self.url_kwargs['service'])
        except exceptions.ServiceDoesNotExist as e:
            return GatewayResponse(e.content, e.status, {'Content-Type': e.content_type})
        result['response'] = await self._get_data(spec)

    async def _get_swagger_spec(self, endpoint_name: str) -> Spec:
        """ Gets swagger spec asynchronously and adds it to specs cache """
        logic_module = self._get_logic_module(endpoint_name)
        schema_url = utils.get_swagger_url_by_logic_module(logic_module)

        if schema_url not in self._specs:
            async with aiohttp.ClientSession() as session:
                async with session.get(schema_url) as response:
                    try:
                        spec_dict = await response.json()
                    except aiohttp.ContentTypeError:
                        raise exceptions.GatewayError(
                            f'Failed to parse swagger schema from {schema_url}. Should be JSON.'
                        )
                swagger_spec = Spec.from_dict(spec_dict, config=self.SWAGGER_CONFIG)
                self._specs[schema_url] = swagger_spec
        return self._specs[schema_url]

    async def _get_data(self, spec: Spec) -> GatewayResponse:
        # create and perform a service data request
        content, status_code, headers = await self._data_request(spec, **self.url_kwargs)

        # aggregate/join with the JoinRecord-models
        if 'join' in self.request.query_params and status_code == 200 and type(content) in [dict, list]:
            try:
                await self._join_response_data(resp_data=content)
            except exceptions.ServiceDoesNotExist as e:
                logger.error(e.content)

        # TODO: old DataMesh aggregation should be here until it's replaced by the new DataMesh

        if type(content) in [dict, list]:
            content = json.dumps(content, cls=utils.GatewayJSONEncoder)

        return GatewayResponse(content, status_code, headers)

    async def _data_request(self, spec: Spec, **kwargs) -> Tuple[Any, int, Dict[str, str]]:

        method, url = self.prepare_data(spec, **kwargs)

        # Check request cache if applicable
        if self.is_valid_for_cache() and url in self._data:
            logger.debug(f'Taking data from cache: {url}')
            return self._data[url]

        # Make request to the service
        async with aiohttp.ClientSession() as session:
            method = getattr(session, method)
            async with method(url, data=self.get_request_data(), headers=self.get_headers()) as response:
                try:
                    content = await response.json()
                except json.JSONDecodeError:
                    content = await response.content.read()
            return_data = (content, response.status, response.headers)

            # Cache data if request is cache-valid
            if self.is_valid_for_cache():
                self._data[url] = return_data

            return return_data

    async def _join_response_data(self, resp_data: Union[dict, list]) -> None:
        """
        Aggregates data from the requested service and from related services asynchronously.
        Uses DataMesh relationship model for this.
        """
        if isinstance(resp_data, dict):
            if 'results' in resp_data:
                # In case of pagination take 'results' as a items data
                resp_data = resp_data.get('results', None)

        relationships, origin_lookup_field = self.get_datamesh_relationships()

        # asynchronously getting all swagger specs from all related services
        tasks = []
        for relationship, _ in relationships:
            related_model = relationship.related_model
            tasks.append(self._get_swagger_spec(related_model.logic_module_endpoint_name))
        await asyncio.gather(*tasks)

        tasks = []
        if isinstance(resp_data, dict):
            # detailed view
            tasks.extend(await self._prepare_datamesh_tasks(resp_data, relationships, origin_lookup_field))
        elif isinstance(resp_data, list):
            # list view
            for data_item in resp_data:
                tasks.extend(await self._prepare_datamesh_tasks(data_item, relationships, origin_lookup_field))
        await asyncio.gather(*tasks)

    async def _prepare_datamesh_tasks(self,
                                      data_item: dict,
                                      relationships: List[Tuple['Relationship', bool]],
                                      origin_lookup_field: str) -> list:
        """ Creates a list of coroutines for extending data from other services asynchronously """
        tasks = []

        # remove query_params from original request
        self.request._request.GET = QueryDict(mutable=True)

        origin_pk = data_item.get(origin_lookup_field)
        if not origin_pk:
            raise exceptions.DataMeshError(
                f'DataMeshConfigurationError: lookup_field_name "{origin_lookup_field}" '
                f'not found in response.')
        for relationship, is_forward_lookup in relationships:
            data_item[relationship.key] = []
            join_records = JoinRecord.objects.get_join_records(origin_pk, relationship, is_forward_lookup)

            # now backwards get related objects through join_records
            if join_records:
                related_model, related_record_field = datamesh_utils.prepare_lookup_kwargs(
                    is_forward_lookup, relationship, join_records[0])
                spec = await self._get_swagger_spec(related_model.logic_module_endpoint_name)
                for join_record in join_records:
                    request_kwargs = {
                        'pk': (str(join_record.related_record_id) if join_record.related_record_id is not None
                               else str(join_record.related_record_uuid)),
                        'model': related_model.endpoint.strip('/'),
                        'method': self.request.META['REQUEST_METHOD'].lower(),
                        'service': related_model.logic_module_endpoint_name,
                    }
                    tasks.append(self._extend_content(spec, data_item[relationship.key], **request_kwargs))

        return tasks

    async def _extend_content(self, spec: Spec, placeholder: list, **request_kwargs) -> None:
        """ Performs data request and extends data with received data """
        content, _, _ = await self._data_request(spec=spec, **request_kwargs)
        if isinstance(content, dict):
            placeholder.append(dict(content))
        else:
            logger.error(f'No response data for join record (request params: {request_kwargs})')