"""本模块提供 Ariadne 内部使用的小工具, 以及方便的 `async_exec` 模块.
"""

# Utility Layout
import asyncio
import functools
import inspect
import sys
import traceback
import warnings
from asyncio.events import AbstractEventLoop
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Generator,
    List,
    NoReturn,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from graia.broadcast import Broadcast
from graia.broadcast.entities.decorator import Decorator
from graia.broadcast.entities.dispatcher import BaseDispatcher
from graia.broadcast.entities.event import Dispatchable
from graia.broadcast.entities.listener import Listener
from graia.broadcast.entities.namespace import Namespace
from graia.broadcast.interfaces.dispatcher import DispatcherInterface
from graia.broadcast.typing import T_Dispatcher
from graia.broadcast.utilles import dispatcher_mixin_handler
from loguru import logger
from typing_extensions import Concatenate

from ..exception import (
    AccountMuted,
    AccountNotFound,
    InvalidArgument,
    InvalidSession,
    InvalidVerifyKey,
    MessageTooLong,
    RemoteException,
    UnknownError,
    UnknownTarget,
    UnVerifiedSession,
)
from ..typing import DictStrAny, P, R, T

if TYPE_CHECKING:
    from ..app import Ariadne

code_exceptions_mapping: Dict[int, Type[Exception]] = {
    1: InvalidVerifyKey,
    2: AccountNotFound,
    3: InvalidSession,
    4: UnVerifiedSession,
    5: UnknownTarget,
    6: FileNotFoundError,
    10: PermissionError,
    20: AccountMuted,
    30: MessageTooLong,
    400: InvalidArgument,
    500: RemoteException,
}


def validate_response(code: Union[Dict[str, Union[int, Any]], int]):
    """验证远程服务器的返回值

    Args:
        code (Union[dict, int]): 返回的对象

    Raises:
        Exception: 请参照 code_exceptions_mapping
    """
    if isinstance(code, dict):
        int_code = code.get("code")
    else:
        int_code = code
    if not isinstance(int_code, int) or int_code == 200 or int_code == 0:
        return
    exc_cls = code_exceptions_mapping.get(int_code)
    if exc_cls:
        raise exc_cls(exc_cls.__doc__, code)
    raise UnknownError(code)


def loguru_excepthook(cls, val, tb, *_, **__):
    """loguru 异常回调

    Args:
        cls (Type[Exception]): 异常类
        val (Exception): 异常的实际值
        tb (TracebackType): 回溯消息
    """
    logger.opt(exception=(cls, val, tb)).error("Exception:")


def loguru_async_handler(_, ctx: dict):
    """loguru 异步异常回调

    Args:
        _ (AbstractEventLoop): 异常发生的事件循环
        ctx (dict): 异常上下文
    """
    if "exception" in ctx:
        logger.opt(exception=ctx["exception"]).error("Exception:")
    else:
        logger.error(f"Exception: {ctx}")


def inject_loguru_traceback(loop: AbstractEventLoop = None):
    """使用 loguru 模块替换默认的 traceback.print_exception 与 sys.excepthook"""
    traceback.print_exception = loguru_excepthook
    sys.excepthook = loguru_excepthook
    if loop:
        loop.set_exception_handler(loguru_async_handler)


def inject_bypass_listener(broadcast: Broadcast):
    """注入 BypassListener 以享受子事件解析.

    Args:
        broadcast (Broadcast): 外部事件系统, 提供了 event_class_generator 方法以生成子事件.
    """

    class BypassListener(Listener):
        """透传监听器的实现"""

        def __init__(
            self,
            callable: Callable,
            namespace: Namespace,
            listening_events: List[Type[Dispatchable]],
            inline_dispatchers: List[T_Dispatcher] = None,
            decorators: List[Decorator] = None,
            priority: int = 16,
        ) -> None:
            events = []
            for event in listening_events:
                events.append(event)
                events.extend(broadcast.event_class_generator(event))
            super().__init__(
                callable,
                namespace,
                events,
                inline_dispatchers=inline_dispatchers,
                decorators=decorators,
                priority=priority,
            )

    import graia.broadcast.entities.listener

    graia.broadcast.entities.listener.Listener = BypassListener  # type: ignore
    graia.broadcast.Listener = BypassListener  # type: ignore
    try:  # Override saya listener
        import graia.saya.builtins.broadcast.schema

        graia.saya.builtins.broadcast.schema.Listener = BypassListener  # type: ignore
    except ImportError:  # Saya not installed, pass.
        pass


def app_ctx_manager(
    func: Callable[Concatenate["Ariadne", P], Awaitable[R]]
) -> Callable[Concatenate["Ariadne", P], Awaitable[R]]:
    """包装声明需要在 Ariadne Context 中执行的函数

    Args:
        func (Callable[P, R]): 被包装的函数

    Returns:
        Callable[P, R]: 包装后的函数
    """

    @functools.wraps(func)
    async def wrapper(self, *args: P.args, **kwargs: P.kwargs):
        from ..context import enter_context

        sys.audit("CallAriadneAPI", func.__name__, args, kwargs)

        with enter_context(app=self):
            return await func(self, *args, **kwargs)

    return wrapper


def gen_subclass(cls: Type[T]) -> Generator[Type[T], None, None]:
    """生成某个类的所有子类 (包括其自身)

    Args:
        cls (Type[T]): 类

    Yields:
        Type[T]: 子类
    """
    yield cls
    for sub_cls in cls.__subclasses__():
        if TYPE_CHECKING:
            assert issubclass(sub_cls, cls)
        yield from gen_subclass(sub_cls)


def wrap_bracket(string: str) -> str:
    """在字符串中转义中括号括号"""
    return string.replace("[", "\\u005b").replace("]", "\\u005d")


def const_call(val: T) -> Callable[[], T]:
    """生成一个返回常量的 Callable

    Args:
        val (T): 常量

    Returns:
        Callable[[], T]: 返回的函数
    """
    return lambda: val


def assert_on_(pre_condition: bool, condition: bool, *message: Any) -> Union[None, NoReturn]:
    """检查条件是否成立, 如果不成立则抛出 ValueError

    Args:
        pre_condition(bool): 前置条件
        condition (bool): 条件语句
        message (Any, optional): 附带的消息.

    Returns:
        Union[None, NoReturn]: 无返回值
    """
    if pre_condition:
        if not condition:
            raise ValueError(*message)


def assert_(condition: bool, *message: Any) -> Union[None, NoReturn]:
    """引发 ValueError 的断言

    Args:
        condition (bool): 条件语句
        *message (Any): 附带的消息.

    Returns:
        Union[None, NoReturn]: 无返回值
    """
    if not condition:
        raise ValueError(*message)


def assert_not_(condition: bool, *message: Any) -> Union[None, NoReturn]:
    """检查条件是否成立, 如果不成立则抛出 ValueError

    Args:
        condition (bool): 条件语句
        *message (Any): 附带的消息.

    Returns:
        Union[None, NoReturn]: 无返回值
    """
    if condition:
        raise ValueError(*message)


def eval_ctx(
    layer: int = 0, globals_: Optional[DictStrAny] = None, locals_: Optional[DictStrAny] = None
) -> Tuple[DictStrAny, DictStrAny]:
    """获取一个上下文的全局和局部变量

    Args:
        layer (int, optional): 层数. Defaults to 0.
        globals_ (Optional[DictStrAny], optional): 全局变量. Defaults to None.
        locals_ (Optional[DictStrAny], optional): 局部变量. Defaults to None.

    Returns:
        Tuple[DictStrAny, DictStrAny]: 全局和局部变量字典.
    """
    frame = inspect.stack()[layer + 1].frame  # add the current frame
    global_dict, local_dict = frame.f_globals, frame.f_locals
    global_dict.update(globals_ or {})
    local_dict.update(locals_ or {})
    return global_dict, local_dict


T_Callable = TypeVar("T_Callable", bound=Callable)


async def await_predicate(predicate: Callable[[], bool], interval: float = 0.01) -> None:
    """异步阻塞至满足 predicate 为 True

    Args:
        predicate (Callable[[], bool]): 判断条件
        interval (float, optional): 每次检查间隔. Defaults to 0.01.
    """
    while not predicate():
        await asyncio.sleep(interval)


async def yield_with_timeout(
    getter_coro: Callable[[], Coroutine[None, None, T]],
    predicate: Callable[[], bool],
    await_length: float = 0.2,
) -> AsyncIterator[T]:
    """在满足 predicate 时返回 getter_coro() 的值

    Args:
        getter_coro (Callable[[], Coroutine[None, None, T]]): 要循环返回的协程函数.
        predicate (Callable[[], bool]): 条件回调函数.
        await_length (float, optional): 等待目前协程的时长. 默认 0.2s.

    Yields:
        T: getter_coro 的返回值
    """
    last_tsk = None
    while predicate():
        last_tsk = last_tsk or {asyncio.create_task(getter_coro())}
        done, last_tsk = await asyncio.wait(last_tsk, timeout=await_length)
        if not done:
            continue
        for t in done:
            res = await t
            yield res
    if last_tsk:
        for tsk in last_tsk:
            tsk.cancel()


def deprecated(remove_ver: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """标注一个方法 / 函数已被弃用

    Args:
        remove_ver (str): 将被移除的版本.

    Returns:
        Callable[[T_Callable], T_Callable]: 包装器.
    """

    def out_wrapper(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            warnings.warn(DeprecationWarning(f"{func.__qualname__} will be removed in {remove_ver}!"))
            logger.warning(f"Deprecated function: {func.__qualname__}")
            logger.warning(f"{func.__qualname__} will be removed in {remove_ver}!")
            return func(*args, **kwargs)

        return wrapper

    return out_wrapper


def resolve_dispatchers_mixin(dispatchers: List[T_Dispatcher]) -> List[T_Dispatcher]:
    """解析 dispatcher list 的 mixin

    Args:
        dispatchers (List[T_Dispatcher]): dispatcher 列表

    Returns:
        List[T_Dispatcher]: 解析后的 dispatcher 列表
    """
    result = []
    for dispatcher in dispatchers:
        result.extend(dispatcher_mixin_handler(dispatcher))
    return result


class ConstantDispatcher(BaseDispatcher):
    """分发常量给指定名称的参数"""

    def __init__(self, context: ContextVar[Dict[str, Any]]) -> None:
        self.ctx_var = context

    async def catch(self, interface: DispatcherInterface):
        if interface.name in self.ctx_var.get():
            return self.ctx_var.get()[interface.name]


class Dummy:
    """Dummy 类, 对所有调用返回 None. (可以预设某些值)"""

    def __init__(self, **kwds):
        for k, v in kwds.items():
            self.__setattr__(k, v)

    def __getattr__(self, *_, **__):
        return self

    def __call__(self, *_, **__):
        return self

    def __await__(self):
        yield
        return self


def signal_handler(callback: Callable[[], None], one_time: bool = True) -> None:
    """注册信号处理器
    Args:
        callback (Callable[[], None]): 信号处理器
        one_time (bool, optional): 是否只执行一次. 默认为 True.
    Returns:
        None
    """
    import signal
    import threading

    if not threading.main_thread().ident == threading.current_thread().ident:
        return

    HANDLED_SIGNAL = (signal.SIGINT, signal.SIGTERM)

    for sig in HANDLED_SIGNAL:
        handler = signal.getsignal(sig)

        def handler_wrapper(sig_num, frame):
            if handler:
                handler(sig_num, frame)
            callback()
            if one_time:
                signal.signal(sig_num, handler)

        signal.signal(sig, handler_wrapper)


# Import layout
from . import async_exec  # noqa: F401, E402
from .async_exec import (  # noqa: F401, E402
    IS_MAIN_PROCESS,
    ParallelExecutor,
    cpu_bound,
    io_bound,
)
