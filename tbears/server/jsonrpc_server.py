# Copyright 2017-2018 theloop Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import sys
import time
import hashlib
from collections import Iterable
from json import JSONDecodeError

from jsonrpcserver import status
from jsonrpcserver.aio import methods
from jsonrpcserver.exceptions import JsonRpcServerError, InvalidParams, ServerError
from sanic import Sanic, response as sanic_response
from iconservice.base.jsonrpc_message_validator import JsonRpcMessageValidator
from iconservice.base.exception import IconServiceBaseException
from iconservice.icon_inner_service import IconScoreInnerService, IconScoreInnerStub
from iconservice.utils.type_converter import TypeConverter
from iconservice.logger import Logger
from iconservice.icon_config import *

from typing import Optional

MQ_TEST = False
if not MQ_TEST:
    from iconservice.icon_inner_service import IconScoreInnerTask

TBEARS_LOG_TAG = 'tbears'
SEPARATE_PROCESS_DEBUG = False

__block_height = 0
__icon_score_service = None
__icon_score_stub = None
__icon_inner_task = None
__type_converter = None
__tx_result_mapper = None

PARSE_ERROR_RESPONSE = '{"jsonrpc":"2.0", "error":{"code":-32700, "message": "Parse error"}, "id": "null"}'

sys.path.append('..')
sys.path.append('.')


def create_hash(data: bytes) -> str:
    return f'0x{hashlib.sha3_256(data).hexdigest()}'


def get_icon_inner_task() -> Optional['IconScoreInnerTask']:
    return __icon_inner_task


def get_icon_score_stub() -> IconScoreInnerStub:
    global __icon_score_stub
    return __icon_score_stub


def get_type_converter() -> TypeConverter:
    global __type_converter
    return __type_converter


def create_icon_score_service(channel: str, amqp_key: str, amqp_target: str, rpc_port: str,
                              icon_score_root_path: str, icon_score_state_db_root_path: str,
                              **kwargs) -> IconScoreInnerService:
    icon_score_queue_name = ICON_SCORE_QUEUE_NAME_FORMAT.format(channel_name=channel,
                                                                amqp_key=amqp_key,
                                                                rpc_port=rpc_port)

    Logger.debug(f'==========create_icon_score_service==========', TBEARS_LOG_TAG)
    Logger.debug(f'icon_score_root_path : {icon_score_root_path}', TBEARS_LOG_TAG)
    Logger.debug(f'icon_score_state_db_root_path  : {icon_score_state_db_root_path}', TBEARS_LOG_TAG)
    Logger.debug(f'amqp_target  : {amqp_target}', TBEARS_LOG_TAG)
    Logger.debug(f'icon_score_queue_name  : {icon_score_queue_name}', TBEARS_LOG_TAG)
    Logger.debug(f'kwargs : {kwargs}', TBEARS_LOG_TAG)
    Logger.debug(f'==========create_icon_score_service==========', TBEARS_LOG_TAG)

    return IconScoreInnerService(amqp_target, icon_score_queue_name,
                                 icon_score_root_path=icon_score_root_path,
                                 icon_score_state_db_root_path=icon_score_state_db_root_path)


def create_icon_score_stub(channel: str, amqp_key: str, amqp_target: str, rpc_port: str,
                           **kwargs) -> IconScoreInnerStub:
    icon_score_queue_name = ICON_SCORE_QUEUE_NAME_FORMAT.format(channel_name=channel,
                                                                amqp_key=amqp_key,
                                                                rpc_port=rpc_port)

    Logger.debug(f'==========create_icon_score_stub==========', TBEARS_LOG_TAG)
    Logger.debug(f'icon_score_queue_name  : {icon_score_queue_name}', TBEARS_LOG_TAG)
    Logger.debug(f'kwargs : {kwargs}', TBEARS_LOG_TAG)
    Logger.debug(f'==========create_icon_score_stub==========', TBEARS_LOG_TAG)

    return IconScoreInnerStub(amqp_target, icon_score_queue_name)


def get_block_height():
    global __block_height
    __block_height += 1
    return __block_height


def integers_to_hex(res: Iterable) -> Iterable:
    if isinstance(res, dict):
        for k, v in res.items():
            if isinstance(v, dict):
                res[k] = integers_to_hex(v)
            elif isinstance(v, list):
                res[k] = integers_to_hex(v)
            elif isinstance(v, int):
                res[k] = hex(v)
    elif isinstance(res, list):
        for k, v in enumerate(res):
            if isinstance(v, dict):
                res[k] = integers_to_hex(v)
            elif isinstance(v, list):
                res[k] = integers_to_hex(v)
            elif isinstance(v, int):
                res[k] = hex(v)
    return res


def get_tx_result_mapper():
    global __tx_result_mapper
    return __tx_result_mapper


def response_to_json(response):
    # if response is tx_result list
    if isinstance(response, list):
        tx_result = response[0]
        tx_hash = tx_result['txHash']
        get_tx_result_mapper().put(tx_hash, tx_result)
        return tx_hash
    else:
        # response is dict including code(int)( and message(str)
        raise GenericJsonRpcServerError(
            code=-response['code'],
            message=response['message'],
            http_status=status.HTTP_BAD_REQUEST
        )


def validate_jsonrpc_message(method: str, params: dict) -> None:
    """Check params of message
    (icx_call, icx_getBalance, icx_getTotalSupply, icx_sendTransaction)

    if params is not valid, raise a sort of IconServiceBaseException

    :param method:
    :param params:
    """
    try:
        JsonRpcMessageValidator.validate(method, params)
        return
    except IconServiceBaseException as e:
        code = -e.code
        message = e.message
    except Exception as e:
        code = ServerError.code
        message = repr(e)

    raise GenericJsonRpcServerError(
        code=code,
        message=message,
        http_status=status.HTTP_BAD_REQUEST)


class GenericJsonRpcServerError(JsonRpcServerError):
    """Raised when the request is not a valid JSON-RPC object.
    User can change code and message properly

    :param data: Extra information about the error that occurred (optional).
    """
    def __init__(self, code: int, message: str, http_status: int, data=None):
        """

        :param code: json-rpc error code
        :param message: json-rpc error message
        :param http_status: http status code
        :param data: json-rpc error data (optional)
        """
        super().__init__(data)

        self.code = code
        self.message = message
        self.http_status = http_status


class TxResultMapper(object):
    def __init__(self, limit_capacity = 1000):
        self.__limit_capacity = limit_capacity
        self.__mapper = dict()
        self.__key_list = []

    def put(self, key, value) -> None:
        self.__check_limit()
        self.__key_list.append(key)
        self.__mapper[key] = value

    def get(self, key):
        return self.__mapper.get(key, None)

    def __getitem__(self, item):
        return self.__mapper[item]

    def __check_limit(self):
        if self.len() > self.__limit_capacity:
            key = self.__key_list[0]
            self.__key_list.pop(0)
            del self.__mapper[key]

    def len(self) -> int:
        return len(self.__mapper)


class MockDispatcher:
    flask_server = None

    @staticmethod
    async def dispatch(request):
        try:
            req = json.loads(request.body.decode())
            req["params"] = req.get("params", {})
            req["params"]["method"] = request.json["method"]
        except JSONDecodeError:
            return sanic_response.json(PARSE_ERROR_RESPONSE, 400)
        else:
            res = await methods.dispatch(req)

            if 'result' in res:
                if isinstance(res['result'], (dict, list, int)):
                    res['result'] = integers_to_hex(res['result'])
            return sanic_response.json(res, status=res.http_status)

    @staticmethod
    @methods.add
    async def hello(**request_params):
        raise Exception()
        # Logger.debug(f'json_rpc_server hello!', TBEARS_LOG_TAG)

    @staticmethod
    @methods.add
    async def icx_sendTransaction(**request_params):
        """ icx_sendTransaction jsonrpc handler.
        We assume that only one tx in a block.

        :param request_params: jsonrpc params field.
        """
        Logger.debug(f'json_rpc_server icx_sendTransaction!', TBEARS_LOG_TAG)

        method = 'icx_sendTransaction'
        validate_jsonrpc_message(method, request_params)

        # Insert txHash into request params
        tx_hash = create_hash(json.dumps(request_params).encode())
        request_params['txHash'] = tx_hash
        tx = {
            'method': 'icx_sendTransaction',
            'params': request_params
        }
        make_request = {'transactions': [tx]}

        block_height: int = get_block_height()
        block_timestamp_us = int(time.time() * 10 ** 6)
        make_request['block'] = {
            'blockHeight': block_height,
            'blockHash': create_hash(block_timestamp_us.to_bytes(8, 'big')),
            'timestamp': block_timestamp_us
        }

        if MQ_TEST:
            response = await get_icon_score_stub().task().icx_send_transaction(make_request)
            if not isinstance(response, list):
                await get_icon_score_stub().task().remove_precommit_state({})
            elif response[0]['status'] == 1:
                await get_icon_score_stub().task().write_precommit_state({})
            else:
                await get_icon_score_stub().task().remove_precommit_state({})
            return response_to_json(response)
        else:
            response = await get_icon_inner_task().icx_send_transaction(make_request)
            if not isinstance(response, list):
                await get_icon_inner_task().remove_precommit_state({})
            elif response[0]['status'] == 1:
                await get_icon_inner_task().write_precommit_state({})
            else:
                await get_icon_inner_task().remove_precommit_state({})
            return response_to_json(response)

    @staticmethod
    @methods.add
    async def icx_call(**request_params):
        Logger.debug(f'json_rpc_server icx_call!', TBEARS_LOG_TAG)

        method = 'icx_call'
        validate_jsonrpc_message(method, request_params)

        make_request = {'method': method, 'params': request_params}

        if MQ_TEST:
            return await get_icon_score_stub().task().icx_call(make_request)
        else:
            return await get_icon_inner_task().icx_call(make_request)

    @staticmethod
    @methods.add
    async def icx_getBalance(**request_params):
        Logger.debug(f'json_rpc_server icx_getBalance!', TBEARS_LOG_TAG)

        method = 'icx_getBalance'
        make_request = {'method': method, 'params': request_params}

        validate_jsonrpc_message(method, request_params)

        if MQ_TEST:
            return await get_icon_score_stub().task().icx_call(make_request)
        else:
            return await get_icon_inner_task().icx_call(make_request)

    @staticmethod
    @methods.add
    async def icx_getTotalSupply(**request_params):
        Logger.debug(f'json_rpc_server icx_getTotalSupply!', TBEARS_LOG_TAG)

        method = 'icx_getTotalSupply'
        make_request = {'method': method, 'params': request_params}

        if MQ_TEST:
            return await get_icon_score_stub().task().icx_call(make_request)
        else:
            return await get_icon_inner_task().icx_call(make_request)

    @staticmethod
    @methods.add
    async def icx_getTransactionResult(**request_params):
        Logger.debug(f'json_rpc_server getTransactionResult!', TBEARS_LOG_TAG)

        try:
            tx_hash = request_params['txHash']
            return get_tx_result_mapper()[tx_hash]
        except Exception:
            raise GenericJsonRpcServerError(
                code=InvalidParams.code,
                message='TransactionResult not found',
                http_status=status.HTTP_BAD_REQUEST)

    @staticmethod
    @methods.add
    async def server_exit(**request_params):
        Logger.debug(f'json_rpc_server server_exit!', TBEARS_LOG_TAG)

        if MQ_TEST:
            await get_icon_score_stub().task().close()

        if MockDispatcher.flask_server is not None:
            MockDispatcher.flask_server.app.stop()

        return '0x0'


class FlaskServer:
    def __init__(self):
        self.__app = Sanic(__name__)
        self.__app.config['ENV'] = 'development'  # Block flask warning message
        MockDispatcher.flask_server = self

    @property
    def app(self):
        return self.__app

    def set_resource(self):
        self.__app.add_route(MockDispatcher.dispatch, '/api/v3/', methods=['POST'], strict_slashes=False)


class SimpleRestServer:
    def __init__(self, port, ip_address=None):
        self.__port = port
        self.__ip_address = ip_address

        self.__server = FlaskServer()
        self.__server.set_resource()

    def get_app(self):
        return self.__server.app

    def run(self):
        Logger.info(f"SimpleRestServer run... {self.__port}", TBEARS_LOG_TAG)

        self.__server.app.run(port=self.__port,
                              host=self.__ip_address,
                              debug=False)


def serve():
    async def __serve():
        init_tbears()
        init_type_converter()
        if MQ_TEST:
            if not SEPARATE_PROCESS_DEBUG:
                await init_icon_score_service()
            await init_icon_score_stub(conf)
        else:
            await init_icon_inner_task(conf)

    if len(sys.argv) == 2:
        path = sys.argv[1]
    else:
        path = './tbears.json'

    conf = load_config(path)
    Logger(path)
    Logger.info(f'config_file: {path}', TBEARS_LOG_TAG)

    server = SimpleRestServer(conf['port'], "0.0.0.0")
    server.get_app().add_task(__serve)
    server.run()


def load_config(path: str) -> dict:
    default_conf = {
        "from": "hxaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "port": 9000,
        "scoreRoot": "./.score",
        "dbRoot": "./.db",
        "accounts": [
            {
                "name": "genesis",
                "address": "hx0000000000000000000000000000000000000000",
                "balance": "0x2961fff8ca4a62327800000"
            },
            {
                "name": "treasury",
                "address": "hx1000000000000000000000000000000000000000",
                "balance": "0x0"
            }
        ],
        "log": {
            "level": "debug",
            "filePath": "./tbears.log",
            "outputType": "console|file"
        }
    }

    try:
        with open(path) as f:
            conf = json.load(f)
    except (OSError, IOError):
        return default_conf

    for key in default_conf:
        if key not in conf:
            conf[key] = default_conf[key]

    return conf


async def init_icon_score_service():
    global __icon_score_service
    __icon_score_service = create_icon_score_service(**DEFAULT_ICON_SERVICE_FOR_TBEARS_ARGUMENT)
    await __icon_score_service.connect(exclusive=True)


async def init_icon_score_stub(conf: dict):
    global __icon_score_stub
    __icon_score_stub = create_icon_score_stub(**DEFAULT_ICON_SERVICE_FOR_TBEARS_ARGUMENT)
    await __icon_score_stub.connect()
    if not SEPARATE_PROCESS_DEBUG:
        await __icon_score_stub.task().open()

    accounts = get_type_converter().convert(conf['accounts'], recursive=False)
    make_request = dict()
    make_request['accounts'] = accounts
    await __icon_score_stub.task().genesis_invoke(make_request)


async def init_icon_inner_task(conf: dict):
    global __icon_inner_task
    __icon_inner_task = IconScoreInnerTask(conf['scoreRoot'], conf['dbRoot'])
    await __icon_inner_task.open()

    accounts = get_type_converter().convert(conf['accounts'], recursive=False)
    make_request = dict()
    make_request['accounts'] = accounts
    await __icon_inner_task.genesis_invoke(make_request)


def init_type_converter():
    global __type_converter

    type_table = {
        'from': 'address',
        'to': 'address',
        'address': 'address',
        'fee': 'int',
        'value': 'int',
        'balance': 'int'
    }
    __type_converter = TypeConverter(type_table)


def init_tbears():
    global __tx_result_mapper
    __tx_result_mapper = TxResultMapper()


if __name__ == '__main__':
    serve()
